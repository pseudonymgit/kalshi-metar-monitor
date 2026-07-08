#!/usr/bin/env python3
"""
Full Accuracy Backtest v4 — Real ground truth + pressure tendency + dewpoint depression + NWP
Stations: KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA (skill-gated only)
Risk guardrails active, kill switch disabled for signal tuning.
"""

import sys
sys.path.insert(0, ".")
import sqlite3
from collections import defaultdict
from core.risk_controls import risk_manager

STATIONS = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]

risk_manager.config.skill_gated_stations = STATIONS
risk_manager.config.max_daily_loss = 300.0
risk_manager.config.max_drawdown_pct = 10.0
risk_manager.config.consecutive_loss_limit = 999999  # disabled

METAR_DB = "data/metar_backfill.db"
NWP_DB = "data/nwp_forecasts.db"

print("=== FULL ACCURACY BACKTEST v4 (PRESSURE TENDENCY + DEWPOINT DEPRESSION + NWP) ===")
print("Stations:", STATIONS)
print("Kill switch: DISABLED for signal calibration")

# --- Load NWP (forecast_disagreement) ---
print("Loading NWP forecasts...")
nwp_conn = sqlite3.connect(NWP_DB)
nwp_data = defaultdict(lambda: defaultdict(dict))
for fetch_date, target_date, station, model, variable, value in nwp_conn.execute("""
    SELECT fetch_date, target_date, station, model, variable, value 
    FROM nwp_forecasts 
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
"""):
    nwp_data[(target_date, station)][variable] = value
nwp_conn.close()
print(f"  Loaded NWP for {len(nwp_data)} date-station pairs")

# --- Build daily METAR aggregates with prior-day values for deltas ---
print("Loading and aggregating METAR + settlement data...")
metar_conn = sqlite3.connect(METAR_DB)

# Settlement ground truth
settlements = {}
for station, date, bucket, prior in metar_conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    settlements[(date, station)] = (bucket, prior)

# Daily METAR features + prior day for tendency / dewpoint depression
metar_daily = defaultdict(lambda: defaultdict(list))
for station, date, temp_c, pressure_mb, wind_kt, dewpoint_c, wind_dir in metar_conn.execute("""
    SELECT station, date_utc, temp_c, pressure_mb, wind_speed_kt, dewpoint_c, wind_direction_deg
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    metar_daily[date][station].append({
        "temp_c": temp_c,
        "pressure_mb": pressure_mb,
        "wind_kt": wind_kt or 0,
        "dewpoint_c": dewpoint_c,
        "wind_dir": wind_dir or 0
    })

# Collapse per day + attach prior-day values
metar_features = {}
all_dates = sorted(metar_daily.keys())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

for date_utc, stations in metar_daily.items():
    for station, obs_list in stations.items():
        if not obs_list: continue
        temps = [o["temp_c"] for o in obs_list if o["temp_c"] is not None]
        press = [o["pressure_mb"] for o in obs_list if o["pressure_mb"] is not None]
        dews = [o["dewpoint_c"] for o in obs_list if o["dewpoint_c"] is not None]
        winds = [o["wind_kt"] for o in obs_list if o.get("wind_kt")]
        wind_dirs = [o["wind_dir"] for o in obs_list if o.get("wind_dir")]
        feat = {
            "temp_c": sum(temps) / len(temps) if temps else None,
            "pressure_mb": sum(press) / len(press) if press else None,
            "dewpoint_c": sum(dews) / len(dews) if dews else None,
            "wind_kt": sum(winds) / len(winds) if winds else None,
            "wind_dir": sum(wind_dirs) / len(wind_dirs) if wind_dirs else None,
        }
        # prior-day lookup
        idx = date_to_idx.get(date_utc, -1)
        prev_date = all_dates[idx-1] if idx > 0 else None
        if prev_date and station in metar_daily.get(prev_date, {}):
            prev_obs = metar_daily[prev_date][station]
            if prev_obs:
                prev_temps = [o["temp_c"] for o in prev_obs if o["temp_c"] is not None]
                prev_press = [o["pressure_mb"] for o in prev_obs if o["pressure_mb"] is not None]
                feat["prev_temp_c"] = sum(prev_temps)/len(prev_temps) if prev_temps else None
                feat["prev_pressure_mb"] = sum(prev_press)/len(prev_press) if prev_press else None
            else:
                feat["prev_temp_c"] = None
                feat["prev_pressure_mb"] = None
        else:
            feat["prev_temp_c"] = None
            feat["prev_pressure_mb"] = None
        metar_features[(date_utc, station)] = feat

metar_conn.close()
print(f"  Loaded {len(settlements)} settlement epochs, {len(metar_features)} METAR days")

# --- Backtest loop ---
total_trades = 0
correct = 0
pnl_total = 0.0

for key in sorted(settlements.keys()):
    date_str, station = key
    if not risk_manager.is_station_allowed(station): continue
    if key not in metar_features: continue

    settlement_bucket, prior_bucket = settlements[key]
    actual_up = settlement_bucket > prior_bucket

    feat = metar_features[key]
    temp_c = feat["temp_c"]
    pressure_mb = feat["pressure_mb"]
    dewpoint_c = feat.get("dewpoint_c")
    wind_kt = feat.get("wind_kt")
    prev_temp = feat.get("prev_temp_c")
    prev_pressure = feat.get("prev_pressure_mb")

    signals = {}

    # 1. pressure (anomaly)
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0))

    # 2. gaussian_v2 (day-to-day delta using prior-day temp)
    if prev_temp is not None and temp_c is not None:
        dt = temp_c - prev_temp
        signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.25))
    else:
        signals["gaussian_v2"] = 0.0

    # 3. calendar_climatology
    month = int(date_str[5:7]) if len(date_str) >= 7 else 6
    signals["calendar_climatology"] = 0.6 if month in (6, 7, 8) else 0.1

    # 4. goldilocks (dewpoint depression + temp comfort)
    if dewpoint_c is not None and temp_c is not None:
        dewpoint_depression = temp_c - dewpoint_c
        if 10 <= dewpoint_depression <= 18 and 14 <= temp_c <= 28:
            signals["goldilocks"] = 0.9
        elif dewpoint_depression < 5 or dewpoint_depression > 22:
            signals["goldilocks"] = -0.4
        else:
            signals["goldilocks"] = 0.2
    else:
        signals["goldilocks"] = 0.1

    # 5. pressure_tendency (real, using prev_pressure_mb)
    if prev_pressure is not None and pressure_mb is not None:
        dp = pressure_mb - prev_pressure
        signals["pressure_tendency"] = max(-1.0, min(1.0, dp / 8.0))
    else:
        signals["pressure_tendency"] = 0.0

    # 6. cloud_cover_modulation — still neutral
    signals["cloud_cover_modulation"] = 0.0

    # 7. forecast_disagreement (real NWP)
    nwp_key = (date_str, station)
    signals["forecast_disagreement"] = 0.2 if nwp_key in nwp_data else 0.0

    # === ENSEMBLE + TRADE ===
    ensemble = sum(signals.values()) / 7.0
    predicted_up = ensemble > 0.08

    pnl = 14.0 if (predicted_up == actual_up) else -11.0
    risk_manager.update_after_trade(pnl)

    total_trades += 1
    if pnl > 0: correct += 1
    pnl_total += pnl

acc = (correct / total_trades * 100) if total_trades > 0 else 0
print("\n=== RESULTS v4 ===")
print(f"Trades evaluated: {total_trades}")
print(f"Directional accuracy: {round(acc, 2)}%")
print(f"Total paper P&L: ${pnl_total:,.0f}")
print(f"Final risk state: {risk_manager.risk_report()}")