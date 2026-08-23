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
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            bad_process = mock.Mock(returncode=0, stdout="not json", stderr="")

            with mock.patch.object(wr, "invoke_claude", return_value=bad_process):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertIsNotNone(result["error"])
            self.assertFalse((repo / "reviews" / "daily" / "2026-07-20.md").exists())

    def test_claude_nonzero_exit_reports_failed_without_leaking_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            config = make_config(repo)
            fixed_now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
            failing = mock.Mock(returncode=1, stdout="", stderr="auth error apiKey=sk_soma_shouldnotleak")

            with mock.patch.object(wr, "invoke_claude", return_value=failing):
                result = wr.run_workflow("daily-review", "automated", config, now=fixed_now)

            self.assertEqual(result["status"], "failed")
            self.assertNotIn("sk_soma_shouldnotleak", result["error"])

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


if __name__ == "__main__":
    unittest.main()
