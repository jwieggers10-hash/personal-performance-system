"""Tests for runner/feedback_policy.py: quiet hours across DST, eligibility,
staleness, grouping and feedback level. Pure functions; synthetic rows."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedback_policy as fp  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def row(event_id=1, *, source="garmin", start=None, minutes=45, first_seen=None, state="new",
        feedback_status="pending", eligible=1, released=None):
    start = start or NOW - timedelta(hours=2)
    event = {
        "id": event_id, "source": source, "state": state, "feedback_status": feedback_status,
        "feedback_eligible": eligible, "start_time_utc": start.isoformat(),
        "end_time_utc": (start + timedelta(minutes=minutes)).isoformat() if minutes is not None else None,
        "first_seen_at": (first_seen or start + timedelta(minutes=(minutes or 0) + 20)).isoformat(),
    }
    event["eligible_since"] = released.isoformat() if released else event["first_seen_at"]
    return event


class QuietHoursTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(fp.in_quiet_hours(local("2026-10-21T21:59:59"), TZ))
        self.assertTrue(fp.in_quiet_hours(local("2026-10-21T22:00:00"), TZ))
        self.assertTrue(fp.in_quiet_hours(local("2026-10-22T03:00:00"), TZ))
        self.assertTrue(fp.in_quiet_hours(local("2026-10-22T07:00:00"), TZ))  # Daily Review time
        self.assertTrue(fp.in_quiet_hours(local("2026-10-22T07:29:59"), TZ))
        self.assertFalse(fp.in_quiet_hours(local("2026-10-22T07:30:00"), TZ))

    def test_next_release_evening_and_early_morning(self):
        self.assertEqual(fp.next_release(local("2026-10-21T23:10"), TZ), local("2026-10-22T07:30"))
        self.assertEqual(fp.next_release(local("2026-10-22T05:00"), TZ), local("2026-10-22T07:30"))
        midday = local("2026-10-22T12:00")
        self.assertEqual(fp.next_release(midday, TZ), midday)

    def test_fall_back_night(self):
        # 2026-10-25: CEST -> CET at 03:00 local. 07:30 CET is 06:30 UTC.
        release = fp.next_release(local("2026-10-24T22:30"), TZ)
        self.assertEqual(release, datetime(2026, 10, 25, 6, 30, tzinfo=UTC))
        # During the repeated hour (02:30 occurs twice) it is still quiet.
        first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
        self.assertEqual(first.astimezone(TZ).hour, second.astimezone(TZ).hour)
        self.assertTrue(fp.in_quiet_hours(first, TZ) and fp.in_quiet_hours(second, TZ))
        # Quiet hours end at 07:30 local, not at a fixed UTC hour.
        self.assertTrue(fp.in_quiet_hours(datetime(2026, 10, 25, 6, 29, tzinfo=UTC), TZ))
        self.assertFalse(fp.in_quiet_hours(datetime(2026, 10, 25, 6, 30, tzinfo=UTC), TZ))

    def test_spring_forward_night(self):
        # 2027-03-28: CET -> CEST at 02:00 local. 07:30 CEST is 05:30 UTC.
        release = fp.next_release(local("2027-03-27T23:00"), TZ)
        self.assertEqual(release, datetime(2027, 3, 28, 5, 30, tzinfo=UTC))
        self.assertTrue(fp.in_quiet_hours(datetime(2027, 3, 28, 5, 29, tzinfo=UTC), TZ))
        self.assertFalse(fp.in_quiet_hours(datetime(2027, 3, 28, 5, 30, tzinfo=UTC), TZ))
        # 22:00 the same evening is 20:00 UTC (summer time).
        self.assertTrue(fp.in_quiet_hours(datetime(2027, 3, 28, 20, 0, tzinfo=UTC), TZ))
        self.assertFalse(fp.in_quiet_hours(datetime(2027, 3, 28, 19, 59, tzinfo=UTC), TZ))

    def test_nonexistent_wall_time_resolves_after_gap(self):
        moment = fp.local_wall_time(date(2027, 3, 28), datetime.strptime("02:30", "%H:%M").time(), TZ)
        self.assertEqual(moment.astimezone(TZ).strftime("%H:%M"), "03:30")

    def test_morning_gate_waits_for_daily_review_until_deadline(self):
        never = lambda d: False  # noqa: E731
        self.assertFalse(fp.may_process("routine", local("2026-10-22T07:45"), TZ, never))
        self.assertTrue(fp.may_process("routine", local("2026-10-22T07:45"), TZ, lambda d: True))
        self.assertTrue(fp.may_process("routine", local("2026-10-22T09:00"), TZ, never))

    def test_urgent_bypasses_quiet_hours_attention_does_not(self):
        night = local("2026-10-22T02:00")
        self.assertTrue(fp.may_process("urgent", night, TZ, lambda d: False))
        self.assertFalse(fp.may_process("attention", night, TZ, lambda d: True))
        self.assertFalse(fp.may_process("routine", night, TZ, lambda d: True))


class EligibilityTests(unittest.TestCase):
    def assess(self, event, *, now=NOW, activated_at=None, apple=False):
        return fp.assess_event(event, now=now, activated_at=activated_at, apple_health_verified=apple)

    def test_fresh_settled_event_eligible(self):
        self.assertEqual(self.assess(row()).decision, "eligible")

    def test_settling(self):
        event = row(first_seen=NOW - timedelta(minutes=3))
        self.assertEqual(self.assess(event), fp.Verdict("wait", "settling"))
        self.assertEqual(fp.revalidate(event, now=NOW, apple_health_verified=False).decision, "eligible")

    def test_exclusions(self):
        cases = {
            "baseline": row(state="baseline", feedback_status="suppressed_baseline", eligible=0),
            "backfill": row(feedback_status="suppressed_backfill", eligible=0),
            "deleted": row(state="deleted", feedback_status="suppressed_deleted", eligible=0),
            "superseded": row(state="superseded", feedback_status="suppressed_superseded", eligible=0),
            "not_eligible": row(state="held", feedback_status="held", eligible=0),
        }
        for reason, event in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(self.assess(event), fp.Verdict("suppressed", reason))

    def test_unverified_apple_health_excluded_even_if_marked_pending(self):
        event = row(source="apple_health")
        self.assertEqual(self.assess(event), fp.Verdict("suppressed", "unverified_structure"))
        self.assertEqual(self.assess(event, apple=True).decision, "eligible")
        unverified = row(source="apple_health", feedback_status="unverified_structure", eligible=0)
        self.assertEqual(self.assess(unverified, apple=True).reason, "unverified_structure")

    def test_late_sync_accepted_within_occurrence_cap(self):
        # AthleteData delivered a 30 h old activity only now.
        late = row(start=NOW - timedelta(hours=30), first_seen=NOW - timedelta(minutes=15))
        self.assertEqual(self.assess(late).decision, "eligible")

    def test_genuinely_historical_event_expires(self):
        historical = row(start=NOW - timedelta(hours=49), first_seen=NOW - timedelta(minutes=15))
        self.assertEqual(self.assess(historical), fp.Verdict("expired", "stale_occurrence"))
        backlog = row(start=NOW - timedelta(days=4), first_seen=NOW - timedelta(minutes=15))
        self.assertEqual(self.assess(backlog).reason, "stale_occurrence")

    def test_notification_window_runs_from_eligibility(self):
        # Eligible 25 h ago but never queued (e.g. the dispatcher was down).
        missed = row(start=NOW - timedelta(hours=26), first_seen=NOW - timedelta(hours=25))
        self.assertEqual(self.assess(missed), fp.Verdict("expired", "notification_window_passed"))

    def test_strength_hold_does_not_count_against_event(self):
        # Garmin strength held 24 h, released 15 min ago.
        held = row(start=NOW - timedelta(hours=25), first_seen=NOW - timedelta(hours=24, minutes=20),
                   released=NOW - timedelta(minutes=15))
        self.assertEqual(self.assess(held).decision, "eligible")
        self.assertEqual(fp.notification_deadline(held), NOW - timedelta(minutes=15) + fp.NOTIFICATION_WINDOW)
        just_released = row(start=NOW - timedelta(hours=25), released=NOW - timedelta(minutes=3))
        self.assertEqual(self.assess(just_released), fp.Verdict("wait", "settling"))

    def test_revalidation_ignores_occurrence_age(self):
        # Accepted at 47 h; quiet hours push generation past 48 h.
        accepted = row(start=NOW - timedelta(hours=47), first_seen=NOW - timedelta(minutes=20))
        later = NOW + timedelta(hours=10)
        self.assertEqual(fp.revalidate(accepted, now=later, apple_health_verified=False).decision, "eligible")
        self.assertEqual(fp.revalidate(accepted, now=NOW + timedelta(hours=24), apple_health_verified=False),
                         fp.Verdict("expired", "notification_window_passed"))

    def test_pre_activation(self):
        activated = NOW
        event = row(start=NOW - timedelta(hours=30))
        self.assertEqual(self.assess(event, activated_at=activated), fp.Verdict("expired", "pre_activation"))
        recent = row(start=NOW - timedelta(hours=10))
        self.assertEqual(self.assess(recent, activated_at=activated).decision, "eligible")
        # Became eligible after activation (released hold / late sync): not a cutover event.
        released_later = row(start=NOW - timedelta(hours=30), released=NOW + timedelta(hours=1))
        self.assertEqual(self.assess(released_later, now=NOW + timedelta(hours=2), activated_at=activated).decision,
                         "eligible")

    def test_unknown_time_expires(self):
        event = row()
        event.update(start_time_utc=None, end_time_utc=None, first_seen_at=None, eligible_since=None)
        self.assertEqual(self.assess(event), fp.Verdict("expired", "unknown_event_time"))


class GroupingAndLevelTests(unittest.TestCase):
    def test_close_events_grouped_far_events_split(self):
        a = row(1, start=NOW - timedelta(hours=10))
        b = row(2, source="hevy", start=NOW - timedelta(hours=8))
        c = row(3, start=NOW - timedelta(hours=2))
        groups = fp.group_events([c, a, b])
        self.assertEqual([[e["id"] for e in g] for g in groups], [[1, 2], [3]])

    def test_group_size_capped(self):
        events = [row(i, start=NOW - timedelta(hours=5) + timedelta(minutes=10 * i)) for i in range(8)]
        sizes = [len(g) for g in fp.group_events(events)]
        self.assertEqual(sizes, [fp.MAX_EVENTS_PER_BATCH, 2])

    def test_levels(self):
        routine = row(minutes=45)
        self.assertEqual(fp.classify_level([routine], previous_training_start=NOW - timedelta(days=1),
                                           history_covers_gap=True), ("routine", []))
        long_run = row(minutes=100)
        self.assertEqual(fp.classify_level([long_run], previous_training_start=NOW - timedelta(days=1),
                                           history_covers_gap=True)[1], ["long_session"])
        hybrid = [row(1), row(2, source="hevy")]
        self.assertIn("hybrid_day", fp.classify_level(hybrid, previous_training_start=None,
                                                      history_covers_gap=False)[1])
        comeback = fp.classify_level([routine], previous_training_start=NOW - timedelta(days=6),
                                     history_covers_gap=True)
        self.assertEqual(comeback, ("significant", ["return_after_break"]))
        unknown_history = fp.classify_level([routine], previous_training_start=None, history_covers_gap=False)
        self.assertEqual(unknown_history, ("routine", []))

    def test_body_composition_alone_is_routine(self):
        weigh_in = row(source="apple_health", minutes=200)
        self.assertEqual(fp.classify_level([weigh_in], previous_training_start=None, history_covers_gap=True),
                         ("routine", []))


if __name__ == "__main__":
    unittest.main()
