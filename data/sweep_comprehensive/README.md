#!/usr/bin/env python3
"""
Final report for the Comprehensive Signal Sweep (2026-08-09)

Executed:
  - Phase A: Signal Fire-Rate Audit — 32 signals tested, 15 firing identified
  - Phase B: Full Correlation Matrix — 8 redundant signals killed, 7 independent kept
  - Phase C: Comprehensive Sweep — 19 stations, 7 signals, 5-level pipeline
  - Phase D: Stratified Sweep — 4 seasons filtered through full pipeline

Files modified:
  - core/signals/__init__.py (SignalRegistry)
  - core/unified_backtest.py (BACKTEST_SIGNALS)
  - scripts/audit_all_signals.py (NEW)
  - scripts/per_parameter_sweep.py (SIGNAL_SAMPLE)
  - data/signal_fire_audit.json (NEW)
  - data/signal_correlation_matrix.json (NEW)
  - data/sweep_comprehensive/ (results)
  - data/sweep_stratified/ (JJA, MAM, DJF, SON results)

Summary:
  Final 7-signal set: gaussian, pressure_delta, seasonal_regime,
    ecmwf_bias_corrected, wind_direction_shift, metar_trend, spike_reversion

  Comprehensive accuracy: 0.554 (optimal), Sharpe: 3.62, PnL: $2.6M
  Walk-forward: 54.8%-57.4% across 3 windows (consistent above coin-flip)
  Best season: SON (0.588), Worst: JJA (0.540)
  Regime-sensitive: variance 0.026 (flagged)
"""