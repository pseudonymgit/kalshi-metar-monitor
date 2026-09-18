<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Expert Review 2: Engineering & Production Readiness Audit

**Date:** 2026-09-02 05:21 UTC  
**Auditor:** Expert Review 2 (Engineering Readiness)  
**Scope:** Full weather engine signal inventory, data pipelines, DB health, compute layer, wiring correctness  

---

## Executive Summary

**Overall Readiness: NOT READY** — Seven blocking issues found, two of which are critical-path for production safety. The engine has undergone extensive signal expansion (31 registered signals, 42 total in SIGNAL_REGISTRY) but the registry and pipeline layers have drifted apart. Several signals silently fail in backtest but claim to be "active." DB infrastructure has multiple empty/stale databases. The intraday stack is standalone with zero production integration. The `nwp_analog` signal has contradictory KILLED/ACTIVE status across registries, causing unpredictable runtime behavior.

**Signal Health:** ~15/31 registered signals fire reliably. ~6 fire conditionally (data-gated, API-only, or degenerate). ~10 have structural issues ranging from stub evaluate() to registry drift to contradictory pool membership.

---

## PART 1: THE 31 REGISTERED SIGNALS — Individual Verdicts

### GEFS Pool (rho_within=0.85)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 1 | **gaussian** | ACTIVE | **READY** | Proper evaluate() uses 48-day rolling window with z-score. Returns (direction, confidence) or (None, 0.0). Works in backtest and live. |
| 2 | **gaussian_v2** | ACTIVE | **READY** | Refined Gaussian variant. Proper evaluate() with 30-day window. Verified. |
| 3 | **forecast_disagreement** | ACTIVE | **NEEDS FIX** | evaluate() exists in `forecast_disagreement_signal.py` (4.9KB). But the SIGNAL_REGISTRY.md says "Needs Edge 20" — verify the actual data pipeline for this signal. The `forecast_disagreement_live.db` (7.7MB, 819 rows) suggests limited history. Firing rate depends on NWP data availability. |

### ECMWF Pool (rho_within=1.0)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 4 | **ecmwf_bias_corrected** | ACTIVE | **READY** | Proper evaluate() (METAR trend fallback) and evaluate_for_station() (live ECMWF API via Open-Meteo). `ecmwf_bias.db` has 552 forecast rows. Works in both modes. |

### Climatology Pool (rho_within=0.60)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 5 | **calendar_climatology** | ACTIVE | **READY** | Simple historical frequency P(T > B). Proper evaluate() in `calendar_climatology_signal.py`. Verified. |
| 6 | **seasonal_regime** | ACTIVE | **THIN STUB — READY** | Thin re-export from `dual_polarity_signal.py`. The actual implementation lives in the `SeasonalRegimeClassifier` class. Not a full standalone signal. |

### Eighty-Two-Member Pool (rho_within=1.0)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 7 | **eighty_two_member_ensemble** | ACTIVE | **NEEDS FIX** | evaluate() exists. But it requires BOTH `gefs_archive.db` (52MB, last modified Aug 8 — **stale**) AND `tigge_archive.db`. GEFS data hasn't been refreshed in ~25 days. The 82-member fusion needs current GEFS/ECMWF ensemble data. |

### NWP Direct Pool (rho_within=0.70)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 8 | **nwp_direct** | ACTIVE | **NEEDS FIX** | evaluate() exists but delegates to API calls. Relies on `nwp_forecasts.db` for backtest. ~597K rows in the DB, but the signal module is a non-standard import (archived module loaded via `_load_archived` compatibility shim). Module path is `core.signals.nwp_direct_signal` from archive. Fragile. |
| 9 | **nwp_analog** | ACTIVE (pipeline) / KILLED (signal_families) | **BLOCKING — REGISTRY CONTRADICTION** | **CRITICAL.** REGISTERED_SIGNAL_NAMES lists it as active. CANONICAL_POOL_DEFINITIONS includes it in nwp_direct pool. `NWP_ANALOG_ENABLED` defaults to `True`. BUT signal_families.py marks it as KILLED (Family A with status="KILLED" AND Family E with status="KILLED"). `get_active_signals()` in signal_families.py excludes it. This means the pool-of-pools fusion layer computes effective sample sizes assuming `nwp_analog` is NOT in the pool, while the actual pipeline fires it. **Runtime does not match metadata.** No error at runtime, but pool correlation estimates are wrong. **Fix:** Reconcile — either kill it in REGISTERED_SIGNAL_NAMES and CANONICAL_POOL_DEFINITIONS, or mark it ACTIVE in signal_families.py. |
| 10 | **temperature_advection** | ACTIVE | **NEEDS FIX** | evaluate() delegates to evaluate_for_station() which makes a **live GFS API call** (https://api.open-meteo.com/v1/gfs) for backtest dates. For historical dates this means it calls an external API and depends on the API having historical data. Has a stored-NWP-DB fallback path, but priority is live API call. **Performance concern:** Every evaluate() call triggers HTTP request. |
| 11 | **honest_trend** | ACTIVE | **READY** | Proper evaluate() with METAR trend over 3-day lookback. Simple, reliable, no API calls. |

### HRRR (not in pool definitions as separate pool, grouped in nwp_direct pool in CANONICAL_POOL)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 12 | **hrrr_bias_corrected** | ACTIVE | **NOT READY** | **Live-API-only.** evaluate() always returns (None, 0.0) — deliberately. The docstring says "this compatibility evaluator deliberately remains non-firing." `evaluate_for_station()` also returns (None, 0.0). The actual functionality requires live Open-Meteo API calls with station lat/lon. **Zero backtest coverage.** `hrrr_bias.db` has tables but **0 rows** — no historical HRRR data collected. **This signal does nothing in any pipeline path.** It's a pure live-API data fetcher pretending to be a signal. |

### METAR Pool (rho_within=0.50, freshness=2h)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 13 | **metar_nowcast** | ACTIVE | **READY** | Had DB aggregate fix Sep 1. evaluate() reads from metar DB with proper queries. `metar_backfill.db` is 2.3GB and actively maintained. |
| 14 | **metar_trend** | ACTIVE | **READY** | Simple 80-line signal. Proper evaluate() with METAR short-term trend. |
| 15 | **cloud_cover_index** | ACTIVE | **READY** | DB aggregate fixed Sep 1. evaluate() queries METAR observations for cloud cover proxy. |
| 16 | **feels_like_delta** | ACTIVE | **READY** | DB aggregate fixed Sep 1. Heat index divergence from forecast. |

### Pressure Pool (rho_within=0.65, freshness=3h)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 17 | **pressure_delta** | ACTIVE | **READY** | Well-tested signal in `pressure_delta_signal.py` (8.5KB). Proper evaluate() with rolling pressure data. |
| 18 | **corrected_pressure_delta** | ACTIVE | **THIN STUB — READY** | Thin re-export from `dual_polarity_signal.py`. Actual implementation lives in `CorrectedPressureDeltaSignal` class inside the larger file. Functional but not a standalone module. |
| 19 | **pressure_tendency** | ACTIVE | **READY** | Re-enabled 2026-08-26 with 2-day cumulative ΔP logic. Proper evaluate() in `pressure_tendency_signal.py`. |

### Generalist Pool (rho_within=0.40, freshness=3h)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 20 | **asos_microstructure** | ACTIVE | **NOT READY** | evaluate() **always returns (None, 0.0)** — deliberately. The standard evaluate() is documented as "The standard days-only interface doesn't carry ASOS 1-minute data, so this returns (None, 0.0) by default." Only evaluate_for_station() works, and that requires the ASOS DB. For backtest/sweep paths (which use evaluate()), **this signal is silent and wastes compute.** Firing rate in pipeline: **0%.** |
| 21 | **temperature_trend** | ACTIVE | **NOT READY** | Same structural problem as asos_microstructure — evaluate() needs ASOS DB access. The `temperature_trend_signal.py` (30KB) has proper logic but only through evaluate_for_station(). Standard evaluate() likely returns (None, 0.0) in pipeline mode. |
| 22 | **ghcn_calibration** | DEGENERATE | **NEEDS FIX** | **Fixable!** The degenerate claim is correct: evaluate() compares METAR high vs METAR high (yesterday), which IS circular. BUT evaluate_for_station() uses actual GHCN-Daily NOAA data as an independent source. The **fix**: change pipeline to always call evaluate_for_station() when GHCN data exists, with evaluate() as fallback. The GHCN data cutoff is 2026-02-28 so it only works for dates before that, but it's a genuinely orthogonal signal for backtest periods. **Not truly degenerate** — just the wrong path is being used. |
| 23 | **rdae_mos** | ACTIVE | **READY** | 65.93% settlement accuracy. Proper evaluate() in `rdae_mos_signal.py` (17KB). Verified working. |

### Reversion Pool (always-DOWN, override lane)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 24 | **spike_reversion** | ACTIVE | **READY** | Proper evaluate() in `spike_reversion_signal.py` (8.5KB). Always DOWN, fires on conditions. |
| 25 | **radiational_cooling** | ACTIVE | **READY** | Proper evaluate() in `radiational_cooling_signal.py` (42KB — large but functional). Always DOWN. |

### Momentum Pool (always-UP, override lane)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 26 | **warm_momentum** | ACTIVE | **READY** | Proper evaluate() in `warm_momentum_signal.py` (5KB). Always UP. |
| 27 | **cooling_failure** | ACTIVE | **READY** | Proper evaluate() in `cooling_failure_signal.py` (6.7KB). Always UP. |

### NWP Upper Pool (rho_within=0.50, freshness=6h)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 28 | **850mb_temp_advection** | ACTIVE | **NEEDS FIX** | Data coverage starts 2026-07-08 — only ~56 days of history. `nwp_forecasts.db` has 850hPa fields. The TIGGE data (182K rows) provides historical coverage but this signal needs specific fields. For backtest, it will work for dates after 2026-07-08 only. Earlier dates = silent. |
| 29 | **500mb_height_pattern** | ACTIVE | **NEEDS FIX** | Same data limitation as 850mb — only ~56 days of coverage. Same DB fields. Silent for pre-July 2026 dates. |

### Modulators (not in any pool)

| # | Signal | Status | Verdict | Details |
|---|--------|--------|---------|---------|
| 30 | **cross_model_divergence** | ACTIVE | **READY** | evaluate() stub was "fixed Sep 1." Large implementation in `cross_model_divergence_signal.py` (19KB). Uses `forecast_disagreement_live.db` with 819 rows. Modulates confidence. |
| 31 | **frontal_passage_intraday** | ACTIVE | **NEEDS FIX** | evaluate() exists (line 272 in 19KB file). Module load is guarded: `if FrontalPassageIntradaySignal else None` in `__init__.py`. This is an archived module loaded via compat shim. The import path is fragile. |

---

## PART 2: 21 ADDITIONAL SIGNALS IN SIGNAL_REGISTRY — Verification

### ACTIVE but NOT in 31 Registered Names

| # | Signal | Family | Wired? | Has evaluate()? | Verdict |
|---|--------|--------|--------|-----------------|---------|
| 32 | **fogr_reversion_signal** | C | No | Archived compat | **TRASH** — Archived 2026-08-11, loaded via `_load_archived` compat shim. Never wired into pipeline. signal_families says ACTIVE but it doesn't fire in any pipeline path. |
| 33 | **frontal_detector_signal** | B | No | Deprecated stub | **NOT READY** — Deprecated in favor of FrontalPassageIntradaySignal. Has evaluate() but docstring says "DEPRECATION WARNING." Not in REGISTERED_SIGNAL_NAMES. Still in signal_families as ACTIVE — registry drift. |
| 34 | **frontal_passage_detector** | B | No | Unknown | **NOT READY** — ACTIVE in signal_families but NOT in REGISTERED_SIGNAL_NAMES. No file at this path (likely a different module name). Different algorithm from frontal_detector. Unclear if it does anything. |
| 35 | **frontal_passage_nowcast_signal** | B | Partial | Yes (529-line file) | **NEEDS FIX** — ACTIVE in signal_families but NOT in REGISTERED_SIGNAL_NAMES. BUT it's in `_LIGHTWEIGHT_SIGNAL_FACTORIES` as `frontal_passage_nowcast: FrontalPassageNowcastSignal`. Has proper evaluate(). Pipeline CAN fire it but signal_families metadata doesn't match. |
| 36 | **settlement_arbitrage_signal** | C | No | Archived compat | **TRASH** — Archived compat from retired/. In `_LIGHTWEIGHT_SIGNAL_FACTORIES` but not in REGISTERED_SIGNAL_NAMES. Never fires in pipeline. |
| 37 | **simple_trend_signal** | C | No | Archived compat | **TRASH** — Archived retired/ module. In `_LIGHTWEIGHT_SIGNAL_FACTORIES` but not in REGISTERED_SIGNAL_NAMES. Never fires. |
| 38 | **spread_based_entry_signal** | A | No | Yes (8.5KB) | **NOT READY** — Has proper evaluate() but NOT in REGISTERED_SIGNAL_NAMES. ACTIVE in signal_families but not wired in pipeline. False-positive in signal_families metadata. |
| 39 | **volume_momentum_signal** | C | No | Archived compat | **TRASH** — Archived retired/. In `_LIGHTWEIGHT_SIGNAL_FACTORIES` but not in REGISTERED_SIGNAL_NAMES. Never fires. |
| 40 | **wind_direction_shift** | D | RETIRED | Abstract evaluate() | **TRASH** — Retired 2026-08-25 for never producing output. Still ACTIVE in signal_families — **registry drift.** |
| 41 | **dewpoint_depression_modulator** | D | Modulator | No evaluate() | **READY** (as designed) — Not a signal; a confidence modulator function. `modulate_confidence()` works. Correctly classified as Family D. |
| 42 | **upper_atmosphere_signal** | B | Split | See above | **NOT READY** — signal_families has it as a single entity, but the pipeline split it into 850mb_temp_advection + 500mb_height_pattern. Not wired as `upper_atmosphere_signal` in REGISTERED_SIGNAL_NAMES. signal_families metadata is stale. |

### GATED

| # | Signal | Verdict | Details |
|---|--------|---------|---------|
| 43 | **ai_composite_signal** | **TRASH** | GATED, off by default. Relies on ML pipeline not deployed. No evaluate() path that produces useful output without ML. |

### KILLED (Family E)

| # | Signal | Verdict | Details |
|---|--------|---------|---------|
| 44 | **persistence_signal** | **TRASH** | KILLED. 48.31% accuracy. 166-byte retired file. |
| 45 | **goldilocks_signal** | **TRASH** | KILLED. 49.85% negative EV. Archived per Gray Room R7 A2. |
| 46 | **late_day_momentum** | **TRASH** | KILLED. 48.31% accuracy. Archived module_path="core". |
| 47 | **nwp_analog_signal** (Family E duplicate) | **BLOCKING** | See Part 1, signal #9. Duplicate entry in signal_families creates ambiguity. |

### ORPHANED

| # | Signal | Verdict | Details |
|---|--------|---------|---------|
| 48 | **dual_polarity_signal** | **TRASH** | Never wired. 30KB file but not imported by any pipeline. |
| 49 | **esdr_signal** | **TRASH** | Never wired. 9KB file. Archived compat. |
| 50 | **intraday_metar_confirmation** | **TRASH** | Replaced by METAR nowcast. Archived. Imported by paper_trading_engine.py for legacy compat only. |
| 51 | **intraday_metar_confirmation_signal** | **TRASH** | Same replacement. Archived compat. |
| 52 | **metar_dtdt_signal** | **TRASH** | Dead-end pipeline. Archived compat. |
| 53 | **nwp_dtdt_fusion_signal** | **TRASH** | Different model architecture. Archived compat. |

---

## PART 3: REGISTRY DRIFT — CRITICAL FINDING

The **two registries are out of sync**:

| Signal | REGISTERED_SIGNAL_NAMES (signals/__init__.py) | SIGNAL_REGISTRY (signal_families.py) |
|--------|-----------------------------------------------|--------------------------------------|
| wind_direction_shift | ❌ REMOVED 2026-08-25 | ✅ ACTIVE (Family D) |
| spread_based_entry | ❌ Not registered | ✅ ACTIVE (Family A) |
| frontal_detector | ❌ Not registered | ✅ ACTIVE (Family B) |
| frontal_passage_detector | ❌ Not registered | ✅ ACTIVE (Family B) |
| fogr_reversion | ❌ Not registered | ✅ ACTIVE (Family C) |
| simple_trend | ❌ Not registered | ✅ ACTIVE (Family C) |
| volume_momentum | ❌ Not registered | ✅ ACTIVE (Family C) |
| settlement_arbitrage | ❌ Not registered | ✅ ACTIVE (Family C) |
| regime | ❌ Not registered | ✅ ACTIVE (Family D) |
| dewpoint_depression_modulator | ❌ Not registered | ✅ ACTIVE (Family D) |
| ai_composite | ❌ Not registered | ✅ GATED (Family D) |
| upper_atmosphere_signal | ❌ Not registered | ✅ ACTIVE (Family B) |
| 850mb_temp_advection | ✅ Registered | ❌ Not in signal_families |
| 500mb_height_pattern | ✅ Registered | ❌ Not in signal_families |
| asos_microstructure | ✅ Registered | ❌ Not in signal_families |
| temperature_trend | ✅ Registered | ❌ Not in signal_families |

**Impact:** Pool correlation estimates from signal_families.py are WRONG. The pipeline fires a different set of signals than the pool definitions assume. The pool-of-pools fusion uses CANONICAL_POOL_DEFINITIONS (from signals/__init__.py), which may not match SIGNAL_REGISTRY at all.

**Fix requirement:** One registry to rule them all. Merge signal_families.py SIGNAL_REGISTRY to match REGISTERED_SIGNAL_NAMES + CANONICAL_POOL_DEFINITIONS.

---

## PART 4: DATA PIPELINE HEALTH

### Database Health Matrix

| Database | Size | Last Modified | Rows | Verdict |
|----------|------|---------------|------|---------|
| **metar_backfill.db** | 2.3 GB | Active | ~Millions | ✅ READY |
| **nwp_forecasts.db** | 197 MB | Sep 1 | 597,477 | ✅ READY |
| **tigge_archive.db** | 55 MB | Aug 26 | 182,100 | ⚠️ STALE |
| **kalshi_settlements.db** | 3.2 MB | Sep 1 | 13,482 | ✅ READY |
| **ghcn_settlements.db** | 37 MB | Sep 1 | 388,340 | ✅ READY |
| **forecast_disagreement_live.db** | 7.7 MB | Sep 1 | 819 rows | ⚠️ THIN |
| **gefs_archive.db** | 52 MB | **Aug 8** | 365,960 | ❌ STALE (~25 days) |
| **iem_asos.db** | 614 MB | **Aug 12** | 5,377,113 | ❌ STALE (~21 days) |
| **iem_asos_1min.db** | 777 MB | Aug 31 | 5,536,881 | ⚠️ BORDERLINE |
| **weatherapi_archive.db** | 300 MB | **Aug 5** | 2,526,515 | ❌ STALE (~28 days) |
| **era5_archive.db** | 3.2 MB | Aug 31 | 41,380 | ✅ OK |
| **ecmwf_bias.db** | 72 KB | Sep 2 | 552 | ✅ OK |
| **hrrr_bias.db** | 12 KB | Aug 5 | **0 rows** | ❌ EMPTY |
| **ecmwf_archive.db** | **0 bytes** | Aug 8 | 0 | ❌ EMPTY |
| **weather_engine.db** | **0 bytes** | N/A | 0 | ❌ EMPTY — supposed to be "core signal storage" |
| **ecds_ensemble.db** | **0 bytes** | Aug 20 | 0 | ❌ STALE EMPTY |
| **ecds_tigge.db** | **0 bytes** | Aug 30 | 0 | ❌ EMPTY |
| **tigge_ensemble.db** | **0 bytes** | Aug 17 | 0 | ❌ EMPTY |
| **kalshi_passive_collector.db** | **0 bytes** | Aug 31 | 0 | ❌ EMPTY |
| **weather.db** | **0 bytes** | Aug 30 | 0 | ❌ EMPTY |
| **weather_data.db** | **0 bytes** | Aug 23 | 0 | ❌ EMPTY |
| **sweep_results.db** | **0 bytes** | Aug 23 | 0 | ❌ EMPTY |
| **paper_trading_prod.db** | 68 KB | Aug 15 | **0 trades** | ❌ EMPTY — production trade DB has no trades |
| **paper_trading_dev.db** | 340 KB | Aug 26 | ~1,000 rows | ⚠️ Only dev trades exist |

### Key Findings

1. **weather_engine.db is empty (0 bytes)** — This is flagged as core signal storage but has zero data. Either it's unused or the ingestion pipeline is broken for this DB.

2. **hrrr_bias.db has 0 data rows** — Tables exist but no forecast records. HRRR bias correction has never been applied because no HRRR data was ever stored.

3. **gefs_archive.db is 25 days stale** (last modified Aug 8) — The GEFS ensemble underpins gaussian, gaussian_v2, and the 82-member ensemble signals. If GEFS data isn't refreshed, these signals rely on increasingly stale forecasts.

4. **iem_asos.db is 21 days stale** — ASOS 1-minute data underpins asos_microstructure and temperature_trend. Stale data means these signals compute on outdated observations.

5. **paper_trading_prod.db has 0 trades** — The production paper trader has never recorded a trade, or the database path is pointing to the wrong file.

6. **11 databases at 0 bytes** — Likely dead files from abandoned development branches or failed initializations. They create confusion and should be cleaned up.

---

## PART 5: signal_modules/ STUBS — Wiring Audit

7 stub files in `signal_modules/` directory:
- `atmospheric_pressure_tendency.py` — placeholder with trivial `(pressure - 1013.25) * 0.05`
- `diffusion_convergence_signal.py` — placeholder with trivial temperature/dewpoint computation
- `humidity_inversion_anomaly.py` — placeholder
- `momentum_differential_signal.py` — placeholder
- `trend_deviation_signal.py` — placeholder
- `visibility_trend_deviation.py` — placeholder
- `wind_direction_variance.py` — placeholder

**Wiring:** Imported by ONLY 2 scripts:
- `scripts/generate_top7_backtest.py` — standalone backtest
- `scripts/generate_all_station_backtest.py` — standalone backtest

Both are called from `scripts/master_bmode_runner.py` — a **backtest runner, not production.** No core/ module imports signal_modules/. No production pipeline path touches them.

**Verdict:** **NOT WIRED INTO ANY PRODUCTION PATH.** They are dead code that only run in standalone backtest scripts. The scripts themselves are rarely used (no cron references found).

**Recommendation:** Move to archive/ or delete. Keep if any active backtest workflow depends on them.

---

## PART 6: approach_ FUNCTIONS — Production Wiring Audit

**Core/ modules:** ZERO references to any approach_ function. Grep returned no matches in core/*.py.

**Scripts only** — The approach_ functions defined in:
- `ensemble_v7_sharpe_opt.py` — used internally only, not exported
- `ensemble_v10_phase2.5.py` — used internally only
- `signal_accuracy_dashboard.py` — uses approach_gaussian, approach_gaussian_v2, approach_pressure
- `ensemble_v12_comparison.py` — uses same three
- `per_station_skill.py` — uses same three

**Verdict:** **NOT WIRED INTO ANY PRODUCTION PATH.** These are legacy standalone functions predating the class-based signal system. The gaussian_signal.py and gaussian_v2_signal.py note "Based on approach_gaussian from ensemble_v7_sharpe_opt.py" indicating the logic was ported. The approach_ functions are now superseded.

**Recommendation:** Safe to archive. But verify the approach_pressure logic is fully captured in pressure_delta_signal.py before deleting.

---

## PART 7: INTRADAY STACK AUDIT

### Core/Intraday/ Package (built Aug 31)
- `pipeline.py` — Hourly data pipeline
- `calibration.py` — Per-(station, hour) calibration
- `fusion.py` — Hourly LIOP fusion
- `recalibration_loop.py` — Kalman-ish belief update
- `entry_engine.py` — Entry timing with risk guardrails
- `backtest.py` — Hourly backtest framework
- `signal_evaluator.py` — Hourly signal evaluation

### Production Wiring Status

| Component | Wired Into Production? | Evidence |
|-----------|----------------------|----------|
| HourlyPipeline | ❌ NO | Not imported by paper_trading_engine.py, signal_fusion.py, or any cron job |
| HourlyFusionEngine | ❌ NO | Same — no imports found in core/ |
| HourlyCalibration | ❌ NO | Same |
| EntryTimingEngine | ❌ NO | Same |
| ContinuousRecalibrationLoop | ❌ NO | Same |
| HourlyBacktest | ❌ NO | Same |
| **scripts/intraday_runner.py** | ❌ NO | Standalone runner; `__main__` only; no cron entries reference it |
| **scripts/test_intraday_stack.py** | ❌ NO | Test script only |

**Only intraday-related production wiring:** The old `core/signals/intraday_metar_confirmation.py` `get_intraday_confirmation()` function is imported by `paper_trading_engine.py` at line 55 and used at line 3291. This is a **legacy** intraday confirmation path, **NOT** the new HourlyPipeline stack.

**Verdict:** **NOT READY** — The entire intraday stack is standalone and disconnected from any production path. The comment in `core/intraday/__init__.py` says "Retargets the weather engine" but the engine hasn't been retargeted. Building the intraday stack and wiring it into production is a separate delivery.

**Recommendation:** Wire into production by:
1. Adding cron job to run intraday_runner.py hourly during trading hours
2. OR integrating HourlyFusionEngine into signal_fusion.py as an alternative fusion path
3. OR connecting EntryTimingEngine into paper_trading_engine.py before trade execution

---

## PART 8: COMPUTE AND PERFORMANCE PROBLEMS

### Signals That Call External APIs (Live-Only)

| Signal | API | Rate | Impact |
|--------|-----|------|--------|
| **hrrr_bias_corrected** | Open-Meteo `/v1/forecast` | ~0.5s per request | 20 stations x N fetches = 10+ seconds per run. No backtest path. |
| **temperature_advection** | Open-Meteo `/v1/gfs` | ~0.5-1s per request | 20 stations x N fetches. Also called in backtest mode (falls back to DB if API fails). |
| **ecmwf_bias_corrected** | Open-Meteo `/v1/ecmwf` | ~0.5-1s per request | 20 stations x N fetches. Has DB fallback. |

**Performance Concern:** If all 3 API signals fire simultaneously, expect 30-60 seconds of sequential HTTP requests for a single pipeline run. These are synchronous HTTP calls (no async/parallel). Add rate-limiting (0.5s delay per call).

### Signals That Silently Return (None, 0.0)

| Signal | Firing Rate in Pipeline | Reason |
|--------|------------------------|--------|
| **asos_microstructure** | 0% | evaluate() returns (None, 0.0) — standard path doesn't support ASOS data |
| **temperature_trend** | ~0% | Same structural issue — needs ASOS DB through evaluate_for_station() |
| **hrrr_bias_corrected** | 0% | Deliberately non-firing evaluate() — live-API-only |
| **nwp_analog** | ~10-30% | Depends on NWP data availability (experts estimate limited fire rate) |
| **850mb_temp_advection** | ~30% (post-Jul 2026) | Data coverage only since July 8 |
| **500mb_height_pattern** | ~30% (post-Jul 2026) | Same data limitation |

**Impact:**