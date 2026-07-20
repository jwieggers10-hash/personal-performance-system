# CLAUDE.md

This is the operational handbook for this repository. Read it first in any new session — assume no memory of prior conversations carries over.

---

## Project Purpose

This repository is a long-term AI performance coaching system, built in four phases: (1) data foundation and integration testing, (2) building the coaching brain — the governing documents in this repository, (3) and (4) not yet defined. The system exists to provide evidence-based, long-term athletic coaching grounded in verified data, not to optimize any single workout or metric in isolation.

---

## Repository Structure

- `CLAUDE.md` — this file; operational handbook for future sessions.
- `athlete-identity.md` — who the athlete is trying to become; mission, principles, and long-term vision. Rarely changes.
- `athlete-profile.md` — factual reference: sports, data sources, current metrics available. No goals or opinions.
- `goals.md` — the athlete's current priorities. Expected to evolve over time.
- `coaching-authority.md` — the governance contract: what the coach may decide versus what remains the athlete's call, by domain.
- `running-philosophy.md` — coaching principles governing running decisions.
- `strength-philosophy.md` — coaching principles governing strength training, built around the athlete's existing progression system.
- `nutrition-philosophy.md` — coaching principles governing nutrition, supporting training and recovery rather than standing as a goal in itself.
- `recovery-policy.md` — how recovery signals should influence training decisions.
- `decision-rules.md` — the general reasoning framework used across every coaching domain.
- `training-schedule.md` — scheduling philosophy: how training is organized over time, not a fixed calendar.
- `constraints.md` — permanent guardrails the system must never violate.
- `lessons-learned.md` — durable, evidence-backed coaching knowledge accumulated over time.
- `data-source-policy.md` — which data source is authoritative for which metric, and duplicate-handling rules.
- `integration-status.md` — verified status and known limitations of each AthleteData MCP integration (Garmin, Hevy, Cronometer, Apple Health).
- `plans/running/`, `plans/strength/`, `plans/nutrition/` — reserved for concrete plans produced by future coaching work; currently empty.
- `reviews/daily/`, `reviews/weekly/`, `reviews/monthly/` — reserved for periodic review records; currently empty.
- `race-preparation/`, `prompts/`, `logs/`, `config/` — reserved for future use; currently empty.

---

## Data Sources

All data retrieval must follow `data-source-policy.md`: Garmin is authoritative for running and recovery data, Hevy for strength training, Cronometer for nutrition, and Apple Health (FeelFit) for body weight and composition. Known gaps and limitations per integration are documented in `integration-status.md` — check it before assuming a metric is available.

---

## Coaching Workflow

When performing any coaching task:

1. Read the governing documents relevant to the domain in question.
2. Retrieve verified AthleteData for the task at hand — do not rely on assumptions or memory of past data.
3. Analyze the retrieved data against the relevant philosophy and decision rules.
4. Explain the reasoning transparently, per `decision-rules.md`.
5. Make a recommendation consistent with `coaching-authority.md`.
6. Update `lessons-learned.md` when a durable, evidence-backed insight emerges — not for routine observations.

---

## Development Workflow

- Philosophy documents (`running-philosophy.md`, `strength-philosophy.md`, `nutrition-philosophy.md`, `recovery-policy.md`) should remain stable; they are not meant to shift with every session.
- `goals.md` may evolve as the athlete's priorities change.
- Integration documents (`data-source-policy.md`, `integration-status.md`) change only when the underlying integrations change.
- New features or documents should preserve consistency with the existing coaching philosophy rather than introduce a competing framework.

---

## General Principles

- Prefer evidence over assumptions.
- Avoid duplicating content that already exists in another document — cross-reference instead.
- Maintain internal consistency across all governing documents.
- Keep documentation concise.
- Ask before making major philosophical changes to an approved document.
