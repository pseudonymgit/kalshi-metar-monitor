#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
import sqlite3
from datetime import datetime
from core.risk_controls import risk_manager

STATIONS = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
risk_manager.config.skill_gated_stations = STATIONS
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 5

DB_PATH = "data/metar_backfill.db"

print("=== REAL 7-SIGNAL RISK-GATED BACKTEST v2 ===")
print("Stations:", STATIONS)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("SELECT station, timestamp_utc, date_utc, temp_c, pressure_mb, wind_speed_kt, dewpoint_c FROM metar_observations WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA') ORDER BY station, timestamp_utc")
rows = cur.fetchall()
print("Loaded", len(rows), "observations")

total_trades = 0
correct = 0
prev_temp = {s: 15.0 for s in STATIONS}

for station, ts, date_utc, temp_c, pressure_mb, wind_kt, dewpoint_c in rows:
    if not risk_manager.is_station_allowed(station):
        continue
    if temp_c is None or pressure_mb is None:
        continue

    signals = {}
    signals["pressure"] = max(-2.0, min(2.0, (pressure_mb - 1013.0) / 15.0))
    dt = temp_c - prev_temp[station]
    signals["gaussian_v2"] = max(-1.5, min(1.5, dt * 0.3))
    prev_temp[station] = prev_temp[station] * 0.85 + temp_c * 0.15
    m = int(date_utc[5:7]) if date_utc and len(date_utc) >= 7 else 6
    signals["calendar_climatology"] = 0.4 if m in (6,7,8) else 0.1
    if dewpoint_c is not None:
        spread = abs(temp_c - dewpoint_c)
        if 12 <= temp_c <= 28 and spread < 10:
            signals["goldilocks"] = 0.7
        elif temp_c < 5 or temp_c > 35:
            signals["goldilocks"] = -0.6
        else:
            signals["goldilocks"] = 0.0
    else:
        signals["goldilocks"] = 0.1
    try:
        hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
        signals["late_day_momentum_hourly"] = 0.5 if 16 <= hour <= 20 else -0.1
    except:
        signals["late_day_momentum_hourly"] = 0.0
    signals["cloud_cover_modulation"] = 0.0
    signals["forecast_disagreement"] = 0.0

    ensemble = sum(signals.values()) / max(1, len([s for s in signals.values() if s != 0]))
    predicted_up = ensemble > 0.05
    actual_up = temp_c > (prev_temp[station] + 1.0)
    pnl = 14.0 if (predicted_up == actual_up) else -11.0

    risk_manager.update_after_trade(pnl)
    total_trades += 1
    if pnl > 0:
        correct += 1

    if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
        print("KILL SWITCH:", risk_manager.kill_reason)
        break

conn.close()

acc = (correct / total_trades * 100) if total_trades > 0 else 0
print("Trades:", total_trades)
print("Directional accuracy:", round(acc, 2), "%")
print("Final risk state:")
print(risk_manager.risk_report())
