# Paper Trading Engine Audit — 2026-07-06

**Auditor:** Gilfoyle  
**Date:** 2026-07-06 07:00 UTC  
**File audited:** `core/paper_trading_engine.py` (v2.0)  
**Comparison:** `scripts/comprehensive_split_backtest.py` (7-signal ensemble backtest) + `core/signal_fusion.py` (SignalFusionEngine)

---

## Executive Summary

The paper trading engine is a **stripped-down system** that shares almost nothing with the backtest ensemble beyond the underlying METAR database. It runs 3 inline signals with naive weighted-average probability, no fusion engine, no calibration, no DS conflict detection, no time-decay reliability, and no isotonic calibration. The backtest ensemble runs 7 signals with LLOP fusion, time-decay reliability management, and DS conflict detection. **These are two entirely different systems.**

---

## 1. What signals does `generate_signals()` actually use?

The paper trading engine's `generate_signals()` method produces signals from **3 sources** (plus a 4th degraded path):

| # | Signal Name | Method | Description |
|---|-------------|--------|-------------|
| 1 | **Temperature reversion** | `_get_prior_day_reversion()` | Computes prior-day settlement delta; if abs(delta) > 2, bets on reversal |
| 2 | **Calendar climatology** | `_get_calendar_climatology_direction()` | Historical average settlement change for same MM-DD; if abs(avg) > 1.5, bets on that direction |
| 3 | **Late-day momentum (hourly)** | `_ldm_hourly_signal()` from `core/late_day_momentum_hourly.py` | Hourly late-day temperature trend with threshold=1.7 |
| 4 | **Late-day METAR momentum** (degraded) | `_analyze_late_day_momentum_signals()` | Same-day METAR temp trend; only fires for today/yesterday; hardcoded to 6 stations only |

**Signals 1 and 2 are inline methods** — they're defined directly in `paper_trading_engine.py` and query the settlement_epochs table for simple historical averages.

**Signal 3** is imported from `core/late_day_momentum_hourly.py` and is the only properly modular signal.

**Signal 4** is a degraded afterthought that only fires for recent dates and 6 hardcoded stations.

**Backtest ensemble has 7 signals:**
1. Reversion (30d)
2. Gaussian (48d)
3. Regime (DTR-scaled)
4. Gaussian v2 (30d)
5. Pressure (delta)
6. Calendar climatology (60d)
7. Goldilocks [R4-1.5]

The paper trading engine uses **only 2 of the 7 backtest signals** (reversion and calendar climatology), and its implementations are simpler inline versions. It's missing Gaussian, Gaussian v2, Regime, Pressure, and Goldilocks entirely. Late-day momentum hourly exists in paper trading but NOT in the backtest ensemble.

---

## 2. What fusion logic does it use?

**None.** The paper trading engine has no fusion logic whatsoever.

Each signal independently generates a `(station, market_type, direction, reason)` tuple. These signals are collected in a flat list and then each one is processed independently through `place_paper_trade()`. There is no aggregation, no voting, no weighted combination, no quorum check.

If two signals disagree on the same station, they both generate trades — potentially in opposite directions. There is no mechanism to detect or resolve this.

**Backtest ensemble uses:**
- **LLOP (Log-odds Linear Opinion Pool):** Converts each signal's confidence to log-odds, takes the weighted sum, and converts back via sigmoid. This is a principled probabilistic fusion that respects the geometry of probability space.
- **Confidence threshold:** Only fires when ≥2 signals agree and the ensemble confidence ≥ 0.7 (or 0.55 in calibrated mode).
- The `SignalFusionEngine` in `core/signal_fusion.py` implements this with 4 layers: calibration, MI-based decorrelation, LLOP, and DS conflict detection.

---

## 3. Does it use the `SignalFusionEngine` from `core/signal_fusion.py`?

**No.** The paper trading engine does not import or reference `signal_fusion.py`, `SignalFusionEngine`, or any of its components. Zero mentions in the import section or anywhere in the file.

The `SignalFusionEngine` exists as a standalone module that is only used by `scripts/ensemble_v11_calibration_fusion.py` (the calibrated ensemble backtest). It has never been wired into paper trading.

---

## 4. Does it use confidence-weighted position sizing?

**Yes**, via `core/position_sizing.py`. This was added as P0.6.

The engine calls `_compute_confidence_weighted_size()` which classifies confidence into HIGH/MEDIUM/LOW tiers and sizes positions accordingly. It uses per-instance config (`DEV_CONFIG`, `PROD_CONFIG`, etc.) with tier-based multipliers.

However, the confidence it feeds into the sizing is the **analytical probability from a single signal**, not a fused ensemble confidence. So the sizing is correct in mechanism but operates on inferior input — it's sizing based on one signal's opinion, not the ensemble's.

The engine also has **cluster budget caps** (R4-1.6) and **same-city pair exposure caps**, which correctly limit aggregate risk. These are functioning properly.

---

## 5. Does it use the DS conflict detection?

**No.** There is no Dempster-Shafer conflict detection in the paper trading engine. No import, no call, no reference.

The `dempster_shafer_conflict()` function in `core/signal_fusion.py` computes conflict mass K between signals and can suppress trades when K is too high. This is entirely absent from paper trading.

Since the paper trading engine doesn't even aggregate signals, it has no concept of inter-signal conflict. Each signal fires independently.

---

## 6. Does it use the time-decay reliability manager?

**No.** The paper trading engine does not import or use `TimeDecaySignalManager` from `core/signal_fusion.py` or `SimpleTimeDecayManager` from `scripts/comprehensive_split_backtest.py`.

The backtest ensemble uses time-decay to:
- Track per-signal per-station recent accuracy (exponential forgetting, decay=0.9, window=30 days)
- Adjust confidence: `adjusted_conf = sqrt(raw_conf * reliability)`
- Modify LLOP weights based on recent reliability
- Suppress signals from stations with <3 observations (fixed in C3)

None of this exists in paper trading. Every signal fires at its raw confidence level regardless of its recent track record.

---

## 7. Does it use isotonic calibration?

**No.** The paper trading engine does not use `CalibrationPipeline` or `IsotonicRegression` from `core/calibration_pipeline.py`. No import, no call.

The backtest's `ensemble_v11_calibration_fusion.py` uses walk-forward isotonic regression to:
- Train per-signal per-city calibrators on historical (raw_conf, correct) pairs
- Convert raw confidence to calibrated probability of correctness
- Feed calibrated probabilities into the LLOP fusion

The paper trading engine uses raw analytical probability directly from `_get_analytical_probability()` — a weighted average of climatology, recent trend, and rolling window. No calibration is applied.

---

## 8. What would it take to wire the full ensemble into paper trading?

This is task **S2** in TASKS.md. The full spec is in the next section, but the high-level answer:

### Required Changes

1. **Extract signals into `core/signals/` (S1 prerequisite)** — Currently, backtest signals are inline functions in `comprehensive_split_backtest.py`. Paper trading signals are inline methods in `paper_trading_engine.py`. These must be extracted into shared modules that both systems import.

2. **Wire SignalFusionEngine into PaperTrader** — Import `SignalFusionEngine` and `TimeDecaySignalManager` from `core/signal_fusion.py`. Initialize them in `PaperTrader.__init__()`. Call `fusion_system.fuse_signals()` instead of the current per-signal `place_paper_trade()` loop.

3. **Replace naive probability with LLOP fusion** — Instead of each signal independently placing trades, collect all signal outputs for a station, pass them through the fusion engine (Layer 0: calibration → Layer 1: MI weights → Layer 2: LLOP → Layer 3: DS conflict), and trade on the fused result.

4. **Add isotonic calibration warmup** — The calibration pipeline needs historical data to train. On first run, it will need to backfill calibration data from the settlement_epochs table. This can be done at startup or via a pre-training script.

5. **Add time-decay reliability tracking** — Initialize `TimeDecaySignalManager` and update it after each settlement. Use `adjust_confidence()` before fusion and `get_lop_weight()` during fusion.

6. **Add DS conflict suppression** — When `dempster_shafer_conflict()` returns K ≥ 0.95, skip the trade. When K ≥ 0.8, suppress toward 0.5. When K < 0.3, amplify slightly.

7. **Change the trade flow** — Currently: signal → place_paper_trade(). New: signal → collect → fuse → single fused trade per station.

8. **Preserve existing infrastructure** — Position sizing, cluster caps, city pair caps, Kalshi calendar, entry window gating, and mark-to-market must all work with the fused signal output.

### Effort Estimate: 2-3 days (matches TASKS.md S2 estimate)

---

## Architecture Comparison

| Component | Paper Trading Engine | Backtest Ensemble |
|-----------|---------------------|-------------------|
| **Signals** | 3 inline (reversion, climatology, LDM hourly) | 7 modular (reversion, gaussian, gaussian_v2, regime, pressure, climatology, goldilocks) |
| **Fusion** | None — independent trades | LLOP (log-odds linear opinion pool) |
| **Calibration** | None | Isotonic regression (walk-forward) |
| **DS Conflict** | None | Yes — K-based suppression |
| **Time-decay** | None | Exponential forgetting (decay=0.9, window=30d) |
| **Position sizing** | Confidence-weighted (3 tiers) | N/A (backtest uses fixed bet) |
| **Risk controls** | Cluster caps, city pair caps | None |
| **Entry timing** | Kalshi calendar + T-18h to T-2h window | N/A |
| **Probability source** | Weighted average (climatology + trend + rolling) | LLOP of calibrated signal probabilities |

---

## Conclusion

The paper trading engine is a **real-time execution system** with good risk controls (position sizing, cluster caps, entry windows, mark-to-market) but **no signal sophistication**. The backtest ensemble is a **signal research system** with sophisticated fusion but no risk controls. Task S2 is about merging these two worlds.

The path is clear: extract shared signals (S1), wire the fusion engine into the paper trader, and keep the existing risk infrastructure intact. The main risk is that the calibration pipeline needs sufficient historical data to train, which connects to the NWP backfill problem (Part 2 of this audit).
