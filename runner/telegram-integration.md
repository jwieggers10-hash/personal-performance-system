# Telegram Delivery Architecture

This document defines the architecture for delivering completed coaching workflow results to the athlete via Telegram. It is a specification only — no Telegram bot exists, no tokens have been created, and no delivery code has been written. It extends `runner/workflow-runner.md`'s Execution Lifecycle and `phase3-workflow-spec.md`'s Telegram Delivery section with the detail needed to eventually implement this safely.

---

## Purpose

Telegram is the production notification channel: it tells the athlete a review is ready and gives them enough to act on immediately, without requiring them to open the repository. It is a delivery mechanism only. It does not generate coaching content, does not make decisions, and is not the system of record — the review file is. If Telegram delivery is ever unavailable, degraded, or removed entirely, the coaching system must continue to function exactly as it does today: reviews are still generated, validated, and saved.

---

## Telegram Bot Architecture

- A single, dedicated Telegram bot, created for this system alone (via BotFather) and used for no other purpose.
- A single private one-to-one chat between that bot and the athlete. `TELEGRAM_CHAT_ID` identifies that specific chat, not a group or channel.
- Delivery is outbound-only: the runner calls the Telegram Bot API's `sendMessage` endpoint over HTTPS after a workflow completes. The server never receives inbound Telegram traffic — no webhook is registered, no port is opened for Telegram, and no polling loop runs. This keeps the Security Baseline in `phase3-workflow-spec.md` (minimal exposed ports, firewall enabled) unaffected by adding Telegram.
- No command handling, no two-way conversation, and no athlete-to-system input arrives through Telegram at this stage. If a future phase wants the athlete to reply (e.g. to supply context back into a workflow), that is a distinct, separately designed feature — not part of this delivery layer.

---

## Required Secrets

- `TELEGRAM_BOT_TOKEN` — authenticates the bot to the Telegram Bot API. Full send access to the athlete's chat; treated as a credential, not a routing detail.
- `TELEGRAM_CHAT_ID` — identifies the destination chat. Not a secret in the cryptographic sense, but treated as sensitive: it is personally identifying and, combined with the bot token, is sufficient to impersonate deliveries to the athlete. Handled with the same care as the token throughout this document.

Both are already reserved (blank) in `runner/config.env` and `runner/config.example.env` from earlier phases. No values are created or filled in by this document.

---

## Secure Loading

Telegram secrets follow exactly the pattern already established for the AthleteData MCP key and Claude invocation config — no new loading mechanism is introduced:

- Stored only in the server-local `runner/config.env` (or an equivalent protected env file), never in the repository, never in a workflow definition, never in a systemd unit file.
- `runner/config.env` stays `600`, owned by the dedicated service user (`coach`), git-ignored.
- For automated runs, loaded via the service unit's `EnvironmentFile=`, read by systemd (root) before it drops privileges to `coach` — the same mechanism already installed for `personal-performance-daily-review.service`.
- For manual runs, loaded via the runner's existing `load_env_file()` / `--env-file` behavior — no separate code path for Telegram config.
- Never interpolated into a log line, error message, or exception string in full. Any error text that could contain the token or chat ID (e.g. a raw HTTP response echoing the request URL) must be redacted before logging, using the same `redact_secrets()` pattern already applied to AthleteData MCP URLs and bearer tokens.

---

## Handoff From the Runner

Telegram delivery is a new, final stage appended to the Execution Lifecycle defined in `runner/workflow-runner.md`, strictly **after** the review is durably saved:

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
Notify (Telegram)      <-- new
    ↓
Release lock
    ↓
Return status
```

The Notify stage:

- Only runs if the Write Review stage actually succeeded (a saved review file, or a `duplicate_skipped`/`failed` result the workflow has decided is still worth surfacing — see Delivery Levels below for what gets sent at all).
- Consumes **only** the already-finalized Structured Result Contract fields (`workflow`, `status`, `local_date`, `review_file`, `confidence`, `notification_level`, `notification_required`, `limitations`, `recommendation_summary` where the workflow provides it) — it does not re-read the review file's full content, re-derive a summary, call Claude, or query AthleteData itself. Whatever the workflow already decided is what gets delivered; the delivery layer's job is formatting and transmission only, never judgment.
- Never blocks or reverses a stage that already completed. A Telegram failure cannot un-save a review, cannot flip `status` from `completed` to `failed`, and cannot cause the lock to be held longer than it otherwise would be.

---

## Delivery Levels

Every delivery decision is driven entirely by the workflow's own `notification_level` (`routine` / `attention` / `urgent`) and `notification_required` fields — the delivery layer adds no coaching judgment of its own about what counts as urgent.

### routine
- Sent as a normal-priority message.
- Confirms a review is ready and gives the one-line recommendation summary.

### attention
- Sent as a normal-priority message, visually distinguished from routine (e.g. a leading text marker such as `[ATTENTION]`) so it doesn't blend into routine daily notifications.
- Includes the recommendation summary and a short reason the review needs a closer look (drawn from `limitations` / the workflow's own reasoning fields, not re-derived by the delivery layer).

### urgent
- Sent as a high-priority message (Telegram's notification is never silenced for this level, regardless of any future "quiet hours" feature).
- Clearly marked (e.g. a leading `[URGENT]`) and kept short enough to be read at a glance.
- If `notification_required` is true but the workflow's own `status` is `failed` (a workflow failure severe enough to require intervention, per `workflows/daily-review.md`'s Urgent rules), the message says plainly that the review could not be completed and why in one sentence — not that a review is ready.

`notification_required: false` (routine completions the workflow doesn't consider worth a push) may still be delivered at routine priority, or suppressed entirely in favor of the athlete checking the review file on their own schedule — this policy choice is left open for the implementation step, not decided here.

---

## Message Formatting Rules

- The Telegram message is a **condensed summary**, never the full `human_readable_review` markdown body. The review file remains the complete record; Telegram is a pointer plus headline.
- Required content, in order: workflow name and local date, delivery-level marker (for `attention`/`urgent`), the recommendation summary, confidence level, and a reference to where the full review lives (its relative path, e.g. `reviews/daily/2026-08-23.md` — not a clickable link, since the file is not web-accessible).
- No raw AthleteData payloads, no raw sensor values dumped wholesale, no internal execution metadata (`execution_id`, lock details, timing) — those belong in operational logs, not athlete-facing messages.
- Plain text or Telegram's basic Markdown/HTML formatting only (bold for the level marker, for example) — no dependency on Telegram features that could silently fail on older clients.

---

## Maximum Message Length Handling

- The Telegram Bot API's `sendMessage` limit is 4096 UTF-8 characters per message.
- Because the message is already a condensed summary (see above), it should never approach this limit under normal conditions.
- If a constructed message would still exceed the limit (e.g. an unusually long list of `limitations`), it is **split into multiple ordered parts, not truncated**. Content is never silently cut short — the full message is always delivered, as more than one Telegram message when necessary.
- Splitting breaks at paragraph, then line, then word boundaries (never mid-word) so each part reads naturally on its own. Each part beyond the first is prefixed with an explicit `(i/N)` ordering marker (e.g. `(2/3) ...`) so the athlete can tell it's a continuation and confirm nothing is missing, even if Telegram delivers the messages with a slight gap between them.
- Parts are sent strictly in order, one at a time. If any part fails to send, delivery stops immediately — later parts are never sent out of sequence or ahead of an earlier failed part. The delivery result reports exactly how many parts were sent before the failure (`parts_sent`), so a partial delivery is always distinguishable from a complete one, never reported as success.
- This is implemented in `runner/telegram_delivery.py`'s `split_message()` and `send_telegram_notification()`.

---

## Delivery Failure Handling

- Per `workflows/daily-review.md`'s Failure Handling rules: **Telegram delivery failure must not invalidate a successfully saved review.** The workflow's `status` (`completed`, `completed_with_limitations`, etc.) is fixed at the point the review is written and is never changed by what happens afterward in the Notify stage.
- A delivery failure is recorded as its own outcome, separate from workflow status — e.g. new fields on the runner's outer envelope (not the workflow's own structured output) such as `notification_delivered` (boolean) and `notification_delivery_error` (short, redacted string). These are proposed additions to the Structured Result Contract in `runner/workflow-runner.md`, to be made when delivery is actually implemented — not implemented by this document.
- If delivery fails, the athlete's only signal is the review file itself and the operational log entry — there is no secondary delivery channel at this stage. That gap is accepted for now and should be revisited once Telegram delivery has run in production long enough to know how often it actually fails.
- A delivery failure must never be retried by re-running the workflow. The workflow already completed successfully; only the notification attempt failed. Re-running the whole workflow to fix a notification would risk violating the one-completed-review-per-local-date idempotency rule for no benefit.

---

## Logging Rules

Consistent with `runner/workflow-runner.md`'s Logging section — operational logs record what the runner did, never coaching content, never secrets:

- Log a `notify_start` / `notify_end` event pair per delivery attempt, with the workflow name, execution ID, `notification_level`, and outcome (`sent`, `failed`, `skipped`).
- Never log the message body, the bot token, or the chat ID. If a value needs to appear for debugging (e.g. confirming which chat a message was routed to), log a fixed, non-reversible reference (e.g. "configured chat") rather than the ID itself.
- A failed HTTP response from the Telegram API is logged only after passing through the same secret-redaction step already applied to AthleteData/Claude errors, since Telegram error bodies can echo back request parameters.

---

## Retry Rules

- One delivery attempt per completed workflow execution, with a small bounded retry for **transient, network-level** failures only (e.g. up to one retry after a short fixed delay for a timeout or 5xx response) — not an indefinite or exponential retry loop.
- A failure that indicates a configuration problem rather than a transient issue (401/403 invalid token, 400 invalid chat ID) is not retried at all within the same execution — retrying won't fix a bad credential, and hammering the Telegram API on a broken config risks rate-limiting the bot entirely.
- No retry is attempted on a later scheduled run for a delivery that failed on an earlier one. Each workflow execution's Notify stage stands on its own, exactly once, matching the "no indefinite retry, retrying is a decision made outside the single execution" principle already established for the runner as a whole.

---

## Security Rules

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` live only in the server-local env file, loaded the same way as every other server-local secret in this system — never in Git, never in a unit file, never in a workflow definition.
- All Telegram API calls use HTTPS (inherent to the Bot API); no plaintext transport is possible or permitted.
- The chat ID must correspond to the athlete's own private chat with the bot only. It must never be a group or channel, which would broaden who can see coaching/health content.
- The bot is single-purpose. It is not reused for any other integration, project, or notification source, so a compromise of this bot's token cannot be leveraged to reach unrelated systems.
- If the token is ever exposed (committed accidentally, logged, shared), it must be revoked and rotated via BotFather immediately — the same urgency as any other credential leak.
- The delivery layer never accepts or executes instructions arriving from Telegram (moot at this stage, since no inbound path exists at all — but stated explicitly so a future two-way feature doesn't casually violate it).

---

## Separation From Workflow Logic

Telegram delivery is a third, thin layer, added below the two already established in `runner/README.md`:

- **Workflow definitions** decide *what* the coaching output is and *how urgent* it is (`notification_level`, `notification_required`, `recommendation_summary`).
- **The runner** orchestrates execution and guarantees a review is safely saved before anything else happens.
- **The delivery layer** (new) takes the already-decided structured result and transmits a formatted summary of it. It performs no coaching reasoning, calls no AthleteData or Claude tools, and cannot alter the workflow's `status`, `confidence`, or any other coaching-facing field. Its only judgment calls are presentational (how to format a level marker, how to split a long message) — never substantive.

This mirrors the existing rule that "adding a new workflow should never require changing the runner": adding or changing the delivery channel should never require changing a workflow definition, and vice versa. A workflow that produces a valid Structured Result Contract does not need to know Telegram exists at all.

---

## Reuse for Weekly and Monthly Reviews

The delivery layer depends only on the common Structured Result Contract envelope (`workflow`, `status`, `local_date`, `review_file`, `confidence`, `notification_level`, `notification_required`) that every workflow — daily, weekly, or monthly — already commits to producing, per the Extensibility section of `runner/workflow-runner.md`. It has no daily-review-specific logic anywhere in it.

Consequently, when Weekly Review and Monthly Review are implemented, they plug into the same Notify stage with zero changes to the delivery layer itself — exactly as they will plug into the runner itself with zero changes there. The only workflow-specific work at that point is each new workflow definition supplying correct `notification_level` and `recommendation_summary` values for its own domain (e.g. a weekly training-load trend versus a single day's readiness), which is already each workflow's own responsibility today.

---

## What This Document Is Not

This is not an implementation plan, a script, or working code. No Telegram bot has been created, no token exists, and `runner/config.env`'s `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` remain blank. Implementing this architecture — creating the bot, filling in real secrets, writing the delivery code, extending the Structured Result Contract with the proposed `notification_delivered` / `notification_delivery_error` fields, and sending a first real test message — is future work requiring separate, explicit approval.
