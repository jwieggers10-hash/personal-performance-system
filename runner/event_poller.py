#!/usr/bin/env python3
"""Detect-only AthleteData event poller (Phase 4.2).

Polls Garmin activities, Hevy workout events and Apple Health body
composition through the read-only AthleteData MCP client, and records what is
new, edited, deleted, held or superseded in a SQLite store under runtime/.

It never invokes Claude, never sends Telegram messages, and never stores
workout contents, measurements or credentials. Feedback eligibility is
recorded for a later phase to act on; nothing here acts on it.

Specification: runner/event-poller.md.

Usage:
    python3 runner/event_poller.py --once        # poll once, persist state
    python3 runner/event_poller.py --dry-run     # poll once against an in-memory copy; nothing persisted
    python3 runner/event_poller.py --status      # print source health and event counts (no identifiers)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Optional

from zoneinfo import ZoneInfo

import athletedata_client as adc
import event_store as es
from workflow_runner import (
    DuplicateExecutionError,
    WorkflowLock,
    default_repo_root,
    load_env_file,
    redact_secrets,
)


# --------------------------------------------------------------------------
# Policy constants (see runner/event-poller.md)
# --------------------------------------------------------------------------

SOURCES = ("garmin", "hevy", "apple_health")
SOURCE_TOOLS = {
    "garmin": "garmin_get_activities",
    "hevy": "hevy_get_workout_events",
    "apple_health": "apple_health_get_body_composition",
}

GARMIN_WINDOW_DAYS = 3
GARMIN_RECONCILIATION_DAYS = 7
GARMIN_LIMIT = 200
APPLE_WINDOW_DAYS = 4
APPLE_RECONCILIATION_DAYS = 14
HEVY_CURSOR_OVERLAP = timedelta(hours=2)
HEVY_INITIAL_LOOKBACK = timedelta(days=14)
HEVY_PAGE_SIZE = 10
HEVY_MAX_PAGES = 20

# A record first seen whose event date is more than this many days before
# the athlete's local today is history arriving late, not a new event.
BACKFILL_DAYS = 3
# Unmatched Garmin strength activities wait this long for a Hevy workout.
STRENGTH_HOLD = timedelta(hours=24)
# Garmin/Hevy strength match: time ranges must overlap, allowing this slack
# at either end; when either end time is unknown, starts must be this close.
MATCH_RANGE_SLACK = timedelta(minutes=15)
MATCH_START_TOLERANCE = timedelta(minutes=30)
# Consecutive complete Garmin polls in which a stored activity inside the
# queried window is absent before it is flagged (never deleted).
MISSING_THRESHOLD = 3
FAILING_THRESHOLD = 4
LOCK_STALE_SECONDS = 900

# Apple Health body composition has not been observed with real records yet
# (Phase 4.1: no data in 30 days), so its parser is tolerant and its events
# are never feedback-eligible until this is set after validation.
APPLE_HEALTH_STRUCTURE_VERIFIED = False
# Hevy "deleted" events have not been observed yet; they are recorded (the
# row is kept, marked deleted) with an unverified_structure detail.
HEVY_DELETE_STRUCTURE_VERIFIED = False

GARMIN_FINGERPRINT_FIELDS = (
    "activityType", "activityName", "startTimeInSeconds", "startTimeOffsetInSeconds", "durationInSeconds",
    "distanceInMeters", "averageHeartRateInBeatsPerMinute", "maxHeartRateInBeatsPerMinute",
    "activeKilocalories", "totalElevationGainInMeters", "steps", "deviceName", "isWebUpload",
)
APPLE_METRIC_LISTS = (
    ("weight", "body_weight"), ("bodyFat", "body_fat"), ("body_fat", "body_fat"), ("bodyFatPercentage", "body_fat"),
)
APPLE_DATE_KEYS = ("date", "local_date", "startDate", "start_date", "day", "timestamp", "time",
                   "recorded_at", "measured_at")
APPLE_VALUE_KEYS = ("value", "kg", "weight_kg", "qty", "quantity", "fraction", "percent")
APPLE_ID_KEYS = ("uuid", "id")

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def iso_utc(moment: datetime) -> str:
    return moment.astimezone(dt_timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[datetime]:
    """Timezone-aware datetimes only; naive or malformed values are unknown."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def valid_date(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _DATE.match(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def safe_token(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _SAFE_TOKEN.match(value) else None


def finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def is_strength(activity_type: Optional[str]) -> bool:
    return bool(activity_type) and "STRENGTH" in activity_type.upper()


def is_backfill(local_date: Optional[str], today: date) -> bool:
    # Unknown dates are treated as backfill: suppress rather than guess.
    if local_date is None:
        return True
    return date.fromisoformat(local_date) < today - timedelta(days=BACKFILL_DAYS)


# --------------------------------------------------------------------------
# Parsing (pure; no store or network access)
# --------------------------------------------------------------------------

@dataclass
class Observation:
    event_key: str
    event_type: str
    local_date: Optional[str]
    fingerprint: str
    activity_type: Optional[str] = None
    start_time_utc: Optional[str] = None
    end_time_utc: Optional[str] = None
    source_updated_at: Optional[str] = None
    deleted: bool = False


@dataclass
class ParsedBatch:
    observations: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    truncated: bool = False
    page_count: int = 1

    def bump(self, name: str, by: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + by

    @property
    def unrecognized(self) -> bool:
        records = self.counts.get("records", 0)
        unusable = self.counts.get("unparseable", 0) + self.counts.get("unidentifiable", 0)
        return records > 0 and unusable == records


def parse_garmin_activities(payload: Any, tz: ZoneInfo, fp: es.Fingerprinter) -> Optional[ParsedBatch]:
    truncated = False
    if isinstance(payload, dict):
        activities = payload.get("activities", payload.get("records"))
        truncated = payload.get("truncated") is True
    else:
        activities = payload
    if not isinstance(activities, list):
        return None

    batch = ParsedBatch(truncated=truncated)
    seen: set = set()
    for activity in activities:
        batch.bump("records")
        if not isinstance(activity, dict):
            batch.bump("unparseable")
            continue
        raw_id = activity.get("activityId", activity.get("summaryId"))
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not str(raw_id).strip():
            batch.bump("unidentifiable")
            continue
        key = str(raw_id).strip()
        if key in seen:
            batch.bump("duplicates")
            continue
        seen.add(key)

        start_epoch = finite_number(activity.get("startTimeInSeconds"))
        start = datetime.fromtimestamp(start_epoch, dt_timezone.utc) if start_epoch is not None else None
        duration = finite_number(activity.get("durationInSeconds"))
        end = start + timedelta(seconds=duration) if start is not None and duration is not None and duration >= 0 \
            else None
        local_date = valid_date(activity.get("local_date"))
        if local_date is None and start is not None:
            offset = finite_number(activity.get("startTimeOffsetInSeconds"))
            local = start + timedelta(seconds=offset) if offset is not None else start.astimezone(tz)
            local_date = local.date().isoformat()

        batch.observations.append(Observation(
            event_key=key,
            event_type="garmin_activity",
            local_date=local_date,
            fingerprint=fp.digest({name: activity.get(name) for name in GARMIN_FINGERPRINT_FIELDS}),
            activity_type=safe_token(activity.get("activityType")),
            start_time_utc=iso_utc(start) if start else None,
            end_time_utc=iso_utc(end) if end else None,
        ))
    return batch


def parse_hevy_events(payload: Any, tz: ZoneInfo, fp: es.Fingerprinter) -> Optional[ParsedBatch]:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    page_count = payload.get("page_count")
    batch = ParsedBatch(page_count=page_count if isinstance(page_count, int) and not isinstance(page_count, bool)
                        and page_count >= 0 else 1)

    for event in payload["events"]:
        batch.bump("records")
        if not isinstance(event, dict):
            batch.bump("unparseable")
            continue
        event_type = event.get("type")
        workout = event.get("workout") if isinstance(event.get("workout"), dict) else None

        if event_type == "updated":
            if workout is None:
                batch.bump("unparseable")
                continue
            workout_id = workout.get("id")
            if not isinstance(workout_id, str) or not workout_id.strip():
                batch.bump("unidentifiable")
                continue
            start = parse_iso(workout.get("start_time"))
            end = parse_iso(workout.get("end_time"))
            updated = parse_iso(workout.get("updated_at")) or parse_iso(workout.get("created_at"))
            batch.observations.append(Observation(
                event_key=workout_id.strip(),
                event_type="hevy_workout",
                local_date=start.astimezone(tz).date().isoformat() if start else None,
                fingerprint=fp.digest(workout),
                start_time_utc=iso_utc(start) if start else None,
                end_time_utc=iso_utc(end) if end else None,
                source_updated_at=iso_utc(updated) if updated else None,
            ))
        elif event_type == "deleted":
            workout_id = event.get("id") or event.get("workout_id") or (workout or {}).get("id")
            if not isinstance(workout_id, str) or not workout_id.strip():
                batch.bump("unidentifiable")
                continue
            deleted_at = parse_iso(event.get("deleted_at"))
            batch.bump("deleted_events")
            batch.observations.append(Observation(
                event_key=workout_id.strip(),
                event_type="hevy_workout",
                local_date=None,
                fingerprint="deleted",
                source_updated_at=iso_utc(deleted_at) if deleted_at else None,
                deleted=True,
            ))
        else:
            batch.bump("unparseable")
    return batch


def _normalize_measurement(event_type: str, value: float) -> str:
    if event_type == "body_fat":
        fraction = value / 100.0 if value > 1.0 else value
        return f"{fraction:.4f}"
    return f"{value:.2f}"


def parse_apple_body_composition(payload: Any, fp: es.Fingerprinter) -> Optional[ParsedBatch]:
    """Tolerant parser for a structure not yet verified against real records.
    The identity key is HMAC(source, metric, date, normalized value): the value
    is used only in memory to compute it and is never stored or logged."""
    if not isinstance(payload, dict):
        return None
    metric_lists = [(event_type, payload[name]) for name, event_type in APPLE_METRIC_LISTS
                    if isinstance(payload.get(name), list)]
    if not metric_lists:
        return None

    batch = ParsedBatch(truncated=payload.get("truncated") is True)
    seen_keys: set = set()
    seen_ids: set = set()
    for event_type, records in metric_lists:
        for record in records:
            batch.bump("records")
            if not isinstance(record, dict):
                batch.bump("unparseable")
                continue
            day = next((d for d in (valid_date(record.get(k)) for k in APPLE_DATE_KEYS) if d), None)
            value = next((v for v in (finite_number(record.get(k)) for k in APPLE_VALUE_KEYS) if v is not None),
                         None)
            if day is None or value is None:
                batch.bump("unparseable")
                continue
            record_id = next((record[k] for k in APPLE_ID_KEYS if isinstance(record.get(k), str) and record[k]),
                             None)
            if record_id is not None:
                if (event_type, record_id) in seen_ids:
                    batch.bump("duplicate_ids")
                    continue
                seen_ids.add((event_type, record_id))
            key = fp.digest(["apple_health", event_type, day, _normalize_measurement(event_type, value)])
            if key in seen_keys:
                batch.bump("duplicates")
                continue
            seen_keys.add(key)
            batch.observations.append(Observation(event_key=key, event_type=event_type, local_date=day,
                                                  fingerprint=key))
    return batch


# --------------------------------------------------------------------------
# Fetching (network; no store writes)
# --------------------------------------------------------------------------

@dataclass
class FetchResult:
    outcome: str  # "ok", "no_data", or "structure_unrecognized"
    batch: Optional[ParsedBatch] = None
    complete: bool = False
    window: Optional[tuple] = None
    reconciliation: bool = False
    calls: int = 0
    new_cursor: Optional[str] = None


def _interpret(result: adc.ToolResult, parser: Callable[[Any], Optional[ParsedBatch]]) -> "tuple[str, Optional[ParsedBatch]]":
    if result.kind != "json":
        return "no_data", None  # prose or empty: never parsed, never stored
    batch = parser(result.payload)
    if batch is None or batch.unrecognized:
        return "structure_unrecognized", None
    return "ok", batch


def fetch_garmin(client, state: dict, *, now: datetime, today: date, tz: ZoneInfo,
                 fp: es.Fingerprinter) -> FetchResult:
    reconciliation = state.get("last_reconciliation_date") != today.isoformat()
    days = GARMIN_RECONCILIATION_DAYS if reconciliation else GARMIN_WINDOW_DAYS
    start = today - timedelta(days=days - 1)
    result = client.call_tool(SOURCE_TOOLS["garmin"], {
        "start_date": start.isoformat(), "end_date": today.isoformat(), "limit": GARMIN_LIMIT,
    })
    outcome, batch = _interpret(result, lambda payload: parse_garmin_activities(payload, tz, fp))
    return FetchResult(outcome, batch, complete=outcome == "ok" and not batch.truncated,
                       window=(start.isoformat(), today.isoformat()), reconciliation=reconciliation, calls=1)


def fetch_apple_health(client, state: dict, *, now: datetime, today: date, tz: ZoneInfo,
                       fp: es.Fingerprinter) -> FetchResult:
    reconciliation = state.get("last_reconciliation_date") != today.isoformat()
    days = APPLE_RECONCILIATION_DAYS if reconciliation else APPLE_WINDOW_DAYS
    start = today - timedelta(days=days - 1)
    result = client.call_tool(SOURCE_TOOLS["apple_health"], {
        "start_date": start.isoformat(), "end_date": today.isoformat(), "limit": 500,
    })
    outcome, batch = _interpret(result, lambda payload: parse_apple_body_composition(payload, fp))
    return FetchResult(outcome, batch, complete=outcome == "ok" and not batch.truncated,
                       window=(start.isoformat(), today.isoformat()), reconciliation=reconciliation, calls=1)


def fetch_hevy(client, state: dict, *, now: datetime, today: date, tz: ZoneInfo,
               fp: es.Fingerprinter) -> FetchResult:
    """Reads every page before anything is written. Any page failing (or the
    page cap being hit) raises, so the caller writes nothing and the cursor
    stays where it was."""
    cursor = parse_iso(state.get("cursor"))
    since = (cursor - HEVY_CURSOR_OVERLAP) if cursor else (now - HEVY_INITIAL_LOOKBACK)
    since_text = since.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    merged: dict = {}
    combined = ParsedBatch()
    page = 1
    calls = 0
    while True:
        if page > HEVY_MAX_PAGES:
            raise adc.AthleteDataError("Hevy pagination exceeded the page limit", "incomplete_pagination")
        result = client.call_tool(SOURCE_TOOLS["hevy"], {"since": since_text, "page": page,
                                                         "pageSize": HEVY_PAGE_SIZE})
        calls += 1
        outcome, batch = _interpret(result, lambda payload: parse_hevy_events(payload, tz, fp))
        if outcome != "ok":
            if page == 1:
                return FetchResult(outcome, calls=calls)
            raise adc.ProtocolError("Hevy page after the first returned no usable data", "incomplete_pagination")

        for name, value in batch.counts.items():
            combined.bump(name, value)
        for observation in batch.observations:
            # A workout can appear on two pages if events shift mid-read;
            # keep the most recent version of it.
            previous = merged.get(observation.event_key)
            if previous is None or (observation.source_updated_at or "") >= (previous.source_updated_at or ""):
                merged[observation.event_key] = observation
        if page >= batch.page_count or not batch.counts.get("records"):
            break
        page += 1

    combined.observations = list(merged.values())
    combined.counts["pages"] = page
    latest = max((o.source_updated_at for o in combined.observations if o.source_updated_at), default=None)
    current = state.get("cursor")
    new_cursor = max(filter(None, (current, latest)), default=None)
    return FetchResult("ok", combined, complete=True, calls=calls, new_cursor=new_cursor)


FETCHERS = {"garmin": fetch_garmin, "hevy": fetch_hevy, "apple_health": fetch_apple_health}


# --------------------------------------------------------------------------
# Applying observations (store writes; always inside one transaction)
# --------------------------------------------------------------------------

def _classify_new(source: str, observation: Observation, *, now: datetime, today: date,
                  baseline: bool) -> "tuple[str, str, int, Optional[str]]":
    """(state, feedback_status, feedback_eligible, hold_until) for a record
    seen for the first time."""
    if observation.deleted:
        return "deleted", "suppressed_deleted", 0, None
    if baseline:
        return "baseline", "suppressed_baseline", 0, None
    if is_backfill(observation.local_date, today):
        return "new", "suppressed_backfill", 0, None
    if source == "apple_health" and not APPLE_HEALTH_STRUCTURE_VERIFIED:
        return "new", "unverified_structure", 0, None
    if source == "garmin" and is_strength(observation.activity_type):
        return "held", "held", 0, iso_utc(now + STRENGTH_HOLD)
    return "new", "pending", 1, None


def apply_fetch(store: es.EventStore, source: str, fetch: FetchResult, *, now: datetime, today: date,
                baseline: bool) -> dict:
    counts: dict = {}

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    now_text = iso_utc(now)
    seen: set = set()
    for observation in fetch.batch.observations:
        seen.add(observation.event_key)
        row = store.get_event(source, observation.event_key)

        if row is None:
            state, feedback_status, eligible, hold_until = _classify_new(source, observation, now=now, today=today,
                                                                         baseline=baseline)
            store.insert_event(
                source=source, event_key=observation.event_key, event_type=observation.event_type,
                activity_type=observation.activity_type, local_date=observation.local_date,
                start_time_utc=observation.start_time_utc, end_time_utc=observation.end_time_utc,
                source_updated_at=observation.source_updated_at, fingerprint=observation.fingerprint,
                state=state, feedback_eligible=eligible, feedback_status=feedback_status, hold_until=hold_until,
                first_seen_at=now_text, last_seen_at=now_text,
            )
            change = "deleted" if observation.deleted else ("baseline" if baseline else "new")
            store.record_change(source, observation.event_key, change, now_text, feedback_status)
            bump(change)
            bump(f"feedback_{feedback_status}")
            continue

        updates: dict = {"last_seen_at": now_text}
        if row["missing_count"]:
            updates["missing_count"] = 0
            if row["missing_count"] >= MISSING_THRESHOLD:
                store.record_change(source, observation.event_key, "reappeared", now_text)
                bump("reappeared")

        if observation.deleted:
            if row["state"] != "deleted":
                updates.update(state="deleted", feedback_eligible=0, feedback_status="suppressed_deleted",
                               source_updated_at=observation.source_updated_at or row["source_updated_at"])
                store.record_change(source, observation.event_key, "deleted", now_text,
                                    None if HEVY_DELETE_STRUCTURE_VERIFIED else "unverified_structure")
                bump("deleted")
            else:
                bump("unchanged")
        elif observation.fingerprint != row["fingerprint"]:
            stale = (source == "hevy" and row["source_updated_at"] and observation.source_updated_at
                     and observation.source_updated_at <= row["source_updated_at"])
            if stale:
                bump("stale_ignored")
            else:
                restored = row["state"] == "deleted"
                updates.update(
                    fingerprint=observation.fingerprint, source_updated_at=observation.source_updated_at,
                    local_date=observation.local_date, start_time_utc=observation.start_time_utc,
                    end_time_utc=observation.end_time_utc, activity_type=observation.activity_type,
                    state=row["state"] if row["state"] in ("held", "superseded") else "edited",
                )
                store.record_change(source, observation.event_key, "restored" if restored else "edited", now_text)
                bump("restored" if restored else "edited")
        else:
            bump("unchanged")
        store.update_event(source, observation.event_key, **updates)

    if source == "garmin" and fetch.complete and fetch.window:
        _detect_missing(store, source, fetch.window, seen, now_text, bump)
    if source == "apple_health" and fetch.window:
        _detect_conflicts(store, fetch.window, now_text, bump)
    return counts


def _detect_missing(store, source, window, seen, now_text, bump) -> None:
    for row in store.events_in_date_window(source, *window):
        if row["event_key"] in seen or row["state"] == "deleted":
            continue
        missing = row["missing_count"] + 1
        store.update_event(source, row["event_key"], missing_count=missing)
        bump("absent_this_poll")
        if missing == MISSING_THRESHOLD:
            store.record_change(source, row["event_key"], "missing_suspected", now_text)
            bump("missing_suspected")


def _detect_conflicts(store, window, now_text, bump) -> None:
    groups: dict = {}
    for row in store.events_in_date_window("apple_health", *window):
        if row["state"] != "deleted":
            groups.setdefault((row["local_date"], row["event_type"]), []).append(row)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        bump("conflicting_days")
        for row in rows:
            if not row["conflict"]:
                store.update_event("apple_health", row["event_key"], conflict=1)
                store.record_change("apple_health", row["event_key"], "conflict_detected", now_text,
                                    "same_day_differing_values")


def _find_overlapping_hevy(garmin_row, hevy_rows) -> Optional[Any]:
    garmin_start = parse_iso(garmin_row["start_time_utc"])
    garmin_end = parse_iso(garmin_row["end_time_utc"])
    if garmin_start is None:
        return None  # never match on date alone
    best, best_gap = None, None
    for hevy in hevy_rows:
        hevy_start = parse_iso(hevy["start_time_utc"])
        hevy_end = parse_iso(hevy["end_time_utc"])
        if hevy_start is None:
            continue
        if garmin_end is not None and hevy_end is not None:
            matched = (garmin_start - MATCH_RANGE_SLACK <= hevy_end
                       and hevy_start <= garmin_end + MATCH_RANGE_SLACK)
        else:
            matched = abs(garmin_start - hevy_start) <= MATCH_START_TOLERANCE
        if matched:
            gap = abs(garmin_start - hevy_start)
            if best_gap is None or gap < best_gap:
                best, best_gap = hevy, gap
    return best


def reconcile_strength_duplicates(store: es.EventStore, *, now: datetime, today: date) -> dict:
    """Hevy is authoritative for strength. A Garmin strength activity still
    awaiting feedback is superseded by an overlapping Hevy workout, or
    released after the hold only once Hevy has been polled successfully
    since the hold expired (so a Hevy outage never releases it early)."""
    counts: dict = {}
    candidates = [row for row in store.events_with_feedback_status("garmin", ("held", "pending"))
                  if is_strength(row["activity_type"])]
    if not candidates:
        return counts
    hevy_rows = store.timed_events("hevy")
    hevy_success = parse_iso(store.get_source_state("hevy").get("last_success_at"))
    now_text = iso_utc(now)

    for row in candidates:
        match = _find_overlapping_hevy(row, hevy_rows)
        if match is not None:
            store.update_event("garmin", row["event_key"], state="superseded", feedback_eligible=0,
                               feedback_status="suppressed_superseded", superseded_by=f"hevy:{match['event_key']}")
            store.record_change("garmin", row["event_key"], "superseded", now_text, "hevy_time_overlap")
            counts["superseded"] = counts.get("superseded", 0) + 1
            continue
        if row["state"] != "held":
            continue
        hold_until = parse_iso(row["hold_until"])
        if hold_until and now >= hold_until and hevy_success and hevy_success >= hold_until:
            feedback_status = "suppressed_backfill" if is_backfill(row["local_date"], today) else "pending"
            store.update_event("garmin", row["event_key"], state="new", feedback_status=feedback_status,
                               feedback_eligible=1 if feedback_status == "pending" else 0)
            store.record_change("garmin", row["event_key"], "released", now_text, "no_hevy_match_after_hold")
            counts["released"] = counts.get("released", 0) + 1
        else:
            counts["still_held"] = counts.get("still_held", 0) + 1
    return counts


# --------------------------------------------------------------------------
# Source state
# --------------------------------------------------------------------------

def _record_success(store, source, state, fetch: FetchResult, *, now_text: str, today: date, baseline: bool) -> None:
    fields: dict = {"last_attempt_at": now_text}
    if fetch.outcome == "structure_unrecognized":
        failures = state["consecutive_failures"] + 1
        fields.update(health="structure_unrecognized", last_error_category="structure_unrecognized",
                      consecutive_failures=failures)
    else:
        fields.update(health="ok" if fetch.outcome == "ok" else "no_data", last_success_at=now_text,
                      last_error_category=None, consecutive_failures=0)
        if fetch.reconciliation and (fetch.complete or fetch.outcome == "no_data"):
            fields["last_reconciliation_date"] = today.isoformat()
        if baseline:
            fields["baseline_completed_at"] = now_text
        if fetch.new_cursor and fetch.complete:
            fields["cursor"] = fetch.new_cursor
    store.update_source_state(source, **fields)


def _record_failure(store, source, state, category: str, now_text: str) -> None:
    failures = state["consecutive_failures"] + 1
    with store.transaction():
        store.update_source_state(source, last_attempt_at=now_text, consecutive_failures=failures,
                                  last_error_category=category,
                                  health="failing" if failures >= FAILING_THRESHOLD else "degraded")


# --------------------------------------------------------------------------
# One poll
# --------------------------------------------------------------------------

def run_poll(store: es.EventStore, *, client=None, client_error: Optional[adc.AthleteDataError] = None,
             now: datetime, tz: ZoneInfo, fp: es.Fingerprinter, log: Callable[..., None],
             dry_run: bool = False) -> dict:
    started = time.monotonic()
    today = now.astimezone(tz).date()
    now_text = iso_utc(now)
    log(event="poll_start", dry_run=dry_run, local_date=today.isoformat())

    setup_error = client_error
    tool_checks: dict = {}
    if setup_error is None:
        try:
            tool_checks = client.verify_tools([SOURCE_TOOLS[s] for s in SOURCES])
        except adc.AthleteDataError as exc:
            setup_error = exc

    summary: dict = {"dry_run": dry_run, "local_date": today.isoformat(), "sources": {}}
    for source in SOURCES:
        source_started = time.monotonic()
        state = store.get_source_state(source)
        entry: dict = {"source": source}
        try:
            if setup_error is not None:
                raise setup_error
            tool_error = tool_checks.get(SOURCE_TOOLS[source])
            if tool_error is not None:
                raise tool_error
            fetch = FETCHERS[source](client, state, now=now, today=today, tz=tz, fp=fp)
            baseline = state["baseline_completed_at"] is None and fetch.outcome == "ok"
            counts: dict = {}
            with store.transaction():
                if fetch.outcome == "ok":
                    counts = apply_fetch(store, source, fetch, now=now, today=today, baseline=baseline)
                _record_success(store, source, state, fetch, now_text=now_text, today=today, baseline=baseline)
            entry.update(outcome=fetch.outcome, baseline=baseline, calls=fetch.calls,
                         reconciliation=fetch.reconciliation, complete=fetch.complete, changes=counts,
                         parsed=dict(fetch.batch.counts) if fetch.batch else {})
            if fetch.window:
                entry["window_days"] = (date.fromisoformat(fetch.window[1])
                                        - date.fromisoformat(fetch.window[0])).days + 1
        except adc.AthleteDataError as exc:
            entry.update(outcome="failed", error_category=exc.category)
            _safe_record_failure(store, source, state, exc.category, now_text)
        except (sqlite3.Error, es.StoreError):
            entry.update(outcome="failed", error_category="store_error")
            _safe_record_failure(store, source, state, "store_error", now_text)
        entry["duration_ms"] = int((time.monotonic() - source_started) * 1000)
        log(event="source_poll", **entry)
        summary["sources"][source] = entry

    try:
        with store.transaction():
            cross = reconcile_strength_duplicates(store, now=now, today=today)
    except (sqlite3.Error, es.StoreError):
        cross = {"error_category": "store_error"}
    summary["cross_source"] = cross
    log(event="cross_source", **cross)

    outcomes = [entry["outcome"] for entry in summary["sources"].values()]
    failed = sum(1 for outcome in outcomes if outcome in ("failed", "structure_unrecognized"))
    summary["outcome"] = "failed" if failed == len(outcomes) else ("partial" if failed else "ok")
    if client is not None:
        summary["http_requests"] = client.request_count
        summary["http_retries"] = client.retry_count
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    try:
        with store.transaction():
            store.record_poll_run(now_text, iso_utc(datetime.now(dt_timezone.utc)), summary["outcome"], summary)
    except sqlite3.Error:
        pass
    log(event="poll_end", outcome=summary["outcome"], duration_ms=summary["duration_ms"],
        http_requests=summary.get("http_requests", 0), http_retries=summary.get("http_retries", 0))
    return summary


def _safe_record_failure(store, source, state, category, now_text) -> None:
    try:
        _record_failure(store, source, state, category, now_text)
    except (sqlite3.Error, es.StoreError):
        pass  # the failure is already in the log line; never let it mask the poll


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def make_logger(log_file: Optional[Path]) -> Callable[..., None]:
    """JSON lines to stderr (the journal under systemd) and optionally a file.
    Callers pass counts, durations, categories and fixed tokens only."""
    def log(**fields: Any) -> None:
        safe = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in fields.items()}
        safe.setdefault("component", "event_poller")
        safe.setdefault("logged_at", datetime.now(dt_timezone.utc).isoformat())
        line = json.dumps(safe, sort_keys=True)
        print(line, file=sys.stderr)
        if log_file:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
    return log


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect-only AthleteData event poller (no Claude, no Telegram).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="poll once and persist state")
    mode.add_argument("--dry-run", action="store_true",
                      help="poll once against an in-memory copy of the state; nothing is persisted")
    mode.add_argument("--status", action="store_true", help="print source health and event counts, then exit")
    parser.add_argument("--repo-root", default=None, help="override the resolved repository root")
    parser.add_argument("--env-file", default=None, help="env file (default runner/config.env)")
    parser.add_argument("--db-path", default=None, help="event store path (default runtime/events.sqlite3)")
    parser.add_argument("--claude-config", default=None,
                        help="Claude Code config holding the athletedata MCP registration (default ~/.claude.json)")
    return parser


def main(argv: Optional[list] = None, *, client_factory: Optional[Callable] = None,
         now_fn: Optional[Callable[[], datetime]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()
    env = load_env_file(Path(args.env_file) if args.env_file else repo_root / "runner" / "config.env")
    env.update(os.environ)

    tz_name = env.get("ATHLETE_TIMEZONE", "").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        print(json.dumps({"outcome": "failed", "error_category": "config_error",
                          "error": "ATHLETE_TIMEZONE missing or invalid"}))
        return 2
    log_file_value = env.get("EVENT_POLLER_LOG_FILE", "").strip()
    log = make_logger(Path(log_file_value) if log_file_value else None)

    runtime_dir = repo_root / "runtime"
    db_path = Path(args.db_path) if args.db_path else runtime_dir / "events.sqlite3"
    key_path = db_path.with_name("event-poller.key")

    if args.status:
        if not db_path.exists():
            print(json.dumps({"status": "no_database"}))
            return 0
        store = es.EventStore.open_readonly(db_path)
        try:
            print(json.dumps(store.status_summary(), indent=2, sort_keys=True))
        finally:
            store.close()
        return 0

    lock = None
    if args.once:
        lock = WorkflowLock(runtime_dir / "locks", "event-poller", LOCK_STALE_SECONDS)
        try:
            lock.acquire()
        except DuplicateExecutionError:
            log(event="poll_skipped", reason="lock_held")
            return 0

    try:
        try:
            key = es.load_fingerprint_key(key_path, create=False)
            if key is None:
                if db_path.exists() and db_path.stat().st_size > 0:
                    # A new key would make every stored fingerprint look edited.
                    raise es.StoreError("fingerprint key missing for an existing event store")
                key = es.load_fingerprint_key(key_path, create=True) if args.once else secrets.token_bytes(32)
            fp = es.Fingerprinter(key)
            store = es.EventStore.open(db_path) if args.once else es.EventStore.open_snapshot(db_path)
        except (es.StoreError, sqlite3.Error, OSError) as exc:
            category = getattr(exc, "category", "store_error")
            log(event="poll_end", outcome="failed", error_category=category)
            print(json.dumps({"outcome": "failed", "error_category": category}))
            return 1

        client, client_error = None, None
        try:
            if client_factory is not None:
                client = client_factory()
            else:
                claude_config = Path(args.claude_config) if args.claude_config else adc.default_claude_config_path()
                client = adc.AthleteDataClient(adc.load_mcp_endpoint(claude_config, repo_root))
        except adc.AthleteDataError as exc:
            client_error = exc

        try:
            now = now_fn() if now_fn else datetime.now(dt_timezone.utc)
            summary = run_poll(store, client=client, client_error=client_error, now=now, tz=tz, fp=fp, log=log,
                               dry_run=bool(args.dry_run))
        finally:
            store.close()
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary["outcome"] == "failed" else 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
