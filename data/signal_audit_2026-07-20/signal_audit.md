# Signal Audit — 2026-07-20

## Summary

| # | Signal | Issues | Severity | Status |
|---|--------|--------|----------|--------|
| 1 | persistence | None | — | PASS |
| 2 | simple_trend | None | — | PASS |
| 3 | gaussian | None | — | PASS |
| 4 | gaussian_v2 | None | — | PASS |
| 5 | calendar_climatology | 1 | MEDIUM | Defective evaluate_for_station |
| 6 | regime | 1 | MEDIUM | evaluate_for_station returns None |
| 7 | forecast_disagreement | 1 | MEDIUM | evaluate_for_station returns None |
| 8 | pressure_delta | None | — | PASS |
| 9 | goldilocks | 1 | HIGH | Look-ahead bias: `today_high` undefined |
| 10 | wind_direction_shift | 2 | HIGH | evaluate() uses current day's data; DB query may include target date |
| 11 | nwp_analog | 1 | LOW | evaluate() stub returns None |
| 12 | temperature_advection | 2 | HIGH | evaluate() stub; live GFS fetch only; no backtest capability |

---

## Signal 1: `persistence_signal.py` — PersistenceSignal
**Status: PASS**

- **Look-ahead bias**: None. Uses `days[idx-2]['high']` (day before yesterday) and `days[idx-1]['high']` (yesterday). Correct.
- **Computation**: Simple comparison. Correct.
- **Data dependencies**: None beyond standard METAR fields.
- **Dead code**: `evaluate_for_station()` delegates to super(). Clean.

---

## Signal 2: `simple_trend_signal.py` — SimpleTrendSignal
**Status: PASS**

- **Look-ahead bias**: None. Uses `days[idx-2]['high']` and `days[idx-1]['high']`. Correct.
- **Computation**: Simple day-over-day comparison. Correct.
- **Data dependencies**: None beyond standard METAR fields.
- **Dead code**: Clean.

---

## Signal 3: `gaussian_signal.py` — GaussianSignal
**Status: PASS**

- **Look-ahead bias**: None. Uses `_window(days, idx, 48, offset=1)` which returns days `[idx-49, idx-1]`. Then compares `_safe_get(days, idx-1, 'high')`. Correct.
- **Computation**: Rolling z-score with 48-day window. Uses population variance (divides by N). Minor but consistent with original spec. 48-day window correct.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: Clean.

---

## Signal 4: `gaussian_v2_signal.py` — GaussianV2Signal
**Status: PASS**

- **Look-ahead bias**: None. Same pattern as gaussian but 30-day window.
- **Computation**: Rolling z-score, 30-day window, threshold 0.5 vs 1.0. Correct.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: Clean.

---

## Signal 5: `calendar_climatology_signal.py` — CalendarClimatologySignal
**Status: MEDIUM — evaluate_for_station() returns None**

- **Look-ahead bias**: None. Uses `_window(days, idx, 60, offset=1)` and `_safe_get(days, idx-1, 'high')`. Correct.
- **Computation**: 60-day rolling z-score, sample variance (divides by N-1), threshold 1.5, confidence capped at 0.6. Correct.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: `evaluate_for_station()` at line 58 returns `(None, 0.0)` unconditionally. This means the signal can never fire through the `evaluate_for_station()` path. This is a **MEDIUM** issue — it affects the signal's usability in deployment contexts that call `evaluate_for_station()` directly (e.g., paper trading, live polling). The `evaluate()` path works fine, so backtesting through `evaluate()` is unaffected.

---

## Signal 6: `regime_signal.py` — RegimeSignal
**Status: MEDIUM — evaluate_for_station() returns None**

- **Look-ahead bias**: None. Uses `_window(days, idx, 15, offset=1)` for volatility, `_window(days, idx, 30, offset=1)` for mean reversion, `_safe_get(days, idx-1, 'high')`. Correct.
- **Computation**: Volatility threshold (std < 1.0), slope threshold (abs(slope) < 0.5), then mean reversion to 30-day mean. Correct.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: `evaluate_for_station()` at line 85 returns `(None, 0.0)`. Same issue as calendar_climatology.
- **Note**: This signal was marked as "dead" in `__init__.py` docstring (S6 task: "Excluded dead signals: pressure_regime, dtr_trend, reversion, regime_signal") but is still registered. This is intentional—it's kept for experimentation but may be removed later.

---

## Signal 7: `forecast_disagreement_signal.py` — ForecastDisagreementSignal
**Status: MEDIUM — evaluate_for_station() returns None**

- **Look-ahead bias**: None. Uses `_window(days, idx, 7, offset=1)` and `_safe_get(days, idx-1, 'high')`. Correct.
- **Computation**: 7-day rolling mean compared to yesterday's high. Sigmoid confidence. Correct.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: `evaluate_for_station()` at line 72 returns `(None, 0.0)`. Same issue as calendar_climatology and regime.

---

## Signal 8: `pressure_delta_signal.py` — PressureDeltaSignal
**Status: PASS**

- **Look-ahead bias**: None. Uses `_compute_weighted_pressure(idx, days, 72)` which iterates `idx-1, idx-2, idx-3, idx-4` and compares to `_safe_get(days, idx-4, 'pressure')`. Correct.
- **Computation**: Exponential weighting with 12-hour half-life on daily data (approximated). Correct within constraints of daily data.
- **Data dependencies**: Standard METAR fields (pressure specifically).
- **Dead code**: `evaluate_for_station()` is properly overridden with equivalent logic. Clean.

---

## Signal 9: `goldilocks_signal.py` — GoldilocksSignal
**Status: HIGH — Look-ahead bias: undefined variable `today_high`**

- **Look-ahead bias**: **CRITICAL BUG**: Line 144 reads `today_high` but this variable is never defined. The code has:
  ```python
  yesterday_high = _safe_get(days, idx - 1, 'high')
  day_before_high = _safe_get(days, idx - 2, 'high')
  if today_high is None or yesterday_high is None:
  ```
  `today_high` is a typo — it should be `day_before_high`. This would cause a `NameError` at runtime if the signal were ever called. However, `_safe_get` with a missing variable name... actually, `today_high` is used as a variable name, not a key. This WILL raise a `NameError` at runtime.

- **Computation**: The goldilocks spike detection logic looks correct conceptually — detecting a rapid temp change between `idx-2` and `idx-1`, then predicting reversion.
- **Data dependencies**: Standard METAR fields only.
- **Dead code**: `_load_signal_state()` and `_parse_signal_data()` are imported but never called from `evaluate()`. They appear to be vestigial from an earlier design.

---

## Signal 10: `wind_direction_shift.py` — WindDirectionShiftSignal
**Status: HIGH — evaluate() may use current day's data**

- **Look-ahead bias**: **HIGH**. The `evaluate()` method at line 218 builds wind history with:
  ```python
  for i in range(idx, idx - self.lookback_days - 1, -1):
  ```
  This starts at `idx` (the CURRENT day) and goes backward. The current day's wind data (`days[idx]`) would NOT be available at prediction time because the prediction is for the temperature change from day `idx-1` to day `idx`. The loop should start at `idx-1` (yesterday) instead of `idx`.

- **Computation**: The circular difference calculation and temperature implication inference is reasonable. The Southerly/Northerly classification logic is a simplified heuristic.
- **Data dependencies**: Requires wind direction and speed in METAR data.
- **Dead code**: `get_historical_wind_data()` and `generate_signal()` are separate DB-based paths that duplicate `evaluate()`. They're used by the `__main__` test but not through the standard interface.

---

## Signal 11: `nwp_analog_signal.py` — NwpAnalogSignal
**Status: LOW — evaluate() stub returns None**

- **Look-ahead bias**: None in `evaluate_nwp_analog()`. Candidates are filtered to `d < target_date` (strictly before target). Correct.
- **Computation**: Weighted k-NN with seasonal filter, distance-weighted voting, beta-binomial estimate. Sound.
- **Data dependencies**: Requires NWP DB (`nwp_forecasts.db`) with specific fields (850hPa temp, 500hPa geopotential, cloud cover, 2m max temp, 2m dewpoint). Also requires METAR DB with `daily_stats` table. The NWP DB has limited coverage (~150 dates per station).
- **Dead code**: `evaluate()` at line 130 returns `(None, 0.0)` — it's a stub that doesn't work from the standard `days` interface. The real work is in `evaluate_nwp_analog()`. This is a **LOW** issue because the signal is designed to work through `evaluate_for_station()`.

---

## Signal 12: `temperature_advection_signal.py` — TemperatureAdvectionSignal
**Status: HIGH — No backtest capability; live GFS fetch only**

- **Look-ahead bias**: N/A — the signal doesn't work in backtest mode at all.
- **Computation**: The advection formula `-u * dT/dx - v * dT/dy` is physically correct. Grid-based gradient computation is sound.
- **Data dependencies**: Requires live GFS API calls to Open-Meteo. No historical GFS data is cached in the NWP DB. The `load_advection_history()` function queries the NWP DB but the `advection_850hPa` variable is only populated by live fetches.
- **Dead code**: `evaluate()` at line 290 returns `(None, 0.0)` — it's a stub. `evaluate_for_station()` calls `compute_signal_for_station()` which does live GFS fetches. This means the signal **cannot be backtested** and **cannot run in any historical context**.
- **Note**: The task description says this signal is "built but untested (waiting on ERA5 backfill)". Confirmed — the signal is structurally complete but has no historical data source.

---

## Cross-Cutting Issues

### Issue A: `evaluate_for_station()` returns None for 4 signals
Three signals (calendar_climatology, regime, forecast_disagreement) unconditionally return `(None, 0.0)` in `evaluate_for_station()`. This means they cannot fire through the DB-based evaluation path. The `evaluate()` path works fine, so backtesting through `evaluate()` is unaffected, but any deployment that calls `evaluate_for_station()` directly will get no signal from these three.

### Issue B: `evaluate()` stub for NWP-dependent signals
Two signals (nwp_analog, temperature_advection) have `evaluate()` stubs that always return `(None, 0.0)`. They rely on `evaluate_for_station()`. This is by design (they require external data sources), but it means they can't participate in the standard `days`-based backtesting loop.

### Issue C: Goldilocks runtime error
The `today_high` variable in goldilocks_signal.py line 144 will cause a runtime `NameError`. This signal has never been tested in production.

### Issue D: Wind direction shift uses current day
The `evaluate()` loop in wind_direction_shift.py starts at `idx` instead of `idx-1`, which means it uses the current day's wind data — a look-ahead bias.

---

## Scoring Summary

| Signal | Look-ahead Bias | Computation Correct | Data Available | Dead Code | Overall |
|--------|-----------------|---------------------|----------------|-----------|---------|
| persistence | ✅ | ✅ | ✅ | ✅ | PASS |
| simple_trend | ✅ | ✅ | ✅ | ✅ | PASS |
| gaussian | ✅ | ✅ | ✅ | ✅ | PASS |
| gaussian_v2 | ✅ | ✅ | ✅ | ✅ | PASS |
| calendar_climatology | ✅ | ✅ | ✅ | ⚠️ MEDIUM | PASS w/ caveat |
| regime | ✅ | ✅ | ✅ | ⚠️ MEDIUM | PASS w/ caveat |
| forecast_disagreement | ✅ | ✅ | ✅ | ⚠️ MEDIUM | PASS w/ caveat |
| pressure_delta | ✅ | ✅ | ✅ | ✅ | PASS |
| goldilocks | ❌ HIGH | ✅ | ✅ | ✅ | FAIL |
| wind_direction_shift | ❌ HIGH | ✅ | ✅ | ⚠️ LOW | FAIL |
| nwp_analog | ✅ | ✅ | ⚠️ Partial | ⚠️ LOW | PASS w/ caveat |
| temperature_advection | N/A | ✅ | ❌ None | ⚠️ HIGH | FAIL |