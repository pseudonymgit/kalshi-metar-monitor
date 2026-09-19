# Calibration Repair — Isotonic Monotonicity + LOW-Market Build

**Date:** 2026-09-18
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)
**Runner:** `scripts/calibration_repair_2026_09_18.py` (deterministic, read-only on DBs)

## 0. What this fixes

`data/platt_calibration_liop_v4.json` had three structural problems blocking
system-wide usefulness:

1. **Negative beta (anti-monotonic Platt)** in three cells — higher raw score
   maps to *lower* calibrated P(correct). Actively wrong, not just thin.
2. **Missing LOW market** — all 8 cells are HIGH; the down-strike market has
   no calibrator at all.
3. **Thin up-direction cells** (n=64,71,105) — up is the hard prediction
   direction.

## 1. Negative-beta cells — now monotonic

Root cause: the LIOP fused raw confidence is degenerate (concentrated near a
single value ~0.79), so Platt MLE on thin cells finds a spurious anti-monotonic
slope. Isotonic regression is monotonicity-constrained by construction and
cannot invert — it degenerates to a flat/near-flat curve at the empirical
P(correct) instead of overfitting an inverted logistic.

### 1a. Surgical v4 repair (`platt_calibration_liop_v4_repaired.json`)

| Cell | old beta | new method | flat calibrated P |
|---|---|---|---|
| `down_HIGH_heuristic_coastal_humid` | -0.239 | isotonic_flat | 0.5 (identity (no bin signal)) |
| `down_HIGH_heuristic_coastal_warm` | -0.68 | isotonic_flat | 0.5909 (single-bin P(correct)) |
| `up_HIGH_heuristic_coastal_warm` | -0.494 | isotonic_flat | 0.8594 (single-bin P(correct)) |

### 1b. Clean-label regenerated calibrator (`platt_calibration_liop_v5.json`)

Built from clean settlement labels + the heuristic raw confidence
(`pressure_delta`, direction-agnostic HIGH/LOW). Every cell is monotonic:
Platt only when beta ≥ 0, isotonic otherwise.

| Cell | old beta | new method |
|---|---|---|
| `down_HIGH_heuristic_coastal_humid` | -0.239 | platt beta=0.1059 |
| `down_HIGH_heuristic_coastal_warm` | -0.68 | isotonic |
| `up_HIGH_heuristic_coastal_warm` | -0.494 | platt beta=0.2823 |

## 2. LOW-market calibrator coverage

The v4 calibrator had 8 HIGH cells and **zero LOW cells**. The LOW (down-
strike) market now has its own block, built by mirroring HIGH on the LOW
settlement surface (min-temp buckets) with the same heuristic raw confidence.
A warming pressure trend predicts warmer max AND warmer min, so the raw
confidence is direction-agnostic to the HIGH/LOW split.

**LOW cells fitted: 10** (of 10 possible: 2 dir × 5 climate).

| Cell | n | method | ECE | Brier |
|---|---|---|---|---|
| `down_LOW_heuristic_coastal_humid` | 1788 | isotonic | 0.0 | 0.245973 |
| `down_LOW_heuristic_coastal_warm` | 1331 | platt | 0.007703 | 0.241903 |
| `down_LOW_heuristic_continental` | 3417 | isotonic | 0.0 | 0.229499 |
| `down_LOW_heuristic_desert` | 1993 | platt | 0.0 | 0.243405 |
| `down_LOW_heuristic_interior` | 7125 | isotonic | 0.0 | 0.238457 |
| `up_LOW_heuristic_coastal_humid` | 1686 | isotonic | 0.0 | 0.22738 |
| `up_LOW_heuristic_coastal_warm` | 1302 | platt | 1e-06 | 0.244774 |
| `up_LOW_heuristic_continental` | 3376 | platt | 0.001939 | 0.221046 |
| `up_LOW_heuristic_desert` | 1899 | isotonic | 0.0 | 0.227744 |
| `up_LOW_heuristic_interior` | 6796 | platt | 0.005616 | 0.229322 |

**HIGH cells: 10** (of 10 possible). Full grid: 20 cells
(2 dir × 2 market × 5 climate).

## 3. Remaining thin cells (need data accumulation, not code)

In the clean-label regenerated calibrator, **no cell is below the n=200
isotonic floor** — clean labels give every heuristic cell n≈1300–7100. The
thin-ness of the v4 up-cells (n=64,71,105) was an artifact of the degenerate
LIOP fusion, not a data shortage. With clean settlement labels, the up
direction is well-sampled and monotonic.

No thin cells remain in the clean-label calibrator.

## 4. Scope compliance

- READ-ONLY on `metar_backfill.db`; no `-wal`/`-journal` sidecars left behind.
- No live/paper trading. No destructive changes to the working tree.
- Did **not** overwrite the v4 file; wrote `_v4_repaired` + `_v5` artifacts.
- **No commit** — staged for Dan's merge approval (weather-engine merge policy).
