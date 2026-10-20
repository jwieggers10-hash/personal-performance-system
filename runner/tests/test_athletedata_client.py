"""Unit tests for runner/athletedata_client.py. All credentials are synthetic;
no test performs a real network request."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import athletedata_client as adc  # noqa: E402
from fake_athletedata import (  # noqa: E402
    SYNTHETIC_KEY, SYNTHETIC_URL, FakeAthleteData, prose_result, text_result, tool,
)

REPO = "/srv/synthetic-repo"


def write_config(directory: Path, data: dict, mode: int = 0o600) -> Path:
    path = directory / ".claude.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)
    return path


def project_config(url=SYNTHETIC_URL, **server_extra) -> dict:
    return {"projects": {REPO: {"mcpServers": {"athletedata": {"type": "http", "url": url, **server_extra}}}}}


def make_client(fake, **kwargs) -> adc.AthleteDataClient:
    sleeps = kwargs.pop("sleeps", [])
    return adc.AthleteDataClient(adc.McpEndpoint(SYNTHETIC_URL), http_post=fake, sleep_fn=sleeps.append, **kwargs)


class LoadEndpointTests(unittest.TestCase):
    def test_reads_project_scoped_registration(self):
        with tempfile.TemporaryDirectory() as td:
            endpoint = adc.load_mcp_endpoint(write_config(Path(td), project_config()), Path(REPO))
            self.assertEqual(endpoint.url, SYNTHETIC_URL)

    def test_falls_back_to_user_scoped_registration(self):
        with tempfile.TemporaryDirectory() as td:
            config = {"mcpServers": {"athletedata": {"type": "http", "url": SYNTHETIC_URL}}}
            endpoint = adc.load_mcp_endpoint(write_config(Path(td), config), Path(REPO))
            self.assertEqual(endpoint.url, SYNTHETIC_URL)

    def test_registered_headers_are_carried_but_never_shown(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_config(Path(td), project_config(headers={"Authorization": "Bearer synthetic.header.tok"}))
            endpoint = adc.load_mcp_endpoint(path, Path(REPO))
            self.assertEqual(dict(endpoint.headers), {"Authorization": "Bearer synthetic.header.tok"})
            self.assertNotIn("synthetic.header.tok", repr(endpoint))
            self.assertNotIn(SYNTHETIC_KEY, repr(endpoint))

    def test_rejects_missing_invalid_open_or_non_https_config(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            cases = {
                "missing": directory / "absent.json",
                "invalid_json": None,
                "group_readable": write_config(directory, project_config(), mode=0o640),
                "no_registration": None,
                "plain_http": None,
            }
            bad = directory / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            os.chmod(bad, 0o600)
            cases["invalid_json"] = bad
            sub = directory / "sub"
            sub.mkdir()
            cases["no_registration"] = write_config(sub, {"projects": {}})
            sub2 = directory / "sub2"
            sub2.mkdir()
            cases["plain_http"] = write_config(sub2, project_config(url="http://mcp.example.test/mcp?apiKey=x"))
            for name, path in cases.items():
                with self.subTest(name), self.assertRaises(adc.CredentialUnavailableError) as ctx:
                    adc.load_mcp_endpoint(path, Path(REPO))
                self.assertEqual(ctx.exception.category, "credential_unavailable")
                self.assertNotIn(SYNTHETIC_KEY, str(ctx.exception))
                self.assertNotIn("mcp.example.test", str(ctx.exception))


class ProtocolTests(unittest.TestCase):
    def test_initialize_verify_and_call_over_sse(self):
        fake = FakeAthleteData(sse=True)
        fake.garmin = []
        client = make_client(fake)
        self.assertEqual(client.verify_tools(["garmin_get_activities"]), {"garmin_get_activities": None})
        result = client.call_tool("garmin_get_activities", {"start_date": "2026-10-18", "end_date": "2026-10-20"})
        self.assertEqual(result.kind, "json")
        self.assertEqual(result.payload["activities"], [])
        methods = [m for m, _ in fake.requests]
        self.assertEqual(methods[:3], ["initialize", "notifications/initialized", "tools/list"])
        # Protocol version header only after initialize.
        self.assertNotIn("MCP-Protocol-Version", fake.requests[0][1])
        self.assertEqual(fake.requests[1][1]["MCP-Protocol-Version"], "2025-06-18")

    def test_plain_json_response(self):
        fake = FakeAthleteData(sse=False)
        client = make_client(fake)
        client.verify_tools(["hevy_get_workout_events"])
        result = client.call_tool("hevy_get_workout_events", {"since": "2026-10-18T00:00:00Z"})
        self.assertEqual(result.kind, "json")

    def test_sse_reply_after_unrelated_notification(self):
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}}
        body = ("event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n"
                f"event: message\ndata: {json.dumps(reply)}\n\n")
        parsed = adc.AthleteDataClient._parse_message("text/event-stream", body, 1)
        self.assertEqual(parsed["id"], 1)

    def test_empty_and_malformed_bodies_are_protocol_errors(self):
        for content_type, body in (("application/json", ""), ("application/json", "{oops"),
                                   ("text/event-stream", "data: not json\n\n"),
                                   ("application/json", json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}}))):
            with self.subTest(body=body), self.assertRaises(adc.ProtocolError):
                adc.AthleteDataClient._parse_message(content_type, body, 1)

    def test_json_rpc_error_is_reported_with_code_only(self):
        fake = FakeAthleteData()
        client = make_client(fake)
        client.verify_tools(["garmin_get_activities"])
        original = fake.__call__

        def erroring(url, body, headers, timeout):
            message = json.loads(body)
            if message.get("method") == "tools/call":
                return 200, {"content-type": "application/json"}, json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32602, "message": f"bad params for {SYNTHETIC_URL}"}})
            return original(url, body, headers, timeout)

        client._http_post = erroring
        with self.assertRaises(adc.ProtocolError) as ctx:
            client.call_tool("garmin_get_activities", {})
        self.assertEqual(ctx.exception.category, "rpc_error")
        self.assertIn("-32602", str(ctx.exception))
        self.assertNotIn(SYNTHETIC_KEY, str(ctx.exception))

    def test_session_id_is_echoed_when_server_assigns_one(self):
        fake = FakeAthleteData()
        original = fake.__call__

        def with_session(url, body, headers, timeout):
            status, response_headers, text = original(url, body, headers, timeout)
            return status, {**response_headers, "mcp-session-id": "synthetic-session"}, text

        client = make_client(with_session)
        client.verify_tools(["garmin_get_activities"])
        self.assertEqual(fake.requests[-1][1].get("Mcp-Session-Id"), "synthetic-session")

    def test_registered_headers_are_sent(self):
        fake = FakeAthleteData()
        client = adc.AthleteDataClient(adc.McpEndpoint(SYNTHETIC_URL, (("X-Synthetic", "1"),)), http_post=fake)
        client.initialize()
        self.assertEqual(fake.requests[0][1]["X-Synthetic"], "1")


class RetryTests(unittest.TestCase):
    def _ready_client(self, fake, sleeps):
        client = make_client(fake, sleeps=sleeps)
        client.verify_tools(["garmin_get_activities"])
        return client

    def test_transient_5xx_is_retried_then_succeeds(self):
        fake, sleeps = FakeAthleteData(), []
        fake.failures["garmin_get_activities"] = [503, 502]
        client = self._ready_client(fake, sleeps)
        self.assertEqual(client.call_tool("garmin_get_activities", {"start_date": "2026-10-20",
                                                                    "end_date": "2026-10-20"}).kind, "json")
        self.assertEqual(sleeps, [2.0, 5.0])
        self.assertEqual(client.retry_count, 2)

    def test_rate_limit_honours_retry_after_but_caps_it(self):
        fake, sleeps = FakeAthleteData(), []
        client = self._ready_client(fake, sleeps)
        original = fake.__call__
        calls = {"n": 0}

        def limited(url, body, headers, timeout):
            if json.loads(body).get("method") == "tools/call" and calls["n"] == 0:
                calls["n"] += 1
                return 429, {"retry-after": "120"}, ""
            return original(url, body, headers, timeout)

        client._http_post = limited
        client.call_tool("garmin_get_activities", {"start_date": "2026-10-20", "end_date": "2026-10-20"})
        self.assertEqual(sleeps, [adc.MAX_RETRY_AFTER_SECONDS])

    def test_rate_limit_exhaustion_raises_rate_limited(self):
        fake, sleeps = FakeAthleteData(), []
        fake.failures["garmin_get_activities"] = [429, 429, 429]
        client = self._ready_client(fake, sleeps)
        with self.assertRaises(adc.HTTPStatusError) as ctx:
            client.call_tool("garmin_get_activities", {})
        self.assertEqual(ctx.exception.category, "rate_limited")
        self.assertEqual(len(sleeps), adc.MAX_ATTEMPTS - 1)

    def test_auth_failure_is_not_retried(self):
        fake, sleeps = FakeAthleteData(), []
        fake.failures["garmin_get_activities"] = [401]
        client = self._ready_client(fake, sleeps)
        with self.assertRaises(adc.HTTPStatusError) as ctx:
            client.call_tool("garmin_get_activities", {})
        self.assertEqual(ctx.exception.category, "auth_failed")
        self.assertEqual(sleeps, [])

    def test_timeouts_are_retried_then_reported(self):
        sleeps = []

        def always_timeout(url, body, headers, timeout):
            raise adc.TransportError("AthleteData MCP request timed out", "timeout")

        client = make_client(always_timeout, sleeps=sleeps)
        with self.assertRaises(adc.TransportError) as ctx:
            client.initialize()
        self.assertEqual(ctx.exception.category, "timeout")
        self.assertEqual(client.request_count, adc.MAX_ATTEMPTS)


class ToolPolicyTests(unittest.TestCase):
    def test_write_tool_is_refused_without_any_request(self):
        fake = FakeAthleteData()
        client = make_client(fake)
        with self.assertRaises(adc.ToolPolicyError):
            client.call_tool("hevy_create_workout", {})
        self.assertEqual(fake.requests, [])

    def test_write_tool_is_refused_even_if_listed(self):
        fake = FakeAthleteData()
        client = make_client(fake)
        outcome = client.verify_tools(["hevy_create_workout"])
        self.assertIsInstance(outcome["hevy_create_workout"], adc.ToolPolicyError)

    def test_unverified_tool_call_is_refused(self):
        client = make_client(FakeAthleteData())
        with self.assertRaises(adc.ToolPolicyError):
            client.call_tool("garmin_get_activities", {})

    def test_tool_without_read_only_annotation_is_refused(self):
        fake = FakeAthleteData()
        fake.tools = [tool("garmin_get_activities", read_only=False)]
        client = make_client(fake)
        self.assertIsInstance(client.verify_tools(["garmin_get_activities"])["garmin_get_activities"],
                              adc.ToolPolicyError)
        with self.assertRaises(adc.ToolPolicyError):
            client.call_tool("garmin_get_activities", {})

    def test_missing_tool_is_unavailable(self):
        fake = FakeAthleteData()
        fake.tools = [tool("garmin_get_activities")]
        outcome = make_client(fake).verify_tools(["hevy_get_workout_events"])
        self.assertIsInstance(outcome["hevy_get_workout_events"], adc.ToolUnavailableError)


class ToolResultTests(unittest.TestCase):
    def _client(self, result):
        fake = FakeAthleteData()
        fake.raw_tool_results["apple_health_get_body_composition"] = result
        client = make_client(fake)
        client.verify_tools(["apple_health_get_body_composition"])
        return client

    def test_prose_is_discarded_keeping_only_its_length(self):
        client = self._client(prose_result("No data. Synthetic weight 81.4 kg mention."))
        result = client.call_tool("apple_health_get_body_composition", {})
        self.assertEqual(result.kind, "text")
        self.assertIsNone(result.payload)
        self.assertGreater(result.text_length, 0)
        self.assertNotIn("81.4", repr(result))

    def test_empty_content_is_empty(self):
        self.assertEqual(self._client({"content": []}).call_tool("apple_health_get_body_composition", {}).kind,
                         "empty")

    def test_structured_content_is_preferred(self):
        result = self._client({"structuredContent": {"weight": []}, **text_result({"ignored": True})}).call_tool(
            "apple_health_get_body_composition", {})
        self.assertEqual(result.payload, {"weight": []})

    def test_tool_error_is_raised_without_its_text(self):
        client = self._client({"isError": True, "content": [{"type": "text", "text": "secret detail 81.4"}]})
        with self.assertRaises(adc.ToolReportedError) as ctx:
            client.call_tool("apple_health_get_body_composition", {})
        self.assertNotIn("81.4", str(ctx.exception))

    def test_client_repr_hides_endpoint(self):
        self.assertNotIn(SYNTHETIC_KEY, repr(make_client(FakeAthleteData())))


if __name__ == "__main__":
    unittest.main()
