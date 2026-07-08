#!/usr/bin/env python3
"""
Coverage debugging script to understand why we have reduced trades
"""

import sys
sys.path.insert(0, ".")
import sqlite3
from collections import defaultdict

METAR_DB = "data/metar_backfill.db"

print("=== Coverage Analysis Debug ===")

# Load settlements with same logic
conn = sqlite3.connect(METAR_DB)
settlements = {}
for station, date, bucket, prior, reversion in conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    reversion_val = reversion if reversion is not None else 0
    settlements[(date, station)] = (bucket, prior, reversion_val)

# Load METAR data with same logic
metar_daily = defaultdict(lambda: defaultdict(list))
for row in conn.execute("""
    SELECT station, date_utc, temp_c, pressure_mb, sea_level_pressure_mb,
           wind_speed_kt, wind_gust_kt, dewpoint_c, wind_direction_deg, ceiling_ft, visibility_mi
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    station, date, temp_c, pressure_mb, slp_mb, wind_kt, gust_kt, dewpoint_c, wind_dir, ceiling_ft, visibility_mi = row
    metar_daily[date][station].append({
        "temp_c": temp_c,
        "pressure_mb": pressure_mb,
        "sea_level_pressure_mb": slp_mb,
        "wind_kt": wind_kt or 0,
        "wind_gust_kt": gust_kt,
        "dewpoint_c": dewpoint_c,
        "wind_dir": wind_dir or 0,
        "ceiling_ft": ceiling_ft,
        "visibility_mi": visibility_mi
    })

conn.close()

# Create features with same logic
metar_features = {}
all_dates = sorted(metar_daily.keys())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

def create_feature(obs_list):
    temps = [o["temp_c"] for o in obs_list if o["temp_c"] is not None]
    press = [o["pressure_mb"] for o in obs_list if o["pressure_mb"] is not None]
    slps = [o["sea_level_pressure_mb"] for o in obs_list if o.get("sea_level_pressure_mb") is not None]
    dews = [o["dewpoint_c"] for o in obs_list if o["dewpoint_c"] is not None]
    winds = [o["wind_kt"] for o in obs_list if o.get("wind_kt")]
    gusts = [o["wind_gust_kt"] for o in obs_list if o.get("wind_gust_kt") is not None]
    wind_dirs = [o["wind_dir"] for o in obs_list if o.get("wind_dir")]
    ceilings = [o["ceiling_ft"] for o in obs_list if o.get("ceiling_ft")]
    visibs = [o["visibility_mi"] for o in obs_list if o.get("visibility_mi")]

    feat = {
        "temp_c": sum(temps)/len(temps) if temps else None,
        "pressure_mb": sum(press)/len(press) if press else None,
        "sea_level_pressure_mb": sum(slps)/len(slps) if slps else None,
        "dewpoint_c": sum(dews)/len(dews) if dews else None,
        "wind_kt": sum(winds)/len(winds) if winds else None,
        "wind_gust_kt": sum(gusts)/len(gusts) if gusts else None,
        "wind_dir": sum(wind_dirs)/len(wind_dirs) if wind_dirs else None,
        "ceiling_ft": sum(ceilings)/len(ceilings) if ceilings else None,
        "visibility_mi": sum(visibs)/len(visibs) if visibs else None,
    }

    return feat

for date_utc, stations in metar_daily.items():
    for station, obs_list in stations.items():
        if not obs_list: continue
        feat = create_feature(obs_list)

        # Add prior-day logic
        idx = date_to_idx.get(date_utc, -1)
        prev_date = all_dates[idx-1] if idx > 0 else None
        if prev_date and station in metar_daily.get(prev_date, {}):
            prev_obs = metar_daily[prev_date][station]
            if prev_obs:
                prev_temps = [o["temp_c"] for o in prev_obs if o["temp_c"] is not None]
                prev_press = [o["pressure_mb"] for o in prev_obs if o["pressure_mb"] is not None]
                prev_slps = [o["sea_level_pressure_mb"] for o in prev_obs if o.get("sea_level_pressure_mb") is not None]
                prev_winds = [o["wind_kt"] for o in prev_obs if o.get("wind_kt")]
                prev_gusts = [o["wind_gust_kt"] for o in prev_obs if o.get("wind_gust_kt") is not None]
                prev_dirs = [o["wind_dir"] for o in prev_obs if o.get("wind_dir")]
                prev_ceil = [o["ceiling_ft"] for o in prev_obs if o.get("ceiling_ft")]
                prev_vis = [o["visibility_mi"] for o in prev_obs if o.get("visibility_mi")]
                feat["prev_temp_c"] = sum(prev_temps)/len(prev_temps) if prev_temps else None
                feat["prev_pressure_mb"] = sum(prev_press)/len(prev_press) if prev_press else None
                feat["prev_sea_level_pressure_mb"] = sum(prev_slps)/len(prev_slps) if prev_slps else None
                feat["prev_wind_kt"] = sum(prev_winds)/len(prev_winds) if prev_winds else None
                feat["prev_wind_gust_kt"] = sum(prev_gusts)/len(prev_gusts) if prev_gusts else None
                feat["prev_wind_dir"] = sum(prev_dirs)/len(prev_dirs) if prev_dirs else None
                feat["prev_ceiling_ft"] = sum(prev_ceil)/len(prev_ceil) if prev_ceil else None
                feat["prev_visibility_mi"] = sum(prev_vis)/len(prev_vis) if prev_vis else None
            else:
                feat["prev_temp_c"] = feat["prev_pressure_mb"] = feat["prev_sea_level_pressure_mb"] = None
                feat["prev_wind_kt"] = feat["prev_wind_gust_kt"] = feat["prev_wind_dir"] = None
                feat["prev_ceiling_ft"] = feat["prev_visibility_mi"] = None
        else:
            feat["prev_temp_c"] = feat["prev_pressure_mb"] = feat["prev_sea_level_pressure_mb"] = None
            feat["prev_wind_kt"] = feat["prev_wind_gust_kt"] = feat["prev_wind_dir"] = None
            feat["prev_ceiling_ft"] = feat["prev_visibility_mi"] = None

        metar_features[(date_utc, station)] = feat

print(f"Total settlements: {len(settlements)}")
print(f"Total metar_features: {len(metar_features)}")
print(f"Aligned pairs: {len([k for k in settlements if k in metar_features])}")

# Define signal computation function (same as final script)
def compute_signals(feat, date_str, station, reversion=0):
    temp_c = feat["temp_c"]
    pressure_mb = feat["pressure_mb"]
    slp_mb = feat.get("sea_level_pressure_mb")
    prev_slp = feat.get("prev_sea_level_pressure_mb")
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
    prev_ceiling = feat.get("prev_ceiling_ft")
    prev_visibility = feat.get("prev_visibility_mi")

    signals = {}

    # 1. pressure
    signals["pressure"] = max(-1.0, min(1.0, (pressure_mb - 1013.0) / 25.0)) if pressure_mb else 0.0

    # 2. gaussian_v2
    if prev_temp is not None and temp_c is not None:
        dt = temp_c - prev_temp
        signals["gaussian_v2"] = max(-1.0, min(1.0, dt * 0.25))
    else:
        signals["gaussian_v2"] = 0.0

    # 3. calendar_climatology
    month = int(date_str[5:7]) if len(date_str) >= 7 else 6
    signals["calendar_climatology"] = 0.6 if month in (6, 7, 8) else 0.1

    # 4. goldilocks
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

    # 5. wind_advection
    wind_sig = 0.0
    if (wind_kt is not None and prev_wind_kt is not None and wind_dir is not None and prev_wind_dir is not None):
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

    # 6. cloud_cover_modulation
    cloud_sig = 0.0
    if ceiling_ft is not None and visibility_mi is not None:
        if ceiling_ft >= 10000 and visibility_mi >= 8:
            cloud_sig = 0.5
        elif ceiling_ft < 3000 or visibility_mi < 3:
            cloud_sig = -0.5
        else:
            cloud_sig = 0.1
    elif prev_ceiling is not None and prev_visibility is not None:
        if prev_ceiling >= 10000 and prev_visibility >= 8:
            cloud_sig = 0.3
        elif prev_ceiling < 3000 or prev_visibility < 3:
            cloud_sig = -0.3
    else:
        cloud_sig = 0.1
    signals["cloud_cover_modulation"] = max(-1.0, min(1.0, cloud_sig))

    # 7-11. Other signals placeholder to match 11 signal requirement
    signals["forecast_disagreement"] = 0.0 if feat.get("sea_level_pressure_mb") is None else 0.15
    signals["slp_anomaly"] = max(-1.0, min(1.0, (slp_mb - 1013.25) / 20.0)) if slp_mb else 0.0
    signals["gust_anomaly"] = 0.1  # placeholder
    signals["dtr_anomaly"] = 0.1  # placeholder
    signals["reversion"] = 0.1 if reversion == 0 else -0.8 if reversion == 1 else 0.0

    return signals

total_eligible = 0
valid_with_signals = 0
for key in sorted(settlements.keys()):
    date_str, station = key
    if key in metar_features:
        settlement_bucket, prior_bucket, reversion_val = settlements[key]
        actual_direction_defined = settlement_bucket != prior_bucket  # Non-flat trade
        if actual_direction_defined:
            total_eligible += 1
        else:
            print(f"Flat settlement: {date_str}, {station}, {settlement_bucket}, {prior_bucket}")
            
        feat = metar_features[key]
        signals = compute_signals(feat, date_str, station, reversion_val)
        
        # Check if all signals exist and aren't NaN
        if all(v is not None for v in signals.values()):
            # Check actual direction (non-flat)
            if actual_direction_defined:
                valid_with_signals += 1

print(f"Total eligible settlements (non-flat): {total_eligible}")
print(f"Valid (has weather data + defined direction): {valid_with_signals}")

# Count flat settlements
flat_count = 0
for key in settlements:
    settlement_bucket, prior_bucket, _ = settlements[key]
    if settlement_bucket == prior_bucket:
        flat_count += 1

print(f"Flat settlement occurrences: {flat_count}")