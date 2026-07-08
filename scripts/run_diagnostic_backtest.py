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
risk_manager.config.consecutive_loss_limit = 999
DB_PATH = "data/metar_backfill.db"
print("=== DIAGNOSTIC BACKTEST (kill switch disabled) ===")
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("SELECT station, timestamp_utc, date_utc, temp_c, pressure_mb, wind_speed_kt, dewpoint_c, ceiling_ft, visibility_mi FROM metar_observations WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA') ORDER BY station, timestamp_utc")
rows = cur.fetchall()
total = 0
correct = 0
prev = {s: 18.0 for s in STATIONS}
for station, ts, date_utc, temp_c, pressure_mb, wind_kt, dewpoint_c, ceiling, vis in rows:
    if not risk_manager.is_station_allowed(station): continue
    if temp_c is None or pressure_mb is None: continue
    signals = {}
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 20.0)) + 0.6
    dt = temp_c - prev[station]
    signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.4)) + 0.5
    prev[station] = prev[station] * 0.8 + temp_c * 0.2
    m = int(date_utc[5:7]) if date_utc and len(date_utc) >= 7 else 6
    signals["calendar_climatology"] = 0.8 if m in (6,7,8) else 0.3
    if dewpoint_c is not None:
        if 10 <= temp_c <= 30: signals["goldilocks"] = 0.9
        else: signals["goldilocks"] = 0.0
    else: signals["goldilocks"] = 0.5
    try: hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour; signals["late_day_momentum_hourly"] = 0.7 if 15 <= hour <= 21 else 0.0
    except: signals["late_day_momentum_hourly"] = 0.3
    if ceiling is not None and vis is not None:
        if ceiling > 10000 and vis > 10: signals["cloud_cover_modulation"] = 0.6
        elif ceiling < 3000 or vis < 3: signals["cloud_cover_modulation"] = -0.7
        else: signals["cloud_cover_modulation"] = 0.0
    else: signals["cloud_cover_modulation"] = 0.2
    signals["forecast_disagreement"] = 0.0
    ensemble = sum(signals.values()) / 7.0
    predicted_up = ensemble > 0.1
    actual_up = temp_c > (prev[station] - 0.5)
    pnl = 14.0 if (predicted_up == actual_up) else -11.0
    risk_manager.update_after_trade(pnl)
    total += 1
    if pnl > 0: correct += 1
    if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown()): break
conn.close()
acc = (correct / total * 100) if total > 0 else 0
print("Trades:", total)
print("Directional accuracy:", round(acc, 2), "%")
print("Risk state:", risk_manager.risk_report())
