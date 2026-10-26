"""Tests for runner/food_log_check.py: complete, incomplete, missing and
stale logs; at most one reminder per day across restarts; Telegram failures
and the retry rule; AthleteData failures; the local run window across DST.
Temporary directories, synthetic data and a scripted Telegram transport only;
nothing contacts AthleteData or Telegram."""

from __future__ import annotations

import io
import contextlib
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import athletedata_client as adc  # noqa: E402
import food_log_check as flc  # noqa: E402
import nutrition_data as nd  # noqa: E402
import telegram_delivery as td  # noqa: E402
import workflow_runner as wr  # noqa: E402
from feedback_fixtures import FakeTelegram  # noqa: E402
from nutrition_fixtures import (  # noqa: E402
    TODAY, TZ, UTC, FakeClient, food_entry, food_page, history, local, usual_day,
)

ENV = {"ATHLETE_TIMEZONE": "Europe/Amsterdam", "TELEGRAM_BOT_TOKEN": "123456:SYNTHETIC-token-value",
       "TELEGRAM_CHAT_ID": "999000111"}
EIGHT_PM = local("2026-10-26 20:00")


def meals(*names):
    return [e for e in usual_day(TODAY) if e["mealType"] in names]


class CheckTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.transport = FakeTelegram()
        self.sleeps, self.logs = [], []

    def tearDown(self):
        self._tmp.cleanup()

    def client(self, today_entries, last_sync=local("2026-10-26 19:42"), hist=None):
        page = food_page((history(TODAY) if hist is None else hist) + today_entries, last_sync)
        return FakeClient({"food_log_get": page})

    def run_check(self, client=None, now=EIGHT_PM, env=ENV, dry_run=False, transport=None):
        client = client or self.client(usual_day(TODAY))
        factory = client if callable(client) and not isinstance(client, FakeClient) else (lambda: client)
        return flc.run_check(repo_root=self.root, env=dict(env), now=now.astimezone(UTC), client_factory=factory,
                             http_post=transport or self.transport, sleep_fn=self.sleeps.append,
                             log=lambda **f: self.logs.append(f), dry_run=dry_run)

    def state(self, day=TODAY):
        path = flc.state_path(self.root, day.isoformat())
        return json.loads(path.read_text()) if path.exists() else None


class DecisionTests(CheckTestCase):
    def test_complete_log_sends_nothing(self):
        result = self.run_check()
        self.assertEqual(result["outcome"], "no_reminder")
        self.assertEqual(self.transport.sent, [])
        self.assertEqual((self.state()["status"], self.state()["reminder"]), ("complete", False))

    def test_missing_dinner(self):
        result = self.run_check(self.client(meals("breakfast", "lunch")))
        self.assertEqual(result["outcome"], "reminder_sent")
        self.assertEqual(self.transport.sent, [
            "Food log check — 20:00\n\n"
            "Cronometer currently shows breakfast and lunch, but no dinner or snacks. Last sync: 19:42.\n\n"
            "If you've eaten anything else today, please add it to Cronometer so your evening review is more "
            "accurate."])
        self.assertEqual((self.state()["delivery"], self.state()["telegram_message_id"]), ("sent", 101))

    def test_low_intake_with_all_slots(self):
        entries = [food_entry(TODAY, "breakfast", 300), food_entry(TODAY, "lunch", 300),
                   food_entry(TODAY, "dinner", 400)]
        self.run_check(self.client(entries))
        self.assertIn("Cronometer shows breakfast, lunch and dinner, but today's total so far (1,000 kcal) is well "
                      "below your usual fully logged day (about 2,400 kcal).", self.transport.sent[0])

    def test_missing_log_with_fresh_sync(self):
        self.run_check(self.client([], last_sync=local("2026-10-26 19:30")))
        self.assertIn("Cronometer shows no food entries for today yet. Last sync: 19:30.", self.transport.sent[0])
        self.assertIn("If you've eaten anything else today", self.transport.sent[0])

    def test_missing_log_with_stale_sync_suggests_a_sync_delay(self):
        self.run_check(self.client([], last_sync=local("2026-10-25 21:14")))
        text = self.transport.sent[0]
        self.assertIn("Last sync: yesterday 21:14. Entries added since then may not have synchronised yet.", text)
        self.assertIn("they may simply not have synchronised yet", text)

    def test_stale_incomplete_log(self):
        result = self.run_check(self.client(meals("breakfast"), last_sync=local("2026-10-26 12:05")))
        self.assertTrue(result["stale"])
        self.assertIn("Last sync: 12:05. Entries added since then may not have synchronised yet.",
                      self.transport.sent[0])

    def test_no_sync_time(self):
        self.run_check(self.client(meals("breakfast"), last_sync=None))
        self.assertIn("No Cronometer sync time is available", self.transport.sent[0])

    def test_stale_but_complete_sends_nothing(self):
        result = self.run_check(self.client(usual_day(TODAY), last_sync=local("2026-10-26 15:00")))
        self.assertEqual((result["outcome"], result["stale"]), ("no_reminder", True))
        self.assertEqual(self.transport.sent, [])

    def test_unknown_pattern_sends_nothing(self):
        result = self.run_check(self.client(meals("breakfast"), hist=history(TODAY, days=2)))
        self.assertEqual((result["outcome"], result["status"]), ("no_reminder", "unknown"))

    def test_no_breakfast_eater_is_not_reminded_about_breakfast(self):
        slots = (("lunch", 900), ("dinner", 1100), ("snack", 400))
        today = [food_entry(TODAY, m, k) for m, k in slots]
        result = self.run_check(self.client(today, hist=history(TODAY, slots=slots)))
        self.assertEqual(result["outcome"], "no_reminder")

    def test_logs_carry_no_intake_values(self):
        self.run_check(self.client(meals("breakfast", "lunch")))
        text = json.dumps(self.logs)
        self.assertNotIn("kcal", text)
        self.assertNotIn("1,150", text)
        self.assertNotIn("Cronometer currently shows", text)
        self.assertNotIn("SYNTHETIC-token", text)


class DuplicateTests(CheckTestCase):
    def test_one_reminder_per_day(self):
        self.run_check(self.client(meals("breakfast")))
        again = self.run_check(self.client(meals("breakfast")), now=local("2026-10-26 20:10"))
        self.assertEqual((again["outcome"], again["reason"]), ("skipped", "already_checked_today"))
        self.assertEqual(len(self.transport.sent), 1)

    def test_no_reminder_decision_also_holds_for_the_day(self):
        self.run_check()
        again = self.run_check(self.client(meals("breakfast")), now=local("2026-10-26 20:30"))
        self.assertEqual(again["reason"], "already_checked_today")
        self.assertEqual(self.transport.sent, [])

    def test_restart_after_a_crash_mid_send_never_resends(self):
        # The state is claimed before the send; a crash leaves it at "sending".
        flc.claim_state(flc.state_path(self.root, TODAY.isoformat()), {"delivery": "sending"})
        result = self.run_check(self.client(meals("breakfast")))
        self.assertEqual(result["reason"], "already_checked_today")
        self.assertEqual(self.transport.sent, [])

    def test_concurrent_run_is_skipped(self):
        lock = wr.WorkflowLock(self.root / "runtime" / "locks", flc.LOCK_NAME, flc.LOCK_STALE_SECONDS)
        lock.acquire()
        try:
            result = self.run_check(self.client(meals("breakfast")))
        finally:
            lock.release()
        self.assertEqual((result["outcome"], result["reason"]), ("skipped", "lock_held"))
        self.assertIsNone(self.state())

    def test_next_day_is_independent(self):
        self.run_check(self.client(meals("breakfast")))
        tomorrow = TODAY + timedelta(days=1)
        page = food_page(history(tomorrow) + [food_entry(tomorrow, "breakfast", 500)], local("2026-10-27 19:40"))
        result = self.run_check(FakeClient({"food_log_get": page}), now=local("2026-10-27 20:00"))
        self.assertEqual(result["outcome"], "reminder_sent")
        self.assertEqual(len(self.transport.sent), 2)

    def test_state_file_is_owner_only(self):
        self.run_check()
        mode = flc.state_path(self.root, TODAY.isoformat()).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_dry_run_sends_and_records_nothing(self):
        result = self.run_check(self.client(meals("breakfast")), dry_run=True, now=local("2026-10-26 14:00"))
        self.assertEqual(result["outcome"], "dry_run_reminder")
        self.assertIn("Food log check — 14:00", result["message"])
        self.assertEqual(self.transport.sent, [])
        self.assertIsNone(self.state())


class TelegramTests(CheckTestCase):
    def deliver(self, script):
        transport = FakeTelegram(script)
        result = self.run_check(self.client(meals("breakfast")), transport=transport)
        return result, transport

    def test_transient_error_is_retried_once(self):
        result, transport = self.deliver([(500, '{"ok":false,"description":"Internal"}')])
        self.assertEqual(result["outcome"], "reminder_sent")
        self.assertEqual((len(transport.sent), self.sleeps), (1, [flc.DEFAULT_RETRY_DELAY]))

    def test_rate_limit_honours_retry_after_within_bounds(self):
        body = json.dumps({"ok": False, "parameters": {"retry_after": 600}})
        result, transport = self.deliver([(429, body)])
        self.assertEqual(result["outcome"], "reminder_sent")
        self.assertEqual(self.sleeps, [flc.MAX_RETRY_DELAY])

    def test_connect_error_is_retried_and_then_given_up(self):
        error = td.TelegramNetworkError("synthetic connect failure")
        result, transport = self.deliver([error, error])
        self.assertEqual(result["outcome"], "reminder_retries_exhausted")
        self.assertEqual(transport.sent, [])
        self.assertEqual(self.state()["delivery"], "retries_exhausted")

    def test_rejected_is_not_retried(self):
        result, transport = self.deliver([(400, '{"ok":false,"description":"chat not found"}')])
        self.assertEqual(result["outcome"], "reminder_rejected")
        self.assertEqual(self.sleeps, [])

    def test_ambiguous_outcomes_are_never_resent(self):
        for step in ((504, "{}"), socket.timeout("read timed out")):
            with self.subTest(step=step):
                self.tearDown()
                self.setUp()
                result, transport = self.deliver([step])
                self.assertEqual(result["outcome"], "reminder_unknown")
                self.assertEqual((transport.sent, self.sleeps), ([], []))
                self.assertEqual(self.state()["delivery"], "unknown")

    def test_failed_delivery_is_not_retried_by_a_later_run(self):
        self.deliver([(400, "{}")])
        again = self.run_check(self.client(meals("breakfast")), now=local("2026-10-26 20:15"))
        self.assertEqual(again["reason"], "already_checked_today")
        self.assertEqual(self.transport.sent, [])

    def test_telegram_not_configured(self):
        env = {"ATHLETE_TIMEZONE": "Europe/Amsterdam"}
        result = self.run_check(self.client(meals("breakfast")), env=env)
        self.assertEqual(result["outcome"], "telegram_not_configured")
        self.assertEqual(self.state()["delivery"], "not_configured")


class AvailabilityAndTimeTests(CheckTestCase):
    def test_athletedata_failure_records_nothing(self):
        for failure in (FakeClient({"food_log_get": adc.TransportError("synthetic", "network")}),
                        FakeClient({}, verify_error=adc.HTTPStatusError(503))):
            with self.subTest(failure=failure):
                result = self.run_check(failure)
                self.assertEqual(result["outcome"], "unavailable")
                self.assertIsNone(self.state())
        def no_credentials():
            raise adc.CredentialUnavailableError("synthetic")
        self.assertEqual(self.run_check(no_credentials)["error_category"], "credential_unavailable")
        self.assertEqual(self.transport.sent, [])

    def test_outside_the_run_window(self):
        for now in (local("2026-10-26 19:00"), local("2026-10-26 22:00"), local("2026-10-26 08:00")):
            with self.subTest(now=now):
                client = self.client(meals("breakfast"))
                result = self.run_check(client, now=now)
                self.assertEqual(result["reason"], "outside_window")
                self.assertEqual(client.calls, [])

    def test_dst_change_days_use_local_time(self):
        cases = [
            (datetime(2026, 10, 25, 19, 0, tzinfo=UTC), "20:00", date(2026, 10, 25)),  # CET, after the change
            (datetime(2027, 3, 28, 18, 0, tzinfo=UTC), "20:00", date(2027, 3, 28)),    # CEST, after the change
        ]
        for now, label, day in cases:
            with self.subTest(now=now):
                self.tearDown()
                self.setUp()
                page = food_page(history(day) + [food_entry(day, "breakfast", 500)], now - timedelta(minutes=20))
                result = self.run_check(FakeClient({"food_log_get": page}), now=now)
                self.assertEqual((result["outcome"], result["local_date"]), ("reminder_sent", day.isoformat()))
                self.assertTrue(self.transport.sent[0].startswith(f"Food log check — {label}"))
                self.assertFalse(result["stale"])

    def test_cli_exit_codes(self):
        env_file = self.root / "env"
        env_file.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = flc.main(["--dry-run", "--env-file", str(env_file), "--repo-root", str(self.root)],
                            now_fn=lambda: EIGHT_PM.astimezone(UTC),
                            client_factory=lambda: self.client(meals("breakfast")))
        self.assertEqual((code, json.loads(out.getvalue())["outcome"]), (0, "dry_run_reminder"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = flc.main(["--once", "--env-file", str(env_file), "--repo-root", str(self.root)],
                            now_fn=lambda: EIGHT_PM.astimezone(UTC),
                            client_factory=lambda: FakeClient({}, verify_error=adc.HTTPStatusError(503)))
        self.assertEqual((code, json.loads(out.getvalue())["outcome"]), (1, "unavailable"))


if __name__ == "__main__":
    unittest.main()
