#!/usr/bin/env python3
"""Claude side of the feedback dispatcher: prompt, structured-output
contract, validation, authentication preflight and failure classification.

Reuses the Daily Review runner's Claude CLI plumbing (invoke_claude,
parse_claude_output, diagnose_claude_failure, redact_secrets) rather than
re-implementing it. Coaching content itself is defined by
workflows/event-feedback.md and the governing documents it lists; this
module only orchestrates and validates.

Specification: runner/feedback-dispatcher.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import workflow_runner as wr


WORKFLOW_NAME = "event-feedback"

VALID_STATUSES = ("completed", "completed_with_limitations", "insufficient_data")
VALID_CONFIDENCE = ("high", "moderate", "low")

MESSAGE_LIMITS = {"routine": 900, "significant": 2500}
RECOMMENDATION_COUNTS = {"routine": (1, 1), "significant": (1, 3)}
MAX_FIELD_LENGTH = 1200

# Every outcome a Claude call can have, as recorded in claude_calls.outcome.
OUTCOME_OK = "ok"
OUTCOME_AUTH = "claude_auth_failure"
OUTCOME_QUOTA = "claude_quota_failure"
OUTCOME_EXECUTION = "claude_execution_failure"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NOT_FOUND = "claude_not_found"
OUTCOME_MALFORMED = "malformed_output"

# How each outcome is handled by the dispatcher.
RETRYABLE_OUTCOMES = {OUTCOME_QUOTA, OUTCOME_EXECUTION, OUTCOME_TIMEOUT, OUTCOME_MALFORMED}
# These are not the batch's fault: the batch is requeued without spending
# one of its attempts, and the dispatcher stops calling Claude this run.
SYSTEMIC_OUTCOMES = {OUTCOME_AUTH, OUTCOME_NOT_FOUND}

AUTH_ESCALATION_THRESHOLD = 3
AUTH_COOLDOWN = timedelta(hours=6)
AUTH_EXPIRY_WARNING = timedelta(days=7)
# At most one corrective retry per generation for malformed output, as in
# the Daily Review runner.
MAX_CALLS_PER_GENERATION = 2

_FENCE = re.compile(r"```")
_URL = re.compile(r"https?://", re.IGNORECASE)


# --------------------------------------------------------------------------
# Prompt and contract
# --------------------------------------------------------------------------

def event_refs(events: list) -> list:
    """Opaque per-batch references (E1, E2, ...) so the structured output
    can be validated without echoing provider identifiers."""
    return [f"E{i}" for i in range(1, len(events) + 1)]


def describe_event(ref: str, event: Any) -> str:
    parts = [f"{ref}: source={event['source']}", f"type={event['event_type']}"]
    if event["activity_type"]:
        parts.append(f"activity_type={event['activity_type']}")
    parts.append(f"local_date={event['local_date']}")
    if event["start_time_utc"]:
        parts.append(f"start_utc={event['start_time_utc']}")
    if event["end_time_utc"]:
        parts.append(f"end_utc={event['end_time_utc']}")
    parts.append(f"source_id={event['event_key']}")
    if event["state"] == "edited":
        parts.append("edited_after_first_detection=true")
    return ", ".join(parts)


def output_schema(level: str) -> dict:
    low, high = RECOMMENDATION_COUNTS[level]
    nullable = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "workflow": {"type": "string", "enum": [WORKFLOW_NAME]},
            "batch_ref": {"type": "string"},
            "level": {"type": "string", "enum": [level]},
            "status": {"type": "string", "enum": list(VALID_STATUSES)},
            "events_covered": {"type": "array", "items": {"type": "string"}},
            "assessment": {"type": "string"},
            "goal_alignment": {"type": "string"},
            "context": {
                "type": "object",
                "properties": {"training_load": nullable, "recent_performance": nullable, "recovery": nullable},
                "required": ["training_load", "recent_performance", "recovery"],
            },
            "recommendations": {"type": "array", "items": {"type": "string"},
                                "minItems": low, "maxItems": high},
            "confidence": {"type": "string", "enum": list(VALID_CONFIDENCE)},
            "limitations": {"type": "array", "items": {"type": "string"}},
            "safety_concern": {"type": "boolean"},
            "message": {"type": "string"},
        },
        "required": ["workflow", "batch_ref", "level", "status", "events_covered", "assessment", "goal_alignment",
                     "context", "recommendations", "confidence", "limitations", "safety_concern", "message"],
    }


def build_prompt(*, repo_root: Path, batch_ref: str, level: str, level_reasons: list, kind: str,
                 events: list, local_date: date, athlete_timezone: str) -> str:
    workflow_file = wr.workflow_definition_path(repo_root, WORKFLOW_NAME)
    governing = wr.parse_governing_documents(workflow_file)
    governing_list = "\n".join(f"- {doc}" for doc in governing) or "(none found)"
    refs = event_refs(events)
    event_lines = "\n".join(describe_event(ref, event) for ref, event in zip(refs, events))
    low, high = RECOMMENDATION_COUNTS[level]
    digest_note = ""
    if kind == "morning_digest":
        digest_note = (
            "\nThis is the grouped morning message for sessions completed during quiet hours. Today's Daily "
            f"Review (reviews/daily/{local_date.isoformat()}.md) may already exist; if it does, read it, stay "
            "consistent with it and do not repeat it.\n"
        )
    return f"""You are executing the "{WORKFLOW_NAME}" coaching workflow.

Read the workflow definition at workflows/{WORKFLOW_NAME}.md before doing anything else and follow it
exactly. Then read these governing documents:
{governing_list}

Feedback level: {level.upper()} (decided by the dispatcher; reasons: {", ".join(level_reasons) or "none"}).
Batch reference: {batch_ref}
Athlete local date: {local_date.isoformat()} ({athlete_timezone})
{digest_note}
New events to give feedback on (retrieve their details yourself from AthleteData, following
data-source-policy.md and integration-status.md):
{event_lines}

Do not create or write any files. Do not update any document. Never include URLs, credentials,
identifiers or raw data dumps in your answer.

Reply with ONLY a single JSON object: workflow="{WORKFLOW_NAME}", batch_ref="{batch_ref}",
level="{level}", events_covered listing exactly {", ".join(refs)}, between {low} and {high}
recommendations, and "message" as the complete athlete-facing text of at most
{MESSAGE_LIMITS[level]} characters, as defined in the workflow's Structured Output section.
"""


def build_retry_prompt(original: str, issue: str) -> str:
    return wr.build_retry_prompt(original, issue)


@dataclass(frozen=True)
class Feedback:
    level: str
    status: str
    confidence: str
    safety_concern: bool
    message: str


def validate_feedback(result: Any, *, batch_ref: str, level: str, refs: list) -> "tuple[Optional[Feedback], list]":
    """Semantic validation on top of the CLI's JSON-schema enforcement.
    Rejects anything that could leak secrets (anything redact_secrets would
    change, or any URL) rather than repairing it."""
    if not isinstance(result, dict):
        return None, ["result is not an object"]
    problems = []
    if result.get("workflow") != WORKFLOW_NAME:
        problems.append("workflow mismatch")
    if result.get("batch_ref") != batch_ref:
        problems.append("batch_ref mismatch")
    if result.get("level") != level:
        problems.append("level mismatch")
    if result.get("status") not in VALID_STATUSES:
        problems.append("invalid status")
    if result.get("confidence") not in VALID_CONFIDENCE:
        problems.append("invalid confidence")
    if not isinstance(result.get("safety_concern"), bool):
        problems.append("safety_concern must be boolean")
    covered = result.get("events_covered")
    if not isinstance(covered, list) or sorted(map(str, covered)) != sorted(refs):
        problems.append("events_covered does not match the batch")
    recommendations = result.get("recommendations")
    low, high = RECOMMENDATION_COUNTS[level]
    if not isinstance(recommendations, list) or not low <= len(recommendations) <= high \
            or not all(isinstance(r, str) and r.strip() for r in recommendations):
        problems.append(f"recommendations must be {low}-{high} non-empty strings")
    context = result.get("context")
    if not isinstance(context, dict):
        problems.append("context missing")
    elif level == "significant" and result.get("status") == "completed" and not any(
            isinstance(context.get(k), str) and context.get(k).strip()
            for k in ("training_load", "recent_performance", "recovery")):
        problems.append("significant feedback needs training-load, performance or recovery context")
    for name in ("assessment", "goal_alignment"):
        value = result.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_LENGTH:
            problems.append(f"{name} missing or too long")
    if not isinstance(result.get("limitations"), list):
        problems.append("limitations must be a list")

    message = result.get("message")
    if not isinstance(message, str) or not message.strip():
        problems.append("message missing")
    else:
        if len(message) > MESSAGE_LIMITS[level]:
            problems.append("message too long")
        if _FENCE.search(message):
            problems.append("message contains a code fence")
        if _URL.search(message) or wr.redact_secrets(message) != message:
            problems.append("message contains a URL or secret-like content")
        if "\\n" in message:
            problems.append("message contains literal backslash-n")

    if problems:
        return None, problems
    return Feedback(level=level, status=result["status"], confidence=result["confidence"],
                    safety_concern=result["safety_concern"], message=message.strip()), []


# --------------------------------------------------------------------------
# Authentication preflight
# --------------------------------------------------------------------------

AUTH_STATUS_TIMEOUT_SECONDS = 20

# What `claude auth status --json` told us. Only the boolean "loggedIn" is
# read; the rest of its output (account e-mail, organisation) is discarded
# unread and never logged.
AUTH_LOGGED_IN = "logged_in"
AUTH_LOGGED_OUT = "logged_out"
AUTH_UNKNOWN = "unknown"
AUTH_NOT_CHECKED = "not_checked_simulation"

# What a passed preflight proves. `claude auth status` reports the CLI's
# locally stored login state without contacting the model; it does not
# prove the stored token will be accepted (expired-but-present tokens may
# still read as logged in). Only a real call proves that, and real calls are
# classified and escalated below.
ASSURANCE_LOCAL = "local_cli_login_state"
ASSURANCE_NONE = "none_simulation"


def cli_auth_status(claude_binary: str, *, cwd: Optional[Path] = None, run: Callable = subprocess.run) -> str:
    """Runs the non-generative `claude auth status --json` (no prompt, no
    model request) and returns AUTH_LOGGED_IN / AUTH_LOGGED_OUT /
    AUTH_UNKNOWN."""
    try:
        completed = run([claude_binary, "auth", "status", "--json"], capture_output=True, text=True,
                        timeout=AUTH_STATUS_TIMEOUT_SECONDS, cwd=str(cwd) if cwd else None)
    except (subprocess.TimeoutExpired, OSError):
        return AUTH_UNKNOWN
    try:
        logged_in = json.loads(completed.stdout or "").get("loggedIn")
    except (json.JSONDecodeError, AttributeError):
        logged_in = None
    if logged_in is True:
        return AUTH_LOGGED_IN
    if logged_in is False:
        return AUTH_LOGGED_OUT
    return AUTH_UNKNOWN


@dataclass(frozen=True)
class Preflight:
    ok: bool
    reason: Optional[str] = None       # fixed vocabulary
    assurance: Optional[str] = None


def auth_preflight(*, claude_binary: str, auth_status: Callable[[], str], now: datetime, consecutive_failures: int,
                   escalated_at: Optional[datetime], auth_expires_on: Optional[date], local_today: date) -> Preflight:
    """Cheap checks before any Claude call: CLI resolvable, configured
    refresh-token expiry not passed, auth circuit closed, and the CLI's own
    login state. Never reads a credential file."""
    if shutil.which(claude_binary) is None and not (os.path.isfile(claude_binary)
                                                     and os.access(claude_binary, os.X_OK)):
        return Preflight(False, "claude_not_found")
    if auth_expires_on is not None and local_today > auth_expires_on:
        return Preflight(False, "auth_expired_by_date")
    if consecutive_failures >= AUTH_ESCALATION_THRESHOLD and escalated_at is not None \
            and now - escalated_at < AUTH_COOLDOWN:
        return Preflight(False, "auth_circuit_open")
    status = auth_status()
    if status == AUTH_NOT_CHECKED:
        return Preflight(True, None, ASSURANCE_NONE)
    if status == AUTH_LOGGED_OUT:
        return Preflight(False, "cli_logged_out")
    if status != AUTH_LOGGED_IN:
        return Preflight(False, "auth_status_unknown")
    return Preflight(True, None, ASSURANCE_LOCAL)


def auth_expiry_approaching(auth_expires_on: Optional[date], local_today: date) -> bool:
    return auth_expires_on is not None and auth_expires_on - local_today <= AUTH_EXPIRY_WARNING


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CallResult:
    outcome: str
    structured: Optional[dict] = None
    issue: Optional[str] = None        # redacted, for the retry prompt only


class ClaudeGateway:
    """Live gateway: one Claude CLI invocation per call."""

    simulated = False

    def __init__(self, config: wr.RunnerConfig, invoke: Optional[Callable] = None):
        self.config = config
        self._invoke = invoke or wr.invoke_claude

    def call(self, prompt: str, schema: dict) -> CallResult:
        try:
            completed = self._invoke(prompt, self.config, json_schema=schema)
        except subprocess.TimeoutExpired:
            return CallResult(OUTCOME_TIMEOUT)
        except FileNotFoundError:
            return CallResult(OUTCOME_NOT_FOUND)
        return classify_completed(completed)


def classify_completed(completed: subprocess.CompletedProcess) -> CallResult:
    if completed.returncode != 0:
        diagnosis = wr.diagnose_claude_failure(completed.stdout, completed.stderr)
        return CallResult(diagnosis.error_category)
    try:
        structured = wr.parse_claude_output(completed.stdout)
    except wr.MalformedOutputError as exc:
        return CallResult(OUTCOME_MALFORMED, issue=wr.redact_secrets(str(exc)))
    return CallResult(OUTCOME_OK, structured=structured)


class SimulatedGateway:
    """--simulate: never starts a process. Returns a synthetic, contract-valid
    answer so the rest of the pipeline can be exercised end to end."""

    simulated = True

    def __init__(self):
        self.calls = 0

    def call(self, prompt: str, schema: dict) -> CallResult:
        self.calls += 1
        props = schema["properties"]
        level = props["level"]["enum"][0]
        batch_ref = re.search(r'batch_ref="([^"]+)"', prompt).group(1)
        listed = re.search(r"events_covered listing exactly (.+?), between", prompt, re.DOTALL).group(1)
        refs = re.findall(r"E\d+", listed)
        return CallResult(OUTCOME_OK, structured={
            "workflow": WORKFLOW_NAME, "batch_ref": batch_ref, "level": level, "status": "completed",
            "events_covered": refs, "assessment": "[simulated]",
            "goal_alignment": "[simulated]",
            "context": {"training_load": "[simulated]", "recent_performance": None, "recovery": None},
            "recommendations": ["[simulated]"], "confidence": "low", "limitations": ["simulation"],
            "safety_concern": False, "message": f"[simulated {level} feedback]",
        })


def parse_expiry_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None
