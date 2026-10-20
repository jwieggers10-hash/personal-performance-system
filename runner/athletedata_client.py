#!/usr/bin/env python3
"""Read-only AthleteData MCP client for the detect-only event poller.

Speaks the AthleteData MCP Streamable HTTP transport directly (stdlib only):
initialize -> notifications/initialized -> tools/list -> tools/call. Verified
against production in Phase 4/4.1: the server is stateless (no
Mcp-Session-Id), answers with text/event-stream or JSON, and returns tool
results as JSON inside a text content block -- or as plain prose when a
source has no data.

Security properties (see runner/event-poller.md, Credential Handling):
- The endpoint URL (which embeds the API key in its query string) is read at
  runtime from Claude Code's own MCP registration and held only in memory. It
  is never logged, never written anywhere, and never part of an exception.
- Only tools in READ_ONLY_TOOL_ALLOWLIST can be called, and only after
  tools/list has confirmed each one exists and is annotated readOnlyHint=true.
- Prose tool output is discarded (only its length is kept): it is never
  logged or persisted, since it can describe the athlete's data.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from workflow_runner import redact_secrets


PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pps-event-poller", "version": "1"}
MCP_SERVER_NAME = "athletedata"

# The only tools this client will ever call. Every one is a read; write tools
# (hevy_create_workout, food_log_entry, push_workout_to_watch, ...) are
# rejected before any network request is made.
READ_ONLY_TOOL_ALLOWLIST = frozenset({
    "garmin_get_activities",
    "hevy_get_workout_events",
    "apple_health_get_body_composition",
})

DEFAULT_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2.0, 5.0)  # sleep before attempt 2 and attempt 3
MAX_RETRY_AFTER_SECONDS = 30.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_TOOLS_LIST_PAGES = 10


# --------------------------------------------------------------------------
# Errors -- every message is a fixed string; nothing from the URL, the
# request, or the response body is ever interpolated into one.
# --------------------------------------------------------------------------

class AthleteDataError(Exception):
    category = "athletedata_error"

    def __init__(self, message: str, category: Optional[str] = None):
        super().__init__(redact_secrets(message))
        if category:
            self.category = category


class CredentialUnavailableError(AthleteDataError):
    category = "credential_unavailable"


class TransportError(AthleteDataError):
    category = "network_error"


class HTTPStatusError(AthleteDataError):
    def __init__(self, status: int):
        self.status = status
        if status == 429:
            category = "rate_limited"
        elif status in (401, 403):
            category = "auth_failed"
        elif status >= 500:
            category = "http_5xx"
        else:
            category = "http_4xx"
        super().__init__(f"AthleteData MCP returned HTTP {status}", category)


class ProtocolError(AthleteDataError):
    category = "malformed_response"


class ToolPolicyError(AthleteDataError):
    category = "tool_not_allowed"


class ToolUnavailableError(AthleteDataError):
    category = "tool_unavailable"


class ToolReportedError(AthleteDataError):
    category = "tool_error"


# --------------------------------------------------------------------------
# Credential source
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class McpEndpoint:
    """The MCP URL and any registered headers. repr=False keeps both out of
    tracebacks, debug output and logs."""
    url: str = field(repr=False)
    headers: tuple = field(default=(), repr=False)


def default_claude_config_path() -> Path:
    # pwd rather than $HOME so the path is right under systemd as well.
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude.json"


def load_mcp_endpoint(config_path: Path, repo_root: Path) -> McpEndpoint:
    """Reads the AthleteData MCP registration that `claude mcp add` already
    stores for this repository (project scope, falling back to user scope).
    Nothing is copied: the file is read on every poll and the values live only
    in memory for that process. Refuses a config file readable by group/other."""
    try:
        mode = config_path.stat().st_mode
    except OSError:
        raise CredentialUnavailableError("Claude Code MCP config not found or not readable") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialUnavailableError("Claude Code MCP config permissions are broader than owner-only")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError:
        raise CredentialUnavailableError("Claude Code MCP config not readable") from None
    except json.JSONDecodeError:
        # Claude Code rewrites this file while it runs; a read can race a write.
        raise CredentialUnavailableError("Claude Code MCP config is not valid JSON (possibly mid-write)") from None

    server = None
    if isinstance(data, dict):
        project = (data.get("projects") or {}).get(str(repo_root)) if isinstance(data.get("projects"), dict) else None
        for scope in (project, data):
            servers = scope.get("mcpServers") if isinstance(scope, dict) else None
            if isinstance(servers, dict) and isinstance(servers.get(MCP_SERVER_NAME), dict):
                server = servers[MCP_SERVER_NAME]
                break
    if server is None:
        raise CredentialUnavailableError("No athletedata MCP registration found for this repository")

    url = server.get("url")
    if server.get("type", "http") not in ("http", "streamable-http") or not isinstance(url, str) \
            or not url.startswith("https://"):
        raise CredentialUnavailableError("athletedata MCP registration is not an https HTTP server")

    headers = server.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in headers.items()):
        raise CredentialUnavailableError("athletedata MCP registration has malformed headers")

    return McpEndpoint(url=url, headers=tuple(sorted(headers.items())))


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _default_http_post(url: str, body: bytes, headers: dict, timeout: float) -> "tuple[int, dict, str]":
    """Returns (status, lower-cased headers, body text). Error bodies are
    discarded; exceptions are re-raised with fixed messages only (urllib's
    own messages can include the host)."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status, {k.lower(): v for k, v in response.headers.items()},
                    response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        exc.close()
        return exc.code, response_headers, ""
    except (TimeoutError, socket.timeout):
        raise TransportError("AthleteData MCP request timed out", "timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TransportError("AthleteData MCP request timed out", "timeout") from None
        raise TransportError("AthleteData MCP network error") from None
    except OSError:
        raise TransportError("AthleteData MCP network error") from None


@dataclass(frozen=True)
class ToolResult:
    kind: str  # "json", "text" (prose, discarded), or "empty"
    payload: Any = None
    text_length: int = 0


class AthleteDataClient:
    def __init__(
        self,
        endpoint: McpEndpoint,
        *,
        http_post: Optional[Callable] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self._endpoint = endpoint
        self._http_post = http_post or _default_http_post
        self._sleep = sleep_fn or time.sleep
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._protocol_version = PROTOCOL_VERSION
        self._session_id: Optional[str] = None
        self._initialized = False
        self._verified_tools: set = set()
        self._next_id = 1
        self.request_count = 0
        self.retry_count = 0

    def __repr__(self) -> str:
        return "AthleteDataClient(<endpoint redacted>)"

    # -- protocol ---------------------------------------------------------

    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        version = result.get("protocolVersion")
        if isinstance(version, str) and version:
            self._protocol_version = version
        if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
            raise ProtocolError("AthleteData MCP server did not advertise the tools capability")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        self._initialized = True

    def verify_tools(self, names) -> dict:
        """Returns {name: None if usable, else the AthleteDataError}. Callers
        isolate failures per source, so one bad tool never blocks the rest."""
        self.initialize()
        listed: dict = {}
        cursor = None
        for _ in range(MAX_TOOLS_LIST_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    listed[tool["name"]] = tool
            cursor = result.get("nextCursor")
            if not cursor:
                break

        outcome: dict = {}
        for name in names:
            annotations = (listed.get(name) or {}).get("annotations") or {}
            if name not in READ_ONLY_TOOL_ALLOWLIST:
                outcome[name] = ToolPolicyError("tool is not on the read-only allowlist")
            elif name not in listed:
                outcome[name] = ToolUnavailableError("tool not offered by the AthleteData MCP server")
            elif annotations.get("readOnlyHint") is not True or annotations.get("destructiveHint") is True:
                outcome[name] = ToolPolicyError("tool is not annotated read-only by the server")
            else:
                outcome[name] = None
                self._verified_tools.add(name)
        return outcome

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if name not in READ_ONLY_TOOL_ALLOWLIST:
            raise ToolPolicyError("tool is not on the read-only allowlist")
        if name not in self._verified_tools:
            raise ToolPolicyError("tool has not been verified read-only via tools/list")

        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") is True:
            raise ToolReportedError("AthleteData tool reported an error")

        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return ToolResult("json", structured)

        text = "".join(
            block.get("text", "") for block in (result.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        if not text.strip():
            return ToolResult("empty")
        try:
            return ToolResult("json", json.loads(text))
        except json.JSONDecodeError:
            return ToolResult("text", None, text_length=len(text))

    # -- transport --------------------------------------------------------

    def _request(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                             expect_response=True)
        if "error" in message:
            code = message["error"].get("code") if isinstance(message["error"], dict) else None
            raise ProtocolError(f"JSON-RPC error {code if isinstance(code, int) else 'unknown'} for {method}",
                                "rpc_error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"JSON-RPC response for {method} has no result object")
        return result

    def _headers(self, method: str) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update(dict(self._endpoint.headers))
        if method != "initialize":
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _backoff(self, attempt: int, retry_after: Optional[str]) -> float:
        delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        return min(delay, MAX_RETRY_AFTER_SECONDS)

    def _post(self, message: dict, *, expect_response: bool) -> Optional[dict]:
        body = json.dumps(message).encode("utf-8")
        method = message.get("method", "")
        for attempt in range(1, self._max_attempts + 1):
            final = attempt == self._max_attempts
            try:
                status, headers, text = self._http_post(self._endpoint.url, body, self._headers(method),
                                                        self._timeout)
            except TransportError:
                if final:
                    raise
                self.retry_count += 1
                self._sleep(self._backoff(attempt, None))
                continue
            finally:
                self.request_count += 1

            if status in RETRYABLE_STATUSES:
                if final:
                    raise HTTPStatusError(status)
                self.retry_count += 1
                self._sleep(self._backoff(attempt, headers.get("retry-after")))
                continue
            if status >= 400:
                raise HTTPStatusError(status)

            session_id = headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id
            if not expect_response:
                return None
            return self._parse_message(headers.get("content-type", ""), text, message["id"])
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse_message(content_type: str, text: str, request_id: int) -> dict:
        if not text or not text.strip():
            raise ProtocolError("AthleteData MCP returned an empty response body")

        candidates: list = []
        if "text/event-stream" in content_type.lower():
            data_lines: list = []
            for line in text.splitlines() + [""]:
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    candidates.append("\n".join(data_lines))
                    data_lines = []
        else:
            candidates.append(text)

        for raw in candidates:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for item in parsed if isinstance(parsed, list) else [parsed]:
                if isinstance(item, dict) and item.get("id") == request_id:
                    return item
        raise ProtocolError("AthleteData MCP response did not contain a valid JSON-RPC reply")
