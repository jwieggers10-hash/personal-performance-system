"""End-to-end tests for runner/feedback_dispatcher.py.

Claude is a scripted FakeGateway and Telegram a scripted FakeTelegram
transport; subprocess and urllib are additionally patched to fail loudly in
the simulation tests. Every record and credential is synthetic.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_poller as ep  # noqa: E402
import event_store as es  # noqa: E402
import feedback_claude as fc  # noqa: E402
import feedback_dispatcher as fd  # noqa: E402
import feedback_policy as fp  # noqa: E402
import feedback_store as fs  # noqa: E402
import telegram_delivery as td  # noqa: E402
import workflow_runner as wr  # noqa: E402
from feedback_fixtures import (  # noqa: E402
    FAKE_TELEGRAM, NOW, REPO_ROOT, TZ, FakeGateway, FakeTelegram, add_event, batches, good_result, items,
    make_dispatcher, make_v1_store, migrated_store,
)


def local(text: str):
    from datetime import datetime
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


class DispatcherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "events.sqlite3"
        self.store = migrated_store(self.path)
        self.logs: list = []
        # Regular training the day before, so sessions are ROUTINE unless a
        # test sets up something significant.
        add_event(self.store.store, "prior", state="baseline", feedback_status="suppressed_baseline", eligible=0,
                  start=NOW - timedelta(days=1))

    def tearDown(self):
        self.store.store.close()
        self._tmp.cleanup()

    def add(self, key, **kwargs):
        return add_event(self.store.store, key, **kwargs)

    def dispatcher(self, **kwargs):
        kwargs.setdefault("logs", self.logs)
        return make_dispatcher(self.store, **kwargs)


class HappyPathTests(DispatcherTestCase):
    def test_routine_event_generated_and_delivered_once(self):
        self.add("g1")
        gateway, transport = FakeGateway(), FakeTelegram()
        d = self.dispatcher(gateway=gateway, transport=transport)
        counts = d.run(NOW)
        self.assertEqual(counts["generated_routine"], 1)
        self.assertEqual(counts["delivered_event"], 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(transport.sent[0].startswith("Training feedback — 2026-10-21"))
        batch = batches(self.store)[0]
        self.assertEqual((batch["state"], batch["level"], batch["parts_sent"]), ("delivered", "routine", 1))
        self.assertEqual(json.loads(batch["telegram_message_ids"]), [101])
        self.assertIsNone(batch["message_text"])  # cleared once terminal
        self.assertIsNotNone(batch["message_sha256"])
        self.assertEqual(items(self.store)[0]["state"], "delivered")
        self.assertIn("ROUTINE", gateway.prompts[0])
        self.assertIn("source_id=g1", gateway.prompts[0])

        # Later runs never resend.
        for minutes in (15, 30, 60 * 5):
            d.run(NOW + timedelta(minutes=minutes))
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(len(gateway.prompts), 1)

    def test_long_session_is_significant(self):
        self.add("g-long", minutes=110, start=NOW - timedelta(hours=3))
        gateway = FakeGateway()
        transport = FakeTelegram()
        self.dispatcher(gateway=gateway, transport=transport).run(NOW)
        self.assertIn("SIGNIFICANT", gateway.prompts[0])
        self.assertIn("long_session", gateway.prompts[0])
        self.assertTrue(transport.sent[0].startswith("Training feedback (detailed)"))

    def test_related_events_grouped_into_one_message(self):
        self.add("g-run", start=NOW - timedelta(hours=4))
        self.add("h-gym", source="hevy", activity_type=None, event_type="workout", start=NOW - timedelta(hours=3))
        transport = FakeTelegram()
        gateway = FakeGateway()
        self.dispatcher(gateway=gateway, transport=transport).run(NOW)
        self.assertEqual(len(transport.sent), 1)
        self.assertIn("E1", gateway.prompts[0])
        self.assertIn("E2", gateway.prompts[0])
        self.assertIn("hybrid_day", gateway.prompts[0])

    def test_unsettled_event_waits(self):
        self.add("g1", first_seen=NOW - timedelta(minutes=2))
        counts = self.dispatcher().run(NOW)
        self.assertEqual(counts.get("waiting_to_settle"), 1)
        self.assertEqual(items(self.store), [])

    def test_safety_concern_marks_message(self):
        self.add("g1")

        def flagged(prompt, schema):
            result = good_result(prompt, schema)
            result.structured["safety_concern"] = True
            return result
        transport = FakeTelegram()
        self.dispatcher(gateway=FakeGateway([flagged]), transport=transport).run(NOW)
        self.assertTrue(transport.sent[0].startswith("[ATTENTION] "))


class EligibilityTests(DispatcherTestCase):
    def test_ineligible_events_never_processed(self):
        self.add("base", state="baseline", feedback_status="suppressed_baseline", eligible=0)
        self.add("back", feedback_status="suppressed_backfill", eligible=0)
        self.add("del", state="deleted", feedback_status="suppressed_deleted", eligible=0)
        self.add("sup", state="superseded", feedback_status="suppressed_superseded", eligible=0)
        self.add("held", feedback_status="held", state="held", eligible=0, activity_type="STRENGTH_TRAINING")
        self.add("ah", source="apple_health", event_type="weight")  # pending but unverified
        gateway, transport = FakeGateway(), FakeTelegram()
        counts = self.dispatcher(gateway=gateway, transport=transport).run(NOW)
        self.assertEqual(gateway.prompts, [])
        self.assertEqual(transport.sent, [])
        self.assertEqual(counts.get("items_suppressed_unverified_structure"), 1)
        self.assertEqual(len(items(self.store)), 1)  # only the Apple Health row was a candidate

    def test_stale_event_expired_not_sent(self):
        self.add("old", start=NOW - timedelta(hours=50))
        gateway = FakeGateway()
        counts = self.dispatcher(gateway=gateway).run(NOW)
        self.assertEqual(counts["items_expired_stale_occurrence"], 1)
        self.assertEqual(gateway.prompts, [])
        self.assertEqual(items(self.store)[0]["reason"], "stale_occurrence")

    def test_first_activation_expires_old_pending_events(self):
        # The two real Phase 4.2 detections were pending for >24 h at cutover.
        self.add("old-hevy", source="hevy", activity_type=None, event_type="workout",
                 start=NOW - timedelta(hours=40))
        self.add("fresh", start=NOW - timedelta(hours=3))
        d = self.dispatcher()
        d.activate(NOW)
        d.activate(NOW + timedelta(days=3))  # never moves once set
        self.assertEqual(self.store.get_meta("feedback_activated_at"), fp.iso_utc(NOW))
        counts = d.run(NOW)
        self.assertEqual(counts["items_expired_pre_activation"], 1)
        self.assertEqual(counts["delivered_event"], 1)

    def test_revalidation_before_generation_suppresses_superseded(self):
        self.add("g-str", activity_type="STRENGTH_TRAINING")
        d = self.dispatcher(review_settled=lambda day: True)
        quiet = local("2026-10-21T23:00")
        self.add("late", start=quiet - timedelta(hours=1))
        d.run(quiet)  # both queued for the morning digest
        with self.store.transaction():
            self.store.store.update_event("garmin", "g-str", state="superseded", feedback_eligible=0,
                                          feedback_status="suppressed_superseded")
        gateway = FakeGateway()
        transport = FakeTelegram()
        d.gateway, d.http_post = gateway, transport
        counts = d.run(local("2026-10-22T07:45"))
        self.assertEqual(counts["revalidation_suppressed_superseded"], 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertNotIn("E2", gateway.prompts[0])

    def test_event_deleted_after_generation_triggers_regeneration_not_send(self):
        self.add("a", start=NOW - timedelta(hours=4))
        self.add("b", start=NOW - timedelta(hours=3))
        transport = FakeTelegram([(500, '{"description":"x"}')])  # first delivery fails transiently
        gateway = FakeGateway()
        d = self.dispatcher(gateway=gateway, transport=transport)
        d.run(NOW)
        with self.store.transaction():
            self.store.store.update_event("garmin", "b", state="deleted", feedback_eligible=0,
                                          feedback_status="suppressed_deleted")
        counts = d.run(NOW + timedelta(minutes=10))
        self.assertEqual(counts["regenerate_after_revalidation"], 1)
        self.assertEqual(transport.sent, [])
        d.run(NOW + timedelta(minutes=25))
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(len(gateway.prompts), 2)
        self.assertNotIn("E2", gateway.prompts[1])
        states = sorted(r["state"] for r in items(self.store))
        self.assertEqual(states, ["delivered", "suppressed"])

    def test_all_events_invalid_suppresses_batch(self):
        self.add("a")
        d = self.dispatcher(review_settled=lambda day: True)
        d.run(local("2026-10-21T23:00"))
        with self.store.transaction():
            self.store.store.update_event("garmin", "a", state="deleted", feedback_eligible=0,
                                          feedback_status="suppressed_deleted")
        counts = d.run(local("2026-10-22T07:45"))
        self.assertEqual(counts["batches_suppressed"], 1)
        self.assertEqual(batches(self.store)[0]["state"], "suppressed")


class QuietHoursTests(DispatcherTestCase):
    def test_night_events_grouped_into_one_morning_message_after_daily_review(self):
        self.add("g-evening", start=local("2026-10-21T21:00"), minutes=50)
        self.add("h-late", source="hevy", activity_type=None, event_type="workout",
                 start=local("2026-10-22T05:30"), minutes=60)
        review_done = {"value": False}
        transport, gateway = FakeTelegram(), FakeGateway()
        d = self.dispatcher(gateway=gateway, transport=transport, review_settled=lambda day: review_done["value"])

        moment = local("2026-10-21T22:22")
        while moment < local("2026-10-22T07:40"):
            d.run(moment)
            moment += timedelta(minutes=15)
        self.assertEqual(gateway.prompts, [])
        self.assertEqual(transport.sent, [])
        digest = batches(self.store)[0]
        self.assertEqual((digest["kind"], digest["batch_key"]), ("morning_digest", "digest:2026-10-22"))
        self.assertEqual(len(items(self.store)), 2)

        review_done["value"] = True
        d.run(local("2026-10-22T07:52"))
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(transport.sent[0].startswith("Morning training feedback"))
        self.assertIn("reviews/daily/2026-10-22.md", gateway.prompts[0])

    def test_failing_daily_review_does_not_block_past_deadline(self):
        self.add("g-evening", start=local("2026-10-21T21:00"))
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport, review_settled=lambda day: False)
        d.run(local("2026-10-21T22:30"))
        d.run(local("2026-10-22T08:45"))
        self.assertEqual(transport.sent, [])
        d.run(local("2026-10-22T09:07"))
        self.assertEqual(len(transport.sent), 1)

    def test_event_after_release_joins_open_digest(self):
        self.add("g-night", start=local("2026-10-22T05:00"))
        d = self.dispatcher(review_settled=lambda day: False)
        d.run(local("2026-10-22T06:30"))
        self.add("g-morning", start=local("2026-10-22T06:40"), first_seen=local("2026-10-22T07:30"))
        counts = d.run(local("2026-10-22T07:45"))
        self.assertEqual(counts.get("items_joined_morning_digest"), 1)
        self.assertEqual(len(batches(self.store)), 1)

    def test_quiet_hours_on_dst_night(self):
        # Fall-back night 2026-10-24/25: release must be 07:30 CET = 06:30 UTC.
        self.add("g", start=local("2026-10-24T21:30"), minutes=20)
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport)
        d.run(local("2026-10-24T22:30"))
        self.assertEqual(batches(self.store)[0]["release_at"], "2026-10-25T06:30:00+00:00")
        from datetime import datetime, timezone
        d.run(datetime(2026, 10, 25, 6, 29, tzinfo=timezone.utc))
        self.assertEqual(transport.sent, [])
        d.run(datetime(2026, 10, 25, 6, 37, tzinfo=timezone.utc))
        self.assertEqual(len(transport.sent), 1)

    def test_no_claude_call_while_daily_review_runs(self):
        self.add("g1")
        gateway = FakeGateway()
        counts = self.dispatcher(gateway=gateway, review_running=lambda: True).run(NOW)
        self.assertEqual(counts["deferred_daily_review_running"], 1)
        self.assertEqual(gateway.prompts, [])


class ClaudeFailureTests(DispatcherTestCase):
    def test_auth_failures_escalate_and_open_circuit(self):
        self.add("g1")
        gateway = FakeGateway([fc.OUTCOME_AUTH] * 5)
        transport = FakeTelegram()
        d = self.dispatcher(gateway=gateway, transport=transport)
        for i in range(3):
            counts = d.run(NOW + timedelta(minutes=15 * i))
            self.assertEqual(counts.get("deferred_claude_auth_failure"), 1)
        self.assertEqual(self.store.get_meta("dispatcher.auth_consecutive_failures"), "3")
        self.assertEqual(batches(self.store)[0]["generation_attempts"], 0)  # auth never burns attempts
        alert = self.store.get_batch_by_key("alert:claude_auth_escalation:2026-10-21")
        self.assertEqual((alert["state"], alert["urgency"]), ("delivered", "urgent"))
        self.assertIn("[URGENT]", transport.sent[0])
        counts = d.run(NOW + timedelta(hours=1))
        self.assertEqual(counts.get("deferred_auth_circuit_open"), 1)
        self.assertEqual(len(gateway.prompts), 3)
        # Escalation alert is sent once per day, not per failure.
        self.assertEqual(sum("[URGENT]" in m for m in transport.sent), 1)

    def test_success_resets_auth_counter(self):
        self.add("g1")
        d = self.dispatcher(gateway=FakeGateway([fc.OUTCOME_AUTH, "ok"]))
        d.run(NOW)
        self.assertEqual(self.store.get_meta("dispatcher.auth_consecutive_failures"), "1")
        d.run(NOW + timedelta(minutes=15))
        self.assertEqual(self.store.get_meta("dispatcher.auth_consecutive_failures"), "0")

    def test_preflight_blocks_when_cli_reports_logged_out_or_unknown(self):
        self.add("g1")
        gateway = FakeGateway()
        transport = FakeTelegram()
        d = self.dispatcher(gateway=gateway, transport=transport, auth_status=lambda: fc.AUTH_LOGGED_OUT)
        for i in range(3):
            counts = d.run(NOW + timedelta(minutes=15 * i))
            self.assertEqual(counts["deferred_cli_logged_out"], 1)
        # A CLI that says it is logged out counts towards escalation.
        self.assertIn("[URGENT]", transport.sent[0])
        counts = self.dispatcher(gateway=gateway, auth_status=lambda: fc.AUTH_UNKNOWN).run(NOW + timedelta(hours=7))
        self.assertEqual(counts["deferred_auth_status_unknown"], 1)
        counts = self.dispatcher(gateway=gateway, claude_binary="/nonexistent/claude").run(NOW + timedelta(hours=7))
        self.assertEqual(counts["deferred_claude_not_found"], 1)
        self.assertEqual(gateway.prompts, [])

    def test_auth_status_only_checked_when_claude_is_needed(self):
        calls = []

        def status():
            calls.append(1)
            return fc.AUTH_LOGGED_IN
        d = self.dispatcher(auth_status=status)
        d.run(NOW)
        self.assertEqual(calls, [])  # nothing to generate
        self.add("g1")
        self.add("g2", start=NOW - timedelta(hours=10))
        d.run(NOW + timedelta(minutes=15))
        self.assertEqual(calls, [1])  # once per run, not per batch

    def test_auth_expiry_warning_and_cutoff(self):
        from datetime import date
        d = self.dispatcher(auth_expires_on=date(2026, 10, 25))
        d.run(NOW)
        alert = self.store.get_batch_by_key("alert:claude_auth_expiry:2026-10-21")
        self.assertEqual(alert["urgency"], "attention")
        self.add("g1")
        gateway = FakeGateway()
        counts = self.dispatcher(gateway=gateway, auth_expires_on=date(2026, 10, 20)).run(NOW)
        self.assertEqual(counts["deferred_auth_expired_by_date"], 1)
        self.assertEqual(gateway.prompts, [])

    def test_malformed_output_gets_one_corrective_retry(self):
        self.add("g1")

        def bad(prompt, schema):
            return good_result(prompt, schema, message="see https://example.invalid/x?token=abc")
        gateway = FakeGateway([bad, "ok"])
        transport = FakeTelegram()
        counts = self.dispatcher(gateway=gateway, transport=transport).run(NOW)
        self.assertEqual(counts["claude_calls"], 2)
        self.assertIn("IMPORTANT: A previous attempt", gateway.prompts[1])
        self.assertEqual(len(transport.sent), 1)

    def test_retry_limit_then_failed(self):
        self.add("g1")
        gateway = FakeGateway([fc.OUTCOME_EXECUTION] * 10)
        d = self.dispatcher(gateway=gateway)
        moment = NOW
        for _ in range(12):
            d.run(moment)
            moment += timedelta(minutes=15)
        batch = batches(self.store)[0]
        self.assertEqual((batch["state"], batch["generation_attempts"]), ("failed", fd.MAX_GENERATION_ATTEMPTS))
        self.assertEqual(batch["error_category"], fc.OUTCOME_EXECUTION)
        self.assertEqual(len(gateway.prompts), fd.MAX_GENERATION_ATTEMPTS)
        self.assertEqual(items(self.store)[0]["state"], "failed")

    def test_backoff_respected(self):
        self.add("g1")
        gateway = FakeGateway([fc.OUTCOME_TIMEOUT, "ok"])
        d = self.dispatcher(gateway=gateway)
        d.run(NOW)
        d.run(NOW + timedelta(minutes=5))
        self.assertEqual(len(gateway.prompts), 1)
        d.run(NOW + timedelta(minutes=16))
        self.assertEqual(len(gateway.prompts), 2)

    def test_daily_budget(self):
        for i in range(4):
            self.add(f"g{i}", start=NOW - timedelta(hours=20 - 4 * i))
        gateway = FakeGateway()
        transport = FakeTelegram()
        d = self.dispatcher(gateway=gateway, transport=transport, daily_budget=2)
        d.run(NOW)
        d.run(NOW + timedelta(minutes=15))
        self.assertEqual(len(gateway.prompts), 2)
        self.assertIsNotNone(self.store.get_batch_by_key("alert:claude_budget_exhausted:2026-10-21"))
        self.assertEqual(self.store.claude_calls_on("2026-10-21"), 2)

    def test_run_generation_limit(self):
        for i in range(5):
            self.add(f"g{i}", start=NOW - timedelta(hours=20 - 4 * i))
        gateway = FakeGateway()
        counts = self.dispatcher(gateway=gateway).run(NOW)
        self.assertEqual(len(gateway.prompts), fd.MAX_GENERATIONS_PER_RUN)
        self.assertEqual(counts["deferred_run_limit"], 5 - fd.MAX_GENERATIONS_PER_RUN)


class TelegramFailureTests(DispatcherTestCase):
    def run_with(self, script, runs=1, step=timedelta(minutes=20)):
        self.add("g1")
        transport = FakeTelegram(script)
        d = self.dispatcher(transport=transport)
        moment = NOW
        for _ in range(runs):
            d.run(moment)
            moment += step
        return transport, batches(self.store)[0]

    def test_transient_failure_retried_then_delivered(self):
        transport, batch = self.run_with([(502, "{}"), td.TelegramNetworkError("connection refused")], runs=3)
        self.assertEqual(batch["state"], "delivered")
        self.assertEqual(batch["delivery_attempts"], 3)
        self.assertEqual(len(transport.sent), 1)

    def test_rate_limit_honours_retry_after(self):
        transport, batch = self.run_with([(429, '{"parameters": {"retry_after": 1800}}')], runs=1)
        self.assertEqual(batch["state"], "queued")
        self.assertEqual(batch["next_attempt_at"], fp.iso_utc(NOW + timedelta(seconds=1800)))

    def test_permanent_rejection_fails_without_retry(self):
        transport, batch = self.run_with([(400, '{"description": "chat not found"}')], runs=3)
        self.assertEqual((batch["state"], batch["error_category"]), ("failed", "telegram_rejected"))
        self.assertEqual(transport.sent, [])

    def test_ambiguous_outcomes_are_never_resent(self):
        for failure in (socket.timeout("timed out"), TimeoutError("timed out"),
                        ConnectionResetError("reset"), (504, "{}")):
            with self.subTest(failure=repr(failure)):
                self.tearDown()
                self.setUp()
                transport, batch = self.run_with([failure], runs=3)
                self.assertEqual((batch["state"], batch["error_category"]), ("failed", "delivery_outcome_unknown"))
                self.assertEqual(transport.sent, [])

    def test_connect_phase_failures_are_retried(self):
        # urllib raises URLError (TelegramNetworkError) only before the
        # request was fully sent, so Telegram cannot have delivered it.
        transport, batch = self.run_with([td.TelegramNetworkError("timed out"),
                                          td.TelegramNetworkError("Name or service not known")], runs=3)
        self.assertEqual(batch["state"], "delivered")
        self.assertEqual(len(transport.sent), 1)

    def test_partially_delivered_message_is_completed_not_regenerated(self):
        self.add("g1", minutes=110, start=NOW - timedelta(hours=4))
        long_message = ("Paragraph of feedback text. " * 40 + "\n\n") * 4

        def long(prompt, schema):
            return good_result(prompt, schema, message=long_message[:2400])
        with mock.patch.object(td, "TELEGRAM_MESSAGE_LIMIT", 800):
            transport = FakeTelegram([None, (502, "{}")])
            gateway = FakeGateway([long])
            d = self.dispatcher(gateway=gateway, transport=transport)
            d.run(NOW)
            with self.store.transaction():
                self.store.store.update_event("garmin", "g1", state="deleted", feedback_eligible=0,
                                              feedback_status="suppressed_deleted")
            d.run(NOW + timedelta(minutes=20))
        batch = batches(self.store)[0]
        self.assertEqual(batch["state"], "delivered")
        self.assertEqual(len(gateway.prompts), 1)
        self.assertEqual(len(transport.sent), batch["parts_total"])

    def test_definitive_failure_can_be_requeued_ambiguous_cannot(self):
        self.add("g1")
        self.add("g2", start=NOW - timedelta(hours=10))
        transport = FakeTelegram([(403, '{"description": "bot was blocked"}'), socket.timeout("timed out")])
        gateway = FakeGateway()
        d = self.dispatcher(gateway=gateway, transport=transport)
        d.run(NOW)
        states = sorted((b["error_category"], b["state"]) for b in batches(self.store))
        self.assertEqual(states, [("delivery_outcome_unknown", "failed"), ("telegram_rejected", "failed")])
        self.assertEqual(fd.requeue_failed_deliveries(self.store, NOW + timedelta(minutes=30)), 1)
        self.assertEqual(fd.requeue_failed_deliveries(self.store, NOW + timedelta(minutes=31)), 0)
        d.run(NOW + timedelta(minutes=35))
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(len(gateway.prompts), 3)  # regenerated: message text is not kept after failure
        self.assertEqual(sorted(b["state"] for b in batches(self.store)), ["delivered", "failed"])

    def test_delivery_retry_limit(self):
        transport, batch = self.run_with([(503, "{}")] * 10, runs=10, step=timedelta(hours=1))
        self.assertEqual((batch["state"], batch["error_category"]), ("failed", "telegram_retries_exhausted"))
        self.assertEqual(batch["delivery_attempts"], fd.MAX_DELIVERY_ATTEMPTS)

    def test_multi_part_resumes_without_resending(self):
        self.add("g1", minutes=110, start=NOW - timedelta(hours=4))
        long_message = ("Paragraph of feedback text. " * 40 + "\n\n") * 4

        def long(prompt, schema):
            return good_result(prompt, schema, message=long_message[:2400])
        with mock.patch.object(td, "TELEGRAM_MESSAGE_LIMIT", 800):
            transport = FakeTelegram([None, (502, "{}")])
            d = self.dispatcher(gateway=FakeGateway([long]), transport=transport)
            d.run(NOW)
            first = batches(self.store)[0]
            self.assertEqual(first["parts_sent"], 1)
            d.run(NOW + timedelta(minutes=20))
        batch = batches(self.store)[0]
        self.assertEqual(batch["state"], "delivered")
        self.assertEqual(len(transport.sent), batch["parts_total"])
        self.assertEqual(len(set(transport.sent)), len(transport.sent))

    def test_telegram_not_configured_keeps_outbox_queued(self):
        self.add("g1")
        counts = self.dispatcher(telegram=None).run(NOW)
        self.assertEqual(counts["deferred_telegram_not_configured"], 1)
        self.assertEqual(batches(self.store)[0]["stage"], "deliver")

    def test_undelivered_feedback_expires(self):
        self.add("g1")
        d = self.dispatcher(telegram=None)
        d.run(NOW)
        d.telegram = FAKE_TELEGRAM
        transport = FakeTelegram()
        d.http_post = transport
        # 08:00 local next day: past the delivery window, outside quiet hours.
        d.run(NOW + timedelta(hours=20))
        self.assertEqual(batches(self.store)[0]["state"], "expired")
        self.assertEqual(transport.sent, [])

    def crash_during_delivery(self, **batch_fields):
        """Leaves a generated batch claimed for delivery by a dead process."""
        self.add("g1")
        d = self.dispatcher(telegram=None)
        d.run(NOW)
        batch_id = batches(self.store)[0]["id"]
        with self.store.transaction():
            self.store.claim_batch(batch_id, stage="deliver", owner="dead", now_text=fp.iso_utc(NOW))
            if batch_fields:
                self.store.update_batch(batch_id, fp.iso_utc(NOW), **batch_fields)
        d.telegram = FAKE_TELEGRAM
        d.http_post = FakeTelegram()
        return d, d.http_post

    def test_crash_with_send_in_flight_is_never_resent(self):
        d, transport = self.crash_during_delivery(parts_total=1, inflight_part=0)
        d.run(NOW + timedelta(minutes=10))
        self.assertEqual(batches(self.store)[0]["state"], "processing")  # lease not yet expired
        counts = d.run(NOW + fd.PROCESSING_LEASE + timedelta(minutes=1))
        self.assertEqual(counts["failed_delivery_outcome_unknown"], 1)
        self.assertEqual(transport.sent, [])
        self.assertEqual(items(self.store)[0]["reason"], "delivery_outcome_unknown")

    def test_crash_before_any_send_resumes_once(self):
        d, transport = self.crash_during_delivery()
        counts = d.run(NOW + fd.PROCESSING_LEASE + timedelta(minutes=1))
        self.assertEqual(counts["recovered_delivery"], 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(batches(self.store)[0]["state"], "delivered")

    def test_crash_after_all_parts_confirmed_marks_delivered_without_resend(self):
        d, transport = self.crash_during_delivery(parts_total=1, parts_sent=1, telegram_message_ids="[7]")
        counts = d.run(NOW + fd.PROCESSING_LEASE + timedelta(minutes=1))
        self.assertEqual(counts["recovered_delivered"], 1)
        self.assertEqual(transport.sent, [])
        self.assertEqual(items(self.store)[0]["state"], "delivered")

    def test_crashed_generation_recovered_and_retried(self):
        self.add("g1")
        d = self.dispatcher()
        d.intake(NOW)
        with self.store.transaction():
            self.store.claim_batch(batches(self.store)[0]["id"], stage="generate", owner="dead",
                                   now_text=fp.iso_utc(NOW))
        counts = d.run(NOW + fd.PROCESSING_LEASE + timedelta(minutes=1))
        self.assertEqual(counts["recovered_generation"], 1)
        self.assertEqual(batches(self.store)[0]["state"], "delivered")


class ConcurrencyTests(DispatcherTestCase):
    def test_two_dispatchers_never_duplicate(self):
        for i in range(3):
            self.add(f"g{i}", start=NOW - timedelta(hours=20 - 6 * i))
        transports = [FakeTelegram(), FakeTelegram()]
        errors: list = []
        barrier = threading.Barrier(2)

        def run(transport):
            # Each dispatcher has its own connection, as two processes would.
            store = fs.FeedbackStore(es.EventStore.open(self.path))
            try:
                d = make_dispatcher(store, transport=transport)
                barrier.wait()
                for minutes in (0, 15, 30):
                    d.run(NOW + timedelta(minutes=minutes))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.store.close()
        threads = [threading.Thread(target=run, args=(t,)) for t in transports]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        sent = transports[0].sent + transports[1].sent
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(items(self.store)), 3)
        self.assertTrue(all(r["state"] == "delivered" for r in batches(self.store)))

    def test_claim_is_compare_and_set(self):
        self.add("g1")
        d = self.dispatcher()
        d.intake(NOW)
        batch_id = batches(self.store)[0]["id"]
        other = fs.FeedbackStore(es.EventStore.open(self.path))
        try:
            with self.store.transaction():
                self.assertTrue(self.store.claim_batch(batch_id, stage="generate", owner="a",
                                                       now_text=fp.iso_utc(NOW)))
            with other.transaction():
                self.assertFalse(other.claim_batch(batch_id, stage="generate", owner="b", now_text=fp.iso_utc(NOW)))
        finally:
            other.store.close()

    def test_live_lock_prevents_second_process(self):
        root = self.dir / "repo"
        (root / "runtime" / "locks").mkdir(parents=True)
        (root / "workflows").mkdir()
        (root / "runtime" / "locks" / "feedback-dispatcher.lock").write_text(
            json.dumps({"pid": os.getpid(), "acquired_at": fp.iso_utc(NOW.now(NOW.tzinfo))}))
        env = self.dir / "env"
        env.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\nFEEDBACK_DISPATCH_ENABLED=true\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = fd.main(["--once", "--repo-root", str(root), "--env-file", str(env),
                            "--db-path", str(self.path)], gateway_factory=lambda c: FakeGateway())
        self.assertEqual(code, 0)
        self.assertEqual(batches(self.store), [])


class RedactionTests(DispatcherTestCase):
    def test_secret_like_messages_rejected(self):
        for message in ("token=abcdef123", "Bearer abc.def.ghi", "visit http://x.invalid", "key sk-ant-abcdefghijk"):
            with self.subTest(message=message):
                feedback, problems = fc.validate_feedback(
                    good_result('batch_ref="B1", events_covered listing exactly E1, between',
                                fc.output_schema("routine"), message=message).structured,
                    batch_ref="B1", level="routine", refs=["E1"])
                self.assertIsNone(feedback)
                self.assertTrue(any("URL or secret" in p for p in problems))

    def test_logs_and_outputs_carry_no_identifiers_or_tokens(self):
        self.add("garmin-activity-98765")
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport)
        counts = d.run(NOW)
        blob = json.dumps(self.logs) + json.dumps(counts)
        self.assertNotIn("98765", blob)
        self.assertNotIn(FAKE_TELEGRAM.bot_token, blob)
        self.assertNotIn(FAKE_TELEGRAM.chat_id, blob)
        status = json.dumps(self.store.status_summary("2026-10-21"))
        self.assertNotIn("98765", status)

    def test_telegram_errors_redacted(self):
        def boom(url, data, timeout=10.0):
            raise td.TelegramNetworkError(f"failed for {url}")
        with self.assertRaises(td.TelegramNetworkError) as ctx:
            td.send_single_message(FAKE_TELEGRAM, "hi", http_post=boom)
        self.assertNotIn(FAKE_TELEGRAM.bot_token, str(ctx.exception))

    def test_message_text_cleared_after_terminal_state(self):
        self.add("g1")
        self.dispatcher(transport=FakeTelegram([(400, "{}")])).run(NOW)
        batch = batches(self.store)[0]
        self.assertEqual(batch["state"], "failed")
        self.assertIsNone(batch["message_text"])


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "events.sqlite3"
        store = make_v1_store(self.path)
        add_event(store, "secret-id-12345", start=NOW - timedelta(hours=3))
        add_event(store, "old-67890", source="hevy", activity_type=None, event_type="workout",
                  start=NOW - timedelta(hours=40))
        add_event(store, "base-1", state="baseline", feedback_status="suppressed_baseline", eligible=0)
        store.close()
        self.env = self.dir / "env"
        self.env.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\nTELEGRAM_BOT_TOKEN=999:REALLOOKING\n"
                            "TELEGRAM_CHAT_ID=424242\nFEEDBACK_DISPATCH_ENABLED=true\n")

    def tearDown(self):
        self._tmp.cleanup()

    def simulate(self, *extra):
        out = io.StringIO()
        forbidden = mock.Mock(side_effect=AssertionError("network or process use in simulation"))
        with mock.patch.object(subprocess, "run", forbidden), \
                mock.patch.object(urllib.request, "urlopen", forbidden), \
                mock.patch.object(wr, "invoke_claude", forbidden), \
                mock.patch.object(td, "_default_http_post", forbidden), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = fd.main(["--simulate", "--env-file", str(self.env), "--db-path", str(self.path),
                            "--repo-root", str(REPO_ROOT), *extra])
        return code, out.getvalue()

    def test_simulation_never_writes_and_never_calls_out(self):
        before = self.path.read_bytes()
        code, output = self.simulate("--at", fp.iso_utc(NOW))
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.dir / "locks").exists())
        report = json.loads(output)
        self.assertEqual(report["claude_invocations"], 0)
        self.assertEqual(report["telegram_requests"], 0)
        self.assertEqual(report["totals"]["items_expired_pre_activation"], 1)
        self.assertEqual(report["totals"]["delivered_event"], 1)
        self.assertEqual(report["events_not_feedback_eligible_by_status"], {"suppressed_baseline": 1})
        conn = sqlite3.connect(str(self.path))
        self.assertEqual(fs.schema_version(conn), 1)
        conn.close()

    def test_simulation_output_is_aggregate_only(self):
        _, output = self.simulate("--at", fp.iso_utc(NOW))
        for secret in ("12345", "67890", "REALLOOKING", "424242"):
            self.assertNotIn(secret, output)

    def test_simulation_over_a_night_shows_queueing(self):
        code, output = self.simulate("--at", "2026-10-21T21:50:00+02:00", "--until", "2026-10-22T09:30:00+02:00")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertGreater(report["ticks"], 40)
        self.assertEqual(report["simulated_telegram_messages"], 1)

    def test_simulation_span_limited(self):
        code, _ = self.simulate("--at", "2026-10-01T00:00:00+00:00", "--until", "2026-10-21T00:00:00+00:00")
        self.assertEqual(code, 2)

    def test_live_mode_disabled_by_default(self):
        env = self.dir / "env-off"
        env.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\n")
        before = self.path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"FEEDBACK_DISPATCH_ENABLED": ""}):
            code = fd.main(["--once", "--env-file", str(env), "--db-path", str(self.path),
                            "--repo-root", str(REPO_ROOT)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["reason"], "dispatch_disabled")
        self.assertEqual(self.path.read_bytes(), before)

    def test_live_mode_refuses_unmigrated_store(self):
        root = self.dir / "repo"
        (root / "workflows").mkdir(parents=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = fd.main(["--once", "--env-file", str(self.env), "--db-path", str(self.path),
                            "--repo-root", str(root)], gateway_factory=lambda c: FakeGateway())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error_category"], "migration_error")


class PollerIsolationTests(unittest.TestCase):
    def test_poller_does_not_import_dispatcher_or_claude_modules(self):
        source = (Path(fd.__file__).parent / "event_poller.py").read_text()
        for name in ("feedback_dispatcher", "feedback_claude", "telegram_delivery", "invoke_claude"):
            self.assertNotIn(name, source)

    def test_poller_unit_still_denies_credentials(self):
        unit = (Path(fd.__file__).parent / "systemd" / "personal-performance-event-poller.service").read_text()
        self.assertIn("InaccessiblePaths=-/home/coach/.claude", unit)
        self.assertIn("InaccessiblePaths=-/home/coach/personal-performance-system/runner/config.env", unit)

    def test_dispatcher_timer_avoids_daily_review_minute(self):
        timer = (Path(fd.__file__).parent / "systemd" / "personal-performance-feedback-dispatcher.timer").read_text()
        self.assertIn("OnCalendar=*-*-* *:07,22,37,52:00", timer)


if __name__ == "__main__":
    unittest.main()


class EventTimingTests(DispatcherTestCase):
    """Occurrence, first-seen, eligibility and notification window."""

    def held_strength(self, start):
        """A Garmin strength activity the poller held at first sight."""
        first_seen = start + timedelta(minutes=80)
        add_event(self.store.store, "g-str", start=start, minutes=60, first_seen=first_seen, state="held",
                  feedback_status="held", eligible=0, activity_type="STRENGTH_TRAINING")
        with self.store.transaction():
            self.store.store.update_event("garmin", "g-str", hold_until=fp.iso_utc(first_seen + ep.STRENGTH_HOLD))

    def release(self, now):
        """Runs the poller's own release logic after a successful Hevy poll."""
        with self.store.transaction():
            self.store.store.update_source_state("hevy", last_success_at=fp.iso_utc(now))
            counts = ep.reconcile_strength_duplicates(self.store.store, now=now, today=now.astimezone(TZ).date())
        self.assertEqual(counts.get("released"), 1)

    def test_garmin_strength_held_24h_still_gets_feedback(self):
        start = NOW - timedelta(hours=26)
        self.held_strength(start)
        released_at = start + timedelta(minutes=80) + ep.STRENGTH_HOLD + timedelta(minutes=10)
        self.release(released_at)
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport)
        d.run(released_at + timedelta(minutes=5))
        self.assertEqual(transport.sent, [])  # settling after release
        counts = d.run(released_at + timedelta(minutes=20))
        self.assertEqual(counts["delivered_event"], 1)

    def test_strength_released_overnight_goes_into_morning_message(self):
        start = local("2026-10-21T05:00")
        self.held_strength(start)
        released_at = local("2026-10-22T06:47")  # hold expired during quiet hours
        self.release(released_at)
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport, review_settled=lambda day: True)
        d.run(local("2026-10-22T07:07"))
        self.assertEqual(batches(self.store)[0]["kind"], "morning_digest")
        d.run(local("2026-10-22T07:37"))
        self.assertEqual(len(transport.sent), 1)  # ~26.5 h after the session

    def test_late_arriving_garmin_activity_gets_feedback(self):
        self.add("g-late", start=NOW - timedelta(hours=30), first_seen=NOW - timedelta(minutes=20))
        counts = self.dispatcher().run(NOW)
        self.assertEqual(counts["delivered_event"], 1)

    def test_late_activity_accepted_overnight_survives_to_morning(self):
        # Accepted at 23:00 with occurrence 40 h old; delivered after 07:30
        # when it is ~49 h old. Waiting is not staleness.
        accepted = local("2026-10-21T23:00")
        self.add("g-late", start=accepted - timedelta(hours=40), first_seen=accepted - timedelta(minutes=20))
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport, review_settled=lambda day: True)
        d.run(accepted)
        d.run(local("2026-10-22T07:37"))
        self.assertEqual(len(transport.sent), 1)

    def test_overnight_event_delayed_by_daily_review(self):
        self.add("g-night", start=local("2026-10-21T22:00"), first_seen=local("2026-10-21T22:47"))
        settled = {"value": False}
        transport = FakeTelegram()
        d = self.dispatcher(transport=transport, review_settled=lambda day: settled["value"])
        moment = local("2026-10-21T23:07")
        while moment < local("2026-10-22T08:50"):
            d.run(moment)
            moment += timedelta(minutes=15)
        self.assertEqual(transport.sent, [])
        settled["value"] = True
        d.run(local("2026-10-22T08:52"))
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(items(self.store)[0]["state"], "delivered")

    def test_genuinely_stale_events_never_get_feedback(self):
        self.add("historic", start=NOW - timedelta(days=3), first_seen=NOW - timedelta(minutes=20))
        self.add("missed", start=NOW - timedelta(hours=27), first_seen=NOW - timedelta(hours=26))
        gateway = FakeGateway()
        counts = self.dispatcher(gateway=gateway).run(NOW)
        self.assertEqual(counts["items_expired_stale_occurrence"], 1)
        self.assertEqual(counts["items_expired_notification_window_passed"], 1)
        self.assertEqual(gateway.prompts, [])

    def test_queued_feedback_expires_at_notification_window(self):
        self.add("g1", first_seen=NOW - timedelta(minutes=20))
        gateway = FakeGateway([fc.OUTCOME_QUOTA] * 10)
        d = self.dispatcher(gateway=gateway)
        d.run(NOW)
        counts = d.run(NOW + fp.NOTIFICATION_WINDOW)
        self.assertEqual(counts["revalidation_expired_notification_window_passed"], 1)
        self.assertEqual(batches(self.store)[0]["state"], "expired")


class MigrationCompatibilityTests(unittest.TestCase):
    """The real poller keeps writing to a migrated store, and rollback keeps
    every detection."""

    def setUp(self):
        from test_event_poller import PollerHarness
        from fake_athletedata import garmin_activity, hevy_updated
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.h = PollerHarness(self.dir)
        self.garmin_activity, self.hevy_updated = garmin_activity, hevy_updated
        poll_local = lambda d, h: local(f"{d}T{h:02d}:00")  # noqa: E731
        self.h.fake.garmin = [garmin_activity(1001, "2026-10-19", poll_local("2026-10-19", 7))]
        self.h.fake.hevy = [hevy_updated("w-base", poll_local("2026-10-19", 18), poll_local("2026-10-19", 19),
                                         poll_local("2026-10-19", 19))]
        self.h.poll(NOW - timedelta(days=2))  # baseline

    def tearDown(self):
        self.h.store.close()
        self._tmp.cleanup()

    def test_poller_writes_after_migration_and_dispatcher_sees_it(self):
        cursor = self.h.state("hevy")["cursor"]
        before = fs.v1_snapshot(self.h.store.conn)
        fs.migrate_to_v2(self.h.store.conn, now_text=fp.iso_utc(NOW))
        self.assertEqual(fs.v1_snapshot(self.h.store.conn), before)
        self.assertEqual(self.h.state("hevy")["cursor"], cursor)
        self.h.fake.garmin.append(self.garmin_activity(1002, "2026-10-21", NOW - timedelta(hours=2)))
        self.h.fake.hevy.append(self.hevy_updated("w-new", NOW - timedelta(hours=6), NOW - timedelta(hours=5),
                                                  NOW - timedelta(hours=5)))
        summary = self.h.poll(NOW - timedelta(minutes=30))
        self.assertIn(summary["outcome"], ("ok", "partial"))
        self.assertEqual(self.h.event("garmin", 1002)["feedback_status"], "pending")
        self.assertEqual(self.h.event("hevy", "w-new")["feedback_status"], "pending")
        self.assertNotEqual(self.h.state("hevy")["cursor"], cursor)
        self.assertEqual(fs.integrity_problems(self.h.store.conn), [])
        transport = FakeTelegram()
        counts = make_dispatcher(fs.FeedbackStore(self.h.store), transport=transport).run(NOW)
        self.assertEqual(counts["items_queued"], 2)
        self.assertEqual(len(transport.sent), 2)  # sessions ended 4 h apart: not grouped
        self.h.poll(NOW + timedelta(minutes=15))  # poller still writes with feedback rows present
        self.assertEqual(fs.integrity_problems(self.h.store.conn), [])

    def test_downgrade_keeps_detections_made_after_migration(self):
        fs.migrate_to_v2(self.h.store.conn, now_text=fp.iso_utc(NOW))
        self.h.fake.garmin.append(self.garmin_activity(1002, "2026-10-21", NOW - timedelta(hours=2)))
        self.h.poll(NOW - timedelta(minutes=30))
        make_dispatcher(fs.FeedbackStore(self.h.store)).run(NOW)
        before = fs.v1_snapshot(self.h.store.conn)
        self.assertEqual(fs.downgrade_to_v1(self.h.store.conn)["applied"], True)
        self.assertEqual(fs.schema_version(self.h.store.conn), 1)
        self.assertFalse(set(fs.V2_TABLES) & fs._tables(self.h.store.conn))
        self.assertEqual(fs.v1_snapshot(self.h.store.conn), before)
        self.assertFalse(fs.downgrade_to_v1(self.h.store.conn)["applied"])
        self.h.poll(NOW + timedelta(minutes=15))  # poller fine on v1 again
        self.assertEqual(fs.migrate_to_v2(self.h.store.conn, now_text=fp.iso_utc(NOW))["applied"], True)

    def test_downgrade_command_backs_up_v2_first(self):
        root = self.dir / "repo"
        (root / "runtime").mkdir(parents=True)
        path = root / "runtime" / "events.sqlite3"
        fs.backup_database(self.h.store.conn, path)
        conn = sqlite3.connect(str(path))
        fs.migrate_to_v2(conn, now_text=fp.iso_utc(NOW))
        conn.close()
        report = fd.downgrade_apply(path, root / "runtime", NOW)
        self.assertTrue(report["applied"])
        backup = sqlite3.connect(str(root / report["backup"]))
        self.assertEqual(fs.schema_version(backup), 2)
        backup.close()
        self.assertFalse((root / "runtime" / "locks" / "event-poller.lock").exists())


class AuthStatusTests(unittest.TestCase):
    def run_status(self, stdout="", returncode=0, exc=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if exc:
                raise exc
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
        return fc.cli_auth_status("claude", run=fake_run), calls

    def test_uses_non_generative_status_command(self):
        status, calls = self.run_status(json.dumps({"loggedIn": True, "email": "athlete@example.invalid"}))
        self.assertEqual(status, fc.AUTH_LOGGED_IN)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["claude", "auth", "status", "--json"])  # no prompt, no --print
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], fc.AUTH_STATUS_TIMEOUT_SECONDS)

    def test_classification(self):
        cases = [
            (dict(stdout=json.dumps({"loggedIn": False}), returncode=1), fc.AUTH_LOGGED_OUT),
            (dict(stdout="not json"), fc.AUTH_UNKNOWN),
            (dict(stdout=json.dumps({"something": "else"})), fc.AUTH_UNKNOWN),
            (dict(stdout=json.dumps(["loggedIn"])), fc.AUTH_UNKNOWN),
            (dict(exc=subprocess.TimeoutExpired("claude", 20)), fc.AUTH_UNKNOWN),
            (dict(exc=FileNotFoundError("claude")), fc.AUTH_UNKNOWN),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.run_status(**kwargs)[0], expected)

    def test_account_details_never_reach_logs(self):
        logs: list = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = migrated_store(Path(tmp.name) / "e.sqlite3")
        self.addCleanup(store.store.close)
        add_event(store.store, "g1")
        payload = json.dumps({"loggedIn": True, "email": "athlete@example.invalid", "orgId": "org-secret-1"})
        status = lambda: fc.cli_auth_status(  # noqa: E731
            "claude", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""))
        counts = make_dispatcher(store, auth_status=status, logs=logs).run(NOW)
        self.assertIn("delivered_event", counts)
        blob = json.dumps(logs)
        self.assertNotIn("athlete@example.invalid", blob)
        self.assertNotIn("org-secret-1", blob)
        self.assertIn(fc.ASSURANCE_LOCAL, blob)

    def test_gateway_passes_feedback_schema_to_cli(self):
        seen = {}

        def fake_invoke(prompt, config, json_schema=None):
            seen["schema"] = json_schema
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"structured_output": {"a": 1}}), stderr="")
        result = fc.ClaudeGateway(config=None, invoke=fake_invoke).call("p", fc.output_schema("routine"))
        self.assertEqual(result.outcome, fc.OUTCOME_OK)
        self.assertEqual(seen["schema"], fc.output_schema("routine"))
