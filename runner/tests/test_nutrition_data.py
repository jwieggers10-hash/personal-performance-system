"""Tests for runner/nutrition_data.py: targets from goals.md, food-log
completeness, Garmin expenditure (completed vs partial, projection, no double
counting), training-day classification, body-composition trends and the
evening facts. All data is synthetic (see nutrition_fixtures.py)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import athletedata_client as adc  # noqa: E402
import nutrition_data as nd  # noqa: E402
from nutrition_fixtures import (  # noqa: E402
    REPO_ROOT, TODAY, TZ, UTC, FakeClient, body_payload, food_entry, food_page, garmin_activity, garmin_day,
    garmin_history, hevy_page, hevy_workout, history, local, micronutrient_row, prose, usual_day,
)

EVENING = local("2026-10-26 21:30")


def targets():
    return nd.load_targets(REPO_ROOT / "goals.md")


def training(day_type, run_minutes=0):
    return nd.DayTraining(TODAY, day_type, run_minutes, 1 if "run" in day_type else 0,
                          1 if "strength" in day_type else 0, (), True)


class TargetsTests(unittest.TestCase):
    def test_goals_md_block_matches_the_approved_targets(self):
        t = targets()
        self.assertEqual(t["status"], "provisional")
        self.assertEqual((t["calories_kcal"]["rest"], t["calories_kcal"]["strength"]), (2300, 2450))
        self.assertEqual(t["protein_g"], {"target": 160, "min": 150, "max": 170})
        self.assertEqual(t["fat_g"], {"target": 65, "min": 60})
        self.assertEqual(t["fiber_g"], {"min": 30})
        self.assertEqual(t["weight_change_kg_per_week"], {"min": -0.35, "max": -0.2})

    def test_day_type_targets(self):
        t = targets()
        cases = [("rest", 0, 2300), ("strength", 0, 2450), ("run", 30, 2600), ("run", 45, 2600),
                 ("run", 60, 2700), ("run", 100, 2850), ("run_and_strength", 30, 2700),
                 ("run_and_strength", 90, 2850)]
        for day_type, minutes, kcal in cases:
            with self.subTest(day_type=day_type, minutes=minutes):
                self.assertEqual(nd.day_targets(t, training(day_type, minutes))["kcal"], kcal)

    def test_combined_day_is_never_the_sum_of_exercise_calories(self):
        t = targets()
        combined = nd.day_targets(t, training("run_and_strength", 120))["kcal"]
        self.assertLessEqual(combined, max(b["kcal"] for b in t["calories_kcal"]["run"]))
        self.assertLess(combined, 2850 + (2450 - 2300))

    def test_carbohydrates_are_the_remainder(self):
        rest = nd.day_targets(targets(), training("rest"))
        self.assertEqual(rest["carbohydrate_g"], 270)  # (2300 - 160*4 - 65*9) / 4 = 268.75 -> nearest 5
        run = nd.day_targets(targets(), training("run", 100))
        self.assertGreater(run["carbohydrate_g"], rest["carbohydrate_g"])

    def test_missing_or_invalid_block_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "goals.md"
            for text in ("# Goals\nno block\n", "```nutrition-targets\n{not json\n```\n",
                         '```nutrition-targets\n{"calories_kcal": {"rest": 2300}}\n```\n'):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text[:20]), self.assertRaises(nd.TargetsError):
                    nd.load_targets(path)
            with self.assertRaises(nd.TargetsError):
                nd.load_targets(Path(tmp) / "absent.md")

    def test_open_ended_band_must_be_last(self):
        bad = json.loads(json.dumps(targets()))
        bad["calories_kcal"]["run"][0]["max_minutes"] = None
        self.assertIn("calories_kcal.run bands invalid", nd.validate_targets(bad))


class FoodLogTests(unittest.TestCase):
    def log(self, today_entries, *, last_sync=local("2026-10-26 19:42"), hist=None):
        return nd.parse_food_log([food_page((history(TODAY) if hist is None else hist) + today_entries, last_sync)])

    def test_complete_day(self):
        c = nd.assess_completeness(self.log(usual_day(TODAY)), TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.reasons, c.stale), ("complete", (), False))
        self.assertEqual(c.present_slots, ("breakfast", "lunch", "dinner", "snack"))

    def test_missing_dinner(self):
        entries = [e for e in usual_day(TODAY) if e["mealType"] in ("breakfast", "lunch")]
        c = nd.assess_completeness(self.log(entries), TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.missing_slots), ("incomplete", ("dinner",)))
        self.assertTrue(c.snack_missing)

    def test_three_slots_are_not_proof_of_completeness(self):
        entries = [food_entry(TODAY, "breakfast", 300), food_entry(TODAY, "lunch", 350),
                   food_entry(TODAY, "dinner", 400)]
        c = nd.assess_completeness(self.log(entries), TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.reasons, c.missing_slots), ("incomplete", ("low_intake",), ()))
        self.assertEqual(round(c.reference_kcal), 2400)

    def test_personal_pattern_without_breakfast(self):
        no_breakfast = (("lunch", 900), ("dinner", 1100), ("snack", 400))
        hist = history(TODAY, slots=no_breakfast)
        today = [food_entry(TODAY, m, k) for m, k in no_breakfast]
        c = nd.assess_completeness(self.log(today, hist=hist), TODAY, local("2026-10-26 20:00"))
        self.assertEqual(c.status, "complete")
        self.assertNotIn("breakfast", nd.personal_pattern(self.log(today, hist=hist), TODAY).expected_slots)

    def test_snack_alone_never_makes_a_log_incomplete(self):
        entries = [e for e in usual_day(TODAY) if e["mealType"] != "snack"]
        entries.append(food_entry(TODAY, "dinner", 350))
        c = nd.assess_completeness(self.log(entries), TODAY, local("2026-10-26 20:00"))
        self.assertEqual(c.status, "complete")
        self.assertTrue(c.snack_missing)

    def test_no_entries_is_missing(self):
        c = nd.assess_completeness(self.log([]), TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.reasons), ("missing", ("no_entries",)))

    def test_too_little_history_is_unknown(self):
        hist = history(TODAY, days=3)
        c = nd.assess_completeness(self.log(usual_day(TODAY), hist=hist), TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.reasons), ("unknown", ("no_personal_pattern",)))

    def test_partially_logged_history_days_do_not_lower_the_pattern(self):
        hist = history(TODAY, days=6) + [food_entry(TODAY - timedelta(days=d), "breakfast", 500)
                                        for d in range(7, 15)]
        log = self.log([food_entry(TODAY, "breakfast", 500)], hist=hist)
        pattern = nd.personal_pattern(log, TODAY)
        self.assertEqual((pattern.logged_days, pattern.expected_slots), (14, nd.CORE_SLOTS))
        self.assertEqual(pattern.reference_kcal, 2400)
        c = nd.assess_completeness(log, TODAY, local("2026-10-26 20:00"))
        self.assertEqual((c.status, c.reasons), ("incomplete", ("missing_slots", "low_intake")))

    def test_too_few_substantial_days_is_unknown(self):
        # 2 full days + 2 large single entries = 4 substantial days (< MIN_HISTORY_DAYS).
        hist = history(TODAY, days=2) + [food_entry(TODAY - timedelta(days=d), "breakfast", 500)
                                        for d in range(3, 15)]
        hist += [food_entry(TODAY - timedelta(days=d), "breakfast", 2400) for d in range(15, 17)]
        c = nd.assess_completeness(self.log(usual_day(TODAY), hist=hist), TODAY, local("2026-10-26 20:00"))
        self.assertEqual(c.status, "unknown")

    def test_staleness(self):
        cases = [(local("2026-10-26 19:42"), False), (local("2026-10-26 17:30"), True), (None, True),
                 (local("2026-10-25 21:10"), True)]
        for last_sync, stale in cases:
            with self.subTest(last_sync=last_sync):
                c = nd.assess_completeness(self.log(usual_day(TODAY), last_sync=last_sync), TODAY,
                                           local("2026-10-26 20:00"))
                self.assertEqual(c.stale, stale)
        c = nd.assess_completeness(self.log(usual_day(TODAY), last_sync=local("2026-10-26 17:30")), TODAY,
                                   local("2026-10-26 20:00"))
        self.assertEqual(c.sync_age_minutes, 150)

    def test_non_intake_rows_are_ignored(self):
        entries = usual_day(TODAY) + [micronutrient_row(TODAY),
                                      food_entry(TODAY, "dinner", 900, pre_logged=True)]
        day = self.log(entries).days[TODAY]
        self.assertEqual((day.kcal, day.entries), (2400, 4))

    def test_placeholder_meal_type_is_not_slot_evidence(self):
        entry = food_entry(TODAY, "breakfast", 400, meal_type_is_placeholder=True)
        self.assertEqual(self.log([entry]).days[TODAY].slots, frozenset())

    def test_whole_day_total_is_complete(self):
        entry = food_entry(TODAY, "snack", 2300, whole_day_total=True, meal_type_is_placeholder=True)
        c = nd.assess_completeness(self.log([entry]), TODAY, local("2026-10-26 20:00"))
        self.assertEqual(c.status, "complete")

    def test_quick_add_entries_make_fiber_unreliable(self):
        log = self.log(usual_day(TODAY) + [food_entry(TODAY, "dinner", 900, fiber=0, sodium=0)])
        self.assertFalse(log.days[TODAY].fiber_reliable)
        self.assertTrue(self.log(usual_day(TODAY)).days[TODAY].fiber_reliable)

    def test_entries_repeated_across_pages_count_once(self):
        entries = usual_day(TODAY)
        log = nd.parse_food_log([food_page(entries, local("2026-10-26 19:00")),
                                 food_page(entries[:2], local("2026-10-26 19:42"))])
        self.assertEqual(log.days[TODAY].kcal, 2400)
        self.assertEqual(log.last_sync, local("2026-10-26 19:42"))

    def test_past_day_completeness(self):
        hist = history(TODAY, skip={TODAY - timedelta(days=2)})
        hist += [food_entry(TODAY - timedelta(days=2), "breakfast", 500)]
        log = self.log([], hist=hist)
        self.assertTrue(nd.day_log_complete(log, TODAY - timedelta(days=1)))
        self.assertFalse(nd.day_log_complete(log, TODAY - timedelta(days=2)))
        self.assertFalse(nd.day_log_complete(log, TODAY - timedelta(days=30)))


class ExpenditureTests(unittest.TestCase):
    def garmin(self, today_row=None, days=14, **kwargs):
        rows = garmin_history(TODAY, days=days, **kwargs) + ([today_row] if today_row else [])
        return nd.parse_daily_summaries(rows)

    def test_completed_day_total_is_resting_plus_active(self):
        g = self.garmin(garmin_day(TODAY, bmr=2233, active=640))
        result = nd.assess_expenditure(g, TODAY, local("2026-10-27 08:00"), TZ)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observed"], {"resting_kcal": 2233, "active_kcal": 640, "total_kcal": 2873,
                                              "covers_hours": 24.0})
        self.assertIsNone(result["projection"])

    def test_partial_day_projection(self):
        g = self.garmin(garmin_day(TODAY, bmr=1773, active=318, window=19.1), active=480)
        result = nd.assess_expenditure(g, TODAY, local("2026-10-26 20:55"), TZ)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["observed"]["total_kcal"], 2091)
        self.assertEqual(result["observed"]["data_through_local"], "19:06")
        p = result["projection"]
        bmr_full = 1773 * 24 / 19.1
        self.assertEqual(p["low_kcal"], round(bmr_full + 318))
        self.assertEqual(p["high_kcal"], round(bmr_full + 318 + 480 / 24 * 4.9))
        self.assertIn("not measured", p["label"])

    def test_no_projection_when_not_defensible(self):
        cases = [
            ("snapshot_covers_too_little_of_the_day", garmin_day(TODAY, bmr=1100, active=200, window=12.0),
             local("2026-10-26 14:00"), 14),
            ("snapshot_not_recent", garmin_day(TODAY, bmr=1600, active=300, window=17.0),
             local("2026-10-26 21:30"), 14),
            ("too_few_completed_days", garmin_day(TODAY, bmr=1773, active=318, window=19.1),
             local("2026-10-26 20:00"), 2),
        ]
        for reason, row, now, days in cases:
            with self.subTest(reason=reason):
                result = nd.assess_expenditure(self.garmin(row, days=days), TODAY, now, TZ)
                self.assertEqual((result["status"], result["projection"], result["projection_unavailable_reason"]),
                                 ("partial", None, reason))

    def test_missing_garmin_day(self):
        result = nd.assess_expenditure(self.garmin(), TODAY, EVENING, TZ)
        self.assertEqual((result["status"], result["projection_unavailable_reason"]), ("missing", "no_garmin_data"))

    def test_dst_day_uses_real_elapsed_time(self):
        # 2026-10-25 has 25 local hours: 20 h after local midnight is 19:00 CET.
        day = date(2026, 10, 25)
        rows = [garmin_day(day - timedelta(days=o)) for o in range(1, 15)] + [garmin_day(day, window=20.0,
                                                                                         bmr=1861, active=200)]
        result = nd.assess_expenditure(nd.parse_daily_summaries(rows), day, local("2026-10-25 21:30"), TZ)
        self.assertEqual(result["observed"]["data_through_local"], "19:00")
        self.assertIsNotNone(result["projection"])

    def test_fullest_snapshot_wins(self):
        rows = [garmin_day(TODAY, active=100, window=10.0), garmin_day(TODAY, active=300, window=19.0)]
        self.assertEqual(nd.parse_daily_summaries(rows)[TODAY].active_kcal, 300)
        self.assertEqual(nd.parse_daily_summaries({"archive_note": "x", "records": rows})[TODAY].window_hours, 19.0)


class TrainingTests(unittest.TestCase):
    def classify(self, garmin=(), hevy=(), day=TODAY):
        return nd.classify_day(day, nd.parse_garmin_activities(list(garmin), TZ),
                               nd.parse_hevy_workouts([hevy_page(list(hevy))], TZ), garmin_ok=True, hevy_ok=True)

    def test_day_types(self):
        run = garmin_activity(TODAY, "RUNNING", 50)
        lift = hevy_workout("h1", local("2026-10-26 07:00"))
        self.assertEqual(self.classify().day_type, "rest")
        self.assertEqual(self.classify(hevy=[lift]).day_type, "strength")
        self.assertEqual(self.classify(garmin=[run]).day_type, "run")
        both = self.classify(garmin=[run], hevy=[lift])
        self.assertEqual((both.day_type, both.run_minutes), ("run_and_strength", 50))
        trail = self.classify(garmin=[garmin_activity(TODAY, "TRAIL_RUNNING", 80)])
        self.assertEqual((trail.day_type, trail.run_minutes), ("run", 80))

    def test_garmin_strength_is_not_a_second_hevy_session(self):
        t = self.classify(garmin=[garmin_activity(TODAY, "STRENGTH_TRAINING", 60)],
                          hevy=[hevy_workout("h1", local("2026-10-26 07:00"))])
        self.assertEqual((t.day_type, t.strength_sessions), ("strength", 1))
        alone = self.classify(garmin=[garmin_activity(TODAY, "STRENGTH_TRAINING", 60)])
        self.assertEqual(alone.day_type, "strength")

    def test_other_activities_do_not_change_the_day_type(self):
        t = self.classify(garmin=[garmin_activity(TODAY, "WALKING", 40)])
        self.assertEqual((t.day_type, t.other_activities), ("rest", ("WALKING",)))

    def test_hevy_day_is_the_local_date(self):
        late = hevy_workout("h1", local("2026-10-27 00:30"))  # 23:30 UTC on the 26th
        self.assertEqual(self.classify(hevy=[late]).day_type, "rest")
        self.assertEqual(self.classify(hevy=[late], day=TODAY + timedelta(days=1)).day_type, "strength")

    def test_garmin_day_from_start_time_when_local_date_missing(self):
        row = {"activityType": "RUNNING", "durationInSeconds": 1800,
               "startTimeInSeconds": int(local("2026-10-26 23:30").timestamp())}
        self.assertEqual(nd.parse_garmin_activities([row], TZ)[0]["day"], TODAY)


class BodyCompositionTests(unittest.TestCase):
    def test_trend_and_versus_target(self):
        weights = {TODAY - timedelta(days=d): 78.0 - (27 - d) * 0.04 for d in range(0, 28, 2)}  # -0.28 kg/week
        result = nd.body_composition(body_payload(weights), TODAY, [-0.35, -0.2])
        w = result["weight_kg"]
        self.assertTrue(w["sufficient"])
        self.assertAlmostEqual(w["slope_per_week"], -0.28, places=2)
        self.assertEqual(w["versus_target"], "within_target")

    def test_duplicates_are_never_averaged(self):
        weights = {TODAY - timedelta(days=d): 77.0 for d in range(0, 28, 2)}
        weights[TODAY] = [77.0, 77.0]                 # identical: counted once
        weights[TODAY - timedelta(days=2)] = [76.0, 79.0]  # conflicting: excluded
        result = nd.body_composition(body_payload(weights), TODAY, None)
        self.assertEqual(result["weight_kg"]["points"], 13)
        self.assertEqual(result["conflicting_dates_excluded"], [(TODAY - timedelta(days=2)).isoformat()])

    def test_insufficient_data_gives_no_slope(self):
        weights = {TODAY - timedelta(days=d): 77.0 for d in range(5)}
        w = nd.body_composition(body_payload(weights), TODAY, [-0.35, -0.2])["weight_kg"]
        self.assertFalse(w["sufficient"])
        self.assertNotIn("versus_target", w)

    def test_body_fat_is_reported_in_percent(self):
        fats = {TODAY - timedelta(days=d): 0.145 for d in range(0, 28, 2)}
        result = nd.body_composition(body_payload({}, fats), TODAY, None)
        self.assertEqual(result["body_fat_percent"]["last_7_day_mean"], 14.5)


def standard_responses(today_food=None, *, today_garmin=None, activities=None, hevy=None, last_sync=None):
    food = history(TODAY) + (usual_day(TODAY) if today_food is None else today_food)
    return {
        "food_log_get": food_page(food, last_sync or local("2026-10-26 19:42")),
        "garmin_get_daily_summaries": garmin_history(TODAY) + [
            today_garmin or garmin_day(TODAY, bmr=1890, active=420, window=20.3)],
        "garmin_get_activities": activities if activities is not None else [],
        "hevy_get_workouts": hevy_page(hevy if hevy is not None else [hevy_workout("h1", local("2026-10-26 07:00"))]),
        "apple_health_get_body_composition": body_payload(
            {TODAY - timedelta(days=d): 77.0 + d * 0.04 for d in range(0, 28, 2)}),
    }


class FetchTests(unittest.TestCase):
    def test_new_tools_are_read_only_allowlisted(self):
        for name in nd.TOOLS:
            self.assertIn(name, adc.READ_ONLY_TOOL_ALLOWLIST)
        for write_tool in ("food_log_entry", "food_log_update", "nutrition_targets_set", "hevy_create_workout"):
            self.assertNotIn(write_tool, adc.READ_ONLY_TOOL_ALLOWLIST)

    def test_fetch_all_sources(self):
        client = FakeClient(standard_responses())
        inputs = nd.fetch_inputs(client, TODAY, TZ)
        self.assertEqual(inputs.errors, {})
        food_call = [args for name, args in client.calls if name == "food_log_get"][0]
        self.assertEqual(food_call["timezone"], "Europe/Amsterdam")
        self.assertEqual(food_call["end_date"], "2026-10-26")

    def test_sources_fail_independently(self):
        responses = standard_responses()
        responses["hevy_get_workouts"] = adc.TransportError("synthetic", "network")
        del responses["apple_health_get_body_composition"]
        inputs = nd.fetch_inputs(FakeClient(responses), TODAY, TZ)
        self.assertEqual(inputs.errors, {"hevy_get_workouts": "network",
                                         "apple_health_get_body_composition": "tool_unavailable"})
        self.assertTrue(inputs.food_pages)

    def test_prose_answer_is_no_rows_not_an_error(self):
        responses = standard_responses()
        responses["garmin_get_activities"] = prose()
        inputs = nd.fetch_inputs(FakeClient(responses), TODAY, TZ)
        self.assertEqual(inputs.errors, {})
        self.assertIn("garmin_get_activities", inputs.empty)

    def test_food_pages_follow_the_cursor(self):
        pages = {None: food_page(usual_day(TODAY), local("2026-10-26 19:42"), next_cursor="c2"),
                 "c2": food_page(history(TODAY))}
        client = FakeClient({"food_log_get": lambda args: pages[args.get("cursor")]})
        inputs = nd.fetch_inputs(client, TODAY, TZ, sources=("food_log_get",))
        self.assertEqual(len(inputs.food_pages), 2)
        self.assertEqual(nd.assess_completeness(nd.parse_food_log(inputs.food_pages), TODAY,
                                                local("2026-10-26 20:00")).status, "complete")

    def test_verification_failure_marks_every_source(self):
        client = FakeClient({}, verify_error=adc.TransportError("synthetic", "network"))
        inputs = nd.fetch_inputs(client, TODAY, TZ)
        self.assertEqual(set(inputs.errors), set(nd.TOOLS))


class FactsTests(unittest.TestCase):
    def facts(self, responses, now=EVENING, targets_value="load"):
        inputs = nd.fetch_inputs(FakeClient(responses), TODAY, TZ)
        t = targets() if targets_value == "load" else targets_value
        return nd.build_facts(inputs, t, TODAY, now.astimezone(UTC), TZ,
                              targets_error=None if t else "goals.md has no nutrition-targets block")

    def test_complete_day_with_projection(self):
        f = self.facts(standard_responses())
        self.assertEqual(f["training_today"]["day_type"], "strength")
        self.assertEqual(f["targets"]["kcal"], 2450)
        self.assertEqual(f["food_today"]["log_status"], "complete")
        self.assertEqual(f["food_today"]["kcal"], 2400)
        self.assertEqual(f["garmin_expenditure_today"]["status"], "partial")
        p = f["garmin_expenditure_today"]["projection"]
        self.assertEqual(f["energy_balance_today"]["low_kcal"], 2400 - p["high_kcal"])
        self.assertEqual(f["energy_balance_today"]["high_kcal"], 2400 - p["low_kcal"])
        self.assertIn("projected", f["energy_balance_today"]["label"])
        self.assertEqual(f["recent_trends"]["complete_log_days"], 7)
        self.assertEqual(f["recent_trends"]["mean_energy_balance_kcal_completed_days"], 2400 - 2733)
        json.dumps(f)  # serialisable for the prompt

    def test_trend_counts_use_fully_logged_days_as_denominator(self):
        responses = standard_responses()
        partial_days = {TODAY - timedelta(days=1), TODAY - timedelta(days=2)}
        low_protein = {TODAY - timedelta(days=d) for d in (3, 4, 5)}
        entries = []
        for offset in range(1, 15):
            day = TODAY - timedelta(days=offset)
            if day in partial_days:
                entries.append(food_entry(day, "breakfast", 500, protein=40))
            else:
                entries += usual_day(day, protein_per_slot=30.0 if day in low_protein else 45.0)
        responses["food_log_get"] = food_page(entries + usual_day(TODAY), local("2026-10-26 19:42"))
        trends = self.facts(responses)["recent_trends"]
        self.assertEqual((trends["complete_log_days"], trends["of_days"]), (5, 7))
        self.assertEqual(trends["days_protein_at_or_above_min"], 2)
        self.assertEqual(trends["protein_min_met_statement"], "Protein reached 150 g on 2 of 5 fully logged days")
        self.assertNotIn("of 7", trends["protein_min_met_statement"])

    def test_workflow_requires_fully_logged_denominators(self):
        text = (REPO_ROOT / "workflows" / "evening-nutrition-review.md").read_text(encoding="utf-8")
        self.assertIn("never out of the calendar days", text)
        self.assertIn("protein_min_met_statement", text)
        self.assertIn('Never write "2 of 7 days"', text)

    def test_incomplete_log_gets_no_balance(self):
        partial = [e for e in usual_day(TODAY) if e["mealType"] == "breakfast"]
        f = self.facts(standard_responses(partial))
        self.assertEqual(f["food_today"]["log_status"], "incomplete")
        self.assertEqual(f["energy_balance_today"], {"available": False, "reason": "food_log_not_complete"})

    def test_no_projection_means_no_balance(self):
        f = self.facts(standard_responses(today_garmin=garmin_day(TODAY, bmr=900, active=100, window=10.0)))
        self.assertIsNone(f["garmin_expenditure_today"]["projection"])
        self.assertEqual(f["energy_balance_today"]["reason"], "no_defensible_expenditure_projection")

    def test_workout_calories_are_never_added(self):
        quiet = self.facts(standard_responses(activities=[]))
        busy = self.facts(standard_responses(activities=[garmin_activity(TODAY, "RUNNING", 60, active_kcal=900)]))
        self.assertEqual(busy["garmin_expenditure_today"]["observed"], quiet["garmin_expenditure_today"]["observed"])
        self.assertEqual(busy["garmin_expenditure_today"]["projection"],
                         quiet["garmin_expenditure_today"]["projection"])
        self.assertEqual(busy["training_today"]["day_type"], "run_and_strength")
        self.assertEqual(busy["targets"]["kcal"], 2850)

    def test_recent_balance_only_for_complete_and_completed_days(self):
        responses = standard_responses()
        yesterday = TODAY - timedelta(days=1)
        responses["food_log_get"] = food_page(
            history(TODAY, skip={yesterday}) + [food_entry(yesterday, "breakfast", 500)] + usual_day(TODAY),
            local("2026-10-26 19:42"))
        responses["garmin_get_daily_summaries"] = [r for r in responses["garmin_get_daily_summaries"]
                                                   if r["calendarDate"] != (TODAY - timedelta(days=2)).isoformat()]
        f = self.facts(responses)
        rows = {r["date"]: r for r in f["recent_days"]}
        self.assertEqual(rows[yesterday.isoformat()]["food_log"], "partial")
        self.assertNotIn("energy_balance_kcal", rows[yesterday.isoformat()])
        self.assertNotIn("energy_balance_kcal", rows[(TODAY - timedelta(days=2)).isoformat()])
        self.assertEqual(f["recent_trends"]["energy_balance_days_used"], 5)

    def test_missing_garmin_and_failed_sources(self):
        responses = standard_responses()
        responses["garmin_get_daily_summaries"] = garmin_history(TODAY)  # nothing for today
        responses["hevy_get_workouts"] = adc.TransportError("synthetic", "network")
        f = self.facts(responses)
        self.assertEqual(f["garmin_expenditure_today"]["status"], "missing")
        self.assertFalse(f["training_today"]["complete"])
        self.assertEqual(f["sources"]["hevy_get_workouts"], "error:network")
        self.assertEqual(f["energy_balance_today"]["available"], False)

    def test_garmin_fetch_failure(self):
        responses = standard_responses()
        responses["garmin_get_daily_summaries"] = adc.HTTPStatusError(503)
        f = self.facts(responses)
        self.assertEqual(f["garmin_expenditure_today"]["status"], "unavailable")

    def test_targets_unavailable(self):
        f = self.facts(standard_responses(), targets_value=None)
        self.assertEqual(f["targets"], {"unavailable": "goals.md has no nutrition-targets block"})
        self.assertIsNone(f["recent_trends"]["days_protein_at_or_above_min"])

    def test_food_source_failure(self):
        responses = standard_responses()
        responses["food_log_get"] = adc.TransportError("synthetic", "network")
        f = self.facts(responses)
        self.assertEqual(f["food_today"], {"available": False})
        self.assertEqual(f["energy_balance_today"]["reason"], "food_log_not_complete")


if __name__ == "__main__":
    unittest.main()
