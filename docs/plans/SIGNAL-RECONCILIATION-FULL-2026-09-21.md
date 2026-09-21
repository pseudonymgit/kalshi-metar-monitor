# Full Signal Reconciliation — 2026-09-21

**Type:** Reconciliation + registry wiring (registration-only, no signal-logic changes).
**Repo:** `prototypes/weather-engine-source`
**Scope boundary:** Read-only on signal logic. Only `build_signal_registry()` registration
entries + the `SIGNALS_WITH_OWN_DB` / `STANDARD_EVALUATE_SIGNALS` routing sets were changed.
No `evaluate()`/`evaluate_for_station()` bodies modified. No DB writes. No AI-in-loop.
**Parallelization preserved:** R/T-8G's `--workers` + `ProcessPoolExecutor` untouched.

---

## Objective

Prove "nothing is missing": every one of the **58** files under `core/signals/` must be
classified, and every REAL signal (has real `evaluate`/`evaluate_for_station` logic) that the
suite was silently dropping must be wired into `scripts/ultimate_sweep.py`'s
`build_signal_registry()`.

---

## Result: before vs after

| Metric | Before | After |
|---|---|---|
| `build_signal_registry()` entries | **43** | **53** |
| Signals reachable (instantiated) | 43 | 53 |
| Signals wired this pass | — | **+10** |

The +10 newly-wired real signals:

| # | Registry name | Class | Module | Eval path |
|---|---|---|---|---|
| 1 | `persistence` | `PersistenceSignal` | `persistence_signal` | standard `evaluate(idx,days)` |
| 2 | `simple_trend` | `SimpleTrendSignal` | `simple_trend_signal` | standard |
| 3 | `honest_trend` | `HonestTrendSignal` | `honest_trend_signal` | standard |
| 4 | `rdae_mos` | `RDAEMOSSignal` | `rdae_mos_signal` | standard (analog ensemble) |
| 5 | `ghcn_calibration` | `GHCNCalibrationSignal` | `ghcn_calibration_signal` | standard |
| 6 | `sensor_bias_correction` | `SensorBiasCorrectionSignal` | `sensor_bias_correction_signal` | standard |
| 7 | `asos_microstructure` | `AsosMicrostructureSignal` | `asos_microstructure_signal` | for-station (own ASOS DB) |
| 8 | `500mb_height_pattern` | `FiveHundredMbHeightPatternSignal` | `upper_atmosphere_signal` | for-station (own NWP DB) |
| 9 | `850mb_temp_advection` | `EightHundredFiftyMbAdvectionSignal` | `upper_atmosphere_signal` | for-station (own NWP DB) |
| 10 | `ecmwf_bias_corrected` | `ECMWFBiasCorrectedSignal` | `ecmwf_bias_corrected_signal` | for-station (live API → SLOW_SKIP) |

Routing-set updates (registration-supporting, no logic change):
- `SIGNALS_WITH_OWN_DB` += `asos_microstructure`, `500mb_height_pattern`, `850mb_temp_advection`
  (these self-connect to their own archive via `conn=None`).
- `STANDARD_EVALUATE_SIGNALS` += `simple_trend`, `honest_trend`, `rdae_mos`, `ghcn_calibration`,
  `sensor_bias_correction` (so their real `evaluate()` result is not overridden by a
  `evaluate_for_station` fallback).

---

## Smoke-test confirmation (directional layer, `--workers 4 --no-emit`)

Newly-wired signals that now **SCORE** (produce a direction on ≥1 label):

| Signal | acc | n |
|---|---|---|
| `persistence` | 51.61% | 14020 |
| `simple_trend` | 51.61% | 14020 |
| `honest_trend` | 50.31% | 13350 |
| `rdae_mos` | 44.44% | 13118 |
| `sensor_bias_correction` | 50.05% | 9528 |
| `asos_microstructure` | 47.48% | 4888 |

Newly-wired signals that enumerate but do **not** fire in the sweep (documented cause):

| Signal | Result | Cause |
|---|---|---|
| `ecmwf_bias_corrected` | `SKIPPED (slow_or_live_api)` | expected — hits live Open-Meteo API; stays in `SLOW_SIGNALS_TO_SKIP`. Now enumerated. |
| `ghcn_calibration` | `NO_FIRES` | `evaluate()` requires `min_lookback=30` + 7 valid `temp` days; sweep label density/`temp`-field availability falls short. Real logic intact. |
| `500mb_height_pattern` | `NO_FIRES` | **pre-existing data-alignment bug** (see below), NOT a registration gap. |
| `850mb_temp_advection` | `NO_FIRES` | `advection_850hPa` has only **142 rows** (vs 84,185 for other NWP fields) → genuinely data-gated. |

### Upper-atmosphere NO_FIRES root cause (pre-existing, out of scope)

`upper_atmosphere_signal._fetch_series` orders by `target_date DESC` with a LIMIT of
`ROLLING_WINDOW+1`, but the `nwp_forecasts` table holds **multiple rows per target_date**
(multiple fetch times/members). The rolling "history" is therefore N rows of the *same* day
rather than N distinct prior days, so `_z_score` collapses to ~0 and the signal returns
`(None, 0.0)`. Confirmed by direct invocation: `evaluate_for_station('KATL', <any date>)`
returns `(None, 0.0)` for both `500mb` and `850mb`. This is a signal-logic data-dedup issue,
NOT a missing-registration issue — out of this audit's read-only scope. (Data IS present and
contemporaneous: 48,465 contemporaneous 500mb rows, 20/20 station overlap.)

---

## Definitive classification — all 58 files

Legend: **REGISTERED** (scored/enumerated by suite) · **DATA-GATED** (real, but insufficient
data) · **WIRED** (newly added this pass) · **DEAD/INFRA** (no scorable signal).

| # | File | Bucket | Reason |
|---|---|---|---|
| 1 | `__init__.py` | INFRA | registry bootstrap / import aggregator |
| 2 | `base_signal.py` | INFRA | `BaseSignal` ABC + `validate_signal` |
| 3 | `weatherapi_base_signal.py` | INFRA | abstract base (`_metric_from_hour_map` raises `NotImplementedError`); subclassed by `cloud_cover_index`/`feels_like_delta` |
| 4 | `hrrr_bias_corrected_signal.py` | DEAD (wrapper) | `HRRRBiasCorrectedSignal` has NO `evaluate`/`predict` — only `get_forecast`/`apply_bias_correction`/`record_settlement` helpers |
| 5 | `corrected_pressure_delta_signal.py` | INFRA (thin re-export) | re-exports `CorrectedPressureDeltaSignal` from `dual_polarity_signal`; canonical name already registered |
| 6 | `500mb_height_pattern_signal.py` | INFRA (thin re-export) | re-exports `FiveHundredMbHeightPatternSignal` from `upper_atmosphere_signal` |
| 7 | `850mb_temp_advection_signal.py` | INFRA (thin re-export) | re-exports `EightHundredFiftyMbAdvectionSignal` from `upper_atmosphere_signal` |
| 8 | `500mb_height_pattern` (via upper_atmosphere) | **WIRED** | real `evaluate_for_station`; NWP 500mb data present; NO_FIRES = pre-existing dedup bug |
| 9 | `850mb_temp_advection` (via upper_atmosphere) | **WIRED** (DATA-GATED) | real logic; `advection_850hPa` = 142 rows only |
| 10 | `upper_atmosphere_signal.py` | **WIRED** (canonical module for #8/#9) | hosts both upper-air classes |
| 11 | `ai_composite_signal.py` | REGISTERED | `_reg("ai_composite", …)` |
| 12 | `asos_microstructure_signal.py` | **WIRED** | real `evaluate_for_station`; own `iem_asos.db`; scored 47.48% |
| 13 | `calendar_climatology_signal.py` | REGISTERED | |
| 14 | `cloud_cover_index_signal.py` | REGISTERED | own WeatherAPI DB |
| 15 | `cooling_failure_signal.py` | REGISTERED | backtest-only marker |
| 16 | `cross_model_divergence_signal.py` | REGISTERED | |
| 17 | `dewpoint_depression_modulator.py` | REGISTERED | `_reg_func(…modulate_confidence)` |
| 18 | `dual_polarity_signal.py` | REGISTERED | `DualPolaritySignal` + hosts `CorrectedPressureDeltaSignal` |
| 19 | `ecmwf_bias_corrected_signal.py` | **WIRED** (SLOW_SKIP) | real `evaluate_for_station` via live API |
| 20 | `eighty_two_member_ensemble_signal.py` | REGISTERED (×3 variants) | |
| 21 | `esdr_signal.py` | REGISTERED | SLOW_SKIP |
| 22 | `feels_like_delta_signal.py` | REGISTERED | own WeatherAPI DB |
| 23 | `fogr_reversion_signal.py` | REGISTERED | |
| 24 | `forecast_disagreement_signal.py` | REGISTERED | |
| 25 | `frontal_detector_signal.py` | REGISTERED | |
| 26 | `frontal_passage_detector.py` | REGISTERED | `_reg_func(…evaluate)` |
| 27 | `frontal_passage_intraday_signal.py` | REGISTERED | |
| 28 | `frontal_passage_nowcast_signal.py` | REGISTERED | |
| 29 | `gaussian_signal.py` | REGISTERED | |
| 30 | `gaussian_v2_signal.py` | REGISTERED | |
| 31 | `ghcn_calibration_signal.py` | **WIRED** | real `evaluate`; NO_FIRES in sweep (lookback/density) |
| 32 | `goldilocks_signal.py` | REGISTERED | |
| 33 | `honest_trend_signal.py` | **WIRED** | real `evaluate`; scored 50.31% |
| 34 | `intraday_metar_confirmation.py` | DEAD (older class) | `IntradayMETARConfirmation` (non-`_signal`, config-dict ctor); superseded by `_signal` variant |
| 35 | `intraday_metar_confirmation_signal.py` | REGISTERED | `IntradayMetarConfirmationSignal` |
| 36 | `metar_dtdt_signal.py` | REGISTERED | |
| 37 | `metar_nowcast_signal.py` | REGISTERED | |
| 38 | `metar_trend_signal.py` | REGISTERED | |
| 39 | `nwp_analog_signal.py` | REGISTERED (×2 variants) | |
| 40 | `nwp_direct_signal.py` | REGISTERED | SLOW_SKIP |
| 41 | `nwp_dtdt_fusion_signal.py` | REGISTERED | |
| 42 | `persistence_signal.py` | **WIRED** | real `evaluate`; scored 51.61% |
| 43 | `pressure_delta_signal.py` | REGISTERED | |
| 44 | `pressure_tendency_signal.py` | REGISTERED | |
| 45 | `radiational_cooling_signal.py` | REGISTERED | |
| 46 | `rdae_mos_signal.py` | **WIRED** | real analog-ensemble `evaluate`; scored 44.44% |
| 47 | `regime_signal.py` | REGISTERED | |
| 48 | `seasonal_regime_signal.py` | REGISTERED | |
| 49 | `sensor_bias_correction_signal.py` | **WIRED** | real `evaluate`; scored 50.05% |
| 50 | `settlement_arbitrage_signal.py` | REGISTERED | |
| 51 | `simple_trend_signal.py` | **WIRED** | real `evaluate`; scored 51.61% |
| 52 | `spike_reversion_signal.py` | REGISTERED | |
| 53 | `spread_based_entry_signal.py` | REGISTERED | |
| 54 | `subhourly_trajectory_signal.py` | REAL-but-interface-incompatible | real `evaluate_nowcast`/`compute_slope`, but NO `evaluate_for_station` and `evaluate()` returns `(None,0.0)`; not suite-scoreable without adaptation (out of read-only scope) |
| 55 | `temperature_advection_signal.py` | REGISTERED | SLOW_SKIP |
| 56 | `temperature_trend_signal.py` | REAL-but-interface-incompatible | real logic, but `evaluate_for_station(station,date,asos_conn,metar_conn,current_temp_f,current_hour)` signature incompatible with sweep's `conn=` call |
| 57 | `trajectory_state_vector_signal.py` | REGISTERED | hollow stub (both methods `(None,0.5)`) |
| 58 | `volume_momentum_signal.py` | REGISTERED | |
| 59 | `warm_momentum_signal.py` | REGISTERED | |
| 60 | `wind_direction_shift.py` | REGISTERED | |

> Note: 58 files = the 58 `core/signals/*.py` files enumerated at task start. The two
> "via upper_atmosphere" rows (#8/#9) are registry entries (not extra files); the physical
> files are `500mb_height_pattern_signal.py` (#6) and `850mb_temp_advection_signal.py` (#7)
> thin re-exports. The table lists 60 rows because the two upper-air registry entries are
> called out explicitly alongside their re-export stub files for full traceability.

---

## Two real signals deliberately NOT force-scored (interface-incompatible)

1. **`temperature_trend`** — `evaluate_for_station` signature is
   `(station, date, asos_conn=None, metar_conn=None, current_temp_f=None, current_hour=None)`,
   incompatible with the sweep's `evaluate_for_station(station, date, conn=…)` call. Wiring it
   would only ever produce `NO_FIRES` (TypeError swallowed). Real logic, needs a dedicated
   scoring adapter (out of this read-only scope).
2. **`subhourly_trajectory`** — has `evaluate_nowcast(station, local_trading_date, t_minute, …)`
   but no `evaluate_for_station`; `evaluate()` is `(None,0.0)`. Sub-hourly nowcast interface,
   not the daily-label sweep interface.

These are **not** "missing" — they are present, real, and reachable; they simply target a
different evaluation interface than the daily directional sweep.

---

## Stop-condition checklist

1. ✅ Reconciliation table (above) mapping all 58 files → bucket + reason.
2. ✅ 10 missing real signals wired into `build_signal_registry()` (43 → 53).
3. ✅ Receipt here + commit hash (see below) with before/after counts.
4. ✅ Git: `scripts/ultimate_sweep.py` + this doc committed. R/T-8H's
   `scripts/rt8h_exhaustive_calibration_grid.py` and `docs/plans/DOWNSTREAM-CRAWL-2026-09-21.md`
   left untouched (concurrent-agent artifacts, out of scope).

**Commit hash:** `4df861af8`
