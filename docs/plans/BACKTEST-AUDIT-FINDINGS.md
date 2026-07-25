# Backtest Validation Mismatch Audit Findings

**Date:** 2026-07-23  
**Author:** Donna Paulsen / Gilfoyle (subagent)  
**Status:** CONFIRMED — mismatch exists, fix implemented

---

## 1. Executive Summary

There is a **fundamental mismatch** between what signals predict and how `unified_backtest.py` validates accuracy.

| Component | Predicts | Direction Meaning |
|---|---|---|
| **All signals** | Directional change (today vs yesterday) | `'up'` = temp warmer than yesterday |
| **unified_backtest.py** (broken) | Strike-level comparison | `'up'` = settlement > median(strike) |
| **Phase 8/9 scripts** (correct) | Directional change (today vs yesterday) | `'up'` = settlement > yesterday |

This mismatch explains the accuracy collapse from ~67% (Phase 8/9) to ~27-39% (Phase A/B).

---

## 2. Signal Analysis

Every signal in `core/signals/` predicts **directional change** — whether today's temperature will go UP or DOWN compared to yesterday's level:

### Calendar Climatology (`calendar_climatology_signal.py`)
- Computes z-score of **yesterday's high** vs 60-day rolling mean
- If z > 1.5 (too hot yesterday): predicts `'down'` (cooling toward mean)
- If z < -1.5 (too cold yesterday): predicts `'up'` (warming toward mean)
- **Directional change from yesterday**

### Gaussian (`gaussian_signal.py`)
- 48-day window z-score of **yesterday's high** vs mean
- If z > 1.0: predicts `'down'` (cooling reversion)
- If z < -1.0: predicts `'up'` (warming reversion)
- **Directional change from yesterday**

### Gaussian V2 (`gaussian_v2_signal.py`)
- Same as Gaussian but 30-day window, lower threshold (0.5)
- **Directional change from yesterday**

### Goldilocks (`goldilocks_signal.py`)
- Spike detection: yesterday vs day-before-yesterday
- Spike up → `'down'` (reversion). Spike down → `'up'` (reversion)
- **Directional change from yesterday**

### Persistence (`persistence_signal.py`)
- Compare yesterday's high vs day-before-yesterday's high
- If yesterday was warmer: predict `'up'` (same direction continues)
- **Directional change from yesterday**

### Forecast Disagreement (`forecast_disagreement_signal.py`)
- Compare yesterday's high vs 7-day rolling mean
- If yesterday was warmer: predict `'down'` (reversion to mean)
- **Directional change from yesterday**

### Pressure Delta (`pressure_delta_signal.py`)
- Exponentially-weighted pressure change (72h vs 3 days ago)
- Rising pressure → `'up'` (warming). Falling pressure → `'down'` (cooling)
- **Directional change**

### Corrected Pressure Delta (`dual_polarity_signal.py`)
- Same but physics-corrected: rising pressure → `'down'` (cooling)
- **Directional change**

### Wind Direction Shift (`wind_direction_shift.py`)
- Shift to southerly → `'up'` (warming). Shift to northerly → `'down'` (cooling)
- **Directional change**

### Frontal Detector (`frontal_detector_signal.py`)
- Warm front → `'up'`. Cold front → `'down'`
- **Directional change**

### Temperature Advection (`temperature_advection_signal.py`)
- GFS 850mb advection: positive → `'up'` (warming). negative → `'down'` (cooling)
- Returns `(None, 0.0)` in backtest (no historical GFS data)
- **Directional change in theory**

### Seasonal Regime (`dual_polarity_signal.py`)
- Returns regime name as "direction" (e.g., `'deep_winter'`, `'warm_season'`)
- **Does NOT return 'up'/'down'** — incompatible with backtest comparison

### NWP Direct (`nwp_direct_signal.py`)
- Compares today's NWP forecast vs tomorrow's NWP forecast
- Today colder than tomorrow → `'up'`. Today warmer than tomorrow → `'down'`
- **Directional change from yesterday**

### Other signals (FOGR, METAR dT/dt, HRRR, etc.)
- All signals in the ensemble implement the BaseSignal `evaluate(idx, days)` interface
- All compare current/recent observations to historical norms and predict reversion
- **All predict directional change (mean reversion or persistence of direction)**

---

## 3. Backtest Validation Comparison

### Phase 8 (correct — ~67% accuracy)
File: `scripts/phase8_combinatorial_search.py`, lines 91-115

```python
def load_market(conn, station, market_type='HIGH'):
    prev = None
    for date_str, bucket in rows:
        if prev is not None and bucket is not None:
            market[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
        else:
            market[date_str] = 'flat'
        prev = bucket
    return market
```

Then `was_correct = pred_dir == actual` where `actual` = `'up'` if today's settlement > yesterday's settlement.

### Phase 9 (correct — ~67% accuracy)
File: `scripts/phase9_combinatorial_search.py`, line 356: same pattern.

### Phase 9 Calibrated Search (correct)
File: `scripts/phase9_calibrated_search.py`, line 205:
```python
actual_direction = days[idx + 1].get('market_dir', 'flat')
```
Also uses the day-over-day comparison.

### unified_backtest.py (BROKEN — ~27-39% accuracy)
File: `core/unified_backtest.py`, lines 254-271

```python
training_temps = [
    market[d['date']]['settlement_bucket']
    for d in days[:train_days]
    if d['date'] in market
]
strike = statistics.median(training_temps)
# ...
actual_direction = 'up' if actual['settlement_bucket'] > strike else 'down'
```

This compares settlement against a **fixed strike price** (median of training period), NOT against yesterday's settlement.

### Phase B Combinatorial Search (BROKEN)
File: `scripts/phaseB_combinatorial_search.py`, lines 137-147

Same strike-based logic as unified_backtest.py.

### Phase B Calibration Pipeline (BROKEN)
File: `scripts/phaseB_calibration_pipeline.py`, lines 87-97

Same strike-based logic — the isotonic regression calibrators are fitting to the wrong target.

---

## 4. Root Cause

The bug was introduced when the code was refactored from inline Phase 8/9 scripts to the unified `core/unified_backtest.py` module. The original Phase 8/9 scripts computed `actual_direction` as day-over-day change:

```python
# Phase 8: correct
market[date] = 'up' if bucket > prev_bucket else 'down'
```

But the unified backtest computed it as strike-level:

```python
# unified_backtest.py: wrong
strike = median(training_temps)
actual_direction = 'up' if settlement > strike else 'down'
```

The person who wrote the unified backtest incorrectly assumed the Kalshi market's price strike was the same as the directional comparison the signals were trained on.

---

## 5. Impact Assessment

| Metric | Phase 8/9 (correct) | Phase A/B (broken) |
|---|---|---|
| Reported accuracy | ~65-67% | ~27-39% |
| Agreement=1 trades | Many | Many |
| Agreement≥2 trades | Many | 0 |
| Agreement≥3 trades | Many | 0 |
| Sharpe ratio | Positive | ~0 or negative |
| Time wasted on calibration | None | Fitting to wrong target |

The calibration pipeline was also fitting isotonic regression models to the wrong target, meaning all Phase B calibration data is invalid.

---

## 6. Fix Applied

### Change 1: `core/unified_backtest.py` — `load_station_data()`

Added tracking of the previous day's settlement bucket in the market dictionary:

```python
market[r[0]] = {
    'settlement_bucket': r[1],
    'reversion': r[2] if r[2] is not None else 0,
    'prev_bucket': prev_bucket,  # NEW: yesterday's settlement
}
prev_bucket = r[1]
```

### Change 2: `core/unified_backtest.py` — `backtest_ensemble()` (the actual_direction computation)

Changed from strike-based to directional change:

```python
# OLD (broken):
strike = statistics.median(training_temps)
actual_direction = 'up' if actual['settlement_bucket'] > strike else 'down'

# NEW (correct):
prev_actual = market.get(prev_date)
if prev_actual is None or prev_actual['settlement_bucket'] is None:
    continue
actual_direction = 'up' if actual['settlement_bucket'] > prev_actual['settlement_bucket'] else 'down'
```

### Change 3: `scripts/phaseB_combinatorial_search.py`

Same fix as unified_backtest.py.

### Change 4: `scripts/phaseB_calibration_pipeline.py`

Same fix — calibration data collection now uses directional change.

---

## 7. Expected Results After Fix

- Accuracy should return to ~65-67% range (matching Phase 8/9)
- Agreement gates (agree=2, 3+) should produce meaningful trade counts
- Sharpe ratio should be positive
- Calibration will produce meaningful isotonic regression models

---

## 8. Remaining Concerns

1. **Seasonal Regime signal** — returns regime names as direction, not 'up'/'down'. This will cause issues in the ensemble vote. Needs separate handling or exclusion from backtests.

2. **Temperature Advection signal** — returns `(None, 0.0)` in backtest mode (no historical GFS grid data). It's a paper-trading-only signal.

3. **Strike-based accuracy** — The paper trading engine and production trading both need strike-based accuracy, NOT directional accuracy. A separate `strike_accuracy` metric should be added to track actual Kalshi market performance.

4. **Calibration data is invalidated** — All Phase B calibration results were fitted to the wrong target and must be regenerated.