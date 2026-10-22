"""Approved-plan (supervised) dispatch: --simulate --max-batches prints a plan
token; --once --expect-plan TOKEN --max-batches N executes exactly that plan
or nothing. Every event and credential is synthetic; Claude and Telegram are
scripted fakes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_store as es  # noqa: E402
import feedback_claude as fc  # noqa: E402
import feedback_dispatcher as fd  # noqa: E402
import feedback_policy as fp  # noqa: E402
import feedback_store as fs  # noqa: E402
from feedback_fixtures import (  # noqa: E402
    NOW, REPO_ROOT, TZ, FakeGateway, FakeTelegram, add_event, batches, items, make_dispatcher, migrated_store,
)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


class SupervisedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "events.sqlite3"
        self.store = migrated_store(self.path)
        add_event(self.store.store, "prior", state="baseline", feedback_status="suppressed_baseline", eligible=0,
                  start=NOW - timedelta(days=1))
        self.gateway, self.transport = FakeGateway(), FakeTelegram()

    def tearDown(self):
        self.store.store.close()
        self._tmp.cleanup()

    def add(self, key, **kwargs):
        return add_event(self.store.store, key, **kwargs)

    def dispatcher(self, store=None, **kwargs):
        kwargs.setdefault("gateway", self.gateway)
        kwargs.setdefault("transport", self.transport)
        return make_dispatcher(store or self.store, **kwargs)

    def plan(self, now=NOW, max_batches=1, **kwargs):
        return self.dispatcher(**kwargs).build_plan(now, created_at=now, max_batches=max_batches)

    def state(self):
        """Every row of every table, to prove a rejected run wrote nothing."""
        conn = self.store.conn
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]
        return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}"))) for t in tables}

    def assert_rejected(self, result, outcome, before):
        self.assertEqual(result["outcome"], outcome)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.gateway.prompts, [])
        self.assertEqual(self.transport.sent, [])


class PlanDigestTests(SupervisedTestCase):
    def test_stable_for_identical_state(self):
        self.add("g1")
        self.add("h1", source="hevy", activity_type=None, event_type="workout", start=NOW - timedelta(hours=8))
        self.assertEqual(self.plan().digest, self.plan().digest)

    def test_independent_of_row_order(self):
        self.add("g1", start=NOW - timedelta(hours=9))
        self.add("g2", start=NOW - timedelta(hours=2))
        d = self.dispatcher()
        original = d.store.candidate_events
        with mock.patch.object(d.store, "candidate_events", side_effect=lambda: list(reversed(original()))):
            reversed_digest = d.build_plan(NOW, created_at=NOW, max_batches=2).digest
        self.assertEqual(reversed_digest, d.build_plan(NOW, created_at=NOW, max_batches=2).digest)

    def test_changes_with_every_relevant_input(self):
        self.add("g1")
        base = self.plan().digest
        self.assertNotEqual(self.plan(max_batches=2).digest, base)
        self.assertNotEqual(self.dispatcher().build_plan(NOW, created_at=NOW - timedelta(minutes=1),
                                                         max_batches=1).digest, base)
        with self.store.transaction():
            self.store.store.update_event("garmin", "g1", state="edited")
        edited = self.plan().digest
        self.assertNotEqual(edited, base)
        with self.store.transaction():
            self.store.store.update_event("garmin", "g1", start_time_utc=fp.iso_utc(NOW - timedelta(hours=3)))
        self.assertNotEqual(self.plan().digest, edited)

    def test_grouping_change_changes_digest(self):
        self.add("g1", start=NOW - timedelta(hours=9))
        self.add("g2", start=NOW - timedelta(hours=2))
        separate = self.plan(max_batches=2)
        self.assertEqual(separate.summary(TZ)["batches_to_process"], 2)
        with self.store.transaction():  # move g1 next to g2: now one group
            self.store.store.update_event("garmin", "g1", start_time_utc=fp.iso_utc(NOW - timedelta(hours=3)),
                                          end_time_utc=fp.iso_utc(NOW - timedelta(hours=2, minutes=15)))
        grouped = self.plan(max_batches=2)
        self.assertEqual(grouped.summary(TZ)["batches_to_process"], 1)
        self.assertNotEqual(grouped.digest, separate.digest)

    def test_summary_and_token_reveal_no_identifiers(self):
        self.add("garmin-activity-918273", start=NOW - timedelta(hours=3))
        plan = self.plan()
        text = json.dumps(plan.summary(TZ)) + plan.token
        self.assertNotIn("918273", text)
        self.assertNotIn("f" * 64, text)  # the stored fingerprint
        self.assertRegex(plan.token, r"^p1\.\d+\.[0-9a-f]{64}$")
        summary = plan.summary(TZ)
        self.assertEqual((summary["eligible_events"], summary["batches_to_process"]), (1, 1))
        self.assertTrue(summary["suitable_for_supervised_execution"])
        self.assertEqual(summary["candidates_by_source"], {"garmin": {"eligible": 1}})
        self.assertEqual(summary["planned_new_batches"][0]["events"],
                         [{"source": "garmin", "activity_type": "running", "local_date": "2026-10-21",
                           "detected_minutes_ago": 115}])


class SupervisedRunTests(SupervisedTestCase):
    def test_approved_plan_delivers_exactly_one_batch(self):
        self.add("old", source="hevy", activity_type=None, event_type="workout", start=NOW - timedelta(hours=40))
        self.add("g-new", start=NOW - timedelta(hours=2))
        plan = self.plan()
        self.assertEqual(plan.summary(TZ)["events_expiring_or_suppressed"], 1)
        result = self.dispatcher().run_supervised(NOW, plan.token, 1)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual([b["state"] for b in batches(self.store)], ["delivered"])
        self.assertEqual(sorted(r["reason"] or "-" for r in items(self.store)), ["-", "pre_activation"])
        self.assertIsNotNone(self.store.get_meta("feedback_activated_at"))
        self.assertIsNotNone(self.store.get_meta(f"dispatcher.plan_consumed.{plan.digest}"))

    def test_new_garmin_event_after_simulation_aborts(self):
        self.add("g1")
        plan = self.plan()
        self.add("g-late", start=NOW - timedelta(hours=8))
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_mismatch", before)

    def test_new_hevy_event_after_simulation_aborts(self):
        self.add("g1")
        plan = self.plan()
        self.add("h-late", source="hevy", activity_type=None, event_type="workout", start=NOW - timedelta(hours=1))
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_mismatch", before)

    def test_deletion_or_supersession_after_simulation_aborts(self):
        for change in ({"state": "deleted", "feedback_status": "suppressed_deleted", "feedback_eligible": 0},
                       {"state": "superseded", "feedback_status": "suppressed_superseded", "feedback_eligible": 0}):
            with self.subTest(change=change["state"]):
                self.tearDown()
                self.setUp()
                self.add("g1")
                plan = self.plan()
                with self.store.transaction():
                    self.store.store.update_event("garmin", "g1", **change)
                before = self.state()
                self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_mismatch", before)

    def test_tampered_and_malformed_tokens(self):
        self.add("g1")
        plan = self.plan()
        flipped = plan.token[:-1] + ("0" if plan.token[-1] != "0" else "1")
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, flipped, 1), "plan_mismatch", before)
        for bad in ("", "p1.123.abc", "p2." + plan.token[3:], plan.token + ".x", plan.digest):
            self.assert_rejected(self.dispatcher().run_supervised(NOW, bad, 1), "plan_invalid", before)

    def test_stale_and_future_approvals_rejected(self):
        self.add("g1")
        plan = self.plan()
        before = self.state()
        late = NOW + fd.PLAN_TTL + timedelta(seconds=1)
        self.assert_rejected(self.dispatcher().run_supervised(late, plan.token, 1), "plan_stale", before)
        early = NOW - fd.PLAN_CLOCK_SKEW - timedelta(seconds=1)
        self.assert_rejected(self.dispatcher().run_supervised(early, plan.token, 1), "plan_stale", before)

    def test_approval_is_single_use(self):
        self.add("g1")
        plan = self.plan()
        self.assertEqual(self.dispatcher().run_supervised(NOW, plan.token, 1)["outcome"], "ok")
        before = self.state()
        sent = list(self.transport.sent)
        result = self.dispatcher().run_supervised(NOW + timedelta(minutes=1), plan.token, 1)
        self.assertEqual(result["outcome"], "plan_already_used")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.transport.sent, sent)

    def test_max_batches_must_match_the_approval(self):
        self.add("g1")
        plan = self.plan(max_batches=1)
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 2), "plan_mismatch", before)

    def test_more_batches_than_allowed_is_unsuitable(self):
        self.add("g1", start=NOW - timedelta(hours=9))
        self.add("g2", start=NOW - timedelta(hours=2))
        plan = self.plan(max_batches=1)
        self.assertIn("exceeds_max_batches", plan.blockers)
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_unsuitable", before)
        two = self.plan(max_batches=2)
        self.assertEqual(self.dispatcher().run_supervised(NOW, two.token, 2)["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 2)

    def test_unsuitable_plans(self):
        cases = {
            "events_settling": dict(first_seen=NOW - timedelta(minutes=3)),
            "nothing_to_process": dict(state="baseline", feedback_status="suppressed_baseline", eligible=0),
        }
        for blocker, kwargs in cases.items():
            with self.subTest(blocker=blocker):
                self.tearDown()
                self.setUp()
                self.add("g1", **kwargs)
                plan = self.plan()
                self.assertIn(blocker, plan.blockers)
                before = self.state()
                self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_unsuitable", before)

    def test_quiet_hours_defer_new_events_so_plan_is_unsuitable(self):
        night = local("2026-10-21T23:00")
        self.add("g1", start=night - timedelta(hours=2))
        plan = self.plan(now=night)
        self.assertIn("new_events_deferred", plan.blockers)
        self.assertFalse(plan.summary(TZ)["suitable_for_supervised_execution"])

    def test_apple_health_events_stay_excluded(self):
        self.add("ah", source="apple_health", event_type="body_weight")
        self.add("g1")
        plan = self.plan()
        self.assertEqual(plan.summary(TZ)["candidates_by_source"]["apple_health"],
                         {"suppressed:unverified_structure": 1})
        self.assertEqual(self.dispatcher().run_supervised(NOW, plan.token, 1)["outcome"], "ok")
        self.assertNotIn("apple_health", self.gateway.prompts[0])


class ExistingOutboxTests(SupervisedTestCase):
    def queue_undelivered_batch(self, key, start):
        """An earlier run generated feedback but Telegram was not configured."""
        self.add(key, start=start)
        make_dispatcher(self.store, telegram=None, gateway=self.gateway).run(NOW - timedelta(minutes=30))
        self.gateway.prompts.clear()

    def test_existing_queued_batch_is_part_of_the_plan(self):
        self.queue_undelivered_batch("g-old", NOW - timedelta(hours=5))
        self.add("g-new", start=NOW - timedelta(hours=2))
        plan = self.plan(max_batches=1)
        self.assertEqual(plan.summary(TZ)["existing_open_batches"], 1)
        self.assertIn("exceeds_max_batches", plan.blockers)  # old delivery + new batch = 2
        plan2 = self.plan(max_batches=2)
        self.assertEqual(self.dispatcher().run_supervised(NOW, plan2.token, 2)["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 2)

    def test_queued_batch_appearing_after_simulation_aborts(self):
        self.add("g-new", start=NOW - timedelta(hours=1))
        plan = self.plan()
        with self.store.transaction():
            self.store.create_batch(batch_key="alert:claude_auth_expiry:2026-10-21", kind="system_alert",
                                    release_at=fp.iso_utc(NOW), now_text=fp.iso_utc(NOW), urgency="attention",
                                    stage="deliver", message_text="synthetic alert")
        before = self.state()
        self.assert_rejected(self.dispatcher().run_supervised(NOW, plan.token, 1), "plan_mismatch", before)

    def test_not_yet_due_batch_is_never_delivered(self):
        self.queue_undelivered_batch("g-old", NOW - timedelta(hours=5))
        with self.store.transaction():
            self.store.update_batch(batches(self.store)[0]["id"], fp.iso_utc(NOW),
                                    next_attempt_at=fp.iso_utc(NOW + timedelta(hours=1)))
        self.add("g-new", start=NOW - timedelta(hours=2))
        plan = self.plan()
        self.assertEqual(plan.blockers, [])
        self.assertEqual(self.dispatcher().run_supervised(NOW, plan.token, 1)["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 1)
        states = {b["batch_key"]: b["state"] for b in batches(self.store)}
        self.assertEqual(sorted(states.values()), ["delivered", "queued"])

    def test_alerts_raised_during_the_run_are_not_delivered(self):
        self.add("g1")
        plan = self.plan(daily_budget=0)
        result = self.dispatcher(daily_budget=0).run_supervised(NOW, plan.token, 1)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["counts"]["deferred_budget_exhausted"], 1)
        alert = self.store.get_batch_by_key("alert:claude_budget_exhausted:2026-10-21")
        self.assertEqual(alert["state"], "queued")
        self.assertEqual(self.transport.sent, [])

    def test_batch_in_progress_blocks_until_lease_then_recovery_is_planned(self):
        self.add("g1")
        d = self.dispatcher(telegram=None)
        d.run(NOW)
        batch_id = batches(self.store)[0]["id"]
        with self.store.transaction():
            self.store.claim_batch(batch_id, stage="deliver", owner="crashed", now_text=fp.iso_utc(NOW))
        self.assertIn("batch_in_progress", self.plan(now=NOW + timedelta(minutes=5)).blockers)
        later = NOW + fd.PROCESSING_LEASE + timedelta(minutes=1)
        plan = self.plan(now=later)
        self.assertEqual(plan.blockers, [])
        self.assertEqual(self.dispatcher().run_supervised(later, plan.token, 1)["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 1)  # no send was in flight: resumed once

    def test_crash_with_send_in_flight_is_planned_as_failure_not_resent(self):
        self.add("g1")
        self.dispatcher(telegram=None).run(NOW)
        batch_id = batches(self.store)[0]["id"]
        with self.store.transaction():
            self.store.claim_batch(batch_id, stage="deliver", owner="crashed", now_text=fp.iso_utc(NOW))
            self.store.update_batch(batch_id, fp.iso_utc(NOW), parts_total=1, inflight_part=0)
        self.add("g-new", start=NOW + timedelta(hours=1), first_seen=NOW + timedelta(hours=1, minutes=20))
        later = NOW + timedelta(hours=1, minutes=40)
        plan = self.plan(now=later)
        self.assertEqual(plan.blockers, [])
        self.assertEqual(self.dispatcher().run_supervised(later, plan.token, 1)["outcome"], "ok")
        self.assertEqual(len(self.transport.sent), 1)  # only the new event's feedback
        self.assertEqual(self.store.get_batch(batch_id)["error_category"], "delivery_outcome_unknown")


class RevalidationAfterApprovalTests(SupervisedTestCase):
    def test_event_invalidated_after_commit_is_suppressed_not_substituted(self):
        self.add("g1")
        self.add("g-other", start=NOW - timedelta(hours=12))  # separate group, needs a second batch
        plan = self.plan(max_batches=2)
        d = self.dispatcher()
        original_generate = d.generate

        def supersede_then_generate(batch_id, now):
            # The poller supersedes the approved event between commit and generation.
            with self.store.transaction():
                self.store.store.update_event("garmin", "g1", state="superseded", feedback_eligible=0,
                                              feedback_status="suppressed_superseded")
            d.generate = original_generate
            return original_generate(batch_id, now)
        d.generate = supersede_then_generate
        self.assertEqual(d.run_supervised(NOW, plan.token, 2)["outcome"], "ok")
        states = sorted(b["state"] for b in batches(self.store))
        self.assertEqual(states, ["delivered", "suppressed"])
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(len(self.gateway.prompts), 1)


class ConcurrencyTests(SupervisedTestCase):
    def test_poller_writes_are_blocked_during_compare_and_claim(self):
        self.add("g1")
        plan = self.plan()
        d = self.dispatcher()
        observed = {}
        original = d.build_plan

        def build_then_try_poller_write(*args, **kwargs):
            result = original(*args, **kwargs)
            poller = sqlite3.connect(str(self.path), timeout=0.2, isolation_level=None)
            try:
                poller.execute("BEGIN IMMEDIATE")
                observed["poller_write"] = "succeeded"
                poller.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                observed["poller_write"] = str(exc)
            finally:
                poller.close()
            return result
        with mock.patch.object(d, "build_plan", side_effect=build_then_try_poller_write):
            self.assertEqual(d.run_supervised(NOW, plan.token, 1)["outcome"], "ok")
        self.assertIn("locked", observed["poller_write"])

    def test_concurrent_dispatchers_with_one_approval_send_once(self):
        self.add("g1")
        plan = self.plan()
        outcomes, transports = [], [FakeTelegram(), FakeTelegram()]
        barrier = threading.Barrier(2)

        def run(transport):
            store = fs.FeedbackStore(es.EventStore.open(self.path))
            try:
                d = make_dispatcher(store, transport=transport)
                barrier.wait()
                outcomes.append(d.run_supervised(NOW, plan.token, 1)["outcome"])
            finally:
                store.store.close()
        threads = [threading.Thread(target=run, args=(t,)) for t in transports]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes)[0], "ok")
        self.assertIn(sorted(outcomes)[1], ("plan_already_used", "plan_mismatch"))
        self.assertEqual(len(transports[0].sent) + len(transports[1].sent), 1)

    def test_crash_after_commit_cannot_be_replayed(self):
        self.add("g1")
        plan = self.plan()
        d = self.dispatcher()
        with mock.patch.object(d, "generate", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                d.run_supervised(NOW, plan.token, 1)
        self.assertEqual(self.dispatcher().run_supervised(NOW, plan.token, 1)["outcome"], "plan_already_used")
        self.assertEqual(self.transport.sent, [])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "workflows").mkdir()
        shutil.copy(REPO_ROOT / "workflows" / "event-feedback.md", self.root / "workflows")
        (self.root / "runtime").mkdir()
        self.path = self.root / "runtime" / "events.sqlite3"
        store = migrated_store(self.path)
        add_event(store.store, "prior", state="baseline", feedback_status="suppressed_baseline", eligible=0,
                  start=NOW - timedelta(days=1))
        add_event(store.store, "g1")
        store.store.close()
        self.env = self.root / "env"
        self.env.write_text(f"ATHLETE_TIMEZONE=Europe/Amsterdam\nCLAUDE_BINARY={sys.executable}\n"
                            "TELEGRAM_BOT_TOKEN=123:SYNTHETIC\nTELEGRAM_CHAT_ID=42\n")
        self.transport, self.gateway = FakeTelegram(), FakeGateway()

    def tearDown(self):
        self._tmp.cleanup()

    def main(self, *argv, enabled=True):
        out = io.StringIO()
        env = {"FEEDBACK_DISPATCH_ENABLED": "true" if enabled else ""}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(fc, "cli_auth_status", return_value=fc.AUTH_LOGGED_IN):
            code = fd.main([*argv, "--repo-root", str(self.root), "--env-file", str(self.env),
                            "--db-path", str(self.path)], now_fn=lambda: NOW,
                           gateway_factory=lambda config: self.gateway, http_post=self.transport)
        return code, json.loads(out.getvalue())

    def test_simulate_then_supervised_run(self):
        code, report = self.main("--simulate", "--max-batches", "1")
        self.assertEqual(code, 0)
        plan = report["supervised_plan"]
        self.assertTrue(plan["suitable_for_supervised_execution"])
        self.assertEqual(report["supervised_result"]["outcome"], "ok")
        self.assertEqual(self.transport.sent, [])  # simulation used its own recording transport
        code, result = self.main("--once", "--expect-plan", plan["plan_token"], "--max-batches", "1")
        self.assertEqual((code, result["outcome"]), (0, "ok"))
        self.assertEqual(len(self.transport.sent), 1)
        code, again = self.main("--once", "--expect-plan", plan["plan_token"], "--max-batches", "1")
        self.assertEqual((code, again["outcome"]), (3, "plan_already_used"))
        self.assertEqual(len(self.transport.sent), 1)

    def test_mismatch_exits_without_changes(self):
        _, report = self.main("--simulate", "--max-batches", "1")
        token = report["supervised_plan"]["plan_token"]
        store = es.EventStore.open(self.path)
        add_event(store, "g-late", start=NOW - timedelta(hours=9))
        before = sorted(map(tuple, store.conn.execute("SELECT * FROM meta")))
        store.close()
        code, result = self.main("--once", "--expect-plan", token, "--max-batches", "1")
        self.assertEqual((code, result["outcome"]), (3, "plan_mismatch"))
        conn = sqlite3.connect(str(self.path))
        self.assertEqual(sorted(map(tuple, conn.execute("SELECT * FROM meta"))), before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_items").fetchone()[0], 0)
        conn.close()
        self.assertEqual((self.gateway.prompts, self.transport.sent), ([], []))

    def test_simulation_at_another_time_gives_no_usable_token(self):
        _, report = self.main("--simulate", "--max-batches", "1", "--at", fp.iso_utc(NOW - timedelta(hours=1)))
        self.assertIsNone(report["supervised_plan"]["plan_token"])

    def test_supervised_run_still_requires_enablement(self):
        _, report = self.main("--simulate", "--max-batches", "1")
        code, result = self.main("--once", "--expect-plan", report["supervised_plan"]["plan_token"],
                                 "--max-batches", "1", enabled=False)
        self.assertEqual((code, result["reason"]), (0, "dispatch_disabled"))
        self.assertEqual(self.transport.sent, [])

    def test_ambiguous_arguments_rejected(self):
        token = "p1.1.%s" % ("a" * 64)
        cases = [
            ("--simulate", "--expect-plan", token),
            ("--once", "--expect-plan", token),
            ("--once", "--max-batches", "1"),
            ("--once", "--expect-plan", token, "--max-batches", "0"),
            ("--once", "--expect-plan", token, "--max-batches", str(fd.MAX_SUPERVISED_BATCHES + 1)),
            ("--once", "--at", fp.iso_utc(NOW)),
            ("--simulate", "--max-batches", "1", "--until", fp.iso_utc(NOW + timedelta(hours=1))),
            ("--status", "--max-batches", "1"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, result = self.main(*argv)
                self.assertEqual((code, result["error_category"]), (2, "invalid_arguments"))
        self.assertEqual(self.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
