#!/usr/bin/env python3
"""Telegram delivery layer for completed coaching workflow results.

Implements the architecture defined in runner/telegram-integration.md: loads
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the same server-local env file the
rest of the runner uses, formats a condensed summary per notification level
(routine/attention/urgent), splits it into ordered parts if it exceeds
Telegram's message-length limit, sends it through the Telegram Bot API, and
reports a structured delivery result.

This module knows nothing about AthleteData, Claude, review-file storage, or
coaching logic -- it only formats and transmits a summary of a result someone
else already decided. It never raises out to a caller: a delivery failure
must never invalidate an already-saved review (see telegram-integration.md's
Delivery Failure Handling section). Standard library only, no third-party
HTTP client, so this module has no dependency beyond the Python interpreter
itself.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096

# One retry for transient (network / 5xx) failures only -- never an
# indefinite or exponential retry loop, per telegram-integration.md's
# Retry Rules.
MAX_SEND_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2

VALID_NOTIFICATION_LEVELS = ("routine", "attention", "urgent")

_LEVEL_MARKER = {"routine": "", "attention": "[ATTENTION] ", "urgent": "[URGENT] "}


# --------------------------------------------------------------------------
# Secret redaction (self-contained -- this module deliberately does not
# import workflow_runner, to keep the delivery layer a one-way dependency:
# the runner may call into this module, this module must never call back
# into the runner or know it exists).
# --------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(apiKey|api_key|token|secret|chat_id)=([^&\s]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.]+", re.IGNORECASE),
    # Telegram Bot API URLs embed the token directly in the path:
    # https://api.telegram.org/bot<TOKEN>/sendMessage
    re.compile(r"(/bot)[A-Za-z0-9:_-]+", re.IGNORECASE),
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

class TelegramDeliveryError(Exception):
    """Base class for delivery-layer errors. Never escapes this module's
    public functions -- always converted into a structured result instead."""


class TelegramNetworkError(TelegramDeliveryError):
    pass


class TelegramAPIError(TelegramDeliveryError):
    def __init__(self, status_code: int, message: str, retry_after: Optional[int] = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


def load_telegram_config(env: Mapping[str, str]) -> Optional[TelegramConfig]:
    """Returns None (not an exception) when either value is missing or
    blank -- "not configured" is an expected, ordinary state for this
    system (Telegram is optional until explicitly set up), not an error."""
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    return TelegramConfig(bot_token=token, chat_id=chat_id)


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def build_message(
    *,
    workflow: str,
    local_date: str,
    notification_level: str,
    confidence: Optional[str],
    review_message: str,
    review_file: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """Builds the condensed, athlete-facing summary text. Never includes raw
    AthleteData, full review markdown, or execution metadata -- per
    telegram-integration.md's Message Formatting Rules, this is a headline
    plus a pointer to the full review file, not the review itself."""
    marker = _LEVEL_MARKER.get(notification_level, "")
    header = f"{marker}{workflow} — {local_date}"

    if status == "failed":
        return f"{header}\n\nReview could not be completed: {error or 'unknown error'}"

    lines = [header]
    if review_message:
        lines.append(review_message)
    lines.append(f"Confidence: {confidence or 'unknown'} | Notification: {notification_level}")
    if review_file:
        lines.append(f"Full review: {review_file}")
    return "\n\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list:
    """Splits text into ordered parts, each <= limit characters, breaking at
    paragraph/line/word boundaries rather than mid-word where possible.
    Order of the returned list is send order -- callers must send parts in
    that order and stop on the first failure (see send_telegram_notification)
    rather than risk delivering them out of sequence."""
    if len(text) <= limit:
        return [text]

    # Reserve room for the "(i/N) " ordering prefix added below, so tagging
    # a chunk afterward can never push it back over the limit.
    prefix_reserve = 16
    chunk_limit = max(limit - prefix_reserve, 1)

    chunks = []
    remaining = text
    while len(remaining) > chunk_limit:
        split_at = remaining.rfind("\n\n", 0, chunk_limit)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, chunk_limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, chunk_limit)
        if split_at <= 0:
            split_at = chunk_limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)

    total = len(chunks)
    if total > 1:
        chunks = [f"({i}/{total}) {chunk}" for i, chunk in enumerate(chunks, start=1)]
    return chunks


# --------------------------------------------------------------------------
# Logging (operational only -- never message content beyond what's already
# redacted, never secrets)
# --------------------------------------------------------------------------

def _default_log(**fields: Any) -> None:
    safe_fields = {k: (redact_secrets(v) if isinstance(v, str) else v) for k, v in fields.items()}
    safe_fields.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    print(json.dumps(safe_fields, sort_keys=True), file=sys.stderr)


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

def _default_http_post(url: str, data: dict, timeout: float = 10.0):
    """Real Telegram Bot API call via urllib (stdlib only). Returns
    (status_code, response_body_text). Raises TelegramNetworkError for
    connection-level failures (DNS, timeout, refused connection, etc.) so
    callers can distinguish "never reached Telegram" from "Telegram
    responded with an error"."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body
    except urllib.error.URLError as exc:
        raise TelegramNetworkError(str(exc.reason)) from exc


def _extract_api_error(response_body: str) -> str:
    try:
        payload = json.loads(response_body)
        if isinstance(payload, dict) and payload.get("description"):
            return str(payload["description"])
    except (json.JSONDecodeError, TypeError):
        pass
    return response_body[:200] if response_body else "no response body"


def _extract_retry_after(response_body: str) -> Optional[int]:
    try:
        payload = json.loads(response_body)
        value = payload.get("parameters", {}).get("retry_after")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _extract_message_id(response_body: str) -> Optional[int]:
    try:
        payload = json.loads(response_body)
        value = payload.get("result", {}).get("message_id")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _send_part(config: TelegramConfig, text: str, http_post: Callable) -> Optional[int]:
    """One sendMessage call. Returns Telegram's message_id when the reply
    carries one."""
    url = f"{TELEGRAM_API_BASE}/bot{config.bot_token}/sendMessage"
    data = {"chat_id": config.chat_id, "text": text}
    status, body = http_post(url, data)
    if status != 200:
        raise TelegramAPIError(status, _extract_api_error(body), _extract_retry_after(body))
    return _extract_message_id(body)


def _send_part_with_retry(
    config: TelegramConfig,
    text: str,
    http_post: Callable,
    sleep_fn: Callable[[float], None],
) -> "tuple[bool, Optional[str]]":
    last_error = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            _send_part(config, text, http_post)
            return True, None
        except TelegramAPIError as exc:
            last_error = redact_secrets(str(exc))
            if 400 <= exc.status_code < 500:
                # Configuration problem (bad token, bad chat id) -- retrying
                # cannot fix this and risks rate-limiting the bot.
                break
            # 5xx: treat as transient, allow one retry.
        except TelegramNetworkError as exc:
            last_error = redact_secrets(f"network error: {exc}")
            # Transient, allow one retry.
        except Exception as exc:  # noqa: BLE001 -- never let an unexpected error escape unredacted
            last_error = redact_secrets(f"unexpected error: {exc}")
            break

        if attempt < MAX_SEND_ATTEMPTS:
            sleep_fn(RETRY_DELAY_SECONDS)

    return False, last_error


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def send_telegram_notification(
    config: Optional[TelegramConfig],
    *,
    workflow: str,
    local_date: str,
    notification_level: str,
    notification_required: bool,
    confidence: Optional[str] = None,
    review_message: str = "",
    review_file: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    http_post: Optional[Callable] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    log: Optional[Callable[..., None]] = None,
) -> dict:
    """The Telegram sender. Formats and sends a routine/attention/urgent
    notification and always returns a structured delivery result -- never
    raises. Workflow-agnostic: takes only generic fields, so it is reusable
    unchanged by Weekly and Monthly Review once they exist (see
    telegram-integration.md's Reuse section)."""
    http_post = http_post or _default_http_post
    sleep_fn = sleep_fn or time.sleep
    log = log or _default_log

    result = {
        "notification_delivered": False,
        "notification_delivery_error": None,
        "notification_skipped": False,
        "parts_sent": 0,
    }

    if config is None:
        result["notification_skipped"] = True
        result["notification_delivery_error"] = (
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing)"
        )
        log(event="notify_skipped", reason="not_configured", notification_level=notification_level)
        return result

    if not notification_required:
        result["notification_skipped"] = True
        log(event="notify_skipped", reason="not_required", notification_level=notification_level)
        return result

    if notification_level not in VALID_NOTIFICATION_LEVELS:
        result["notification_skipped"] = True
        result["notification_delivery_error"] = f"invalid notification_level: {notification_level!r}"
        log(event="notify_skipped", reason="invalid_level", notification_level=str(notification_level))
        return result

    text = build_message(
        workflow=workflow,
        local_date=local_date,
        notification_level=notification_level,
        confidence=confidence,
        review_message=review_message,
        review_file=review_file,
        status=status,
        error=error,
    )
    parts = split_message(text)

    log(event="notify_start", workflow=workflow, notification_level=notification_level, parts_total=len(parts))

    sent = 0
    for part in parts:
        ok, err = _send_part_with_retry(config, part, http_post, sleep_fn)
        if not ok:
            result["notification_delivery_error"] = err
            result["parts_sent"] = sent
            log(event="notify_end", status="failed", parts_sent=sent, parts_total=len(parts), error=err)
            return result
        sent += 1

    result["notification_delivered"] = True
    result["parts_sent"] = sent
    log(event="notify_end", status="sent", parts_sent=sent, parts_total=len(parts))
    return result


def send_single_message(config: TelegramConfig, text: str, *, http_post: Optional[Callable] = None) -> Optional[int]:
    """Exactly one sendMessage attempt, no retry, for callers that track
    delivery state themselves (the feedback dispatcher's outbox). Returns the
    message_id; raises TelegramAPIError or TelegramNetworkError (both with
    redacted messages), or the transport's own timeout exception."""
    try:
        return _send_part(config, text, http_post or _default_http_post)
    except TelegramAPIError as exc:
        raise TelegramAPIError(exc.status_code, redact_secrets(str(exc)), exc.retry_after) from None
    except TelegramNetworkError as exc:
        raise TelegramNetworkError(redact_secrets(str(exc))) from None


def deliver_review(
    result: dict,
    bot_token: Optional[str],
    chat_id: Optional[str],
    *,
    http_post: Optional[Callable] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    log: Optional[Callable[..., None]] = None,
) -> dict:
    """Adapter used by workflow_runner.py's Notify stage: pulls the common
    envelope fields out of a finished runner result dict and sends a
    delivery through send_telegram_notification(). Workflow-agnostic --
    reads only fields every workflow's Structured Result Contract already
    commits to producing, so it works unchanged for daily, weekly, or
    monthly review results."""
    config = load_telegram_config(
        {"TELEGRAM_BOT_TOKEN": bot_token or "", "TELEGRAM_CHAT_ID": chat_id or ""}
    )
    return send_telegram_notification(
        config,
        workflow=result.get("workflow") or "unknown",
        local_date=result.get("local_date") or "unknown",
        notification_level=result.get("notification_level") or "routine",
        notification_required=bool(result.get("notification_required")),
        confidence=result.get("confidence"),
        review_message=result.get("recommendation_summary") or "",
        review_file=result.get("review_file"),
        status=result.get("status"),
        error=result.get("error"),
        http_post=http_post,
        sleep_fn=sleep_fn,
        log=log,
    )
