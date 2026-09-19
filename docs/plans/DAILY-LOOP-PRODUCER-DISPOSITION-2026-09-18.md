# Daily-LOOP Producer — Disposition

**Date:** 2026-09-18 10:47 UTC
**Decision owner:** Gilfoyle (architecture decision — not a Dan gate)
**Repo:** `prototypes/weather-engine-source`, branch `temp-sprint-branch` (dirty tree preserved)
**Trigger:** R8 runtime work (Ray) surfaced that the bridge's `daily_loop_output.json`
producer was absent post-pivot, leaving the h-24 daily anchor fail-closed (correct but dead).

---

## 1. Disposition: **RESTORE** (keep the producer; do not decommission the bridge)

The daily-LOOP producer is restored and retained. The bridge is **not** decommissioned.

The producer already exists and is correct: `scripts/daily_loop_producer.py` (rebuilt
2026-09-17 by Ray, currently **untracked** — see §4 for staging). It is a deterministic,
self-contained rebuild that regenerates `data/daily_loop_output.json` from the two
surviving ground-truth inputs (`metar_backfill.db::daily_stats` and
`nwp_forecasts.db::nwp_forecasts`). It runs clean (`--dry-run` verified 2026-09-18:
`mean_confidence=0.785`, `stations_with_signals=20`, `bridge_ready=true`).

---

## 2. Reasoning

### 2a. Is the h-24 anchor still needed as the intraday foundation?

**Yes — still a design requirement, not obsolete.**

The h-24 anchor is the daily Bayesian *prior* that the hourly loop updates as intraday
observations roll in. The pivot to intraday-first changed *emphasis* (the intraday loop
is now the product), but it did not remove the *need* for a daily foundation: the
intraday runner is architected to seed its h-24 estimate from the daily consensus and
then update hour-by-hour. Removing the producer would eliminate the only surviving
writer of that prior, leaving the hourly loop with no daily initialization path.

### 2b. What does the intraday runner actually depend on downstream?

**This is the decisive fact, and it is uncomfortable — the bridge is currently a dangling writer.**

Evidence (grep across all tracked code, excluding graphify/snapshot noise):

| Consumer | Reads `daily_loop_output.json`? | Reads `anchor_h24.json`? |
|---|---|---|
| `scripts/intraday_bridge.py` | ✅ (sole reader) | ✅ (sole writer) |
| `scripts/intraday_runner.py` (the live product) | ❌ | ❌ |
| `scripts/intraday_daemon.py` | ❌ | ❌ |
| `scripts/intraday_watchdog.py` | ❌ | ❌ |
| `tests/test_r8_fixes.py` | — | ✅ (asserts fail-closed only) |

`intraday_runner.py` reads `metar_backfill.db`, `iem_asos_1min.db`, `nwp_forecasts.db`,
`gefs_archive.db`, `ghcn_settlements.db` — **it never touches `anchor_h24.json` or
`daily_loop_output.json`**. The only reader of `anchor_h24.json` is the bridge that wrote it.

So the h-24 anchor is a **design seam that no current code consumes**. The v1.0 bridge
docstring ("the hourly pipeline consumes this as a h-24 prior") describes an *intended*
consumer that does not exist in the post-pivot tree. The canonical hourly scorer that
would consume it — `core/intraday/backtest.py::HourlyBacktest` — exists only as an
**orphaned `.pyc`** whose source was never committed (documented in
`INTRADAY-PATH-CALIBRATION-2026-09-17.md` §3).

### 2c. Why RESTORE rather than DECOMMISSION, given 2b?

Three reasons, ranked:

1. **The producer is already built and correct.** Decommissioning would throw away a
   working, deterministic artifact that cost real recovery effort, and would leave the
   fail-closed bridge permanently dead — trading a *dangling* seam for a *gone* seam.

2. **The prior is still the architectural intent.** The intraday-first pivot did not
   delete the h-24 prior from the design; it orphaned the consumer. The correct fix for
   a missing consumer is to *restore the consumer*, not to delete the producer that the
   consumer will need. Decommissioning now would force a second rebuild when the hourly
   scorer is re-integrated.

3. **Fail-closed is the correct posture, not a signal to delete.** The bridge correctly
   halts (non-zero, no write) when the producer is missing/stale. That is R8 working as
   intended. The fix is to keep the producer running on schedule so the bridge never
   halts — not to remove the bridge.

---

## 3. The real gap (do not paper over)

RESTORE closes the *producer* gap. It does **not** close the *consumer* gap:

- **`anchor_h24.json` has no live consumer.** The h-24 prior is not yet wired into the
  intraday runner. This is a separate, pre-existing architectural debt that predates
  the producer question and is **out of scope** for this disposition.
- **`core/intraday/backtest.py` source is missing** (orphaned `.pyc` only). Until that
  scorer is recovered/re-written and wired to consume the anchor, the daily prior will
  remain latent.

**Recommendation (for Dan/Gerri, not blocking this decision):** schedule a follow-up to
either (a) wire `anchor_h24.json` into `intraday_runner.py` as the h-24 prior seed, or
(b) formally record the h-24 anchor as *deprecated* and remove the bridge+producer in a
single, reviewed change. Either is a legitimate end-state; neither changes today's
RESTORE decision, because the producer must exist in the RESTORE branch and the
DECOMMISSION branch is only reachable after the consumer question is settled by (b).

---

## 4. Deliverables / staging (no commit, dirty tree preserved)

The producer is **untracked** and must be staged for Dan's merge approval. No commit is
made here, per boundary.

- **`scripts/daily_loop_producer.py`** — the restored producer (untracked `??`). Stage it:
  ```
  git add scripts/daily_loop_producer.py
  ```
- **`docs/plans/DAILY-LOOP-PRODUCER-DISPOSITION-2026-09-18.md`** — this file (new).
- **NOT staged:** the 13 modified tracked files (`core/signal_families.py`, `core/signals/*`,
  `scripts/*`) and all untracked graphify/snapshot/report/backup dirs — these are
  pre-existing dirty-tree state unrelated to this decision and are left untouched.

**Schedule wiring (no change required now):** the producer should run daily *before* the
bridge's 11:00 UTC cron (`WE-intraday-bridge-daily`). The producer's docstring specifies
"cron at 10:00 UTC / 6AM ET". A cron entry for `scripts/daily_loop_producer.py` is the
missing operational piece; flagging it as a follow-up (cron add is a live-infra change,
out of scope for this read-only disposition).

---

## 5. Receipt / verification

- Producer dry-run verified 2026-09-18: exit 0, `bridge_ready=true`,
  `stations_with_signals=20`, `mean_confidence=0.785`.
- Existing artifacts confirm the chain is coherent: `data/daily_loop_output.json`
  (`run_date=2026-09-17`, `mean_confidence=0.771`) → `data/intraday/anchor_h24.json`
  (`direction=UP`, `source=daily_loop`, `bridge_ts=2026-09-17T15:00`).
- No commit made. Dirty tree preserved (`git status --porcelain` unchanged except this new
  doc).

**Blocked / Owner / Dan needed / Meanwhile:**
- **Blocked:** h-24 anchor has no live consumer (`anchor_h24.json` unwired to intraday runner).
- **Owner:** Gilfoyle (consumer wiring / scorer recovery) or Gerri (deprecation call).
- **Dan needed:** No — this RESTORE decision is architectural and is now made. A later
  deprecation-vs-wire decision *would* need Dan/Gerri.
- **Meanwhile:** producer is restored and correct; bridge will stop fail-closing once the
  producer runs on schedule.
