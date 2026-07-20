# Runner Architecture

This document describes the architecture of the workflow runner — the reusable execution framework that will eventually run every coaching workflow (Daily Review and everything that follows it). It is architectural only: no code, scripts, or execution logic exists yet.

---

## What the Runner Is

The runner is a thin, reusable execution shell. It knows how to load a workflow, invoke Claude Code to execute it, validate what comes back, save the result, and report status. It does not know anything about coaching — no philosophy, no decision rules, no recommendation logic. All of that lives in the workflow definitions themselves (e.g. `workflows/daily-review.md`). This separation is intentional: adding a new workflow should never require changing the runner, and changing the runner should never require touching coaching logic.

---

## Relationship Between Components

- **Workflow definitions** (`workflows/*.md`) — the complete operational contract for a given workflow: what to retrieve, how to analyze it, what output to produce, and under what authority. The runner treats a workflow definition as an input, not something it interprets on the coaching level.
- **Claude Code** — the reasoning engine. It reads the governing documents, retrieves AthleteData, applies the workflow's analysis and decision logic, and produces both the human-readable review and the structured result. The runner invokes Claude Code — interactively for manual mode today, non-interactively once that mode exists — but performs none of the reasoning itself.
- **AthleteData** — the MCP data source Claude Code queries during execution, governed by `data-source-policy.md` and constrained by the documented limitations in `integration-status.md`. The runner does not talk to AthleteData directly; it is Claude Code, acting under a workflow definition, that does.
- **Review storage** — where the human-readable output lands: `reviews/daily/`, `reviews/weekly/`, `reviews/monthly/`, per the Storage section of `phase3-workflow-spec.md`. The runner is responsible for writing to this location according to the idempotency rules each workflow defines, not for deciding what the review says.
- **Structured output** — the machine-readable result every workflow produces alongside its human-readable review. The runner validates this output against the contract in `workflow-runner.md` before deciding what happens next (save, notify, mark failed).
- **Telegram** — the future delivery channel. The runner will eventually pass the structured result's notification fields to a Telegram delivery step. That step does not exist yet; today the runner only needs to produce a result shaped correctly enough for that step to consume later.
- **Future schedulers** (systemd timers) — will invoke the runner on a schedule instead of a human invoking it manually. The runner is designed so both paths converge on the same execution lifecycle; a scheduler is just another caller.

---

## How Workflows Will Eventually Be Executed

Two execution modes exist, both described in each workflow definition's own Execution Modes section:

- **Manual** — a person runs the workflow through Claude Code directly, optionally supplying athlete context (symptoms, soreness, intended workout, and so on). Useful for testing and ad hoc reviews.
- **Automated** — a scheduler invokes the runner with no human present. The runner supplies the configured athlete timezone and repository root, and the workflow runs without interactive input, using only what AthleteData and prior context provide.

The runner's job is to make these two paths behave identically from the workflow's point of view — the same governing documents get read, the same data gets retrieved, the same validation happens, and the same structured result shape comes out — regardless of who or what triggered the run.

---

## What This Document Is Not

This is not an implementation plan, a script, or a systemd configuration. The detailed execution contract — inputs, responsibilities, lifecycle, result schema, locking, storage, logging, and failure handling — is defined in `runner/workflow-runner.md`.
