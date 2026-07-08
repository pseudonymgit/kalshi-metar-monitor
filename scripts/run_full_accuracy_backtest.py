#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
import sqlite3
import numpy as np
from datetime import datetime
from core.risk_controls import risk_manager

STATIONS = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
risk_manager.config.skill_gated_stations = STATIONS
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 5

DB_PATH = "data/metar_backfill.db"
CALIB_PATH = "scripts/calibration_map.npy"

print("=== FULL ACCURACY BACKTEST (all levers) ===")
print("Stations:", STATIONS)

ir = np.load(CALIB_PATH, allow_pickle=True).item()

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Real ground truth from settlement_epochs + METAR signals
cur.execute("""
    SELECT e.station, e.local_trading_date, e.market_type,
           e.settlement_bucket, e.prior_settlement_bucket,
           o.temp_c, o.pressure_mb, o.wind_speed_kt, o.dewpoint_c,
           o.ceiling_ft, o.visibility_mi
    FROM settlement_epochs e
    LEFT JOIN metar_observations o 
      ON e.station = o.station 
     AND e.local_trading_date = DATE(o.timestamp_utc)
    WHERE e.station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND e.epoch_status = 'closed'
    ORDER BY e.local_trading_date, e.station
""")

rows = cur.fetchall()
print(f"Loaded {len(rows)} settlement epochs")

total_trades = 0
correct = 0

for row in rows:
    station, date, market_type, settlement, prior, temp_c, pressure_mb, wind_kt, dewpoint_c, ceiling, vis = row
    if not risk_manager.is_station_allowed(station): continue
    if settlement is None or prior is None: continue

    actual_up = settlement > prior   # REAL Kalshi ground truth

    signals = {}
    if temp_c is not None and pressure_mb is not None:
        signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 20.0)) + 0.6
        signals["gaussian_v2"] = 0.5
        signals["calendar_climatology"] = 0.4 if int(str(date)[5:7]) in (6,7,8) else 0.1
        if dewpoint_c is not None:
            signals["goldilocks"] = 0.9 if 10 <= temp_c <= 30 else 0.0
        else:
            signals["goldilocks"] = 0.5
        signals["late_day_momentum_hourly"] = 0.3
        if ceiling is not None and vis is not None:
            signals["cloud_cover_modulation"] = 0.6 if ceiling > 10000 and vis > 10 else (-0.7 if ceiling < 3000 or vis < 3 else 0.0)
        else:
            signals["cloud_cover_modulation"] = 0.2
        signals["forecast_disagreement"] = 0.0   # NWP join later

    ensemble = sum(signals.values()) / max(1, len(signals))
    calib_prob = float(ir.transform([ensemble])[0])
    predicted_up = calib_prob > 0.5

    pnl = 14.0 if (predicted_up == actual_up) else -11.0

    risk_manager.update_after_trade(pnl)
    total_trades += 1
    if pnl > 0: correct += 1

    if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
        print("KILL SWITCH:", risk_manager.kill_reason)
        break

conn.close()

acc = (correct / total_trades * 100) if total_trades > 0 else 0
print("Trades evaluated:", total_trades)
print("Directional accuracy (real settlement truth + calibration):", round(acc, 2), "%")
print("Final risk state:", risk_manager.risk_report())
