#!/usr/bin/env python3
"""Deterministic nutrition and energy facts (Phase 5B).

Shared by the 20:00 food-log check (runner/food_log_check.py) and the 21:30
Evening Nutrition Review (runner/evening_nutrition_review.py). Pure functions
over AthleteData payloads plus fetch helpers that use the read-only
AthleteData client; no Claude, no Telegram, no writes.

Every number that reaches the athlete is computed here from retrieved data.
Nothing is estimated that the data does not support, and the one projection
(today's Garmin expenditure) is labelled as a projection with its method and
range. See runner/nutrition-workflows.md.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import athletedata_client as adc

UTC = dt_timezone.utc

MEAL_SLOTS = ("breakfast", "lunch", "dinner", "snack")
CORE_SLOTS = ("breakfast", "lunch", "dinner")  # a snack alone never makes a log incomplete

# Completeness heuristics (runner/nutrition-workflows.md, Food-Log Completeness).
HISTORY_DAYS = 28            # personal logging pattern window, excluding the day assessed
MIN_HISTORY_DAYS = 5         # logged days needed before a personal pattern is used at all
CORE_SLOT_MIN_SHARE = 0.5    # a core slot is "usual" when logged on at least half of logged days
LOW_INTAKE_SHARE = 0.65      # below this share of a usual full day the log is likely incomplete
STALE_AFTER = timedelta(hours=2)
QUICK_ADD_MIN_KCAL = 200     # an entry this large with no fiber and no sodium carries no nutrient detail

# Garmin expenditure (runner/nutrition-workflows.md, Energy Expenditure).
GARMIN_DAY_HOURS = 24.0      # Garmin days are 24 h records, also across DST changes
PROJECTION_MIN_WINDOW_HOURS = 16.0
PROJECTION_MAX_LAG = timedelta(hours=3)
PROJECTION_MIN_COMPLETED_DAYS = 3
EXPENDITURE_HISTORY_DAYS = 14

RECENT_DAYS = 7
WEIGHT_TREND_DAYS = 28
WEIGHT_MIN_POINTS = 8
WEIGHT_MIN_SPAN_DAYS = 14

TOOLS = ("food_log_get", "garmin_get_daily_summaries", "garmin_get_activities", "hevy_get_workouts",
         "apple_health_get_body_composition")
MAX_FOOD_PAGES = 5
MAX_HEVY_PAGES = 3
HEVY_PAGE_SIZE = 10


class TargetsError(Exception):
    """goals.md has no usable nutrition-targets block."""


def parse_iso(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_day(text: Any) -> Optional[date]:
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rows(payload: Any, key: str) -> list:
    """AthleteData list tools answer with a bare array, or with an object
    carrying notes ahead of the rows under `key`."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [r for r in payload[key] if isinstance(r, dict)]
    return []


def local_time_label(moment: Optional[datetime], tz: ZoneInfo, reference_day: date) -> Optional[str]:
    """'19:42' for the reference day, 'yesterday 21:14', or '24 Oct 19:02'."""
    if moment is None:
        return None
    local = moment.astimezone(tz)
    if local.date() == reference_day:
        return local.strftime("%H:%M")
    if local.date() == reference_day - timedelta(days=1):
        return "yesterday " + local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b %H:%M')}"


# --------------------------------------------------------------------------
# Targets (goals.md is the single source of truth)
# --------------------------------------------------------------------------

_TARGETS_BLOCK = re.compile(r"```nutrition-targets\s*\n(.*?)\n```", re.DOTALL)


def load_targets(goals_path: Path) -> dict:
    try:
        text = goals_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TargetsError("goals.md is not readable") from exc
    match = _TARGETS_BLOCK.search(text)
    if not match:
        raise TargetsError("goals.md has no nutrition-targets block")
    try:
        targets = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TargetsError("goals.md nutrition-targets block is not valid JSON") from exc
    problems = validate_targets(targets)
    if problems:
        raise TargetsError("goals.md nutrition-targets block is invalid: " + "; ".join(problems))
    return targets


def _bands_valid(bands: Any) -> bool:
    if not isinstance(bands, list) or not bands:
        return False
    for i, band in enumerate(bands):
        if not isinstance(band, dict) or not isinstance(band.get("kcal"), int):
            return False
        last = i == len(bands) - 1
        if last != (band.get("max_minutes") is None):
            return False  # only the final band is open-ended
        if not last and not isinstance(band.get("max_minutes"), int):
            return False
    return True


def validate_targets(targets: Any) -> list:
    if not isinstance(targets, dict):
        return ["not an object"]
    problems = []
    kcal = targets.get("calories_kcal") or {}
    for key in ("rest", "strength"):
        if not isinstance(kcal.get(key), int):
            problems.append(f"calories_kcal.{key} missing")
    for key in ("run", "run_and_strength"):
        if not _bands_valid(kcal.get(key)):
            problems.append(f"calories_kcal.{key} bands invalid")
    protein = targets.get("protein_g") or {}
    if not all(isinstance(protein.get(k), int) for k in ("target", "min", "max")):
        problems.append("protein_g incomplete")
    fat = targets.get("fat_g") or {}
    if not all(isinstance(fat.get(k), int) for k in ("target", "min")):
        problems.append("fat_g incomplete")
    if not isinstance((targets.get("fiber_g") or {}).get("min"), int):
        problems.append("fiber_g.min missing")
    weight = targets.get("weight_change_kg_per_week") or {}
    if not all(isinstance(weight.get(k), (int, float)) for k in ("min", "max")):
        problems.append("weight_change_kg_per_week incomplete")
    return problems


def _band_kcal(bands: list, minutes: float) -> int:
    for band in bands:
        if band["max_minutes"] is None or minutes <= band["max_minutes"]:
            return band["kcal"]
    return bands[-1]["kcal"]


def day_targets(targets: dict, training: "DayTraining") -> dict:
    """Targets for one day type. A combined day uses its own bands, capped by
    design: total load is assessed, exercise calories are never added up."""
    kcal_table = targets["calories_kcal"]
    if training.day_type == "run_and_strength":
        kcal, basis = _band_kcal(kcal_table["run_and_strength"], training.run_minutes), "run_and_strength"
    elif training.day_type == "run":
        kcal, basis = _band_kcal(kcal_table["run"], training.run_minutes), "run"
    else:
        kcal, basis = kcal_table[training.day_type], training.day_type
    protein, fat = targets["protein_g"], targets["fat_g"]
    carbs = max(0, kcal - protein["target"] * 4 - fat["target"] * 9) / 4
    return {
        "status": targets.get("status"),
        "day_type_basis": basis,
        "run_minutes": round(training.run_minutes),
        "kcal": kcal,
        "protein_g": {"target": protein["target"], "range": [protein["min"], protein["max"]]},
        "fat_g": {"target": fat["target"], "min": fat["min"]},
        "fiber_g_min": targets["fiber_g"]["min"],
        "carbohydrate_g": int(round(carbs / 5) * 5),
        "weight_change_kg_per_week": [targets["weight_change_kg_per_week"]["min"],
                                      targets["weight_change_kg_per_week"]["max"]],
        "reassess_from": targets.get("reassess_from"),
    }


# --------------------------------------------------------------------------
# Food log (Cronometer via food_log_get)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DayLog:
    day: date
    slots: frozenset
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float
    entries: int
    quick_add_entries: int
    whole_day: bool

    @property
    def fiber_reliable(self) -> bool:
        return self.quick_add_entries == 0


@dataclass(frozen=True)
class FoodLog:
    days: dict                     # date -> DayLog (only days holding calories)
    last_sync: Optional[datetime]  # Cronometer's latest_data_received_at


def parse_food_log(payloads: list) -> FoodLog:
    """Merges food_log_get pages. Rows without calories (Cronometer's daily
    micronutrient row) and pre-logged (planned, not eaten) rows are not
    intake. A placeholder meal type is not evidence of a meal slot."""
    acc: dict = {}
    last_sync = None
    seen = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for provider in ((payload.get("sync") or {}).get("providers") or []):
            if isinstance(provider, dict) and provider.get("provider") == "cronometer":
                moment = parse_iso(provider.get("latest_data_received_at"))
                if moment and (last_sync is None or moment > last_sync):
                    last_sync = moment
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("pre_logged") is True:
                continue
            key = entry.get("id")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            kcal = _number(entry.get("calories"))
            day = parse_day(entry.get("date"))
            if day is None or not kcal or kcal <= 0:
                continue
            d = acc.setdefault(day, {"slots": set(), "kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0,
                                     "fiber": 0.0, "entries": 0, "quick": 0, "whole": False})
            meal = (entry.get("mealType") or entry.get("meal_type") or "").lower()
            if meal in MEAL_SLOTS and entry.get("meal_type_is_placeholder") is not True:
                d["slots"].add(meal)
            d["kcal"] += kcal
            d["protein"] += _number(entry.get("proteinGrams")) or 0.0
            d["carbs"] += _number(entry.get("carbsGrams")) or 0.0
            d["fat"] += _number(entry.get("fatGrams")) or 0.0
            fiber = _number(entry.get("fiberGrams"))
            d["fiber"] += fiber or 0.0
            d["entries"] += 1
            if kcal >= QUICK_ADD_MIN_KCAL and not fiber and not _number(entry.get("sodiumMg")):
                d["quick"] += 1
            if entry.get("whole_day_total") is True:
                d["whole"] = True
    days = {day: DayLog(day, frozenset(v["slots"]), v["kcal"], v["protein"], v["carbs"], v["fat"], v["fiber"],
                        v["entries"], v["quick"], v["whole"]) for day, v in acc.items()}
    return FoodLog(days, last_sync)


@dataclass(frozen=True)
class Pattern:
    logged_days: int
    expected_slots: tuple       # usual core slots, in meal order
    snack_usual: bool
    reference_kcal: Optional[float]  # median intake of substantial days


def personal_pattern(log: FoodLog, target: date) -> Pattern:
    """The athlete's usual logging pattern, learned only from substantial
    days (intake at least LOW_INTAKE_SHARE of the upper-quartile day), so
    partially logged days never lower the expectations they are judged by."""
    window = [log.days[d] for d in sorted(log.days)
              if target - timedelta(days=HISTORY_DAYS) <= d < target]
    if len(window) < MIN_HISTORY_DAYS:
        return Pattern(len(window), (), False, None)
    upper_quartile = statistics.quantiles([d.kcal for d in window], n=4)[2]
    substantial = [d for d in window if d.kcal >= LOW_INTAKE_SHARE * upper_quartile]
    if len(substantial) < MIN_HISTORY_DAYS:
        return Pattern(len(window), (), False, None)
    share = {slot: sum(1 for d in substantial if slot in d.slots) / len(substantial) for slot in MEAL_SLOTS}
    expected = tuple(s for s in CORE_SLOTS if share[s] >= CORE_SLOT_MIN_SHARE)
    reference = statistics.median(d.kcal for d in substantial) if expected else None
    return Pattern(len(window), expected, share["snack"] >= CORE_SLOT_MIN_SHARE, reference)


@dataclass(frozen=True)
class Completeness:
    status: str                 # complete | incomplete | missing | unknown
    reasons: tuple              # missing_slots, low_intake, no_entries, no_personal_pattern
    present_slots: tuple
    missing_slots: tuple
    snack_missing: bool         # usual snack not logged (mentioned only, never a reason on its own)
    kcal: float
    reference_kcal: Optional[float]
    last_sync: Optional[datetime]
    stale: bool
    sync_age_minutes: Optional[int]
    history_logged_days: int


def assess_completeness(log: FoodLog, target: date, now: datetime) -> Completeness:
    """Is the target day's log plausibly complete at `now`? Meal slots and
    intake are judged against the athlete's own recent pattern; three slots
    alone are not proof of completeness."""
    pattern = personal_pattern(log, target)
    day = log.days.get(target)
    present = tuple(s for s in MEAL_SLOTS if day and s in day.slots)
    kcal = day.kcal if day else 0.0
    age = None if log.last_sync is None else now - log.last_sync
    stale = age is None or age > STALE_AFTER
    age_minutes = None if age is None else max(0, int(age.total_seconds() // 60))
    missing = tuple(s for s in pattern.expected_slots if s not in present)
    snack_missing = pattern.snack_usual and "snack" not in present

    if day is None:
        status, reasons = "missing", ("no_entries",)
    elif day.whole_day:
        status, reasons, missing, snack_missing = "complete", (), (), False
    elif not pattern.expected_slots:
        status, reasons = "unknown", ("no_personal_pattern",)
    else:
        reasons = []
        if missing:
            reasons.append("missing_slots")
        if pattern.reference_kcal and kcal < LOW_INTAKE_SHARE * pattern.reference_kcal:
            reasons.append("low_intake")
        status, reasons = ("incomplete" if reasons else "complete"), tuple(reasons)
    return Completeness(status, reasons, present, missing, snack_missing, kcal, pattern.reference_kcal,
                        log.last_sync, stale, age_minutes, pattern.logged_days)


def day_log_complete(log: FoodLog, day: date) -> bool:
    """For finished past days: the usual core slots are present and intake is
    not implausibly low against the pattern before that day."""
    entry = log.days.get(day)
    if entry is None:
        return False
    if entry.whole_day:
        return True
    pattern = personal_pattern(log, day)
    if not pattern.expected_slots or not set(pattern.expected_slots) <= entry.slots:
        return False
    return not (pattern.reference_kcal and entry.kcal < LOW_INTAKE_SHARE * pattern.reference_kcal)


# --------------------------------------------------------------------------
# Garmin daily energy expenditure (garmin_get_daily_summaries)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GarminDay:
    day: date
    bmr_kcal: Optional[float]
    active_kcal: Optional[float]
    window_hours: float
    partial: bool

    @property
    def total_kcal(self) -> Optional[float]:
        if self.bmr_kcal is None or self.active_kcal is None:
            return None
        return self.bmr_kcal + self.active_kcal


def parse_daily_summaries(payload: Any) -> dict:
    """One row per calendar day: Garmin's resting (BMR) and active calories
    above resting. Activity rows are never read here, so workout calories
    cannot be added on top of the daily active figure."""
    days: dict = {}
    for row in _rows(payload, "records"):
        day = parse_day(row.get("calendarDate"))
        if day is None:
            continue
        window = _number(row.get("day_window_hours"))
        if window is None:
            duration = _number(row.get("durationInSeconds"))
            window = duration / 3600 if duration is not None else 0.0
        partial = row.get("is_partial_day") is True or window < GARMIN_DAY_HOURS - 0.5
        parsed = GarminDay(day, _number(row.get("bmrKilocalories")), _number(row.get("activeKilocalories")),
                           round(window, 2), partial)
        current = days.get(day)
        if current is None or parsed.window_hours > current.window_hours:
            days[day] = parsed
    return days


def assess_expenditure(garmin: dict, target: date, now: datetime, tz: ZoneInfo) -> dict:
    """Observed expenditure for the target day, plus a full-day projection
    only when the snapshot covers most of the day, is recent, and enough
    completed days exist to bound the remaining activity.

    Method: BMR accrues at a constant rate, so the full-day BMR is the
    observed BMR scaled to 24 h. Remaining active calories lie between none
    (low end) and the median completed-day active rate for the remaining
    hours (high end)."""
    row = garmin.get(target)
    completed = [g for d, g in sorted(garmin.items())
                 if target - timedelta(days=EXPENDITURE_HISTORY_DAYS) <= d < target
                 and not g.partial and g.total_kcal is not None]
    context = {
        "completed_days_used": len(completed),
        "completed_day_median_total_kcal": (round(statistics.median([g.total_kcal for g in completed]))
                                            if completed else None),
    }
    if row is None or row.total_kcal is None:
        return {"status": "missing", "projection": None, "projection_unavailable_reason": "no_garmin_data",
                **context}

    result = {
        "status": "partial" if row.partial else "completed",
        "observed": {"resting_kcal": round(row.bmr_kcal), "active_kcal": round(row.active_kcal),
                     "total_kcal": round(row.total_kcal), "covers_hours": row.window_hours},
        "projection": None,
        "projection_unavailable_reason": None,
        **context,
    }
    if not row.partial:
        return result

    # In UTC: aware arithmetic within one ZoneInfo is wall-clock arithmetic,
    # which is an hour off on DST-change days.
    midnight = datetime.combine(target, time(0), tzinfo=tz).astimezone(UTC)
    data_through = midnight + timedelta(hours=row.window_hours)
    result["observed"]["data_through_local"] = data_through.astimezone(tz).strftime("%H:%M")
    reason = None
    if row.window_hours < PROJECTION_MIN_WINDOW_HOURS:
        reason = "snapshot_covers_too_little_of_the_day"
    elif now - data_through > PROJECTION_MAX_LAG:
        reason = "snapshot_not_recent"
    elif len(completed) < PROJECTION_MIN_COMPLETED_DAYS:
        reason = "too_few_completed_days"
    if reason:
        result["projection_unavailable_reason"] = reason
        return result

    remaining = max(0.0, GARMIN_DAY_HOURS - row.window_hours)
    bmr_full = row.bmr_kcal * GARMIN_DAY_HOURS / row.window_hours
    rate = statistics.median([g.active_kcal for g in completed]) / GARMIN_DAY_HOURS
    low = bmr_full + row.active_kcal
    high = low + rate * remaining
    result["projection"] = {
        "label": "projected full-day total (not measured)",
        "low_kcal": round(low),
        "high_kcal": round(high),
        "remaining_hours": round(remaining, 1),
        "method": "resting calories scaled to 24 h; remaining activity between none and the median "
                  "completed-day active rate",
    }
    return result


# --------------------------------------------------------------------------
# Training context (Garmin activities, Hevy workouts)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DayTraining:
    day: date
    day_type: str            # rest | strength | run | run_and_strength
    run_minutes: float
    run_sessions: int
    strength_sessions: int
    other_activities: tuple  # Garmin activity types that are neither run nor strength
    complete: bool           # both training sources answered


def parse_garmin_activities(payload: Any, tz: ZoneInfo) -> list:
    acts = []
    for row in _rows(payload, "activities"):
        day = parse_day(row.get("local_date"))
        if day is None:
            start = _number(row.get("startTimeInSeconds"))
            if start is None:
                continue
            day = datetime.fromtimestamp(start, UTC).astimezone(tz).date()
        acts.append({"day": day, "type": str(row.get("activityType") or "UNKNOWN").upper(),
                     "minutes": (_number(row.get("durationInSeconds")) or 0.0) / 60})
    return acts


def parse_hevy_workouts(pages: list, tz: ZoneInfo) -> list:
    workouts, seen = [], set()
    for payload in pages:
        for row in _rows(payload, "workouts"):
            start = parse_iso(row.get("start_time"))
            if start is None or row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            end = parse_iso(row.get("end_time"))
            minutes = (end - start).total_seconds() / 60 if end and end > start else 0.0
            workouts.append({"day": start.astimezone(tz).date(), "minutes": minutes})
    return workouts


def _is_run(activity_type: str) -> bool:
    return "RUN" in activity_type


def _is_strength(activity_type: str) -> bool:
    return "STRENGTH" in activity_type


def classify_day(day: date, garmin_acts: list, hevy: list, *, garmin_ok: bool, hevy_ok: bool) -> DayTraining:
    runs = [a for a in garmin_acts if a["day"] == day and _is_run(a["type"])]
    garmin_strength = [a for a in garmin_acts if a["day"] == day and _is_strength(a["type"])]
    other = tuple(sorted({a["type"] for a in garmin_acts
                          if a["day"] == day and not _is_run(a["type"]) and not _is_strength(a["type"])}))
    hevy_sessions = [w for w in hevy if w["day"] == day]
    # data-source-policy.md: a generic Garmin strength activity is never a
    # second session when Hevy holds the detailed workout.
    strength = len(hevy_sessions) if hevy_sessions else len(garmin_strength)
    run_minutes = sum(a["minutes"] for a in runs)
    if runs and strength:
        day_type = "run_and_strength"
    elif runs:
        day_type = "run"
    elif strength:
        day_type = "strength"
    else:
        day_type = "rest"
    return DayTraining(day, day_type, run_minutes, len(runs), strength, other, garmin_ok and hevy_ok)


# --------------------------------------------------------------------------
# Body composition (Apple Health)
# --------------------------------------------------------------------------

def _dedupe_by_date(rows: list) -> "tuple[dict, list]":
    """data-source-policy.md: duplicates are never averaged. Identical
    same-date values count once; differing values exclude the date."""
    by_date: dict = {}
    for row in rows:
        day, value = parse_day(row.get("date")), _number(row.get("value"))
        if day is not None and value is not None:
            by_date.setdefault(day, set()).add(round(value, 3))
    values, conflicts = {}, []
    for day, vals in sorted(by_date.items()):
        if len(vals) == 1:
            values[day] = vals.pop()
        else:
            conflicts.append(day.isoformat())
    return values, conflicts


def _slope_per_week(points: dict) -> Optional[float]:
    days = sorted(points)
    xs = [(d - days[0]).days for d in days]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(points[d] for d in days)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (points[d] - mean_y) for x, d in zip(xs, days)) / denom
    return slope * 7


def trend(values: dict, target: date, scale: float = 1.0, decimals: int = 2) -> dict:
    window = {d: v * scale for d, v in values.items() if target - timedelta(days=WEIGHT_TREND_DAYS) < d <= target}
    days = sorted(window)
    span = (days[-1] - days[0]).days if days else 0
    result: dict = {"points": len(days), "span_days": span, "slope_per_week": None,
                    "last_7_day_mean": None, "previous_7_day_mean": None}
    last7 = [window[d] for d in days if d > target - timedelta(days=7)]
    prev7 = [window[d] for d in days if target - timedelta(days=14) < d <= target - timedelta(days=7)]
    if last7:
        result["last_7_day_mean"] = round(statistics.fmean(last7), decimals)
    if prev7:
        result["previous_7_day_mean"] = round(statistics.fmean(prev7), decimals)
    if len(days) >= WEIGHT_MIN_POINTS and span >= WEIGHT_MIN_SPAN_DAYS:
        slope = _slope_per_week(window)
        result["slope_per_week"] = None if slope is None else round(slope, decimals)
    result["sufficient"] = result["slope_per_week"] is not None
    return result


def body_composition(payload: Any, target: date, weight_range: Optional[list]) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    weights, weight_conflicts = _dedupe_by_date(_rows(payload, "weight"))
    fats, fat_conflicts = _dedupe_by_date(_rows(payload, "bodyFat"))
    weight = trend(weights, target)
    fat = trend(fats, target, scale=100.0, decimals=1)
    if weight["sufficient"] and weight_range:
        slope = weight["slope_per_week"]
        lo, hi = min(weight_range), max(weight_range)
        weight["versus_target"] = ("faster_loss_than_target" if slope < lo
                                   else "within_target" if slope <= hi else "slower_than_target")
    return {"weight_kg": weight, "body_fat_percent": fat, "note": "body fat is a noisy scale estimate",
            "conflicting_dates_excluded": sorted(set(weight_conflicts + fat_conflicts))}


# --------------------------------------------------------------------------
# Fetching (read-only AthleteData client)
# --------------------------------------------------------------------------

@dataclass
class Inputs:
    food_pages: list = field(default_factory=list)
    garmin_daily: Any = None
    garmin_activities: Any = None
    hevy_pages: list = field(default_factory=list)
    body: Any = None
    errors: dict = field(default_factory=dict)   # source -> error category
    empty: set = field(default_factory=set)      # sources that answered with prose (no rows)


def _error_category(exc: Exception) -> str:
    return getattr(exc, "category", None) or type(exc).__name__


def fetch_food_pages(client, start: date, end: date, tz_name: str) -> list:
    pages, cursor = [], None
    for _ in range(MAX_FOOD_PAGES):
        args = {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": tz_name, "limit": 1000}
        if cursor:
            args["cursor"] = cursor
        result = client.call_tool("food_log_get", args)
        if result.kind != "json" or not isinstance(result.payload, dict):
            raise adc.ProtocolError("food_log_get did not return JSON")
        pages.append(result.payload)
        cursor = result.payload.get("next_cursor")
        if not cursor:
            return pages
    raise adc.AthleteDataError("food log pagination exceeded the page limit", "incomplete_pagination")


def fetch_inputs(client, target: date, tz: ZoneInfo, *, sources: tuple = TOOLS) -> Inputs:
    """Each source is isolated: one failure is recorded, never fatal."""
    inputs = Inputs()
    try:
        checks = client.verify_tools(list(sources))
    except adc.AthleteDataError as exc:
        inputs.errors = {name: _error_category(exc) for name in sources}
        return inputs
    usable = {name for name, problem in checks.items() if problem is None}
    for name, problem in checks.items():
        if problem is not None:
            inputs.errors[name] = _error_category(problem)

    def call(name: str, args: dict) -> Any:
        result = client.call_tool(name, args)
        if result.kind != "json":
            inputs.empty.add(name)
            return None
        return result.payload

    tz_name = str(tz)
    steps = {
        "food_log_get": lambda: inputs.food_pages.extend(
            fetch_food_pages(client, target - timedelta(days=HISTORY_DAYS), target, tz_name)),
        "garmin_get_daily_summaries": lambda: setattr(inputs, "garmin_daily", call(
            "garmin_get_daily_summaries", {"start_date": (target - timedelta(days=EXPENDITURE_HISTORY_DAYS)).isoformat(),
                                           "end_date": target.isoformat(), "limit": 60})),
        "garmin_get_activities": lambda: setattr(inputs, "garmin_activities", call(
            "garmin_get_activities", {"start_date": (target - timedelta(days=EXPENDITURE_HISTORY_DAYS)).isoformat(),
                                      "end_date": target.isoformat(), "limit": 100})),
        "hevy_get_workouts": lambda: _fetch_hevy(client, inputs, target - timedelta(days=EXPENDITURE_HISTORY_DAYS), tz),
        "apple_health_get_body_composition": lambda: setattr(inputs, "body", call(
            "apple_health_get_body_composition", {"start_date": (target - timedelta(days=WEIGHT_TREND_DAYS + 7)).isoformat(),
                                                  "end_date": target.isoformat(), "limit": 300})),
    }
    for name in sources:
        if name not in usable:
            continue
        try:
            steps[name]()
        except adc.AthleteDataError as exc:
            inputs.errors[name] = _error_category(exc)
    return inputs


def _fetch_hevy(client, inputs: Inputs, oldest: date, tz: ZoneInfo) -> None:
    for page in range(1, MAX_HEVY_PAGES + 1):
        result = client.call_tool("hevy_get_workouts", {"page": page, "pageSize": HEVY_PAGE_SIZE})
        if result.kind != "json":
            inputs.empty.add("hevy_get_workouts")
            return
        inputs.hevy_pages.append(result.payload)
        workouts = parse_hevy_workouts([result.payload], tz)
        page_count = result.payload.get("page_count") if isinstance(result.payload, dict) else None
        if not workouts or min(w["day"] for w in workouts) < oldest \
                or (isinstance(page_count, int) and page >= page_count):
            return


# --------------------------------------------------------------------------
# Evening facts
# --------------------------------------------------------------------------

def _source_status(inputs: Inputs, name: str) -> str:
    if name in inputs.errors:
        return "error:" + inputs.errors[name]
    return "ok_no_rows" if name in inputs.empty else "ok"


def build_facts(inputs: Inputs, targets: Optional[dict], target: date, now: datetime, tz: ZoneInfo,
                targets_error: Optional[str] = None) -> dict:
    """Everything the Evening Nutrition Review may quote as numbers."""
    food_ok = "food_log_get" not in inputs.errors
    log = parse_food_log(inputs.food_pages) if food_ok else FoodLog({}, None)
    garmin = parse_daily_summaries(inputs.garmin_daily) if "garmin_get_daily_summaries" not in inputs.errors else {}
    acts_ok = "garmin_get_activities" not in inputs.errors
    hevy_ok = "hevy_get_workouts" not in inputs.errors
    acts = parse_garmin_activities(inputs.garmin_activities, tz) if acts_ok else []
    hevy = parse_hevy_workouts(inputs.hevy_pages, tz) if hevy_ok else []

    training = classify_day(target, acts, hevy, garmin_ok=acts_ok, hevy_ok=hevy_ok)
    day_target = day_targets(targets, training) if targets else None
    completeness = assess_completeness(log, target, now) if food_ok else None
    expenditure = (assess_expenditure(garmin, target, now, tz)
                   if "garmin_get_daily_summaries" not in inputs.errors
                   else {"status": "unavailable", "projection": None,
                         "projection_unavailable_reason": "garmin_fetch_failed"})

    today_log = log.days.get(target)
    food_today: dict = {"available": food_ok}
    if food_ok:
        food_today.update({
            "as_of_local": now.astimezone(tz).strftime("%H:%M"),
            "kcal": round(today_log.kcal) if today_log else 0,
            "protein_g": round(today_log.protein_g) if today_log else 0,
            "carbohydrate_g": round(today_log.carbs_g) if today_log else 0,
            "fat_g": round(today_log.fat_g) if today_log else 0,
            "fiber_g": round(today_log.fiber_g, 1) if today_log else 0,
            "fiber_reliable": today_log.fiber_reliable if today_log else False,
            "quick_add_entries_without_nutrient_detail": today_log.quick_add_entries if today_log else 0,
            "log_status": completeness.status,
            "log_status_reasons": list(completeness.reasons),
            "meal_slots_present": list(completeness.present_slots),
            "usual_meal_slots_missing": list(completeness.missing_slots),
            "usual_fully_logged_day_kcal": (round(completeness.reference_kcal)
                                            if completeness.reference_kcal else None),
            "last_cronometer_sync_local": local_time_label(completeness.last_sync, tz, target),
            "sync_stale": completeness.stale,
        })

    balance_today: dict = {"available": False}
    projection = expenditure.get("projection")
    if completeness is None or completeness.status != "complete":
        balance_today["reason"] = "food_log_not_complete"
    elif projection is None:
        balance_today["reason"] = "no_defensible_expenditure_projection"
    else:
        balance_today = {
            "available": True,
            "label": "projected, provisional: intake so far minus projected full-day expenditure",
            "low_kcal": round(completeness.kcal - projection["high_kcal"]),
            "high_kcal": round(completeness.kcal - projection["low_kcal"]),
        }

    recent, balances, intake_days = [], [], []
    for offset in range(RECENT_DAYS, 0, -1):
        day = target - timedelta(days=offset)
        entry = log.days.get(day)
        complete = food_ok and day_log_complete(log, day)
        g = garmin.get(day)
        g_total = g.total_kcal if g and not g.partial else None
        row = {
            "date": day.isoformat(),
            "day_type": classify_day(day, acts, hevy, garmin_ok=acts_ok, hevy_ok=hevy_ok).day_type,
            "food_log": "complete" if complete else ("partial" if entry else "none"),
            "intake_kcal": round(entry.kcal) if entry else None,
            "protein_g": round(entry.protein_g) if entry else None,
            "garmin_total_kcal": round(g_total) if g_total is not None else None,
        }
        if complete:
            intake_days.append(entry)
            if g_total is not None:
                row["energy_balance_kcal"] = round(entry.kcal - g_total)
                balances.append(entry.kcal - g_total)
        recent.append(row)

    trends = {"complete_log_days": len(intake_days), "of_days": RECENT_DAYS}
    if intake_days:
        trends.update({
            "mean_intake_kcal_complete_days": round(statistics.fmean(d.kcal for d in intake_days)),
            "mean_protein_g_complete_days": round(statistics.fmean(d.protein_g for d in intake_days)),
            "days_protein_at_or_above_min": (sum(1 for d in intake_days
                                                 if d.protein_g >= targets["protein_g"]["min"])
                                             if targets else None),
        })
    if len(balances) >= 3:
        trends["mean_energy_balance_kcal_completed_days"] = round(statistics.fmean(balances))
        trends["energy_balance_days_used"] = len(balances)

    return {
        "local_date": target.isoformat(),
        "computed_at_local": now.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
        "sources": {name: _source_status(inputs, name) for name in TOOLS},
        "targets": day_target if day_target else {"unavailable": targets_error or "targets not loaded"},
        "training_today": {**{k: v for k, v in asdict(training).items() if k != "day"},
                           "run_minutes": round(training.run_minutes)},
        "food_today": food_today,
        "garmin_expenditure_today": expenditure,
        "energy_balance_today": balance_today,
        "recent_days": recent,
        "recent_trends": trends,
        "body_composition": (body_composition(inputs.body, target,
                                              day_target["weight_change_kg_per_week"] if day_target else None)
                             if "apple_health_get_body_composition" not in inputs.errors else {"unavailable": True}),
    }
