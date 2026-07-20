# Workflow Runner Specification

This document defines the complete specification for the reusable workflow runner described in `runner/README.md`. It applies to every workflow the runner will ever execute, starting with Daily Review. It is a specification only — no code, scripts, or systemd units exist yet.

---

## Inputs

The runner accepts:

- **Workflow name** — which workflow definition to execute (e.g. `daily-review`).
- **Execution mode** — `manual` or `automated`.
- **Athlete timezone** — the configured local timezone used to compute the athlete's local date and review filenames.
- **Optional athlete context** — free-form context supplied in manual mode only (symptoms, soreness, intended workout, and similar). Never present in automated mode.
- **Repository root** — the resolved path to the repository, so the runner can locate governing documents, workflow definitions, and review storage regardless of the working directory it was invoked from.

---

## Runner Responsibilities

The runner must:

1. Load environment (timezone, repository path, and other configured values — never hard-coded).
2. Resolve repository paths.
3. Read governing documents, as directed by the workflow definition.
4. Execute the requested workflow.
5. Retrieve fresh AthleteData.
6. Validate returned data.
7. Produce human-readable output.
8. Produce structured output.
9. Save review files when appropriate.
10. Return an execution result.

The runner itself must not contain coaching logic. It does not decide what a good recommendation is, what counts as adequate recovery, or how to weigh conflicting signals — all of that belongs to the workflow definition being executed. The runner's job ends at orchestration: invoke, validate, store, report.

---

## Execution Lifecycle

```
Initialize
    ↓
Validate environment
    ↓
Acquire workflow lock
    ↓
Load workflow
    ↓
Execute Claude
    ↓
Validate structured output
    ↓
Write review
    ↓
Release lock
    ↓
Return status
```

Each stage either completes and passes control to the next, or fails in a way that is recorded and surfaced (see Failure Handling) rather than allowed to leave the system in an ambiguous state — for example, a lock must always be released or provably expire, even if a later stage fails.

---

## Structured Result Contract

Every workflow execution returns a result with at least these fields:

- `workflow`
- `status`
- `execution_mode`
- `execution_id`
- `started_at`
- `finished_at`
- `local_date`
- `review_file`
- `notification_required`
- `notification_level`
- `confidence`
- `limitations`
- `error`

This is the runner's outer envelope — the execution metadata common to any workflow. A specific workflow definition (such as `workflows/daily-review.md`) may extend this with additional fields specific to its own domain (for example, `recommendation_category`, `missing_data`, `integration_failures`, `data_retrieved_at`). Where a field name is shared between this envelope and a workflow's own structured output — `workflow`, `status`, `execution_mode`, `local_date`, `review_file`, `notification_required`, `notification_level`, `confidence`, `limitations`, `error` — the two must agree; the workflow does not report a different value than the runner recorded for that execution. `started_at`/`finished_at` are the runner's process-level execution bounds; a workflow's own `executed_at` timestamp should fall within them.

No final serialization format is chosen at this stage. The field contract above exists so a future implementation has something precise to validate against.

---

## Locking

Each workflow execution must acquire a lock scoped to that workflow and the run it corresponds to (for a daily workflow, scoped to the athlete's local date) before doing any work, and release it when the run reaches a terminal state. This prevents two executions of the same workflow-run from overlapping — whether triggered twice by a scheduler, once by a scheduler and once manually, or by a retry racing an in-flight run.

A lock must not become permanent. If a run dies without releasing its lock (a crash, a killed process, a server restart), a later execution must be able to detect that the lock is stale and proceed rather than being blocked indefinitely by a lock nothing will ever release.

---

## Storage

The runner writes two kinds of output, kept separate:

- **Review files** — the human-readable coaching output, written to the workflow's designated location (e.g. `reviews/daily/YYYY-MM-DD.md`), following that workflow's own idempotency rules (see its Storage and Idempotency section).
- **Structured results** — the machine-readable execution result described above. Where this is persisted, retained, or only returned to the caller is not decided at this stage.

Neither is written until the corresponding stage of the Execution Lifecycle has actually succeeded — the runner does not write a partial or placeholder review file and fill it in later.

---

## Logging

The runner produces operational logs — records of what the runner itself did (started, acquired lock, invoked Claude, validated output, wrote review, released lock, failed at stage X) — kept entirely separate from the coaching review files a workflow produces. An operational log entry is about the execution; a review file is about the athlete. Operational logs must not contain secrets, authorization headers, or raw AthleteData payloads, consistent with the Logging section of `phase3-workflow-spec.md`.

---

## Failure Handling

The runner must account for:

- **Claude failure** — the invocation itself fails or errors out. Recorded as a failed execution; no review file is written or overwritten.
- **AthleteData failure** — one or more integrations are unavailable. This is not automatically a runner-level failure; the workflow definition decides whether the result is `completed_with_limitations` or something more severe. The runner's job is to make sure that decision is possible, not to make it.
- **Malformed output** — Claude returns output that doesn't match the Structured Result Contract. Treated as a failed execution rather than passed through and trusted.
- **Storage failure** — the review file cannot be written. Never reported as a successful completion.
- **Timeout** — the execution exceeds its allotted time. Terminated, recorded as a failed or timed-out execution, and the lock is released rather than left held.
- **Duplicate execution** — a second execution for the same workflow-run is attempted while one is already in progress or already completed. Detected via the lock (in-progress case) or via the idempotency rule the workflow defines (already-completed case), and reported as `duplicate_skipped` rather than run again.

A failed or timed-out execution does not retry indefinitely on its own; retrying is a decision made outside the single execution (by a human, or later by a scheduler's own policy), not something the runner does automatically and silently.

---

## Extensibility

A new workflow (Weekly Review, Monthly Review, Recovery Check, or anything added later) plugs into this runner without any change to the runner itself, provided it:

- Is a workflow definition following the Standard Workflow Structure and Required Sections in `phase3-workflow-spec.md`.
- Produces output conforming to the Structured Result Contract above, extended as needed with its own domain-specific fields.
- Defines its own storage location and idempotency rule, the way `workflows/daily-review.md` does for `reviews/daily/`.
- Defines its own failure-handling specifics where they differ from the general rules above (for example, which integrations are essential versus optional for that particular workflow).

The runner's role stays fixed regardless of how many workflows exist: load the named workflow, execute it, validate what comes back, store it, and report the result.
