"""Tests for runner/evening_nutrition_review.py and its integration with the
workflow runner and the feedback dispatcher: facts in the prompt, duplicate
prevention, missing/malformed Claude output, authentication failures,
Telegram failures, the automated run window across DST, and lock
coordination in both directions. Claude is always mocked; Telegram goes to a
scripted transport; AthleteData is a scripted client. Temporary directories
only."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import athletedata_client as adc  # noqa: E402
import evening_nutrition_review as enr  # noqa: E402
import feedback_dispatcher as fd  # noqa: E402
import telegram_delivery as td  # noqa: E402
import workflow_runner as wr  # noqa: E402
from feedback_fixtures import NOW as DISPATCH_NOW, FakeGateway, FakeTelegram, add_event, make_dispatcher, migrated_store  # noqa: E402
from nutrition_fixtures import REPO_ROOT, TODAY, UTC, FakeClient, local  # noqa: E402
from test_nutrition_data import standard_responses  # noqa: E402

EVENING = local("2026-10-26 21:30")
SUMMARY = ("Day: Strength · Log: complete (4 meals, last sync 19:42)\n"
           "Intake: 2,400 kcal · P 160 g\nTomorrow: add a portion of vegetables at lunch.")


def structured(local_date="2026-10-26", **overrides) -> dict:
    result = {
        "workflow": "evening-nutrition-review", "status": "completed", "execution_mode": "automated",
        "local_date": local_date, "confidence": "moderate", "notification_level": "routine",
        "notification_required": True, "limitations": [], "missing_data": [], "integration_failures": [],
        "recommendation_category": "on_track", "recommendation_summary": SUMMARY,
        "data_retrieved_at": "2026-10-26 21:30 CET",
        "human_readable_review": f"# Evening Nutrition Review — {local_date}\n\n## Objective\n\nSynthetic.\n",
    }
    result.update(overrides)
    return result


def claude_ok(result: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"structured_output": result}), stderr="")


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_lock(lock_dir: Path, key: str, pid: int, age: timedelta = timedelta(0)) -> Path:
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{key}.lock"
    path.write_text(json.dumps({"pid": pid, "acquired_at": (datetime.now(UTC) - age).isoformat()}))
    return path


class EveningTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "workflows").mkdir()
        shutil.copy(REPO_ROOT / "workflows" / "evening-nutrition-review.md", self.root / "workflows")
        shutil.copy(REPO_ROOT / "goals.md", self.root)
        self.env_file = self.root / "env"
        self.env_file.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\nCLAUDE_BINARY=/nonexistent/claude\n"
                                 "TELEGRAM_BOT_TOKEN=123456:SYNTHETIC-token-value\nTELEGRAM_CHAT_ID=999000111\n")
        self.transport = FakeTelegram()
        self.prompts: list = []
        self.client_calls = 0

    def tearDown(self):
        self._tmp.cleanup()

    def client_factory(self, responses=None):
        def factory():
            self.client_calls += 1
            return FakeClient(responses or standard_responses())
        return factory

    def run_main(self, *outputs, now=EVENING, mode="automated", factory=None, transport=None, extra=()):
        outputs = list(outputs) or [claude_ok(structured())]

        def fake_invoke(prompt, config, json_schema=None):
            self.prompts.append(prompt)
            step = outputs.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(wr, "invoke_claude", side_effect=fake_invoke), \
                mock.patch.object(td, "_default_http_post", transport or self.transport), \
                mock.patch.object(td.time, "sleep", lambda s: None):
            code = enr.main(["--mode", mode, "--env-file", str(self.env_file), "--repo-root", str(self.root),
                             *extra], now_fn=lambda: now.astimezone(UTC),
                            client_factory=factory or self.client_factory(), sleep_fn=lambda s: None)
        return code, json.loads(out.getvalue())

    def review_file(self, day="2026-10-26") -> Path:
        return self.root / "reviews" / "nutrition" / f"{day}.md"


class EveningRunTests(EveningTestCase):
    def test_completed_review(self):
        code, result = self.run_main()
        self.assertEqual((code, result["status"]), (0, "completed"))
        self.assertEqual(result["review_file"], "reviews/nutrition/2026-10-26.md")
        self.assertTrue(self.review_file().exists())
        prompt = self.prompts[0]
        self.assertIn('"evening-nutrition-review" coaching workflow', prompt)
        self.assertIn("Runner-computed facts for this review", prompt)
        facts = json.loads(prompt.split("them:\n", 1)[1].split("\n\nRetrieve fresh", 1)[0])
        self.assertEqual(facts["targets"]["kcal"], 2450)
        self.assertEqual(facts["food_today"]["log_status"], "complete")
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn(SUMMARY, self.transport.sent[0])
        self.assertTrue(self.transport.sent[0].startswith("evening-nutrition-review — 2026-10-26"))

    def test_duplicate_run_skips_before_facts_and_claude(self):
        self.run_main()
        calls_after_first = self.client_calls
        code, result = self.run_main(now=local("2026-10-26 21:45"))
        self.assertEqual((code, result["status"]), (0, "duplicate_skipped"))
        self.assertEqual(self.client_calls, calls_after_first)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.transport.sent), 1)

    def test_malformed_output_is_retried_once_then_fails(self):
        bad = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
        wrong = claude_ok(structured(workflow="daily-review"))
        code, result = self.run_main(bad, wrong)
        self.assertEqual((code, result["status"]), (1, "failed"))
        self.assertIn("workflow mismatch", result["error"])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("Runner-computed facts", self.prompts[1])
        self.assertIn("previous attempt", self.prompts[1])
        self.assertFalse(self.review_file().exists())
        self.assertEqual(len(self.transport.sent), 1)
        self.assertTrue(self.transport.sent[0].startswith("[URGENT] evening-nutrition-review"))

    def test_missing_response_then_valid(self):
        empty = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        code, result = self.run_main(empty, claude_ok(structured()))
        self.assertEqual((code, result["status"]), (0, "completed"))
        self.assertEqual(len(self.prompts), 2)

    def test_authentication_failure_is_classified_and_not_retried(self):
        logged_out = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps({"type": "result", "is_error": True,
                                      "result": "Not logged in · Please run /login"}), stderr="")
        code, result = self.run_main(logged_out)
        self.assertEqual((code, result["status"]), (1, "failed"))
        self.assertIn("claude_auth_failure", result["error"])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("re-authenticate", self.transport.sent[0])
        self.assertNotIn("Not logged in", self.transport.sent[0])

    def test_timeout_fails_without_retry(self):
        code, result = self.run_main(subprocess.TimeoutExpired("claude", 600))
        self.assertEqual((result["status"], result["error"]), ("failed", "Claude execution timed out"))
        self.assertEqual(len(self.prompts), 1)

    def test_telegram_transient_failure_is_retried(self):
        transport = FakeTelegram([(502, '{"ok":false}')])
        code, result = self.run_main(transport=transport)
        self.assertTrue(result["notification_delivered"])
        self.assertEqual(len(transport.sent), 1)

    def test_telegram_rejection_keeps_the_review(self):
        transport = FakeTelegram([(400, '{"ok":false,"description":"chat not found"}')])
        code, result = self.run_main(transport=transport)
        self.assertEqual((code, result["status"], result["notification_delivered"]), (0, "completed", False))
        self.assertIn("chat not found", result["notification_delivery_error"])
        self.assertTrue(self.review_file().exists())

    def test_outside_window_does_nothing(self):
        for now in (local("2026-10-26 20:59"), local("2026-10-27 00:10"), local("2026-10-26 09:00")):
            with self.subTest(now=now):
                code, result = self.run_main(now=now)
                self.assertEqual((code, result["status"], result["reason"]), (0, "skipped", "outside_window"))
        self.assertEqual((self.prompts, self.client_calls, self.transport.sent), ([], 0, []))
        manual_code, manual = self.run_main(claude_ok(structured(execution_mode="manual")),
                                            now=local("2026-10-26 14:00"), mode="manual")
        self.assertEqual(manual["status"], "completed")

    def test_unavailable_athletedata_still_reviews_with_limitations(self):
        def factory():
            raise adc.CredentialUnavailableError("synthetic")
        code, result = self.run_main(claude_ok(structured(status="completed_with_limitations")), factory=factory)
        self.assertEqual(result["status"], "completed_with_limitations")
        self.assertIn('"food_log_get": "error:credential_unavailable"', self.prompts[0])

    def test_dst_change_days(self):
        cases = [(datetime(2026, 10, 25, 20, 30, tzinfo=UTC), "2026-10-25"),   # 21:30 CET
                 (datetime(2027, 3, 28, 19, 30, tzinfo=UTC), "2027-03-28")]    # 21:30 CEST
        for now, day in cases:
            with self.subTest(day=day):
                code, result = self.run_main(claude_ok(structured(local_date=day)), now=now)
                self.assertEqual((result["status"], result["local_date"]), ("completed", day))
                self.assertTrue(self.review_file(day).exists())

    def test_generic_runner_cli_refuses_the_workflow(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wr.main(["--workflow", "evening-nutrition-review", "--mode", "automated"])
        self.assertEqual(code, 2)
        self.assertIn("runner/evening_nutrition_review.py", out.getvalue())

    def test_print_facts_takes_no_lock_and_calls_no_claude(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(wr, "invoke_claude") as invoke:
            code = enr.main(["--print-facts", "--env-file", str(self.env_file), "--repo-root", str(self.root)],
                            now_fn=lambda: EVENING.astimezone(UTC), client_factory=self.client_factory())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["local_date"], "2026-10-26")
        invoke.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())

    def test_workflow_definition_governing_documents_exist(self):
        docs = wr.parse_governing_documents(REPO_ROOT / "workflows" / "evening-nutrition-review.md")
        self.assertIn("goals.md", docs)
        self.assertEqual(len(docs), len(set(docs)))
        for doc in docs:
            self.assertTrue((REPO_ROOT / doc).exists(), doc)


class CoordinationTests(EveningTestCase):
    def lock_dir(self) -> Path:
        return self.root / "runtime" / "locks"

    def test_wait_for_dispatcher(self):
        self.assertEqual(enr.wait_for_dispatcher(self.root), "idle")
        write_lock(self.lock_dir(), "feedback-dispatcher", dead_pid())
        self.assertEqual(enr.wait_for_dispatcher(self.root), "idle")  # stale: its process is gone
        path = write_lock(self.lock_dir(), "feedback-dispatcher", os.getpid())
        sleeps = []
        def release_on_second_poll(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                path.unlink()
        self.assertEqual(enr.wait_for_dispatcher(self.root, sleep_fn=release_on_second_poll), "waited")
        self.assertEqual(sleeps, [enr.DISPATCHER_POLL_SECONDS] * 2)

    def test_wait_for_dispatcher_is_bounded(self):
        write_lock(self.lock_dir(), "feedback-dispatcher", os.getpid())
        clock = iter(range(0, 100000, 15))
        outcome = enr.wait_for_dispatcher(self.root, sleep_fn=lambda s: None, monotonic=lambda: next(clock))
        self.assertEqual(outcome, "timeout")

    def test_dispatcher_lock_stale_seconds_matches_the_dispatcher(self):
        self.assertEqual(enr.DISPATCHER_LOCK_STALE_SECONDS, fd.LOCK_STALE_SECONDS)
        self.assertEqual(enr.DISPATCHER_LOCK, "feedback-dispatcher")

    def test_review_waits_for_a_running_dispatcher_before_claude(self):
        path = write_lock(self.lock_dir(), "feedback-dispatcher", os.getpid())
        order = []
        real_wait = enr.wait_for_dispatcher

        def tracked_wait(repo_root, **kwargs):
            def sleep(seconds):
                order.append("poll")
                path.unlink(missing_ok=True)
            kwargs["sleep_fn"] = sleep
            outcome = real_wait(repo_root, **kwargs)
            order.append(outcome)
            return outcome

        outputs = [claude_ok(structured())]

        def fake_invoke(prompt, config, json_schema=None):
            order.append("claude")
            # While the review runs, its lock pauses the dispatcher's Claude calls.
            order.append(("dispatcher_gate", fd.coaching_workflow_running(self.root)))
            return outputs.pop(0)

        with mock.patch.object(enr, "wait_for_dispatcher", side_effect=tracked_wait), \
                mock.patch.object(wr, "invoke_claude", side_effect=fake_invoke), \
                mock.patch.object(td, "_default_http_post", self.transport), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            enr.main(["--mode", "automated", "--env-file", str(self.env_file), "--repo-root", str(self.root)],
                     now_fn=lambda: EVENING.astimezone(UTC), client_factory=self.client_factory())
        self.assertEqual(order, ["poll", "waited", "claude", ("dispatcher_gate", True)])
        self.assertFalse(fd.coaching_workflow_running(self.root))  # released afterwards

    def test_coaching_workflow_running(self):
        self.assertFalse(fd.coaching_workflow_running(self.root))
        cases = [
            ("evening-nutrition-review-2026-10-26", os.getpid(), timedelta(0), True),
            ("daily-review-2026-10-26", os.getpid(), timedelta(0), True),
            ("evening-nutrition-review-2026-10-25", dead_pid(), timedelta(0), False),   # crashed run
            ("daily-review-2026-10-20", os.getpid(), timedelta(hours=2), False),       # overdue
            ("food-log-check", os.getpid(), timedelta(0), False),                      # no Claude
            ("feedback-dispatcher", os.getpid(), timedelta(0), False),                 # itself
        ]
        for key, pid, age, expected in cases:
            with self.subTest(key=key):
                path = write_lock(self.lock_dir(), key, pid, age)
                self.assertEqual(fd.coaching_workflow_running(self.root), expected)
                path.unlink()

    def test_morning_gate_is_unchanged(self):
        (self.root / "reviews" / "daily").mkdir(parents=True)
        (self.root / "reviews" / "daily" / "2026-10-26.md").write_text("synthetic")
        self.assertTrue(fd.daily_review_settled(self.root, TODAY))
        write_lock(self.lock_dir(), "evening-nutrition-review-2026-10-26", os.getpid())
        self.assertTrue(fd.daily_review_settled(self.root, TODAY))
        write_lock(self.lock_dir(), "daily-review-2026-10-26", os.getpid())
        self.assertFalse(fd.daily_review_settled(self.root, TODAY))


class DispatcherGateTests(unittest.TestCase):
    """The dispatcher's default gate (no injected review_running) against a
    temporary repository's runtime/locks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "workflows").mkdir()
        shutil.copy(REPO_ROOT / "workflows" / "event-feedback.md", self.root / "workflows")
        self.store = migrated_store(self.root / "events.sqlite3")
        add_event(self.store.store, "prior", state="baseline", feedback_status="suppressed_baseline", eligible=0,
                  start=DISPATCH_NOW - timedelta(days=1))
        add_event(self.store.store, "g1")

    def tearDown(self):
        self.store.store.close()
        self._tmp.cleanup()

    def run_dispatcher(self):
        gateway = FakeGateway()
        counts = make_dispatcher(self.store, gateway=gateway, review_running=None, repo_root=self.root,
                                 transport=FakeTelegram()).run(DISPATCH_NOW)
        return counts, gateway

    def test_evening_review_lock_pauses_claude_calls(self):
        write_lock(self.root / "runtime" / "locks", "evening-nutrition-review-2026-10-21", os.getpid())
        counts, gateway = self.run_dispatcher()
        self.assertEqual(counts["deferred_coaching_workflow_running"], 1)
        self.assertEqual(gateway.prompts, [])

    def test_stale_evening_lock_does_not_pause_feedback(self):
        write_lock(self.root / "runtime" / "locks", "evening-nutrition-review-2026-10-20", dead_pid())
        counts, gateway = self.run_dispatcher()
        self.assertNotIn("deferred_coaching_workflow_running", counts)
        self.assertEqual(len(gateway.prompts), 1)

    def test_no_lock_feedback_proceeds(self):
        counts, gateway = self.run_dispatcher()
        self.assertEqual(counts.get("delivered_event"), 1)


if __name__ == "__main__":
    unittest.main()
