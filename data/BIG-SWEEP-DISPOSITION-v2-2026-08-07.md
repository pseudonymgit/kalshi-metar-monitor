# Big Sweep — Final Disposition (2026-08-07, v2)

**Method:** Parallel sweep — 4 workers, 20K configs per signal (professionally allocated), 38 signals, **7 minutes total runtime**
**Config bounds:** kelly_fraction [0.1, 1.0], max_contracts 1000, fixed seed 42
**Calibration:** goldilocks + trajectory + metar_trend added to calibration targets

## ✅ ADVANCE — 6 Signals (Honest Metrics)

| Signal | Acc | Trades | Sharpe | $/Trade | Notes |
|--------|:---:|:------:|:-----:|:-------:|-------|
| **forecast_disagreement** | **66.3%** | 884 | 2.77 | $14 | ⭐ Best volume. Orthogonal. Reliable. |
| **metar_trend** | **64.7%** | 499 | 4.86 | $13 | Honest METAR trend (was fake 86.1% ECMWF). |
| **calendar_climatology** | **65.6%** | 337 | 2.76 | $13 | Reliable baseline. Converged fast. |
| **pressure_delta** | **63.4%** | 1,234 | 3.04 | $12 | Best volume. Most orthogonal (ρ=0.03). |
| **gaussian_v2** | **62.0%** | 909 | 10.77 | $154 | High Sharpe. P&L higher than ideal but honest. |
| **gaussian** | **59.7%** | 1,235 | 10.61 | $230 | Most volume. High confidence drives more contracts. |

## ⏸️ PARK — 2 Signals (Below 58% Gate)

| Signal | Acc | Trades | $/Trade | Notes |
|--------|:---:|:------:|:-------:|-------|
| wind_direction_shift | 48.4% | 31 | $5 | Below coin flip. |
| radiational_cooling | 0.0% | 0 | $0 | Previously 17.7% — not producing trades this run. |

## ⏸️ PARK — 29 Infrastructure-Blocked Signals

Same 26 as before, plus nwp_analog, nwp_direct, nwp_dtdt_fusion, pressure_tendency, radiational_cooling, regime, settlement_arbitrage, spread_based_entry, temperature_advection, volume_momentum — all need data pipelines rebuilt.

## ❌ KILL — Goldilocks & Spike Reversion (Merge)

Both produce identical results (72% acc, 15+ Sharpe, $294/trade). Only 18-35 trades — statistically meaningless. Merge into one lane signal.

## P&L Comparison: Before vs After Fix

| Signal | Before ($M) | After ($K) | Drop | Accurate? |
|--------|:----------:|:---------:|:----:|:---------:|
| gaussian | $1,052K | $284K | **-73%** | Still high ($230/trade) |
| gaussian_v2 | $936K | $140K | **-85%** | Better ($154/trade) |
| forecast_disagreement | $34K | $13K | **-62%** | Honest ($14/trade) |
| pressure_delta | $15K | $15K | **-0%** | Honest ($12/trade) |
| calendar_climatology | $3.5K | $4.2K | **+21%** | Honest ($13/trade) |

## The Honest Ensemble

**These 4 signals form a clean, honest ensemble:**
- forecast_disagreement (66.3%) — 884 trades
- metar_trend (64.7%) — 499 trades
- calendar_climatology (65.6%) — 337 trades
- pressure_delta (63.4%) — 1,234 trades

Combined: **~2,954 trades, ~64.9% avg accuracy, ~$39K total P&L, ~$13/trade.**