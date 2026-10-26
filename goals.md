# Goals

*This document is expected to evolve over time. Update it whenever priorities change rather than treating it as fixed.*

## Primary Long-Term Goals

- Build and maintain a high level of lifelong fitness.
- Become a stronger and faster hybrid athlete.
- Maximize health, performance, and longevity together.
- Train consistently for years without chronic injury.

## Current Training Goals

Confirmed by the athlete on 2026-10-26, to be balanced against each other rather than pursued one at a time:

1. Sustainably reduce body-fat percentage.
2. Preserve or increase muscle mass.
3. Improve strength and progressive overload in the gym, using the current double-progression system.
4. Improve running performance and endurance.

Build sustainable habits that can be maintained long term. The Performance Priorities below still decide genuine conflicts: fat loss is pursued only at a rate that does not undermine health, recovery, muscle preservation, strength progression or running performance.

## Secondary Goals

These are valuable but should never override the primary goals above.

- Improve recovery quality.
- Improve nutrition consistency.
- Improve body composition gradually.
- Continue learning and refining training using objective data.

## Performance Priorities

When trade-offs exist between goals, decisions should follow this order:

1. Long-term health always comes first.
2. Avoiding injury takes priority over short-term performance.
3. Running goals take priority over strength goals when they directly conflict.
4. Strength training should support running rather than compromise it.
5. Nutrition and recovery should support both rather than become goals in themselves.

## Success Metrics

Progress should be assessed from the objective data already established as authoritative in `data-source-policy.md`, rather than from subjective impression:

- **Running performance** — pace and heart-rate trends at comparable efforts, tracked via Garmin.
- **Strength progression** — working weight and rep progression per exercise under the double-progression system, tracked via Hevy.
- **Body composition** — direction of the weight and body-fat trend over time (not single-day readings), tracked via Apple Health.
- **Consistency** — adherence of logged training sessions to what was planned, over weeks and months rather than single days.
- **Recovery** — stability or improvement in resting heart rate and stress trends, tracked via Garmin.
- **Nutrition consistency** — frequency of fully logged days meeting the Nutrition Targets below, tracked via Cronometer.

## Nutrition Targets (provisional)

Approved by the athlete on 2026-10-26 as starting targets. This section is the single source of truth for nutrition targets; other documents and the runner read them from here.

- **Calories by day type:** rest day 2,300 kcal; strength day 2,450 kcal; running day 2,600–2,850 kcal by run duration (up to 45 min: 2,600; up to 75 min: 2,700; longer: 2,850). A day with both running and strength is judged on total training load, not by adding exercise calories: 2,700 kcal with up to 45 min of running, otherwise 2,850.
- **Protein:** 160 g/day; practical range 150–170 g.
- **Fat:** about 65 g/day; minimum around 60 g.
- **Fiber:** at least 30 g/day, judged only when the log carries reliable fiber data.
- **Carbohydrates:** the remaining calories, higher on running days.
- **Body weight:** a provisional loss of about 0.2–0.35 kg/week, read from the multi-week trend, never from single weigh-ins.

Reassess after 2–3 weeks (from 2026-11-09, by 2026-11-16) using sufficiently complete nutrition logs, the body-weight trend and training performance. Health, recovery, muscle preservation, strength progression and running performance come before the rate of loss; an aggressive deficit that undermines training is never the goal.

Machine-readable copy of the targets above (read by `runner/nutrition_data.py`; keep the two in step):

```nutrition-targets
{
  "version": 1,
  "status": "provisional",
  "approved": "2026-10-26",
  "reassess_from": "2026-11-09",
  "reassess_by": "2026-11-16",
  "calories_kcal": {
    "rest": 2300,
    "strength": 2450,
    "run": [
      {"max_minutes": 45, "kcal": 2600},
      {"max_minutes": 75, "kcal": 2700},
      {"max_minutes": null, "kcal": 2850}
    ],
    "run_and_strength": [
      {"max_minutes": 45, "kcal": 2700},
      {"max_minutes": null, "kcal": 2850}
    ]
  },
  "protein_g": {"target": 160, "min": 150, "max": 170},
  "fat_g": {"target": 65, "min": 60},
  "fiber_g": {"min": 30},
  "carbohydrate_g": "remainder",
  "weight_change_kg_per_week": {"min": -0.35, "max": -0.2}
}
```

## Review Process

This document should be reviewed and updated whenever priorities change — for example, after a shift in what the athlete is training toward, a change in life circumstances affecting availability or recovery capacity, or a deliberate re-ranking of the priorities above. It should not be treated as a one-time record.
