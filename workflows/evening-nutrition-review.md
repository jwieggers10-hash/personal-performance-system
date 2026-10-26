# Evening Nutrition Review Workflow

## Purpose

The Evening Nutrition Review assesses the current day's logged nutrition against the approved targets, the day's training and Garmin energy expenditure, and recent trends, and gives one concrete improvement for tomorrow. It supports the four current goals in `goals.md` together: sustainable fat loss, muscle preservation, strength progression and running performance.

It is advisory (`coaching-authority.md`, Nutrition): the athlete decides what to eat, and compliance is never assumed.

---

## Workflow Identity

- Workflow name: `evening-nutrition-review`
- Human title: `Evening Nutrition Review`
- Run identity: athlete local date plus workflow name
- Review path: `reviews/nutrition/YYYY-MM-DD.md` (excluded from Git)
- Started only through `runner/evening_nutrition_review.py`, scheduled 21:30 Europe/Amsterdam

---

## Execution Modes

- **Automated** (scheduled): no athlete input; uses the runner-computed facts and retrieved data only.
- **Manual**: same contract; any athlete-provided context is confirmed subjective feedback, not sensor data.

---

## Governing Documents

Before analysis, read:

- `athlete-identity.md`
- `goals.md`
- `coaching-authority.md`
- `nutrition-philosophy.md`
- `recovery-policy.md`
- `decision-rules.md`
- `constraints.md`
- `data-source-policy.md`
- `integration-status.md`
- `lessons-learned.md`

Targets come only from the Nutrition Targets section of goals.md, as resolved for today in the facts.

---

## Runner-Computed Facts

The prompt contains a JSON facts block computed by the runner from read-only AthleteData immediately before this run. Its numbers are authoritative: quote them as given and never recompute, re-total or replace them.

- `targets` — today's targets for the classified day type (`day_type_basis`). If it says `unavailable`, state that no targets could be loaded and make no target comparison.
- `training_today` — day type from Hevy (strength) and Garmin (running). A Garmin strength activity is never a second session when Hevy holds it. If `complete` is false, a training source failed: say the day type may be incomplete.
- `food_today` — intake so far, as of `as_of_local`, with `log_status`:
  - `complete`: the log matches the athlete's usual pattern. Still say it is as of the check time.
  - `incomplete` / `missing`: say so first, name what appears to be missing (`usual_meal_slots_missing`, `log_status_reasons`), and make **no** calorie or energy-balance judgement for today. Protein may be described only as "so far".
  - `unknown`: too little history to judge completeness; say so and treat totals as possibly incomplete.
  - `sync_stale` true: the latest entries may not have synchronised; say so with `last_cronometer_sync_local`.
  - `fiber_reliable` false: quick-add entries carry no fiber detail, so fiber is understated; do not judge fiber against its minimum.
- `garmin_expenditure_today`:
  - `observed` is what Garmin has recorded so far (`covers_hours`, `data_through_local`) — label it "so far", never as the day's total.
  - `projection`, when present, is a projected full-day range, not a measurement: always present it as "projected" with its range.
  - `projection_unavailable_reason` set: say that today's full-day expenditure cannot be estimated defensibly and why, and rely on completed days instead.
  - `status` `missing` / `unavailable`: say Garmin expenditure is unavailable today.
- `energy_balance_today` — present only when the log is complete **and** a projection exists. It is a projected, provisional range; never present a single exact deficit or surplus. When unavailable, say why (from `reason`).
- `recent_days` and `recent_trends` — the previous seven days. Energy balances exist only for days with a complete log and a completed Garmin day. Use trends only when `complete_log_days` is at least 4; otherwise say the logs are too incomplete for a trend.
- `body_composition` — the weight trend (`slope_per_week`, `versus_target`) is meaningful only when `sufficient` is true. Body fat is a noisy scale estimate: mention its direction at most, never a single reading. Dates with conflicting duplicate readings are already excluded.
- `sources` — any `error:` entry is an integration failure to report.

You may use AthleteData MCP read tools for supplementary context (for example which Hevy exercises were trained, or what kind of foods dominated the day) but never to replace a fact above. Never call a tool that creates, changes or deletes data.

---

## Analysis Framework

Balance the four goals; follow `goals.md` Performance Priorities when they conflict.

1. **Fat loss** — weight trend against the target rate, completed-day energy balance trend; today's projected balance only as supporting context.
2. **Muscle preservation** — protein against target and range, today and across complete days; flag repeated days below the minimum.
3. **Strength** — on strength days: protein and total intake adequate for recovery.
4. **Running** — on running days: carbohydrate and total intake adequate to fuel and recover; flag a deep deficit on hard or long running days.

Also consider fat at or above its minimum, fiber when reliable, and nutrition quality where the log supports it (for example frequent quick-add entries limit what can be said). Flag apparent under-fuelling or an aggressive deficit as a risk to training and health (`nutrition-philosophy.md`), not as a success. A single day never establishes a trend.

---

## Recommendation Categories

`recommendation_category` is one of:

- `on_track` — today and recent trends fit the targets and goals.
- `adjust_tomorrow` — one specific change would improve alignment.
- `log_incomplete` — today's log is incomplete or missing; the main action is logging.
- `more_information_needed` — essential data is missing (for example no targets or no food data).

---

## Telegram Message (`recommendation_summary`)

`recommendation_summary` is the complete message the athlete receives on Telegram: plain text, English, at most 1,400 characters, no markdown tables or code. Use this shape, omitting lines that do not apply:

```
Day: <day type> · Log: <status, meals, last sync>
Intake: <kcal> · P <g> · C <g> · F <g> · Fibre <g or "not reliable">
Target: <kcal> · P <target> (<range>) · F ≥<min> · Fibre ≥<min>
Garmin: <observed so far> · projected <low–high> (if available)
Balance: <projected range, or why none>
Body composition: <weight trend vs target; body fat direction at most>
Fuel & recovery: <one line for the day type>
Last 7 days: <complete-day count; mean intake and protein; balance trend if usable>
Tomorrow: <exactly one specific, actionable improvement>
```

If the log is incomplete or missing, the first line after `Day:` says so plainly, and intake is labelled "so far" with no calorie judgement.

---

## Human-Readable Output

`human_readable_review` is the full markdown review saved to `reviews/nutrition/YYYY-MM-DD.md`, using the Required Sections of `phase3-workflow-spec.md` where they apply: Objective, Data Retrieved (sources and freshness), Missing Information, Analysis (per goal), Key Findings, Recommendation (the one action for tomorrow), Reasoning, Confidence.

---

## Structured Output

The runner's Structured Result Contract applies unchanged (`runner/workflow-runner.md`). In particular:

- `workflow` is `evening-nutrition-review`; `local_date` is the run's local date.
- `status`: `completed`, or `completed_with_limitations` whenever the log is not `complete`, Garmin data is partial without projection or missing, targets are unavailable, or any source failed.
- `recommendation_category`: as above.
- `missing_data` and `integration_failures`: short strings naming what was missing or failed.
- `data_retrieved_at`: the facts' `computed_at_local`.

---

## Notification Rules

- `notification_required` is always true: the review is the evening message.
- `routine`: normal review, including an incomplete log.
- `attention`: possible under-fuelling (for example a deep projected deficit on a hard training day, or protein below the minimum on most recent complete days), a weight trend faster than the target range, or an integration failure that prevents the review's main purpose.
- `urgent`: never for nutrition content; the runner uses it only for a failed run.

---

## Storage and Idempotency

One review per local date. An automated rerun returns `duplicate_skipped` without calling Claude; a manual rerun is saved alongside as `.manual-<time>.md`. The runner owns the lock, the files and delivery.

---

## Failure Handling

Handled by the runner: a classified Claude failure (authentication, quota, execution, timeout) or invalid output after one corrective retry ends the run as `failed` and sends one urgent notice; nothing is retried automatically. Data gaps are not failures: report them and complete with limitations.

---

## Non-Permissions

The workflow must not:

- Invent meals, intake, expenditure, targets or measurements.
- Present projected expenditure or balance as measured, or compare a full day's intake with a partial day's expenditure.
- Add workout calories to Garmin's daily figures.
- Judge calories on an incomplete log, or a trend on a single day.
- Prescribe meal plans, restrictive diets or aggressive deficits.
- Write files, change data in any connected service, or update governing documents.

---

## Acceptance Criteria

A valid run quotes the facts as given; labels "so far", "projected" and "provisional" correctly; flags every incomplete or missing data source; balances the four goals; and ends with exactly one actionable improvement for tomorrow.
