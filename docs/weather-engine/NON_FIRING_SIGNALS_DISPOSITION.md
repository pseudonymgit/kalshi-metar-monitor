# Non-Firing Signals Disposition

**Source:** Full fire-rate audit (40,885 samples/signal, 20 stations)
**Date:** 2026-08-10
**Audit:** `scripts/audit_all_signals.py` (full range)
**Disposition:** `scripts/audit_all_signals.py` (signal registry with mode/notes)

## Classification Key

| Category | Label | Description |
|----------|-------|-------------|
| **(a)** | Base/non-signal | ABC, base class, module-level functions, or not a signal class |
| **(b)** | Deprecated | Explicitly deprecated and returns (None, 0.0) |
| **(c)** | Needs unavailable data source | Requires data (table, column, DB, API) not available |
| **(d)** | Has bugs | Bugs in implementation that prevent firing |
| **(e)** | Genuinely doesn't work | Tested thoroughly, doesn't fire with available data |

---

## 1. Base Class / Non-Signal (3 files)

### `base_signal.py` — BaseSignal (ABC)
- **Class:** (a) Base class / abstract
- **Disposition:** NOT a signal. Abstract base class. All signals extend this.
- **Action:** Keep as-is.

### `weatherapi_base_signal.py` — WeatherAPIBaseSignal
- **Class:** (a) Base class (abstract)
- **Details:** Abstract base class requiring abstract `name` property. No concrete subclass exists in codebase. Can't instantiate.
- **Action:** Keep as base class. If WeatherAPI-based signals are needed later, extend from this.

### `dual_polarity_signal.py` — DualPolaritySignal
- **Class:** (a) Wrapper class
- **Details:** Requires `wrapped_signal` argument — it's a wrapper that applies seasonal polarity inversion to an existing signal. Not a standalone signal.
- **Action:** Keep as-is. The actual subclasses `CorrectedPressureDeltaSignal` and `SeasonalRegimeClassifier` in the same module ARE used and fire in the registry.

---

## 2. Module-Level Functions (3 files)

### `dewpoint_depression_modulator.py`
- **Class:** (a) Module-level functions only
- **Details:** `modulate_confidence()` and `_get_most_recent_dpd()` — helper functions, not a signal class. No BaseSignal subclass.
- **Action:** Keep as module. Used as a confidence modulator helper.

### `frontal_passage_detector.py`
- **Class:** (a) Module-level functions only
- **Details:** `evaluate()`, `get_frontal_conditions()`, `get_temperature_gradient_at_station()` — standalone functions. No BaseSignal subclass.
- **Action:** Keep as module. Provides shared helper functions for frontal analysis.

### `intraday_metar_confirmation.py` — IntradayMETARConfirmation
- **Class:** (a) Not a BaseSignal subclass
- **Details:** Custom class with `get_intraday_confirmation()` — separate from the true signal `intraday_metar_confirmation_signal.py` which IS registered. This is a utility class.
- **Action:** Keep as utility. The actual registered signal is `intraday_metar_confirmation_signal.py`.

---

## 3. Deprecated (1 file)

### `frontal_detector_signal.py` — FrontalDetectorSignal
- **Class:** (b) Deprecated
- **Details:** The `evaluate()` method explicitly logs a WARNING: "FrontalDetectorSignal (daily) is DEPRECATED. Use FrontalPassageIntradaySignal for intraday frontal passage detection. This signal will not generate trades." Returns (None, 0.0) unconditionally.
- **Action:** Keep as-is for backward compatibility. Remove from sweep once FrontalPassageIntradaySignal is fully verified.

---

## 4. Needs Unavailable Data Source (7 files)

### `ai_composite_signal.py` — AiCompositeSignal
- **Class:** (c) Needs data source (ai_forecasts table)
- **Details:** Queries `ai_forecasts` table from NWP DB for AI model predictions (aigfs, graphcast, aifs). This table does not exist in any available database.
- **Required data:** `ai_forecasts` table with AI model forecast temperatures.
- **Build effort:** Medium — needs AI model data collection pipeline.
- **Action:** Keep in codebase. Wire when AI forecast data becomes available.

### `esdr_signal.py` — EsdrSignal
- **Class:** (c) Needs data source (member_index column)
- **Details:** Queries `nwp_forecasts.member_index` column for ensemble member data. The `nwp_forecasts` table exists but lacks `member_index` column. Member data is stored as `temperature_2m_max_membergep01` variables instead.
- **Required data:** `member_index` column in `nwp_forecasts`, OR rewrite to use variable naming convention.
- **Build effort:** Medium — rewrite ESDR query to match existing variable encoding.
- **Action:** Keep in codebase. Fix query or add member_index column.

### `metar_nowcast_signal.py` — MetarNowcastSignal
- **Class:** (c) Needs data source (metar_archive.db)
- **Details:** Looks for `metar_archive.db` for live METAR observations. This DB doesn't exist — all METAR data is in `metar_backfill.db`.
- **Required data:** Live METAR data table or rewrite to use current DB.
- **Build effort:** Low — change DB path or query.
- **Action:** Keep in codebase. Update to query `metar_backfill.db` for intraday confirmation.

### `hrrr_bias_corrected_signal.py` — HRRRBiasCorrectedSignal
- **Class:** (c) Needs live API data
- **Details:** Fetches HRRR model data from Open-Meteo API. No evaluate() method — only get_forecast(). Live-API-only signal.
- **Required data:** Live HRRR API access at forecast time.
- **Action:** Not a backtestable signal. Keep operational.

### `temperature_advection_signal.py` — TemperatureAdvectionSignal
- **Class:** (c) Needs live GFS data (network-bound)
- **Details:** Fetches GFS grid data via Open-Meteo API at evaluation time. Each call ~0.66s network I/O. Cannot be backtested offline.
- **Required data:** GFS grid data API.
- **Action:** Keep as live signal. Wire into operational pipeline only.

### `nwp_direct_signal.py` — NwpDirectSignal
- **Class:** (c) Needs station context
- **Details:** evaluate() needs `_station` attribute set externally; returns (None, 0.0) without it. Continuation-style signals require station/date via evaluate_for_station().
- **Required data:** Station context (available via evaluate_for_station).
- **Action:** Register with station-interface mode. Could fire via evaluate_for_station with NWP data.

### `nwp_dtdt_fusion_signal.py` — NwpDtdtFusionSignal
- **Class:** (c) Needs NWP dT/dt data
- **Details:** Combines NWP temperature delta with METAR derivative. Requires NWP forecast data for temperature change computation.
- **Required data:** NWP forecast data with temperature deltas.
- **Action:** Keep in codebase. Requires NWP data integration.

---

## 5. Has Bugs (1 file)

### `frontal_passage_nowcast_signal.py` — FrontalPassageNowcastSignal
- **Class:** (d) Non-standard evaluate() interface
- **Details:** `evaluate()` takes `evaluate(self, context: dict)` instead of standard `evaluate(self, idx, days)`. Causes `TypeError: takes 2 positional arguments but 3 were given` when called with standard interface. Has working `evaluate_for_station()`.
- **Fix applied:** Registered with interface mode 'station' in audit.
- **Further fix needed:** Add standard `evaluate(idx, days)` wrapper that extracts station/date and calls evaluate_for_station().
- **Build effort:** Low (~10 lines wrapper).
- **Action:** Fix evaluate() to accept (idx, days) and extract station/date from days[0].

---

## 6. Genuinely Doesn't Work (12 files)

### Days-based signals (tested with full data, no errors, return (None, 0.0))

#### `cloud_cover_index_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** The evaluate() method accesses `days[idx].get('cloud_cover')` — but the standard daily aggregation only produces high/low/temp/wind/pressure fields, not cloud cover.
- **Disposition:** (e) Genuinely doesn't work. Requires cloud cover data not in standard METAR aggregation.
- **Action:** Document as needing cloud cover field. Won't fire without schema change.

#### `cross_model_divergence_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Relies on multi-model divergence which isn't in GEFS single-source days data.
- **Disposition:** (e) Genuinely doesn't work with available data.
- **Action:** Keep for multi-model ensemble path.

#### `eighty_two_member_ensemble_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Needs 82-member GEFS ensemble data. GEFS archive has 31 members max. This is a legacy signal for the older 82-member GEFS configuration.
- **Disposition:** (e) Genuinely doesn't work. GEFS now has 31 members.
- **Action:** Deprecate. Update to 31-member ensemble if needed.

#### `feels_like_delta_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Uses wind chill / heat index computation. Needs wind/humidity fields not in standard days aggregation.
- **Disposition:** (e) Genuinely doesn't work with standard METAR aggregation.
- **Action:** Keep as experimental. Won't fire without schema enhancement.

#### `fogr_reversion_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** FOG reversion — requires fog detection logic. evaluate() computes fog probability from temp-dewpoint spread but returns (None, 0.0) for most cases.
- **Disposition:** (e) Genuinely doesn't work. Fog events are rare (∼1% of days).
- **Action:** Keep as experimental low-frequency signal.

#### `frontal_passage_intraday_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** evaluate() requires `station` field in days dict (checks `day.get("station")`). Standard days dicts don't have station field. Returns (None, 0.0).
- **Disposition:** (e) Genuinely doesn't work. Design requires station-aware days.
- **Action:** Fix: add station to days dict or use evaluate_for_station().

#### `intraday_metar_confirmation_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** evaluate() uses `days[idx]` directly without extracting station/date. The signal's evaluate() needs intraday METAR data not available in daily aggregation.
- **Disposition:** (e) Genuinely doesn't work with daily data.
- **Action:** Keep as experimental. Requires intraday METAR data integration.

#### `metar_dtdt_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Temperature derivative signal — computes dT/dt from consecutive days. evaluate() has computation that returns (None, 0.0) for most values.
- **Disposition:** (e) Genuinely doesn't work. Temperature changes are usually below threshold.
- **Action:** Review threshold and criteria. May need retuning.

#### `pressure_tendency_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Similar to pressure_delta but uses tendency computation. evaluate() returns (None, 0.0) — pressure change threshold not exceeded.
- **Disposition:** (e) Genuinely doesn't work. Pressure_delta handles the same concept at 58% fire rate.
- **Action:** Identify as duplicate. Recommend deprecation.

#### `settlement_arbitrage_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Settlement arbitrage — needs Kalshi settlement price data. evaluate() checks for arbitrage opportunities but METAR-only data has no prices.
- **Disposition:** (e) Genuinely doesn't work. Requires market price data, not METAR data.
- **Action:** Keep for production pipeline (needs live market data).

#### `volume_momentum_signal.py`
- **Errors:** None
- **Fire Rate:** 0.0%
- **Diagnosis:** Volume/momentum signal. evaluate() requires volume data not in METAR days.
- **Disposition:** (e) Genuinely doesn't work. No volume data in standard aggregation.
- **Action:** Keep for market data integration path.

#### `regime_signal.py`
- **Errors:** None
- **Fire Rate:** 0.1% (42/40,265)
- **Diagnosis:** Regime classification returns direction only 0.1% of the time. Below 1% threshold but technically fires.
- **Disposition:** (e) Genuinely doesn't work at useful rate. 0.1% fire rate is too low for production.
- **Action:** Review threshold tuning. If regime detection can be improved, can be reactivated.

---

## Summary

| Category | Count | Files |
|----------|-------|-------|
| **(a)** Base/non-signal | 6 | `base_signal`, `weatherapi_base`, `dual_polarity`, `dewpoint_depression_modulator`, `frontal_passage_detector`, `intraday_metar_confirmation` |
| **(b)** Deprecated | 1 | `frontal_detector` |
| **(c)** Needs data source | 7 | `ai_composite`, `esdr`, `metar_nowcast`, `hrrr_bias_corrected`, `temperature_advection`, `nwp_direct`, `nwp_dtdt_fusion` |
| **(d)** Has bugs | 1 | `frontal_passage_nowcast` (fixed interface classification) |
| **(e)** Genuinely doesn't work | 10 | `cloud_cover_index`, `cross_model_divergence`, `eighty_two_member_ensemble`, `feels_like_delta`, `fogr_reversion`, `intraday_metar_confirmation`, `metar_dtdt`, `pressure_tendency`, `regime`, `volume_momentum` |

**Total non-firing files: 25** (21 per full audit + 4 base/module files not in audit)

## Fixable Signals (Task 5 action items)

| Signal | Fix | Effort |
|--------|-----|--------|
| `frontal_passage_intraday` | Add station field to days dict, or use evaluate_for_station | Low |
| `nwp_direct` | Wire via station interface + NWP data | Low |
| `frontal_passage_nowcast` | Add standard evaluate(idx, days) wrapper | Low |

After fixes, run full fire-rate audit again.

## Signals Formally Retired

| Signal | Reason |
|--------|--------|
| `pressure_tendency` | Duplicate of pressure_delta (58% fire rate) |
| `eighty_two_member_ensemble` | GEFS now has 31 members, not 82 |

All other signals kept in codebase as experimental or for their respective
data-path integration points.