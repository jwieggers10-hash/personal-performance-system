# Athlete Profile

## Athlete Overview

The athlete trains in running and strength training, with data logged across Garmin, Hevy, Cronometer, and Apple Health. These sources are aggregated through the AthleteData MCP integration, which was verified during Phase 1.

## Current Sports

- Running
- Strength training

## Data Sources

The following authoritative sources were established in `data-source-policy.md` and confirmed operational during Phase 1 integration testing:

- **Garmin** — source of truth for running activities, heart rate, sleep, resting heart rate, stress, Body Battery, and other available recovery data.
- **Hevy** — source of truth for strength exercises, sets, repetitions, loads, and progression.
- **Cronometer** — source of truth for calories, protein, carbohydrates, fat, fiber, and other nutrition data.
- **Apple Health (FeelFit)** — source of truth for body weight and body-composition measurements.

Duplicate-handling rules established in `data-source-policy.md`:

- Never count both Garmin and Strava copies of the same run.
- Never count a generic Garmin strength activity as a second strength workout when the detailed Hevy workout represents the same session.
- Never average duplicate weight measurements from multiple apps.
- Prefer the authoritative source listed above.
- Flag possible duplicates rather than silently deciding when uncertain.

## Current Training Approach

Phase 1 testing sampled a limited recent data window rather than a full training history. Within that window, running sessions ranging from approximately 5–14 km were recorded in Garmin, and strength sessions logged in Hevy were organized into named routines (e.g., an upper-body-focused session). This reflects only what was observed in the sampled window, not a confirmed ongoing schedule or frequency.

## Available Metrics

Data confirmed available through AthleteData spans:

- Running (activity summaries, pace, heart rate, and related metrics)
- Strength training (exercises, sets, reps, and load)
- Body composition (weight and body-fat trend data)
- Nutrition (daily calorie and macronutrient totals)
- Recovery metrics (resting heart rate, stress, sleep, Body Battery)

## Known Data Limitations

Documented in full in `integration-status.md`. In summary:

- Garmin: per-lap/split detail and some sleep metrics (sleep score, HRV, SpO2) are not reliably available through current integration tools.
- Hevy: set-level RPE is not recorded, and workout timestamps may reflect delayed logging rather than real-time tracking.
- Cronometer: only current-day nutrition data was confirmed available; historical nutrition data could not be retrieved during testing.
- Apple Health: the originating app (FeelFit specifically, versus another app writing to Apple Health) cannot be confirmed from available data, and occasional duplicate same-date entries were observed.

## Coaching Assumptions

Based on verified Phase 1 results, the coaching system may safely assume:

- Recent running data (distance, duration, pace, heart rate) retrieved from Garmin is reliable.
- Recent strength session data (exercises, sets, reps, load) retrieved from Hevy is reliable.
- Current-day nutrition totals from Cronometer are reliable when present; historical nutrition trends should not be assumed available without re-verification.
- Recent body-weight and body-fat data from Apple Health is usable for trend tracking provided duplicate same-date entries are excluded rather than averaged.
- Any data field not explicitly confirmed as available during Phase 1 should be treated as unverified rather than assumed present.
