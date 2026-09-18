# M-Workstream Calibration Execution — Receipt

**Date:** 2026-09-17 UTC
**Author:** Ray Donovan (subagent — script execution)
**Repo:** `prototypes/weather-engine-source`, branch `temp-sprint-branch`
**Type:** SCRIPT EXECUTION ONLY. No calibration logic written, modified, or re-derived.
**Scope compliance:** Ran the two canonical calibration entry points. Wrote only this receipt doc + the scripts' own gitignored output artifacts. No source-code mutation. No data mutation. No commit. No paper/live trading. No daemon restart.

---

## 0. Verdict: **PASS** (nonzero receipts produced)

Calibration now produces **nonzero calibration receipts** against the clean
settlement data. This is a direct contrast to the prior "zero-station" failure.

| Metric | Value |
|---|---|
| Stations with nonzero calibration (pipeline #1) | **30** (of 31 closed-epoch stations; `TEST` row skipped — 0 days) |
| Per-signal-per-city calibrators fitted (pipeline #1) | **89** |
| Total calibration observations collected | **87,889** |
| Signals with trades (pipeline #2) | **7 / 11** |
| Signals returning zero (pipeline #2) | **4** |

---

## 1. Commands run (exact)

```bash
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source

# Entry point 1 — canonical 6-signal pipeline wrapper
python3 scripts/run_calibration_pipeline.py --db-path data/metar_backfill.db

# Entry point 2 — canonical all-signal calibrator (needs project root on PYTHONPATH
# because core/calibrate_all_signals.py does not self-insert project root; it does
# `from signals import ...` which resolves only when the repo root is importable)
env PYTHONPATH=/home/node/.openclaw/workspace/prototypes/weather-engine-source \
    python3 core/calibrate_all_signals.py
```

Note: `core/calibrate_all_signals.py` fails with `ModuleNotFoundError: No module
named 'core'` when run bare (`python3 core/calibrate_all_signals.py`), because the
`signals` package imports `core.station_effects`. `scripts/run_calibration_pipeline.py`
handles this correctly by inserting the project root into `sys.path`. Running the
second entry point requires `PYTHONPATH=<repo-root>` (or running from repo root with
the root already importable). This is a pre-existing wiring quirk, not a blocker.

---

## 2. Entry point 1 — `scripts/run_calibration_pipeline.py`

Signal registry restricted to 6 desired signals; only 3 fired (persistence,
simple_trend, goldilocks produced 0 trades at every station):

```
Loaded 6 signals: ['persistence', 'simple_trend', 'gaussian', 'gaussian_v2', 'pressure_delta', 'goldilocks']
Testing on 31 stations
...
Total observations collected: 87889

Calibration Summary
Per-signal-per-city calibrators: 89
Global signal fallbacks: 3
Global calibrator: fitted
Total history entries: 87889

Sample Calibrations (gaussian@KNYC)
  raw=0.30 -> calibrated=0.653
  raw=0.50 -> calibrated=0.653
  raw=0.70 -> calibrated=0.653
  raw=0.90 -> calibrated=0.653
  raw=1.00 -> calibrated=0.653
```

**Interpretation:** 89 nonzero station-level calibrators across 30 stations.
Every one of the 31 settlement-epoch stations except `TEST` (0 days) yielded
`station_days` far above the 250-day minimum (1695–2083 days each). The pipeline
no longer collapses to zero stations.

Caveat: the `gaussian@KNYC` sample maps every raw confidence to the same
calibrated value (0.653). This is the isotonic calibrator collapsing to the base
rate (the marginal correct rate) because gaussian's raw z-score confidence is a
poor ranker of correctness — see §4 for the accuracy reality. This is expected
isotonic behavior, not a failure: the calibrator is "fitted", it just learns the
signal is weak.

---

## 3. Entry point 2 — `core/calibrate_all_signals.py` (authoritative accuracy + confidence)

Walk-forward (180-day train / 30-day test) isotonic calibration, 20 stations,
11 signals in registry. Full per-signal table:

| Signal | Raw acc | Trades | Cal acc | Raw Brier | Cal Brier | Coverage |
|---|---|---|---|---|---|---|
| calendar_climatology | 27.25% | 8,748 | 27.25% | 0.29424 | 0.19527 | 100.0% |
| cloud_cover_index | — | 0 | — | — | — | — (no trades) |
| cross_model_divergence | — | 0 | — | — | — | — (no trades) |
| feels_like_delta | — | 0 | — | — | — | — (no trades) |
| forecast_disagreement | 39.71% | 16,623 | 39.71% | 0.41617 | 0.23376 | 100.0% |
| frontal_passage_intraday | — | 0 | — | — | — | — (no trades) |
| gaussian | 30.62% | 16,158 | 30.62% | 0.69377 | 0.21110 | 100.0% |
| gaussian_v2 | 37.46% | 25,900 | 37.46% | 0.54489 | 0.23320 | 100.0% |
| metar_trend | **53.14%** | 37,797 | **53.14%** | 0.24681 | 0.24238 | 97.43% |
| pressure_delta | 49.56% | 21,611 | 49.56% | 0.33344 | 0.24842 | 100.0% |
| radiational_cooling | 48.63% | 1,865 | 48.63% | 0.25634 | 0.23066 | 99.73% |

**Aggregate:**

| Metric | Value |
|---|---|
| Signals with trades | 7 / 11 |
| Avg raw accuracy | **40.91%** |
| Avg calibrated accuracy | **40.91%** |
| Avg accuracy delta | 0.00 pp |
| Avg raw Brier | 0.39795 |
| Avg calibrated Brier | **0.22783** |
| Avg Brier improvement | **+0.17013** |

**Key facts:**

1. **Directional accuracy** is real and reported: best signal `metar_trend` at
   53.14% over 37,797 trades; aggregate 40.91%. Note raw == calibrated accuracy
   everywhere because calibration adjusts **confidence**, not direction — the
   direction label is unchanged by isotonic calibration.

2. **Calibrated confidence** is real and materially improved: aggregate Brier
   falls from 0.398 → 0.228 (+0.170). The biggest absolute wins are on the
   signals whose raw confidence was badly miscalibrated (gaussian 0.694→0.211,
   gaussian_v2 0.545→0.233). This is the calibration working as designed.

3. **Directional accuracy is below the Gray Room gate (≥58%).** No signal clears
   58%. `metar_trend` (53.14%) is the closest. This is a *true negative* finding,
   not a bug: the clean settlement data now produces honest, weak directional
   skill rather than the fabricated garbage that the corrupt buckets previously
   produced.

---

## 4. Sharpe — NOT produced by the calibration entry points

The task listed Sharpe as a required output. **Neither canonical calibration
entry point computes Sharpe.** `grep -i sharpe` across
`scripts/run_calibration_pipeline.py`, `core/calibrate_all_signals.py`, and
`core/calibration_pipeline.py` returns zero matches.

Sharpe is a **trading/backtest metric**, not a calibration metric. It lives in
`core/paper_trading_engine.py::compute_sharpe()` (recently corrected to default
cold-start Sharpe `1.0 → 0.0`, per `FEE-SHARPE-CORRECTNESS-FIX-2026-09-17.md`),
fed by a P&L stream from the paper-trading loop — a different, downstream layer.

**Sharpe is therefore reported as NOT AVAILABLE from the M-calibration run.**
Producing it would require running the backtest/paper loop (which the task
explicitly forbids: "Do not run paper/live trading"). A Sharpe number cannot be
honestly attached to a calibration receipt; doing so would be fabrication.

**Action for Dan/Gerri:** if the M-workstream genuinely requires a Sharpe
alongside calibration, the correct source is the backtest layer
(`core/paper_trading_engine.py`), run separately from calibration. The calibration
receipt's honest outputs are directional accuracy + calibrated confidence (Brier).

---

## 5. Why 4 signals return zero (precise, per the mandate to diagnose)

The 4 zero-trade signals are: `cloud_cover_index`, `cross_model_divergence`,
`feels_like_delta`, `frontal_passage_intraday`.

Root cause is in `core/calibrate_all_signals.py::calibrate_signal_single`: it calls
`signal_obj.evaluate(idx, days)` against **daily-aggregated** data
(`load_station_data` produces daily max/min/avg/temp/dewpoint/wind/pressure rows).
These four signals require intraday or feature inputs not present in the daily
aggregate (e.g. cloud-cover series, multi-model divergence, feels-like temperature,
intraday frontal-passage timing). Their `evaluate(idx, days)` returns
`(None, *)` / non-positive confidence for every `idx`, so `if direction is None or
confidence < 0.0: continue` drops every sample → zero trades.

This is a **wiring/feature-availability gap**, not a data-corruption regression:
these 4 signals were never wired to the daily-aggregate `evaluate(idx, days)`
interface used by this calibrator. They are unrelated to the settlement-epochs
cleanup (which affected only `settlement_bucket` ground-truth values).

The 7 signals that DO trade (`calendar_climatology`, `forecast_disagreement`,
`gaussian`, `gaussian_v2`, `metar_trend`, `pressure_delta`,
`radiational_cooling`) all implement the daily `evaluate(idx, days)` contract.

---

## 6. Ground-truth state verified (settlement data is clean)

Confirmed the settlement-epochs rewrite landed and is in the expected end-state:

| Check | Result |
|---|---|
| `settlement_epochs` total | 117,517 |
| `epoch_status='closed'` | 117,489 |
| `epoch_status='unresolved'` | 28 |
| Corrupt buckets (>150 or <-50) remaining | **28** (all = the 28 intentional `unresolved` rows) |
| Closed-row bucket range | [-22, 119] — clean |
| Closed-epoch distinct stations | 31 (incl. 1 `TEST` row, 0 days) |

The "625 corrupt buckets → 0 residual" claim is **confirmed**: the 625 corrupt
rows were reduced to 28 intentional `unresolved` rows (left with still-corrupt
buckets by design, to be excluded by downstream backtests rather than silently
consumed). All 117,489 `closed` rows are clean. The calibration run above
therefore consumed clean ground truth for every `closed` epoch.

---

## 7. Write-scope / side-effects disclosure

| Item | State |
|---|---|
| Calibration code | NOT modified |
| Source/data (DB) | NOT mutated |
| `data/calibrated_pipeline.pkl` | overwritten by pipeline #1 (gitignored; script's intended output) |
| `data/calibration_all_signals.json` | overwritten by pipeline #2 (gitignored; script's intended output) |
| This receipt doc | written (gitignored `data/`/`docs/plans/` — untracked) |
| Git commit | none |

No repo tracked-file dirtiness introduced by this run (both artifacts are
gitignored under `data/`).

---

## 8. Handoff / next action

- **Dan/Gerri:** calibration is now producing honest nonzero receipts on clean
  settlement data. Directional accuracy is real but weak (aggregate 40.91%,
  best signal 53.14%) — below the ≥58% Gray Room gate.
- **Sharpe is out of scope for this calibration run** (see §4); it requires the
  backtest/paper layer, which was explicitly excluded from this task.
- **4 signals (cloud_cover_index, cross_model_divergence, feels_like_delta,
  frontal_passage_intraday)** are wired only to intraday/feature inputs, not the
  daily `evaluate(idx, days)` interface — a pre-existing feature-availability gap,
  unrelated to settlement cleanup.

Receipt: `commit=n/a` (script execution + doc only, no tracked changes).
