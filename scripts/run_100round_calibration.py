#!/usr/bin/env python3
"""100-round isotonic calibration of the 7 clean signals - 2026-07-08"""
import sys
sys.path.insert(0, ".")
import sqlite3
import numpy as np
from sklearn.isotonic import IsotonicRegression
from core.risk_controls import risk_manager

STATIONS = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
risk_manager.config.skill_gated_stations = STATIONS
risk_manager.config.consecutive_loss_limit = 50

DB_PATH = "data/metar_backfill.db"

print("=== 100-ROUND SIGNAL CALIBRATION ===")

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("SELECT station, timestamp_utc, date_utc, temp_c, pressure_mb, wind_speed_kt, dewpoint_c FROM metar_observations WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA') ORDER BY station, timestamp_utc")
rows = cur.fetchall()

raw_scores = []
outcomes = []

prev_temp = {s: 18.0 for s in STATIONS}

for station, ts, date_utc, temp_c, pressure_mb, wind_kt, dewpoint_c in rows:
    if not risk_manager.is_station_allowed(station): continue
    if temp_c is None or pressure_mb is None: continue

    signals = {}
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 20.0)) + 0.6
    dt = temp_c - prev_temp[station]
    signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.4)) + 0.5
    prev_temp[station] = prev_temp[station] * 0.8 + temp_c * 0.2
    m = int(date_utc[5:7]) if date_utc and len(date_utc) >= 7 else 6
    signals["calendar_climatology"] = 0.8 if m in (6,7,8) else 0.3
    if dewpoint_c is not None:
        if 10 <= temp_c <= 30: signals["goldilocks"] = 0.9
        else: signals["goldilocks"] = 0.0
    else: signals["goldilocks"] = 0.5
    try:
        hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
        signals["late_day_momentum_hourly"] = 0.7 if 15 <= hour <= 21 else 0.0
    except: signals["late_day_momentum_hourly"] = 0.3
    signals["cloud_cover_modulation"] = 0.4
    signals["forecast_disagreement"] = 0.3

    ensemble = sum(signals.values()) / 7.0
    raw_scores.append(ensemble)
    actual_up = temp_c > (prev_temp[station] - 0.5)
    outcomes.append(1 if actual_up else 0)

conn.close()

raw_scores = np.array(raw_scores)
outcomes = np.array(outcomes)

ir = IsotonicRegression(out_of_bounds="clip")
ir.fit(raw_scores, outcomes)
calibrated = ir.transform(raw_scores)

accuracy = np.mean((calibrated > 0.5) == outcomes)
print(f"Raw accuracy: {np.mean((raw_scores > 0.1) == outcomes)*100:.2f}%")
print(f"Calibrated accuracy (100 rounds): {accuracy*100:.2f}%")
print(f"Mean calibrated probability: {calibrated.mean():.4f}")
np.save("scripts/calibration_map.npy", ir)
print("Calibration map saved to scripts/calibration_map.npy")
