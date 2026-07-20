# Signal Audit Recommendations — 2026-07-20

## Summary

Total signals registered: 13
Broken at runtime: 1
NWP-dependent (no backtest): 3
Working in backtest: 12

## Critical Issues to Fix

### 1. Goldilocks — NameError
`today_high` is undefined (typo — should be `day_before_high`).
Fix: change line 144 from `if today_high is None` to `if day_before_high is None`.
This is a runtime crash, not just a logic error.

### 2. Wind Direction Shift — Look-ahead bias
`evaluate()` loop starts at `idx` (current day) instead of `idx-1` (yesterday).
Fix: change `range(idx, idx - self.lookback_days - 1, -1)` to `range(idx - 1, idx - self.lookback_days - 2, -1)`.

### 3. Temperature Advection — No backtest capability
Relies on live GFS API calls. No historical data cached. Blocked on ERA5 backfill.

### 4. evaluate_for_station() stubs for 3 signals
calendar_climatology, regime, forecast_disagreement all return (None, 0.0) unconditionally.
This prevents these signals from firing in DB-based deployments.
Fix: implement proper SQL queries, similar to pressure_delta or base_signal.

## Performance Summary (from Phase C)

| Signal | Accuracy | Trades (mean) | Sharpe | Coverage |
|--------|----------|---------------|--------|----------|
| wind_direction_shift      | 0.5886 |      447 | 2.9466 | 0.2608 |
| nwp_analog                | 0.0000 |        0 | 0.0000 | 0.0000 |
| persistence               | 0.5095 |     3030 | 0.3066 | 1.7664 |
| simple_trend              | 0.5095 |     3030 | 0.3066 | 1.7664 |
| gaussian                  | 0.6666 |     1300 | 5.6695 | 0.7578 |
| gaussian_v2               | 0.6395 |     2079 | 4.6359 | 1.2122 |
| pressure_delta            | 0.6074 |     1735 | 3.5183 | 1.0113 |
| regime                    | 0.2412 |        4 | 1.5953 | 0.0024 |
| forecast_disagreement     | 0.6495 |     1278 | 5.0145 | 0.7449 |
| calendar_climatology      | 0.6935 |      690 | 6.7544 | 0.4021 |
| temperature_advection     | 0.0000 |        0 | 0.0000 | 0.0000 |
| frontal_detector          | 0.4669 |     1510 | -1.0589 | 0.8802 |

## Calibration Impact (from Phase B)

Walk-forward isotonic calibration: 180d train, 30d test.
Signals where calibration degrades performance are flagged.
| Signal | Raw Acc | Calibrated Acc | Brier | ECE |
|--------|---------|----------------|-------|-----|
| wind_direction_shift      | 0.5994 | 0.5994 | 0.2837 | 0.1912 |
| nwp_analog                | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| persistence               | 0.5094 | 0.5094 | 0.2938 | 0.2095 |
| simple_trend              | 0.5094 | 0.5094 | 0.2619 | 0.1095 |
| gaussian                  | 0.6638 | 0.6638 | 0.3334 | 0.0000 |
| gaussian_v2               | 0.6390 | 0.6390 | 0.3054 | 0.0685 |
| pressure_delta            | 0.6099 | 0.6099 | 0.2676 | 0.1719 |
| regime                    | 0.7561 | 0.7561 | 0.1810 | 0.2067 |
| forecast_disagreement     | 0.6453 | 0.6453 | 0.2536 | 0.1430 |
| calendar_climatology      | 0.6880 | 0.6880 | 0.2258 | 0.1152 |
| temperature_advection     | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| frontal_detector          | 0.4628 | 0.4628 | 0.2888 | 0.1435 |

Note: Calibration accuracy = raw accuracy here (walk-forward prediction accuracy).
Full isotonic calibration would adjust confidence values, not direction.

## Agreement Layer Findings (from Phase D)

- 2_of_9: acc=0.5399, trades=1530
- 3_of_9: acc=0.6388, trades=1066
- 4_of_9: acc=0.7150, trades=586
- 5_of_9: acc=0.7674, trades=258
- 6_of_9: acc=0.7808, trades=73
- 7_of_9: acc=0.8519, trades=27

- Conviction > 0.5: acc=0.6539, trades=1020
- Conviction > 0.6: acc=0.6970, trades=660
- Conviction > 0.7: acc=0.7550, trades=151
- Conviction > 0.8: acc=0.0000, trades=0

## Correlation Redundancy (from Phase E)

Redundant pairs (r > 0.75): 7
- persistence <-> simple_trend: r = 1.0
- gaussian <-> gaussian_v2: r = 1.0
- gaussian <-> calendar_climatology: r = 1.0
- gaussian_v2 <-> calendar_climatology: r = 1.0
- forecast_disagreement <-> calendar_climatology: r = 0.9981
- gaussian <-> forecast_disagreement: r = 0.9887
- gaussian_v2 <-> forecast_disagreement: r = 0.9736

Orthogonal pairs (|r| < 0.2): 40
- wind_direction_shift <-> nwp_analog: r = 0.0
- wind_direction_shift <-> persistence: r = 0.0189
- wind_direction_shift <-> simple_trend: r = 0.0189
- wind_direction_shift <-> gaussian: r = 0.004
- wind_direction_shift <-> gaussian_v2: r = -0.0023
- wind_direction_shift <-> regime: r = 0.0
- wind_direction_shift <-> forecast_disagreement: r = 0.0183
- wind_direction_shift <-> calendar_climatology: r = 0.0414
- wind_direction_shift <-> temperature_advection: r = 0.0
- wind_direction_shift <-> frontal_detector: r = -0.0664
- nwp_analog <-> persistence: r = 0.0
- nwp_analog <-> simple_trend: r = 0.0
- nwp_analog <-> gaussian: r = 0.0
- nwp_analog <-> gaussian_v2: r = 0.0
- nwp_analog <-> pressure_delta: r = 0.0
- nwp_analog <-> regime: r = 0.0
- nwp_analog <-> forecast_disagreement: r = 0.0
- nwp_analog <-> calendar_climatology: r = 0.0
- nwp_analog <-> temperature_advection: r = 0.0
- nwp_analog <-> frontal_detector: r = 0.0
- persistence <-> pressure_delta: r = 0.0907
- persistence <-> regime: r = 0.0
- persistence <-> temperature_advection: r = 0.0
- simple_trend <-> pressure_delta: r = 0.0907
- simple_trend <-> regime: r = 0.0
- simple_trend <-> temperature_advection: r = 0.0
- gaussian <-> regime: r = 0.0
- gaussian <-> temperature_advection: r = 0.0
- gaussian_v2 <-> regime: r = 0.0
- gaussian_v2 <-> temperature_advection: r = 0.0
- pressure_delta <-> regime: r = 0.0
- pressure_delta <-> temperature_advection: r = 0.0
- pressure_delta <-> frontal_detector: r = -0.1156
- regime <-> forecast_disagreement: r = 0.0
- regime <-> calendar_climatology: r = 0.0
- regime <-> temperature_advection: r = 0.0
- regime <-> frontal_detector: r = 0.0
- forecast_disagreement <-> temperature_advection: r = 0.0
- calendar_climatology <-> temperature_advection: r = 0.0
- temperature_advection <-> frontal_detector: r = 0.0

## Recommendations

### Keep (has edge)
- calendar_climatology: 63.2% standalone accuracy confirmed.
- persistence: Simple baseline, useful for ensemble comparison.
- pressure_delta: Physically grounded, good orthogonal signal.

### Investigate (inconclusive)
- gaussian / gaussian_v2: Z-score reversion may overlap with calendar_climatology.
- forecast_disagreement: Conceptually sound but needs evaluate_for_station fix.
- nwp_analog: Promising (k-NN with NWP), needs more NWP data.

### Drop / Merge (redundant if high correlation)
- simple_trend: Highly correlated with persistence (same logic).
- regime: Marked as dead, kept for experimentation but low signal count.
- gaussian_v2: Overlaps with gaussian (different window, same math).

### Blocked (fix first)
- goldilocks: NameError, cannot run. Fix the typo.
- wind_direction_shift: Look-ahead bias, must fix before trusting results.
- temperature_advection: Needs ERA5 backfill for historical testing.
- calendar_climatology/regime/forecast_disagreement: Fix evaluate_for_station().
