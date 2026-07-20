# Phase 3 Workflow Specification

## Purpose

Every coaching workflow built in Phase 3 follows the same reasoning framework established during Phase 2. This document is the implementation standard that future workflows — Daily Review, Weekly Review, Monthly Review, Workout Analysis, Recovery Check, Nutrition Review, Running Planning, Strength Review, Race Preparation, and anything added later — must conform to, so that each one feels like part of one coherent coaching system rather than a separate script with its own rules.

The production system will run as an automated, always-on service on a DigitalOcean Ubuntu server the athlete manages. The Deployment Architecture section below defines how that runs; the sections that follow it define how every workflow reasons regardless of where or how it is invoked.

No workflow may bypass:

- `coaching-authority.md`
- `decision-rules.md`
- `running-philosophy.md`
- `strength-philosophy.md`
- `recovery-policy.md`
- `nutrition-philosophy.md`
- `constraints.md`

---

## Standard Workflow Structure

Every workflow must follow this order. No applicable step may be skipped.

1. Read governing documents relevant to the workflow's domain.
2. Determine the workflow's objective for this run.
3. Retrieve required AthleteData.
4. Validate the data actually retrieved.
5. Identify missing information.
6. Analyze according to the relevant coaching philosophy.
7. Apply `decision-rules.md`.
8. Generate recommendations.
9. Explain reasoning.
10. State confidence and limitations.
11. Suggest next actions, when appropriate.

A workflow that produces a recommendation without having gone through the prior steps is not a valid execution of this system, regardless of how correct the recommendation happens to be.

---

## Required Sections

Every workflow's output should use these sections wherever applicable:

- **Objective** — what this specific run of the workflow is trying to accomplish.
- **Data Retrieved** — what was actually fetched, from which source, and how recent it is.
- **Missing Information** — what could not be retrieved or is not currently available, stated plainly rather than omitted.
- **Analysis** — interpretation of the retrieved data against the relevant philosophy and policy documents.
- **Key Findings** — the notable takeaways from the analysis, stated concisely.
- **Recommendations** — proposed actions, scoped to the authority defined in `coaching-authority.md`.
- **Reasoning** — why each recommendation follows from the analysis, traceable to `decision-rules.md` and the relevant philosophy document.
- **Confidence** — how certain the conclusions are, and what would increase or decrease that confidence.
- **Next Actions** — what should happen next, if anything: a follow-up check, a request for athlete input, or nothing further required.

A section that doesn't apply to a given workflow may be omitted, but it should not be silently skipped when it does apply.

---

## Data Retrieval Principles

Workflows should:

- Always retrieve fresh AthleteData for the task at hand — never rely on memory of prior sessions or assume prior data still holds.
- Respect `data-source-policy.md` for which source is authoritative and how duplicates are handled.
- Respect the documented limitations in `integration-status.md` rather than assuming a metric is available because it exists in principle.
- Clearly distinguish verified data from assumptions at every step, not just in a final caveat.

---

## Decision Principles

Every workflow must:

- Follow `decision-rules.md`.
- Respect the authority boundaries in `coaching-authority.md`.
- Remain consistent with `athlete-identity.md`.
- Prioritize `goals.md`.
- Consider recovery before recommending an increase in training.
- Explain trade-offs explicitly rather than presenting a recommendation as though no trade-off existed.

---

## Communication Standards

Every workflow should:

- Be concise.
- Explain its reasoning rather than asserting a conclusion.
- Separate verified facts from interpretation.
- Admit uncertainty where it exists.
- Never fabricate a conclusion the data doesn't support.
- Avoid unnecessary repetition, including repeating content already established in a governing document.

---

## Deployment Architecture

This section defines the production deployment target and its operating constraints. It is binding on every workflow implemented from this point forward.

### Deployment Target

The production coaching system runs on a persistent DigitalOcean Ubuntu server. This is the defined deployment target, not one option among several.

The server hosts:

- The Git repository
- Claude Code
- AthleteData MCP configuration
- Workflow definitions
- Workflow runners
- Scheduled jobs
- Generated review files
- Telegram delivery components
- Operational logs

The repository remains the source of truth for workflow definitions and coaching documents.

### Operating Model

The system runs without an interactive terminal session. Scheduled workflows execute through a non-interactive Claude Code invocation or another supported non-interactive Claude interface. Manual execution through Claude Code commands remains available for testing and ad hoc reviews, alongside the automated path — the two are not mutually exclusive.

### Scheduling

Systemd services and systemd timers are the preferred production scheduling mechanism. Cron is not the primary design unless there is a clear reason systemd cannot support a requirement.

Each automated workflow should eventually have:

- A dedicated systemd service
- A corresponding systemd timer
- Explicit environment loading
- Logging through journald or an application log
- Timeout handling
- Failure status reporting
- Protection against overlapping runs

Examples of future scheduled workflows include Daily Review, Weekly Review, and Monthly Review. No service or timer files are created at this stage.

### Repository Location

The implementation assumes a fixed repository location on the server, but no exact path is hard-coded at this stage. Scripts should be designed so the path is configured through one central environment or configuration value, and all runner scripts must resolve paths reliably regardless of the shell's working directory.

### Runtime User

The coaching system runs under a dedicated non-root Linux user. The service must not require routine root privileges. Root access is needed only for initial server setup, package installation, and systemd registration.

### Environment and Secrets

Secrets must not be stored in the repository. A protected environment file or external secret store holds values such as:

- AthleteData MCP API key
- Telegram bot token
- Telegram chat ID
- Any Claude authentication or API credentials
- Repository path
- Athlete timezone

The environment file should:

- Be readable only by the dedicated service user and administrators
- Never be committed to Git
- Never be printed in logs
- Be referenced by systemd services

An example template using placeholder values only will be provided later. The real environment file is not created at this stage.

### Timezone

The athlete's timezone must be explicitly configured. Every workflow must distinguish:

- Athlete local date
- Server execution timestamp
- UTC timestamp when useful

Workflow dates and review filenames are based on the athlete's configured local timezone, not the server's default timezone.

### Storage

Generated reviews are stored at:

- `reviews/daily/YYYY-MM-DD.md`
- `reviews/weekly/YYYY-Www.md`
- `reviews/monthly/YYYY-MM.md`

— corresponding to the Daily Review, Weekly Review, and Monthly Review workflows described under Workflow Categories below.

Operational runtime data is stored separately from coaching documents. Future directories such as `runtime/locks/`, `runtime/state/`, and `logs/` are proposed for this purpose and should be excluded from Git where appropriate. None of these directories are created at this stage.

### Git Behavior

Automated coaching runs may create or update review files locally. They must not automatically:

- Commit changes
- Push changes
- Modify governing documents
- Rewrite goals
- Edit philosophy documents
- Update `lessons-learned.md`

Git commits and pushes require a separately approved workflow. The implementation should preserve uncommitted review files safely rather than risk losing them.

### Process Locking

Each workflow must prevent overlapping execution. The design should support a lock mechanism such as `flock`, systemd execution controls, or an atomic lock file. A stale lock must not permanently disable future runs.

### Failure Handling

The deployment must account for: server reboot, network outage, AthleteData MCP outage, Claude execution failure, Telegram delivery failure, repository write failure, malformed workflow output, workflow timeout, and duplicate scheduled execution.

Systemd should restart only components for which automatic restart is safe. A coaching workflow should not repeatedly retry indefinitely. Failures should be recorded and surfaced clearly rather than failing silently.

### Boot Behavior

Required services and timers should be enabled to resume automatically after a server reboot. Missed scheduled runs should be handled intentionally, distinguishing between:

- Workflows that may run shortly after a missed timer
- Workflows that should be skipped if their scheduled window has passed

Final behavior for each workflow is not defined here; each workflow's own specification must state its policy when it is implemented.

### Logging

The system should log workflow name, start time, completion time, execution status, review output path, notification status, and error category.

Logs must not include API keys, authentication headers, raw secret-bearing MCP URLs, or unnecessary raw health data. Human-readable coaching output belongs in review files, not operational logs.

### Telegram Delivery

Telegram is the production delivery channel. The delivery layer should eventually:

- Send the human-readable recommendation
- Include the confidence level
- Highlight attention or urgent statuses
- Avoid sending excessive raw data
- Report when a complete review could not be generated

Telegram failure must not cause the generated review file to be lost. Telegram delivery is not implemented at this stage.

### Backups and Recovery

The design should support recovering the server from failure. The Git repository preserves committed source files and configuration templates; uncommitted generated reviews require a separate backup or synchronization strategy. The final backup mechanism is not selected here, but is marked as a required deployment task before production use.

### Security Baseline

The production design assumes:

- SSH key authentication
- Root login disabled or restricted
- Password authentication disabled where practical
- Firewall enabled
- Only required ports exposed
- Automatic security updates considered
- Dedicated non-root service user
- Least-privilege file permissions
- No secrets committed to Git

No infrastructure automation is created at this stage.

---

## Workflow Categories

These are the workflows this specification is designed to standardize. None are implemented yet.

### Daily Review
- **Purpose:** A short check-in on the athlete's current status — recovery, recent training, and anything requiring attention today.
- **Inputs:** Most recent recovery signals, the most recent training session(s), recent nutrition log if available.
- **Outputs:** A brief status summary, any flags, and same-day adjustment guidance if warranted.

### Weekly Review
- **Purpose:** Assess the past week's training load, consistency, and recovery trend against current goals.
- **Outputs:** A weekly summary, trend assessment, and recommended direction for the coming week.
- **Inputs:** The week's running and strength sessions, recovery trend over the week, nutrition consistency, adherence to the schedule.

### Monthly Review
- **Purpose:** Evaluate longer-term trends in performance, body composition, consistency, and progress toward goals.
- **Inputs:** The month's aggregated training and recovery data, body-composition trend, progress against `goals.md`.
- **Outputs:** A monthly summary, trend analysis, and a flag if `goals.md` itself may need revisiting.

### Workout Analysis
- **Purpose:** Analyze one specific completed session — running or strength — in detail.
- **Inputs:** The specific activity's data, plus enough recent history to give it context.
- **Outputs:** Session-specific analysis and how it fits (or doesn't) into the broader trend.

### Recovery Check
- **Purpose:** Assess current recovery status to inform an immediate training decision.
- **Inputs:** Recent sleep, resting heart rate, stress, Body Battery, recent training load, and subjective feedback if given.
- **Outputs:** A recovery status assessment and a recommendation — proceed, modify, or rest.

### Nutrition Review
- **Purpose:** Assess nutrition consistency and adequacy relative to training demands and goals.
- **Inputs:** Logged nutrition data over the relevant window, training load context.
- **Outputs:** A nutrition summary, any identified gaps, and recommendations consistent with `nutrition-philosophy.md`.

### Running Planning
- **Purpose:** Determine the direction of upcoming running training based on current fitness, recovery, and goals.
- **Inputs:** Recent running trends, recovery status, `goals.md`, and race calendar if applicable.
- **Outputs:** A running training direction consistent with `running-philosophy.md` and `training-schedule.md` — not a rigid workout-by-workout prescription produced outside this framework.

### Strength Review
- **Purpose:** Analyze strength training progress within the athlete's own established progression system.
- **Inputs:** Recent Hevy workout and routine data, recovery status.
- **Outputs:** A progress summary, plateau or risk flags, and recommendations that work within the existing system rather than replacing it.

### Race Preparation
- **Purpose:** Guide preparation for a specific upcoming race.
- **Inputs:** Race details (date, distance, target if any), current fitness and training data, recovery status.
- **Outputs:** Race-specific preparation guidance — taper and execution considerations — consistent with the Race Preparation Philosophy in `running-philosophy.md`.

---

## Extensibility

A new workflow may be added later if it follows the Standard Workflow Structure and Required Sections defined above, stays within the authority defined in `coaching-authority.md`, and remains consistent with every governing document rather than introducing a competing framework. Before it is implemented, it should be described here — Purpose, Inputs, Outputs — the same way the workflows above are, so the full set of workflows remains visible in one place.

---

## Phase 3 Implementation Order

This is the defined order of implementation for Phase 3:

1. Finalize the DigitalOcean automation architecture.
2. Implement Daily Review as a reusable workflow definition.
3. Implement the non-interactive workflow runner.
4. Implement structured workflow output validation.
5. Implement review-file storage and idempotency.
6. Test all components manually on the DigitalOcean server.
7. Create systemd service and timer definitions.
8. Add Telegram delivery.
9. Add failure notifications and operational monitoring.
10. Add backup and recovery procedures.
11. Extend the architecture to weekly and monthly workflows.
12. Document deployment and maintenance procedures.
