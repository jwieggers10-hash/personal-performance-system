#!/usr/bin/env python3
"""Schema v2 of the event store, and the feedback dispatcher's store access.

Version 2 is purely additive: it creates the dispatcher's own tables next to
the v1 tables written by the detect-only poller, and bumps meta.schema_version.
No v1 table, column, row, fingerprint, cursor or baseline is touched, so the
poller keeps working unchanged on a migrated store (event_store.py accepts
both versions) and a v1 backup restores the pre-migration state exactly.

Like event_store.py, nothing here stores workout contents, measurements or
credentials. The one exception is the outbox: a generated feedback message
is kept in feedback_batches.message_text only until the batch reaches a
terminal state, then cleared (its SHA-256 and length stay for auditing).

Specification: runner/feedback-dispatcher.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Optional

import event_store as es


TARGET_SCHEMA_VERSION = 2

FEEDBACK_STATES = ("queued", "processing", "delivered", "failed", "expired", "suppressed")
TERMINAL_STATES = ("delivered", "failed", "expired", "suppressed")
BATCH_KINDS = ("event", "morning_digest", "system_alert")
BATCH_STAGES = ("generate", "deliver")
FEEDBACK_LEVELS = ("routine", "significant")
URGENCIES = ("routine", "attention", "urgent")

_STATE_CHECK = ", ".join(f"'{s}'" for s in FEEDBACK_STATES)

# One statement per entry: executescript() would commit implicitly and break
# the single-transaction guarantee of migrate_to_v2().
SCHEMA_V2_STATEMENTS = (
    f"""
    CREATE TABLE feedback_batches (
        id                    INTEGER PRIMARY KEY,
        batch_key             TEXT NOT NULL UNIQUE,
        kind                  TEXT NOT NULL CHECK (kind IN ('event', 'morning_digest', 'system_alert')),
        urgency               TEXT NOT NULL DEFAULT 'routine' CHECK (urgency IN ('routine', 'attention', 'urgent')),
        level                 TEXT CHECK (level IS NULL OR level IN ('routine', 'significant')),
        state                 TEXT NOT NULL CHECK (state IN ({_STATE_CHECK})),
        stage                 TEXT NOT NULL CHECK (stage IN ('generate', 'deliver')),
        local_date            TEXT,
        release_at            TEXT NOT NULL,
        next_attempt_at       TEXT,
        generation_attempts   INTEGER NOT NULL DEFAULT 0 CHECK (generation_attempts >= 0),
        delivery_attempts     INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
        last_attempt_at       TEXT,
        processing_owner      TEXT,
        processing_started_at TEXT,
        generated_at          TEXT,
        delivered_at          TEXT,
        parts_total           INTEGER,
        parts_sent            INTEGER NOT NULL DEFAULT 0 CHECK (parts_sent >= 0),
        inflight_part         INTEGER,
        telegram_message_ids  TEXT,
        message_text          TEXT,
        message_sha256        TEXT,
        message_length        INTEGER,
        error_category        TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX feedback_batches_by_state ON feedback_batches (state, stage, release_at)",
    f"""
    CREATE TABLE feedback_items (
        id          INTEGER PRIMARY KEY,
        event_id    INTEGER NOT NULL UNIQUE REFERENCES events (id),
        batch_id    INTEGER REFERENCES feedback_batches (id),
        state       TEXT NOT NULL CHECK (state IN ({_STATE_CHECK})),
        level       TEXT CHECK (level IS NULL OR level IN ('routine', 'significant')),
        reason      TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        delivered_at TEXT
    )
    """,
    "CREATE INDEX feedback_items_by_batch ON feedback_items (batch_id)",
    """
    CREATE TABLE claude_calls (
        id          INTEGER PRIMARY KEY,
        batch_id    INTEGER REFERENCES feedback_batches (id),
        local_date  TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        outcome     TEXT NOT NULL
    )
    """,
    "CREATE INDEX claude_calls_by_date ON claude_calls (local_date)",
    """
    CREATE TABLE dispatch_runs (
        id          INTEGER PRIMARY KEY,
        started_at  TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        mode        TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        summary     TEXT NOT NULL
    )
    """,
)

V2_TABLES = ("feedback_batches", "feedback_items", "claude_calls", "dispatch_runs")
V1_TABLES = ("meta", "source_state", "events", "event_changes", "poll_runs")


class MigrationError(es.StoreError):
    category = "migration_error"


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def schema_version(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None


def _tables(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def integrity_problems(conn: sqlite3.Connection) -> list:
    problems = [row[0] for row in conn.execute("PRAGMA integrity_check") if row[0] != "ok"]
    problems += [f"foreign_key_violation:{row[0]}" for row in conn.execute("PRAGMA foreign_key_check")]
    return problems


def verify_v2(conn: sqlite3.Connection) -> list:
    problems = []
    if schema_version(conn) != TARGET_SCHEMA_VERSION:
        problems.append("schema_version_not_2")
    missing = (set(V1_TABLES) | set(V2_TABLES)) - _tables(conn)
    problems += [f"missing_table:{name}" for name in sorted(missing)]
    return problems + integrity_problems(conn)


def migrate_to_v2(conn: sqlite3.Connection, *, now_text: str) -> dict:
    """Upgrades a v1 store to v2 in one IMMEDIATE transaction. Idempotent: a
    v2 store is verified and left untouched. Any failure rolls back to the
    untouched v1 state. Refuses stores that fail integrity checks or carry a
    version it does not know."""
    if conn.in_transaction:
        raise MigrationError("migration must not run inside an open transaction")
    pre = integrity_problems(conn)
    if pre:
        raise MigrationError("integrity check failed before migration")

    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)
        if version == TARGET_SCHEMA_VERSION:
            conn.execute("ROLLBACK")
            problems = verify_v2(conn)
            if problems:
                raise MigrationError("store reports version 2 but fails verification")
            return {"from_version": 2, "to_version": 2, "applied": False}
        if version != 1:
            raise MigrationError(f"unsupported schema version {version}")
        missing_v1 = set(V1_TABLES) - _tables(conn)
        if missing_v1:
            raise MigrationError("v1 tables missing")
        clash = set(V2_TABLES) & _tables(conn)
        if clash:
            raise MigrationError("v2 tables already exist in a v1 store")
        for statement in SCHEMA_V2_STATEMENTS:
            conn.execute(statement)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(TARGET_SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_v2_migrated_at', ?)", (now_text,))
        problems = verify_v2(conn)
        if problems:
            raise MigrationError("post-migration verification failed")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    post = integrity_problems(conn)
    if post:
        raise MigrationError("integrity check failed after migration")
    return {"from_version": 1, "to_version": 2, "applied": True}


def downgrade_to_v1(conn: sqlite3.Connection) -> dict:
    """Rollback without data loss: drops only the v2 tables and restores
    schema_version 1, keeping every detection made since the migration.
    One IMMEDIATE transaction; a v1 store is left untouched."""
    if conn.in_transaction:
        raise MigrationError("downgrade must not run inside an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)
        if version == 1:
            conn.execute("ROLLBACK")
            return {"from_version": 1, "to_version": 1, "applied": False}
        if version != TARGET_SCHEMA_VERSION:
            raise MigrationError(f"unsupported schema version {version}")
        # Children first (foreign keys).
        for table in ("claude_calls", "feedback_items", "dispatch_runs", "feedback_batches"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.execute("DELETE FROM meta WHERE key = 'schema_v2_migrated_at' OR key = 'feedback_activated_at' "
                     "OR key LIKE 'dispatcher.%'")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    if integrity_problems(conn):
        raise MigrationError("integrity check failed after downgrade")
    return {"from_version": 2, "to_version": 1, "applied": True}


def backup_database(source: sqlite3.Connection, destination_path) -> None:
    """Online, consistent copy via the SQLite backup API (owner-only file)."""
    fd = os.open(str(destination_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    target = sqlite3.connect(str(destination_path))
    try:
        source.backup(target)
    finally:
        target.close()


# --------------------------------------------------------------------------
# Dispatcher store access
# --------------------------------------------------------------------------

def message_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# When an event became feedback-eligible: its release from a strength hold
# (event_changes 'released'), otherwise when the poller first saw it.
ELIGIBLE_SINCE_SQL = """COALESCE((SELECT MAX(c.detected_at) FROM event_changes c
                                  WHERE c.source = e.source AND c.event_key = e.event_key
                                    AND c.change = 'released'), e.first_seen_at) AS eligible_since"""


class FeedbackStore:
    """Thin layer over an EventStore connection holding a v2 schema. Callers
    wrap multi-statement changes in store.transaction()."""

    def __init__(self, store: es.EventStore):
        version = schema_version(store.conn)
        if version != TARGET_SCHEMA_VERSION:
            raise MigrationError(f"feedback dispatch requires schema version 2 (found {version})")
        self.store = store
        self.conn = store.conn

    def transaction(self):
        return self.store.transaction()

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- events -----------------------------------------------------------

    def candidate_events(self) -> list:
        """Events the poller marked feedback-eligible that have no feedback
        item yet. Final eligibility (staleness, activation, Apple Health) is
        decided by the policy layer."""
        return self.conn.execute(
            f"""SELECT e.*, {ELIGIBLE_SINCE_SQL} FROM events e
               LEFT JOIN feedback_items f ON f.event_id = e.id
               WHERE f.id IS NULL AND e.feedback_eligible = 1 AND e.feedback_status = 'pending'
                 AND e.state IN ('new', 'edited')
               ORDER BY e.id"""
        ).fetchall()

    def ineligible_event_counts(self) -> dict:
        counts: dict = {}
        for row in self.conn.execute(
            """SELECT e.feedback_status AS status, COUNT(*) AS n FROM events e
               LEFT JOIN feedback_items f ON f.event_id = e.id
               WHERE f.id IS NULL AND NOT (e.feedback_eligible = 1 AND e.feedback_status = 'pending'
                                           AND e.state IN ('new', 'edited'))
               GROUP BY 1"""
        ):
            counts[row["status"]] = row["n"]
        return counts

    def event_by_id(self, event_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    def training_events_between(self, start_text: str, end_text: str) -> list:
        """Non-deleted, non-superseded Garmin/Hevy events (any feedback status,
        baseline included) whose start falls in [start, end)."""
        return self.conn.execute(
            """SELECT * FROM events WHERE source IN ('garmin', 'hevy') AND state NOT IN ('deleted', 'superseded')
               AND start_time_utc IS NOT NULL AND start_time_utc >= ? AND start_time_utc < ?""",
            (start_text, end_text),
        ).fetchall()

    def baseline_completed_at(self, source: str) -> Optional[str]:
        row = self.conn.execute("SELECT baseline_completed_at FROM source_state WHERE source = ?",
                                (source,)).fetchone()
        return row["baseline_completed_at"] if row else None

    # -- items ------------------------------------------------------------

    def create_item(self, event_id: int, *, state: str, now_text: str, batch_id: Optional[int] = None,
                    level: Optional[str] = None, reason: Optional[str] = None) -> Optional[int]:
        """INSERT OR IGNORE on the UNIQUE event_id: a second dispatcher (or a
        rerun) can never create a second item -- and so never a second
        feedback -- for the same event. Returns None when one already exists."""
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO feedback_items (event_id, batch_id, state, level, reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, batch_id, state, level, reason, now_text, now_text),
        )
        return cursor.lastrowid if cursor.rowcount else None

    def batch_items(self, batch_id: int, *, active_only: bool = True) -> list:
        clause = "AND f.state NOT IN ('delivered', 'failed', 'expired', 'suppressed')" if active_only else ""
        return self.conn.execute(
            f"""SELECT f.id AS item_id, f.state AS item_state, f.reason AS item_reason, e.*, {ELIGIBLE_SINCE_SQL}
                FROM feedback_items f JOIN events e ON e.id = f.event_id
                WHERE f.batch_id = ? {clause} ORDER BY e.start_time_utc, e.id""",
            (batch_id,),
        ).fetchall()

    def set_item_state(self, item_id: int, state: str, now_text: str, *, reason: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE feedback_items SET state = ?, reason = COALESCE(?, reason), updated_at = ?, "
            "delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END WHERE id = ?",
            (state, reason, now_text, state, now_text, item_id),
        )

    def set_active_items_state(self, batch_id: int, state: str, now_text: str, *,
                               reason: Optional[str] = None, level: Optional[str] = None) -> None:
        self.conn.execute(
            """UPDATE feedback_items SET state = ?, reason = COALESCE(?, reason), level = COALESCE(?, level),
                   updated_at = ?, delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END
               WHERE batch_id = ? AND state NOT IN ('delivered', 'failed', 'expired', 'suppressed')""",
            (state, reason, level, now_text, state, now_text, batch_id),
        )

    # -- batches ----------------------------------------------------------

    def get_batch(self, batch_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM feedback_batches WHERE id = ?", (batch_id,)).fetchone()

    def get_batch_by_key(self, batch_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM feedback_batches WHERE batch_key = ?", (batch_key,)).fetchone()

    def create_batch(self, *, batch_key: str, kind: str, release_at: str, now_text: str,
                     local_date: Optional[str] = None, urgency: str = "routine", level: Optional[str] = None,
                     stage: str = "generate", message_text: Optional[str] = None) -> Optional[int]:
        """Idempotent on batch_key: returns None if that key already exists."""
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO feedback_batches
                   (batch_key, kind, urgency, level, state, stage, local_date, release_at, message_text,
                    message_sha256, message_length, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_key, kind, urgency, level, stage, local_date, release_at, message_text,
             message_digest(message_text) if message_text else None,
             len(message_text) if message_text else None, now_text, now_text),
        )
        return cursor.lastrowid if cursor.rowcount else None

    def due_batches(self, now_text: str) -> list:
        return self.conn.execute(
            """SELECT * FROM feedback_batches WHERE state = 'queued' AND release_at <= ?
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY CASE urgency WHEN 'urgent' THEN 0 WHEN 'attention' THEN 1 ELSE 2 END, release_at, id""",
            (now_text, now_text),
        ).fetchall()

    def queued_batches(self) -> list:
        return self.conn.execute("SELECT * FROM feedback_batches WHERE state = 'queued' ORDER BY id").fetchall()

    def stale_processing_batches(self, cutoff_text: str) -> list:
        return self.conn.execute(
            "SELECT * FROM feedback_batches WHERE state = 'processing' AND processing_started_at < ?",
            (cutoff_text,),
        ).fetchall()

    def claim_batch(self, batch_id: int, *, stage: str, owner: str, now_text: str) -> bool:
        """Compare-and-set queued -> processing. Exactly one concurrent
        dispatcher can win; the loser sees rowcount 0 and moves on."""
        cursor = self.conn.execute(
            """UPDATE feedback_batches SET state = 'processing', processing_owner = ?, processing_started_at = ?,
                   last_attempt_at = ?, updated_at = ?
               WHERE id = ? AND state = 'queued' AND stage = ?""",
            (owner, now_text, now_text, now_text, batch_id, stage),
        )
        if cursor.rowcount != 1:
            return False
        self.set_active_items_state(batch_id, "processing", now_text)
        return True

    def update_batch(self, batch_id: int, now_text: str, **fields: Any) -> None:
        allowed = {
            "state", "stage", "level", "next_attempt_at", "generation_attempts", "delivery_attempts",
            "processing_owner", "processing_started_at", "generated_at", "delivered_at", "parts_total",
            "parts_sent", "inflight_part", "telegram_message_ids", "error_category", "release_at", "urgency",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise es.StoreError(f"unknown feedback_batches fields: {sorted(unknown)}")
        fields["updated_at"] = now_text
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE feedback_batches SET {assignments} WHERE id = ?", (*fields.values(), batch_id))

    def set_message(self, batch_id: int, text: Optional[str], now_text: str) -> None:
        self.conn.execute(
            "UPDATE feedback_batches SET message_text = ?, message_sha256 = ?, message_length = ?, updated_at = ? "
            "WHERE id = ?",
            (text, message_digest(text) if text else None, len(text) if text else None, now_text, batch_id),
        )

    def clear_message_text(self, batch_id: int, now_text: str) -> None:
        """Terminal states keep only the digest and length of the message."""
        self.conn.execute("UPDATE feedback_batches SET message_text = NULL, updated_at = ? WHERE id = ?",
                          (now_text, batch_id))

    def finish_batch(self, batch_id: int, state: str, now_text: str, *, error_category: Optional[str] = None,
                     item_reason: Optional[str] = None) -> None:
        if state not in TERMINAL_STATES:
            raise es.StoreError(f"not a terminal state: {state}")
        fields: dict = {"state": state, "processing_owner": None, "processing_started_at": None,
                        "next_attempt_at": None, "inflight_part": None}
        if error_category is not None:
            fields["error_category"] = error_category
        if state == "delivered":
            fields["delivered_at"] = now_text
        self.update_batch(batch_id, now_text, **fields)
        self.set_active_items_state(batch_id, state, now_text, reason=item_reason)
        self.clear_message_text(batch_id, now_text)

    def requeue_batch(self, batch_id: int, now_text: str, **fields: Any) -> None:
        fields.update(state="queued", processing_owner=None, processing_started_at=None, inflight_part=None)
        self.update_batch(batch_id, now_text, **fields)
        self.set_active_items_state(batch_id, "queued", now_text)

    def requeue_failed_delivery(self, batch_id: int, now_text: str, retry_safe_categories: tuple) -> bool:
        """Operator retry of a delivery that failed definitively (Telegram
        confirmed it was not delivered). Never applies to an ambiguous
        outcome. The message text is gone once terminal, so the batch goes
        back to generation; its items are reattached."""
        batch = self.get_batch(batch_id)
        if batch is None or batch["state"] != "failed" or batch["error_category"] not in retry_safe_categories \
                or batch["parts_sent"]:
            return False
        self.update_batch(batch_id, now_text, state="queued", stage="generate", delivery_attempts=0,
                          generation_attempts=0, next_attempt_at=None, error_category=None, parts_total=None,
                          telegram_message_ids=None, inflight_part=None)
        self.conn.execute(
            "UPDATE feedback_items SET state = 'queued', reason = NULL, updated_at = ? "
            "WHERE batch_id = ? AND state = 'failed'", (now_text, batch_id))
        return True

    # -- Claude budget ----------------------------------------------------

    def record_claude_call(self, *, batch_id: Optional[int], local_date: str, started_at: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO claude_calls (batch_id, local_date, started_at, outcome) VALUES (?, ?, ?, 'started')",
            (batch_id, local_date, started_at),
        )
        return cursor.lastrowid

    def finish_claude_call(self, call_id: int, *, outcome: str, finished_at: str) -> None:
        self.conn.execute("UPDATE claude_calls SET outcome = ?, finished_at = ? WHERE id = ?",
                          (outcome, finished_at, call_id))

    def claude_calls_on(self, local_date: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM claude_calls WHERE local_date = ?",
                                 (local_date,)).fetchone()[0]

    # -- runs and reporting -----------------------------------------------

    def record_dispatch_run(self, *, started_at: str, finished_at: str, mode: str, outcome: str,
                            summary: dict) -> None:
        self.conn.execute(
            "INSERT INTO dispatch_runs (started_at, finished_at, mode, outcome, summary) VALUES (?, ?, ?, ?, ?)",
            (started_at, finished_at, mode, outcome, json.dumps(summary, sort_keys=True)),
        )

    def status_summary(self, local_date: str) -> dict:
        def grouped(sql: str) -> dict:
            out: dict = {}
            for row in self.conn.execute(sql):
                out.setdefault(row[0], {})[row[1]] = row[2]
            return out

        last_run = self.conn.execute(
            "SELECT started_at, finished_at, mode, outcome FROM dispatch_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "schema_version": schema_version(self.conn),
            "feedback_activated_at": self.get_meta("feedback_activated_at"),
            "batches_by_kind_and_state": grouped(
                "SELECT kind, state, COUNT(*) FROM feedback_batches GROUP BY 1, 2"),
            "items_by_state_and_reason": grouped(
                "SELECT state, COALESCE(reason, '-'), COUNT(*) FROM feedback_items GROUP BY 1, 2"),
            "claude_calls_today": self.claude_calls_on(local_date),
            "auth_consecutive_failures": int(self.get_meta("dispatcher.auth_consecutive_failures") or 0),
            "auth_escalated_at": self.get_meta("dispatcher.auth_escalated_at"),
            "last_dispatch_run": dict(last_run) if last_run else None,
        }


# meta keys the migration itself writes; everything else in v1 must be identical.
_MIGRATION_META_KEYS = ("schema_version", "schema_v2_migrated_at", "feedback_activated_at")


def v1_snapshot(conn: sqlite3.Connection) -> dict:
    """Every v1 row, for proving a migration preserved them."""
    snapshot = {}
    for table in V1_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        if table == "meta":
            rows = [r for r in rows if r[0] not in _MIGRATION_META_KEYS and not r[0].startswith("dispatcher.")]
        snapshot[table] = sorted(tuple(r) for r in rows)
    return snapshot
