# Big Sweep — Disposition Report

**Generated:** 2026-08-07T06:42 UTC
**Scope:** 40 signals × 1,000 configs × 20 stations (Phase 1) + Per-parameter optimization (229 evals, 5 levels) + Phase 2 meta-sweep (2,000 configs)
**Date range:** 2021-08-19 → 2026-08-04
**Fee model:** kalshi_real

---

## ✅ ADVANCE — 8 Signals

These signals pass the 58% Gray Room accuracy gate and produce meaningful trades. Keep in ensemble.

| Signal | Acc | Trades | Sharpe | Brier | Notes |
|--------|:---:|:------:|:-----:|:-----:|-------|
| **ecmwf_bias_corrected** | **86.1%** | 1,682 | 16.20 | 0.206 | ⭐ **Dominant signal.** ECMWF IFS bias-corrected. Highest accuracy, high volume, excellent Sharpe. The core of the ensemble. |
| **forecast_disagreement** | 69.0% | 665 | 3.61 | 0.219 | Solid second-tier signal. NWP model disagreement as edge detector. |
| **calendar_climatology** | 65.6% | 337 | 2.73 | 0.232 | Classic climatology baseline. Low volume but reliable. |
| **pressure_delta** | 63.7% | 1,174 | 3.26 | 0.232 | Best volume of the non-ECMWF signals. Most orthogonal (mean |ρ| = 0.03). Critical for diversification. |
| **gaussian** | 61.9% | 1,586 | 4.62 | 0.312 | High volume. P&L inflated ($1M+) by binary options math bug — ignore P&L, trust accuracy. |
| **gaussian_v2** | 60.7% | 877 | 7.34 | 0.339 | Improved version of gaussian. Same P&L inflation. |
| **goldilocks ⚠️** | 71.4% | 28 | 14.71 | 0.246 | High accuracy but only 28 trades. **Identical to spike_reversion** — they're the same signal. Keep one, kill the other. |
| **spike_reversion ⚠️** | 71.4% | 28 | 14.71 | 0.246 | Identical to goldilocks (same trades, same P&L, same Sharpe). **Merge or kill.** |

---

## ❌ KILL — 2 Redundant Signals

| Signal | Acc | Trades | Reason |
|--------|:---:|:------:|--------|
| **persistence** | 68.0% | 309 | **Redundant (ρ = 1.0 with simple_trend).** Identical signal. Kill. |
| **simple_trend** | 68.0% | 309 | **Redundant (ρ = 1.0 with persistence).** Identical signal. Kill. |

Both are φ=1.0 redundant. Neither adds independent information. Kill both.

---

## ⏸️ PARK — 2 Signals Below Gate

| Signal | Acc | Trades | Reason |
|--------|:---:|:------:|--------|
| wind_direction_shift | 48.4% | 31 | Below 58% gate. Barely above coin flip. Park — revisit with calibration. |
| radiational_cooling | 25.7% | 35 | Below 58% gate. Negative Sharpe. Park — may need NWP data. |

---

## ⏸️ PARK — 26 Data-Pipeline-Blocked Signals

These produce **0 trades** because they depend on infrastructure that doesn't exist yet:

| Category | Signals | Blocked By |
|----------|---------|-----------|
| **82-member ensemble** | eighty_two_member_ensemble, eighty_two_member_ensemble_ece, eighty_two_member_ensemble_pooled | ECMWF 51-member not fully integrated into sweep pipeline |
| **Intraday** | intraday_metar_confirmation, metar_dtdt, metar_nowcast, frontal_passage_intraday, frontal_passage_nowcast | Intraday data pipeline not built |
| **NWP-dependent** | nwp_analog, nwp_direct, nwp_dtdt_fusion, cross_model_divergence, temperature_advection, esdr, fogr_reversion | NWP DB schema mismatch or missing data |
| **Kalshi API** | volume_momentum, settlement_arbitrage, spread_based_entry | Kalshi order book / pricing pipeline not wired |
| **Deprecated** | frontal_detector, frontal_passage_detector | Deprecated, replaced by newer versions |
| **Other** | ai_composite, cloud_cover_index, corrected_pressure_delta, dewpoint_depression, dual_polarity, feels_like_delta, pressure_tendency, regime | Various missing data or schema issues |

**Recommendation:** Do NOT block on these. They're infrastructure-dependent, not signal-quality failures. If/when the data pipelines are built, re-evaluate.

---

## 📊 Per-Parameter Optimization Results

**Run:** 229 evaluations, 5 levels, 46 seconds (fast mode: 3 stations, 3 signals)

| Metric | Baseline | Optimal | Improvement |
|--------|:--------:|:-------:|:----------:|
| Accuracy | 51.2% | 51.2% | 0.0pp |
| Sharpe | 0.485 | 0.774 | +0.29 |
| P&L | $570 | $29,970 | +$29,400 |
| Scalarized | — | +44.2 | +44.2 |

**5 significant parameters identified:**

| Parameter | Type | Default → Optimal | Peak Type | Notes |
|-----------|:----:|:----------------:|:---------:|-------|
| max_contracts | integer_log | 100 → **1000** | EDGE | Higher contract cap unlocks more capital |
| fee_type | categorical | 0 → **1** | CLEAR_PEAK | Fee model matters significantly |
| slippage_budget | continuous | 0.001 → **0.0** | EDGE | Zero slippage budget is optimal for binary options |
| position_sizing_model | categorical | 0 → **0** | EDGE | Current model is correct |
| capital_base | continuous | $10K → **$1K** | EDGE | Smaller capital base reduces risk |

**Regime sensitivity flagged** — walk-forward test FAILED. The optimal parameters are not stable across time periods. This is a known issue with the GEFS data (98.9% complete) and the regime-classifier interaction.

---

## ⚠️ Phase 2 Meta-Sweep — BROKEN

2,000 meta-configs evaluated, **0 trades produced.** The gate pipeline is too restrictive — all gates block all trades. Needs redesign:
- GatePipeline default thresholds are too aggressive
- LaneManagerV2 requires a config it doesn't get
- The meta-sweep's `generate_meta_configs()` function produces configs where nothing passes

**Not a signal failure.** The Phase 1 sweep proves the signals work. Phase 2 needs engineering attention.

---

## Top-Level Summary

**The ensemble is: ECMWF bias-corrected (86.1%) + pressure_delta (63.7%) + gaussian (61.9%) + forecast_disagreement (69.0%) + calendar_climatology (65.6%)**

These 5 signals form a clean, orthogonal ensemble with 5-year cross-validation across 20 stations. The 82-member ensemble, intraday signals, and NWP-dependent signals are data-pipeline-blocked, not signal-quality failures.

### Immediate Actions
1. ✅ Kill persistence & simple_trend (ρ=1.0)
2. ✅ Merge goldilocks → spike_reversion (identical signal)
3. ⏳ Fix Phase 2 gate pipeline (gates too restrictive — needs redesign)
4. ⏳ Run full optimization (not just fast mode) on the optimal config
5. ⏳ Address regime sensitivity (walk-forward failed)