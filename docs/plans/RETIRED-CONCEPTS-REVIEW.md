# Retired / Parked Module Concepts Review

**Date:** 2026-08-11  
**Context:** Weather engine switching from P3 production pipeline (32.9% accuracy) to SignalRegistry backtest pipeline (13 signals, 57.76% unified, 73.03% best config).  
**Mandate:** Surviving pipeline is B-mode compliant (deterministic scripts, no AI/ML in the signal loop).  
**Author:** Subagent weather-trading-systems-review  

---

## Executive Summary

**23 modules reviewed.** Of these:

| Disposition | Count | Modules |
|---|---|---|
| **EXTRACT** — worth pulling into SignalRegistry pipeline | 6 | execution_simulator, strike_selector, settlement_execution_gate, spatial_coherence, luck_elimination, leverage_manager (selected functions) |
| **EXPLORE** — worth evaluating but not committing yet | 4 | kalman_temperature_tracker, forecast_confidence_modulator, dual_hypothesis_engine, order_book_baseline |
| **SKIP** — parked for good reasons | 8 | rdae_mos, bma_emos, walk_forward_optimizer, ensemble_fraction, ensemble_diversity, seasonal_diurnal_curve, enso_regime_bias, fee_aware_kelly_position_sizing |
| **ALREADY_COVERED** — concept exists in SignalRegistry | 5 | ensemble_agreement (3-of-4 gate), conviction, forecast_disagreement, fee_aware_filter, adaptive_thresholds |

**Overall: 6 modules with extractable code, 4 worth exploring further, rest are duplicates, superseded, or not B-mode compliant.**

---

## HIGH PRIORITY MODULES

### core/rdae_mos.py — Regime-Dependent Analog Ensemble + MOS
- **Status:** SKIP
- **Summary:** Regime-dependent analog ensemble using METAR variables as predictors, with isotonic regression MOS calibration. Targets ≥60% standalone accuracy. Currently only stage 1 (METAR-only) implemented.
- **Contains useful code?** Yes — the isotonic regression MOS calibration and regime classifier logic are well-engineered. But the entire concept (analog ensemble + MOS) is a heavyweight approach that competes with — rather than complements — the existing SignalRegistry signals.
- **B-mode compliant?** Yes — pure numpy/scipy, no AI/ML. But uses scipy.optimize.minimize_scalar which adds a non-trivial dependency.
- **Integration effort:** High — this would need to become signal #14. The METAR-only limitation means it's incomplete; it needs NWP integration (stage 2) to reach its ≥60% target. The analog retrieval is computationally expensive (scans full history per prediction).
- **Recommendation:** SKIP. The concept is promising but incomplete and heavy. The SignalRegistry already achieves 57-73% without it. If future work revisits analog methods, use this as a reference implementation — don't re-integrate.

### core/bma_emos.py — Bayesian Model Averaging + EMOS
- **Status:** SKIP
- **Summary:** Bayesian Model Averaging (BMA) and Ensemble Model Output Statistics (EMOS) for ensemble calibration. Loads pre-computed parameters from a training pipeline. Uses station clustering and empirical Bayes shrinkage.
- **Contains useful code?** Yes — the `bma_calibrate()`, `emos_calibrate()`, and `calibrate_ensemble()` functions are well-structured. However, this is a statistical post-processing layer for ensemble forecasts, not a signal.
- **B-mode compliant?** Yes — pure numpy/scipy.
- **Integration effort:** Medium — would require a training pipeline (scripts/train_bma_emos.py) to produce the parameter cache, then wire into the SignalRegistry's calibration layer. Since the SignalRegistry relies on walk-forward calibration, adding BMA/EMOS would mean maintaining two calibration systems.
- **Recommendation:** SKIP. The SignalRegistry's existing calibration (p3_calibration_engine) already handles probability calibration via isotonic regression. BMA/EMOS would duplicate this with more complexity and a training pipeline dependency. If the SignalRegistry calibration proves insufficient, revisit this as an alternative.

### core/walk_forward_optimizer.py — Walk-Forward Threshold Optimizer
- **Status:** SKIP
- **Summary:** Optimizes confidence thresholds per regime (DJF vs JJA) using sklearn TimeSeriesSplit walk-forward validation. Designed to fix "inverted optimization problem" where unified thresholds hurt performance across seasons.
- **Contains useful code?** Yes — the regime-aware threshold optimization logic. But it depends on pandas and sklearn.
- **B-mode compliant?** Yes.
- **Integration effort:** Medium — the unified_backtest pipeline already has its own walk-forward implementation. This module focuses on regime-specific *threshold* optimization, which is a different concern. The existing `adaptive_thresholds.py` handles this better with Bayesian Beta-Bernoulli posteriors.
- **Recommendation:** SKIP. The SignalRegistry already uses walk-forward validation. Regime-specific thresholds are handled by AdaptiveThresholdRegistry (Beta-Bernoulli), which is more principled than the grid-search approach here.

### core/execution_simulator.py — Monte Carlo Execution Simulation
- **Status:** EXPLORE → EXTRACT
- **Summary:** Realistic execution simulator with Monte Carlo P&L ranges. Models spread widening (1.3x), market impact (square-root model), partial fills, and reports P&L percentiles (5th/50th/95th). 1000 scenario Monte Carlo per trade.
- **Contains useful code?** Yes — `ExecutionSimulator.simulate_trade()`, `MonteCarloResult` dataclass, `TradeSimulationResult` dataclass. The market impact model (`impact ∝ sqrt(size/depth)`) and spread widening factor are reusable.
- **B-mode compliant?** Yes — purely deterministic math and random seed. No AI/ML.
- **Integration effort:** Low — `ExecutionSimulator` is a standalone class with one dependency (`MARKET_COST_MODEL` from market_cost_model.py). It can be wired directly into the trade evaluation pipeline to provide realistic edge estimates that account for execution friction.
- **Recommendation:** EXTRACT into the SignalRegistry pipeline as a pre-trade validation step. Before placing a backtest trade, run it through the execution simulator. If the 5th-percentile P&L is negative despite positive model edge, the trade is too risky for the available liquidity. This would improve robustness without changing any signals.

### core/ensemble_fraction.py — Bias Correction + Fraction Computation
- **Status:** SKIP
- **Summary:** Loads pre-computed GEFS bias corrections from JSON, applies per-station seasonal bias corrections, and computes ensemble fraction (what fraction of ensemble members exceed a threshold).
- **Contains useful code?** `apply_bias_correction()` and `compute_ensemble_fraction()` are clean functions. But these are specific to the GEFS ensemble pipeline, which is being retired.
- **B-mode compliant?** Yes.
- **Integration effort:** Low (extraction), but the concept is tied to the retired GEFS data pipeline.
- **Recommendation:** SKIP. The bias correction methodology (seasonally-varying per-station bias from JSON) is a useful pattern. If the SignalRegistry ever needs bias correction for NWP inputs, use this module as a reference. Don't re-integrate now — no GEFS data in the surviving pipeline.

### core/ensemble_diversity.py — Diversity Score
- **Status:** SKIP
- **Summary:** Computes diversity score from signal vote distribution. Penalizes confidence when all signals agree too often (indicating redundancy, not consensus).
- **Contains useful code?** The diversity formula (`1 - |2 * max_vote_share - 1|`) is interesting but trivial (10 lines).
- **B-mode compliant?** Yes.
- **Integration effort:** Low.
- **Recommendation:** SKIP. The concept is addressed differently in the SignalRegistry: signal fusion weights handle signal redundancy through mutual information weights (from information_theoretic_weighting.py). Adding a diversity penalty on top would double-count. Keep in mind for diagnosis only.

### core/ensemble_agreement.py — 3-of-4 Ensemble Agreement Gate
- **Status:** ALREADY COVERED
- **Summary:** Reads real NWP forecasts (GFS, ECMWF, ICON, GEM) from DB. Requires ≥3 of 4 models to agree on direction before emitting a signal. Noise filter, not a standalone signal.
- **Contains useful code?** `EnsembleAgreementGate.evaluate()` and the NWP DB query logic. But this concept is already in the SignalRegistry: `ForecastDisagreementSignal` and the ensemble confidence modulation layer.
- **B-mode compliant?** Yes.
- **Integration effort:** Already handled.
- **Recommendation:** ALREADY COVERED. The SignalRegistry's confidence modulation and signal fusion already account for multi-model agreement. No need to add a separate gate.

### core/strike_selector.py — Volume-Weighted Strike Selection
- **Status:** EXPLORE → EXTRACT
- **Summary:** Selects optimal strike price based on Kalshi API market depth and liquidity. Uses volume and spread metrics, not prediction. Caches order book data. Targets liquid strikes for trade execution.
- **Contains useful code?** Yes — `StrikeSelector.select_strike()` with volume/depth analysis, `_check_rate_limit()` reusable for Kalshi API calls, `_make_request()` with rate limit handling.
- **B-mode compliant?** Yes — purely API-based data selection.
- **Integration effort:** Low — self-contained with only `requests` as dependency. The rate limit tracker and order book cache are reusable components.
- **Recommendation:** EXTRACT into the execution layer. The trading pipeline needs a strike selection step before order submission. Current paper trading assumes a fixed strike; this module provides a principled way to select the best liquid one. Use it as the contract selection step in the production pipeline.

### core/contract_selection_optimizer.py — Multi-Contract Risk Optimization
- **Status:** EXPLORE
- **Summary:** When multiple contracts exist (D+1 and D+2), selects the best risk-adjusted edge. Considers spread tightness, time decay penalty, liquidity. Scores contracts with weighted criteria.
- **Contains useful code?** Yes — `ContractSelectionOptimizer.select_best_contract()`, `ContractCandidate` / `SelectionResult` dataclasses, scoring weights.
- **B-mode compliant?** Yes.
- **Integration effort:** Low.
- **Recommendation:** EXPLORE. Worth integrating alongside strike_selector for contract horizon selection. Currently the pipeline trades D+1 only; this module enables intelligent D+2 deployment. However, since the current backtest is D+1 only, defer until D+2 trading is added.

### core/settlement_cascade.py — Settlement Timing Analysis
- **Status:** SKIP (concept useful, code not)
- **Summary:** Predicts optimal exit/re-entry timing around settlement. Calculates cascade windows (90 min before settlement, re-entry afterward). Returns timing recommendations.
- **Contains useful code?** The cascade timing model (exit 90 min early, re-enter 30 min after) is a heuristic, not data-driven. The timing recommendation logic is ~50 lines of hardcoded thresholds.
- **B-mode compliant?** Yes.
- **Integration effort:** Low.
- **Recommendation:** SKIP as written. The concept of settlement timing management is valuable but the implementation is heuristic. If the SignalRegistry pipeline needs settlement timing, implement it with data-backed exit timing from the actual Kalshi settlement data, not hardcoded 90-min windows.

### core/settlement_execution_gate.py — Trade Gating Before Settlement
- **Status:** EXPLORE → EXTRACT
- **Summary:** Checks whether a settlement epoch is tradeable before signaling. Five gates: market open, settlement window, station cooldown, boundary cooldown, approved stations list.
- **Contains useful code?** Yes — `SettlementExecutionGate.evaluate()`, `GateResult` / `GateVerdict`, cooldown management, entry window calculation (T-18h to T-2h). Reusable gate pattern.
- **B-mode compliant?** Yes — deterministic checks only.
- **Integration effort:** Low — standalone class with datetime dependencies only.
- **Recommendation:** EXTRACT into the production trading pipeline. The gate pattern (pass/fail with structured verdicts) is exactly what's needed for the pre-trade validation step. Use it to prevent trades on weekends, outside trading windows, or when station-level cooldowns are active.

### core/settlement_fidelity.py — 5-min Station Bias Correction
- **Status:** EXPLORE
- **Summary:** Diagnoses divergence between METAR daily_stats and settlement_epochs. Applies 0.4°F correction for 5-minute reporting stations (KNYC Central Park). Tracks per-station bias vs Kalshi settlement.
- **Contains useful code?** Yes — `SettlementFidelity.run_diagnostic()` for detecting station bias, `apply_5min_bias_correction()` for the correction function.
- **B-mode compliant?** Yes.
- **Integration effort:** Low.
- **Recommendation:** EXPLORE. The 5-minute bias correction for KNYC is important — Central Park's ASOS reports at 5-minute intervals, capturing more extreme temps than hourly stations. This could cause systematic over/under-estimation. However, verify the 0.4°F magnitude against actual settlement data before adding to the pipeline. The diagnostic (run_diagnostic) is useful for ongoing quality monitoring.

### core/settlement_processor.py — Settlement Calculation
- **Status:** SKIP (stub)
- **Summary:** Extracted stub from paper_trading_engine.py. Process settlement calculation, epoch management. Currently has empty method bodies.
- **Contains useful code?** No — stubs only.
- **B-mode compliant?** N/A.
- **Integration effort:** Would require a full implementation.
- **Recommendation:** SKIP. This is a stub with no working code. The settlement processing logic lives in pnl_tracking.py (process_settlements_for_date), which has the correct payout calculation.

### core/pnl_tracking.py — Profit & Loss Tracking
- **Status:** EXPLORE
- **Summary:** Full P&L tracking with trade settlement, reconciliation, calibration metrics, Sharpe calculation, and reporting. Correctly computes HIGH/LOW payouts based on strike prices. Extracted from paper_trading_engine during monolith decomposition.
- **Contains useful code?** Yes — `process_settlements_for_date()` with correct payout logic, `daily_reconciliation()`, `calculate_calibration_metrics_for_date()`, `compute_sharpe()`, `generate_calibration_report()`.
- **B-mode compliant?** Yes.
- **Integration effort:** Medium — this module assumes a paper_trading_engine instance and SQLite schema. The payout logic is correct and worth preserving, but the full module is tightly coupled to the paper trading DB schema.
- **Recommendation:** EXPLORE. The correct payout calculation (strike price-based, not direction-based) is critical for accurate backtesting. If the SignalRegistry pipeline uses a different trade storage format, extract only the settlement math (payout calculation per market type). Do not re-integrate the full module without adapting to the new schema.

### core/luck_elimination.py — Skill vs Luck Testing
- **Status:** EXPLORE → EXTRACT
- **Summary:** Three statistical methods: Monte Carlo null distribution (date-shuffled accuracy floor), block bootstrap (autocorrelation-aware confidence intervals), luck-adjusted accuracy (observed minus null median). Used to answer: "is this signal actually skilled or just lucky?"
- **Contains useful code?** Yes — `null_distribution()`, `bootstrap_ci()`, `luck_adjusted_accuracy()`, `p_value()`. These are well-engineered statistical tools.
- **B-mode compliant?** Yes — pure numpy/random, no AI/ML.
- **Integration effort:** Medium — requires wiring dependencies (stations, signal eval function, metrics function) via `wire_dependencies()`. But this is a one-time setup.
- **Recommendation:** EXTRACT into the SignalRegistry evaluation framework. This is the only module that provides rigorous skill-vs-luck separation. Run it once after each SignalRegistry backtest to produce luck-adjusted accuracy metrics. The results would strengthen confidence in the 57.76% unified accuracy number.

### core/cost_utils.py — Cost Utilities
- **Status:** SKIP
- **Summary:** Simplified cost functions: `calculate_spread_cost()`, `get_cost()`, `adjust_cost_for_volume()`, `get_effective_cost_for_trade()`. All are thin wrappers around the spread math.
- **Contains useful code?** The spread cost calculations are correct but trivial.
- **B-mode compliant?** Yes.
- **Integration effort:** N/A.
- **Recommendation:** SKIP. The `market_cost_model.py` module (which IS in the surviving pipeline) already provides a single source of truth for costs (ROUND_TRIP_FEE = 0.0205, spread = 0.031, etc.). `cost_utils.py` duplicates this with less precision. Use MARKET_COST_MODEL instead.

---

## MEDIUM PRIORITY MODULES

### core/regime_switching_hmm.py — HMM Regime Detection
- **Status:** SKIP
- **Summary:** 3-5 state Gaussian HMM for regime detection using temperature variance, pressure tendency, humidity, wind, and cloud cover features. Trained on historical METAR data via hmmlearn.
- **Contains useful code?** Feature extraction (rolling volatility, pressure tendency) and regime probability smoothing. But the HMM training is the core value.
- **B-mode compliant?** NO — `hmmlearn.GaussianHMM` is a machine learning model (unsupervised learning on historical data to estimate transition/emission probabilities). This violates the "no AI/ML in the signal loop" constraint. The feature extraction code is reusable but not unique.
- **Integration effort:** High — would need a training pipeline and real-time inference integration.
- **Recommendation:** SKIP — not B-mode compliant. If regime modulation is needed, use deterministic regime definitions (calendar-based seasons + predefined features) instead of HMM. The feature extraction (rolling temp std, pressure tendency) can be used for deterministic regime definitions.

### core/kalman_temperature_tracker.py — Kalman Filter Temperature Tracking
- **Status:** EXPLORE
- **Summary:** Full Kalman filter implementation for live temperature tracking. State: [T, dT/dt]. Observed: METAR temps. Uses diurnal cycle as control input. Computes Gaussian settlement probability P(T > target). Outlier rejection, adaptive initialization via climate priors.
- **Contains useful code?** Yes — `KalmanTemperatureTracker` class is well-engineered (~500 lines). `predict()`, `update()`, `get_settlement_probability()`, `get_edge()` (edge calculation), `_estimate_diurnal_rate()`. Designed per TRAJECTORY-KALMAN-SPEC.md.
- **B-mode compliant?** Yes — pure deterministic math. No training, no ML.
- **Integration effort:** Medium — depends on `station_registry.py` and `metar_backfill.db` for climate priors. The tracker produces settlement probabilities that feed into the signal pipeline.
- **Recommendation:** EXPLORE strongly. This is arguably the most valuable module in the entire set. It provides the live temperature probability mechanism that the SignalRegistry pipeline currently lacks. The Kalman filter is:
  - B-mode compliant (deterministic math)
  - Already designed for live probability updates (not just backtesting)
  - Produces edge calculations (model-based fair value vs market price)
  - Has outlier rejection for bad METAR readings
  
  **To integrate:** Add as a signal confidence modifier. The Kalman filter's `get_settlement_probability()` can modulate signal confidence based on real-time METAR trajectory alignment. If the Kalman filter predicts T>85°F with high confidence and a signal also predicts UP, boost confidence. If they disagree, reduce.

### core/spatial_coherence.py — Spatial Correlation Verification
- **Status:** EXPLORE → EXTRACT
- **Summary:** Comprehensive spatial coherence verification system. Region-based clustering (NE/SE/SC/MW/RW/PAC), distance decay (Gaussian + decorrelation length), wind direction enhancement, altitude penalty, coastal handling, sea breeze detection. Produces confidence modulation factor M(Phi).
- **Contains useful code?** Yes — `SpatialCoherenceGate` class, `compute_spatial_weight()`, `modulate_confidence()`, `get_cluster_confirmation()`, sea breeze detection, station metadata with 20 stations.
- **B-mode compliant?** Yes — deterministic math only.
- **Integration effort:** Medium — requires station metadata and METAR wind direction data. The modulation factor feeds into signal confidence adjustment.
- **Recommendation:** EXTRACT as a confidence modulation layer. The spatial coherence system is well-designed and documented (cross-refs SPATIAL-COHERENCE-SPEC.md). Add it as a post-signal step in the SignalRegistry pipeline:
  1. For each station+date, check nearby station signals
  2. Compute spatial coherence Phi
  3. Modulate signal confidence up (if confirmed) or down (if isolated/contradicted)
  
  This is especially valuable for stations in dense clusters (NE, SC) where confirmation is meaningful, and would help avoid isolated false positives.

### core/forecast_disagreement.py — GFS vs NWS Disagreement Signal
- **Status:** ALREADY COVERED
- **Summary:** Forecast disagreement signal: when GFS and NWS disagree by >5°F, bet in GFS direction. Backtest uses proxies (yesterday's high vs 7-day mean). Live uses NWS API.
- **Contains useful code?** `forecast_disagreement_signal()`, live NWS API fetcher, walk-forward backtest.
- **B-mode compliant?** Yes.
- **Integration effort:** N/A.
- **Recommendation:** ALREADY COVERED. The SignalRegistry includes `ForecastDisagreementSignal` which implements the same logic. This module was the prototype for that signal. No need to re-integrate.

### core/conviction.py — Signal Agreement Score + Conviction
- **Status:** ALREADY COVERED
- **Summary:** Implements SH6 spec: Signal Agreement Score (SAS) computation, ECE-adjusted conviction scoring, regime fit assessment. Formula: `Conviction = |LLOP_prob - 0.5| × 2 × (0.3 + SAS) × (1 - ECE) × tanh(sample/50)`.
- **Contains useful code?** `ConvictionScorer` class with `compute_signal_agreement_score()`, `compute_calibration_state()`, `compute_conviction_score()`.
- **B-mode compliant?** Yes.
- **Integration effort:** N/A.
- **Recommendation:** ALREADY COVERED. The SignalRegistry's confidence scoring in p3_calibration_engine handles this. The SH6 formula is informative as a reference but the pipeline already implements conviction scoring through signal fusion weights.

### core/dual_hypothesis_engine.py — Spike Detection H1/H2
- **Status:** EXPLORE
- **Summary:** Evaluates METAR temperature spikes: H1 (transient → reversion, tradeable) vs H2 (structural → new daily extreme, not tradeable). Threshold: delta < 0.3°F = H1. Confidence computed with asymmetric parameters for up/down reversions.
- **Contains useful code?** Yes — `DualHypothesisEngine.evaluate()`, `evaluate_from_scratch()`, `SpikeHypothesis` enum, asymmetric confidence parameters derived from Gray Room R7-A1.
- **B-mode compliant?** Yes — purely deterministic thresholds.
- **Integration effort:** Low — standalone class with no external dependencies.
- **Recommendation:** EXPLORE. The spike detection concept (transient vs structural classification) is a unique methodology not present in the SignalRegistry. If real-time METAR monitoring is in scope, this module provides a principled way to:
  - Filter out false signals that coincide with transient spikes (structural spike → no edge)
  - Amplify signals that align with reversion expectations (transient spike → boost confidence)
  
  Integration would add ~30 lines to the real-time evaluation pipeline.

### core/seasonal_diurnal_curve.py — Expected Temp by Month+Hour
- **Status:** SKIP
- **Summary:** Pre-computed expected temperature for each (station, month, local_hour) combination from historical METAR data. Used as baseline for anomaly detection. Confidence modulation based on cloud cover and observation count.
- **Contains useful code?** `DiurnalCurveModel` class with `get_expected_temp()`, `get_anomaly()`, `get_anomaly_zscore()`, `get_confidence()`.
- **B-mode compliant?** Yes — lookup-based, no training.
- **Integration effort:** Low — depends on `seasonal_diurnal_curves.json` data file.
- **Recommendation:** SKIP. The signal pipeline already computes anomaly-based signals through the Gaussian model (`p3_gaussian.py`) and calendar climatology. The diurnal curve model provides more granular (hourly) baselines, but this level of granularity isn't needed for daily settlement contract trading (where we care about daily max, not hourly temp).

### core/enso_regime_bias.py — ENSO/PDO/NAO/AO Bias Adjustments
- **Status:** SKIP
- **Summary:** Per-city, per-season, per-regime temperature anomaly adjustments using climate indices (ONI, PDO, NAO, AO). Loads data from NOAA CPC text files. Applies adjustments to Gaussian model mean.
- **Contains useful code?** Climate index parsing functions (`parse_oni_data()`, `parse_pdo_data()`, etc.), `get_enso_adjustment()`, `get_regime_adjustment()`, regional grouping for small-sample fallback.
- **B-mode compliant?** Yes — deterministic lookup.
- **Integration effort:** Medium — requires climate index data files to be maintained and updated.
- **Recommendation:** SKIP. Large-scale climate modes (ENSO, NAO) operate on multi-month timescales and produce very small per-day adjustments (typically <1°F). The signal pipeline already captures these effects through its walk-forward training, which implicitly absorbs seasonal and interannual patterns. Adding explicit ENSO adjustments would create a risk of double-counting effects that are already captured in the training data.

### core/forecast_confidence_modulator.py — Multi-Model Agreement Modulation
- **Status:** EXPLORE
- **Summary:** Modulates ensemble probability using agreement across 4 deterministic models (GFS, ECMWF, ICON, GEM). Formula: `p_adj = 0.5 + (p_ens - 0.5) × agreement^β`. Includes bias correction per model-station pair, directional/magnitude agreement separation, and position sizing gate.
- **Contains useful code?** Yes — `ForecastConfidenceModulator` class, `modulate()` full pipeline, `_compute_directional_agreement()`, `_compute_magnitude_dispersion()`, NWP DB and GEFS fallback logic.
- **B-mode compliant?** Yes — deterministic math.
- **Integration effort:** Medium — depends on NWP forecast DB and pre-computed bias data. The GEFS fallback provides a path even without live NWP data.
- **Recommendation:** EXPLORE. This is a more sophisticated version of the ensemble agreement gate. The key innovation is not binary (agree/disagree) but continuous modulation with β exponent control. If the SignalRegistry's confidence calibration proves insufficient for live trading, this modulator provides a principled way to incorporate multi-model agreement. However, the current pipeline achieves 57-73% without it, so this is a future optimization, not an immediate need.

---

## LOW PRIORITY MODULES

### core/live_trader.py — Live Trading Engine
- **Status:** SKIP
- **Summary:** Live trading engine with real fills, kill switch validation, risk limits. Calls Kalshi API via order_manager for actual trade execution.
- **Contains useful code?** Kill switch check, risk limit check pattern. But tightly coupled to the retired paper_trading_engine.py.
- **Recommendation:** SKIP. Superseded by the paper_trading_engine in the SignalRegistry pipeline. The live trading integration will use a new execution layer, not this legacy code.

### core/live_trading_loop.py — Live Trading Loop (Stub)
- **Status:** SKIP
- **Summary:** Placeholder stub for live trading orchestration. Empty `run_cycle()` method. Designed for Phase D implementation.
- **Contains useful code?** No — stub only.
- **Recommendation:** SKIP. No working code.

### core/intraday_trading_loop.py — Intraday Trading
- **Status:** SKIP
- **Summary:** Separate intraday trading mode running hourly via cron. Four-stage sequential trigger architecture (A→B→C→D). Dual exit strategy with time/confidence decay. Spread-based entry avoidance.
- **Contains useful code?** Sequential trigger architecture and exit management logic. But this is designed for a different trading frequency.
- **Recommendation:** SKIP. The current focus is daily contracts settled at bucket hour. Intraday trading is a separate product not in scope. If intraday is revisited later, the sequential trigger architecture is a useful design pattern to reference.

### core/trading_modes.py — Paper/Live Mode Separation
- **Status:** SKIP
- **Summary:** Enum and config class separating paper mode (fake fills, no risk controls) from live mode (real fills, kill switch).
- **Contains useful code?** `TradingMode` enum, `TradingModeConfig` dataclass. Pattern is useful but trivial.
- **Recommendation:** SKIP. The surviving pipeline handles mode separation differently (paper_trading_engine for backtesting, separate live path).

### core/production_gate.py — Real-Money Gate + Loss Limiter
- **Status:** SKIP (pattern useful)
- **Summary:** Config-locked production gate for spike reversion. Requires ≥58% accuracy, ≥100 trades, Sharpe ≥1.0, ≤30% station concentration. Loss limiter: scales down after 3+ consecutive losses.
- **Contains useful code?** Gate pattern (config-locked, checklist-based) is reusable. Loss distribution analysis concept.
- **Recommendation:** SKIP as code, but ADOPT the pattern. The config-locked gate with minimum accuracy/trades/Sharpe requirements is a good pattern for graduating signals from paper to live. Implement a similar gate in the SignalRegistry pipeline with the 57.76% threshold.

### core/visibility_hooks.py — Observability Events
- **Status:** SKIP
- **Summary:** Pub-sub visibility event system for pipeline observability. Hook registration and emission at boundary points.
- **Contains useful code?** The event envelope pattern is clean but overengineered for the current codebase.
- **Recommendation:** SKIP. Use the existing structured_logger for observability.

### core/structured_logger.py — JSON Logger
- **Status:** SKIP
- **Summary:** JSON-formatted structured logging replacing ad-hoc print() calls. One JSON object per log line with timestamp, level, module, event_id.
- **Contains useful code?** `StructuredFormatter` class, `get_logger()` factory. The JSON format is well-designed.
- **Recommendation:** SKIP. The SignalRegistry pipeline already uses standard logging. The JSON format is a style preference, not a functional requirement. Keep in mind for future observability upgrades if the pipeline needs machine-parsable logs.

### core/round_number_anchoring.py — Behavioral Bias Detection
- **Status:** SKIP
- **Summary:** Detects market mispricing at round number thresholds (80, 85, 90°F) by comparing climatological probability vs market-implied probability.
- **Contains useful code?** Concept of comparing market probability to climatological probability at specific thresholds. But the implementation is simple.
- **Recommendation:** SKIP. The SignalRegistry's anomaly-based signals already capture temperature deviations at all thresholds. Round-number anchoring is a behavioral finance concept that has limited edge in Kalshi's binary options markets, where contract prices efficiently reflect settlement probabilities.

### core/nws_revision_model.py — NWS Revision Tracking (Scaffold)
- **Status:** SKIP
- **Summary:** Scaffold for tracking NWS observation revisions (initial vs revised). Implementation deferred — requires CDS API (ERA5 backfill).
- **Contains useful code?** No — scaffold only.
- **Recommendation:** SKIP. Not implemented. Defer indefinitely unless CDS API access becomes available.

### core/lever_manager.py — 4-Stage Position Sizing
- **Status:** EXPLORE → EXTRACT (selected functions)
- **Summary:** Four-stage monotonic-decreasing position sizing: Kelly base → variance adjustment → drawdown multiplier → concentration cap. Uses `LeverState` dataclass for state tracking.
- **Contains useful code?** The pipeline order is well-designed. The `LeverManager.var_adjusted_kelly()` and drawdown management logic are useful.
- **B-mode compliant?** Yes — pure numpy.
- **Integration effort:** Medium — depends on `fee_aware_kelly_position_sizing.py`.
- **Recommendation:** EXTRACT the drawdown management portion only. The Kelly sizing is already handled by the fee-aware Kelly module in the surviving pipeline. The drawdown multiplier (halve at X%, stop at Y%) and concentration cap (max % per station/signal) are not present in the current pipeline and would add safety.

### core/adaptive_thresholds.py — Bayesian Threshold Control
- **Status:** ALREADY COVERED
- **Summary:** Beta-Bernoulli conjugate prior for per-signal/per-station accuracy thresholds. Dual EMA (15d fast + 90d slow) for seasonal baseline. Momentum dampening (max 5pp/day).
- **Contains useful code?** `AdaptiveThresholdRegistry` with `get_threshold()`, `record_outcome()`, `filter_signals()`. The Beta-Bernoulli approach is principled.
- **B-mode compliant?** Yes.
- **Integration effort:** Already in pipeline?
- **Recommendation:** ALREADY COVERED. The SignalRegistry pipeline uses this module. No re-integration needed.

### core/fee_aware_filter.py — Fee-Aware Entry Filter
- **Status:** ALREADY COVERED
- **Summary:** Entry rules rejecting trades when dollar edge after fees < minimum threshold (0.50) or price outside [0.05, 0.95] (penny-contract trap).
- **Contains useful code?** `FeeAwareEntryFilter.evaluate_entry()` with edge calculation.
- **Recommendation:** ALREADY COVERED by MARKET_COST_MODEL in the surviving pipeline.

### core/fee_aware_kelly_position_sizing.py — Kelly Sizing
- **Status:** ALREADY COVERED
- **Summary:** Fractional Kelly position sizing with fee adjustments. 50% fractional Kelly, max 25% of balance per trade, 30-day window.
- **Contains useful code?** `KellyPositionSizer` with correct Kelly formula and fee adjustments.
- **Recommendation:** ALREADY COVERED. The SignalRegistry pipeline already includes fee-aware Kelly position sizing (KellyPositionSizer in the surviving codebase). No re-integration needed.

### core/order_book_baseline.py — Order Book Baseline Computation
- **Status:** EXPLORE
- **Summary:** Per-station, per-bucket, per-hour-of-day rolling baseline for order book metrics (bid/ask size, spread, volume). Short (20-period), diurnal (hour-of-day), and long (1-day) windows. B-mode compliant.
- **Contains useful code?** Baseline computation methodology and station timezone mapping.
- **B-mode compliant?** Yes.
- **Integration effort:** Low.
- **Recommendation:** EXPLORE for the execution layer. Order book baselines enable detecting abnormal market conditions (widening spreads, thinning liquidity) before trade placement. Useful but not critical for the initial SignalRegistry deployment.

### core/order_book_collector.py — Order Book Polling
- **Status:** SKIP
- **Summary:** Staggered polling of Kalshi order book endpoints across 20 stations × 2 markets (HIGH/LOW) every 30s. Uses rate-limit-safe API calls.
- **Contains useful code?** Staggered polling architecture and rate limit guard. But tightly coupled to a specific Kalshi API version.
- **B-mode compliant?** Yes.
- **Integration effort:** Would need to be updated for current Kalshi API.
- **Recommendation:** SKIP for now. Order book collection is useful for live execution but not for backtesting. If live trading is added, implement a fresh collector using the current Kalshi API, not this legacy version.

---

## Action Items (Priority Order)

1. **EXTRACT — execution_simulator.py** into the pre-trade validation step. Run Monte Carlo simulation before accepting backtest trades. (~1 day)
2. **EXTRACT — strike_selector.py** as the contract selection step for trade execution. (~half day)
3. **EXTRACT — settlement_execution_gate.py** for pre-trade gating (market open, cooldowns, approved stations). (~half day)
4. **EXTRACT — spatial_coherence.py** as a confidence modulation layer post-signal evaluation. (~1 day)
5. **EXTRACT — luck_elimination.py** as a post-backtest verification step for skill-vs-luck analysis. (~1 day)
6. **EXTRACT — lever_manager.py** (drawdown multiplier + concentration cap only). (~half day)
7. **EXPLORE — kalman_temperature_tracker.py** for live probability updates. Highest potential impact but requires careful integration with the signal pipeline. (~2-3 days)
8. **EXPLORE — dual_hypothesis_engine.py** for real-time METAR spike filtering. (~half day)
9. **EXPLORE — forecast_confidence_modulator.py** for live multi-model agreement modulation (post-backtest optimization).
10. **EXPLORE — settlement_fidelity.py** for ongoing station bias monitoring.

---

## Key Takeaways

- **6 modules** have extractable code ready for the SignalRegistry pipeline
- **KalmanTemperatureTracker** is the single highest-value parked module — it provides the live probability engine the pipeline currently lacks
- **RDAE-MOS** and **BMA/EMOS** are promising but too heavy and incomplete for current needs
- **HMM regime detection** is explicitly not B-mode compliant (uses hmmlearn/GaussianHMM) and must remain retired
- **No novel formula or methodology was found** that isn't accessible elsewhere — all useful concepts are well-known statistical/financial methods applied to weather markets