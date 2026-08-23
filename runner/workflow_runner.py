#!/usr/bin/env python3
"""Reusable non-interactive workflow runner.

Implements the Execution Lifecycle defined in runner/workflow-runner.md,
extended with the Notify stage from runner/telegram-integration.md:

    Initialize -> Validate environment -> Acquire workflow lock -> Load workflow
    -> Execute Claude -> Validate structured output -> Write review
    -> Notify (Telegram) -> Release lock -> Return status

This script contains no coaching logic. All reasoning, philosophy compliance,
and output structure belong to the workflow definition under workflows/*.md,
which Claude Code reads and executes. The runner's job is orchestration only:
invoke, validate, store, report. Telegram delivery (see telegram_delivery.py)
is a separate, one-way dependency of this module -- it never runs coaching
logic and a delivery failure never invalidates an already-saved review.

Governing documents for this implementation:
    phase3-workflow-spec.md, workflows/daily-review.md, runner/README.md,
    runner/workflow-runner.md, runner/telegram-integration.md, constraints.md,
    data-source-policy.md, integration-status.md, CLAUDE.md

Usage:
    python3 runner/workflow_runner.py --workflow daily-review --mode automated
    python3 runner/workflow_runner.py --workflow daily-review --mode manual --context "slept poorly, sore calves"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError as exc:  # pragma: no cover - Python 3.9+ ships zoneinfo
    raise RuntimeError("Python 3.9+ (with zoneinfo) is required") from exc

import telegram_delivery


# --------------------------------------------------------------------------
# Contract constants (must match workflows/daily-review.md and
# runner/workflow-runner.md exactly -- see the Structured Output /
# Structured Result Contract sections of those files).
# --------------------------------------------------------------------------

RUNNER_ENVELOPE_FIELDS = (
    "workflow",
    "status",
    "execution_mode",
    "execution_id",
    "started_at",
    "finished_at",
    "local_date",
    "review_file",
    "notification_required",
    "notification_level",
    "confidence",
    "limitations",
    "error",
)

VALID_STATUSES = {"completed", "completed_with_limitations", "failed", "duplicate_skipped"}
VALID_EXECUTION_MODES = {"manual", "automated"}
VALID_CONFIDENCE = {"high", "moderate", "low"}
VALID_NOTIFICATION_LEVELS = {"routine", "attention", "urgent"}

# Fields the *workflow's own* structured output (e.g. daily-review.md) is
# expected to carry, in addition to the runner envelope above. Not all of
# these are required for every workflow; missing ones are noted, not invented.
WORKFLOW_RESULT_FIELDS = (
    "recommendation_category",
    "recommendation_summary",
    "missing_data",
    "integration_failures",
    "data_retrieved_at",
)

DEFAULT_LOCK_STALE_SECONDS = 3600
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 600
DEFAULT_CLAUDE_BINARY = "claude"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_ALLOWED_TOOLS = "Read,mcp__athletedata__*"

# Redact anything that looks like a secret-bearing query param or bearer token
# before it can reach a log line or an error field. Belt-and-suspenders on top
# of never passing secrets into strings we log in the first place.
_SECRET_PATTERNS = (
    re.compile(r"(apiKey|api_key|token|secret)=([^&\s]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.]+", re.IGNORECASE),
)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: m.group(1) + "[REDACTED]", redacted)
    return redacted


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class RunnerError(Exception):
    """Base class for errors the runner reports as a failed/duplicate result."""


class ConfigError(RunnerError):
    pass


class DuplicateExecutionError(RunnerError):
    pass


class MalformedOutputError(RunnerError):
    pass


class DuplicateReviewError(RunnerError):
    pass


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE env file. Missing file is not an error --
    the caller falls back to real process environment variables."""
    values: dict = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass
class RunnerConfig:
    repo_root: Path
    athlete_timezone: str
    claude_binary: str = DEFAULT_CLAUDE_BINARY
    claude_model: Optional[str] = None
    timeout_seconds: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS
    max_budget_usd: Optional[str] = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS
    lock_stale_seconds: int = DEFAULT_LOCK_STALE_SECONDS
    log_file: Optional[Path] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str], repo_root_override: Optional[str] = None) -> "RunnerConfig":
        repo_root_value = repo_root_override or env.get("REPO_ROOT")
        repo_root = Path(repo_root_value).resolve() if repo_root_value else default_repo_root()

        athlete_timezone = env.get("ATHLETE_TIMEZONE", "").strip()
        if not athlete_timezone:
            raise ConfigError(
                "ATHLETE_TIMEZONE is required (see phase3-workflow-spec.md Timezone section) "
                "and was not found in the environment or env file."
            )
        try:
            ZoneInfo(athlete_timezone)
        except Exception as exc:
            raise ConfigError(f"ATHLETE_TIMEZONE '{athlete_timezone}' is not a valid IANA timezone name") from exc

        if not (repo_root / "workflows").is_dir():
            raise ConfigError(
                f"Resolved repo root '{repo_root}' does not contain a workflows/ directory; "
                "set REPO_ROOT explicitly."
            )

        log_file_value = env.get("RUNNER_LOG_FILE", "").strip()

        return cls(
            repo_root=repo_root,
            athlete_timezone=athlete_timezone,
            claude_binary=env.get("CLAUDE_BINARY", DEFAULT_CLAUDE_BINARY) or DEFAULT_CLAUDE_BINARY,
            claude_model=(env.get("CLAUDE_MODEL", "").strip() or None),
            timeout_seconds=int(env.get("CLAUDE_TIMEOUT_SECONDS", DEFAULT_CLAUDE_TIMEOUT_SECONDS)),
            max_budget_usd=(env.get("CLAUDE_MAX_BUDGET_USD", "").strip() or None),
            permission_mode=env.get("CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE) or DEFAULT_PERMISSION_MODE,
            allowed_tools=env.get("CLAUDE_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS) or DEFAULT_ALLOWED_TOOLS,
            lock_stale_seconds=int(env.get("LOCK_STALE_SECONDS", DEFAULT_LOCK_STALE_SECONDS)),
            log_file=(Path(log_file_value) if log_file_value else None),
            telegram_bot_token=(env.get("TELEGRAM_BOT_TOKEN", "").strip() or None),
            telegram_chat_id=(env.get("TELEGRAM_CHAT_ID", "").strip() or None),
        )


def default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Time / identity
# --------------------------------------------------------------------------

def athlete_local_now(tz_name: str, now: Optional[datetime] = None) -> datetime:
    reference = now or datetime.now(dt_timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt_timezone.utc)
    return reference.astimezone(ZoneInfo(tz_name))


def execution_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# Workflow definition loading
# --------------------------------------------------------------------------

_GOVERNING_DOCS_HEADING = re.compile(r"^##\s+Governing Documents\s*$", re.MULTILINE)
_SECTION_BREAK = re.compile(r"^\s*(---|##\s)", re.MULTILINE)
_BACKTICK_MD_FILE = re.compile(r"`([\w\-]+\.md)`")


def workflow_definition_path(repo_root: Path, workflow_name: str) -> Path:
    return repo_root / "workflows" / f"{workflow_name}.md"


def parse_governing_documents(workflow_file: Path) -> list:
    """Extract the backtick-quoted *.md filenames listed under the workflow
    definition's own '## Governing Documents' section, so this list is never
    hand-duplicated out of sync with the workflow file itself."""
    text = workflow_file.read_text(encoding="utf-8")
    heading_match = _GOVERNING_DOCS_HEADING.search(text)
    if not heading_match:
        return []
    remainder = text[heading_match.end():]
    break_match = _SECTION_BREAK.search(remainder)
    section = remainder[: break_match.start()] if break_match else remainder
    return _BACKTICK_MD_FILE.findall(section)


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def build_prompt(
    workflow_name: str,
    workflow_file: Path,
    governing_documents: list,
    execution_mode: str,
    local_date: date,
    athlete_timezone: str,
    athlete_context: Optional[str],
) -> str:
    previous_date = local_date - timedelta(days=1)
    governing_docs_list = "\n".join(f"- {doc}" for doc in governing_documents) or "(none found)"

    context_block = ""
    if athlete_context:
        context_block = (
            "\nThe athlete has provided the following context for today, directly, in manual mode. "
            "Treat it as confirmed subjective feedback, not objective sensor data:\n"
            f'"{athlete_context}"\n'
        )

    all_result_fields = list(RUNNER_ENVELOPE_FIELDS) + list(WORKFLOW_RESULT_FIELDS) + ["human_readable_review"]

    return f"""You are executing the "{workflow_name}" coaching workflow.

Read the complete workflow definition at {workflow_file.name} in the workflows/ directory
before doing anything else, and follow it exactly. It defines your operational contract.

Before analysis, read these governing documents (per the workflow definition's own
Governing Documents section):
{governing_docs_list}

Execution details:
- Execution mode: {execution_mode}
- Athlete local date: {local_date.isoformat()}
- Athlete timezone: {athlete_timezone}
- Previous local date: {previous_date.isoformat()}
{context_block}
Retrieve fresh AthleteData yourself using the available AthleteData MCP tools. Do not rely on
memory of prior sessions. Follow data-source-policy.md for source authority and duplicate
handling, and integration-status.md for known limitations -- never assume a field is available
because it exists in principle.

Do not create or write any files yourself. Do not update lessons-learned.md, goals.md, or any
governing document. Your only output is the final answer described below -- the runner (not you)
is responsible for saving the review file.

When you are done, reply with ONLY a single JSON object as your final message -- no markdown code
fences, no explanatory text before or after it. The object must have exactly these top-level
fields: {", ".join(all_result_fields)}.

Field notes:
- "workflow" must be exactly "{workflow_name}".
- "execution_mode" must be exactly "{execution_mode}".
- "local_date" must be exactly "{local_date.isoformat()}".
- "status" must be one of: {", ".join(sorted(VALID_STATUSES))}.
- "confidence" must be one of: {", ".join(sorted(VALID_CONFIDENCE))}.
- "notification_level" must be one of: {", ".join(sorted(VALID_NOTIFICATION_LEVELS))}.
- "limitations", "missing_data", and "integration_failures" are arrays of short strings (may be empty).
- "notification_required" is a boolean.
- "error" is null unless status is "failed", in which case it is a short description with no secrets.
- "review_file" is your proposed relative path, e.g. "reviews/daily/{local_date.isoformat()}.md" --
  you do not write this file yourself.
- "human_readable_review" is the complete markdown review text following the Human-Readable Output
  structure defined in the workflow definition, as a single string (use \\n for line breaks).

If a responsible recommendation cannot be made because essential information is missing, set
"status" to "completed_with_limitations" or use recommendation_category "more_information_needed"
as defined in the workflow -- do not invent an answer.
"""


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------

class WorkflowLock:
    """Atomic lock file, scoped to one workflow-run. Uses O_CREAT|O_EXCL so it
    works identically on the Ubuntu production target and on a developer
    machine, without depending on the POSIX-only fcntl module."""

    def __init__(self, lock_dir: Path, key: str, stale_seconds: int):
        self.path = lock_dir / f"{key}.lock"
        self.stale_seconds = stale_seconds
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            self._acquired = True
            return

        if self._is_stale():
            self._remove_quietly()
            if self._try_create():
                self._acquired = True
                return

        raise DuplicateExecutionError(f"Lock already held: {self.path}")

    def release(self) -> None:
        if self._acquired:
            self._remove_quietly()
            self._acquired = False

    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            payload = json.dumps({"pid": os.getpid(), "acquired_at": datetime.now(dt_timezone.utc).isoformat()})
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _is_stale(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Unreadable/corrupt lock file -- treat as stale rather than
            # blocking every future run forever.
            return True

        pid = data.get("pid")
        if isinstance(pid, int) and not _pid_is_running(pid):
            return True

        acquired_at = data.get("acquired_at")
        if acquired_at:
            try:
                acquired_dt = datetime.fromisoformat(acquired_at)
                age = (datetime.now(dt_timezone.utc) - acquired_dt).total_seconds()
                if age > self.stale_seconds:
                    return True
            except ValueError:
                return True

        return False

    def _remove_quietly(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "WorkflowLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except (OSError, AttributeError):
        # Platform doesn't support this check (e.g. some non-POSIX systems) --
        # fall back to age-based staleness only.
        return True
    return True


# --------------------------------------------------------------------------
# Claude invocation
# --------------------------------------------------------------------------

def invoke_claude(prompt: str, config: RunnerConfig) -> subprocess.CompletedProcess:
    argv = [
        config.claude_binary,
        "--print",
        "--output-format", "json",
        "--permission-mode", config.permission_mode,
        # --allowedTools is variadic in the Claude Code CLI (accepts <tools...>);
        # passed as two argv elements it would swallow the prompt argument that
        # follows. The "=" form keeps it a single argv element.
        f"--allowedTools={config.allowed_tools}",
    ]
    if config.claude_model:
        argv += ["--model", config.claude_model]
    if config.max_budget_usd:
        argv += ["--max-budget-usd", config.max_budget_usd]
    argv.append(prompt)

    return subprocess.run(
        argv,
        cwd=str(config.repo_root),
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
    )


def parse_claude_output(raw_stdout: str) -> dict:
    """Defensively parse Claude's --output-format json response. The CLI
    wraps the final answer text in an outer object (typically under a
    "result" key); the answer text itself is expected to be the JSON object
    this workflow was asked to produce. Both layers are handled, and any
    failure is reported as malformed output rather than guessed at."""
    try:
        outer = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(f"Claude output was not valid JSON: {redact_secrets(str(exc))}") from exc

    candidate: Any = outer
    if isinstance(outer, dict) and isinstance(outer.get("result"), str):
        try:
            candidate = json.loads(outer["result"])
        except json.JSONDecodeError as exc:
            raise MalformedOutputError(
                f"Claude's final answer text was not valid JSON: {redact_secrets(str(exc))}"
            ) from exc

    if not isinstance(candidate, dict):
        raise MalformedOutputError("Claude's structured result was not a JSON object")

    return candidate


def validate_structured_result(
    result: dict,
    expected_workflow: str,
    expected_execution_mode: str,
    expected_local_date: date,
) -> list:
    """Returns a list of problems (empty if the result is acceptable)."""
    problems = []

    for field_name in RUNNER_ENVELOPE_FIELDS:
        if field_name in ("execution_id", "started_at", "finished_at", "review_file", "error"):
            continue  # supplied/validated by the runner itself, not required from Claude
        if field_name not in result:
            problems.append(f"missing required field: {field_name}")

    if result.get("workflow") != expected_workflow:
        problems.append(f"workflow mismatch: expected {expected_workflow!r}, got {result.get('workflow')!r}")
    if result.get("execution_mode") != expected_execution_mode:
        problems.append(
            f"execution_mode mismatch: expected {expected_execution_mode!r}, got {result.get('execution_mode')!r}"
        )
    if result.get("local_date") != expected_local_date.isoformat():
        problems.append(
            f"local_date mismatch: expected {expected_local_date.isoformat()!r}, got {result.get('local_date')!r}"
        )

    status = result.get("status")
    if status is not None and status not in VALID_STATUSES:
        problems.append(f"invalid status: {status!r}")

    confidence = result.get("confidence")
    if confidence is not None and confidence not in VALID_CONFIDENCE:
        problems.append(f"invalid confidence: {confidence!r}")

    notification_level = result.get("notification_level")
    if notification_level is not None and notification_level not in VALID_NOTIFICATION_LEVELS:
        problems.append(f"invalid notification_level: {notification_level!r}")

    if "human_readable_review" not in result or not isinstance(result.get("human_readable_review"), str):
        problems.append("missing or non-string human_readable_review")

    return problems


# --------------------------------------------------------------------------
# Review storage
# --------------------------------------------------------------------------

def review_path_for(repo_root: Path, workflow_review_type: str, local_date: date) -> Path:
    return repo_root / "reviews" / workflow_review_type / f"{local_date.isoformat()}.md"


def write_review_file(canonical_path: Path, content: str, execution_mode: str) -> Path:
    """Idempotency per workflows/daily-review.md Storage and Idempotency:
    one completed review per athlete local date; never silently overwritten.
    A manual rerun when a completed review already exists is written under a
    clearly distinct filename instead of overwriting the scheduled review."""
    canonical_path.parent.mkdir(parents=True, exist_ok=True)

    if not canonical_path.exists():
        canonical_path.write_text(content, encoding="utf-8")
        return canonical_path

    if execution_mode == "automated":
        raise DuplicateReviewError(f"Completed review already exists: {canonical_path}")

    # Manual rerun with an existing review: write alongside it, clearly labeled.
    timestamp_suffix = datetime.now(dt_timezone.utc).strftime("%H%M%SZ")
    manual_path = canonical_path.with_name(f"{canonical_path.stem}.manual-{timestamp_suffix}{canonical_path.suffix}")
    manual_path.write_text(content, encoding="utf-8")
    return manual_path


# --------------------------------------------------------------------------
# Logging (operational only -- never coaching content, never secrets)
# --------------------------------------------------------------------------

def log_event(config: Optional[RunnerConfig], **fields: Any) -> None:
    safe_fields = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in fields.items()}
    safe_fields.setdefault("logged_at", datetime.now(dt_timezone.utc).isoformat())
    line = json.dumps(safe_fields, sort_keys=True)
    print(line, file=sys.stderr)
    if config and config.log_file:
        try:
            config.log_file.parent.mkdir(parents=True, exist_ok=True)
            with config.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # logging failure must never crash the workflow run


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

# Only the daily review exists so far; each entry names the review-type
# subdirectory under reviews/ that the workflow writes into (see Extensibility
# in runner/workflow-runner.md for how a future workflow registers here).
WORKFLOW_REVIEW_TYPES = {
    "daily-review": "daily",
}


def run_workflow(
    workflow_name: str,
    execution_mode: str,
    config: RunnerConfig,
    athlete_context: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    exec_id = execution_id()
    started_at = datetime.now(dt_timezone.utc)

    result: dict = {
        "workflow": workflow_name,
        "status": "failed",
        "execution_mode": execution_mode,
        "execution_id": exec_id,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "local_date": None,
        "review_file": None,
        "notification_required": True,
        "notification_level": "urgent",
        "confidence": None,
        "limitations": [],
        "error": None,
        # Set for real only in the finally block below, for executions that
        # actually reached the Notify stage (i.e. held the workflow lock).
        # Left at these defaults for early rejections (invalid mode, unknown
        # workflow, etc.) where no delivery was ever attempted.
        "notification_delivered": False,
        "notification_delivery_error": None,
    }

    if execution_mode not in VALID_EXECUTION_MODES:
        result["error"] = f"invalid execution_mode: {execution_mode}"
        result["finished_at"] = datetime.now(dt_timezone.utc).isoformat()
        return result

    if execution_mode == "automated" and athlete_context:
        result["error"] = "athlete_context is not permitted in automated mode"
        result["finished_at"] = datetime.now(dt_timezone.utc).isoformat()
        return result

    local_dt = athlete_local_now(config.athlete_timezone, now=now)
    local_date = local_dt.date()
    result["local_date"] = local_date.isoformat()

    log_event(config, event="workflow_start", workflow=workflow_name, execution_id=exec_id,
              execution_mode=execution_mode, local_date=result["local_date"])

    workflow_file = workflow_definition_path(config.repo_root, workflow_name)
    if not workflow_file.is_file():
        result["error"] = f"unknown workflow: {workflow_name} (no {workflow_file} found)"
        result["finished_at"] = datetime.now(dt_timezone.utc).isoformat()
        log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id, status="failed",
                  error_category="unknown_workflow")
        return result

    review_type = WORKFLOW_REVIEW_TYPES.get(workflow_name)
    canonical_review_path = (
        review_path_for(config.repo_root, review_type, local_date) if review_type else None
    )

    lock_dir = config.repo_root / "runtime" / "locks"
    lock_key = f"{workflow_name}-{local_date.isoformat()}"
    lock = WorkflowLock(lock_dir, lock_key, config.lock_stale_seconds)

    try:
        lock.acquire()
    except DuplicateExecutionError as exc:
        result["status"] = "duplicate_skipped"
        result["notification_required"] = False
        result["notification_level"] = "routine"
        result["error"] = str(exc)
        result["finished_at"] = datetime.now(dt_timezone.utc).isoformat()
        log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                  status="duplicate_skipped", error_category="lock_held")
        return result

    try:
        governing_documents = parse_governing_documents(workflow_file)
        prompt = build_prompt(
            workflow_name=workflow_name,
            workflow_file=workflow_file,
            governing_documents=governing_documents,
            execution_mode=execution_mode,
            local_date=local_date,
            athlete_timezone=config.athlete_timezone,
            athlete_context=athlete_context,
        )

        log_event(config, event="claude_invoke_start", workflow=workflow_name, execution_id=exec_id)
        try:
            completed = invoke_claude(prompt, config)
        except subprocess.TimeoutExpired:
            result["error"] = "Claude execution timed out"
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="failed", error_category="timeout")
            return result
        except FileNotFoundError as exc:
            result["error"] = f"Claude binary not found: {redact_secrets(str(exc))}"
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="failed", error_category="claude_not_found")
            return result

        if completed.returncode != 0:
            result["error"] = redact_secrets(
                f"Claude exited with status {completed.returncode}: {completed.stderr.strip()[:500]}"
            )
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="failed", error_category="claude_execution_failure")
            return result

        try:
            structured = parse_claude_output(completed.stdout)
        except MalformedOutputError as exc:
            result["error"] = str(exc)
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="failed", error_category="malformed_output")
            return result

        problems = validate_structured_result(structured, workflow_name, execution_mode, local_date)
        if problems:
            result["error"] = "structured output failed validation: " + "; ".join(problems)
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="failed", error_category="malformed_output")
            return result

        # Structured output is valid -- adopt its coaching-facing fields.
        # recommendation_summary is a workflow-specific field (not part of
        # the runner's own envelope contract) carried through only so the
        # Notify stage has a short summary to deliver.
        for key in ("confidence", "notification_level", "notification_required", "limitations",
                    "recommendation_summary"):
            if key in structured:
                result[key] = structured[key]

        review_content = structured["human_readable_review"]

        if canonical_review_path is not None:
            try:
                written_path = write_review_file(canonical_review_path, review_content, execution_mode)
            except DuplicateReviewError as exc:
                result["status"] = "duplicate_skipped"
                result["notification_required"] = False
                result["notification_level"] = "routine"
                result["error"] = str(exc)
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="duplicate_skipped", error_category="review_already_exists")
                return result
            except OSError as exc:
                result["error"] = f"failed to write review file: {redact_secrets(str(exc))}"
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="failed", error_category="storage_failure")
                return result

            result["review_file"] = str(written_path.relative_to(config.repo_root))

        result["status"] = structured.get("status") or "completed"
        result["error"] = None

        log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                  status=result["status"], review_file=result["review_file"],
                  notification_status=result["notification_level"])
        return result
    except Exception as exc:  # noqa: BLE001 -- last resort: report, never crash silently
        result["status"] = "failed"
        result["error"] = redact_secrets(f"unexpected error: {exc}")
        log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                  status="failed", error_category="unexpected_error")
        return result
    finally:
        result["finished_at"] = datetime.now(dt_timezone.utc).isoformat()
        # Notify stage (runner/telegram-integration.md): attempted for every
        # execution that reached this point, after the review is already
        # durably saved (or the run otherwise reached a terminal state) and
        # before the lock is released. A delivery failure -- or Telegram not
        # being configured at all, the normal state today -- must never
        # crash the workflow or change its already-decided status.
        try:
            delivery = telegram_delivery.deliver_review(
                result, config.telegram_bot_token, config.telegram_chat_id,
            )
            result["notification_delivered"] = delivery["notification_delivered"]
            result["notification_delivery_error"] = delivery["notification_delivery_error"]
        except Exception as exc:  # noqa: BLE001 -- notification must never mask the workflow result
            result["notification_delivered"] = False
            result["notification_delivery_error"] = redact_secrets(f"unexpected notify error: {exc}")
        lock.release()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reusable non-interactive coaching workflow runner.")
    parser.add_argument("--workflow", required=True, help="Workflow name, e.g. daily-review")
    parser.add_argument("--mode", required=True, choices=sorted(VALID_EXECUTION_MODES))
    parser.add_argument("--context", default=None, help="Athlete-provided context (manual mode only)")
    parser.add_argument("--env-file", default=None, help="Path to an env file (defaults to runner/config.env)")
    parser.add_argument("--repo-root", default=None, help="Override the resolved repository root")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    repo_root_for_env = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()
    env_file_path = Path(args.env_file) if args.env_file else (repo_root_for_env / "runner" / "config.env")
    file_env = load_env_file(env_file_path)

    merged_env = dict(file_env)
    merged_env.update(os.environ)  # real environment variables win over the file

    try:
        config = RunnerConfig.from_env(merged_env, repo_root_override=args.repo_root)
    except ConfigError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stdout)
        return 2

    result = run_workflow(
        workflow_name=args.workflow,
        execution_mode=args.mode,
        config=config,
        athlete_context=args.context,
    )

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("completed", "completed_with_limitations", "duplicate_skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
