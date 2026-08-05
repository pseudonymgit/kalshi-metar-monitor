# HRRR Pivot Plan — ECMWF IFS Replacement + Rolling Bias Correction

**Date:** 2026-08-05  
**Author:** Donna (subagent)  
**Status:** Plan for review by Dan / Gerri

---

## 1. ECMWF IFS Replacement: Can We Swap HRRR for ECMWF?

**→ YES.** Drop-in replacement with minor code changes.

### Evidence

| Check | Result |
|---|---|
| `ecmwf_ifs` on Open-Meteo forecast API | **Confirmed working** — live API call returns hourly temperature_2m for KNYC |
| Existing collector uses `v1/ecmwf` endpoint | **Confirmed** — `scripts/nwp_backfill_30d.py` line 90: `("ecmwf", "https://api.open-meteo.com/v1/ecmwf")` |
| `live_multi_model_consensus()` in `multi_model_ensemble.py` | Already uses `ecmwf_ifs` as the API model param for the ECMWF model |
| NWP DB has ECMWF forecasts | **Confirmed** — 431k rows, model `ecmwf`, all 20 stations, daily max/min + hourly aggregated vars |
| ECMWF is the best-performing model | `MODEL_MAE` in `multi_model_ensemble.py`: ECMWF = 2.1°F vs GFS = 2.4°F vs ICON = 2.6°F vs GEM = 2.8°F |

### What changes in the signal code

The signal `core/signals/hrrr_bias_corrected_signal.py` needs:

1. **Rename the class** → `ECMWFBiasCorrectedSignal` (or keep `NWPBiasCorrectedSignal` for model-agnostic naming)
2. **Change the API URL** — instead of `api.open-meteo.com/v1/forecast` with no model param, use explicit `models=ecmwf_ifs025` (or `models=ecmwf_ifs`)
3. **Rename the bias DB** — `hrrr_bias.db` → `ecmwf_bias.db` (or `nwp_bias.db`), rename tables `hrrr_bias` → `ecmwf_bias`, `hrrr_forecasts` → `ecmwf_forecasts`
4. **Update imports** in paper_trading_engine.py, lane_manager.py, operation_state.py, scripts
5. **Update `core/signals/__init__.py`** — change registry entry

The `_fetch_from_open_meteo()` method in the signal currently uses `temperature_unit=fahrenheit` and `forecast_days=3`. ECMWF IFS supports all of these. The hourly response structure is identical — `{hourly: {time: [...], temperature_2m: [...]}}` — so the parsing code is unchanged.

### Key difference: HRRR vs ECMWF IFS

| Aspect | HRRR | ECMWF IFS |
|---|---|---|
| Resolution | 3km | ~25km (0.25°) |
| Forecast horizon | 48h (short) | 10-15 days |
| Refresh rate | Hourly | 12h (00z/12z runs) |
| Availability on Open-Meteo | **NOT available** | **Available** (`ecmwf_ifs025` or `v1/ecmwf`) |
| Accuracy | Best for 0-18h | Best overall (lowest MAE) |

For Kalshi daily max markets (settled on bucket temps), ECMWF IFS is actually **better suited** — the smaller diurnal range of IFS vs HRRR doesn't matter for bucket direction, and IFS's longer lead time is irrelevant since we only need 1-2 day forecasts.

---

## 2. Rolling Bias Correction on Existing Data: Does It Add Value?

**→ YES.** It's a fundamentally different signal from Edge 20's inverse-MAE weighting.

### Why they're different

| Aspect | Edge 20 (inverse-MAE) | Rolling Bias Correction |
|---|---|---|
| **Weighting** | Static weights per model (long-term average) | Dynamic per-station bias (14-day window) |
| **Adaptation speed** | Weeks/months (recalculated periodically) | Days (each settlement updates the bias) |
| **Granularity** | Global model-level MAE | Per-station, per-model bias |
| **What it captures** | Which model is generally better | Recent model drift (seasonal transitions, data assimilation changes) |
| **Signal type** | Ensemble consensus | Corrected point forecast |
| **Complementarity** | Static rank → good for long-term signal | Dynamic drift → good for catching regime changes |

### How bias correction adds value

- **Systematic error removal**: If ECMWF consistently over-forecasts KNYC by 0.8°F, removing that bias improves the raw forecast's accuracy directly.
- **Adaptive drift capture**: If a model's bias shifts from +0.5°F to -0.3°F over 7 days (e.g., seasonal transition), the rolling window catches this. Edge 20's static MAE doesn't.
- **Per-station calibration**: A model may have different bias in Phoenix vs Boston. The rolling bias is per-station; Edge 20's MAE weights are per-model only.

### Quantified expectation

The existing `get_station_bias()` logic is already in the code — it computes `avg(actual - forecast)` over a 14-day rolling window with a minimum of 7 observations and a ±5°F cap. This is a production-ready mechanism. The missing piece is the actual data pipeline (forecast → actual → bias update).

---

## 3. Differential Bias: What Is It and What's Most Valuable?

**Differential bias** = comparing bias-corrected forecasts across models (or across time for the same model).

### Three definitions scoped

| # | Definition | Formula | What it measures | Value |
|---|---|---|---|---|
| **A** | **Cross-model divergence** | `(GFS_forecast - GFS_bias) - (ECMWF_forecast - ECMWF_bias)` | How much the models disagree *after* removing each model's systematic error | **HIGHEST VALUE** — this is a cleaner version of Edge 20's disagreement signal. If GFS says 80°F (corrected, MAE=2.4) and ECMWF says 74°F (corrected, MAE=2.1), the divergence flags true uncertainty vs model-specific noise. |
| **B** | **Bias drift** | `bias_today - bias_7_days_ago` | Second derivative of model accuracy — how fast is the model's systematic error changing | **MEDIUM VALUE** — good regime change detector, but derivative signals are noisy with only 14-data-point windows. |
| **C** | **Standard bias correction** | `forecast - actual` (accumulated as rolling mean) | What the model crew is doing — the baseline bias term | **BASELINE** (already implemented). The value is in the corrected forecast, not the bias itself. |

### Recommendation: Build A (cross-model divergence) as the differential signal

**Why A is most valuable:**

1. **Edge 20 already uses disagreement** (z-score modulation) but only on *raw* forecasts. Differential bias removes each model's systematic error before computing disagreement, producing a cleaner signal.
2. **When models disagree after bias correction**, the divergence is more likely to be real uncertainty (e.g., a front passing through) rather than model-specific noise.
3. **Can be implemented as a confidence modulator** — wider divergence after bias correction → suppress confidence, narrower divergence → amplify confidence.
4. **Directly integrates with the existing `forecast_disagreement_signal.py`** — just plug in bias-corrected forecasts instead of raw forecasts.

**Implementation:** `differential_bias_spread = abs(bias_corrected_gfs_temp - bias_corrected_ecmwf_temp)`. When this spread is > 2σ of historical, suppress confidence. When < 0.5σ, amplify.

---

## 4. Implementation Plan

### Phase 1: ECMWF Pivot (minimal — 1-2 hours code)

**Files to modify:**

| File | Change |
|---|---|
| `core/signals/hrrr_bias_corrected_signal.py` | 1. Rename class → `ECMWFBiasCorrectedSignal` (or `NWPBiasCorrectedSignal`). 2. Change `_fetch_from_open_meteo()` to use `models=ecmwf_ifs025` (or `v1/ecmwf`). 3. Rename bias DB tables from `hrrr_bias` → `ecmwf_bias`. 4. Rename file → `ecmwf_bias_corrected_signal.py` (or keep as `nwp_bias_corrected_signal.py`). |
| `core/paper_trading_engine.py` | Update import from `HRRRBiasCorrectedSignal` → `ECMWFBiasCorrectedSignal`. Update `HAS_HRRR_BIAS_CORRECTED` → `HAS_ECMWF_BIAS_CORRECTED`. Update Signal 9 wiring. |
| `core/lane_manager.py` | Update signal name references (`hrrr_bias_corrected` → `ecmwf_bias_corrected`). |
| `core/operation_state.py` | Update signal name references. |
| `core/intraday_trading_loop.py` | Update import and references. |
| `core/signals/__init__.py` | Update registry entry. |
| `scripts/run_hrrr_signal.py` | Rename → `run_ecmwf_signal.py` (or `run_nwp_signal.py`). Update imports. |
| `scripts/backtest_hrrr_bias_corrected.py` | Rename, update imports. |
| `scripts/phaseB_*.py` | Update any references to HRRR signal. |

### Phase 2: Bias Pipeline (connect forecast → actual → bias update)

**New/modified code:**

| Component | What it does |
|---|---|
| `core/signals/ecmwf_bias_corrected_signal.py` (modified) | Add `_record_actual_from_settlement()` — on settlement close, read actual daily max from `daily_stats` or `settlement_epochs`, compute bias, store. |
| `core/signals/ecmwf_bias_corrected_signal.py` (existing) | `get_station_bias()` already works — reads rolling 14-day bias from the DB. |
| `core/paper_trading_engine.py` | After settlement, call `record_settlement(station, date, hour, actual_temp)` to update bias. |
| Cron (daily) | After settlement epoch closes each day, run a bias update pass: for each station, fetch actual max from daily_stats, record bias. |

### Phase 3: Cross-Model Differential Bias (optional, high value)

| Component | What it does |
|---|---|
| `core/signals/differential_bias_signal.py` | New signal. Compute bias-corrected forecasts for GFS and ECMWF, compare their divergence. Output confidence modulation. |
| Integration with `forecast_disagreement_signal.py` | Feed differential bias spread as additional confidence input. |

### Phase 4: Data Pipeline

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────────┐
│ Open-Meteo API  │────→│ nwp_forecasts.db │────→│ ECMWF bias signal  │
│ (ecmwf_ifs025)  │     │ (daily fetches)  │     │ (rolling 14d bias) │
└─────────────────┘     └───────┬─────────┘     └─────────────────────┘
                                │
                                │ (forecast temp for target_date)
                                ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────────┐
│ METAR stations  │────→│ daily_stats.db  │────→│ Bias computation    │
│ (actuals)       │     │ (actual max/min)│     │ actual - forecast   │
└─────────────────┘     └─────────────────┘     └─────────────────────┘
```

---

## 5. Backfill Status

### NWP data: Already collected
- **nwp_forecasts.db**: 431,180 rows across 20 stations, 4 models (GFS, ECMWF, ICON, GEM), daily fetch dates
- **ECMWF data**: All 20 stations, temperature_2m_max + temperature_2m_min + hourly aggregated vars
- **Date range**: 2021-01-02 to 2026-08-14 (forecast target dates)
- **Recent fetches**: ECMWF fetched daily since 2026-08-01 (at least 5 consecutive days of daily fetches)
- **No backfill needed for the signal itself** — the bias window is only 14 days and starts cold. First valid bias appears after 7 observations.

### Actuals data: Already collected
- **daily_stats**: 51,724 rows (1,733 days per station, 2021-01-01 to 2026-08-05)
- **settlement_epochs**: 98,640 rows of settled trade outcomes
- **Bias computation**: Join `nwp_forecasts` (fetch_date = target_date - 1) with `daily_stats` (date_utc = target_date) for each station

### What's needed for the first bias computation
- **Nothing to backfill** — the rolling bias starts cold. After 7 settlement days with forecast+actual pairs, the first reliable bias estimate becomes available.
- **Optional**: For a faster warm start, we can compute historical bias from the existing NWP data + daily_stats (forecast issued 1 day ahead vs actual). This gives us 30+ days of bias history immediately.

---

## 6. Risk Assessment

| Risk | Impact | Mitigation |
|---|---|---|
| ECMWF IFS hourly data not available for all stations | Low | Open-Meteo v1/ecmwf covers all 20 stations (confirmed by existing NWP data) |
| 14-day window too short for stable bias | Low | `MIN_BIAS_OBSERVATIONS = 7` ensures minimum. ±5°F cap prevents runaway. Can increase to 21 days after validation. |
| Bias correction doesn't improve accuracy | Medium | Backtest against historical data (Phase 2). If no improvement, the signal is a no-op (confidence = 0.5, no bias applied). |
| Model drift correlation between models | Medium | Cross-model differential bias (Phase 3) handles this — if all models drift together, the differential signal stays within bounds. |
| Open-Meteo rate limiting | Low | 10k free requests/day. Running ECMWF hourly for 20 stations × 3 days = 60 requests/day. Well within limits. |

---

## 7. Recommended Next Steps

1. **Dan approves** pivot direction and scope (Phase 1 only, or Phase 1+2, or all phases)
2. **Gilfoyle** implements Phase 1 (ECMWF replacement) — estimated 1-2 hours
3. **Marty Byrde** validates against historical data — does bias-corrected ECMWF outperform raw ECMWF?
4. **Gerri** reviews the differential bias design (Phase 3) — is it worth the complexity vs existing Edge 20 disagreement?
5. **Deploy**: Rename signal, update DB, update cron collectors, update paper_trading_engine

---

## Appendix: Quick Reference — Files to Modify

### Rename/Create
- `core/signals/hrrr_bias_corrected_signal.py` → `core/signals/ecmwf_bias_corrected_signal.py`
- `scripts/run_hrrr_signal.py` → `scripts/run_ecmwf_signal.py`
- `scripts/backtest_hrrr_bias_corrected.py` → `scripts/backtest_ecmwf_bias_corrected.py`

### Update imports
- `core/paper_trading_engine.py` (lines 258-265, 401, 416-418, 1111-1134, 1228)
- `core/intraday_trading_loop.py` (lines 65, 92, 153, 225, 498)
- `core/lane_manager.py` (lines 61-63)
- `core/operation_state.py` (line 146)
- `core/signals/__init__.py` (line 32, registry entry)

### Database
- `data/hrrr_bias.db` → `data/ecmwf_bias.db` (migrate tables: `hrrr_bias` → `ecmwf_bias`, `hrrr_forecasts` → `ecmwf_forecasts`)

## Implementation Notes — Phase 1 Complete (2026-08-05)

**Executed by:** Gilfoyle (subagent)

### What was done

1. **Renamed** `core/signals/hrrr_bias_corrected_signal.py` → `core/signals/ecmwf_bias_corrected_signal.py` (git mv)
2. **Renamed class** `HRRRBiasCorrectedSignal` → `ECMWFBiasCorrectedSignal`
3. **Changed API endpoint** from `v1/forecast` (auto-select) to `v1/ecmwf` (explicit ECMWF IFS)
   - Verified: Live API call to KNYC returns hourly temperature_2m data
   - Both `v1/ecmwf` and `v1/forecast?models=ecmwf_ifs025` work; chose `v1/ecmwf` (matches existing NWP collector `nwp_backfill_30d.py`)
4. **Renamed DB tables** `hrrr_bias` → `ecmwf_bias`, `hrrr_forecasts` → `ecmwf_forecasts`, column `hrrr_temp_f` → `ecmwf_temp_f`
5. **Migrated DB** `data/hrrr_bias.db` → `data/ecmwf_bias.db` (old DB preserved as backup)
6. **Renamed scripts** `run_hrrr_signal.py` → `run_ecmwf_signal.py`, `backtest_hrrr_bias_corrected.py` → `backtest_ecmwf_bias_corrected.py`
7. **Updated imports** in:
   - `core/paper_trading_engine.py` — `HAS_HRRR_BIAS_CORRECTED` → `HAS_ECMWF_BIAS_CORRECTED`, `HRRRBiasCorrectedSignal` → `ECMWFBiasCorrectedSignal`, signal name `hrrr_bias_corrected` → `ecmwf_bias_corrected`
   - `core/intraday_trading_loop.py` — import, `STAGE_B_SIGNALS`, `self.hrrr` → `self.ecmwf`, docstrings
   - `core/lane_manager.py` — signal name references
   - `core/operation_state.py` — signal name in DEGRADED core signals
   - `core/signals/__init__.py` — docstring registry entry
   - `scripts/phaseB_calibration_pipeline.py` — skip lists
   - `scripts/phaseB_signal_benchmarks.py` — skip lists
   - `scripts/phaseB_one_shot.py` — skip lists
   - `scripts/phaseB_combinatorial_search.py` — families and subsets
   - `tests/test_advance_signals.py` — class name, imports, DB paths
   - `tests/test_architecture_decomposition.py` — module path reference

### Verification results

| Check | Result |
|---|---|
| Live KNYC fetch | Max=87.0°F, Min=74.8°F, Confidence=0.5 (cold start, no bias yet) |
| Forecasts stored in ecmwf_forecasts | 72 rows (3 days × 24h) |
| Trade signal generation | KNYC → UP (conf=0.55, above 75°F threshold) |
| Test suite (ECMWF tests) | 6/6 pass |
| Test suite (overall) | 20/21 pass (1 pre-existing MetarNowcast failure) |
| Old DB preserved | `data/hrrr_bias.db` (0 rows — HRRR was never populated) |

### Guardrails respected

- ✅ B-Mode compliant: no AI/ML in the signal loop
- ✅ Rolling 14-day bias correction logic preserved unchanged
- ✅ TIGGE backfill, graphify, calibration, crons NOT touched
- ✅ Model source changed from HRRR to ECMWF IFS only
- ✅ Old DB backed up, not deleted

### API endpoint confirmed

**Chosen:** `https://api.open-meteo.com/v1/ecmwf`

**Parameters:** `latitude, longitude, hourly=temperature_2m, forecast_days=3, temperature_unit=fahrenheit, timezone=auto`

**Response structure:** `{hourly: {time: [...], temperature_2m: [...]}}` — identical to v1/forecast, no parsing changes needed.

**Also tested:** `https://api.open-meteo.com/v1/forecast?models=ecmwf_ifs025` — returns identical data. Chose `v1/ecmwf` for consistency with existing NWP collector (`nwp_backfill_30d.py` line 90).

### Next steps for Phase 2

1. Connect settlement actuals → bias update pipeline (see Phase 2 in plan above)
2. Warm-start bias from existing NWP data + daily_stats (optional)
3. Remove old `data/hrrr_bias.db` after confirming production stability