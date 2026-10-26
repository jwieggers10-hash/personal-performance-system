# Nutrition Workflows (Phase 5B)

Two scheduled jobs support the nutrition goals in `goals.md`:

| Job | When (Europe/Amsterdam) | Claude | Sends |
|---|---|---|---|
| Food-log check (`runner/food_log_check.py`) | 20:00 | No | At most one reminder per day, only when the log looks incomplete or unsynchronised |
| Evening Nutrition Review (`runner/evening_nutrition_review.py`, `workflows/evening-nutrition-review.md`) | 21:30 | Yes (≤ 2 calls) | The evening review, or one urgent notice if the run fails |

**Status: built and tested; not deployed.** The units in `runner/systemd/` are templates; nothing is installed or enabled.

| File | Role |
|---|---|
| `runner/nutrition_data.py` | Deterministic facts: targets from `goals.md`, food-log completeness, Garmin expenditure and projection, day type, body-composition trend. |
| `runner/food_log_check.py` | 20:00 check: decision, reminder text, per-day state, Telegram delivery. |
| `runner/evening_nutrition_review.py` | 21:30 entry point: facts and dispatcher coordination, then the shared workflow runner. |
| `workflows/evening-nutrition-review.md` | The coaching contract Claude follows. |
| `runner/systemd/personal-performance-{food-log-check,evening-nutrition-review}.{service,timer}` | Templates. |

Both read AthleteData through the read-only client (`runner/athletedata_client.py`): `food_log_get`, `garmin_get_daily_summaries`, `garmin_get_activities`, `hevy_get_workouts`, `apple_health_get_body_composition`, each on the allowlist and verified `readOnlyHint=true` via `tools/list` before use. No production nutrition data is ever written.

---

## Targets

Defined only in the Nutrition Targets section of `goals.md`; `nutrition_data.load_targets()` reads its fenced `nutrition-targets` JSON block. A missing or invalid block never stops the review: it reports the targets as unavailable and makes no target comparison. Day type picks the calorie target; a combined run-and-strength day uses its own capped bands rather than adding exercise calories. Carbohydrate is the remainder after the protein and fat targets.

## Food-Log Completeness

Cronometer has no meal times and no fixed sync cadence, so completeness is judged against the athlete's own recent pattern (`assess_completeness`):

1. **Pattern** — the last 28 days, learned only from *substantial* days (intake ≥ 65 % of the upper-quartile day), so partially logged days never lower the bar. A core slot (breakfast, lunch, dinner) is expected when logged on at least half of those days. Snacks never make a log incomplete on their own. Fewer than 5 substantial days: no pattern (`unknown`).
2. **Today** — `missing` (no entries); `incomplete` when an expected slot is absent or intake is below 65 % of the usual substantial day (three slots alone are not proof of completeness); `complete` otherwise, or when the log is flagged as a whole-day total.
3. **Freshness** — `stale` when the last Cronometer sync is more than 2 hours old or unknown: entries may not have synchronised yet.

### 20:00 decision

| Status | Action |
|---|---|
| `complete`, `unknown` | Nothing sent. |
| `incomplete`, `missing` | One reminder naming what looks missing; if stale, it says the entries may not have synchronised yet. |
| AthleteData unavailable | Nothing sent, nothing recorded, exit 1 (visible in `systemctl`); a manual rerun may try again. |

Duplicate prevention: the decision is recorded in `runtime/food-log-check/<date>.json` (owner-only), created exclusively **before** any send; any existing file for the day ends the run. A file lock prevents concurrent runs. The script acts only between 19:45 and 21:15 local (`--dry-run` excepted). Logs carry status, reason codes, slot names and sync age — never intake values or message text.

Telegram: one attempt; one retry only when Telegram cannot have received the message (HTTP 429/5xx other than 504, or a connect-phase network error), honouring `retry_after` up to 60 s. Ambiguous outcomes (504, read timeout, reset) and rejections are never resent.

## Energy Expenditure

From `garmin_get_daily_summaries` only: total = resting (`bmrKilocalories`) + active above resting (`activeKilocalories`). Activity calories are read for nothing but classification, so workout calories can never be added on top.

- **Completed days** (24 h records) are compared directly with complete food logs: per-day balance and a 7-day mean over days where both are complete.
- **Today** is a partial snapshot (`covers_hours`, `data_through_local`), always labelled "so far". A full-day **projection** is made only when the snapshot covers ≥ 16 h, reaches within 3 h of now, and ≥ 3 completed days exist: resting calories scaled to 24 h, plus remaining activity between none (low) and the median completed-day active rate (high). Otherwise the review says why no projection exists.
- **Today's balance** exists only when the log is `complete` and a projection exists, and is a projected range, never a single figure.
- Snapshot times are computed in UTC, so the 23- and 25-hour DST days are handled correctly.

## Evening Nutrition Review

`evening_nutrition_review.py --mode automated` → shared runner (`run_workflow`) with a `prepare_prompt` hook that runs under the review's lock, after the duplicate check and before Claude:

1. Computes the facts (`nutrition_data.build_facts`) and adds them to the prompt as authoritative JSON.
2. Waits, bounded at 25 minutes, for an in-flight feedback-dispatcher run to finish.

Everything else is the runner's, unchanged: one review per local date (`reviews/nutrition/<date>.md`, gitignored), `duplicate_skipped` without facts or Claude for a second automated run, one corrective retry for malformed output, classified Claude failures (authentication, quota, execution, timeout) ending as `failed` with one urgent Telegram notice and no automatic retry, and Telegram delivery with one retry for transient errors. An automated run outside 21:00–23:45 local does nothing. `workflow_runner.py --workflow evening-nutrition-review` is refused: the review always needs its facts.

### Coordination with the feedback dispatcher

- The review's lock (`runtime/locks/evening-nutrition-review-<date>.lock`) pauses the dispatcher's Claude calls exactly as the Daily Review's does (`feedback_dispatcher.coaching_workflow_running`); stale locks are ignored.
- A dispatcher generation already running may still make its at most two Claude calls; the review waits for the dispatcher's lock to be released (≤ 25 min), then proceeds. After a timeout no new dispatcher Claude call can start, so the review proceeds rather than skip the evening.
- The 20:00 check uses no Claude and needs no coordination.

---

## Deployment (requires approval)

1. Run the tests: `/usr/bin/python3 -m unittest discover -s runner/tests`
2. Read-only checks against production (no lock, no Claude, no Telegram, no state):
   ```bash
   /usr/bin/python3 runner/food_log_check.py --dry-run
   /usr/bin/python3 runner/evening_nutrition_review.py --print-facts
   ```
3. Optionally, one supervised manual review (one Claude call, one Telegram message):
   `/usr/bin/python3 runner/evening_nutrition_review.py --mode manual`
4. From an interactive root SSH session:
   ```bash
   cd /home/coach/personal-performance-system
   sudo install -m 644 -o root -g root \
       runner/systemd/personal-performance-food-log-check.service \
       runner/systemd/personal-performance-food-log-check.timer \
       runner/systemd/personal-performance-evening-nutrition-review.service \
       runner/systemd/personal-performance-evening-nutrition-review.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now personal-performance-food-log-check.timer personal-performance-evening-nutrition-review.timer
   systemctl list-timers 'personal-performance-*' --no-pager
   ```

No configuration change is needed: both use the existing `runner/config.env` (timezone, Claude, Telegram).

## Rollback

```bash
sudo systemctl disable --now personal-performance-food-log-check.timer personal-performance-evening-nutrition-review.timer
sudo rm /etc/systemd/system/personal-performance-{food-log-check,evening-nutrition-review}.{service,timer}
sudo systemctl daemon-reload
```

The Daily Review, event poller and feedback dispatcher are unaffected by either job's presence or absence.
