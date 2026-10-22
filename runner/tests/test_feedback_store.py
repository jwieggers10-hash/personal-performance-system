"""Schema v2 migration tests (runner/feedback_store.py).

Runs against a store built by the unchanged v1 code path (event_store.py),
populated like production: baseline rows, a Hevy cursor, pending events,
audit rows and poll runs. Everything is synthetic.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_store as es  # noqa: E402
import feedback_dispatcher as fd  # noqa: E402
import feedback_store as fs  # noqa: E402
from feedback_fixtures import NOW, add_event, iso, make_v1_store  # noqa: E402


def populate_like_production(store: es.EventStore) -> None:
    for i in range(3):
        add_event(store, f"g-base-{i}", state="baseline", feedback_status="suppressed_baseline", eligible=0,
                  start=NOW - timedelta(days=3 + i))
    add_event(store, "g-new", start=NOW - timedelta(hours=5))
    add_event(store, "h-new", source="hevy", activity_type=None, event_type="workout",
              start=NOW - timedelta(hours=30))
    with store.transaction():
        store.record_change("garmin", "g-new", "new", iso(NOW), "pending")
        store.record_poll_run(iso(NOW), iso(NOW), "ok", {"sources": {"garmin": {"new": 1}}})


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.sqlite3"
        self.store = make_v1_store(self.path)
        populate_like_production(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_v1_store_is_untouched_until_explicit_migration(self):
        self.store.close()
        reopened = es.EventStore.open(self.path)  # what the poller does every tick
        self.assertEqual(fs.schema_version(reopened.conn), 1)
        self.assertNotIn("feedback_batches", fs._tables(reopened.conn))
        self.store = reopened

    def test_migration_preserves_every_v1_row(self):
        before = fs.v1_snapshot(self.store.conn)
        cursor_before = self.store.get_source_state("hevy")["cursor"]
        result = fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        self.assertEqual(result, {"from_version": 1, "to_version": 2, "applied": True})
        self.assertEqual(fs.v1_snapshot(self.store.conn), before)
        self.assertEqual(self.store.get_source_state("hevy")["cursor"], cursor_before)
        self.assertEqual(fs.verify_v2(self.store.conn), [])

    def test_migration_is_idempotent(self):
        fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        snapshot = fs.v1_snapshot(self.store.conn)
        again = fs.migrate_to_v2(self.store.conn, now_text=iso(NOW + timedelta(hours=1)))
        self.assertFalse(again["applied"])
        self.assertEqual(fs.v1_snapshot(self.store.conn), snapshot)
        stamp = self.store.conn.execute("SELECT value FROM meta WHERE key = 'schema_v2_migrated_at'").fetchone()[0]
        self.assertEqual(stamp, iso(NOW))

    def test_failed_migration_rolls_back_completely(self):
        before = fs.v1_snapshot(self.store.conn)
        statements = fs.SCHEMA_V2_STATEMENTS + ("CREATE TABLE broken (",)
        with mock.patch.object(fs, "SCHEMA_V2_STATEMENTS", statements):
            with self.assertRaises(sqlite3.Error):
                fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        self.assertEqual(fs.schema_version(self.store.conn), 1)
        self.assertFalse(set(fs.V2_TABLES) & fs._tables(self.store.conn))
        self.assertEqual(fs.v1_snapshot(self.store.conn), before)
        self.assertFalse(self.store.conn.in_transaction)

    def test_unknown_version_refused(self):
        self.store.conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        with self.assertRaises(fs.MigrationError):
            fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))

    def test_partial_v2_tables_in_v1_store_refused(self):
        self.store.conn.execute("CREATE TABLE feedback_items (id INTEGER)")
        with self.assertRaises(fs.MigrationError):
            fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        self.assertEqual(fs.schema_version(self.store.conn), 1)

    def test_refuses_inside_open_transaction(self):
        self.store.conn.execute("BEGIN")
        try:
            with self.assertRaises(fs.MigrationError):
                fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        finally:
            self.store.conn.execute("ROLLBACK")

    def test_poller_store_operates_on_v2(self):
        fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        self.store.close()
        self.store = es.EventStore.open(self.path)
        with self.store.transaction():
            self.store.update_event("garmin", "g-new", state="edited")
        self.assertEqual(self.store.status_summary()["schema_version"], 2)
        self.assertEqual(fs.integrity_problems(self.store.conn), [])

    def test_feedback_store_requires_v2(self):
        with self.assertRaises(fs.MigrationError):
            fs.FeedbackStore(self.store)

    def test_unique_event_prevents_second_item(self):
        fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        store = fs.FeedbackStore(self.store)
        event_id = self.store.get_event("garmin", "g-new")["id"]
        self.assertIsNotNone(store.create_item(event_id, state="queued", now_text=iso(NOW)))
        self.assertIsNone(store.create_item(event_id, state="queued", now_text=iso(NOW)))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("INSERT INTO feedback_items (event_id, state, created_at, updated_at) "
                                    "VALUES (?, 'queued', 'x', 'x')", (event_id,))

    def test_state_vocabulary_enforced(self):
        fs.migrate_to_v2(self.store.conn, now_text=iso(NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "INSERT INTO feedback_batches (batch_key, kind, state, stage, release_at, created_at, updated_at) "
                "VALUES ('k', 'event', 'sent', 'generate', 'x', 'x', 'x')")


class MigrationCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "runtime").mkdir()
        self.path = self.root / "runtime" / "events.sqlite3"
        store = make_v1_store(self.path)
        populate_like_production(store)
        store.close()

    def tearDown(self):
        self._tmp.cleanup()

    def digest(self):
        return self.path.read_bytes()

    def test_migrate_check_never_writes(self):
        before = self.digest()
        report = fd.migrate_check(self.path, NOW)
        self.assertTrue(report["applied"])
        self.assertFalse(report["rerun_applied"])
        self.assertTrue(report["v1_rows_preserved"])
        self.assertEqual(report["verification_problems"], [])
        self.assertEqual(self.digest(), before)

    def test_migrate_apply_backs_up_then_migrates(self):
        report = fd.migrate_apply(self.path, self.root / "runtime", NOW)
        self.assertTrue(report["applied"])
        backup = self.root / report["backup"]
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        conn = sqlite3.connect(str(backup))
        self.assertEqual(fs.schema_version(conn), 1)
        conn.close()
        conn = sqlite3.connect(str(self.path))
        self.assertEqual(fs.schema_version(conn), 2)
        conn.close()
        again = fd.migrate_apply(self.path, self.root / "runtime", NOW)
        self.assertFalse(again["applied"])
        self.assertIsNone(again["backup"])

    def test_migrate_apply_refused_while_poller_runs(self):
        lock_dir = self.root / "runtime" / "locks"
        lock_dir.mkdir()
        (lock_dir / "event-poller.lock").write_text(
            '{"pid": %d, "acquired_at": "%s"}' % (os.getpid(), iso(NOW.now(NOW.tzinfo))))
        before = self.digest()
        with self.assertRaises(fd.wr.DuplicateExecutionError):
            fd.migrate_apply(self.path, self.root / "runtime", NOW)
        self.assertEqual(self.digest(), before)


if __name__ == "__main__":
    unittest.main()
