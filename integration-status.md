## Garmin MCP Verification

Status:
PASS (Operational)

Summary:
The Garmin integration is successfully connected through AthleteData MCP and read-only access has been verified.

Verified capabilities:
- Recent running activities can be retrieved.
- Activity summaries include distance, duration, pace, heart rate, cadence, elevation and device information.
- Recovery metrics including resting heart rate, stress and Body Battery can be retrieved.
- Real Garmin activity identifiers are available.

Current MCP limitations observed:
- Split/lap data is not consistently exposed.
- garmin_get_activity does not currently return the detailed splits described by its documentation.
- garmin_get_activity_file reports lap counts but returns empty lap objects.
- Sleep data may contain multiple tentative snapshots instead of a single finalized record.
- Sleep score, sleep HRV and sleep SpO₂ were not available through the current MCP responses.

Impact:
These limitations do not prevent running analysis or coaching.
Advanced metrics should be treated as optional until AthleteData exposes them consistently.

Next action:
Continue Phase 1 by testing the Hevy integration.

## Hevy MCP Verification

Status:
PASS (Operational)

Summary:
The Hevy integration is successfully connected through AthleteData MCP and read-only access has been verified. The latest completed workout was retrieved with full exercise, set, weight, and repetition detail.

Verified capabilities:
- The latest completed workout can be retrieved with title, identifier, start/end time, and linked routine.
- The full exercise list is returned in order, with exercise names and stable template identifiers.
- Every working set includes weight and repetitions, sufficient for the athlete's 2-set / 5-10-rep progression system.
- Workout notes and routine linkage are retrievable.

Known limitations:
- RPE is not logged on any set.
- A gap of roughly 12 hours exists between a workout's session time and its created/updated timestamps; the linked routine's own update timestamp lands within milliseconds of the workout's creation timestamp.
- A note attached to an exercise at the routine level did not carry over to the completed workout record.
- No explicit "source" field is present in the payload; source is inferred from which tool responded.

Root cause of limitations:
RPE absence and the timestamp gap reflect the athlete's own logging behavior (Hevy supports delayed/manual entry), not an AthleteData defect. The missing "source" field is an AthleteData response-shape characteristic.

Impact on Phase 2:
None of these block strength-progression tracking under the athlete's stated system, since weight, reps, and set counts are reliably present and linked to a stable exercise identifier.

Recommendation:
Treat RPE as unavailable rather than assumed zero. Do not rely on Hevy timestamps for same-day chronological ordering against other sources.

## Cronometer MCP Verification

Status:
PASS (Operational) — re-verified 2026-10-26 (Phase 5A/5B); Phase 1 rated it PARTIAL.

Summary:
Cronometer nutrition arrives through AthleteData's `food_log_get` (via Terra). Historical days are now retrievable: weekly aggregates back to late July 2026 and full entries for any requested range, with the day boundaries taken from the `timezone` argument (Europe/Amsterdam). The Phase 1 finding that only the current day held data no longer applies.

Verified capabilities:
- Per meal-slot entries (breakfast, lunch, dinner, snack) with calories, protein, carbohydrate, fat, and usually fiber and sodium; per-day totals; weekly aggregates over logged days.
- A daily micronutrient row per day (no calories) carrying the full Cronometer micronutrient panel.
- `sync.providers[].latest_data_received_at`: when Cronometer data last reached AthleteData.

Known limitations:
- Foods in one meal slot are merged into one entry; meal times are placeholders (midnight), so meal timing cannot be analysed.
- Sync has no fixed cadence. Over 13–26 Oct 2026 each day's entries arrived in one batch, mostly 18:30–20:30 local, once at 21:32; anything logged after the last sync is absent.
- Quick-add entries carry calories and macros but no fiber, sodium or micronutrients, understating those totals for the day.
- Logging completeness varies (in the last 13 finished days of the 5A sample: 9 fully logged, 3 partial, 1 empty). A missing or short day is a logging gap, not evidence of low intake.
- Cronometer's own targets are not exposed; approved targets live in `goals.md`.

Impact:
Daily and multi-day nutrition analysis is supported when the day's log is complete. Completeness must be judged per day (`runner/nutrition_data.py`), and trends use fully logged days only.

## Garmin Daily Expenditure Verification

Status:
PASS (Operational) — verified 2026-10-26 (Phase 5B) via `garmin_get_daily_summaries`.

Verified capabilities:
- Per calendar day: `bmrKilocalories` (resting) and `activeKilocalories` (above resting, the watch's all-day tally); total = resting + active.
- `day_window_hours` and `is_partial_day`: a day in progress is a cumulative snapshot covering only part of the day (for example 19.1 of 24 hours at 20:00 local).

Known limitations:
- Today's figures are never final before midnight; they cannot be compared with a full day's intake as if covering the same period.
- An activity's `activeKilocalories` includes resting calories during the session and overlaps the daily active figure; activity and daily calories are never added together.
- Strength sessions logged in Hevy are not recorded as Garmin activities; their effort appears only in the all-day active figure, as far as the watch measured it.
- Expenditure is a device estimate, not a measurement.

## Apple Health MCP Verification

Status:
PARTIAL

Summary:
The Apple Health integration returns real, recent body-weight and body-fat records, but cannot confirm FeelFit specifically as the originating app, and contains unresolved duplicate entries.

Verified capabilities:
- Recent body-weight and body-fat records are retrievable over a 30-day window.
- Weight and body-fat records can be paired by date for the large majority of dates.
- No conflicting (differing) values were found on any date.

Known limitations:
- No source-app or device field is present, so FeelFit cannot be confirmed as the originating app for any record.
- Two dates contain duplicate records (identical value, different UUID) that remain unresolved.
- One date has a weight record with no corresponding body-fat record.
- Timestamps are date-only, with no time-of-day precision.

Root cause of limitations:
The missing source-app field is an AthleteData MCP limitation; Apple HealthKit itself tracks a source per sample, but this tool does not expose it. Duplicate records cannot be attributed to HealthKit or to AthleteData's sync layer without that field.

Impact on Phase 2:
Body-composition trend tracking is usable if duplicates are excluded (not averaged) per record. FeelFit-specific attribution should not be assumed for any individual record.

Recommendation:
Deduplicate by (date, value) rather than averaging when computing trends. Revisit FeelFit-specific attribution if AthleteData adds a source field in the future.

Update (2026-10-26, Phase 5A):
- Records now carry a time of day (`startDate`); morning weigh-ins are consistent (about 37 in 60 days).
- One conflicting same-date duplicate was observed (two body-fat values 9 s apart on 2026-09-22). Conflicting same-date values are excluded, never averaged; identical duplicates count once.
