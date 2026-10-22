"""Shared synthetic fixtures for the feedback dispatcher tests.

Every event, identifier, credential and message here is synthetic. Nothing
invokes Claude, contacts Telegram, or touches runtime/.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_store as es  # noqa: E402
import feedback_claude as fc  # noqa: E402
import feedback_dispatcher as fd  # noqa: E402
import feedback_store as fs  # noqa: E402
import telegram_delivery as td  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
# Wednesday 2026-10-21 14:00 CEST.
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_AT = "2026-10-01T08:00:00+00:00"
FAKE_TELEGRAM = td.TelegramConfig(bot_token="123456:SYNTHETIC-token-value", chat_id="999000111")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def make_v1_store(path: Path) -> es.EventStore:
    store = es.EventStore.open(path)
    with store.transaction():
        for source in ("garmin", "hevy", "apple_health"):
            store.update_source_state(source, baseline_completed_at=BASELINE_AT, cursor="c" if source == "hevy" else None,
                                      health="ok", last_success_at=BASELINE_AT)
    return store


def add_event(store: es.EventStore, key: str, *, source: str = "garmin", start: datetime = None,
              minutes: int = 45, first_seen: datetime = None, state: str = "new", feedback_status: str = "pending",
              eligible: int = 1, activity_type: str = "running", event_type: str = "activity") -> int:
    start = start or NOW - timedelta(hours=2)
    first_seen = first_seen or start + timedelta(minutes=minutes + 20)
    with store.transaction():
        store.insert_event(
            source=source, event_key=key, event_type=event_type,
            activity_type=activity_type if source != "apple_health" else None,
            local_date=start.astimezone(TZ).date().isoformat(), start_time_utc=iso(start),
            end_time_utc=iso(start + timedelta(minutes=minutes)), source_updated_at=None,
            fingerprint="f" * 64, state=state, feedback_eligible=eligible, feedback_status=feedback_status,
            first_seen_at=iso(first_seen), last_seen_at=iso(first_seen),
        )
    return store.get_event(source, key)["id"]


def migrated_store(path: Path) -> fs.FeedbackStore:
    store = make_v1_store(path)
    fs.migrate_to_v2(store.conn, now_text=iso(NOW))
    return fs.FeedbackStore(store)


def good_result(prompt: str, schema: dict, *, message: str = None) -> fc.CallResult:
    result = fc.SimulatedGateway().call(prompt, schema)
    if message is not None:
        result.structured["message"] = message
    else:
        result.structured["message"] = "Solid easy run.\nKeep tomorrow easy."
    return result


class FakeGateway:
    simulated = False

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.prompts: list = []

    def call(self, prompt, schema):
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if callable(outcome):
            return outcome(prompt, schema)
        if outcome == "ok":
            return good_result(prompt, schema)
        return fc.CallResult(outcome)


class FakeTelegram:
    """Scripted sendMessage transport: each entry is (status, body) or an
    exception instance to raise; defaults to success."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent: list = []
        self.next_id = 100

    def __call__(self, url, data, timeout=10.0):
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step
        if step is not None:
            return step
        self.sent.append(data["text"])
        self.next_id += 1
        return 200, json.dumps({"ok": True, "result": {"message_id": self.next_id}})


def make_dispatcher(store: fs.FeedbackStore, *, gateway=None, telegram=FAKE_TELEGRAM, transport=None,
                    review_settled=lambda d: True, review_running=lambda: False, logs=None, **settings):
    options = dict(repo_root=REPO_ROOT, tz=TZ, claude_binary=sys.executable, auth_status=lambda: fc.AUTH_LOGGED_IN)
    options.update(settings)
    return fd.Dispatcher(
        store, fd.Settings(**options), gateway=gateway or FakeGateway(), telegram=telegram,
        http_post=transport if transport is not None else FakeTelegram(),
        log=(lambda **fields: logs.append(fields)) if logs is not None else None,
        review_settled=review_settled, review_running=review_running,
    )


def batches(store: fs.FeedbackStore) -> list:
    return store.conn.execute("SELECT * FROM feedback_batches ORDER BY id").fetchall()


def items(store: fs.FeedbackStore) -> list:
    return store.conn.execute("SELECT * FROM feedback_items ORDER BY id").fetchall()
