"""Synthetic fixtures for the Phase 5B nutrition tests: Cronometer food-log
pages, Garmin daily summaries and activities, Hevy workouts, Apple Health
body composition, and a scripted read-only AthleteData client. Every value is
synthetic; nothing contacts AthleteData, Claude or Telegram."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import athletedata_client as adc  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 26)  # a Monday, CET


def local(text: str) -> datetime:
    """'2026-10-26 20:00' in Europe/Amsterdam, as an aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


_ids = iter(range(1, 10 ** 6))


def food_entry(day: date, meal: str, kcal, *, protein=30.0, carbs=60.0, fat=15.0, fiber=6.0, sodium=300.0,
               **extra) -> dict:
    return {"id": f"syn-{next(_ids)}", "source": "cronometer", "date": day.isoformat(), "mealType": meal,
            "calories": kcal, "proteinGrams": protein, "carbsGrams": carbs, "fatGrams": fat,
            "fiberGrams": fiber, "sodiumMg": sodium, **extra}


def micronutrient_row(day: date) -> dict:
    """Cronometer's daily micronutrient row: no calories, not a meal."""
    return {"id": f"syn-{next(_ids)}", "source": "cronometer", "date": day.isoformat(), "mealType": "snack",
            "calories": None, "proteinGrams": None, "waterMl": 1500.0}


def food_page(entries: list, last_sync: datetime = None, next_cursor: str = None) -> dict:
    providers = [] if last_sync is None else [{
        "provider": "cronometer", "latest_data_received_at": last_sync.astimezone(UTC).isoformat(),
        "last_logged_date": None}]
    page = {"sync": {"providers": providers}, "entries": entries, "daily_totals": [],
            "total_entries": len(entries)}
    if next_cursor:
        page["next_cursor"] = next_cursor
    return page


USUAL = (("breakfast", 500), ("lunch", 650), ("dinner", 900), ("snack", 350))  # 2,400 kcal


def usual_day(day: date, slots=USUAL, protein_per_slot=40.0) -> list:
    return [food_entry(day, meal, kcal, protein=protein_per_slot) for meal, kcal in slots]


def history(target: date, days: int = 14, slots=USUAL, skip=()) -> list:
    entries = []
    for offset in range(1, days + 1):
        day = target - timedelta(days=offset)
        if day not in skip:
            entries += usual_day(day, slots) + [micronutrient_row(day)]
    return entries


def garmin_day(day: date, *, bmr=2233, active=500, window=24.0) -> dict:
    row = {"calendarDate": day.isoformat(), "bmrKilocalories": bmr, "activeKilocalories": active,
           "day_window_hours": window, "is_partial_day": window < 24.0,
           "durationInSeconds": int(window * 3600)}
    return row


def garmin_history(target: date, days: int = 14, **kwargs) -> list:
    return [garmin_day(target - timedelta(days=o), **kwargs) for o in range(1, days + 1)]


def garmin_activity(day: date, activity_type="RUNNING", minutes=45, active_kcal=600) -> dict:
    return {"activityType": activity_type, "durationInSeconds": minutes * 60, "local_date": day.isoformat(),
            "activeKilocalories": active_kcal}


def hevy_workout(workout_id: str, start: datetime, minutes: int = 60) -> dict:
    return {"id": workout_id, "title": "Synthetic Session",
            "start_time": start.astimezone(UTC).isoformat(),
            "end_time": (start + timedelta(minutes=minutes)).astimezone(UTC).isoformat(), "exercises": []}


def hevy_page(workouts: list, page=1, page_count=1) -> dict:
    return {"page": page, "page_count": page_count, "workouts": workouts}


def body_payload(weights: dict, fats: dict = None) -> dict:
    """{date: value} maps; a list value yields several same-date rows."""
    def rows(values, kind):
        out = []
        for day, value in values.items():
            for v in (value if isinstance(value, list) else [value]):
                out.append({"date": day.isoformat(), "type": kind, "uuid": f"syn-{next(_ids)}", "value": v})
        return out
    return {"weight": rows(weights, "body_mass"), "bodyFat": rows(fats or {}, "body_fat")}


class FakeClient:
    """Scripted stand-in for AthleteDataClient: responses map a tool name to
    a payload, a callable(arguments) -> payload, a ToolResult, or an
    exception to raise. Unlisted tools are reported unavailable."""

    def __init__(self, responses: dict, verify_error: Exception = None):
        self.responses = responses
        self.verify_error = verify_error
        self.calls: list = []

    WRITE_TOOLS = {"food_log_entry": {"readOnlyHint": False, "destructiveHint": False},
                   "food_log_delete": {"readOnlyHint": False, "destructiveHint": True},
                   "push_workout_to_watch": {}}  # unannotated: never treated as read-only

    def list_tools(self):
        if self.verify_error:
            raise self.verify_error
        tools = {n: {"name": n, "annotations": {"readOnlyHint": True, "destructiveHint": False}}
                 for n in self.responses}
        tools.update({n: {"name": n, "annotations": a} for n, a in self.WRITE_TOOLS.items()})
        return tools

    def verify_tools(self, names):
        if self.verify_error:
            raise self.verify_error
        return {n: None if n in self.responses else adc.ToolUnavailableError("not offered") for n in names}

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        response = self.responses[name]
        if callable(response) and not isinstance(response, adc.ToolResult):
            response = response(arguments)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, adc.ToolResult):
            return response
        return adc.ToolResult("json", response)


def prose(length: int = 40) -> adc.ToolResult:
    return adc.ToolResult("text", None, text_length=length)
