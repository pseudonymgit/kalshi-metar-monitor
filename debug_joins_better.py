#!/usr/bin/env python3
"""
Better debug script to identify the join issue between settlements and metar_features
"""

import sys
sys.path.insert(0, ".")
import sqlite3
from collections import defaultdict

METAR_DB = "data/metar_backfill.db"

print("=== Better Debugging of Join Issue ===")

# Load settlements
conn = sqlite3.connect(METAR_DB)
cursor = conn.cursor()

# Check some sample settlement data and metar data to understand format differences
print("\nSample settlement dates with stations:")
settlement_samples = cursor.execute("""
    SELECT station, local_trading_date
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
    LIMIT 10
""").fetchall()

for station, date in settlement_samples:
    print(f"  Settlement: {station} {date}")

print("\nSample METAR dates with stations:")
metar_samples = cursor.execute("""
    SELECT station, date_utc
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
    LIMIT 10
""").fetchall()

for station, date in metar_samples:
    print(f"  METAR: {station} {date}")

# Check if we need to align date formats - typically settlements might be the NEXT day of observations
print("\nChecking if there's a systematic date difference...")

# Let's see if for each settlement date, the corresponding metar data is the day before
settlement_dates = cursor.execute("""
    SELECT DISTINCT local_trading_date 
    FROM settlement_epochs
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND prior_settlement_bucket IS NOT NULL
    ORDER BY local_trading_date
    LIMIT 5
""").fetchall()

metar_dates = cursor.execute("""
    SELECT DISTINCT date_utc 
    FROM metar_observations
    WHERE station IN ('KNYC','KLAX','KMDW','KBOS','KATL','KSFO','KSEA')
      AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL
    ORDER BY date_utc
    LIMIT 5
""").fetchall()

print(f"\nSample settlement dates: {[d[0] for d in settlement_dates]}")
print(f"Sample metar dates: {[d[0] for d in metar_dates]}")

# Test if settlement date is for T+1 compared to metar date being T
test_settlement = cursor.execute("""
    SELECT DISTINCT local_trading_date FROM settlement_epochs
    WHERE station='KNYC' LIMIT 1
""").fetchone()[0]

test_metar = cursor.execute("""
    SELECT DISTINCT date_utc FROM metar_observations
    WHERE station='KNYC' AND temp_c IS NOT NULL AND pressure_mb IS NOT NULL AND date_utc <= ?
    ORDER BY date_utc DESC LIMIT 1
""", (test_settlement,)).fetchone()

if test_metar:
    print(f"\nExample for KNYC: settlement date '{test_settlement}' vs metar date '{test_metar[0]}'")
    print(f"So the weather on {test_metar[0]} predicts the movement that will happen on {test_settlement}")

conn.close()

print("\nThis suggests the METAR observations on date T predict settlements on date T+1 or T (the same day).")