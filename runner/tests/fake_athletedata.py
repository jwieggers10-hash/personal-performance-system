"""Synthetic AthleteData MCP server for tests. Shapes follow the structures
verified in Phase 4.1 (Garmin activities, Hevy workout events); Apple Health
body composition follows the documented {weight: [...], bodyFat: [...]}
shape, which has not yet been observed with real records. Every value and
credential here is synthetic."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

SYNTHETIC_KEY = "sk_soma_SYNTHETICkey1234567890"
SYNTHETIC_URL = f"https://mcp.example.test/mcp?apiKey={SYNTHETIC_KEY}"


def tool(name, read_only=True, destructive=False):
    return {"name": name, "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive}}


DEFAULT_TOOLS = [
    tool("garmin_get_activities"),
    tool("hevy_get_workout_events"),
    tool("apple_health_get_body_composition"),
    tool("hevy_create_workout", read_only=False),
]


def text_result(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def prose_result(text):
    return {"content": [{"type": "text", "text": text}]}


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def garmin_activity(activity_id, local_date, start, *, duration=3600, activity_type="RUNNING", name="Easy Run",
                    distance=10000.0):
    return {
        "activityId": activity_id, "summaryId": str(activity_id * 10), "activityType": activity_type,
        "activityName": name, "startTimeInSeconds": int(start.timestamp()), "startTimeOffsetInSeconds": 7200,
        "durationInSeconds": duration, "distanceInMeters": distance, "local_date": local_date,
        "local_weekday": "Monday", "averageHeartRateInBeatsPerMinute": 141, "deviceName": "synthetic-watch",
        "userId": "00000000-0000-0000-0000-000000000000",
    }


def hevy_updated(workout_id, start, end, updated, *, title="Push Day", reps=8):
    return {"type": "updated", "workout": {
        "id": workout_id, "title": title, "description": "", "routine_id": "routine-synthetic",
        "start_time": start.isoformat(), "end_time": end.isoformat(),
        "created_at": _z(end), "updated_at": _z(updated),
        "exercises": [{"index": 0, "title": "Bench Press", "exercise_template_id": "TPL1",
                       "sets": [{"index": 0, "type": "normal", "reps": reps, "weight_kg": 60.0}]}],
    }}


def hevy_deleted(workout_id, deleted_at):
    return {"type": "deleted", "id": workout_id, "deleted_at": _z(deleted_at)}


class FakeAthleteData:
    """Callable with the AthleteDataClient http_post signature."""

    def __init__(self, *, sse=True):
        self.sse = sse
        self.tools = list(DEFAULT_TOOLS)
        self.garmin: list = []
        self.garmin_truncated = False
        self.hevy: list = []
        self.apple = None  # None -> prose "no data" reply, like production
        self.failures: dict = {}  # tool name -> list of HTTP statuses for successive calls
        self.raw_tool_results: dict = {}  # tool name -> result dict override
        self.calls: list = []  # (tool name, arguments)
        self.requests: list = []  # (method, headers)

    def __call__(self, url, body, headers, timeout):
        message = json.loads(body)
        method = message.get("method")
        self.requests.append((method, dict(headers)))
        if "id" not in message:
            return 202, {}, ""
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "athletedata", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"]["arguments"]
            self.calls.append((name, arguments))
            pending = self.failures.get(name)
            if pending:
                status = pending.pop(0)
                return status, {"retry-after": "1"} if status == 429 else {}, ""
            if name in self.raw_tool_results:
                result = self.raw_tool_results[name]
            else:
                result = getattr(self, "_" + name)(arguments)
        else:
            return 404, {}, ""
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if self.sse:
            return 200, {"content-type": "text/event-stream"}, f"event: message\ndata: {json.dumps(payload)}\n\n"
        return 200, {"content-type": "application/json"}, json.dumps(payload)

    def calls_for(self, name):
        return [arguments for tool_name, arguments in self.calls if tool_name == name]

    def _garmin_get_activities(self, arguments):
        start, end = arguments["start_date"], arguments["end_date"]
        rows = [a for a in self.garmin if start <= a["local_date"] <= end]
        return text_result({"coverage_gap": "synthetic note", "kcal_note": "synthetic note",
                            "activities": rows, **({"truncated": True} if self.garmin_truncated else {})})

    def _hevy_get_workout_events(self, arguments):
        since = datetime.fromisoformat(arguments["since"].replace("Z", "+00:00"))

        def stamp(event):
            raw = event["workout"]["updated_at"] if event["type"] == "updated" else event["deleted_at"]
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))

        matching = [e for e in self.hevy if stamp(e) >= since]
        size = int(arguments.get("pageSize", 10))
        page = int(arguments.get("page", 1))
        page_count = math.ceil(len(matching) / size)
        return text_result({"page": page, "page_count": page_count,
                            "events": matching[(page - 1) * size: page * size]})

    def _apple_health_get_body_composition(self, arguments):
        if self.apple is None:
            return prose_result("No Apple Health body composition data in that window. Synthetic notice.")
        return text_result(self.apple)
