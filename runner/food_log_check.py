#!/usr/bin/env python3
"""20:00 food-log completeness check (Phase 5B).

Deterministic: reads today's Cronometer log (read-only, via food_log_get),
judges it against the athlete's own recent logging pattern, and sends at most
one Telegram reminder per local day when the log looks incomplete or has not
synchronised. Sends nothing when the log looks complete. No Claude.

    python3 runner/food_log_check.py --once            # scheduled run (20:00 local)
    python3 runner/food_log_check.py --dry-run         # decision and message only; no send, no state

Duplicate prevention: one state file per local date under
runtime/food-log-check/, created exclusively before any send, so a restart
can never produce a second reminder. Logs carry decisions and reason codes
only -- never intake values. See runner/nutrition-workflows.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time as time_module
from datetime import datetime, time, timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import athletedata_client as adc
import nutrition_data as nd
import telegram_delivery as td
import workflow_runner as wr

UTC = dt_timezone.utc
LOCK_NAME = "food-log-check"
LOCK_STALE_SECONDS = 900
STATE_DIR = Path("runtime") / "food-log-check"
# A scheduled run outside this local window (e.g. a delayed start) sends nothing.
RUN_WINDOW = (time(19, 45), time(21, 15))
MAX_SEND_ATTEMPTS = 2
DEFAULT_RETRY_DELAY = 30
MAX_RETRY_DELAY = 60


def _default_log(**fields: Any) -> None:
    fields.setdefault("logged_at", datetime.now(UTC).isoformat())
    print(json.dumps({k: wr.redact_secrets(v) if isinstance(v, str) else v for k, v in fields.items()},
                     sort_keys=True), file=sys.stderr)


# --------------------------------------------------------------------------
# Decision and message (pure)
# --------------------------------------------------------------------------

def needs_reminder(c: nd.Completeness) -> bool:
    """Missing or likely incomplete logs get a reminder. 'unknown' (no
    personal pattern yet) and 'complete' never do."""
    return c.status in ("missing", "incomplete")


def _join(items: list, word: str = "and") -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def build_reminder(c: nd.Completeness, now_local: datetime) -> str:
    day = now_local.date()
    header = f"Food log check — {now_local.strftime('%H:%M')}"
    sync = nd.local_time_label(c.last_sync, now_local.tzinfo, day)

    if c.status == "missing":
        what = "Cronometer shows no food entries for today yet."
    else:
        present = [s if s != "snack" else "snacks" for s in c.present_slots]
        absent = list(c.missing_slots) + (["snacks"] if c.snack_missing else [])
        if c.missing_slots:
            what = (f"Cronometer currently shows {_join(present)}, but no {_join(absent, 'or')}."
                    if present else f"Cronometer shows no {_join(absent, 'or')} yet.")
        else:
            what = (f"Cronometer shows {_join(present)}, but today's total so far ({round(c.kcal):,} kcal) "
                    f"is well below your usual fully logged day (about {round(c.reference_kcal or 0):,} kcal).")

    if sync is None:
        sync_line = "No Cronometer sync time is available, so recent entries may not have synchronised yet."
    elif c.stale:
        sync_line = f"Last sync: {sync}. Entries added since then may not have synchronised yet."
    else:
        sync_line = f"Last sync: {sync}."

    if c.status == "missing" and c.stale:
        close = ("If you've already logged today's meals, they may simply not have synchronised yet; "
                 "otherwise, please add them to Cronometer so your evening review is accurate.")
    else:
        close = ("If you've eaten anything else today, please add it to Cronometer "
                 "so your evening review is more accurate.")
    return f"{header}\n\n{what} {sync_line}\n\n{close}"


# --------------------------------------------------------------------------
# State (one decision per local date)
# --------------------------------------------------------------------------

def state_path(repo_root: Path, local_date: str) -> Path:
    return repo_root / STATE_DIR / f"{local_date}.json"


def claim_state(path: Path, record: dict) -> bool:
    """Creates the day's state file exclusively; False if it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True)
    return True


def update_state(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Telegram (one reminder; retried only when Telegram cannot have received it)
# --------------------------------------------------------------------------

def send_reminder(config: td.TelegramConfig, text: str, *, http_post: Optional[Callable],
                  sleep_fn: Callable[[float], None]) -> "tuple[str, Optional[int]]":
    """('sent' | 'rejected' | 'unknown' | 'retries_exhausted', message_id).
    Same classification as the feedback dispatcher: an HTTP error reply
    other than 504, or a connect-phase network error, means Telegram did not
    deliver it, so one retry is safe. Anything else is ambiguous and is never
    resent."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        retry_after = None
        try:
            return "sent", td.send_single_message(config, text, http_post=http_post)
        except td.TelegramAPIError as exc:
            if exc.status_code == 504:
                return "unknown", None
            if not (exc.status_code == 429 or exc.status_code >= 500):
                return "rejected", None
            retry_after = exc.retry_after
        except td.TelegramNetworkError:
            pass
        except Exception:  # noqa: BLE001 -- read timeouts, resets: the message may have arrived
            return "unknown", None
        if attempt < MAX_SEND_ATTEMPTS:
            sleep_fn(min(retry_after or DEFAULT_RETRY_DELAY, MAX_RETRY_DELAY))
    return "retries_exhausted", None


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run_check(*, repo_root: Path, env: dict, now: datetime, client_factory: Callable[[], Any],
              http_post: Optional[Callable] = None, sleep_fn: Callable[[float], None] = time_module.sleep,
              log: Callable[..., None] = _default_log, dry_run: bool = False) -> dict:
    tz = ZoneInfo(env.get("ATHLETE_TIMEZONE", "").strip())
    now_local = now.astimezone(tz)
    local_date = now_local.date()
    base = {"local_date": local_date.isoformat(), "dry_run": dry_run}

    if not dry_run and not (RUN_WINDOW[0] <= now_local.time() <= RUN_WINDOW[1]):
        log(event="food_log_check_skipped", reason="outside_window")
        return {**base, "outcome": "skipped", "reason": "outside_window"}

    lock = wr.WorkflowLock(repo_root / "runtime" / "locks", LOCK_NAME, LOCK_STALE_SECONDS)
    try:
        lock.acquire()
    except wr.DuplicateExecutionError:
        log(event="food_log_check_skipped", reason="lock_held")
        return {**base, "outcome": "skipped", "reason": "lock_held"}
    try:
        path = state_path(repo_root, local_date.isoformat())
        if not dry_run and path.exists():
            log(event="food_log_check_skipped", reason="already_checked_today")
            return {**base, "outcome": "skipped", "reason": "already_checked_today"}

        try:
            client = client_factory()
            inputs = nd.fetch_inputs(client, local_date, tz, sources=("food_log_get",))
        except adc.AthleteDataError as exc:
            inputs = nd.Inputs(errors={"food_log_get": nd._error_category(exc)})
        if "food_log_get" in inputs.errors:
            # No decision is recorded, so a later manual run may try again.
            log(event="food_log_check_end", outcome="unavailable", error_category=inputs.errors["food_log_get"])
            return {**base, "outcome": "unavailable", "error_category": inputs.errors["food_log_get"]}

        c = nd.assess_completeness(nd.parse_food_log(inputs.food_pages), local_date, now)
        decision = {"status": c.status, "reasons": list(c.reasons), "missing_slots": list(c.missing_slots),
                    "stale": c.stale, "sync_age_minutes": c.sync_age_minutes,
                    "history_logged_days": c.history_logged_days}
        record = {"local_date": local_date.isoformat(), "checked_at": now.astimezone(UTC).isoformat(), **decision}

        if not needs_reminder(c):
            if not dry_run:
                claim_state(path, {**record, "reminder": False})
            log(event="food_log_check_end", outcome="no_reminder", **decision)
            return {**base, "outcome": "no_reminder", **decision}

        text = build_reminder(c, now_local)
        if dry_run:
            log(event="food_log_check_end", outcome="dry_run_reminder", **decision)
            return {**base, "outcome": "dry_run_reminder", "message": text, **decision}

        config = td.load_telegram_config(env)
        record.update(reminder=True, message_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        if config is None:
            claim_state(path, {**record, "delivery": "not_configured"})
            log(event="food_log_check_end", outcome="telegram_not_configured", **decision)
            return {**base, "outcome": "telegram_not_configured", **decision}
        if not claim_state(path, {**record, "delivery": "sending"}):
            log(event="food_log_check_skipped", reason="already_checked_today")
            return {**base, "outcome": "skipped", "reason": "already_checked_today"}

        delivery, message_id = send_reminder(config, text, http_post=http_post, sleep_fn=sleep_fn)
        update_state(path, {**record, "delivery": delivery, "telegram_message_id": message_id})
        log(event="food_log_check_end", outcome="reminder_" + delivery, **decision)
        return {**base, "outcome": "reminder_" + delivery, **decision}
    finally:
        lock.release()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="20:00 Cronometer food-log completeness check.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="scheduled run: may send one reminder per day")
    mode.add_argument("--dry-run", action="store_true", help="print the decision and message; send nothing")
    parser.add_argument("--env-file", default=None, help="defaults to runner/config.env")
    parser.add_argument("--repo-root", default=None)
    return parser


def main(argv: Optional[list] = None, *, now_fn: Optional[Callable[[], datetime]] = None,
         client_factory: Optional[Callable[[], Any]] = None, http_post: Optional[Callable] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else wr.default_repo_root()
    env = wr.load_env_file(Path(args.env_file) if args.env_file else repo_root / "runner" / "config.env")
    env.update(os.environ)
    try:
        ZoneInfo(env.get("ATHLETE_TIMEZONE", "").strip())
    except Exception:  # noqa: BLE001
        print(json.dumps({"outcome": "failed", "error_category": "invalid_timezone"}))
        return 2
    factory = client_factory or (lambda: adc.AthleteDataClient(
        adc.load_mcp_endpoint(adc.default_claude_config_path(), repo_root)))
    result = run_check(repo_root=repo_root, env=env, now=(now_fn or (lambda: datetime.now(UTC)))(),
                       client_factory=factory, http_post=http_post, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["outcome"] in ("unavailable", "reminder_rejected", "reminder_unknown",
                                      "reminder_retries_exhausted") else 0


if __name__ == "__main__":
    sys.exit(main())
