"""Tests for runner/workflow_runner.py.

Standard library only (unittest + unittest.mock). No real `claude` process is
ever invoked -- invoke_claude() is patched in every test that exercises
run_workflow(), consistent with runner/workflow-runner.md's separation of the
runner (orchestration, tested here) from coaching logic (owned by Claude and
the workflow definition, not exercised here).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workflow_runner as wr  # noqa: E402


SAMPLE_WORKFLOW_MD = """# Daily Review Workflow

## Purpose

Sample purpose text.

---

## Governing Documents

Before analysis, read:

- `athlete-identity.md`
- `goals.md`
- `data-source-policy.md`

Use these as governing context.

---

## Other Section

Irrelevant content that must not be picked up as a governing document.
"""


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "workflows").mkdir(parents=True)
    (tmp_path / "workflows" / "daily-review.md").write_text(SAMPLE_WORKFLOW_MD, encoding="utf-8")
    return tmp_path


def make_config(tmp_path: Path, **overrides) -> wr.RunnerConfig:
    defaults = dict(
        repo_root=tmp_path,
        athlete_timezone="Europe/Amsterdam",
        lock_stale_seconds=3600,
    )
    defaults.update(overrides)
    return wr.RunnerConfig(**defaults)


def valid_structured_result(workflow="daily-review", mode="automated", local_date="2026-07-20") -> dict:
    return {
        "workflow": workflow,
        "status": "completed",
        "execution_mode": mode,
        "local_date": local_date,
        "confidence": "moderate",
        "notification_level": "routine",
        "notification_required": False,
        "limitations": [],
        "missing_data": [],
        "integration_failures": [],
        "recommendation_category": "proceed_as_planned",
        "recommendation_summary": "Proceed as planned.",
        "human_readable_review": f"# Daily Review — {local_date}\n\n## Today's Recommendation\n\nProceed.\n",
    }


class LoadEnvFileTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(wr.load_env_file(Path("/does/not/exist.env")), {})

    def test_parses_key_value_pairs_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / "config.env"
            env_path.write_text("# comment\n\nATHLETE_TIMEZONE=Europe/Amsterdam\nEMPTY=\n", encoding="utf-8")
            values = wr.load_env_file(env_path)
            self.assertEqual(values["ATHLETE_TIMEZONE"], "Europe/Amsterdam")
            self.assertEqual(values["EMPTY"], "")
            self.assertNotIn("# comment", values)


class RunnerConfigTests(unittest.TestCase):
    def test_requires_athlete_timezone(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            with self.assertRaises(wr.ConfigError):
                wr.RunnerConfig.from_env({"REPO_ROOT": str(repo)})

    def test_rejects_invalid_timezone(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            with self.assertRaises(wr.ConfigError):
                wr.RunnerConfig.from_env({"REPO_ROOT": str(repo), "ATHLETE_TIMEZONE": "Not/AZone"})

    def test_rejects_repo_root_without_workflows_dir(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(wr.ConfigError):
                wr.RunnerConfig.from_env({"REPO_ROOT": td, "ATHLETE_TIMEZONE": "Europe/Amsterdam"})

    def test_loads_valid_config_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = wr.RunnerConfig.from_env({"REPO_ROOT": str(repo), "ATHLETE_TIMEZONE": "Europe/Amsterdam"})
            self.assertEqual(config.repo_root, repo)
            self.assertEqual(config.claude_binary, wr.DEFAULT_CLAUDE_BINARY)
            self.assertEqual(config.lock_stale_seconds, wr.DEFAULT_LOCK_STALE_SECONDS)


class AthleteLocalNowTests(unittest.TestCase):
    def test_converts_utc_to_local_timezone(self):
        # 2026-07-19 23:30 UTC is 2026-07-20 01:30 in Europe/Amsterdam (CEST, UTC+2)
        fixed_utc = datetime(2026, 7, 19, 23, 30, tzinfo=timezone.utc)
        local = wr.athlete_local_now("Europe/Amsterdam", now=fixed_utc)
        self.assertEqual(local.date(), date(2026, 7, 20))


class GoverningDocumentsTests(unittest.TestCase):
    def test_extracts_backtick_md_files_under_heading_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            docs = wr.parse_governing_documents(repo / "workflows" / "daily-review.md")
            self.assertEqual(docs, ["athlete-identity.md", "goals.md", "data-source-policy.md"])


class BuildPromptTests(unittest.TestCase):
    def test_prompt_includes_workflow_name_and_local_date(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            prompt = wr.build_prompt(
                workflow_name="daily-review",
                workflow_file=repo / "workflows" / "daily-review.md",
                governing_documents=["goals.md"],
                execution_mode="automated",
                local_date=date(2026, 7, 20),
                athlete_timezone="Europe/Amsterdam",
                athlete_context=None,
            )
            self.assertIn("daily-review", prompt)
            self.assertIn("2026-07-20", prompt)
            self.assertIn("goals.md", prompt)

    def test_prompt_includes_athlete_context_when_provided(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            prompt = wr.build_prompt(
                workflow_name="daily-review",
                workflow_file=repo / "workflows" / "daily-review.md",
                governing_documents=[],
                execution_mode="manual",
                local_date=date(2026, 7, 20),
                athlete_timezone="Europe/Amsterdam",
                athlete_context="sore calves",
            )
            self.assertIn("sore calves", prompt)

    def test_prompt_never_instructs_literal_backslash_n_for_line_breaks(self):
        # Regression guard: build_prompt() previously told Claude to "use \n
        # for line breaks" in human_readable_review, which -- under the
        # --json-schema/tool-call structured-output path -- was observed in
        # production to make Claude write the literal two-character sequence
        # backslash-n into the field instead of an actual line break. The
        # instruction must require real newlines and must not tell Claude to
        # type an escape sequence.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            prompt = wr.build_prompt(
                workflow_name="daily-review",
                workflow_file=repo / "workflows" / "daily-review.md",
                governing_documents=[],
                execution_mode="automated",
                local_date=date(2026, 7, 20),
                athlete_timezone="Europe/Amsterdam",
                athlete_context=None,
            )
            self.assertNotIn("use \\n for line breaks", prompt)
            self.assertIn("real, actual line breaks", prompt)
            self.assertIn("never the literal two-character sequence", prompt)


class InvokeClaudeArgvTests(unittest.TestCase):
    """Regression coverage for the --allowedTools/prompt argv-swallowing bug:
    --allowedTools is variadic in the Claude Code CLI (<tools...>), so passing
    it and its value as two separate argv elements lets it consume whatever
    argv element follows -- including the prompt itself, if the prompt were
    also passed as a bare trailing element. Unlike other invoke_claude()
    coverage, this test mocks subprocess.run directly (not invoke_claude) so
    it actually exercises the argv list being built."""

    def _fake_completed_process(self):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

    def test_allowed_tools_passed_as_single_argv_element_with_equals(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo, allowed_tools="Read,mcp__athletedata__*")
            with mock.patch.object(wr.subprocess, "run", return_value=self._fake_completed_process()) as mocked_run:
                wr.invoke_claude("the prompt text", config)

            argv = mocked_run.call_args.args[0]

            # The flag and its value must be one argv element, not two --
            # otherwise the variadic parser would absorb the next element.
            self.assertIn("--allowedTools=Read,mcp__athletedata__*", argv)
            self.assertNotIn("--allowedTools", argv)

            # The prompt must survive as its own final argv element, distinct
            # from the allowedTools value.
            self.assertEqual(argv[-1], "the prompt text")

    def test_json_schema_flag_passed_and_matches_schema_builder(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            with mock.patch.object(wr.subprocess, "run", return_value=self._fake_completed_process()) as mocked_run:
                wr.invoke_claude("the prompt text", config)

            argv = mocked_run.call_args.args[0]

            self.assertIn("--json-schema", argv)
            schema_value = argv[argv.index("--json-schema") + 1]
            self.assertEqual(json.loads(schema_value), wr.build_structured_output_json_schema())

            # The prompt must still survive as the final argv element.
            self.assertEqual(argv[-1], "the prompt text")


class BuildStructuredOutputJsonSchemaTests(unittest.TestCase):
    """These tests exist specifically to catch drift between the CLI-enforced
    schema and validate_structured_result()'s own semantic checks -- both
    must stay derived from the same RUNNER_ENVELOPE_FIELDS / WORKFLOW_RESULT_
    FIELDS / VALID_* constants."""

    def test_required_fields_match_non_runner_supplied_envelope_fields(self):
        schema = wr.build_structured_output_json_schema()
        expected_required = (
            set(wr.RUNNER_ENVELOPE_FIELDS) | set(wr.WORKFLOW_RESULT_FIELDS) | {"human_readable_review"}
        ) - {"execution_id", "started_at", "finished_at", "review_file", "error"}
        self.assertEqual(set(schema["required"]), expected_required)

    def test_enum_properties_match_valid_value_constants(self):
        schema = wr.build_structured_output_json_schema()
        self.assertEqual(set(schema["properties"]["status"]["enum"]), wr.VALID_STATUSES)
        self.assertEqual(set(schema["properties"]["execution_mode"]["enum"]), wr.VALID_EXECUTION_MODES)
        self.assertEqual(set(schema["properties"]["confidence"]["enum"]), wr.VALID_CONFIDENCE)
        self.assertEqual(set(schema["properties"]["notification_level"]["enum"]), wr.VALID_NOTIFICATION_LEVELS)


class BuildRetryPromptTests(unittest.TestCase):
    def test_includes_original_prompt_and_specific_issue(self):
        retry_prompt = wr.build_retry_prompt("ORIGINAL PROMPT TEXT", "some specific validation issue")
        self.assertIn("ORIGINAL PROMPT TEXT", retry_prompt)
        self.assertIn("some specific validation issue", retry_prompt)
        self.assertIn("violated the required structured-output contract", retry_prompt)

    def test_redacts_secrets_in_the_issue_text(self):
        retry_prompt = wr.build_retry_prompt("ORIGINAL", "leaked apiKey=sk_soma_shouldnotleak in error")
        self.assertNotIn("sk_soma_shouldnotleak", retry_prompt)


class WorkflowLockTests(unittest.TestCase):
    def test_acquire_and_release_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            lock = wr.WorkflowLock(lock_dir, "daily-review-2026-07-20", stale_seconds=3600)
            lock.acquire()
            self.assertTrue(lock.path.exists())
            lock.release()
            self.assertFalse(lock.path.exists())

    def test_second_acquire_raises_when_lock_is_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            first = wr.WorkflowLock(lock_dir, "daily-review-2026-07-20", stale_seconds=3600)
            first.acquire()
            second = wr.WorkflowLock(lock_dir, "daily-review-2026-07-20", stale_seconds=3600)
            with self.assertRaises(wr.DuplicateExecutionError):
                second.acquire()
            first.release()

    def test_stale_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            lock_dir.mkdir(parents=True)
            stale_path = lock_dir / "daily-review-2026-07-20.lock"
            old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            stale_path.write_text(json.dumps({"pid": 999999999, "acquired_at": old_timestamp}), encoding="utf-8")

            lock = wr.WorkflowLock(lock_dir, "daily-review-2026-07-20", stale_seconds=3600)
            lock.acquire()  # must not raise
            self.assertTrue(lock.path.exists())
            lock.release()

    def test_corrupt_lock_file_is_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            lock_dir.mkdir(parents=True)
            (lock_dir / "daily-review-2026-07-20.lock").write_text("not json", encoding="utf-8")

            lock = wr.WorkflowLock(lock_dir, "daily-review-2026-07-20", stale_seconds=3600)
            lock.acquire()
            lock.release()


class ParseClaudeOutputTests(unittest.TestCase):
    def test_parses_result_wrapped_json(self):
        inner = valid_structured_result()
        outer = json.dumps({"result": json.dumps(inner)})
        parsed = wr.parse_claude_output(outer)
        self.assertEqual(parsed["workflow"], "daily-review")

    def test_parses_direct_json_without_wrapper(self):
        inner = valid_structured_result()
        parsed = wr.parse_claude_output(json.dumps(inner))
        self.assertEqual(parsed["workflow"], "daily-review")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(wr.MalformedOutputError):
            wr.parse_claude_output("not json at all")

    def test_raises_when_result_text_is_not_json(self):
        outer = json.dumps({"result": "not json either"})
        with self.assertRaises(wr.MalformedOutputError):
            wr.parse_claude_output(outer)

    def test_prefers_structured_output_over_result_string(self):
        inner = valid_structured_result()
        # A deliberately-broken "result" string proves structured_output wins
        # rather than being a fallback that's merely tried first.
        outer = json.dumps({"result": "not json at all", "structured_output": inner})
        parsed = wr.parse_claude_output(outer)
        self.assertEqual(parsed, inner)

    def test_falls_back_to_result_string_when_structured_output_absent(self):
        inner = valid_structured_result()
        outer = json.dumps({"result": json.dumps(inner)})
        parsed = wr.parse_claude_output(outer)
        self.assertEqual(parsed, inner)


class ValidateStructuredResultTests(unittest.TestCase):
    def test_valid_result_has_no_problems(self):
        result = valid_structured_result()
        problems = wr.validate_structured_result(result, "daily-review", "automated", date(2026, 7, 20))
        self.assertEqual(problems, [])

    def test_flags_missing_field(self):
        result = valid_structured_result()
        del result["confidence"]
        problems = wr.validate_structured_result(result, "daily-review", "automated", date(2026, 7, 20))
        self.assertTrue(any("confidence" in p for p in problems))

    def test_flags_workflow_mismatch(self):
        result = valid_structured_result(workflow="weekly-review")
        problems = wr.validate_structured_result(result, "daily-review", "automated", date(2026, 7, 20))
        self.assertTrue(any("workflow mismatch" in p for p in problems))

    def test_flags_local_date_mismatch(self):
        result = valid_structured_result(local_date="2026-07-19")
        problems = wr.validate_structured_result(result, "daily-review", "automated", date(2026, 7, 20))
        self.assertTrue(any("local_date mismatch" in p for p in problems))

    def test_flags_invalid_confidence(self):
        result = valid_structured_result()
        result["confidence"] = "extremely high"
        problems = wr.validate_structured_result(result, "daily-review", "automated", date(2026, 7, 20))
        self.assertTrue(any("invalid confidence" in p for p in problems))


class WriteReviewFileTests(unittest.TestCase):
    def test_first_write_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviews" / "daily" / "2026-07-20.md"
            written = wr.write_review_file(path, "content", "automated")
            self.assertEqual(written, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "content")

    def test_automated_mode_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviews" / "daily" / "2026-07-20.md"
            wr.write_review_file(path, "first", "automated")
            with self.assertRaises(wr.DuplicateReviewError):
                wr.write_review_file(path, "second", "automated")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_manual_mode_writes_alternate_file_when_canonical_exists(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviews" / "daily" / "2026-07-20.md"
            wr.write_review_file(path, "first", "automated")
            written = wr.write_review_file(path, "second", "manual")
            self.assertNotEqual(written, path)
            self.assertIn("manual-", written.name)
            self.assertEqual(path.read_text(encoding="utf-8"), "first")
            self.assertEqual(written.read_text(encoding="utf-8"), "second")


class RedactSecretsTests(unittest.TestCase):
    def test_redacts_api_key_query_param(self):
        text = "https://mcp.athletedata.health/mcp?apiKey=sk_soma_abc123"
        self.assertNotIn("sk_soma_abc123", wr.redact_secrets(text))

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer abc.def.ghi"
        self.assertNotIn("abc.def.ghi", wr.redact_secrets(text))

    def test_leaves_ordinary_text_untouched(self):
        text = "Garmin resting heart rate is 44 bpm"
        self.assertEqual(wr.redact_secrets(text), text)

    # All credentials below are synthetic test values.

    def test_redacts_anthropic_api_key(self):
        text = "ANTHROPIC key sk-ant-api03-SYNTHETICtestKEY_0123456789-abcdef in use"
        redacted = wr.redact_secrets(text)
        self.assertNotIn("SYNTHETICtestKEY_0123456789", redacted)
        self.assertIn("in use", redacted)

    def test_redacts_authorization_header_with_and_without_scheme(self):
        for text in ("Authorization: Bearer synthetic.jwt.value",
                     "authorization=Basic c3ludGhldGljOnVzZXI=",
                     "Authorization: rawSyntheticToken123",
                     '{"Authorization": "Bearer synthetic.jwt.value"}'):
            redacted = wr.redact_secrets(text)
            for secret in ("synthetic.jwt.value", "c3ludGhldGljOnVzZXI=", "rawSyntheticToken123"):
                self.assertNotIn(secret, redacted, text)

    def test_redacts_x_api_key_header(self):
        redacted = wr.redact_secrets("x-api-key: syntheticHeaderKey987")
        self.assertNotIn("syntheticHeaderKey987", redacted)

    def test_redacts_json_style_credential_fields(self):
        text = json.dumps({
            "accessToken": "synthAccess111",
            "refresh_token": "synthRefresh222",
            "api_key": "synthApiKey333",
            "client_secret": "synthClient444",
            "password": "synthPassword555",
            "note": "keep me",
        })
        redacted = wr.redact_secrets(text)
        for secret in ("synthAccess111", "synthRefresh222", "synthApiKey333", "synthClient444", "synthPassword555"):
            self.assertNotIn(secret, redacted)
        self.assertIn("keep me", redacted)

    def test_redacts_entire_url_query_string_for_any_parameter_name(self):
        text = "connect failed: https://mcp.example.test/mcp?session=abc&k=syntheticUnknownParam9 (timeout)"
        redacted = wr.redact_secrets(text)
        self.assertNotIn("syntheticUnknownParam9", redacted)
        self.assertNotIn("session=abc", redacted)
        self.assertIn("https://mcp.example.test/mcp?", redacted)
        self.assertIn("(timeout)", redacted)

    def test_leaves_url_without_query_and_iso_timestamps_untouched(self):
        text = "see https://example.test/docs/page at 2026-10-20T05:00:01+00:00"
        self.assertEqual(wr.redact_secrets(text), text)


# Trimmed from real `claude --print --output-format json` output captured with
# no credentials / a synthetic invalid API key (exit status 1, empty stderr).
CLI_NOT_LOGGED_IN_STDOUT = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
    "api_error_status": None, "num_turns": 1, "result": "Not logged in · Please run /login",
    "session_id": "c56cdef1-6a2a-42fb-95be-5b77cc40a86a",
})
CLI_INVALID_API_KEY_STDOUT = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
    "api_error_status": 401, "api_error_origin": "first_party", "num_turns": 1,
    "result": "Invalid API key · Fix external API key",
    "session_id": "a00c4f0b-308b-4808-9870-73a534b0ea7a",
})


def cli_error_stdout(result_text: str, api_error_status=None, **extra) -> str:
    payload = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
               "api_error_status": api_error_status, "num_turns": 1, "result": result_text}
    payload.update(extra)
    return json.dumps(payload)


class DiagnoseClaudeFailureTests(unittest.TestCase):
    def test_not_logged_in_is_auth_failure(self):
        d = wr.diagnose_claude_failure(CLI_NOT_LOGGED_IN_STDOUT, "")
        self.assertEqual((d.error_category, d.signal, d.stdout_format),
                         ("claude_auth_failure", "not_logged_in", "json"))

    def test_invalid_api_key_is_auth_failure(self):
        d = wr.diagnose_claude_failure(CLI_INVALID_API_KEY_STDOUT, "")
        self.assertEqual((d.error_category, d.signal), ("claude_auth_failure", "invalid_api_key"))
        self.assertEqual(d.cli_fields["api_error_status"], 401)

    def test_expired_oauth_token_is_auth_failure(self):
        d = wr.diagnose_claude_failure(
            cli_error_stdout("OAuth token has expired. Please obtain a new token or refresh your existing token.",
                             401), "")
        self.assertEqual((d.error_category, d.signal), ("claude_auth_failure", "oauth_token_expired"))

    def test_status_401_with_unrecognized_text_is_auth_failure(self):
        d = wr.diagnose_claude_failure(cli_error_stdout("Something unexpected happened", 401), "")
        self.assertEqual((d.error_category, d.signal), ("claude_auth_failure", "http_401"))

    def test_usage_limit_is_quota_failure(self):
        d = wr.diagnose_claude_failure(cli_error_stdout("Claude AI usage limit reached|1760000000"), "")
        self.assertEqual((d.error_category, d.signal), ("claude_quota_failure", "usage_limit_reached"))

    def test_credit_balance_is_quota_failure(self):
        d = wr.diagnose_claude_failure(cli_error_stdout("Credit balance is too low"), "")
        self.assertEqual((d.error_category, d.signal), ("claude_quota_failure", "credit_balance_too_low"))

    def test_status_429_is_quota_failure(self):
        d = wr.diagnose_claude_failure(cli_error_stdout("Request failed", 429), "")
        self.assertEqual((d.error_category, d.signal), ("claude_quota_failure", "http_429"))

    def test_auth_signal_in_stderr_is_recognized(self):
        d = wr.diagnose_claude_failure("", "Error: Failed to authenticate. API Error: 401")
        self.assertEqual(d.error_category, "claude_auth_failure")
        self.assertEqual(d.stdout_format, "empty")

    def test_unrecognized_failure_is_generic_execution_failure(self):
        d = wr.diagnose_claude_failure(cli_error_stdout("Tool execution crashed", 500), "")
        self.assertEqual((d.error_category, d.signal), ("claude_execution_failure", None))

    def test_non_json_stdout_is_classified_without_parsing(self):
        d = wr.diagnose_claude_failure("panic: Not logged in · Please run /login\n", "")
        self.assertEqual((d.error_category, d.stdout_format), ("claude_auth_failure", "non_json"))
        self.assertEqual(d.cli_fields, {})

    def test_cli_fields_are_allowlisted_and_never_include_free_text(self):
        stdout = cli_error_stdout("Coaching text: Sleep 6h, HRV low, skip intervals",
                                  terminal_reason="api error with spaces and Bearer synthetic.tok")
        d = wr.diagnose_claude_failure(stdout, "")
        self.assertNotIn("result", d.cli_fields)
        self.assertNotIn("session_id", d.cli_fields)
        # A non-token string value is dropped rather than logged.
        self.assertNotIn("terminal_reason", d.cli_fields)
        self.assertTrue(set(d.cli_fields) <= set(wr._CLI_DIAGNOSTIC_FIELDS))
        self.assertNotIn("HRV", json.dumps(d.cli_fields))

    def test_describe_uses_fixed_guidance_and_never_includes_stdout(self):
        stdout = cli_error_stdout("Not logged in · Please run /login. Coaching: HRV low today")
        d = wr.diagnose_claude_failure(stdout, "")
        message = wr.describe_claude_failure(1, d, "")
        self.assertIn("claude_auth_failure: not_logged_in", message)
        self.assertIn("re-authenticate", message)
        self.assertNotIn("HRV", message)
        self.assertNotIn("stderr:", message)

    def test_describe_keeps_redacted_stderr_excerpt(self):
        d = wr.diagnose_claude_failure("", "boom apiKey=sk_soma_synthetic123456")
        message = wr.describe_claude_failure(2, d, "boom apiKey=sk_soma_synthetic123456")
        self.assertIn("stderr: boom", message)
        self.assertNotIn("sk_soma_synthetic123456", message)


class RunWorkflowTests(unittest.TestCase):
    def _fake_completed_process(self, structured: dict, returncode: int = 0):
        return mock.Mock(returncode=returncode, stdout=json.dumps({"result": json.dumps(structured)}), stderr="")

    def test_successful_run_writes_review_and_returns_completed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")

            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["error"])
            review_file = repo / result["review_file"]
            self.assertTrue(review_file.exists())
            # lock must be released after a successful run
            self.assertFalse((repo / "runtime" / "locks" / "daily-review-2026-07-20.lock").exists())

    def test_valid_first_attempt_invokes_claude_exactly_once(self):
        # A valid first response must never trigger the retry path.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")

            with mock.patch.object(wr, "invoke_claude",
                                    return_value=self._fake_completed_process(structured)) as mocked:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(mocked.call_count, 1)

    def test_review_file_contains_real_newlines_not_literal_backslash_n(self):
        # Regression test for the production newline defect: a successfully
        # parsed structured result whose human_readable_review contains real
        # newline characters (exactly what json.loads() / --json-schema's
        # structured_output produce for a well-formed response) must reach
        # the review file on disk with those same real newlines -- not the
        # literal two-character sequence backslash-n.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")
            structured["human_readable_review"] = (
                "# Daily Review — 2026-07-20\n\n## Today's Recommendation\n\nProceed as planned.\n"
            )

            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            review_file = repo / result["review_file"]
            content = review_file.read_text(encoding="utf-8")
            self.assertGreater(content.count("\n"), 3)
            self.assertNotIn("\\n", content)

    def test_malformed_first_attempt_retries_and_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")
            bad_process = mock.Mock(returncode=0, stdout="not json", stderr="")
            good_process = self._fake_completed_process(structured)
            fake_delivery = {"notification_delivered": True, "notification_delivery_error": None,
                              "notification_skipped": False, "parts_sent": 1}

            with mock.patch.object(wr, "invoke_claude", side_effect=[bad_process, good_process]) as mocked_invoke, \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", return_value=fake_delivery) as mocked_notify:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(mocked_invoke.call_count, 2)
            review_file = repo / result["review_file"]
            self.assertTrue(review_file.exists())
            # Exactly one review write and one notification, even though two
            # Claude attempts occurred.
            self.assertEqual(mocked_notify.call_count, 1)

    def test_failed_validation_first_attempt_retries_and_succeeds(self):
        # Distinct from malformed JSON: the first attempt is valid JSON that
        # fails validate_structured_result() (missing a required field).
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            invalid_structured = valid_structured_result(local_date="2026-07-20")
            del invalid_structured["confidence"]
            invalid_process = mock.Mock(
                returncode=0, stdout=json.dumps({"result": json.dumps(invalid_structured)}), stderr=""
            )
            good_structured = valid_structured_result(local_date="2026-07-20")
            good_process = self._fake_completed_process(good_structured)
            fake_delivery = {"notification_delivered": True, "notification_delivery_error": None,
                              "notification_skipped": False, "parts_sent": 1}

            with mock.patch.object(wr, "invoke_claude",
                                    side_effect=[invalid_process, good_process]) as mocked_invoke, \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", return_value=fake_delivery) as mocked_notify:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(mocked_invoke.call_count, 2)
            review_file = repo / result["review_file"]
            self.assertTrue(review_file.exists())
            self.assertEqual(mocked_notify.call_count, 1)

    def test_retry_prompt_states_previous_contract_violation(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")
            bad_process = mock.Mock(returncode=0, stdout="not json", stderr="")
            good_process = self._fake_completed_process(structured)

            with mock.patch.object(wr, "invoke_claude", side_effect=[bad_process, good_process]) as mocked_invoke:
                wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(mocked_invoke.call_count, 2)
            second_call_prompt = mocked_invoke.call_args_list[1].args[0]
            self.assertIn("violated the required structured-output contract", second_call_prompt)
            self.assertIn("not valid JSON", second_call_prompt)

    def test_both_attempts_malformed_fails_after_exactly_two_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            bad_process = mock.Mock(returncode=0, stdout="not json", stderr="")
            fake_delivery = {"notification_delivered": True, "notification_delivery_error": None,
                              "notification_skipped": False, "parts_sent": 1}

            with mock.patch.object(wr, "invoke_claude", side_effect=[bad_process, bad_process]) as mocked_invoke, \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", return_value=fake_delivery) as mocked_notify:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertIsNotNone(result["error"])
            self.assertEqual(mocked_invoke.call_count, 2)
            self.assertFalse((repo / "reviews" / "daily" / "2026-07-20.md").exists())
            # Existing failed-workflow + urgent failure-notification behavior
            # is preserved: exactly one notification, at urgent priority.
            self.assertEqual(mocked_notify.call_count, 1)
            self.assertEqual(result["notification_level"], "urgent")
            self.assertTrue(result["notification_required"])

    def test_timeout_does_not_retry(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

            with mock.patch.object(
                wr, "invoke_claude", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=600)
            ) as mocked_invoke:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertIn("timed out", result["error"])
            self.assertEqual(mocked_invoke.call_count, 1)

    def test_duplicate_lock_short_circuits_without_invoking_claude(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

            held_lock = wr.WorkflowLock(repo / "runtime" / "locks", "daily-review-2026-07-20", 3600)
            held_lock.acquire()
            try:
                with mock.patch.object(wr, "invoke_claude") as mocked:
                    result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)
                mocked.assert_not_called()
            finally:
                held_lock.release()

            self.assertEqual(result["status"], "duplicate_skipped")

    def test_duplicate_review_file_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            existing = repo / "reviews" / "daily" / "2026-07-20.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("already here", encoding="utf-8")

            structured = valid_structured_result(local_date="2026-07-20")
            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "duplicate_skipped")
            self.assertEqual(existing.read_text(encoding="utf-8"), "already here")

    def test_malformed_claude_output_reports_failed(self):
        # invoke_claude's mock returns the same malformed output on every
        # call, so this also exercises "both attempts malformed" -- the
        # retry is attempted (call_count == 2) and still ends up failed.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            bad_process = mock.Mock(returncode=0, stdout="not json", stderr="")

            with mock.patch.object(wr, "invoke_claude", return_value=bad_process) as mocked_invoke:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertIsNotNone(result["error"])
            self.assertFalse((repo / "reviews" / "daily" / "2026-07-20.md").exists())
            self.assertEqual(mocked_invoke.call_count, 2)

    def test_claude_nonzero_exit_reports_failed_without_leaking_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            failing = mock.Mock(returncode=1, stdout="", stderr="auth error apiKey=sk_soma_shouldnotleak")

            with mock.patch.object(wr, "invoke_claude", return_value=failing) as mocked_invoke:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertNotIn("sk_soma_shouldnotleak", result["error"])
            # A nonzero CLI exit is a distinct failure mode from malformed
            # output and must never trigger the retry.
            self.assertEqual(mocked_invoke.call_count, 1)

    def _capture_log_events(self):
        events = []
        return events, mock.patch.object(wr, "log_event", side_effect=lambda config, **f: events.append(f))

    def test_claude_auth_failure_is_classified_logged_and_notified_safely(self):
        # Reproduces the Sep 21 - Oct 19 production failure shape: exit 1,
        # empty stderr, diagnosis only in the JSON stdout.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            failing = mock.Mock(returncode=1, stdout=CLI_NOT_LOGGED_IN_STDOUT, stderr="")
            fake_delivery = {"notification_delivered": True, "notification_delivery_error": None,
                             "notification_skipped": False, "parts_sent": 1}
            events, log_patch = self._capture_log_events()

            with mock.patch.object(wr, "invoke_claude", return_value=failing) as mocked_invoke, \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", return_value=fake_delivery) as notify, \
                 log_patch:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(mocked_invoke.call_count, 1)  # nonzero exit is still never retried
            self.assertIn("claude_auth_failure: not_logged_in", result["error"])
            self.assertNotIn("session_id", result["error"])
            # Existing urgent failure notification is preserved.
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(result["notification_level"], "urgent")
            self.assertTrue(result["notification_required"])

            end = [e for e in events if e.get("event") == "workflow_end"][-1]
            self.assertEqual(end["error_category"], "claude_auth_failure")
            self.assertEqual(end["failure_signal"], "not_logged_in")
            self.assertEqual(end["exit_code"], 1)
            self.assertEqual(end["stdout_format"], "json")
            self.assertEqual(end["stderr_bytes"], 0)
            self.assertEqual(end["cli"]["terminal_reason"], "api_error")
            self.assertTrue(end["cli"]["is_error"])
            logged = json.dumps(events)
            self.assertNotIn("Please run /login", logged)
            self.assertNotIn("c56cdef1-6a2a-42fb-95be-5b77cc40a86a", logged)

    def test_claude_failure_log_never_contains_stdout_coaching_content_or_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            stdout = cli_error_stdout(
                "Coaching: RHR 44, weight trend down. Tool error at "
                "https://mcp.example.test/mcp?apiKey=sk_soma_syntheticLEAK1 Bearer synthetic.jwt.LEAK2",
                500,
            )
            failing = mock.Mock(returncode=1, stdout=stdout,
                                stderr="fatal: x-api-key: syntheticLEAK3 sk-ant-api03-syntheticLEAK4xxxxx")
            events, log_patch = self._capture_log_events()

            with mock.patch.object(wr, "invoke_claude", return_value=failing), log_patch:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            surfaced = json.dumps(events) + result["error"]
            for leaked in ("RHR 44", "weight trend", "syntheticLEAK1", "LEAK2", "syntheticLEAK3",
                           "syntheticLEAK4", "mcp.example.test"):
                self.assertNotIn(leaked, surfaced)
            self.assertIn("claude_execution_failure", result["error"])

    def test_automated_run_skips_claude_when_review_already_exists(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo, telegram_bot_token="123:synthetic", telegram_chat_id="1")
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            existing = repo / "reviews" / "daily" / "2026-07-20.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("already here", encoding="utf-8")
            events, log_patch = self._capture_log_events()

            with mock.patch.object(wr, "invoke_claude") as mocked_invoke, \
                 mock.patch.object(wr.telegram_delivery, "_default_http_post") as http_post, \
                 log_patch:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            mocked_invoke.assert_not_called()
            http_post.assert_not_called()  # no Telegram message for a duplicate skip
            self.assertEqual(result["status"], "duplicate_skipped")
            self.assertFalse(result["notification_required"])
            self.assertIn("already exists", result["error"])
            self.assertEqual(existing.read_text(encoding="utf-8"), "already here")
            self.assertFalse((repo / "runtime" / "locks" / "daily-review-2026-07-20.lock").exists())
            end = [e for e in events if e.get("event") == "workflow_end"][-1]
            self.assertEqual(end["error_category"], "review_already_exists")
            self.assertEqual(end["skipped_stage"], "before_claude")
            self.assertFalse(any(e.get("event") == "claude_invoke_start" for e in events))

    def test_manual_run_still_invokes_claude_when_review_already_exists(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            existing = repo / "reviews" / "daily" / "2026-07-20.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("already here", encoding="utf-8")
            structured = valid_structured_result(mode="manual", local_date="2026-07-20")

            with mock.patch.object(wr, "invoke_claude",
                                   return_value=self._fake_completed_process(structured)) as mocked_invoke:
                result = wr.run_workflow("daily-review", "manual", config, now=fixed_now)

            self.assertEqual(mocked_invoke.call_count, 1)
            self.assertEqual(result["status"], "completed")
            self.assertIn(".manual-", result["review_file"])
            self.assertEqual(existing.read_text(encoding="utf-8"), "already here")

    def test_write_time_guard_still_catches_review_created_during_claude_run(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            existing = repo / "reviews" / "daily" / "2026-07-20.md"
            structured = valid_structured_result(local_date="2026-07-20")
            completed = self._fake_completed_process(structured)

            def review_appears_mid_run(*_args, **_kwargs):
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text("written by another process", encoding="utf-8")
                return completed

            events, log_patch = self._capture_log_events()
            with mock.patch.object(wr, "invoke_claude", side_effect=review_appears_mid_run) as mocked_invoke, \
                 log_patch:
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(mocked_invoke.call_count, 1)
            self.assertEqual(result["status"], "duplicate_skipped")
            self.assertEqual(existing.read_text(encoding="utf-8"), "written by another process")
            end = [e for e in events if e.get("event") == "workflow_end"][-1]
            self.assertEqual(end["skipped_stage"], "at_write")

    def test_failure_message_reaches_telegram_formatter_without_stdout_content(self):
        # End-to-end through the real deliver_review/build_message path, with
        # only the HTTP transport stubbed -- confirms Telegram compatibility.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo, telegram_bot_token="123:synthetic", telegram_chat_id="1")
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            failing = mock.Mock(returncode=1, stdout=CLI_INVALID_API_KEY_STDOUT, stderr="")
            sent = []

            def fake_post(url, data, timeout=10.0):
                sent.append(data["text"])
                return 200, json.dumps({"ok": True})

            with mock.patch.object(wr, "invoke_claude", return_value=failing), \
                 mock.patch.object(wr.telegram_delivery, "_default_http_post", side_effect=fake_post):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(len(sent), 1)
            self.assertTrue(result["notification_delivered"])
            self.assertIn("Review could not be completed", sent[0])
            self.assertIn("claude_auth_failure: invalid_api_key", sent[0])
            self.assertNotIn("Fix external API key", sent[0])

    def test_unknown_workflow_reports_failed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            result = wr.run_workflow("weekly-review", "automated", config)
            self.assertEqual(result["status"], "failed")
            self.assertIn("unknown workflow", result["error"])

    def test_automated_mode_rejects_athlete_context(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            result = wr.run_workflow("daily-review", "automated", config, athlete_context="sore calves")
            self.assertEqual(result["status"], "failed")
            self.assertIn("athlete_context", result["error"])

    def test_lock_released_even_when_claude_invocation_raises_unexpectedly(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

            with mock.patch.object(wr, "invoke_claude", side_effect=RuntimeError("boom")):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertIn("unexpected error", result["error"])
            self.assertFalse((repo / "runtime" / "locks" / "daily-review-2026-07-20.lock").exists())

    def test_notify_skips_cleanly_when_telegram_not_configured(self):
        # No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set on config (the default
        # in make_config()) -- the Notify stage must not attempt any HTTP
        # call and must not affect the already-decided workflow status.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")

            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            self.assertFalse(result["notification_delivered"])
            self.assertIsNotNone(result["notification_delivery_error"])

    def test_notify_failure_does_not_change_workflow_status_or_crash(self):
        # A Telegram delivery that raises unexpectedly must never mask or
        # invalidate an already-successful (and already-saved) review --
        # per runner/telegram-integration.md's Delivery Failure Handling.
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo, telegram_bot_token="123:fake", telegram_chat_id="1")
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")

            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)), \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", side_effect=RuntimeError("telegram boom")):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "completed")
            review_file = repo / result["review_file"]
            self.assertTrue(review_file.exists())
            self.assertFalse(result["notification_delivered"])
            self.assertIn("telegram boom", result["notification_delivery_error"])
            self.assertFalse((repo / "runtime" / "locks" / "daily-review-2026-07-20.lock").exists())

    def test_notify_success_populates_delivery_fields(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo, telegram_bot_token="123:fake", telegram_chat_id="1")
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            structured = valid_structured_result(local_date="2026-07-20")
            fake_delivery = {
                "notification_delivered": True,
                "notification_delivery_error": None,
                "notification_skipped": False,
                "parts_sent": 1,
            }

            with mock.patch.object(wr, "invoke_claude", return_value=self._fake_completed_process(structured)), \
                 mock.patch.object(wr.telegram_delivery, "deliver_review", return_value=fake_delivery):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertTrue(result["notification_delivered"])
            self.assertIsNone(result["notification_delivery_error"])


if __name__ == "__main__":
    unittest.main()
