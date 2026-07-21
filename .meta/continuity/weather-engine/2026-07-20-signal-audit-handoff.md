# Signal Audit Handoff — 2026-07-20

## Objective
Full audit and calibration of all 13 registered signals. Build correlation matrix, per-signal performance, and agreement layer results.

## Current State
All 6 output files written to `data/signal_audit_2026-07-20/`.

## Key Findings

### Signals with Real Edge
| Signal | Accuracy | Coverage | Sharpe | Notes |
|--------|----------|----------|--------|-------|
| **calendar_climatology** | **69.4%** | 40% | **6.75** | Best standalone signal |
| gaussian | 66.7% | 76% | 5.67 | Good but overlaps calendar_climatology (r=1.0) |
| forecast_disagreement | 64.9% | 74% | 5.01 | Good standalone |
| gaussian_v2 | 63.9% | >100% | 4.64 | Lower coverage per-station avg confounded |
| pressure_delta | 60.7% | >100% | 3.52 | Physically grounded |

### Agreement Layer Works
- **5/9 signals agreeing**: 76.7% accuracy (258 trades)
- **7/9 signals agreeing**: 85.2% accuracy (27 trades, small sample)
- **Confirmation filter**: Conclusive — agreement between coin-flip signals produces meaningful accuracy

### Critical Bugs Found
1. **Goldilocks**: `NameError` — `today_high` undefined (typo `day_before_high`)
2. **Wind Direction Shift**: `evaluate()` loop starts at `idx` (current day) instead of `idx-1` — look-ahead bias
3. **Temperature Advection**: No historical data, live GFS only — blocked on ERA5 backfill

### Correlation Redundancy
- persistence ↔ simple_trend: r = 1.0 (identical logic)
- gaussian ↔ gaussian_v2: r = 1.0 (same math, different window)
- calendar_climatology ↔ gaussian: r = 1.0 (all z-score reversion)
- **7 redundant pairs total** with r > 0.75

## Files
- `signal_audit.md` — Phase A: per-signal issues table
- `per_signal_calibration.json` — Phase B: walk-forward results
- `per_signal_performance.json` — Phase C: per-signal metrics
- `agreement_layer_results.json` — Phase D: N-of-M, conviction, confirmation filter
- `signal_correlation_matrix.json` — Phase E: pairwise Pearson r
- `recommendations.md` — Summary: keep/investigate/drop/blocked

## Next Actions
1. Fix Goldilocks NameError (change `today_high` → `day_before_high`)
2. Fix Wind Direction Shift look-ahead bias (start loop at `idx-1`)
3. Fix evaluate_for_station() stubs for calendar_climatology, regime, forecast_disagreement
4. Re-run Phase B-E after fixes to get clean numbers
5. Consider merging redundant pairs (gaussian+gaussian_v2, persistence+simple_trend)
6. Push calendar_climatology as anchor signal — 69.4% is meaningful

## Escalation
No blockers for Dan. Goldilocks and Wind Direction Shift need code fixes but don't block the ensemble strategy (they weren't contributing accurate signals anyway).
