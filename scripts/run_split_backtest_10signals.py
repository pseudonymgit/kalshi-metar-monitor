#!/usr/bin/env python3
"""
Split Backtest — Per-signal directional accuracy on the clean 10-signal set
Each signal evaluated standalone (no ensemble) against real settlement ground truth.
Same 7 stations, same risk guardrails (limit=8 diagnostic), full coverage.
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
risk_manager.config.consecutive_loss_limit = 8

METAR_DB = "data/metar_backfill.db"
NWP_DB = "data/nwp_forecasts.db"

print("=== SPLIT BACKTEST — 10 SIGNALS (standalone accuracy) ===")
print("Stations:", STATIONS)
print("Risk limit: 8 consecutive losses (diagnostic)")

# NWP
nwp_conn = sqlite3.connect(NWP_DB)
nwp_data = defaultdict(lambda: defaultdict(list))
for fetch_date, target_date, station, model, variable, value in nwp_conn.execute("""
    SELECT fetch_date, target_date, station, model, variable, value 
    FROM nwp_forecasts 
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND variable IN ('temperature_2m_max','temperature_2m_min','pressure_mb')
"""):
    nwp_data[(target_date, station)][variable].append(value)
nwp_conn.close()

# METAR + settlement (full coverage structure)
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
for row in metar_conn.execute("""
    SELECT station, date_utc, temp_f, temp_c, pressure_mb, sea_level_pressure_mb,
           wind_speed_kt, wind_gust_kt, dewpoint_c, wind_direction_deg, ceiling_ft, visibility_mi
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    station, date, temp_f, temp_c, pressure_mb, slp_mb, wind_kt, gust_kt, dewpoint_c, wind_dir, ceiling_ft, visibility_mi = row
    metar_daily[date][station].append({
        "temp_f": temp_f, "temp_c": temp_c, "pressure_mb": pressure_mb,
        "sea_level_pressure_mb": slp_mb, "wind_kt": wind_kt or 0,
        "wind_gust_kt": gust_kt, "dewpoint_c": dewpoint_c,
        "wind_dir": wind_dir or 0, "ceiling_ft": ceiling_ft, "visibility_mi": visibility_mi
    })

metar_features = {}
all_dates = sorted(metar_daily.keys())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

for date_utc, stations in metar_daily.items():
    for station, obs_list in stations.items():
        if not obs_list: continue
        temps_f = [o["temp_f"] for o in obs_list if o["temp_f"] is not None]
        temps_c = [o["temp_c"] for o in obs_list if o["temp_c"] is not None]
        press = [o["pressure_mb"] for o in obs_list if o["pressure_mb"] is not None]
        slps = [o["sea_level_pressure_mb"] for o in obs_list if o.get("sea_level_pressure_mb") is not None]
        dews = [o["dewpoint_c"] for o in obs_list if o["dewpoint_c"] is not None]
        winds = [o["wind_kt"] for o in obs_list if o.get("wind_kt")]
        gusts = [o["wind_gust_kt"] for o in obs_list if o.get("wind_gust_kt") is not None]
        wind_dirs = [o["wind_dir"] for o in obs_list if o.get("wind_dir")]
        ceilings = [o["ceiling_ft"] for o in obs_list if o.get("ceiling_ft")]
        visibs = [o["visibility_mi"] for o in obs_list if o.get("visibility_mi")]
        dtr_f = max(temps_f) - min(temps_f) if temps_f else None

        feat = {
            "temp_c": sum(temps_c)/len(temps_c) if temps_c else None,
            "pressure_mb": sum(press)/len(press) if press else None,
            "sea_level_pressure_mb": sum(slps)/len(slps) if slps else None,
            "dewpoint_c": sum(dews)/len(dews) if dews else None,
            "wind_kt": sum(winds)/len(winds) if winds else None,
            "wind_gust_kt": sum(gusts)/len(gusts) if gusts else None,
            "wind_dir": sum(wind_dirs)/len(wind_dirs) if wind_dirs else None,
            "ceiling_ft": sum(ceilings)/len(ceilings) if ceilings else None,
            "visibility_mi": sum(visibs)/len(visibs) if visibs else None,
            "dtr_f": dtr_f,
        }

        idx = date_to_idx.get(date_utc, -1)
        prev_date = all_dates[idx-1] if idx > 0 else None
        if prev_date and station in metar_daily.get(prev_date, {}):
            prev_obs = metar_daily[prev_date][station]
            if prev_obs:
                prev_temps_f = [o["temp_f"] for o in prev_obs if o["temp_f"] is not None]
                prev_temps_c = [o["temp_c"] for o in prev_obs if o["temp_c"] is not None]
                prev_press = [o["pressure_mb"] for o in prev_obs if o["pressure_mb"] is not None]
                prev_slps = [o["sea_level_pressure_mb"] for o in prev_obs if o.get("sea_level_pressure_mb") is not None]
                prev_winds = [o["wind_kt"] for o in prev_obs if o.get("wind_kt")]
                prev_gusts = [o["wind_gust_kt"] for o in prev_obs if o.get("wind_gust_kt") is not None]
                prev_dirs = [o["wind_dir"] for o in prev_obs if o.get("wind_dir")]
                prev_ceil = [o["ceiling_ft"] for o in prev_obs if o.get("ceiling_ft")]
                prev_vis = [o["visibility_mi"] for o in prev_obs if o.get("visibility_mi")]
                feat["prev_temp_c"] = sum(prev_temps_c)/len(prev_temps_c) if prev_temps_c else None
                feat["prev_pressure_mb"] = sum(prev_press)/len(prev_press) if prev_press else None
                feat["prev_sea_level_pressure_mb"] = sum(prev_slps)/len(prev_slps) if prev_slps else None
                feat["prev_wind_kt"] = sum(prev_winds)/len(prev_winds) if prev_winds else None
                feat["prev_wind_gust_kt"] = sum(prev_gusts)/len(prev_gusts) if prev_gusts else None
                feat["prev_wind_dir"] = sum(prev_dirs)/len(prev_dirs) if prev_dirs else None
                feat["prev_ceiling_ft"] = sum(prev_ceil)/len(prev_ceil) if prev_ceil else None
                feat["prev_visibility_mi"] = sum(prev_vis)/len(prev_vis) if prev_vis else None
                feat["prev_dtr_f"] = (max(prev_temps_f) - min(prev_temps_f)) if prev_temps_f else None
            else:
                for k in ["prev_temp_c","prev_pressure_mb","prev_sea_level_pressure_mb","prev_wind_kt","prev_wind_gust_kt","prev_wind_dir","prev_ceiling_ft","prev_visibility_mi","prev_dtr_f"]:
                    feat[k] = None
        else:
            for k in ["prev_temp_c","prev_pressure_mb","prev_sea_level_pressure_mb","prev_wind_kt","prev_wind_gust_kt","prev_wind_dir","prev_ceiling_ft","prev_visibility_mi","prev_dtr_f"]:
                feat[k] = None

        metar_features[(date_utc, station)] = feat

metar_conn.close()

def compute_signals(feat, date_str, station, nwp_data):
    temp_c = feat["temp_c"]
    pressure_mb = feat["pressure_mb"]
    slp_mb = feat.get("sea_level_pressure_mb")
    dewpoint_c = feat.get("dewpoint_c")
    prev_temp = feat.get("prev_temp_c")
    wind_kt = feat.get("wind_kt")
    gust_kt = feat.get("wind_gust_kt")
    prev_wind_kt = feat.get("prev_wind_kt")
    prev_gust = feat.get("prev_wind_gust_kt")
    wind_dir = feat.get("wind_dir")
    prev_wind_dir = feat.get("prev_wind_dir")
    ceiling_ft = feat.get("ceiling_ft")
    visibility_mi = feat.get("visibility_mi")
    dtr_f = feat.get("dtr_f")
    prev_dtr = feat.get("prev_dtr_f")

    signals = {}

    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0)) if pressure_mb else 0.0

    if prev_temp is not None and temp_c is not None:
        dt = temp_c - prev_temp
        signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.25))
    else:
        signals["gaussian_v2"] = 0.0

    month = int(date_str[5:7]) if len(date_str) >= 7 else 6
    signals["calendar_climatology"] = 0.6 if month in (6, 7, 8) else 0.1

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

    wind_sig = 0.0
    if (wind_kt is not None and prev_wind_kt is not None and wind_dir is not None and prev_wind_dir is not None):
        speed_delta = wind_kt - prev_wind_kt
        dir_delta = abs(wind_dir - prev_wind_dir)
        if dir_delta > 180: dir_delta = 360 - dir_delta
        if speed_delta > 4 and dir_delta > 45: wind_sig = 0.6
        elif speed_delta < -4 and dir_delta > 45: wind_sig = -0.4
        else: wind_sig = speed_delta / 12.0
    signals["wind_advection"] = max(-1.0, min(1.0, wind_sig))

    cloud_sig = 0.0
    if ceiling_ft is not None and visibility_mi is not None:
        if ceiling_ft >= 10000 and visibility_mi >= 8: cloud_sig = 0.5
        elif ceiling_ft < 3000 or visibility_mi < 3: cloud_sig = -0.5
        else: cloud_sig = 0.1
    signals["cloud_cover_modulation"] = max(-1.0, min(1.0, cloud_sig))

    nwp_key = (date_str, station)
    if nwp_key in nwp_data:
        vars_at_key = nwp_data[nwp_key]
        spreads = []
        for var_name in ('temperature_2m_max', 'temperature_2m_min', 'pressure_mb'):
            vals = vars_at_key.get(var_name, [])
            if len(vals) >= 2: spreads.append(max(vals) - min(vals))
        if spreads:
            avg_spread = sum(spreads) / len(spreads)
            signals["forecast_disagreement"] = max(-1.0, min(1.0, avg_spread / 3.0))
        else:
            signals["forecast_disagreement"] = 0.15
    else:
        signals["forecast_disagreement"] = 0.0

    signals["slp_anomaly"] = max(-1.0, min(1.0, (slp_mb - 1013.25) / 20.0)) if slp_mb else 0.0

    gust_sig = 0.0
    if gust_kt is not None:
        gust_norm = min(1.0, gust_kt / 35.0)
        if prev_gust is not None:
            gust_delta = gust_kt - prev_gust
            if gust_delta > 8: gust_sig = 0.6
            elif gust_delta < -8: gust_sig = -0.5
            else: gust_sig = gust_norm * 0.4 + (gust_delta / 20.0)
        else:
            gust_sig = gust_norm * 0.4
    signals["gust_anomaly"] = max(-1.0, min(1.0, gust_sig))

    dtr_sig = 0.0
    if dtr_f is not None:
        dtr_norm = max(-1.0, min(1.0, (dtr_f - 15.0) / 12.0))
        if prev_dtr is not None:
            dtr_delta = dtr_f - prev_dtr
            if dtr_delta < -6: dtr_sig = 0.7
            elif dtr_delta > 6: dtr_sig = -0.6
            else: dtr_sig = dtr_norm * 0.5 + (dtr_delta / 15.0)
        else:
            dtr_sig = dtr_norm * 0.4
    signals["dtr_anomaly"] = max(-1.0, min(1.0, dtr_sig))

    return signals

# Evaluate each signal standalone
SIGNALS = ["pressure","gaussian_v2","calendar_climatology","goldilocks","wind_advection","cloud_cover_modulation","forecast_disagreement","slp_anomaly","gust_anomaly","dtr_anomaly"]

results = {s: {"trades": 0, "correct": 0, "pnl": 0.0} for s in SIGNALS}

for key in sorted(settlements.keys()):
    date_str, station = key
    if not risk_manager.is_station_allowed(station): continue
    if key not in metar_features: continue
    settlement_bucket, prior_bucket = settlements[key]
    actual_up = settlement_bucket > prior_bucket
    feat = metar_features[key]
    signals = compute_signals(feat, date_str, station, nwp_data)

    for s in SIGNALS:
        pred_up = signals[s] > 0.0   # simple threshold: positive signal = predict up
        correct = (pred_up == actual_up)
        pnl = 14.0 if correct else -11.0

        results[s]["trades"] += 1
        if correct: results[s]["correct"] += 1
        results[s]["pnl"] += pnl

print("\n=== PER-SIGNAL STANDALONE RESULTS ===")
print(f"{'Signal':<25} {'Trades':>8} {'Accuracy':>9} {'P&L':>12}")
print("-" * 58)
for s in SIGNALS:
    r = results[s]
    acc = (r["correct"] / r["trades"] * 100) if r["trades"] > 0 else 0
    print(f"{s:<25} {r['trades']:>8} {round(acc,2):>8.2f}% ${r['pnl']:>10,.0f}")

print("\nNote: Each signal evaluated independently with simple >0 threshold. No ensemble, no risk gating on individual signals.")