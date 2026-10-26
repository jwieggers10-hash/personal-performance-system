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
from typing import Any, Callable, Mapping, Optional

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

# At most one retry, and only for a malformed/invalid structured response --
# never for a timeout, a nonzero Claude CLI exit, or any other failure mode.
# See build_retry_prompt() and the retry loop in run_workflow().
MAX_CLAUDE_ATTEMPTS = 2

# Redact anything that looks like a secret before it can reach a log line or
# an error field. Belt-and-suspenders on top of never passing secrets into
# strings we log in the first place. Order matters: whole URL query strings
# (where the AthleteData MCP key lives) and full Authorization header values
# are removed first, so the narrower patterns below only see what remains.
_SECRET_PATTERNS = (
    # Any URL's entire query string -- not just known parameter names.
    re.compile(r"(https?://[^\s?#\"'<>]+\?)[^\s#\"'<>]+", re.IGNORECASE),
    # Authorization / X-Api-Key header values, with or without a scheme.
    re.compile(
        r"(\b(?:authorization|proxy-authorization|x-api-key)[\"']?\s*[:=]\s*[\"']?)"
        r"(?:(?:bearer|basic|token)\s+)?[^\s\"',;}]+",
        re.IGNORECASE,
    ),
    # JSON-style credential fields: "api_key": "...", "accessToken": "...", etc.
    re.compile(
        r"(\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|"
        r"client[_-]?secret|password|bot[_-]?token|oauth[_-]?token)\"\s*:\s*\")[^\"]*",
        re.IGNORECASE,
    ),
    # key=value pairs outside a URL (e.g. env-file or CLI echoes).
    re.compile(r"(apiKey|api_key|token|secret|password)=([^&\s]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.]+", re.IGNORECASE),
    # Anthropic API / OAuth keys (sk-ant-...) and similar sk_/sk- prefixed keys.
    re.compile(r"\b(sk[-_])[A-Za-z0-9_\-]{8,}"),
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
    prompt_supplement: Optional[str] = None,
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
    supplement_block = f"\n{prompt_supplement.strip()}\n" if prompt_supplement else ""

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
{context_block}{supplement_block}
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
  structure defined in the workflow definition, as a single string containing ordinary multi-line
  text with real, actual line breaks between lines -- never the literal two-character sequence
  backslash-n in place of a line break.

If a responsible recommendation cannot be made because essential information is missing, set
"status" to "completed_with_limitations" or use recommendation_category "more_information_needed"
as defined in the workflow -- do not invent an answer.
"""


def build_retry_prompt(original_prompt: str, issue: str) -> str:
    """Builds the corrective prompt for the single permitted retry when
    Claude's previous response violated the structured-output contract
    (malformed JSON, or a validate_structured_result() failure).

    This is a fresh, independent Claude invocation -- not a resumed session --
    so it carries no memory of the prior attempt. The correction below says so
    plainly (asking Claude to redo the whole task, including data retrieval)
    rather than implying a continuity that doesn't exist.
    """
    return (
        f"{original_prompt}\n"
        "---\n"
        "IMPORTANT: A previous attempt at this exact task failed because its final "
        "response violated the required structured-output contract above and could "
        "not be used. The specific problem was:\n"
        f"{redact_secrets(issue)}\n\n"
        "This is a fresh attempt with no memory of the previous one -- complete the "
        "task again in full (retrieve fresh AthleteData, analyze it per the governing "
        "documents, as instructed above). This time, make sure your final message is "
        "ONLY the raw JSON object described above: no markdown code fences, no "
        "explanatory text before or after it, and nothing else.\n"
    )


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

    def is_held(self) -> bool:
        """Another process holds this lock: the file exists and is not stale
        by the same rule acquire() applies. Read-only."""
        return self.path.exists() and not self._is_stale()

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

# Fields whose *value* is constrained beyond "some string" -- driven by the
# same VALID_* constants validate_structured_result() checks against, so the
# CLI-enforced schema below and the runner's own semantic validation can
# never silently drift apart.
_SCHEMA_ENUM_FIELDS = {
    "status": sorted(VALID_STATUSES),
    "execution_mode": sorted(VALID_EXECUTION_MODES),
    "confidence": sorted(VALID_CONFIDENCE),
    "notification_level": sorted(VALID_NOTIFICATION_LEVELS),
}
_SCHEMA_ARRAY_OF_STRING_FIELDS = {"limitations", "missing_data", "integration_failures"}
_SCHEMA_BOOLEAN_FIELDS = {"notification_required"}

# Fields the runner itself supplies/validates rather than requiring them from
# Claude -- mirrors validate_structured_result()'s own exclusion list exactly
# (see the loop over RUNNER_ENVELOPE_FIELDS at the top of that function).
_SCHEMA_RUNNER_SUPPLIED_FIELDS = {"execution_id", "started_at", "finished_at", "review_file", "error"}


def build_structured_output_json_schema() -> dict:
    """JSON Schema passed to `claude --json-schema`, built directly from this
    module's own envelope/enum constants (RUNNER_ENVELOPE_FIELDS,
    WORKFLOW_RESULT_FIELDS, VALID_*) so CLI-level schema enforcement can never
    silently drift from validate_structured_result()'s semantic checks below.

    This schema enforces shape, type, and enum membership only -- it is a
    first line of defense, not a replacement for validate_structured_result(),
    which still runs unchanged on whatever comes back from Claude.
    """
    all_fields = list(RUNNER_ENVELOPE_FIELDS) + list(WORKFLOW_RESULT_FIELDS) + ["human_readable_review"]

    properties: dict = {}
    for field_name in all_fields:
        if field_name in _SCHEMA_ENUM_FIELDS:
            properties[field_name] = {"type": "string", "enum": _SCHEMA_ENUM_FIELDS[field_name]}
        elif field_name in _SCHEMA_ARRAY_OF_STRING_FIELDS:
            properties[field_name] = {"type": "array", "items": {"type": "string"}}
        elif field_name in _SCHEMA_BOOLEAN_FIELDS:
            properties[field_name] = {"type": "boolean"}
        elif field_name == "human_readable_review":
            properties[field_name] = {"type": "string"}
        else:
            properties[field_name] = {"type": ["string", "null"]}

    required = [f for f in all_fields if f not in _SCHEMA_RUNNER_SUPPLIED_FIELDS]

    return {"type": "object", "properties": properties, "required": required}


@dataclass(frozen=True)
class ClaudeToolPolicy:
    """An enforced tool restriction for one Claude invocation, replacing the
    configured permission mode and allowed tools. With permission_mode
    "dontAsk" only allowed_tools can run; disallowed_tools are denied in every
    mode; `tools` limits the built-in tool set (MCP tools are unaffected)."""
    tools: str
    permission_mode: str
    allowed_tools: str
    disallowed_tools: str


def invoke_claude(prompt: str, config: RunnerConfig,
                  json_schema: Optional[dict] = None,
                  tool_policy: Optional[ClaudeToolPolicy] = None) -> subprocess.CompletedProcess:
    """json_schema defaults to the Daily Review contract; other callers (the
    feedback dispatcher) pass their own. Without tool_policy the argv is the
    one the Daily Review and dispatcher have always used."""
    argv = [
        config.claude_binary,
        "--print",
        "--output-format", "json",
        "--permission-mode", tool_policy.permission_mode if tool_policy else config.permission_mode,
        # --allowedTools is variadic in the Claude Code CLI (accepts <tools...>);
        # passed as two argv elements it would swallow the prompt argument that
        # follows. The "=" form keeps it a single argv element.
        f"--allowedTools={tool_policy.allowed_tools if tool_policy else config.allowed_tools}",
        # --json-schema is a single-value (non-variadic) flag, so it's safe as
        # two argv elements. Enforces the Structured Result Contract's shape
        # at the CLI boundary -- see build_structured_output_json_schema().
        "--json-schema", json.dumps(json_schema if json_schema is not None
                                    else build_structured_output_json_schema()),
    ]
    if tool_policy:
        # Both variadic as well, hence the "=" form.
        argv += [f"--tools={tool_policy.tools}", f"--disallowedTools={tool_policy.disallowed_tools}"]
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
    """Defensively parse Claude's --output-format json response. When
    --json-schema was honored, the CLI provides an already-validated
    "structured_output" object directly on the outer response -- preferred
    here since it needs no further JSON parsing and is guaranteed schema-valid
    by the CLI itself. Otherwise, falls back to the older path: the outer
    object wraps the final answer text under a "result" key, and that text is
    expected to itself be the JSON object this workflow was asked to produce.
    Any failure at either layer is reported as malformed output rather than
    guessed at -- no fence-stripping, no permissive repair."""
    try:
        outer = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(f"Claude output was not valid JSON: {redact_secrets(str(exc))}") from exc

    if isinstance(outer, dict) and isinstance(outer.get("structured_output"), dict):
        return outer["structured_output"]

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


# --------------------------------------------------------------------------
# Nonzero-exit diagnosis
# --------------------------------------------------------------------------

# With --output-format json the Claude CLI reports failures (e.g. "Not logged
# in · Please run /login", "Invalid API key") as a JSON result object on
# stdout and exits 1 with an empty stderr -- so stderr alone carries no
# diagnosis. Only these fixed-vocabulary metadata fields of that object are
# ever logged. Free text ("result") is read solely to match the fixed signals
# below and is never logged or forwarded, since on other failure modes it
# could carry coaching content. Note "subtype" can read "success" even when
# is_error is true, so it is recorded but never used for classification.
_CLI_DIAGNOSTIC_FIELDS = (
    "type", "subtype", "terminal_reason", "is_error", "api_error_status", "api_error_origin", "num_turns",
)
_SAFE_DIAGNOSTIC_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# (error_category, signal, pattern) -- first match wins, so more specific
# signals come first within each category, and auth precedes quota.
_CLAUDE_FAILURE_SIGNALS = (
    ("claude_auth_failure", "oauth_token_expired",
     re.compile(r"oauth token (?:has )?expired|token (?:has )?expired|token.{0,20}revoked", re.IGNORECASE)),
    ("claude_auth_failure", "invalid_api_key", re.compile(r"invalid (?:x-)?api[ -]key", re.IGNORECASE)),
    ("claude_auth_failure", "not_logged_in", re.compile(r"not logged in|/login\b", re.IGNORECASE)),
    ("claude_auth_failure", "authentication_failed",
     re.compile(r"failed to authenticate|authentication[_ ]error|unauthori[sz]ed", re.IGNORECASE)),
    ("claude_quota_failure", "usage_limit_reached",
     re.compile(r"usage limit|hit your limit|limit reached|limit will reset", re.IGNORECASE)),
    ("claude_quota_failure", "credit_balance_too_low", re.compile(r"credit balance", re.IGNORECASE)),
    ("claude_quota_failure", "rate_limited", re.compile(r"rate[_ ]limit", re.IGNORECASE)),
    ("claude_quota_failure", "max_budget_exceeded", re.compile(r"max(?:imum)?[_ ]budget", re.IGNORECASE)),
)
_CLAUDE_FAILURE_STATUS_SIGNALS = {
    401: ("claude_auth_failure", "http_401"),
    403: ("claude_auth_failure", "http_403"),
    429: ("claude_quota_failure", "http_429"),
}

# Fixed, athlete-safe guidance per category. This text (not CLI output) is
# what reaches result["error"] and therefore the Telegram failure message.
_CLAUDE_FAILURE_GUIDANCE = {
    "claude_auth_failure": "Claude Code authentication failed or expired; "
                           "re-authenticate the coach account on the server.",
    "claude_quota_failure": "Claude usage, rate, or budget limit reached; rerun once the limit resets.",
    "claude_execution_failure": "Claude Code execution failed; see the runner log for diagnostics.",
}


@dataclass(frozen=True)
class ClaudeFailureDiagnosis:
    error_category: str
    signal: Optional[str]
    stdout_format: str  # "json", "non_json", or "empty"
    cli_fields: dict = field(default_factory=dict)  # allowlisted, validated values only


def diagnose_claude_failure(stdout: Optional[str], stderr: Optional[str]) -> ClaudeFailureDiagnosis:
    stdout = stdout or ""
    stderr = stderr or ""
    cli_fields: dict = {}
    texts = [stderr]

    if not stdout.strip():
        stdout_format = "empty"
    else:
        try:
            outer = json.loads(stdout)
        except json.JSONDecodeError:
            outer = None
        if isinstance(outer, dict):
            stdout_format = "json"
            for name in _CLI_DIAGNOSTIC_FIELDS:
                value = outer.get(name)
                if isinstance(value, bool):
                    cli_fields[name] = value
                elif isinstance(value, int) and 0 <= value < 100000:
                    cli_fields[name] = value
                elif isinstance(value, str) and _SAFE_DIAGNOSTIC_TOKEN.match(value):
                    cli_fields[name] = value
            for name in ("result", "error", "terminal_reason"):
                if isinstance(outer.get(name), str):
                    texts.append(outer[name])
        else:
            stdout_format = "non_json"
            texts.append(stdout[-2000:])

    combined = "\n".join(texts)
    for category, signal, pattern in _CLAUDE_FAILURE_SIGNALS:
        if pattern.search(combined):
            return ClaudeFailureDiagnosis(category, signal, stdout_format, cli_fields)

    status = cli_fields.get("api_error_status")
    if not isinstance(status, bool) and status in _CLAUDE_FAILURE_STATUS_SIGNALS:
        category, signal = _CLAUDE_FAILURE_STATUS_SIGNALS[status]
        return ClaudeFailureDiagnosis(category, signal, stdout_format, cli_fields)

    return ClaudeFailureDiagnosis("claude_execution_failure", None, stdout_format, cli_fields)


def describe_claude_failure(returncode: int, diagnosis: ClaudeFailureDiagnosis, stderr: Optional[str]) -> str:
    """Builds result["error"] for a nonzero exit: fixed category text plus
    the (redacted, truncated) stderr excerpt the runner has always included.
    Never includes stdout, which may carry coaching content."""
    label = diagnosis.error_category + (f": {diagnosis.signal}" if diagnosis.signal else "")
    message = f"Claude exited with status {returncode} ({label}). {_CLAUDE_FAILURE_GUIDANCE[diagnosis.error_category]}"
    stderr_excerpt = (stderr or "").strip()[:500]
    if stderr_excerpt:
        message += f" stderr: {stderr_excerpt}"
    return redact_secrets(message)


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

# Each entry names the review-type subdirectory under reviews/ that the
# workflow writes into (see Extensibility in runner/workflow-runner.md for how
# a future workflow registers here).
WORKFLOW_REVIEW_TYPES = {
    "daily-review": "daily",
    "evening-nutrition-review": "nutrition",
}

# Workflows that need runner-computed facts in their prompt and so must be
# started through their own entry point, never through this CLI directly.
WORKFLOW_ENTRY_POINTS = {
    "evening-nutrition-review": "runner/evening_nutrition_review.py",
}


def run_workflow(
    workflow_name: str,
    execution_mode: str,
    config: RunnerConfig,
    athlete_context: Optional[str] = None,
    now: Optional[datetime] = None,
    prepare_prompt: Optional[Callable[[datetime], Optional[str]]] = None,
    tool_policy_factory: Optional[Callable[[], ClaudeToolPolicy]] = None,
) -> dict:
    """prepare_prompt, when given, runs under the workflow lock after the
    duplicate check and before Claude; its text is added to the prompt (the
    Evening Nutrition Review passes its computed facts this way).
    tool_policy_factory, when given, is evaluated at the same point and its
    policy is enforced on every Claude invocation of the run."""
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
        # Automated runs never overwrite a completed review, so if today's
        # canonical review already exists (e.g. a Persistent=true catch-up or
        # a manual `systemctl start` after the 07:00 run), skip before spending
        # a Claude invocation. Checked under the lock so a concurrent run
        # can't slip in between. Manual mode is unchanged: it still runs and
        # writes a distinct .manual-* file. The write-time DuplicateReviewError
        # guard below stays as the backstop.
        if execution_mode == "automated" and canonical_review_path is not None \
                and canonical_review_path.exists():
            result["status"] = "duplicate_skipped"
            result["notification_required"] = False
            result["notification_level"] = "routine"
            result["error"] = f"Completed review already exists: {canonical_review_path}"
            log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                      status="duplicate_skipped", error_category="review_already_exists",
                      skipped_stage="before_claude")
            return result

        prompt_supplement = prepare_prompt(local_dt) if prepare_prompt else None
        tool_policy = tool_policy_factory() if tool_policy_factory else None
        governing_documents = parse_governing_documents(workflow_file)
        prompt = build_prompt(
            workflow_name=workflow_name,
            workflow_file=workflow_file,
            governing_documents=governing_documents,
            execution_mode=execution_mode,
            local_date=local_date,
            athlete_timezone=config.athlete_timezone,
            athlete_context=athlete_context,
            prompt_supplement=prompt_supplement,
        )

        # Execute Claude -> Validate structured output, with at most one
        # bounded, explicitly-corrective retry (MAX_CLAUDE_ATTEMPTS) when the
        # response is malformed JSON or fails validate_structured_result().
        # Every other failure mode (timeout, missing binary, nonzero exit)
        # returns immediately below, without retrying -- retrying those would
        # not fix the underlying problem and is explicitly out of scope for
        # this loop. The loop reuses the same workflow execution and lock
        # (already acquired above); it never writes a review file or sends a
        # notification itself -- those stages, reached at most once each, are
        # unchanged and still happen only after the loop below produces a
        # validated `structured` result.
        attempt_prompt = prompt
        structured: Optional[dict] = None
        last_structural_error: Optional[str] = None

        for attempt in range(1, MAX_CLAUDE_ATTEMPTS + 1):
            log_event(config, event="claude_invoke_start", workflow=workflow_name, execution_id=exec_id,
                      attempt=attempt)
            try:
                completed = (invoke_claude(attempt_prompt, config, tool_policy=tool_policy) if tool_policy
                             else invoke_claude(attempt_prompt, config))
            except subprocess.TimeoutExpired:
                result["error"] = "Claude execution timed out"
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="failed", error_category="timeout", attempt=attempt)
                return result
            except FileNotFoundError as exc:
                result["error"] = f"Claude binary not found: {redact_secrets(str(exc))}"
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="failed", error_category="claude_not_found", attempt=attempt)
                return result

            if completed.returncode != 0:
                diagnosis = diagnose_claude_failure(completed.stdout, completed.stderr)
                result["error"] = describe_claude_failure(completed.returncode, diagnosis, completed.stderr)
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="failed", error_category=diagnosis.error_category,
                          failure_signal=diagnosis.signal, exit_code=completed.returncode,
                          stdout_format=diagnosis.stdout_format, stdout_bytes=len(completed.stdout or ""),
                          stderr_bytes=len(completed.stderr or ""), cli=diagnosis.cli_fields, attempt=attempt)
                return result

            try:
                candidate = parse_claude_output(completed.stdout)
                problems = validate_structured_result(candidate, workflow_name, execution_mode, local_date)
                if problems:
                    raise MalformedOutputError("structured output failed validation: " + "; ".join(problems))
            except MalformedOutputError as exc:
                last_structural_error = str(exc)
                if attempt < MAX_CLAUDE_ATTEMPTS:
                    log_event(config, event="malformed_output_retry", workflow=workflow_name,
                              execution_id=exec_id, attempt=attempt, issue=last_structural_error)
                    attempt_prompt = build_retry_prompt(prompt, last_structural_error)
                    continue
                result["error"] = last_structural_error
                log_event(config, event="workflow_end", workflow=workflow_name, execution_id=exec_id,
                          status="failed", error_category="malformed_output", attempts=attempt)
                return result

            structured = candidate
            break

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
                          status="duplicate_skipped", error_category="review_already_exists",
                          skipped_stage="at_write")
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
    if args.workflow in WORKFLOW_ENTRY_POINTS:
        print(json.dumps({"status": "failed", "error": f"run {args.workflow} via "
                                                       f"{WORKFLOW_ENTRY_POINTS[args.workflow]}"}))
        return 2

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
