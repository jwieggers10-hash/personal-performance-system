#!/usr/bin/env python3
"""Evening Nutrition Review entry point (Phase 5B, scheduled 21:30 local).

Runs workflows/evening-nutrition-review.md through the shared workflow runner,
which keeps its lock, duplicate check, Claude invocation, corrective retry,
failure classification, review storage (reviews/nutrition/) and Telegram
delivery unchanged. This module adds, under that lock and before Claude:

1. Facts: today's food log, Garmin expenditure, training and body-composition
   numbers computed deterministically by nutrition_data.py from read-only
   AthleteData, plus the approved targets parsed from goals.md.
2. Coordination: the runner's lock already pauses the feedback dispatcher's
   Claude calls (feedback_dispatcher.coaching_workflow_running); this waits,
   bounded, for a dispatcher run that is already in flight to finish.

    python3 runner/evening_nutrition_review.py --mode automated
    python3 runner/evening_nutrition_review.py --print-facts   # facts only: no lock, Claude or Telegram
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as time_module
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import athletedata_client as adc
import nutrition_data as nd
import workflow_runner as wr

UTC = dt_timezone.utc
WORKFLOW = "evening-nutrition-review"
# An automated run started outside this local window (late boot, manual
# start at the wrong time) does nothing: no stale or misdated review.
AUTOMATED_WINDOW = (time(21, 0), time(23, 45))

DISPATCHER_LOCK = "feedback-dispatcher"
DISPATCHER_LOCK_STALE_SECONDS = 3600  # feedback_dispatcher.LOCK_STALE_SECONDS
# Once this review's lock exists the dispatcher starts no new generation; a
# generation already running makes at most two Claude calls of
# CLAUDE_TIMEOUT_SECONDS (600 s) each, so this bound covers it with margin.
DISPATCHER_MAX_WAIT = timedelta(minutes=25)
DISPATCHER_POLL_SECONDS = 15


def wait_for_dispatcher(repo_root: Path, *, sleep_fn: Callable[[float], None] = time_module.sleep,
                        monotonic: Callable[[], float] = time_module.monotonic,
                        log: Callable[..., None] = lambda **f: None,
                        max_wait: timedelta = DISPATCHER_MAX_WAIT) -> str:
    """'idle', 'waited' or 'timeout'. On timeout the review proceeds: no new
    dispatcher Claude call can start meanwhile, so what remains is a stuck
    non-Claude step, and a missed evening review is the worse outcome."""
    lock = wr.WorkflowLock(repo_root / "runtime" / "locks", DISPATCHER_LOCK, DISPATCHER_LOCK_STALE_SECONDS)
    if not lock.is_held():
        return "idle"
    log(event="dispatcher_wait_start")
    deadline = monotonic() + max_wait.total_seconds()
    while lock.is_held():
        if monotonic() >= deadline:
            log(event="dispatcher_wait_end", outcome="timeout")
            return "timeout"
        sleep_fn(DISPATCHER_POLL_SECONDS)
    log(event="dispatcher_wait_end", outcome="released")
    return "waited"


def compute_facts(repo_root: Path, tz: ZoneInfo, now: datetime, client_factory: Callable[[], Any]) -> dict:
    target = now.astimezone(tz).date()
    try:
        targets, targets_error = nd.load_targets(repo_root / "goals.md"), None
    except nd.TargetsError as exc:
        targets, targets_error = None, str(exc)
    try:
        inputs = nd.fetch_inputs(client_factory(), target, tz)
    except adc.AthleteDataError as exc:
        inputs = nd.Inputs(errors={name: nd._error_category(exc) for name in nd.TOOLS})
    return nd.build_facts(inputs, targets, target, now, tz, targets_error=targets_error)


def facts_block(facts: dict) -> str:
    return (
        "Runner-computed facts for this review (authoritative; computed deterministically from read-only "
        "AthleteData and goals.md immediately before this invocation). Quote these numbers as given: do not "
        "recompute, re-total or replace them, and do not add workout calories to Garmin's daily figures. "
        "Follow the workflow definition for how to use and label them:\n"
        + json.dumps(facts, indent=2, sort_keys=True)
    )


def make_prepare(repo_root: Path, tz: ZoneInfo, *, client_factory: Callable[[], Any],
                 sleep_fn: Callable[[float], None] = time_module.sleep,
                 monotonic: Callable[[], float] = time_module.monotonic,
                 log: Callable[..., None] = lambda **f: None) -> Callable[[datetime], str]:
    def prepare(local_dt: datetime) -> str:
        facts = compute_facts(repo_root, tz, local_dt, client_factory)
        log(event="facts_computed", sources=facts["sources"],
            log_status=facts["food_today"].get("log_status"),
            expenditure_status=facts["garmin_expenditure_today"].get("status"))
        outcome = wait_for_dispatcher(repo_root, sleep_fn=sleep_fn, monotonic=monotonic, log=log)
        log(event="dispatcher_coordination", outcome=outcome)
        return facts_block(facts)
    return prepare


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evening Nutrition Review (21:30 Europe/Amsterdam).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mode", choices=sorted(wr.VALID_EXECUTION_MODES))
    mode.add_argument("--print-facts", action="store_true", help="print the computed facts and exit")
    parser.add_argument("--env-file", default=None, help="defaults to runner/config.env")
    parser.add_argument("--repo-root", default=None)
    return parser


def main(argv: Optional[list] = None, *, now_fn: Optional[Callable[[], datetime]] = None,
         client_factory: Optional[Callable[[], Any]] = None,
         sleep_fn: Callable[[float], None] = time_module.sleep) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else wr.default_repo_root()
    env = wr.load_env_file(Path(args.env_file) if args.env_file else repo_root / "runner" / "config.env")
    env.update(os.environ)
    try:
        config = wr.RunnerConfig.from_env(env, repo_root_override=args.repo_root)
    except wr.ConfigError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 2
    tz = ZoneInfo(config.athlete_timezone)
    now = (now_fn or (lambda: datetime.now(UTC)))()
    factory = client_factory or (lambda: adc.AthleteDataClient(
        adc.load_mcp_endpoint(adc.default_claude_config_path(), config.repo_root)))

    if args.print_facts:
        print(json.dumps(compute_facts(config.repo_root, tz, now, factory), indent=2, sort_keys=True))
        return 0

    local = now.astimezone(tz)
    if args.mode == "automated" and not (AUTOMATED_WINDOW[0] <= local.time() <= AUTOMATED_WINDOW[1]):
        wr.log_event(config, event="workflow_skipped", workflow=WORKFLOW, reason="outside_window")
        print(json.dumps({"workflow": WORKFLOW, "status": "skipped", "reason": "outside_window"}))
        return 0

    prepare = make_prepare(config.repo_root, tz, client_factory=factory, sleep_fn=sleep_fn,
                           log=lambda **fields: wr.log_event(config, workflow=WORKFLOW, **fields))
    result = wr.run_workflow(WORKFLOW, args.mode, config, now=now, prepare_prompt=prepare)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("completed", "completed_with_limitations", "duplicate_skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
