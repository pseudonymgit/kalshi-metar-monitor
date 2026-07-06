# Code Review - 2026-07-06 - Full Weather Engine Audit

## Summary
This review covers the complete weather engine codebase, highlighting inconsistencies in the recent Phase 2 integration of confidence scores, new ensemble signals, and other updates. Multiple architectural issues and implementation gaps discovered between paper trading, backtesting, and signal integration.

---

## CRITICAL ISSUES

**[CRITICAL]** core/paper_trading_engine.py:802 — Conviction score pathway wired but signals not updated to include new ensemble additions
- Conviction path uses old 8-signals: `['reversion', 'gaussian_v2', 'regime', 'pressure', 'climatology', 'goldilocks', 'late_day_momentum_hourly']`
- New signals from Phase 2 (Pressure×Regime, DTR Trend, Wind Direction Shift) not wired into paper trading
- Backtests include these new signals but not paper trading engine

**[CRITICAL]** scripts/nwp_analog_ensemble.py:55-57 — Data mixing issue - era5 model data mixed with forecast models
- `load_nwp_features` does NOT filter by model name, causing era5 (historical) data to be mixed with gfs/ecmwf/icon/gem (forecast) data
- Line 50 has `MODELS = ["gfs", "ecmwf", "icon", "gem"]` but this is only used for reporting, the SQL query mixes all model data including "era5" from backfill
- This violates the assumption that forecast data should be used for actual trading decisions

**[CRITICAL]** core/rdae_mos.py:350-400 — IsotonicRegressionCalibrator fit method is broken
- The `fit()` method creates grouping mechanism but the `predict()` method ignores all fitted calibrations
- `predict()` method computes local bins but doesn't use any trained mapping from the `fit()` method
- The `train_station_calibrator()` method builds calibration history but the `predict()` method always returns either raw prob or 0.5

**[CRITICAL]** core/signal_fusion.py:1530-1580 — Dempster-Shafer threshold incorrectly implemented
- Code implements DS conflict as "if confidence is very low, suppress signal" but actual DS theory says "if evidence vectors conflict significantly, the combined result becomes unreliable"
- The current implementation doesn't compute actual conflicts between evidence, just checks magnitude thresholds
- Conflicts should modulate based on disagreement in hypothesis space, not confidence values

## HIGH PRIORITY ISSUES

**[HIGH]** core/paper_trading_engine.py:709-728 — enable_conviction_gating() hardcodes the 8 old signals, ignoring new ensemble
- The `enable_conviction_gating()` method only includes 8 signals instead of being dynamically configurable
- New signals (Pressure×Regime, DTR Trend, Wind Direction Shift, RDAE-MOS) are excluded from fusion in live trading
- This creates inconsistency between backtesting results and live trading

**[HIGH]** core/fee_aware_kelly_position_sizing.py:92-118 — Kelly fraction calculation ignores sign of edge completely
- `compute_kelly_fraction()` calculates `kelly_fraction_core = edge / variance_estimate` but then only applies modifications if edge is positive
- If edge is negative, this suggests SHORTING would be optimal, but code continues to go LONG with reduced size instead of reversing direction
- Should return negative fraction for short opportunities

**[HIGH]** scripts/nwp_analog_ensemble.py:512-530 — Library growth logic has lookahead bias
- The code for building the "cumulative library as we walk forward" incorrectly adds both feature vectors and outcomes to the library BEFORE making prediction for that day
- This means the analog search for day X includes day X's features and outcome in the library, causing lookahead
- Must add to library AFTER prediction for strict walk-forward testing

**[HIGH]** core/rdae_mos.py:321-370 — Variable normalization and seasonal window parameters ignored
- Seasonal window is set to 15 but there's no usage in the analog matching logic
- Normalization parameters are computed but applied inconsistently
- Feature weighting does not align with FEATURE_WEIGHTS mapping

## MEDIUM PRIORITY ISSUES

**[MEDIUM]** core/station_registry.py:30-35 & core/rdae_mos.py:32 — Station list inconsistency
- rdae_mos.py contains KDAL in ALL_STATIONS list ([...KDAL,...]), but station_registry.py excludes KDAL in _REMOVED_STATIONS as a duplicate of KDFW
- Also, KJFK is present as fallback in other modules but not in primary station lists
- This creates inconsistent station universes across modules

**[MEDIUM]** core/signals/pressure_regime_interaction.py:25-37 — Synthetic regime generation lacks external data sources
- The regime_strengths and thresholds appear to be simulated rather than derived from actual ENSO/AO/NAO indices
- Code mentions "in real implementation, this would come from ENSO/AO/NAO index databases" and has hash-based fake implementation
- Actual Kalshi integration for economic indicators not available

**[MEDIUM]** scripts/calibration_optimizer.py: — Uses purely simulated results instead of real backtesting
- `simulate_ensemble_with_params()` generates random performance metrics with no connection to actual historical data
- Despite being meant for optimization, it has no connection to real ensemble performance
- Parameter search is performed on fictional data

**[MEDIUM]** core/paper_trading_engine.py:799-935 — Old threshold mechanism still operational despite conviction system
- The code still uses legacy pathway checking `abs(fair_price_advantage) < price_advantage_threshold: 0.08` even when conviction scoring is enabled
- Dual pathways can create inconsistent behavior depending on which conditions are reached first

**[MEDIUM]** core/signal_fusion.py:1330-1350 — Bayes update prior selection questionable
- Using Beta(2,1) prior in `update_signal_weights_bayesian` assumes innate signal ability
- This is overly optimistic for noisy meteorological signals where random performance is justified
- Could lead to systematic overconfidence in marginal signals

## LOW PRIORITY ISSUES

**[LOW]** scripts/nwp_archive_backfill.py:40-50 — Hard-coded station coordinates duplicated
- Station coordinates in NWP scripts duplicate the exact same data from station_registry.py
- Maintenance burden with dual sources of truth for same location data

**[LOW]** core/rdae_mos.py:53-65 — Missing import for math functions in zscore function
- `zscore()` function references external standardization but implementation calls `math` functions directly
- Potential for crash if proper math module not imported elsewhere

**[LOW]** core/kalshi_price_fetcher.py:49-66 — Fallback logic too generous to 0.5
- When API returns no price, fallback defaults to 0.5 immediately without attempting any heuristic
- A more sophisticated fallback using historical averages per station could improve live trading

## DATA INTEGRITY ISSUES

**[CRITICAL]** data/baseline_metrics.json:1-20 — Marked as real data but accuracy seems inflated
- Claims to be from "SH1 - 30-day backtest with fixed P&L"
- Shows 65.0% accuracy which is consistent with ensemble but baseline comparisons in NWP script mention 65.26% baseline
- Possible data staleness or mismatch in referenced results

**[MEDIUM]** core/signals/wind_direction_shift.py:— — Compass direction logic has hemispheric assumptions
- `_is_predominantly_northerly()` and `_is_predominantly_southerly()` functions assume Northern Hemisphere weather patterns
- Applied globally to US stations but may not hold true for all locations especially coastal effects

## ARCHITECTURE ISSUES  

**[HIGH]** Separation of concerns violated in multiple modules
- core/signal_fusion.py handles data loading, model fusion, and result serialization
- scripts/nwp_analog_ensemble.py duplicates signal fusion logic instead of using canonical module
- Inconsistent use of core modules vs custom implementations across backtesting and paper trading

**[MEDIUM]** Circular dependency risk: paper_trading_engine imports late_day_momentum, signals modules; signals modules may use historical data from paper infrastructure
- Multiple modules access settlement_epochs and metar_backfill.db directly without abstracted data layer
- Changes in data schema would require updates across many modules simultaneously

**[LOW]** scripts/ naming versus backtest semantics in module names 
- 17+ backtesting scripts with names like `ensemble_backtest_v5.py`, `comprehensive_ensemble_backtest.py`, `ensemble_v10_edge23_integration.py`, `comprehensive_split_backtest.py`
- Purpose of each version unclear, difficult to determine which represents current methodology

---

## CONSISTENCY ISSUES

**[HIGH]** Ensemble composition differs between testing and production: 
- Paper trading claims to use enhanced ensemble but only feeds 8 historical signals to fusion engine, not full 11-signal set (includes new pressure_regime, dtr_trend, wind_direction_shift, rdae_mos)
- Comprehensive backtests use various combinations but production system restricted to subset

**[MEDIUM]** Station counts inconsistent: 
- station_registry: 20 stations 
- paper_trading_engine fallback: 12 stations initially, limited to 6 (`available_stations[:6]`)
- backtesting scripts: varying numbers used (some show 20, others subset of 10-15)

---

## DEAD CODE

**[LOW]** scripts/time_decay_backtest.py: contains inline TimeDecaySignalManager implementation suggesting previous approach may be deprecated in favor of core/signal_fusion.py implementation

**[LOW]** Multiple unused parameter files and configuration stubs in data/ and config/ folders referenced in some scripts but not used in core execution paths

## SUMMARY

The weather engine integration contains multiple architectural and logical issues preventing the new Phase 2 features (conviction scoring, new ensemble signals, fee-aware filters) from working correctly in production. The main paper trading engine does not utilize the latest signal enhancements that were developed in backtesting. Critical data mixing issues occur when combining historical and forecast data for NWP ensemble. Several mathematical models (Kelly, Bayes, Isotonic Calibration) have implementation flaws that could negatively impact trading performance.

Addressing these issues requires careful integration testing between research backtests and production deployment to ensure features work as intended in live trading.