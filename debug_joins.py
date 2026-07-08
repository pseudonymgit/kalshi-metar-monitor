#!/usr/bin/env python3
"""
Debug script to identify join issues between settlements and metar_features
"""

import sys
sys.path.insert(0, ".")
import sqlite3
from collections import defaultdict

METAR_DB = "data/metar_backfill.db"

print("=== Debugging Join Issues Between Settlements and METAR ===")

# Load settlements
metar_conn = sqlite3.connect(METAR_DB)
settlements = {}
station_dates_in_settlements = set()
for station, date, bucket, prior, reversion in metar_conn.execute("""
    SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
"""):
    settlements[(date, station)] = (bucket, prior, reversion if reversion is not None else 0)
    station_dates_in_settlements.add((date, station))

print(f"Loaded {len(settlements)} settlements from {len(station_dates_in_settlements)} unique station-dates")

# Load raw observations to check what dates are available
metar_daily = defaultdict(lambda: defaultdict(list))
for station, date_utc, temp_c, pressure_mb in metar_conn.execute("""
    SELECT station, date_utc, temp_c, pressure_mb
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
"""):
    metar_daily[date_utc][station].append({"temp_c": temp_c, "pressure_mb": pressure_mb})

print(f"Found raw observations on {len(metar_daily)} different dates")

# Create collapsed features
metar_features = {}
all_dates = sorted(metar_daily.keys())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

# Check which settlements have corresponding weather data
missing_weather = []
matching_pairs = []
for date_str, station in station_dates_in_settlements:
    if (date_str, station) in metar_daily:
        matching_pairs.append((date_str, station))
    else:
        missing_weather.append((date_str, station))

print(f"Settlements with matching weather data: {len(matching_pairs)}")
print(f"Settlements with NO matching weather data: {len(missing_weather)}")

# Let's take a sample of missing pairs to investigate formatting
print(f"\nSample of missing pairs (first few):")
for i, (date_str, station) in enumerate(missing_weather[:10]):
    # Check if maybe the date format is different - check if there are variations
    possible_matches = [(k[0], k[1]) for k in metar_daily.keys() if k[1] == station and (k[0] == date_str or k[0].replace('-', '') == date_str.replace('-', ''))]

    print(f"  {date_str} {station} - matched in raw: {'YES' if possible_matches else 'NO'}")
    
# Test raw availability
print(f"\nTotal unique station-date pairs with observations: {sum(len(stations) for stations in metar_daily.values())}")

# Check the first few settlement dates with their corresponding raw data existence
print(f"\nVerifying some settlement dates exist in metar_daily:")
sample_settlements = list(station_dates_in_settlements)[:5]
for date, station in sample_settlements:
    exists = (date, station) in metar_daily
    print(f"  {date} {station}: {'EXISTS' if exists else 'NOT FOUND'}")


# Create final metar_features dictionary with prior-day logic (like original script)
for date_utc, stations in metar_daily.items():
    for station, obs_list in stations.items():
        if not obs_list: continue
        temps = [o["temp_c"] for o in obs_list if o["temp_c"] is not None]
        press = [o["pressure_mb"] for o in obs_list if o["pressure_mb"] is not None]
        
        feat = {
            "temp_c": sum(temps)/len(temps) if temps else None,
            "pressure_mb": sum(press)/len(press) if press else None,
        }
        
        # Add prior-day logic
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
                feat["prev_temp_c"] = feat["prev_pressure_mb"] = None
        else:
            feat["prev_temp_c"] = feat["prev_pressure_mb"] = None

        metar_features[(date_utc, station)] = feat

print(f"Final metar_features dictionary: {len(metar_features)} entries")

# Final check - how many settlements pairs have corresponding metar features
final_match_count = 0
for (date_str, station) in station_dates_in_settlements:
    if (date_str, station) in metar_features:
        final_match_count += 1

print(f"Final settlement items with metar_features: {final_match_count}")
print(f"Total settlement items: {len(station_dates_in_settlements)}")

# Close db connection
metar_conn.close()