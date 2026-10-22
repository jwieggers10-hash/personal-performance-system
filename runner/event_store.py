#!/usr/bin/env python3
"""SQLite-backed state for the detect-only event poller.

Stores identifiers, dates, keyed fingerprints and fixed-vocabulary states
only -- never workout contents, body-weight or body-fat values, or
credentials. Fingerprints and Apple Health identity keys are HMAC-SHA256
digests under a per-installation key kept in a separate owner-only file, so
the low-entropy values behind them cannot be recovered from the database by
brute force alone. Schema documented in runner/event-poller.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA_VERSION = 1
# Versions this module (and therefore the detect-only poller) can operate on.
# Version 2 only adds the feedback dispatcher's own tables (feedback_store.py);
# every v1 table and column is unchanged, so the poller reads and writes a v2
# store exactly as before. This module never upgrades a store itself: the v1 ->
# v2 migration is an explicit, separate step (feedback_dispatcher.py --migrate).
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    source                   TEXT PRIMARY KEY,
    baseline_completed_at    TEXT,
    cursor                   TEXT,
    last_attempt_at          TEXT,
    last_success_at          TEXT,
    last_reconciliation_date TEXT,
    health                   TEXT NOT NULL DEFAULT 'unknown',
    last_error_category      TEXT,
    consecutive_failures     INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    event_key         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    activity_type     TEXT,
    local_date        TEXT,
    start_time_utc    TEXT,
    end_time_utc      TEXT,
    source_updated_at TEXT,
    fingerprint       TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (state IN
                        ('baseline', 'new', 'edited', 'deleted', 'held', 'superseded')),
    feedback_eligible INTEGER NOT NULL DEFAULT 0 CHECK (feedback_eligible IN (0, 1)),
    feedback_status   TEXT NOT NULL CHECK (feedback_status IN
                        ('pending', 'held', 'suppressed_baseline', 'suppressed_backfill',
                         'suppressed_superseded', 'suppressed_deleted', 'unverified_structure')),
    superseded_by     TEXT,
    hold_until        TEXT,
    conflict          INTEGER NOT NULL DEFAULT 0 CHECK (conflict IN (0, 1)),
    missing_count     INTEGER NOT NULL DEFAULT 0,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    UNIQUE (source, event_key)
);

CREATE INDEX IF NOT EXISTS events_by_source_date ON events (source, local_date);
CREATE INDEX IF NOT EXISTS events_by_feedback ON events (feedback_status);

CREATE TABLE IF NOT EXISTS event_changes (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    change      TEXT NOT NULL,
    detail      TEXT,
    detected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_changes_by_event ON event_changes (source, event_key);

CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    summary     TEXT NOT NULL
);
"""

SOURCE_STATE_FIELDS = (
    "baseline_completed_at", "cursor", "last_attempt_at", "last_success_at", "last_reconciliation_date",
    "health", "last_error_category", "consecutive_failures",
)


class StoreError(Exception):
    category = "store_error"


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

class Fingerprinter:
    """Keyed HMAC-SHA256 over a canonical JSON encoding."""

    def __init__(self, key: bytes):
        if len(key) < 32:
            raise StoreError("fingerprint key must be at least 32 bytes")
        self._key = key

    def __repr__(self) -> str:
        return "Fingerprinter(<key redacted>)"

    def digest(self, value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def load_fingerprint_key(path: Path, *, create: bool) -> Optional[bytes]:
    """Reads the per-installation HMAC key, creating it (owner-only) when
    create=True. Returns None when it does not exist and create=False."""
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise StoreError("fingerprint key file permissions are broader than owner-only")
        try:
            key = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        except ValueError:
            raise StoreError("fingerprint key file is corrupt") from None
        if len(key) < 32:
            raise StoreError("fingerprint key file is corrupt")
        return key
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)
    return key


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class EventStore:
    def __init__(self, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.conn = conn
        self._migrate()

    @classmethod
    def open(cls, path: Path) -> "EventStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        # isolation_level=None: autocommit; transactions are explicit below.
        conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        if not existed:
            os.chmod(path, 0o600)
        return cls(conn)

    @classmethod
    def open_snapshot(cls, path: Path) -> "EventStore":
        """In-memory copy for --dry-run: the on-disk database (if any) is
        opened read-only and copied, so nothing a dry-run does can reach it."""
        memory = sqlite3.connect(":memory:", isolation_level=None)
        if path.exists():
            source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            try:
                source.backup(memory)
            finally:
                source.close()
        return cls(memory)

    @classmethod
    def open_readonly(cls, path: Path) -> "EventStore":
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        store = cls.__new__(cls)
        store.conn = conn
        return store

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        # executescript() commits implicitly, so it runs on its own; every
        # statement in SCHEMA is idempotent (IF NOT EXISTS).
        self.conn.executescript(SCHEMA)
        with self.transaction():
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                                  (str(SCHEMA_VERSION),))
            elif int(row["value"]) not in SUPPORTED_SCHEMA_VERSIONS:
                raise StoreError(f"unsupported event store schema version {row['value']}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- source state -----------------------------------------------------

    def get_source_state(self, source: str) -> dict:
        row = self.conn.execute("SELECT * FROM source_state WHERE source = ?", (source,)).fetchone()
        if row is None:
            return {"source": source, "baseline_completed_at": None, "cursor": None, "last_attempt_at": None,
                    "last_success_at": None, "last_reconciliation_date": None, "health": "unknown",
                    "last_error_category": None, "consecutive_failures": 0}
        return dict(row)

    def update_source_state(self, source: str, **fields: Any) -> None:
        unknown = set(fields) - set(SOURCE_STATE_FIELDS)
        if unknown:
            raise StoreError(f"unknown source_state fields: {sorted(unknown)}")
        self.conn.execute("INSERT OR IGNORE INTO source_state (source) VALUES (?)", (source,))
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self.conn.execute(f"UPDATE source_state SET {assignments} WHERE source = ?",
                              (*fields.values(), source))

    # -- events -----------------------------------------------------------

    def get_event(self, source: str, event_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE source = ? AND event_key = ?",
                                 (source, event_key)).fetchone()

    def insert_event(self, **fields: Any) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO events ({columns}) VALUES ({placeholders})", tuple(fields.values()))

    def update_event(self, source: str, event_key: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE events SET {assignments} WHERE source = ? AND event_key = ?",
                          (*fields.values(), source, event_key))

    def record_change(self, source: str, event_key: str, change: str, detected_at: str,
                      detail: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO event_changes (source, event_key, change, detail, detected_at) VALUES (?, ?, ?, ?, ?)",
            (source, event_key, change, detail, detected_at),
        )

    def events_in_date_window(self, source: str, start_date: str, end_date: str) -> list:
        return self.conn.execute(
            "SELECT * FROM events WHERE source = ? AND local_date BETWEEN ? AND ?",
            (source, start_date, end_date),
        ).fetchall()

    def events_with_feedback_status(self, source: str, statuses: tuple) -> list:
        placeholders = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM events WHERE source = ? AND feedback_status IN ({placeholders})",
            (source, *statuses),
        ).fetchall()

    def timed_events(self, source: str) -> list:
        return self.conn.execute(
            "SELECT * FROM events WHERE source = ? AND state != 'deleted' AND start_time_utc IS NOT NULL",
            (source,),
        ).fetchall()

    def record_poll_run(self, started_at: str, finished_at: str, outcome: str, summary: dict) -> None:
        self.conn.execute(
            "INSERT INTO poll_runs (started_at, finished_at, outcome, summary) VALUES (?, ?, ?, ?)",
            (started_at, finished_at, outcome, json.dumps(summary, sort_keys=True)),
        )

    # -- reporting (counts only, never keys) -------------------------------

    def status_summary(self) -> dict:
        sources = {row["source"]: dict(row) for row in self.conn.execute("SELECT * FROM source_state")}
        for row in self.conn.execute(
            "SELECT source, state, feedback_status, COUNT(*) AS n FROM events GROUP BY 1, 2, 3"
        ):
            entry = sources.setdefault(row["source"], {"source": row["source"]})
            entry.setdefault("events_by_state", {})
            entry.setdefault("events_by_feedback_status", {})
            entry["events_by_state"][row["state"]] = entry["events_by_state"].get(row["state"], 0) + row["n"]
            entry["events_by_feedback_status"][row["feedback_status"]] = (
                entry["events_by_feedback_status"].get(row["feedback_status"], 0) + row["n"]
            )
        for entry in sources.values():
            entry["cursor_set"] = bool(entry.pop("cursor", None))
        last_run = self.conn.execute(
            "SELECT started_at, finished_at, outcome FROM poll_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        version = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return {
            "schema_version": int(version["value"]) if version else SCHEMA_VERSION,
            "sources": sources,
            "last_poll_run": dict(last_run) if last_run else None,
            "total_changes_recorded": self.conn.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0],
        }
