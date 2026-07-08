#!/usr/bin/env python3
"""
Clean 7-Signal Baseline Backtest (pressure_tendency REMOVED)
- Real settlement ground truth
- Real day-to-day temp delta (gaussian_v2)
- Real dewpoint depression (goldilocks)
- Real pressure anomaly
- Real NWP (thin)
- Risk guardrails active, kill switch DISABLED for this diagnostic
- Exactly 7 signals, 7 stations, skill-gated only
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
risk_manager.config.consecutive_loss_limit = 8  # DIAGNOSTIC while developing accuracy

METAR_DB = "data/metar_backfill.db"
NWP_DB = "data/nwp_forecasts.db"

print("=== CLEAN 7-SIGNAL BASELINE (pressure_tendency DROPPED) ===")
print("Stations:", STATIONS)
print("Kill switch: DISABLED")

# NWP - store ALL models per (date, station) for real disagreement spread
nwp_conn = sqlite3.connect(NWP_DB)
nwp_data = defaultdict(lambda: defaultdict(list))  # (date,station) -> variable -> list of model values
for fetch_date, target_date, station, model, variable, value in nwp_conn.execute("""
    SELECT fetch_date, target_date, station, model, variable, value 
    FROM nwp_forecasts 
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND variable IN ('temperature_2m_max','temperature_2m_min','pressure_mb')
"""):
    nwp_data[(target_date, station)][variable].append(value)
nwp_conn.close()
print(f"  NWP loaded for {len(nwp_data)} date-station pairs (multi-model enabled)")

# METAR + settlement
metar_conn = sqlite3.connect(METAR_DB)

settlements = {}
for station, date, bucket, prior in metar_conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    settlements[(date, station)] = (bucket, prior)

metar_daily = defaultdict(lambda: defaultdict(list))
for station, date, temp_c, pressure_mb, wind_kt, dewpoint_c, wind_dir, ceiling_ft, visibility_mi in metar_conn.execute("""
    SELECT station, date_utc, temp_c, pressure_mb, wind_speed_kt, dewpoint_c, wind_direction_deg, ceiling_ft, visibility_mi
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    metar_daily[date][station].append({
        "temp_c": temp_c, "pressure_mb": pressure_mb, "wind_kt": wind_kt or 0,
        "dewpoint_c": dewpoint_c, "wind_dir": wind_dir or 0
    })

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
        ceilings = [o["ceiling_ft"] for o in obs_list if o.get("ceiling_ft")]
        visibilities = [o["visibility_mi"] for o in obs_list if o.get("visibility_mi")]
        feat = {
            "temp_c": sum(temps)/len(temps) if temps else None,
            "pressure_mb": sum(press)/len(press) if press else None,
            "dewpoint_c": sum(dews)/len(dews) if dews else None,
            "wind_kt": sum(winds)/len(winds) if winds else None,
            "wind_dir": sum(wind_dirs)/len(wind_dirs) if wind_dirs else None,
            "ceiling_ft": sum(ceilings)/len(ceilings) if ceilings else None,
            "visibility_mi": sum(visibilities)/len(visibilities) if visibilities else None,
        }
        idx = date_to_idx.get(date_utc, -1)
        prev_date = all_dates[idx-1] if idx > 0 else None
        if prev_date and station in metar_daily.get(prev_date, {}):
            prev_obs = metar_daily[prev_date][station]
            if prev_obs:
                prev_temps = [o["temp_c"] for o in prev_obs if o["temp_c"] is not None]
                prev_winds = [o["wind_kt"] for o in prev_obs if o.get("wind_kt")]
                prev_dirs = [o["wind_dir"] for o in prev_obs if o.get("wind_dir")]
                prev_ceil = [o["ceiling_ft"] for o in prev_obs if o.get("ceiling_ft")]
                prev_vis = [o["visibility_mi"] for o in prev_obs if o.get("visibility_mi")]
                feat["prev_temp_c"] = sum(prev_temps)/len(prev_temps) if prev_temps else None
                feat["prev_wind_kt"] = sum(prev_winds)/len(prev_winds) if prev_winds else None
                feat["prev_wind_dir"] = sum(prev_dirs)/len(prev_dirs) if prev_dirs else None
                feat["prev_ceiling_ft"] = sum(prev_ceil)/len(prev_ceil) if prev_ceil else None
                feat["prev_visibility_mi"] = sum(prev_vis)/len(prev_vis) if prev_vis else None
            else:
                feat["prev_temp_c"] = None
                feat["prev_wind_kt"] = None
                feat["prev_wind_dir"] = None
                feat["prev_ceiling_ft"] = None
                feat["prev_visibility_mi"] = None
        else:
            feat["prev_temp_c"] = None
            feat["prev_wind_kt"] = None
            feat["prev_wind_dir"] = None
            feat["prev_ceiling_ft"] = None
            feat["prev_visibility_mi"] = None
        metar_features[(date_utc, station)] = feat

metar_conn.close()
print(f"  Loaded {len(settlements)} settlement epochs, {len(metar_features)} METAR days")

# Backtest
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
    prev_temp = feat.get("prev_temp_c")
    wind_kt = feat.get("wind_kt")
    wind_dir = feat.get("wind_dir")
    prev_wind_kt = feat.get("prev_wind_kt")
    prev_wind_dir = feat.get("prev_wind_dir")
    ceiling_ft = feat.get("ceiling_ft")
    visibility_mi = feat.get("visibility_mi")
    prev_ceiling = feat.get("prev_ceiling_ft")
    prev_visibility = feat.get("prev_visibility_mi")

    signals = {}

    # 1. pressure
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0))

    # 2. gaussian_v2 (real delta)
    if prev_temp is not None and temp_c is not None:
        dt = temp_c - prev_temp
        signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.25))
    else:
        signals["gaussian_v2"] = 0.0

    # 3. calendar_climatology
    month = int(date_str[5:7]) if len(date_str) >= 7 else 6
    signals["calendar_climatology"] = 0.6 if month in (6, 7, 8) else 0.1

    # 4. goldilocks (dewpoint depression)
    if dewpoint_c is not None and temp_c is not None:
        dd = temp_c - dewpoint_c
        if 10 <= dd <= 18 and 14 <= temp_c <= 28:
            signals["goldilocks"] = 0.9
        elif dd < 5 or dd > 22:
            signals["goldilocks"] = -0.4
        else:
            signals["goldilocks"] = 0.2
    else:
        signals["goldilocks"] = 0.1

    # 5. wind_advection (real lever - direction + speed delta)
    wind_sig = 0.0
    if (wind_kt is not None and prev_wind_kt is not None and
        wind_dir is not None and prev_wind_dir is not None):
        speed_delta = wind_kt - prev_wind_kt
        dir_delta = abs(wind_dir - prev_wind_dir)
        if dir_delta > 180: dir_delta = 360 - dir_delta
        if speed_delta > 4 and dir_delta > 45:
            wind_sig = 0.6
        elif speed_delta < -4 and dir_delta > 45:
            wind_sig = -0.4
        else:
            wind_sig = speed_delta / 12.0
    signals["wind_advection"] = max(-1.0, min(1.0, wind_sig))

    # 6. cloud_cover_modulation (NEW real lever - ceiling + visibility)
    cloud_sig = 0.0
    if ceiling_ft is not None and visibility_mi is not None:
        # clear/high ceiling + good visibility → warmer bias
        if ceiling_ft >= 10000 and visibility_mi >= 8:
            cloud_sig = 0.5
        # low ceiling or poor visibility → cooler bias
        elif ceiling_ft < 3000 or visibility_mi < 3:
            cloud_sig = -0.5
        else:
            cloud_sig = 0.1
    elif prev_ceiling is not None and prev_visibility is not None:
        # prior-day proxy when current missing
        if prev_ceiling >= 10000 and prev_visibility >= 8:
            cloud_sig = 0.3
        elif prev_ceiling < 3000 or prev_visibility < 3:
            cloud_sig = -0.3
    signals["cloud_cover_modulation"] = max(-1.0, min(1.0, cloud_sig))

    # 7. forecast_disagreement (REAL multi-model spread)
    nwp_key = (date_str, station)
    if nwp_key in nwp_data:
        vars_at_key = nwp_data[nwp_key]
        spreads = []
        for var_name in ('temperature_2m_max', 'temperature_2m_min', 'pressure_mb'):
            vals = vars_at_key.get(var_name, [])
            if len(vals) >= 2:
                spreads.append(max(vals) - min(vals))
        if spreads:
            avg_spread = sum(spreads) / len(spreads)
            # normalize: 0-2°C / 0-2 mb spread → 0..0.8 signal
            signals["forecast_disagreement"] = max(-1.0, min(1.0, avg_spread / 3.0))
        else:
            signals["forecast_disagreement"] = 0.15
    else:
        signals["forecast_disagreement"] = 0.0

    # Ensemble
    ensemble = sum(signals.values()) / 7.0
    predicted_up = ensemble > 0.08

    pnl = 14.0 if (predicted_up == actual_up) else -11.0
    risk_manager.update_after_trade(pnl)

    total_trades += 1
    if pnl > 0: correct += 1
    pnl_total += pnl

    # Risk gates (production guardrails) - kill switch now active
    if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()):
        print("KILL SWITCH TRIGGERED:", risk_manager.kill_reason)
        break

acc = (correct / total_trades * 100) if total_trades > 0 else 0
print("\n=== CLEAN 7-SIGNAL RESULTS ===")
print(f"Trades evaluated: {total_trades}")
print(f"Directional accuracy: {round(acc, 2)}%")
print(f"Total paper P&L: ${pnl_total:,.0f}")
print(f"Final risk state: {risk_manager.risk_report()}")