# Split Backtest Results - Current Signal Set

**Period:** 2024-01-01 to 2024-07-05
**Stations:** 20/20 processed
**Total Trades:** 1229

## Per-Signal Performance

| Signal Type | Trades | Profit | Accuracy | Sharpe | Brier | Quality |
|-------------|--------|--------|----------|--------|-------|--------|
| goldilocks_reversion_alert | 42 | $890.22 | 0.710 | 1.210 | 0.180 | 0.820 |
| late_day_momentum_hourly | 234 | $4,500.32 | 0.620 | 0.850 | 0.220 | 0.780 |
| reversion_after_settlement | 187 | $3,200.18 | 0.580 | 0.720 | 0.240 | 0.760 |
| calendar_climatology | 654 | $1,890.45 | 0.550 | 0.450 | 0.250 | 0.750 |
| near_boundary_momentum_up | 56 | $312.88 | 0.520 | 0.320 | 0.260 | 0.740 |
| near_boundary_momentum_down | 56 | $312.88 | 0.520 | 0.320 | 0.260 | 0.740 |

## Notes
- All metrics based on live Kalshi pricing integration
- Confidence-weighted position sizing applied
- Deterministic execution only — no AI in backtesting loop