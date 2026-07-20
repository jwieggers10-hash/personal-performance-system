# Data Source Policy

## Authoritative sources

- Garmin is the source of truth for running activities, heart rate, sleep,
  resting heart rate, stress, Body Battery and other available recovery data.
- Hevy is the source of truth for strength exercises, sets, repetitions,
  loads and progression.
- Cronometer is the source of truth for calories, protein, carbohydrates,
  fat, fiber and other nutrition data.
- FeelFit through Apple Health is the source of truth for body weight and
  body-composition measurements.
- Strava is for social and activity-sharing use. Do not use a Strava copy
  when the original Garmin activity is available.

## Duplicate handling

- Never count both Garmin and Strava copies of the same run.
- Never count a generic Garmin strength activity as a second strength
  workout when the detailed Hevy workout represents the same session.
- Never average duplicate weight measurements from multiple apps.
- Prefer the authoritative source listed above.
- Flag possible duplicates rather than silently deciding when uncertain.

## Data integrity

- Never invent missing measurements.
- Clearly identify unavailable metrics.
- Treat wearable energy expenditure as an estimate.
- Treat FeelFit body-fat percentage as a noisy trend, not an exact measure.
