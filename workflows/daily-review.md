# Daily Review Workflow

## Purpose

The Daily Review workflow evaluates the athlete's current recovery, recent training, available nutrition information, and subjective feedback in order to make a responsible recommendation for the current day.

The workflow must not invent a planned workout if none is known.

---

## Workflow Identity

- Workflow name: `daily-review`
- Human title: `Daily Review`
- Review type: daily
- Run identity: athlete local date plus workflow name
- Proposed review path: `reviews/daily/YYYY-MM-DD.md`

The final filename is based on the athlete's configured local timezone, not the server's default timezone.

---

## Execution Modes

### Manual Mode

Manual execution may include athlete-provided context, such as:

- Symptoms
- Soreness
- Sleep quality
- Schedule constraints
- Intended workout
- Travel
- Illness
- Subjective fatigue

Direct athlete statements are treated as confirmed subjective feedback, not objective sensor data.

### Automated Mode

Automated execution runs without interactive input. It must:

- Use the configured athlete timezone.
- Retrieve fresh AthleteData.
- Avoid interactive confirmation.
- Produce both human-readable and structured output.
- Remain useful when one or more integrations are unavailable.
- Avoid asking follow-up questions unless a responsible recommendation is impossible.

If essential information is missing, the workflow should return `more_information_needed` rather than inventing an answer.

---

## Governing Documents

Before analysis, read:

- `athlete-identity.md`
- `athlete-profile.md`
- `goals.md`
- `coaching-authority.md`
- `running-philosophy.md`
- `strength-philosophy.md`
- `nutrition-philosophy.md`
- `recovery-policy.md`
- `decision-rules.md`
- `training-schedule.md`
- `constraints.md`
- `data-source-policy.md`
- `integration-status.md`
- `lessons-learned.md`

Use these as governing context. Do not summarize all of them in the final review.

---

## Review Window

Use the athlete's current local date. The workflow should normally evaluate:

- Current-day recovery information.
- Current-day nutrition information, when available.
- Previous-day completed training.
- Recent short-term training and recovery context when needed.

Define exact timestamps:

- Athlete local date
- Execution timestamp
- UTC timestamp
- Data retrieval timestamp

Current-day data must be treated carefully:

- Current-day nutrition is provisional until the day is complete.
- Absence of current-day activity does not prove a rest day.
- Early-morning data may be incomplete.
- Missing and incomplete data must be distinguished from one another.

---

## AthleteData Retrieval

Retrieve only what is needed.

### Recovery

When available, retrieve:

- Sleep duration and available sleep detail
- Resting heart rate
- Stress
- Body Battery
- Recent activity load
- Other verified recovery indicators exposed by AthleteData

Do not assume sleep score, HRV, SpO₂, or other advanced fields are available unless actually returned and verified.

### Running

Retrieve:

- Previous-day Garmin running activities
- Relevant recent running context
- Distance
- Duration
- Pace
- Heart rate
- Other verified summary metrics when useful

Do not assume splits or laps are available.

### Strength

Retrieve:

- Previous-day Hevy workouts
- Relevant recent strength context
- Exercises
- Sets
- Repetitions
- Load

Do not treat a generic Garmin strength activity as an additional session when Hevy represents the same workout.

### Nutrition

Retrieve:

- Current-day Cronometer totals when available
- Previous-day totals when available
- Calories
- Protein
- Carbohydrates
- Fat
- Fiber

Current-day totals must be labeled provisional. Do not claim historical nutrition trends unless historical data is actually available.

### Body Composition

Retrieve recent Apple Health weight or body-composition data only when relevant to a current goal or meaningful trend.

Do not react to one isolated measurement. Do not average duplicate same-date entries.

### Subjective Feedback

Include athlete-provided context when available. Do not create subjective feedback in automated mode.

---

## Data Validation

Before analysis:

- Verify dates and timestamps.
- Identify missing fields.
- Distinguish incomplete data from unavailable data.
- Apply source-authority rules from `data-source-policy.md`.
- Apply duplicate-handling rules.
- Avoid double-counting Garmin and Strava activities.
- Avoid double-counting Garmin and Hevy strength records.
- Exclude duplicate same-date body-composition entries rather than averaging them.
- Treat unverified fields as unavailable.
- Flag unresolved conflicts.
- State integration failures explicitly.

Do not silently infer values.

---

## Analysis Framework

Evaluate the athlete's current state across:

- Recovery
- Recent running stress
- Recent strength stress
- Interaction between running and strength
- Available nutrition and fueling
- Subjective feedback
- Injury or illness risk
- Consistency with current goals
- Likely suitability of training today

Use multiple signals together. Do not derive a readiness conclusion from one isolated metric. Do not fabricate a numerical readiness score.

Clearly distinguish:

- Verified fact
- Athlete-reported feedback
- Interpretation
- Uncertainty

---

## Recommendation Categories

The workflow must select exactly one primary recommendation category:

- `proceed_as_planned`
- `proceed_with_minor_modifications`
- `reduce_intensity`
- `reduce_volume`
- `recovery_focused_day`
- `rest_day`
- `more_information_needed`

The recommendation must respect `coaching-authority.md`.

### Running

The coach may directly recommend:

- Proceeding
- Rescheduling
- Reducing volume
- Reducing intensity
- Changing the purpose of the session
- Replacing a run with recovery

Do not invent a specific planned workout when none is documented or supplied.

### Strength

The coach may recommend:

- Scheduling changes
- Delaying a session
- Reducing fatigue
- Avoiding progression during poor recovery
- Risk-management modifications

The coach must not replace the athlete's established strength progression system.

### Nutrition

Nutrition recommendations are advisory. The workflow may identify:

- Inadequate fueling
- Poor recovery support
- Incomplete intake data
- Likely need for additional carbohydrate, protein, fluids, or total energy

Do not present medical nutrition advice or diagnose deficiencies from one day of data.

---

## Safety and Escalation

When symptoms suggest possible injury or illness:

- Do not diagnose.
- Recommend a conservative training response.
- Identify the limitation of coaching advice.
- Recommend professional evaluation when appropriate.
- Prioritize medical care over training when symptoms appear severe or urgent.

The system must not create artificial urgency.

---

## Human-Readable Output

Use this exact structure:

```
# Daily Review — YYYY-MM-DD

## Today's Recommendation

State the primary recommendation in one or two direct sentences.
Include the recommendation category in plain language.

## Verified Data

Summarize the most relevant verified data.
Use domain subsections only when helpful:
- Recovery
- Recent Training
- Nutrition
- Body Composition
Do not dump raw data.

## Athlete Feedback

Include only when athlete feedback was provided.

## Analysis

Explain how the relevant signals interact.
Separate fact, interpretation, and uncertainty.

## Adjustments for Today

Provide practical adjustments.
Do not prescribe a detailed workout unless modifying a known planned workout.

## Confidence and Limitations

Use exactly one confidence level: High, Moderate, or Low.
Explain the main reason for that confidence level.

## What to Watch

List only the most important symptoms, recovery indicators, or performance
signals to monitor today. Keep this concise.
```

---

## Structured Output

In addition to the human-readable review, the workflow produces a machine-readable result with these fields:

- `workflow`
- `status`
- `local_date`
- `executed_at`
- `executed_at_utc`
- `data_retrieved_at`
- `execution_mode`
- `recommendation_category`
- `recommendation_summary`
- `confidence`
- `notification_level`
- `limitations`
- `missing_data`
- `integration_failures`
- `review_file`
- `notification_required`
- `error`

Allowed values:

### status
- `completed`
- `completed_with_limitations`
- `failed`
- `duplicate_skipped`

### execution_mode
- `manual`
- `automated`

### confidence
- `high`
- `moderate`
- `low`

### notification_level
- `routine`
- `attention`
- `urgent`

The human-readable review and the structured result must agree with each other. A final serialization format is not chosen at this stage, but the field contract above is precise enough for the future runner to validate against.

---

## Notification Rules

### Routine

Use for a normal completed review without a meaningful warning.

### Attention

Use when there is:

- A recommended training reduction
- Meaningful recovery concern
- Relevant integration failure
- Unresolved data issue affecting confidence
- Possible injury concern that is not urgent

### Urgent

Use only for:

- Severe athlete-reported symptoms
- A significant safety concern
- A workflow failure that prevents responsible coaching and requires intervention

Do not mark normal fatigue as urgent.

---

## Storage and Idempotency

Intended review path: `reviews/daily/YYYY-MM-DD.md`

Rules:

- One completed Daily Review per athlete local date.
- Do not silently overwrite an existing completed review.
- A duplicate automated run should return `duplicate_skipped`.
- Retries may replace only a failed or clearly incomplete result.
- Manual reruns must be clearly identified and must not silently overwrite the scheduled review.
- The future runner must prevent overlapping runs.
- Storage failure must not be reported as a successful completed workflow.

No review files are created at this stage.

---

## Failure Handling

Define behavior for:

- AthleteData completely unavailable
- Garmin unavailable
- Hevy unavailable
- Cronometer unavailable
- Apple Health unavailable
- Malformed Claude output
- Timeout
- Duplicate run
- Review-file write failure
- Notification failure

Rules:

- One integration failure should not automatically fail the whole review.
- Conclusions depending on the failed source must be withheld.
- If enough data remains, return `completed_with_limitations`.
- If a responsible recommendation cannot be made, use `more_information_needed`.
- Telegram delivery failure must not invalidate a successfully saved review.
- Errors must not include secrets or raw authorization URLs.

---

## Lessons Learned

Do not update `lessons-learned.md` during a normal Daily Review. The workflow may suggest a possible lesson only when a durable pattern appears supported by repeated evidence. Any actual modification requires explicit athlete approval.

---

## Non-Permissions

This workflow must not:

- Modify governing documents.
- Modify `goals.md`.
- Modify philosophy documents.
- Modify `coaching-authority.md`.
- Update `lessons-learned.md`.
- Commit or push Git changes.
- Alter integrations.
- Change system configuration.
- Take real-world actions for the athlete.

Its permitted outputs are limited to:

- The Daily Review result
- The proposed review file
- The structured workflow result
- The future Telegram message payload

---

## Acceptance Criteria

The workflow definition is complete when it:

- Supports manual and automated execution.
- Retrieves fresh AthleteData.
- Handles partial integration failure.
- Validates data before analysis.
- Produces one clear recommendation category.
- Produces human-readable and structured output.
- Defines idempotency and storage behavior.
- Respects all governing documents.
- Does not require interactive input in automated mode.
- Does not implement the runner or scheduler.
