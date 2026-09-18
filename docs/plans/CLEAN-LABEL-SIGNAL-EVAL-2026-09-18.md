# Clean-Label Signal Eval — Execution Receipt

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Runner:** `scripts/clean_label_signal_eval.py` (single deterministic pass)
**Result JSON:** `reports/clean_label_signal_eval_2026-09-18.json`
**Type:** READ-ONLY eval. No signal revived/re-wired/re-registered. No commit. No paper/live trading.

---

## 0. What this is

A single-pass directional eval of the three revived-signal candidates + one
reference against the **clean** settlement labels (the labels repaired by the
2026-09-17 settlement-epochs rewrite). It replaces the prior subagent that died
looping ~177k `evaluate_for_station` calls over 1h45m.

**Anti-loop guarantee:** per-station daily series are loaded ONCE into an
in-memory `days` list and each daily signal is driven via `evaluate(idx, days)`
— never `evaluate_for_station()` per call (which re-ran GROUP BY over the 2.4 GB
`metar_backfill.db` on every (station, date)). `nwp_analog`'s feature table,
per-station norm stats, and METAR highs are cached inside the signal object.
Total wall time: **78 seconds**.

---

## 1. Clean-label filter

| Field | Value |
|---|---|
| `market_type` | `HIGH` |
| `epoch_status` | `closed` |
| `settlement_bucket` | `[-22, 119]` |
| `prior_settlement_bucket` | `[-22, 119]` |
| Excluded `unresolved` rows | 28 |
| Excluded linkage rows (day-after-unresolved) | 18 |

The range filter on **both** buckets excludes the 28 `unresolved` rows (their own
bucket left corrupt) and the 18 linkage rows (their `prior_settlement_bucket`
points at an unresolved day's corrupt bucket) in one deterministic pass, per
`SETTLEMENT-EPOCHS-REWRITE-EXECUTED-2026-09-17.md` §2/§4. Direction = `up` if
today's bucket > prior, `down` if less; flat rows skipped.

Clean HIGH epochs scored: **52,299** across **30** stations.

---

## 2. Results (directional accuracy + MCC)

| Signal | Accuracy | MCC | n | fired stations |
|---|---|---|---|---|
| `pressure_delta` (reference) | **61.85%** | **+0.2375** | 30,676 | 30 |
| `corrected_pressure_delta` | **38.15%** | **−0.2375** | 30,676 | 30 |
| `nwp_analog` | **52.77%** | **+0.0627** | 10,791 | 20 |
| `goldilocks` microstructure spike-reversion edge | *N/A — intraday* | *N/A* | — | — |

Confusion (up = positive):

| Signal | TP | FP | TN | FN |
|---|---|---|---|---|
| `pressure_delta` | 9,475 | 5,524 | 9,497 | 6,180 |
| `corrected_pressure_delta` | 6,180 | 9,497 | 5,524 | 9,475 |
| `nwp_analog` | 2,469 | 1,975 | 3,225 | 3,122 |

---

## 3. Reading the numbers

**`corrected_pressure_delta` is the exact directional mirror of `pressure_delta`**
(38.15% vs 61.85%; MCC −0.2375 vs +0.2375; TP/FP/TN/FN transposed). This is the
physics-corrected inversion working as designed — rising pressure → `down`, not
`up`. The prior kill verdict (**MCC = −0.2167, "actively harmful"**) was measured
on the *inverted* logic against the pre-rewrite labels. Against clean labels the
plain `pressure_delta` direction wins at 61.85% / +0.2375 MCC, and the
physics-corrected flip scores the mirror (38.15% / −0.2375). **The physics
inversion does NOT beat the reference on clean labels** — it is its negation.

**`nwp_analog`** scores **52.77% / +0.0627 MCC** (10,791 fires across 20 stations).
Better than the historical 32.63% kill figure, but well below the Gray Room
≥58% / MCC threshold. The data-gating condition has been lifted (NWP DB populated,
20 stations), but the deterministic k-NN analog does not clear the revival bar on
clean labels.

**Goldilocks microstructure edge:** not scored (see §4) — the intraday label
surface is absent from this container.

---

## 4. Goldilocks microstructure spike-reversion edge — bottleneck (not scored)

The separated real-time `microstructure_spike_reversion` edge is an **intraday**
alert (emitted via `core/data_processor.py →
_compute_microstructure_spike_confidence`), driven by the `transition_events`
label surface — **not** a daily directional signal with an `evaluate(idx, days)`
interface. Its intraday label surface (`transition_events` in
`/var/data/alerts.db`) is **not present in this container**. It therefore cannot
be scored against the daily `settlement_epochs` HIGH buckets without fabricating
a label, and no number is produced here.

The daily-level `SpikeReversionSignal` (formerly `goldilocks`) remains
KEEP-KILLED per `KILLED-SIGNAL-REVIEW-2026-09-18.md` §3 and is intentionally
**not** scored as a revival candidate.

**Required to measure this edge:** the `transition_events` table (intraday
transition history, present only in `/var/data/alerts.db` at runtime), plus its
own intraday label surface. That data is not in this repo/container.

---

## 5. Determinism / no-mutation verification

- Single pass; 78s wall time; no tuning loop; stdlib-only (numpy already used by
  the nwp signal).
- Read-only on `data/metar_backfill.db`, `data/nwp_forecasts.db`.
- Row counts intact post-run: `settlement_epochs` = 117,517 (117,489 `closed`,
  28 `unresolved`). No `-wal`/`-journal` sidecars left behind.
- DB mtimes unchanged (no write).

---

## 6. Handoff / disposition

- **`corrected_pressure_delta`:** physics inversion confirmed working, but it is
  the negation of `pressure_delta` and does not clear the revival bar on clean
  labels. **Recommend KEEP-KILLED** (or, if ever reconsidered, only as an
  inverted reference — never as an independent edge).
- **`nwp_analog`:** 52.77% / +0.0627 MCC — below Gray Room ≥58% threshold.
  **Recommend KEEP-KILLED** pending a materially better clean-label figure.
- **`goldilocks` microstructure edge:** unmeasured — blocked on the intraday
  `transition_events` surface. **Not revive-able** from this container.
- **Owner of any follow-up on the microstructure edge:** Gilfoyle (needs runtime
  access to `/var/data/alerts.db`); decision gate for revival remains Dan.
