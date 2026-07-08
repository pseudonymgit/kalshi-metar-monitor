#!/usr/bin/env python3
"""
Clean 7-Station / 7-Signal Backtest — Honest Baseline
Date: 2026-07-08
Purpose: First honest post-Phase-1 metrics on skill-gated stations only.

Stations:  KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA
Signals:   gaussian_v2, pressure, calendar_climatology, goldilocks,
           late_day_momentum_hourly, cloud_cover_modulation, forecast_disagreement

P&L:       Uses current_market_price (NOT fill_price) — verified in paper_trading_engine.py:944
Dead signals removed: reversion*, pressure_regime_interaction, dtr_trend, regime_signal
"""

SKILL_GATED_STATIONS = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]

CLEAN_SIGNALS = [
    "gaussian_v2",
    "pressure",
    "calendar_climatology",
    "goldilocks",
    "late_day_momentum_hourly",
    "cloud_cover_modulation",
    "forecast_disagreement",
]

print("=== CLEAN 7-STATION / 7-SIGNAL BACKTEST ===")
print(f"Stations: {SKILL_GATED_STATIONS}")
print(f"Signals:  {CLEAN_SIGNALS}")
print("P&L rule: current_market_price (not fill_price)")
print("Dead signals purged: reversion*, pressure_regime_interaction, dtr_trend, regime_signal")
print()
print("Status: Architecture clean. Ready for METAR + settlement database wiring.")
print("Next: Point this at the data and compute directional accuracy + honest Sharpe.")
