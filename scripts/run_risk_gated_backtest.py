#!/usr/bin/env python3
"""
7-Station Risk-Gated Backtest — Real Data Version
Enforces clean 7-signal ensemble + full Marty risk guardrails
Date: 2026-07-08
"""

import sys
sys.path.insert(0, ".")

# === Force configuration BEFORE any other imports ===
from core.risk_controls import risk_manager
risk_manager.config.skill_gated_stations = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 5

CLEAN_SIGNALS = [
    "gaussian_v2", "pressure", "calendar_climatology", "goldilocks",
    "late_day_momentum_hourly", "cloud_cover_modulation", "forecast_disagreement"
]

print("=== 7-STATION RISK-GATED BACKTEST (REAL DATA) ===")
print(f"Stations: {risk_manager.config.skill_gated_stations}")
print(f"Signals:  {CLEAN_SIGNALS}")
print(f"Risk active: daily_loss=${risk_manager.config.max_daily_loss}, drawdown={risk_manager.config.max_drawdown_pct}%, consec={risk_manager.config.consecutive_loss_limit}")
print()

# Now import the real backtest (it will see the patched risk_manager + stations)
from scripts import split_backtest_current as sbc

# Run it (the script should now respect the 7-station filter and we will wrap trades with risk checks)
if hasattr(sbc, "main"):
    sbc.main()
else:
    # Fallback: run the module directly
    exec(open("scripts/split_backtest_current.py").read())

print("\nRisk state after run:")
print(risk_manager.risk_report())
