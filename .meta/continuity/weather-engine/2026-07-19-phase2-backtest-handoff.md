# Phase 2.4 Backtest Handoff — Temperature Advection (Signal 6) Validation

**Date:** 2026-07-19
**Script:** `scripts/run_phase2_backtest.py`
**Results:** `data/phase2_backtest_results.json`
**Previous:** Phase 2.3 (predecessor subagent consumed all tokens, no output)
**Next:** Phase 2.5 — Historical NWP reanalysis backfill for advection data

---

## Objective

Validate the 850-mb Temperature Advection signal (Signal 6) against current baselines. Run 5 experiments: standalone, full ensemble, best Phase 6 combo + Signal 6, walk-forward, and impact analysis.

## Current State

### Key Findings

1. **Temperature Advection is LIVE-ONLY** — Signal 6 requires real-time GFS 850-mb forecast data. The `evaluate()` method (BaseSignal interface) is a stub returning `(None, 0.0)`. Only `evaluate_for_station()` works — and it requires live GFS API calls. Historical backtest is impossible without stored NWP data.

2. **Stored NWP data is insufficient** — Only 19 advection records exist in `nwp_forecasts.db` (across 19 stations, all from recent live GFS fetches). No historical data is available for backtesting.

3. **Full ensemble baseline established** (all 12 non-advection signals):
   - 33,888 trades across 20 stations
   - 0.5541 accuracy
   - 3.6608 avg Sharpe ratio
   - 0.9959+ coverage (signals fire on nearly every day)

4. **Best Phase 6 combo validated** (`calendar_climatology+gaussian+pressure_delta+forecast_disagreement`):
   - 27,473 trades
   - 0.5719 accuracy
   - Adding Signal 6 has zero effect (doesn't fire on historical data)

5. **Walk-forward validation** (4 time splits + holdout):
   - Mean accuracy: 0.5485
   - Range: 0.5369 – 0.5595
   - Std: 0.0104 (low variance = stable)
   - No evidence of overfitting or look-ahead bias

6. **Calendar Climatology alone** is the strongest single signal at 0.6317 accuracy (7,364 trades).

### Files Created

| File | Size | Purpose |
|---|---|---|
| `scripts/run_phase2_backtest.py` | ~29 KB | Backtest script with all 5 experiments |
| `data/phase2_backtest_results.json` | ~117 KB | Full results with per-station breakdowns |

## Next Actions

1. **Phase 2.5** — Implement historical NWP reanalysis backfill for advection data. Backfill GFS 850-mb temperature advection for all 20 cities using ERA5 reanalysis or Open-Meteo historical GFS archive.

2. **Collect daily GFS data** — Deploy a daily cron job to fetch GFS 850-mb fields for all 20 cities and store in `nwp_forecasts.db`. Target: ≥30 days of history for rolling std normalization.

3. **Re-run standalone backtest** — Once ≥30 days of advection data exists, re-run Experiment 1 (standalone Signal 6) to measure directional accuracy.

4. **Live performance tracking** — Monitor Signal 6's directional accuracy in production paper trading. Compare against the 70-75% theoretical accuracy from the Gray Room meteorology literature.

5. **Consider signal integration** — If live performance exceeds 60% directional accuracy, add `temperature_advection` to the production ensemble with equal weight.

## Stop Conditions

- **Escalate if:** Signal 6 live performance falls below 50% directional accuracy after 60 days of observation
- **Escalate if:** Phase 2.5 backfill reveals technical issues with reanalysis data architecture
- **Stop if:** Signal 6 shows negative correlation with the ensemble (indicates data quality issue)

## Escalation Path

- **Technical (Gilfoyle):** NWP data collection, backfill architecture, GFS API issues
- **Strategic (Gerri):** Whether to invest in historical NWP reanalysis backfill vs. focus on live signal collection
- **Financial (Marty):** Cost-benefit of adding Signal 6 to production ensemble

## Files Referenced

- `prototypes/weather-engine-source/core/signals/temperature_advection_signal.py` — Signal 6 implementation
- `prototypes/weather-engine-source/core/signals/__init__.py` — SignalRegistry with Signal 6 registered
- `prototypes/weather-engine-source/data/phase6_combinatorial_search.json` — Phase 6 best combo source
- `prototypes/weather-engine-source/data/phase2_backtest_results.json` — This run's output
- `prototypes/weather-engine-source/data/nwp_forecasts.db` — NWP forecast data store (19 advection records)