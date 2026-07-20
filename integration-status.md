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
PARTIAL

Summary:
The Cronometer integration is connected and returns correctly attributed nutrition data, but no complete day of historical nutrition data could be retrieved; only the current in-progress day contained real entries.

Verified capabilities:
- Records are explicitly attributed to Cronometer via a source field.
- Daily calorie, protein, carbohydrate, fat, and fiber totals are populated and internally consistent with entry-level data when present.
- Stable entry identifiers are returned.

Known limitations:
- No real nutrition data was found for any date across a roughly 2.5-month test window; only a zero-valued placeholder record existed for the day before the current day.
- Multiple foods within a single meal are merged into one combined record rather than itemized individually.
- Timestamps default to midnight rather than reflecting actual meal times.

Root cause of limitations:
Undetermined. This could be an athlete logging gap (no meals recorded in Cronometer during the tested window) or a Terra-bridge sync/backfill gap. It cannot be distinguished from AthleteData responses alone.

Impact on Phase 2:
Historical nutrition trend analysis should be considered provisional until historical Cronometer data is confirmed. Daily nutrition analysis works when data is present, but long-term nutrition trends should not be relied upon until the historical data gap is understood.

Recommendation:
Confirm directly in the Cronometer app whether historical entries exist for the tested window before relying on this integration for nutrition trends.

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
