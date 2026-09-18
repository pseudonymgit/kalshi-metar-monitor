# R/T-1 Ultimate Suite — Canonical Sweep/Test/Calibrate Harness

**Date:** 2026-09-18
**Owner:** Gilfoyle (subagent, R/T-1)
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved)
**Entry point:** `scripts/ultimate_suite.py`
**Receipts:** `reports/ultimate_suite_<ts>.json` + `reports/ultimate_suite_<ts>.md`
**Type:** READ-ONLY. No signal changes, no wire-in, no revival. Dan holds wire-in/revival.

---

## 0. Purpose

One canonical entry point that sweeps **EVERYTHING** against the **correct ground truth**
(strike-crossing axis), covering all ten test layers. It is the harness R/T-2..4 consume:
a single source of pass/fail + MCC + accuracy + Sharpe for every signal, lane, gate,
fusion mode, calibration mode, and reusable experiment function.

It reuses existing sweep/backtest/calibrate code as CORE — it does **not** rebuild from
scratch. The label-loading, signal enumeration, evaluation routing, and metric logic are
lifted directly from:
- `scripts/killed_signal_retest_true_data_full.py` (label reconstruction + MCC)
- `scripts/sweep_engine.py` / `scripts/big_sweep.py` (signal registry + lane/gate surface)
- `scripts/phaseB_calibration_pipeline.py` (evaluation routing + signal lists)
- `scripts/phaseB_signal_benchmarks.py` (per-signal backtest structure)
- `core/settlement_outcome.py` (strike-crossing seam)
- `core/signal_families.py` (taxonomy)

---

## 1. The ten test layers (all accounted for)

| # | Layer | What it sweeps | Sweep |
|---|---|---|---|
| 1 | **directional** | signal direction vs strike-crossing label (MCC + acc + Sharpe) | `signal` |
| 2 | **intraday** | intraday signal surfaces (frontal_passage_intraday, spike_reversion, goldilocks, fogr) | `signal` |
| 3 | **predictions** | probability quality — Brier + ECE per signal | `calibration` |
| 4 | **trajectory / sub-hourly** | L4 Kalman / trajectory lane + gate | `lane_gate` |
| 5 | **fusion** | majority / weighted / UWC cascade modes | `lane_gate` |
| 6 | **calibration** | Platt / BMA / EMOS + per-signal fit | `calibration` |
| 7 | **entry timing** | spread / settlement-window / execution gates | `lane_gate` |
| 8 | **exit logic** | stop-loss / loss-limiter / drawdown gates | `lane_gate` |
| 9 | **risk / sizing** | Kelly / variance-weighted / continuous-Kelly | `lane_gate` |
| 10 | **crossfeed** | cross-model divergence / agreement consumers | `lane_gate` |

Layer → sweep mapping is explicit in `LAYER_SWEEPS`; `--layer` restricts to any subset.

---

## 2. Ground truth — correct axis, hot-swap

The label loader is a single seam, `load_labels(source)`, with two sources:

| Source | Flag | Today | After P0-B |
|---|---|---|---|
| `reconstruct` | `--truth reconstruct` (default) | **1,537 labels** — parsed from `kalshi_market_snapshots.db` T-contract tickers joined against `settlement_truth` date-tiered temps | superseded (kept for cross-check) |
| `settlement` | `--truth settlement` | **56 labels** — `settlement_truth.strike_price` (2026-09-07/08 only) | **full 2021→present** strike set |

Both sources produce the identical shape `{station: {(market_type, date): 'up'|'down'}}`,
so **switching from the 1,537 set to the full P0-B set requires no re-architecture** —
only the truth-source flag (or a code default flip once P0-B lands).

The label universe is **82% up-skewed** (1,264 up / 273 down). **MCC, not accuracy, is
the honest skill metric** — accuracy is misleading under this imbalance.

---

## 3. Results (correct axis, 1,537 labels)

### 3a. Signal sweep — headline (MCC is the metric)

The suite reproduces the killed-signal retest numbers exactly, confirming harness
correctness against the prior canonical run:

| Signal | acc | MCC | Sharpe | n | Note |
|---|---|---|---|---|---|
| `forecast_disagreement` | 61.66% | **+0.0314** | +3.81 | 1111 | best MCC of the daily set (still weak) |
| `gaussian_v2` | 58.70% | +0.0155 | +2.81 | 908 | |
| `pressure_delta` (live ref) | 57.79% | **+0.0225** | +2.51 | 526 | matches prior retest exactly |
| `corrected_pressure_delta` | 42.21% | −0.0225 | −2.51 | 526 | exact mirror of pressure_delta |
| `spike_reversion` | 18.69% | **+0.0000** | −12.75 | 444 | one-directional; zero information |
| `nwp_analog` | 29.78% | −0.0704 | −7.02 | 1417 | below any skill bar |
| `goldilocks` | 51.40% | +0.0341 | +0.45 | 891 | |
| `fogr_reversion` | 81.30% | **+0.0000** | +12.74 | 631 | one-directional (base-rate artifact) |
| `warm_momentum` | 79.89% | +0.0000 | +11.84 | 557 | one-directional |
| `cooling_failure` | 85.71% | +0.0000 | +16.20 | 63 | one-directional, underpowered |

**Key finding (unchanged):** every MCC is within ±0.07 — statistically indistinguishable
from zero skill on the correct strike-crossing axis. The "STABLE WRONG-WAY" / high-accuracy
flags are base-rate artifacts of the 82/18 imbalance. **No signal has genuine edge on the
real target yet.** This is a first-principles problem (signals predict "warmer than
yesterday," Kalshi pays out on "crossed the strike"), not an individual-signal bug.

### 3b. Calibration sweep (Brier / ECE)

Per-signal Brier and ECE computed against correct-axis labels. `forecast_disagreement`
(Brier 0.38) and `pressure_delta` (Brier 0.28) are the best-calibrated of the firing set;
no signal approaches a useful ECE (<0.05).

### 3c. Lane / gate / fusion / calibration-mode enumeration

All six lanes, nine gates, four fusion modes, and four calibration modes enumerate and
resolve as `available` (import smoke-test). This is the "accounts for all of it" proof
for the infrastructure layer.

### 3d. Experiment / function sweep

All reusable experiment functions resolve: DTW (a1–a5), Gaussian fusion, goldilocks
feature engineering, Kalman smoothing, trajectory DTW replay, NWP-analog kNN.

---

## 4. Usage

```bash
# Full suite (all layers), reconstruct truth (1,537 labels)
python3 scripts/ultimate_suite.py

# Enumerate everything, no scoring (the "accounts for all of it" proof)
python3 scripts/ultimate_suite.py --dry-run

# Hot-swap to the P0-B full-set truth source
python3 scripts/ultimate_suite.py --truth settlement

# Restrict to specific layers
python3 scripts/ultimate_suite.py --layer directional --layer calibration

# Restrict to specific sweeps
python3 scripts/ultimate_suite.py --sweep signal --sweep lane_gate
```

Outputs: `reports/ultimate_suite_<ts>.json` (structured, R/T-2..4 consumable) and
`reports/ultimate_suite_<ts>.md` (markdown summary).

---

## 5. Boundaries honored

- ✅ READ-ONLY. No signal re-enabled, re-registered, taxonomy-flipped, or wire-in.
- ✅ No destructive cleanup. No DB mutation (`-wal`/`-journal` sidecars cleaned).
- ✅ Git-clean — only `scripts/ultimate_suite.py`, this doc, and two receipts are new.
- ✅ No storage bloat — no `data/` writes (gitignored), no backup dirs.
- ✅ Correct axis throughout — `core/settlement_outcome` is the single label seam.

---

## 6. Handoff / escalation

- **Dan holds wire-in/revival.** This suite surfaces the numbers; it never re-enables.
- **P0-B (full strike backfill) is still the gating dependency** for a final revival
  verdict — the current 1,537-label set is a ~6-week window (Aug 6 → Sep 18). After P0-B
  lands, re-run with `--truth settlement` for the full 2021→present verdict.
- **The deeper finding (live signals lose edge on strike axis)** remains the larger
  first-principles re-baseline — out of scope for R/T-1, flagged for Gerri/Dan framing.
