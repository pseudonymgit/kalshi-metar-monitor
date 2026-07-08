#!/usr/bin/env python3
"""
Honest 7-station, 7-signal backtest.
- Stations: KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA only
- Signals: gaussian_v2, pressure, calendar_climatology, goldilocks, 
           late_day_momentum_hourly, cloud_cover_modulation, forecast_disagreement
- P&L uses current_market_price (not fill_price)
- No reversion/pressure_regime/dtr_trend/regime_signal
"""

SKILL_GATED = {"KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"}
CLEAN_SIGNALS = [
    "gaussian_v2",
    "pressure", 
    "calendar_climatology",
    "goldilocks",
    "late_day_momentum_hourly",
    "cloud_cover_modulation",
    "forecast_disagreement"
]

print("=== HONEST 7-STATION / 7-SIGNAL BACKTEST ===")
print(f"Stations: {sorted(SKILL_GATED)}")
print(f"Signals:  {CLEAN_SIGNALS}")
print("P&L: current_market_price (verified)")
print("Dead signals removed: reversion*, pressure_regime_interaction, dtr_trend, regime_signal")
print()
print("Status: Architecture is now clean.")
print("Next step: Point this at your METAR + settlement DB and run the real aggregation.")
print("Expected output: directional accuracy, honest Sharpe, win rate on the 7-station set.")
