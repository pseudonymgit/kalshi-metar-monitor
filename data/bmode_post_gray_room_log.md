# B-Mode Post-Gray Room Execution Log

**Date:** 2026-08-03 09:15 UTC
**Branch:** `bmode-post-gray-room`
**Executor:** Gilfoyle (subagent dispatch via Donna)
**Status:** EXECUTED WITH STOP CONDITIONS

## Summary

| Task | Status | Result | Stop Condition |
|------|--------|--------|----------------|
| P0.1 — Fix $54/mo cron waste | ✅ | Model changed to `ollama/qwen2.5-coder:7b` | N/A |
| P0 — CLI Verification | ⚠️ STOP | 90% agreement, below 95% threshold | **HIT** — report to Donna |
| P1 — Gaussian Fusion | ✅ | 68.32% (+2.15pp), confirmed | ✅ Verified |
| P2 — Goldilocks Lane | ⚠️ PARK | 3.92% precision, 0% recall | **HIT** — needs IEM 1-min data |
| P3 — Cascade v1 Research | ⚠️ | Bayesian too conservative (rho=0.60) | Research spike only |
| P4 — Trajectory Research | ⚠️ | 26.3% agreement rate | Research spike only |

## P0.1 — Fix $54/mo Cron Waste

**Cron job:** `gefs-paper-trading-cron` (ID: `ae1f48ff-9dd9-43df-910c-8053897d70c9`)
**Change:** Model `openai/gpt-5.4-mini` → `ollama/qwen2.5-coder:7b`
**Rationale:** The cron executes a deterministic Python script (`gefs_paper_trading_cron.py`) with zero AI calls. The model assignment was paying for nothing. The cron-allowable model `ollama/qwen2.5-coder:7b` is used for heartbeat/cron work per the model allowlist policy.

## P0 — CLI Settlement Verification

**Script:** `scripts/verify_cli_settlements.py`
**Method:** Fetch NWS observations via pagination for all 20 stations, compute daily max temps using station-local timezone, compare with Kalshi settlement data within 1°F tolerance.

**Result:** 90.00% overall agreement (120 matched dates across 20 stations)
**Threshold:** >95% required to proceed

**Stations with <95% agreement:**
- KDEN: 50.0% (only 189 obs returned — thin data)
- KNYC: 50.0% (only 264 obs returned — thin data)
- KMDW: 66.7%
- KDFW: 83.3%
- KLAX: 83.3%
- KPHX: 83.3%
- KSAT: 83.3%

**Assessment:** The 90% agreement is close to the 95% threshold. The discrepancy is partially explained by thin data coverage (only 8 recent days per station with NWS API). Stations with low obs counts (KDEN: 189, KNYC: 264) contribute disproportionately to the disagreement. The local-date grouping fix (using station timezone) improved results from 82.5% to 90%.

**Recommendation:** Re-run with a larger data window (NWS API pagination limited to ~8 days) or use the IEM ASOS API for 1-minute data. The remaining disagreement may be genuine NWS-vs-Kalshi source differences.

## P1 — Gaussian Fusion Confirmed

**Script:** `scripts/apply_gaussian_fusion.py`
**Method:** GEFS + Gaussian weighted voting (auto-voting mode, not GEFS-direction-forced), with direction-specific calibration.

**Result:**
- Accuracy: 68.32% (+2.15pp over baseline)
- P&L: $34,521.48
- Sharpe: 9.61
- Trades: 1,411
- Config: GEFS weight=0.6617, Gaussian weight=0.6405

**Wiring instructions for production cron:**
1. Add METAR daily loading (`load_metar_daily()`) to `gefs_paper_trading_cron.py`
2. Add Gaussian signal computation (48-day z-score, z > 1.0 threshold)
3. Replace direct GEFS-only trade decision with weighted voting
4. Weights: GEFS=0.6617, Gaussian=0.6405
5. Apply direction-specific calibration on fused confidence
6. Verify METAR data freshness for the Gaussian window

## P2 — Goldilocks Lane (PARKED)

**Module:** `core/lane2_goldilocks.py` (standalone extraction)
**Script:** `scripts/extract_goldilocks_lane.py`

**Result:**
- Instant Cross Revert: 51 signals, 2 TP, 49 FP → 3.92% precision, 3.92% recall
- Trend Extrapolation: 0 predictions

**Root Cause:** The Goldilocks signal requires IEM 1-minute ASOS data to detect temperature spikes that are missed by the official hourly METAR observations. With METAR-only data, the signal cannot achieve the ≥70% precision or ≥50% recall targets. The signal fires when METAR observations show a transient boundary crossing, but the daily max (computed from the same METAR data) is always at or above the boundary.

**Recommended Fix:** 
1. Fetch IEM 1-minute ASOS data for the backtest (e.g., `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py`)
2. Evaluate against the NWS official daily max (from `kalshi_settlements.db`), which uses the 1-minute data
3. The signal is: IEM shows a spike to 85°F, METAR misses it, Kalshi settles at 84.7°F → TP

**Trend Extrapolation Fix:** The momentum window requires consecutive observations with increasing timestamps. METAR data has gaps (1-3hr intervals). The momentum check should be more tolerant of gaps.

## P3 — Cascade v1 Research

**Script:** `scripts/research_cascade_v1.py`
**Method:** Beta-binomial Layer 1 with correlation-adjusted effective sample size.

**Results:**
| Method | Acc% | P&L | Sharpe | Trades |
|--------|-----:|----:|-------:|------:|
| Point-estimate (baseline) | 60.58% | $35,883 | 10.14 | 2,364 |
| Bayesian (rho=0.60) | 0.00% | $0 | 0.00 | 0 |
| Bayesian (rho=0.0, naive) | 61.05% | $36,813 | 10.68 | 2,303 |

**Analysis:**
- Bayesian with rho=0.60 produces 0 trades because n_eff = 31/(1+30*0.6) = 1.63. The effective sample size is too small to overcome the uniform prior, and the variance penalty makes all edges below threshold.
- Naive (rho=0.0, treating 31 members as independent) is slightly better than baseline (+0.47pp), but this is overconfident.
- The pool-of-pools approach (GEFS + ECMWF as separate pools) is needed for meaningful Bayesian inference.

**Recommendation:** Implement pool-of-pools (Gray Room Expert 1 EL-1) before re-evaluating the Bayesian cascade. Need ECMWF member data for cross-pool correlation.

## P4 — Trajectory Research Spike

**Script:** `scripts/research_trajectory.py`
**Method:** DTW epoch-sequence matching on 5 features (T, Td, P, WS, WD) with climate-zone pooling.

**Results:**
- Corpus: 40,752 station-dates across 20 stations
- 5-day sequences: 40,672
- Query dates (KMDW): 30, DTW matched: 19
- Agreement rate (top analog bucket vs actual): 26.3%

**Analysis:** The 26.3% agreement rate is low but expected for a first-pass implementation. The DTW matching uses candidate sequences from ALL stations (including cross-zone), which dilutes the station-specific signals. The prototype confirms:
- The corpus construction works (40K+ station-dates)
- DTW matching is computationally feasible (FastDTW with radius=3)
- Climate-zone pooling is needed (same-station analogs are thin)

**Recommendation:** 
1. Increase the station-zone boost (cross-zone from 0.5 to 0.25)
2. Only use same-climate-zone candidates (not all stations)
3. Add feature standardization (z-score per station×season)
4. Implement the 30-day shadow run against GEFS decisions

## Files Created

- `scripts/verify_cli_settlements.py` — CLI verification
- `core/lane2_goldilocks.py` — Goldilocks lane module
- `scripts/extract_goldilocks_lane.py` — Goldilocks backtest
- `scripts/apply_gaussian_fusion.py` — Gaussian fusion test
- `scripts/research_cascade_v1.py` — Cascade research
- `scripts/research_trajectory.py` — Trajectory research
- `docs/weather-engine/backtests/cli_verification_20260803.json` — CLI results
- `docs/weather-engine/backtests/gaussian_fused_20260803.json` — Gaussian fusion results
- `docs/weather-engine/backtests/goldilocks_backtest_20260803.json` — Goldilocks results
- `docs/weather-engine/backtests/cascade_v1_research_20260803.json` — Cascade results
- `docs/weather-engine/backtests/trajectory_research_20260803.json` — Trajectory results

## Cron Changes

- `gefs-paper-trading-cron`: model changed from `openai/gpt-5.4-mini` to `ollama/qwen2.5-coder:7b` (saves ~$54/mo)

## Accuracy Improvement Suggestions

1. **CLI verification:** Use IEM ASOS 1-minute API instead of NWS observations endpoint for better temporal coverage and station-local date accuracy.
2. **Gaussian fusion:** The 48-day window is fixed. For adaptive window size based on seasonal autocorrelation, the z-score confidence would improve.
3. **Goldilocks:** The lane is fundamentally about IEM vs METAR data speed. Without IEM 1-minute data, the signal cannot work. The metar_monitor.py code already has the correct logic — it's the data source that's the bottleneck.
4. **Cascade:** The pool-of-pools approach (GEFS + ECMWF) is the correct path. Measure cross-pool correlation from the existing archive data first.
5. **Trajectory:** Climate-zone pooling helps but cross-zone dilution is a problem. Use soft clustering (Gaussian mixture) instead of hard zone boundaries.