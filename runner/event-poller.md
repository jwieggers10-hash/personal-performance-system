# Event Poller (Phase 4.2 — detect-only)

The event poller notices when new training or body-composition data has reached AthleteData, and records it. It is **detect-only**: it never invokes Claude, never sends Telegram messages, and never produces coaching feedback. It records which events *would* be eligible for feedback so a later phase can act on them under the confirmed rules (hybrid feedback, quiet hours 22:00–07:00 Europe/Amsterdam, grouped delivery after the 07:00 Daily Review).

It is independent of the Daily Review: separate code path, lock, state and systemd units.

---

## Architecture

```
systemd timer (:02/:17/:32/:47) -> event_poller.py --once
    -> load ATHLETE_TIMEZONE (runner/config.env), take runtime/locks/event-poller.lock
    -> athletedata_client.py: initialize -> tools/list (verify read-only) -> tools/call x3
         garmin_get_activities | hevy_get_workout_events | apple_health_get_body_composition
    -> per source: fetch everything -> one SQLite transaction (apply + source state)
    -> cross-source pass (Garmin/Hevy strength duplicates)
    -> poll_runs row, JSON summary to stdout, JSON log lines to stderr (journal)
```

| File | Role |
|---|---|
| `runner/athletedata_client.py` | Stdlib MCP Streamable-HTTP client: JSON and SSE replies, bounded retry, strict read-only tool allowlist verified against `tools/list` `readOnlyHint`. |
| `runner/event_store.py` | SQLite schema, transactions, keyed HMAC fingerprints, status summary. |
| `runner/event_poller.py` | Fetchers, parsers, change detection, cross-source rules, CLI. |
| `runner/systemd/personal-performance-event-poller.{service,timer}` | Inactive units (not installed). |

Reused from the Daily Review runner: `load_env_file`, `redact_secrets`, `WorkflowLock`.

A poll that finds nothing new costs six HTTP requests (initialize, the initialized notification, tools/list, and one call per source; Hevy adds a request per extra page of ten events) and no Claude invocation — about 576 requests a day at 15-minute intervals.

---

## Data-Source Behaviour and Limitations

Structures verified against production in Phase 4.1 unless stated otherwise.

### Garmin — `garmin_get_activities` (verified)
- Identity: `activityId` (fallback `summaryId`). Rows with neither are counted as `unidentifiable` and skipped.
- No created/updated timestamps exist, so edits are detected by a keyed fingerprint over the summary fields (`GARMIN_FINGERPRINT_FIELDS`).
- Window: rolling 3 local days every poll; the first poll of each local day widens to 7 days (reconciliation). Activities arrive by Garmin webhook and can be late; anything first seen inside the window is still new.
- Absence is never deletion. An activity missing from a *complete* (non-truncated) response for 3 consecutive polls gets a `missing_suspected` audit entry; the row is kept, and a later sighting records `reappeared`.
- Strength sessions logged in Hevy currently do not appear in Garmin at all for this athlete (AthleteData `coverage_gap`), so the Garmin/Hevy duplicate rule is defensive.

### Hevy — `hevy_get_workout_events` (verified for `updated`; `deleted` unverified)
- Identity: `workout.id`. New = unseen id; edited = changed fingerprint with a newer `updated_at`. An older version re-read never overwrites a newer one.
- Incremental: `since = cursor − 2 h` (first run: now − 14 days). Every page is read before anything is written; any page failing, or more than 20 pages, writes nothing and leaves the cursor unchanged. The cursor only moves to the latest `updated_at` actually processed — never to "now".
- Late logging is handled because events are keyed on `updated_at`, not session time.
- `deleted` events are parsed tolerantly (`id`/`workout_id`) and mark the row `deleted` with an `unverified_structure` audit detail; the row is never removed. `HEVY_DELETE_STRUCTURE_VERIFIED` should be set once a real delete has been observed.

### Apple Health — `apple_health_get_body_composition` (**not yet verified**)
- No real records were available in Phase 4.1 (none in 30 days); with no data the tool returns prose, recorded as `no_data`.
- The parser accepts the documented `{weight: [...], bodyFat: [...]}` shape with common date/value/id key names. Any other shape is `structure_unrecognized`: counted as a source failure, no records stored, no baseline taken.
- Identity: `HMAC(metric, date, normalized value)` — weight to 0.01 kg, body fat as a fraction to 0.0001 (percent inputs converted). Duplicate UUIDs and same-value duplicates collapse to one event; two different values for the same metric on the same date are both kept and flagged `conflict`.
- Window: rolling 4 days; first poll of each local day widens to 14 days.
- **No Apple Health event is feedback-eligible** (`unverified_structure`) until `APPLE_HEALTH_STRUCTURE_VERIFIED` is set after validating real records.

### Cross-source (strength)
- Hevy is authoritative. A Garmin strength activity (`activityType` contains `STRENGTH`) still awaiting feedback is `superseded` by a Hevy workout whose time range overlaps it (±15 min slack; if an end time is unknown, starts within 30 min). Same date alone never matches.
- An unmatched Garmin strength activity is `held` for 24 h, and released to `pending` only once Hevy has been polled successfully *after* the hold expired — a Hevy outage never releases it early. A Hevy workout arriving after release still supersedes it while feedback is pending.
- Superseding never deletes: the Garmin row stays, with `superseded_by` and an audit entry.

### Backfill guard
Any record first seen with an event date more than 3 days before the athlete's local today is `suppressed_backfill`. Unknown dates are treated the same way.

---

## Credential Handling

- The AthleteData MCP URL (API key in its query string) is read on every run from Claude Code's own registration in `~/.claude.json` (`projects[<repo>].mcpServers.athletedata`, falling back to user scope). It is held in memory only; it is never copied into another file, logged, printed, or included in an exception.
- The file must be owner-only (`0600`); anything broader is refused (`credential_unavailable`).
- Client errors carry fixed messages and HTTP status codes only; response bodies and prose tool output are discarded.
- **Limitation:** this relies on Claude Code's internal config format, which a CLI update could change, and Claude Code rewrites the file while it runs (a read can race a write). Both surface as `credential_unavailable` for that poll and are retried on the next tick; no insecure fallback exists. If the format changes, the fix is to adapt `load_mcp_endpoint()`, not to copy the key elsewhere.
- `runtime/event-poller.key` is a per-installation HMAC key (`0600`, created on the first `--once`). It is not an API credential; it only makes stored fingerprints and Apple Health identity keys unrecoverable without it. Deleting it while `events.sqlite3` exists is refused, because every stored fingerprint would then look edited.

---

## Database Schema

`runtime/events.sqlite3` (`0600`; `runtime/` is gitignored). Schema version 1.

| Table | Contents |
|---|---|
| `meta` | `schema_version`. |
| `source_state` | One row per source: `baseline_completed_at`, `cursor` (Hevy), `last_attempt_at`, `last_success_at`, `last_reconciliation_date`, `health` (`ok`, `no_data`, `degraded`, `failing`, `structure_unrecognized`), `last_error_category`, `consecutive_failures`. |
| `events` | `UNIQUE(source, event_key)`. `event_type`, `activity_type`, `local_date`, `start_time_utc`, `end_time_utc`, `source_updated_at`, `fingerprint` (HMAC), `state`, `feedback_eligible`, `feedback_status`, `superseded_by`, `hold_until`, `conflict`, `missing_count`, `first_seen_at`, `last_seen_at`. |
| `event_changes` | Append-only audit: `baseline`, `new`, `edited`, `deleted`, `restored`, `superseded`, `released`, `missing_suspected`, `reappeared`, `conflict_detected`, with a fixed-vocabulary `detail`. |
| `poll_runs` | One row per poll: outcome and the counts-only summary. |

`state`: `baseline`, `new`, `edited`, `deleted`, `held`, `superseded`.
`feedback_status`: `pending` (the only eligible status), `held`, `suppressed_baseline`, `suppressed_backfill`, `suppressed_superseded`, `suppressed_deleted`, `unverified_structure`. Both are enforced by `CHECK` constraints.

Never stored: workout contents, set/rep/load data, heart rate, distance, body-weight or body-fat values, prose tool output, credentials. Event keys are provider identifiers (Garmin activity id, Hevy workout UUID) or HMAC digests (Apple Health).

---

## First-Run Baseline

Per source, independently: the first poll that returns a *structured* response registers every record it sees as `baseline` / `suppressed_baseline`. A source whose first polls fail, return prose (`no_data`), or return an unrecognized structure takes its baseline later, on its own first structured response — so a source connected (or resumed, like Apple Health) later never floods history as new events.

---

## Operations

Logs are JSON lines on stderr (the journal under systemd), with counts, durations, categories and fixed tokens only — never identifiers, values, URLs or keys. Events: `poll_start`, `source_poll`, `cross_source`, `poll_end`, `poll_skipped` (lock held). Optional `EVENT_POLLER_LOG_FILE` must point inside `runtime/` when run under the hardened unit.

Exit codes: `0` ok, partial, or skipped because another poll holds the lock; `1` every source failed, or a fatal store/key problem; `2` configuration error.

```bash
# Health and counts (read-only; no identifiers)
/usr/bin/python3 runner/event_poller.py --status

# Recent runs under systemd
journalctl -u personal-performance-event-poller.service --since today -o cat
```

`health` moves to `degraded` on the first failure and `failing` at 4 consecutive failures; one success resets it. Rate limits (HTTP 429) and transient 5xx/timeouts are retried at most twice per request (2 s, 5 s; `Retry-After` honoured up to 30 s).

---

## Dry Run

```bash
cd /home/coach/personal-performance-system
/usr/bin/python3 runner/event_poller.py --dry-run
```

Performs the same read-only AthleteData calls and the full detection pipeline against an **in-memory copy** of the store; nothing is written (no database, key, or lock file is created or changed). Without an existing store it shows what a first baseline would register. The stdout summary contains counts only.

---

## Shadow-Mode Validation (before any feedback phase)

1. Run `--dry-run`; confirm all three sources respond and counts look plausible.
2. With approval, run `--once` manually to take the baseline; check `--status` shows only `baseline` events.
3. Install and enable the timer (below). Leave it running 48–72 h.
4. Daily, compare `--status` and the journal with what the athlete actually did: every real Garmin run and Hevy session should appear once as `new`/`pending`, nothing should be duplicated, strength sessions should not be double-counted, failures should be rare and self-healing.
5. When Apple Health starts delivering records again, validate the structure against real records before setting `APPLE_HEALTH_STRUCTURE_VERIFIED`.

---

## Deployment (requires approval; not done in Phase 4.2)

```bash
cd /home/coach/personal-performance-system
/usr/bin/python3 -m unittest discover -s runner/tests        # full suite
/usr/bin/python3 runner/event_poller.py --dry-run             # no state written
/usr/bin/python3 runner/event_poller.py --once                # takes the baseline
/usr/bin/python3 runner/event_poller.py --status

sudo cp runner/systemd/personal-performance-event-poller.service /etc/systemd/system/
sudo cp runner/systemd/personal-performance-event-poller.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now personal-performance-event-poller.timer
systemctl list-timers 'personal-performance-*'
```

The Daily Review units are not touched by any of these steps.

## Rollback

```bash
sudo systemctl disable --now personal-performance-event-poller.timer
sudo rm /etc/systemd/system/personal-performance-event-poller.service \
        /etc/systemd/system/personal-performance-event-poller.timer
sudo systemctl daemon-reload
# Optional, only to discard detection history (state is otherwise inert):
#   rm runtime/events.sqlite3 runtime/event-poller.key
git revert <phase 4.2 commit>
```

Removing the poller has no effect on the Daily Review: it shares no state, lock or unit with it.
