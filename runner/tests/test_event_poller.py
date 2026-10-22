"""Unit tests for runner/event_poller.py and runner/event_store.py.

Polls run end-to-end through the real AthleteDataClient against the
synthetic server in fake_athletedata.py. Every record, measurement and
credential is synthetic; no test touches the network, Claude, or Telegram.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import athletedata_client as adc  # noqa: E402
import event_poller as ep  # noqa: E402
import event_store as es  # noqa: E402
from fake_athletedata import (  # noqa: E402
    SYNTHETIC_KEY, SYNTHETIC_URL, FakeAthleteData, garmin_activity, hevy_deleted, hevy_updated, text_result,
)

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)  # 12:00 local, local date 2026-10-20
FIXED_KEY = b"k" * 32


def local(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=TZ)


class PollerHarness:
    def __init__(self, directory: Path):
        self.db_path = directory / "events.sqlite3"
        self.store = es.EventStore.open(self.db_path)
        self.fake = FakeAthleteData()
        self.fp = es.Fingerprinter(FIXED_KEY)
        self.logs: list = []
        self.sleeps: list = []

    def poll(self, now=NOW):
        client = adc.AthleteDataClient(adc.McpEndpoint(SYNTHETIC_URL), http_post=self.fake,
                                       sleep_fn=self.sleeps.append)
        return ep.run_poll(self.store, client=client, now=now, tz=TZ, fp=self.fp,
                           log=lambda **fields: self.logs.append(fields))

    def event(self, source, key):
        return self.store.get_event(source, str(key))

    def events(self, source):
        return self.store.conn.execute("SELECT * FROM events WHERE source = ?", (source,)).fetchall()

    def changes(self, source, key=None):
        if key is None:
            return [r["change"] for r in self.store.conn.execute(
                "SELECT change FROM event_changes WHERE source = ? ORDER BY id", (source,))]
        return [r["change"] for r in self.store.conn.execute(
            "SELECT change FROM event_changes WHERE source = ? AND event_key = ? ORDER BY id", (source, str(key)))]

    def state(self, source):
        return self.store.get_source_state(source)


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.h = PollerHarness(self.dir)

    def tearDown(self):
        self.h.store.close()
        self._tmp.cleanup()

    def baseline(self):
        """Seeds one record per source, runs the first poll, returns its summary."""
        self.h.fake.garmin = [garmin_activity(1001, "2026-10-19", local("2026-10-19", 7))]
        self.h.fake.hevy = [hevy_updated("w-base", local("2026-10-19", 18), local("2026-10-19", 19),
                                         local("2026-10-19", 19, 30))]
        self.h.fake.apple = {"weight": [{"date": "2026-10-19", "value": 81.37, "uuid": "u-1"}], "bodyFat": []}
        return self.h.poll()


class BaselineTests(PollerTestCase):
    def test_first_run_registers_baseline_and_suppresses_feedback(self):
        summary = self.baseline()
        self.assertEqual(summary["outcome"], "ok")
        for source in ep.SOURCES:
            self.assertIsNotNone(self.h.state(source)["baseline_completed_at"], source)
            for row in self.h.events(source):
                self.assertEqual((row["state"], row["feedback_status"], row["feedback_eligible"]),
                                 ("baseline", "suppressed_baseline", 0))
        pending = self.h.store.conn.execute("SELECT COUNT(*) FROM events WHERE feedback_eligible = 1").fetchone()[0]
        self.assertEqual(pending, 0)

    def test_repeat_poll_is_idempotent(self):
        self.baseline()
        events_before = self.h.store.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        changes_before = self.h.store.conn.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0]
        summary = self.h.poll(NOW + timedelta(minutes=15))
        events_after = self.h.store.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        self.assertEqual(len(events_before), len(events_after))
        for before, after in zip(events_before, events_after):
            for column in ("state", "feedback_status", "fingerprint", "first_seen_at"):
                self.assertEqual(before[column], after[column])
        self.assertEqual(self.h.store.conn.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0],
                         changes_before)
        self.assertEqual(summary["sources"]["garmin"]["changes"], {"unchanged": 1})

    def test_baseline_is_independent_per_source(self):
        self.h.fake.garmin = [garmin_activity(1001, "2026-10-19", local("2026-10-19", 7))]
        self.h.fake.hevy = [hevy_updated("w-base", local("2026-10-19", 18), local("2026-10-19", 19),
                                         local("2026-10-19", 19, 30))]
        self.h.fake.failures["garmin_get_activities"] = [503, 503, 503]
        first = self.h.poll()
        self.assertEqual(first["sources"]["garmin"]["outcome"], "failed")
        self.assertIsNone(self.h.state("garmin")["baseline_completed_at"])
        self.assertIsNotNone(self.h.state("hevy")["baseline_completed_at"])

        later = NOW + timedelta(minutes=15)
        self.h.fake.hevy.append(hevy_updated("w-new", local("2026-10-20", 9), local("2026-10-20", 10),
                                             later - timedelta(minutes=5)))
        self.h.poll(later)
        self.assertEqual(self.h.event("garmin", 1001)["state"], "baseline")
        self.assertEqual(self.h.event("hevy", "w-new")["feedback_status"], "pending")


class GarminTests(PollerTestCase):
    def test_new_activity_after_baseline_is_pending(self):
        self.baseline()
        self.h.fake.garmin.append(garmin_activity(1002, "2026-10-20", local("2026-10-20", 7)))
        self.h.poll(NOW + timedelta(minutes=15))
        row = self.h.event("garmin", 1002)
        self.assertEqual((row["state"], row["feedback_status"], row["feedback_eligible"]), ("new", "pending", 1))
        self.assertEqual(row["activity_type"], "RUNNING")
        self.assertEqual(row["local_date"], "2026-10-20")

    def test_changed_summary_is_recorded_as_edit(self):
        self.baseline()
        self.h.fake.garmin.append(garmin_activity(1002, "2026-10-20", local("2026-10-20", 7)))
        self.h.poll(NOW + timedelta(minutes=15))
        self.h.fake.garmin[-1]["activityName"] = "Renamed Run"
        summary = self.h.poll(NOW + timedelta(minutes=30))
        self.assertEqual(self.h.changes("garmin", 1002), ["new", "edited"])
        self.assertEqual(self.h.event("garmin", 1002)["state"], "edited")
        self.assertEqual(self.h.event("garmin", 1002)["feedback_status"], "pending")
        self.assertEqual(summary["sources"]["garmin"]["changes"].get("edited"), 1)

    def test_missing_activity_needs_repeated_absence_and_is_never_deleted(self):
        self.baseline()
        self.h.fake.garmin = []
        for i in range(1, ep.MISSING_THRESHOLD):
            self.h.poll(NOW + timedelta(minutes=15 * i))
            self.assertNotIn("missing_suspected", self.h.changes("garmin", 1001))
        self.h.poll(NOW + timedelta(minutes=15 * ep.MISSING_THRESHOLD))
        self.assertEqual(self.h.changes("garmin", 1001)[-1], "missing_suspected")
        row = self.h.event("garmin", 1001)
        self.assertIsNotNone(row)
        self.assertNotEqual(row["state"], "deleted")

        self.h.fake.garmin = [garmin_activity(1001, "2026-10-19", local("2026-10-19", 7))]
        self.h.poll(NOW + timedelta(hours=2))
        self.assertEqual(self.h.changes("garmin", 1001)[-1], "reappeared")
        self.assertEqual(self.h.event("garmin", 1001)["missing_count"], 0)

    def test_truncated_response_never_counts_absences(self):
        self.baseline()
        self.h.fake.garmin = []
        self.h.fake.garmin_truncated = True
        for i in range(1, ep.MISSING_THRESHOLD + 2):
            self.h.poll(NOW + timedelta(minutes=15 * i))
        self.assertEqual(self.h.event("garmin", 1001)["missing_count"], 0)

    def test_daily_reconciliation_widens_window_once_per_local_day(self):
        self.baseline()
        self.h.poll(NOW + timedelta(minutes=15))
        self.h.poll(NOW + timedelta(days=1))
        windows = [(a["start_date"], a["end_date"]) for a in self.h.fake.calls_for("garmin_get_activities")]
        self.assertEqual(windows, [("2026-10-14", "2026-10-20"), ("2026-10-18", "2026-10-20"),
                                   ("2026-10-15", "2026-10-21")])

    def test_late_arrivals_are_pending_inside_window_and_backfill_outside(self):
        self.baseline()
        self.h.fake.garmin.append(garmin_activity(1003, "2026-10-18", local("2026-10-18", 7)))
        self.h.fake.garmin.append(garmin_activity(1004, "2026-10-15", local("2026-10-15", 7)))
        self.h.poll(NOW + timedelta(minutes=15))  # 3-day window: 1004 not visible yet
        self.assertEqual(self.h.event("garmin", 1003)["feedback_status"], "pending")
        self.assertIsNone(self.h.event("garmin", 1004))
        self.h.poll(NOW + timedelta(days=1))  # next day's 7-day reconciliation
        self.assertEqual(self.h.event("garmin", 1004)["feedback_status"], "suppressed_backfill")
        self.assertEqual(self.h.event("garmin", 1004)["feedback_eligible"], 0)

    def test_activity_without_any_identifier_is_skipped_and_counted(self):
        self.baseline()
        orphan = garmin_activity(1005, "2026-10-20", local("2026-10-20", 7))
        del orphan["activityId"], orphan["summaryId"]
        self.h.fake.garmin.append(orphan)
        summary = self.h.poll(NOW + timedelta(minutes=15))
        self.assertEqual(summary["sources"]["garmin"]["parsed"]["unidentifiable"], 1)
        self.assertEqual(len(self.h.events("garmin")), 1)


class HevyTests(PollerTestCase):
    def test_new_edit_and_overlap_reread(self):
        self.baseline()
        t1 = NOW + timedelta(minutes=15)
        self.h.fake.hevy.append(hevy_updated("w-1", local("2026-10-20", 9), local("2026-10-20", 10),
                                             t1 - timedelta(minutes=5)))
        self.h.poll(t1)
        self.assertEqual(self.h.event("hevy", "w-1")["feedback_status"], "pending")

        t2 = t1 + timedelta(minutes=15)
        summary = self.h.poll(t2)  # overlap re-reads w-1 unchanged
        self.assertEqual(summary["sources"]["hevy"]["changes"].get("unchanged"), 1)
        self.assertEqual(self.h.changes("hevy", "w-1"), ["new"])

        t3 = t2 + timedelta(minutes=15)
        self.h.fake.hevy[-1] = hevy_updated("w-1", local("2026-10-20", 9), local("2026-10-20", 10),
                                            t3 - timedelta(minutes=5), reps=10)
        self.h.poll(t3)
        self.assertEqual(self.h.changes("hevy", "w-1"), ["new", "edited"])
        self.assertEqual(self.h.event("hevy", "w-1")["state"], "edited")

    def test_older_version_never_overwrites_newer(self):
        self.baseline()
        t1 = NOW + timedelta(minutes=15)
        newer = hevy_updated("w-1", local("2026-10-20", 9), local("2026-10-20", 10), t1, reps=10)
        older = hevy_updated("w-1", local("2026-10-20", 9), local("2026-10-20", 10), t1 - timedelta(minutes=30))
        self.h.fake.hevy.append(newer)
        self.h.poll(t1 + timedelta(minutes=1))
        fingerprint = self.h.event("hevy", "w-1")["fingerprint"]
        self.h.fake.hevy[-1] = older
        summary = self.h.poll(t1 + timedelta(minutes=16))
        self.assertEqual(self.h.event("hevy", "w-1")["fingerprint"], fingerprint)
        self.assertEqual(summary["sources"]["hevy"]["changes"].get("stale_ignored"), 1)

    def test_cursor_advances_to_latest_update_and_queries_with_overlap(self):
        self.baseline()
        base_updated = local("2026-10-19", 19, 30)
        self.assertEqual(ep.parse_iso(self.h.state("hevy")["cursor"]), base_updated)
        self.h.poll(NOW + timedelta(minutes=15))
        since = self.h.fake.calls_for("hevy_get_workout_events")[-1]["since"]
        self.assertEqual(ep.parse_iso(since), base_updated - ep.HEVY_CURSOR_OVERLAP)
        first_since = self.h.fake.calls_for("hevy_get_workout_events")[0]["since"]
        self.assertEqual(ep.parse_iso(first_since), NOW - ep.HEVY_INITIAL_LOOKBACK)

    def test_all_pages_are_read(self):
        start = local("2026-10-19", 6)
        self.h.fake.hevy = [hevy_updated(f"w-{i}", start + timedelta(minutes=i), start + timedelta(minutes=i + 50),
                                         start + timedelta(minutes=i + 60)) for i in range(25)]
        summary = self.h.poll()
        self.assertEqual(len(self.h.events("hevy")), 25)
        self.assertEqual(summary["sources"]["hevy"]["calls"], 3)
        self.assertEqual([a["page"] for a in self.h.fake.calls_for("hevy_get_workout_events")], [1, 2, 3])

    def test_failed_page_writes_nothing_and_keeps_cursor(self):
        self.baseline()
        cursor = self.h.state("hevy")["cursor"]
        start = local("2026-10-20", 6)
        self.h.fake.hevy += [hevy_updated(f"w-{i}", start, start + timedelta(minutes=50),
                                          NOW - timedelta(minutes=i)) for i in range(15)]
        def http_post(url, body, headers, timeout):
            message = json.loads(body)
            if message.get("method") == "tools/call" and message["params"]["arguments"].get("page") == 2:
                return 500, {}, ""
            return FakeAthleteData.__call__(self.h.fake, url, body, headers, timeout)

        client = adc.AthleteDataClient(adc.McpEndpoint(SYNTHETIC_URL), http_post=http_post, sleep_fn=lambda s: None)
        summary = ep.run_poll(self.h.store, client=client, now=NOW + timedelta(minutes=15), tz=TZ, fp=self.h.fp,
                              log=lambda **f: None)
        self.assertEqual(summary["sources"]["hevy"]["outcome"], "failed")
        self.assertEqual(summary["sources"]["hevy"]["error_category"], "http_5xx")
        self.assertEqual(len(self.h.events("hevy")), 1)  # only the baseline workout
        self.assertEqual(self.h.state("hevy")["cursor"], cursor)
        self.assertEqual(self.h.state("hevy")["consecutive_failures"], 1)

    def test_page_limit_is_treated_as_incomplete(self):
        start = local("2026-10-19", 6)
        self.h.fake.hevy = [hevy_updated(f"w-{i}", start, start + timedelta(minutes=50),
                                         start + timedelta(minutes=60 + i)) for i in range(30)]
        with mock.patch.object(ep, "HEVY_MAX_PAGES", 2):
            summary = self.h.poll()
        self.assertEqual(summary["sources"]["hevy"]["error_category"], "incomplete_pagination")
        self.assertEqual(self.h.events("hevy"), [])
        self.assertIsNone(self.h.state("hevy")["cursor"])
        self.assertIsNone(self.h.state("hevy")["baseline_completed_at"])

    def test_delete_event_marks_row_deleted_and_keeps_audit(self):
        self.baseline()
        t1 = NOW + timedelta(minutes=15)
        self.h.fake.hevy.append(hevy_updated("w-1", local("2026-10-20", 9), local("2026-10-20", 10),
                                             t1 - timedelta(minutes=5)))
        self.h.poll(t1)
        self.h.fake.hevy.append(hevy_deleted("w-1", t1 + timedelta(minutes=5)))
        self.h.poll(t1 + timedelta(minutes=15))
        row = self.h.event("hevy", "w-1")
        self.assertEqual((row["state"], row["feedback_status"], row["feedback_eligible"]),
                         ("deleted", "suppressed_deleted", 0))
        self.assertEqual(self.h.changes("hevy", "w-1"), ["new", "deleted"])
        detail = self.h.store.conn.execute(
            "SELECT detail FROM event_changes WHERE event_key = 'w-1' AND change = 'deleted'").fetchone()[0]
        self.assertEqual(detail, "unverified_structure")

    def test_late_logged_workout_is_still_new(self):
        self.baseline()
        t1 = NOW + timedelta(minutes=15)
        # Session two days ago, only logged in Hevy now.
        self.h.fake.hevy.append(hevy_updated("w-late", local("2026-10-18", 18), local("2026-10-18", 19), t1))
        self.h.poll(t1 + timedelta(minutes=1))
        row = self.h.event("hevy", "w-late")
        self.assertEqual((row["local_date"], row["feedback_status"]), ("2026-10-18", "pending"))


class CrossSourceTests(PollerTestCase):
    def _strength(self, activity_id, start, minutes=60):
        return garmin_activity(activity_id, start.date().isoformat(), start, duration=minutes * 60,
                               activity_type="STRENGTH_TRAINING", name="Strength", distance=0.0)

    def test_overlapping_hevy_supersedes_garmin_strength(self):
        self.baseline()
        start = local("2026-10-20", 8)
        t1 = NOW + timedelta(minutes=15)
        self.h.fake.garmin.append(self._strength(2001, start))
        self.h.fake.hevy.append(hevy_updated("w-s", start + timedelta(minutes=3), start + timedelta(minutes=58), t1))
        self.h.poll(t1 + timedelta(minutes=1))
        garmin = self.h.event("garmin", 2001)
        self.assertEqual((garmin["state"], garmin["feedback_status"], garmin["superseded_by"]),
                         ("superseded", "suppressed_superseded", "hevy:w-s"))
        self.assertEqual(self.h.changes("garmin", 2001), ["new", "superseded"])
        self.assertEqual(self.h.event("hevy", "w-s")["feedback_status"], "pending")

    def test_unmatched_strength_is_held_then_released_after_24h(self):
        self.baseline()
        start = local("2026-10-20", 8)
        self.h.fake.garmin.append(self._strength(2002, start))
        t1 = NOW + timedelta(minutes=15)
        self.h.poll(t1)
        row = self.h.event("garmin", 2002)
        self.assertEqual((row["state"], row["feedback_status"], row["feedback_eligible"]), ("held", "held", 0))
        self.h.poll(t1 + timedelta(hours=12))
        self.assertEqual(self.h.event("garmin", 2002)["state"], "held")
        self.h.fake.garmin = [a for a in self.h.fake.garmin]  # still visible in the 3-day window
        self.h.poll(t1 + timedelta(hours=24, minutes=15))
        row = self.h.event("garmin", 2002)
        self.assertEqual((row["state"], row["feedback_status"], row["feedback_eligible"]), ("new", "pending", 1))
        self.assertEqual(self.h.changes("garmin", 2002), ["new", "released"])

    def test_hold_is_not_released_while_hevy_is_failing(self):
        self.baseline()
        self.h.fake.garmin.append(self._strength(2003, local("2026-10-20", 8)))
        t1 = NOW + timedelta(minutes=15)
        self.h.poll(t1)
        self.h.fake.failures["hevy_get_workout_events"] = [503] * 3
        self.h.poll(t1 + timedelta(hours=25))
        self.assertEqual(self.h.event("garmin", 2003)["state"], "held")

    def test_same_day_without_time_overlap_is_never_merged(self):
        self.baseline()
        self.h.fake.garmin.append(self._strength(2004, local("2026-10-20", 7)))
        t1 = NOW + timedelta(minutes=15)
        self.h.fake.hevy.append(hevy_updated("w-pm", local("2026-10-20", 18), local("2026-10-20", 19), t1))
        self.h.poll(t1 + timedelta(minutes=1))
        self.assertEqual(self.h.event("garmin", 2004)["state"], "held")
        self.assertIsNone(self.h.event("garmin", 2004)["superseded_by"])

    def test_hevy_arriving_after_release_still_supersedes_pending_garmin(self):
        self.baseline()
        start = local("2026-10-20", 8)
        self.h.fake.garmin.append(self._strength(2005, start))
        t1 = NOW + timedelta(minutes=15)
        self.h.poll(t1)
        self.h.poll(t1 + timedelta(hours=24, minutes=15))
        self.assertEqual(self.h.event("garmin", 2005)["feedback_status"], "pending")
        t2 = t1 + timedelta(hours=25)
        self.h.fake.hevy.append(hevy_updated("w-very-late", start, start + timedelta(minutes=55), t2))
        self.h.poll(t2 + timedelta(minutes=1))
        self.assertEqual(self.h.event("garmin", 2005)["state"], "superseded")
        self.assertEqual(self.h.changes("garmin", 2005), ["new", "released", "superseded"])


class AppleHealthTests(PollerTestCase):
    VALUES = ("81.37", "81.4", "0.2134", "21.34", "79.9")

    def test_records_are_detected_but_never_feedback_eligible_while_unverified(self):
        self.baseline()
        self.h.fake.apple["weight"].append({"date": "2026-10-20", "value": 81.4, "uuid": "u-2"})
        self.h.fake.apple["bodyFat"].append({"date": "2026-10-20", "value": 0.2134, "uuid": "u-3"})
        self.h.poll(NOW + timedelta(minutes=15))
        new_rows = [r for r in self.h.events("apple_health") if r["state"] == "new"]
        self.assertEqual(len(new_rows), 2)
        for row in new_rows:
            self.assertEqual((row["feedback_status"], row["feedback_eligible"]), ("unverified_structure", 0))
        self.assertEqual({r["event_type"] for r in new_rows}, {"body_weight", "body_fat"})

    def test_duplicates_by_value_or_uuid_collapse_to_one_event(self):
        self.h.fake.apple = {"weight": [
            {"date": "2026-10-19", "value": 81.37, "uuid": "u-a"},
            {"date": "2026-10-19", "value": 81.37, "uuid": "u-b"},  # same reading, different UUID
            {"date": "2026-10-19", "value": 81.37, "uuid": "u-a"},  # repeated UUID
        ], "bodyFat": [{"date": "2026-10-19", "value": 21.34}, {"date": "2026-10-19", "value": 0.2134}]}
        summary = self.h.poll()
        parsed = summary["sources"]["apple_health"]["parsed"]
        self.assertEqual(len(self.h.events("apple_health")), 2)
        self.assertEqual(parsed["duplicate_ids"], 1)
        self.assertEqual(parsed["duplicates"], 2)  # value duplicate + percent/fraction duplicate

    def test_conflicting_same_day_readings_are_flagged_not_merged(self):
        self.h.fake.apple = {"weight": [{"date": "2026-10-19", "value": 81.37, "uuid": "u-a"},
                                        {"date": "2026-10-19", "value": 79.9, "uuid": "u-b"}], "bodyFat": []}
        self.h.poll()
        rows = self.h.events("apple_health")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["conflict"] == 1 for r in rows))
        self.assertEqual(self.h.changes("apple_health").count("conflict_detected"), 2)

    def test_store_never_contains_measurement_values(self):
        self.h.fake.apple = {"weight": [{"date": "2026-10-19", "value": 81.37}, {"date": "2026-10-20", "value": 79.9}],
                             "bodyFat": [{"date": "2026-10-19", "value": 0.2134}]}
        self.h.poll()
        self.h.store.close()
        raw = self.h.db_path.read_bytes()
        dump = "\n".join(sqlite3.connect(self.h.db_path).iterdump())
        self.h.store = es.EventStore.open(self.h.db_path)
        for value in self.VALUES:
            self.assertNotIn(value.encode(), raw)
            self.assertNotIn(value, dump)
        self.assertNotIn("81.37", json.dumps(self.h.logs))

    def test_prose_response_is_no_data_without_baseline(self):
        self.h.fake.apple = None
        summary = self.h.poll()
        state = self.h.state("apple_health")
        self.assertEqual(summary["sources"]["apple_health"]["outcome"], "no_data")
        self.assertEqual((state["health"], state["consecutive_failures"]), ("no_data", 0))
        self.assertIsNone(state["baseline_completed_at"])
        self.assertEqual(self.h.events("apple_health"), [])
        # Data appearing later becomes the baseline, not a flood of new events.
        self.h.fake.apple = {"weight": [{"date": "2026-10-20", "value": 81.37}], "bodyFat": []}
        self.h.poll(NOW + timedelta(minutes=15))
        self.assertEqual(self.h.events("apple_health")[0]["state"], "baseline")

    def test_unknown_structures_are_reported_without_baseline_or_records(self):
        for payload in ({"samples": [{"d": "2026-10-19"}]},
                        {"weight": [{"when": "yesterday", "amount": "heavy"}]},
                        [{"date": "2026-10-19", "value": 81.37}]):
            with self.subTest(payload=str(payload)[:40]):
                self.h.fake.apple = payload
                summary = self.h.poll()
                self.assertEqual(summary["sources"]["apple_health"]["outcome"], "structure_unrecognized")
                self.assertEqual(self.h.state("apple_health")["health"], "structure_unrecognized")
                self.assertIsNone(self.h.state("apple_health")["baseline_completed_at"])
                self.assertEqual(self.h.events("apple_health"), [])

    @staticmethod
    def realistic(day: str, kind: str, value: float, uuid: str) -> dict:
        """Record shape observed from production on 2026-10-22 (synthetic values)."""
        return {"date": day, "startDate": f"{day}T07:12:00.000+0200", "type": kind, "uuid": uuid, "value": value}

    def test_production_first_baseline_then_next_day_reconciliation(self):
        """Mirrors production: the day's reconciliation ran while the source
        returned prose; records then appeared, so the baseline came from the
        4-day window mid-day. The next morning's 14-day reconciliation sees
        older history for the first time -- it must be backfill, never new."""
        days = [f"2026-10-{d:02d}" for d in range(8, 21)]
        history = {"weight": [self.realistic(d, "body_mass", 80.0 + i / 10, f"w-{d}") for i, d in enumerate(days)],
                   "bodyFat": [self.realistic(d, "body_fat", 0.20 + i / 1000, f"f-{d}") for i, d in enumerate(days)]}

        def serve(arguments):
            return {name: [r for r in records if arguments["start_date"] <= r["date"] <= arguments["end_date"]]
                    for name, records in history.items()}
        self.h.fake.apple = None
        self.h.poll(NOW - timedelta(hours=2))  # reconciliation done, no data
        with mock.patch.object(self.h.fake, "_apple_health_get_body_composition",
                               side_effect=lambda a: text_result(serve(a))):
            self.h.poll(NOW)  # first structured response: 4-day window baseline
            baseline = self.h.events("apple_health")
            self.assertEqual({r["state"] for r in baseline}, {"baseline"})
            self.assertEqual(len(baseline), 2 * 4)  # 2026-10-17..20

            # A re-sync of the same records (same uuid, same value) changes nothing.
            self.h.poll(NOW + timedelta(minutes=15))
            self.assertEqual(len(self.h.events("apple_health")), 8)

            # Next local day: 14-day reconciliation plus one genuinely new weigh-in.
            history["weight"].append(self.realistic("2026-10-21", "body_mass", 79.5, "w-new"))
            self.h.poll(NOW + timedelta(days=1))
        rows = self.h.events("apple_health")
        by_status: dict = {}
        for row in rows:
            by_status[row["feedback_status"]] = by_status.get(row["feedback_status"], 0) + 1
        # 2026-10-08..16 first seen now: more than 3 days old -> backfill.
        self.assertEqual(by_status["suppressed_backfill"], 2 * 9)
        self.assertEqual(by_status["suppressed_baseline"], 8)
        # The new weigh-in is detected but stays ineligible while unverified.
        self.assertEqual(by_status["unverified_structure"], 1)
        self.assertNotIn("pending", by_status)
        self.assertEqual(sum(r["feedback_eligible"] for r in rows), 0)

    def test_window_is_four_days_with_fourteen_day_daily_reconciliation(self):
        self.h.fake.apple = {"weight": [], "bodyFat": []}
        self.h.poll()
        self.h.poll(NOW + timedelta(minutes=15))
        windows = [(a["start_date"], a["end_date"]) for a in self.h.fake.calls_for("apple_health_get_body_composition")]
        self.assertEqual(windows, [("2026-10-07", "2026-10-20"), ("2026-10-17", "2026-10-20")])


class FailureHandlingTests(PollerTestCase):
    def test_rate_limit_is_retried_with_bounded_backoff(self):
        self.h.fake.failures["garmin_get_activities"] = [429]
        summary = self.h.poll()
        self.assertEqual(summary["sources"]["garmin"]["outcome"], "ok")
        self.assertEqual(self.h.sleeps, [2.0])
        self.assertEqual(summary["http_retries"], 1)

    def test_failures_are_isolated_per_source_and_counted(self):
        self.baseline()
        for i in range(ep.FAILING_THRESHOLD):
            self.h.fake.failures["garmin_get_activities"] = [429] * adc.MAX_ATTEMPTS
            summary = self.h.poll(NOW + timedelta(minutes=15 * (i + 1)))
            self.assertEqual(summary["outcome"], "partial")
            self.assertEqual(summary["sources"]["hevy"]["outcome"], "ok")
            self.assertEqual(summary["sources"]["garmin"]["error_category"], "rate_limited")
        state = self.h.state("garmin")
        self.assertEqual((state["health"], state["consecutive_failures"], state["last_error_category"]),
                         ("failing", ep.FAILING_THRESHOLD, "rate_limited"))
        self.h.poll(NOW + timedelta(hours=3))
        self.assertEqual((self.h.state("garmin")["health"], self.h.state("garmin")["consecutive_failures"]),
                         ("ok", 0))

    def test_store_failure_mid_apply_rolls_back_the_whole_source(self):
        self.h.fake.garmin = [garmin_activity(1001 + i, "2026-10-19", local("2026-10-19", 7 + i)) for i in range(3)]
        original = self.h.store.record_change
        calls = {"n": 0}

        def fail_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("synthetic disk I/O error")
            return original(*args, **kwargs)

        with mock.patch.object(self.h.store, "record_change", side_effect=fail_on_second):
            summary = self.h.poll()
        self.assertEqual(summary["sources"]["garmin"]["error_category"], "store_error")
        self.assertEqual(self.h.events("garmin"), [])
        self.assertIsNone(self.h.state("garmin")["baseline_completed_at"])
        self.assertEqual(self.h.state("garmin")["consecutive_failures"], 1)
        self.assertEqual(self.h.store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_setup_failure_marks_every_source_failed(self):
        summary = ep.run_poll(self.h.store, client=None,
                              client_error=adc.CredentialUnavailableError("synthetic"), now=NOW, tz=TZ,
                              fp=self.h.fp, log=lambda **f: self.h.logs.append(f))
        self.assertEqual(summary["outcome"], "failed")
        for source in ep.SOURCES:
            self.assertEqual(self.h.state(source)["last_error_category"], "credential_unavailable")

    def test_only_allowlisted_tools_are_ever_called(self):
        self.baseline()
        self.h.poll(NOW + timedelta(minutes=15))
        self.assertTrue({name for name, _ in self.h.fake.calls} <= adc.READ_ONLY_TOOL_ALLOWLIST)


class StoreTests(unittest.TestCase):
    def test_unique_source_key_constraint(self):
        store = es.EventStore(sqlite3.connect(":memory:", isolation_level=None))
        row = dict(source="garmin", event_key="1", event_type="garmin_activity", fingerprint="f", state="new",
                   feedback_status="pending", first_seen_at="t", last_seen_at="t")
        store.insert_event(**row)
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert_event(**row)

    def test_invalid_state_is_rejected_by_schema(self):
        store = es.EventStore(sqlite3.connect(":memory:", isolation_level=None))
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert_event(source="garmin", event_key="1", event_type="x", fingerprint="f", state="bogus",
                               feedback_status="pending", first_seen_at="t", last_seen_at="t")

    def test_fingerprint_key_is_created_owner_only_and_reloaded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "event-poller.key"
            self.assertIsNone(es.load_fingerprint_key(path, create=False))
            key = es.load_fingerprint_key(path, create=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(es.load_fingerprint_key(path, create=False), key)
            os.chmod(path, 0o644)
            with self.assertRaises(es.StoreError):
                es.load_fingerprint_key(path, create=False)

    def test_fingerprints_depend_on_the_key(self):
        a, b = es.Fingerprinter(b"a" * 32), es.Fingerprinter(b"b" * 32)
        self.assertNotEqual(a.digest({"v": 81.37}), b.digest({"v": 81.37}))
        self.assertEqual(a.digest({"x": 1, "y": 2}), a.digest({"y": 2, "x": 1}))


class CliTests(unittest.TestCase):
    """Drives main() with a synthetic repo, config and credential file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "runtime" / "locks").mkdir(parents=True)
        self.env_file = self.repo / "config.env"
        self.env_file.write_text("ATHLETE_TIMEZONE=Europe/Amsterdam\n", encoding="utf-8")
        self.claude_config = Path(self._tmp.name) / ".claude.json"
        self.claude_config.write_text(json.dumps({"projects": {str(self.repo.resolve()): {"mcpServers": {
            "athletedata": {"type": "http", "url": SYNTHETIC_URL}}}}}), encoding="utf-8")
        os.chmod(self.claude_config, 0o600)
        self.fake = FakeAthleteData()
        self.fake.garmin = [garmin_activity(1001, "2026-10-19", local("2026-10-19", 7))]
        self.db = self.repo / "runtime" / "events.sqlite3"
        self.key = self.repo / "runtime" / "event-poller.key"

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(adc, "_default_http_post", self.fake), \
             mock.patch.dict(os.environ, {}, clear=False), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            os.environ.pop("ATHLETE_TIMEZONE", None)
            code = ep.main([*flags, "--repo-root", str(self.repo), "--env-file", str(self.env_file),
                            "--claude-config", str(self.claude_config)], now_fn=lambda: NOW)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_once_persists_state_and_never_exposes_credentials(self):
        code, out, err = self.run_main("--once")
        self.assertEqual(code, 0)
        self.assertTrue(self.db.exists())
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        for text in (out, err, self.db.read_bytes().decode("latin-1")):
            self.assertNotIn(SYNTHETIC_KEY, text)
            self.assertNotIn("mcp.example.test", text)
        self.assertFalse((self.repo / "runtime" / "locks" / "event-poller.lock").exists())

    def test_dry_run_never_modifies_persistent_state(self):
        self.run_main("--once")
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        key_digest = hashlib.sha256(self.key.read_bytes()).hexdigest()
        self.fake.garmin.append(garmin_activity(1002, "2026-10-20", local("2026-10-20", 7)))
        code, out, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(json.loads(out)["sources"]["garmin"]["changes"].get("new"), 1)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256(self.key.read_bytes()).hexdigest(), key_digest)
        self.assertEqual(sorted(p.name for p in (self.repo / "runtime").iterdir()),
                         ["event-poller.key", "events.sqlite3", "locks"])

    def test_dry_run_without_existing_state_creates_nothing(self):
        code, out, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sources"]["garmin"]["changes"], {"baseline": 1,
                                                                            "feedback_suppressed_baseline": 1})
        self.assertFalse(self.db.exists())
        self.assertFalse(self.key.exists())

    def test_missing_key_for_existing_store_is_refused(self):
        self.run_main("--once")
        self.key.unlink()
        for flag in ("--once", "--dry-run"):
            code, out, _ = self.run_main(flag)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["error_category"], "store_error")
        self.assertFalse(self.key.exists())

    def test_concurrent_run_is_skipped_while_lock_is_held(self):
        held = ep.WorkflowLock(self.repo / "runtime" / "locks", "event-poller", ep.LOCK_STALE_SECONDS)
        held.acquire()
        try:
            code, _, err = self.run_main("--once")
        finally:
            held.release()
        self.assertEqual(code, 0)
        self.assertIn("lock_held", err)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.fake.requests, [])

    def test_status_reports_counts_without_identifiers(self):
        self.run_main("--once")
        code, out, _ = self.run_main("--status")
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertEqual(status["sources"]["garmin"]["events_by_state"], {"baseline": 1})
        self.assertNotIn("1001", out)
        self.assertNotIn("cursor\":", out.replace("cursor_set", ""))

    def test_credential_problem_fails_cleanly_without_leaking(self):
        os.chmod(self.claude_config, 0o644)
        code, out, err = self.run_main("--once")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["sources"]["garmin"]["error_category"], "credential_unavailable")
        self.assertNotIn(SYNTHETIC_KEY, out + err)
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
