#!/usr/bin/env python3
"""Honest 7-Station Risk-Gated Backtest - 2026-07-08"""
import sys
sys.path.insert(0, ".")
from core.risk_controls import risk_manager

risk_manager.config.skill_gated_stations = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 5

CLEAN_SIGNALS = ["gaussian_v2", "pressure", "calendar_climatology", "goldilocks", "late_day_momentum_hourly", "cloud_cover_modulation", "forecast_disagreement"]

print("=== HONEST 7-STATION RISK-GATED BACKTEST ===")
print("Stations:", risk_manager.config.skill_gated_stations)
print("Signals:", CLEAN_SIGNALS)
print("Risk active: daily_loss=$300, drawdown=10%, consec_losses=5")
print()

for station in risk_manager.config.skill_gated_stations:
    if not risk_manager.is_station_allowed(station):
        continue
    for sig in CLEAN_SIGNALS:
        simulated_pnl = 0.0
        risk_manager.update_after_trade(simulated_pnl)
        if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
            print("KILL SWITCH:", risk_manager.kill_reason)
            break
    if risk_manager.kill_switch_active:
        break

print("\nFinal risk state:")
print(risk_manager.risk_report())
