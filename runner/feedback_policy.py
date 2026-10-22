#!/usr/bin/env python3
"""Pure decision rules for the feedback dispatcher: eligibility, staleness,
quiet hours (Europe/Amsterdam, DST-aware), grouping and feedback level.

No I/O, no Claude, no Telegram. Every function takes the current time
explicitly so the rules are testable across DST transitions.

Specification: runner/feedback-dispatcher.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------
# Policy constants (see runner/feedback-dispatcher.md)
# --------------------------------------------------------------------------

# Four distinct times govern an event (see runner/feedback-dispatcher.md):
#   occurrence     -- when the activity happened (end, else start)
#   first seen     -- when the poller detected it (late AthleteData sync)
#   eligible since -- when it became feedback-eligible: first seen, or its
#                     release from the 24 h Garmin strength hold
#   notification window -- eligible since + NOTIFICATION_WINDOW
# A deliberate hold or delayed sync never counts against an event: the
# occurrence cap applies once, when the event is first accepted, and after
# that only the time since eligibility matters, so quiet hours and the
# Daily Review wait cannot expire an accepted event.
MAX_OCCURRENCE_AGE = timedelta(hours=48)
NOTIFICATION_WINDOW = timedelta(hours=24)
# Cutover: events already eligible at first activation get feedback only if
# they occurred within this window before it.
ACTIVATION_LOOKBACK = timedelta(hours=24)
# Wait this long after the poller first saw an event, so related events from
# the next poll tick (e.g. a Hevy session overlapping a Garmin activity) are
# grouped together instead of producing two messages.
SETTLE_DELAY = timedelta(minutes=10)
GROUP_GAP = timedelta(hours=3)
MAX_EVENTS_PER_BATCH = 6

QUIET_START = time(22, 0)
# Routine feedback is held until after the 07:00 Daily Review...
MORNING_RELEASE = time(7, 30)
# ...and, between MORNING_RELEASE and this deadline, until that review has
# actually finished. The deadline keeps a failing Daily Review (as in
# September 2026) from blocking feedback indefinitely.
DAILY_REVIEW_WAIT_DEADLINE = time(9, 0)

SIGNIFICANT_DURATION = timedelta(minutes=90)
RETURN_AFTER_GAP = timedelta(days=4)

TRAINING_SOURCES = ("garmin", "hevy")

UTC = timezone.utc


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Quiet hours
# --------------------------------------------------------------------------

def local_wall_time(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of a local wall-clock time. A wall time that does not
    exist (spring-forward gap) resolves to the first instant after the gap; an
    ambiguous one (fall-back overlap) to its first occurrence."""
    aware = datetime.combine(day, wall).replace(tzinfo=tz, fold=0)
    return aware.astimezone(UTC).astimezone(tz).astimezone(UTC)


def in_quiet_hours(now: datetime, tz: ZoneInfo) -> bool:
    local_time = now.astimezone(tz).time()
    return local_time >= QUIET_START or local_time < MORNING_RELEASE


def next_release(now: datetime, tz: ZoneInfo) -> datetime:
    """When routine feedback queued at `now` may be processed: `now` outside
    quiet hours, otherwise the next MORNING_RELEASE in local time."""
    local = now.astimezone(tz)
    if local.time() >= QUIET_START:
        return local_wall_time(local.date() + timedelta(days=1), MORNING_RELEASE, tz)
    if local.time() < MORNING_RELEASE:
        return local_wall_time(local.date(), MORNING_RELEASE, tz)
    return now.astimezone(UTC)


def morning_gate_open(now: datetime, tz: ZoneInfo, daily_review_settled: Callable[[date], bool]) -> bool:
    """Routine processing between MORNING_RELEASE and the deadline waits for
    today's Daily Review to have finished."""
    local = now.astimezone(tz)
    if MORNING_RELEASE <= local.time() < DAILY_REVIEW_WAIT_DEADLINE:
        return daily_review_settled(local.date())
    return True


def may_process(urgency: str, now: datetime, tz: ZoneInfo, daily_review_settled: Callable[[date], bool]) -> bool:
    """Genuine urgent alerts bypass quiet hours; everything else respects them."""
    if urgency == "urgent":
        return True
    if in_quiet_hours(now, tz):
        return False
    return morning_gate_open(now, tz, daily_review_settled)


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

def event_time(event: Any) -> Optional[datetime]:
    """Occurrence: the activity's end, else its start, else when the poller
    first saw it."""
    for column in ("end_time_utc", "start_time_utc", "first_seen_at"):
        moment = parse_iso(event[column])
        if moment is not None:
            return moment
    return None


def eligible_since(event: Any) -> Optional[datetime]:
    keys = event.keys() if hasattr(event, "keys") else ()
    if "eligible_since" in keys:
        moment = parse_iso(event["eligible_since"])
        if moment is not None:
            return moment
    return parse_iso(event["first_seen_at"])


def notification_deadline(event: Any) -> Optional[datetime]:
    since = eligible_since(event)
    return since + NOTIFICATION_WINDOW if since else None


@dataclass(frozen=True)
class Verdict:
    decision: str           # 'eligible', 'wait', 'expired', 'suppressed'
    reason: Optional[str]   # fixed vocabulary


def assess_event(event: Any, *, now: datetime, activated_at: Optional[datetime],
                 apple_health_verified: bool) -> Verdict:
    """Intake decision for a candidate event (feedback_eligible=1, status
    pending, no feedback item yet)."""
    structural = structural_problem(event, apple_health_verified=apple_health_verified)
    if structural:
        return Verdict("suppressed", structural)
    occurred, since = event_time(event), eligible_since(event)
    if occurred is None or since is None:
        return Verdict("expired", "unknown_event_time")
    if activated_at is not None and since < activated_at and occurred < activated_at - ACTIVATION_LOOKBACK:
        return Verdict("expired", "pre_activation")
    if now - occurred > MAX_OCCURRENCE_AGE:
        return Verdict("expired", "stale_occurrence")
    if now > since + NOTIFICATION_WINDOW:
        return Verdict("expired", "notification_window_passed")
    if now - since < SETTLE_DELAY:
        return Verdict("wait", "settling")
    return Verdict("eligible", None)


def structural_problem(event: Any, *, apple_health_verified: bool) -> Optional[str]:
    """Reasons an event must never receive feedback, whatever its age."""
    status = event["feedback_status"]
    if event["state"] == "deleted" or status == "suppressed_deleted":
        return "deleted"
    if event["state"] == "superseded" or status == "suppressed_superseded":
        return "superseded"
    if event["state"] == "baseline" or status == "suppressed_baseline":
        return "baseline"
    if status == "suppressed_backfill":
        return "backfill"
    if event["source"] == "apple_health" and (not apple_health_verified or status == "unverified_structure"):
        return "unverified_structure"
    if status != "pending" or not event["feedback_eligible"] or event["state"] not in ("new", "edited"):
        return "not_eligible"
    return None


def revalidate(event: Any, *, now: datetime, apple_health_verified: bool) -> Verdict:
    """Before generation and before delivery of an accepted event: it must
    still be structurally eligible and inside its notification window.
    Occurrence age and activation are not re-checked -- they were decided at
    intake, and re-checking them would expire events merely for waiting out
    quiet hours or the Daily Review."""
    structural = structural_problem(event, apple_health_verified=apple_health_verified)
    if structural:
        return Verdict("suppressed", structural)
    deadline = notification_deadline(event)
    if deadline is None:
        return Verdict("expired", "unknown_event_time")
    if now > deadline:
        return Verdict("expired", "notification_window_passed")
    return Verdict("eligible", None)


# --------------------------------------------------------------------------
# Grouping and level
# --------------------------------------------------------------------------

def group_events(events: Iterable[Any]) -> list:
    """Clusters events whose activity times are within GROUP_GAP of the
    previous one (a hybrid day's run and gym session, a double session),
    capped at MAX_EVENTS_PER_BATCH. Returns lists of events, in time order."""
    ordered = sorted(events, key=lambda e: (event_time(e) or datetime.max.replace(tzinfo=UTC), e["id"]))
    groups: list = []
    previous: Optional[datetime] = None
    for event in ordered:
        moment = event_time(event)
        if (groups and previous is not None and moment is not None and moment - previous <= GROUP_GAP
                and len(groups[-1]) < MAX_EVENTS_PER_BATCH):
            groups[-1].append(event)
        else:
            groups.append([event])
        previous = moment
    return groups


def duration(event: Any) -> Optional[timedelta]:
    start, end = parse_iso(event["start_time_utc"]), parse_iso(event["end_time_utc"])
    if start is None or end is None or end < start:
        return None
    return end - start


def classify_level(events: list, *, previous_training_start: Optional[datetime],
                   history_covers_gap: bool) -> "tuple[str, list]":
    """ROUTINE or SIGNIFICANT, from metadata only (the dispatcher never sees
    workout contents). Returns (level, reasons). Body-composition events
    alone are always routine: a single reading is never a trend."""
    reasons = []
    training = [e for e in events if e["source"] in TRAINING_SOURCES]
    if not training:
        return "routine", reasons
    if any((duration(e) or timedelta(0)) >= SIGNIFICANT_DURATION for e in training):
        reasons.append("long_session")
    if len({e["source"] for e in training}) > 1:
        reasons.append("hybrid_day")
    if history_covers_gap:
        first = min((event_time(e) for e in training if event_time(e)), default=None)
        if first is not None and (previous_training_start is None
                                  or first - previous_training_start >= RETURN_AFTER_GAP):
            reasons.append("return_after_break")
    return ("significant" if reasons else "routine"), reasons


def combined_level(levels: Iterable[Optional[str]]) -> str:
    return "significant" if "significant" in set(levels) else "routine"
