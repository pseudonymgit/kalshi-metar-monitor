#!/usr/bin/env python3
"""
ECMWF Targeted Backfill Script

Backfills missing ECMWF forecast dates to match GFS coverage,
then re-runs NWP Direct Signal assessment with the expanded dataset.

Current state (2026-07-22):
- ECMWF: 59 unique fetch days (2025-05-01 to 2026-07-21)
- GFS: 69 unique fetch days (same range)
- Gap: ~10 missing fetch days

Usage:
    python3 scripts/backfill_ecmwf.py [--db-path PATH] [--dry-run] [--skip-backfill] [--skip-assessment]
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"

# All 20 Kalshi cities
CITIES = [
    ("KATL", "Atlanta", 33.64, -84.43),
    ("KAUS", "Austin", 30.20, -97.67),
    ("KBOS", "Boston", 42.37, -71.01),
    ("KDCA", "Washington DC", 38.85, -77.04),
    ("KDEN", "Denver", 39.86, -104.67),
    ("KDFW", "Dallas", 32.90, -97.04),
    ("KHOU", "Houston", 29.98, -95.36),
    ("KLAS", "Las Vegas", 36.08, -115.16),
    ("KLAX", "Los Angeles", 33.94, -118.41),
    ("KMDW", "Chicago", 41.79, -87.75),
    ("KMIA", "Miami", 25.80, -80.29),
    ("KMSP", "Minneapolis", 44.88, -93.22),
    ("KMSY", "New Orleans", 29.99, -90.26),
    ("KNYC", "New York", 40.71, -74.01),
    ("KOKC", "Oklahoma City", 35.39, -97.60),
    ("KPHL", "Philadelphia", 39.87, -75.24),
    ("KPHX", "Phoenix", 33.43, -112.01),
    ("KSAT", "San Antonio", 29.53, -98.47),
    ("KSEA", "Seattle", 47.45, -122.31),
    ("KSFO", "San Francisco", 37.62, -122.38),
]

ECMWF_API = "https://api.open-meteo.com/v1/ecmwf"

DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
HOURLY_VARS = [
    "temperature_850hPa",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
    "dew_point_2m",
    "geopotential_height_500hPa",
]


def parse_args():
    parser = argparse.ArgumentParser(description="ECMWF Targeted Backfill")
    parser.add_argument("--db-path", help="Database file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze gaps but don't fetch data")
    parser.add_argument("--skip-backfill", action="store_true",
                        help="Skip the backfill, only assess")
    parser.add_argument("--skip-assessment", action="store_true",
                        help="Skip NWP Direct Signal assessment after backfill")
    return parser.parse_args()


def get_db_path(args, cwd):
    if args.db_path:
        return Path(args.db_path).absolute()
    if os.environ.get('NWP_DB_PATH'):
        return Path(os.environ['NWP_DB_PATH']).absolute()
    return (PROJECT_ROOT / NWP_DB_PATH_DEFAULT).absolute()


def analyze_gaps(conn):
    """Analyze which dates ECMWF is missing vs GFS."""
    c = conn.cursor()

    # Get distinct ECMWF fetch dates
    c.execute("SELECT DISTINCT fetch_date FROM nwp_forecasts WHERE model='ecmwf' ORDER BY fetch_date")
    ecmwf_dates = set(row[0] for row in c.fetchall())

    # Get distinct GFS fetch dates
    c.execute("SELECT DISTINCT fetch_date FROM nwp_forecasts WHERE model='gfs' ORDER BY fetch_date")
    gfs_dates = set(row[0] for row in c.fetchall())

    missing = sorted(gfs_dates - ecmwf_dates)
    extra = sorted(ecmwf_dates - gfs_dates)

    # Also check coverage per station for temp_2m_max
    c.execute("""
        SELECT model, COUNT(DISTINCT fetch_date || target_date || station)
        FROM nwp_forecasts
        WHERE variable = 'temperature_2m_max'
        GROUP BY model
        ORDER BY COUNT(*) DESC
    """)
    coverage = {row[0]: row[1] for row in c.fetchall()}

    return {
        'ecmwf_total_fetch_days': len(ecmwf_dates),
        'gfs_total_fetch_days': len(gfs_dates),
        'missing_dates': missing,
        'extra_dates': extra,
        'coverage': coverage,
    }


def fetch_ecmwf_past(api_url, lat, lon, past_days):
    """Fetch ECMWF historical forecasts for N past days."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
        "past_days": past_days,
    }
    url = api_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "OpenClaw-WeatherEngine/1.0"
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"    Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < 2:
                wait = 3 * (attempt + 1)
                print(f"    Retry {attempt+1}/3: {e}, waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": str(e)}

    return {"error": "Max retries exceeded"}


def store_forecast(conn, data, model_name, station, fetch_date, fetch_timestamp):
    """Parse and store forecast data."""
    c = conn.cursor()
    stored = 0
    errors = []

    if "error" in data:
        return 0, [data["error"]]

    daily = data.get("daily", {})
    daily_time = daily.get("time", [])

    # Process daily variables
    for var in DAILY_VARS:
        values = daily.get(var, [])
        for i, target_date in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (fetch_date, target_date, station, model_name,
                          var, float(values[i]), fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    # Process hourly variables — aggregate to daily mean
    hourly = data.get("hourly", {})
    hourly_time = hourly.get("time", [])

    for var in HOURLY_VARS:
        values = hourly.get(var, [])
        if not values:
            continue

        daily_means = {}
        for i, ts in enumerate(hourly_time):
            if i < len(values) and values[i] is not None:
                date = ts[:10]
                if date not in daily_means:
                    daily_means[date] = []
                daily_means[date].append(float(values[i]))

        for target_date, vals in daily_means.items():
            if vals:
                daily_mean = sum(vals) / len(vals)
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (fetch_date, target_date, station, model_name,
                          f"{var}_daily_mean", daily_mean, fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    conn.commit()
    return stored, errors


def run_backfill(conn, args):
    """Run the ECMWF backfill for missing dates."""
    gaps = analyze_gaps(conn)
    missing = gaps['missing_dates']

    if not missing:
        print("\nNo missing ECMWF dates to backfill. Coverage already matches GFS.")
        return gaps

    # Group missing dates into contiguous blocks to minimize API calls
    date_objs = [datetime.strptime(d, '%Y-%m-%d').date() for d in missing]
    blocks = []
    current_block = [date_objs[0]]
    for d in date_objs[1:]:
        if (d - current_block[-1]).days == 1:
            current_block.append(d)
        else:
            blocks.append(current_block)
            current_block = [d]
    blocks.append(current_block)

    total_blocks = len(blocks)
    print(f"\nECMWF Backfill: {len(missing)} missing dates across {total_blocks} contiguous blocks")
    print(f"Missing date range: {missing[0]} to {missing[-1]}")

    if args.dry_run:
        print("\nDRY RUN — not fetching data")
        for i, block in enumerate(blocks):
            block_dates = [d.strftime('%Y-%m-%d') for d in block]
            print(f"  Block {i+1}/{total_blocks}: {block_dates[0]} to {block_dates[-1]} ({len(block)} days)")
        return gaps

    print(f"\nBackfilling {len(CITIES)} stations...")

    fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()
    total_stored = 0
    total_errors = []

    # Process each contiguous block
    for block_idx, block in enumerate(blocks):
        block_dates = [d.strftime('%Y-%m-%d') for d in block]
        past_days = len(block) + 5  # Add buffer
        print(f"\nBlock {block_idx+1}/{total_blocks}: {block_dates[0]} to {block_dates[-1]} ({past_days} past_days)")

        for station, city_name, lat, lon in CITIES:
            print(f"  {station} ({city_name})...", end=" ", flush=True)

            data = fetch_ecmwf_past(ECMWF_API, lat, lon, past_days)

            if "error" in data:
                print(f"ERROR: {data['error']}")
                total_errors.append(f"{station}: {data['error']}")
                time.sleep(1)
                continue

            stored, errors = store_forecast(
                conn, data, "ecmwf", station, fetch_date, fetch_timestamp)
            total_stored += stored
            total_errors.extend(errors)
            print(f"OK ({stored} values)")

            time.sleep(0.5)  # Rate limiting

    print(f"\n=== Backfill Complete ===")
    print(f"Total values stored: {total_stored}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        print(f"First 5 errors: {total_errors[:5]}")

    return gaps


def run_assessment(conn):
    """Run NWP Direct Signal assessment with the expanded ECMWF dataset."""
    print("\n=== NWP Direct Signal Assessment (Post-Backfill) ===")

    c = conn.cursor()

    # Compare model coverage
    c.execute("""
        SELECT model, COUNT(DISTINCT fetch_date || target_date || station)
        FROM nwp_forecasts
        WHERE variable = 'temperature_2m_max'
        GROUP BY model
        ORDER BY COUNT(*) DESC
    """)
    print("\nCoverage after backfill:")
    for row in c.fetchall():
        print(f"  {row[0]}: {row[1]} unique combos")

    # ECMWF vs GFS forecast counts by station
    print("\nECMWF vs GFS forecast days (temperature_2m_max) by station:")
    c.execute("""
        SELECT
            e.station,
            e.forecast_days as ecmwf_days,
            g.forecast_days as gfs_days,
            ROUND(100.0 * e.forecast_days / MAX(g.forecast_days, 1), 1) as pct
        FROM (
            SELECT station, COUNT(DISTINCT target_date) as forecast_days
            FROM nwp_forecasts
            WHERE model='ecmwf' AND variable='temperature_2m_max'
            GROUP BY station
        ) e
        JOIN (
            SELECT station, COUNT(DISTINCT target_date) as forecast_days
            FROM nwp_forecasts
            WHERE model='gfs' AND variable='temperature_2m_max'
            GROUP BY station
        ) g ON e.station = g.station
        ORDER BY pct
    """)
    for row in c.fetchall():
        print(f"  {row[0]}: ECMWF={row[1]} days, GFS={row[2]} days ({row[3]}%)")

    # Direction agreement between ECMWF and GFS
    print("\nECMWF-GFS directional agreement (temperature_2m_max trend):")
    c.execute("""
        SELECT
            ROUND(AVG(CASE WHEN e.value > g.value AND s.prev_value IS NOT NULL
                           AND s.prev_value < s.current_value THEN 1.0
                           WHEN e.value < g.value AND s.prev_value IS NOT NULL
                           AND s.prev_value > s.current_value THEN 1.0
                           ELSE 0.0 END), 3) as agreement
        FROM nwp_forecasts e
        JOIN nwp_forecasts g
            ON e.target_date = g.target_date
            AND e.station = g.station
            AND e.variable = g.variable
        LEFT JOIN (
            SELECT station, target_date,
                   value as current_value,
                   LAG(value) OVER (PARTITION BY station ORDER BY target_date) as prev_value
            FROM nwp_forecasts
            WHERE model='gfs' AND variable='temperature_2m_max'
        ) s ON e.station = s.station AND e.target_date = s.target_date
        WHERE e.model='ecmwf' AND g.model='gfs'
          AND e.variable='temperature_2m_max'
          AND e.value IS NOT NULL AND g.value IS NOT NULL
    """)
    row = c.fetchone()
    if row and row[0] is not None:
        print(f"  Directional agreement: {row[0]*100:.1f}%")
    else:
        print("  Insufficient overlapping data for agreement calculation")


def main():
    args = parse_args()
    db_path = get_db_path(args, os.getcwd())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"ECMWF Backfill & Assessment — {datetime.now(timezone.utc).isoformat()}")
    print(f"Database: {db_path}")

    conn = sqlite3.connect(db_path)

    # Pre-backfill analysis
    gaps = analyze_gaps(conn)
    print(f"\nPre-backfill analysis:")
    print(f"  ECMWF fetch days: {gaps['ecmwf_total_fetch_days']}")
    print(f"  GFS fetch days:   {gaps['gfs_total_fetch_days']}")
    print(f"  Missing dates:    {len(gaps['missing_dates'])}")
    if gaps['missing_dates'][:5]:
        print(f"  Sample missing:   {gaps['missing_dates'][:5]}")
    print(f"  Extra ECMWF dates (not in GFS): {len(gaps['extra_dates'])}")

    # Backfill
    if not args.skip_backfill:
        gaps = run_backfill(conn, args)
    else:
        print("\nSkipping backfill (--skip-backfill)")

    # Post-backfill assessment
    if not args.skip_assessment:
        # Re-analyze after backfill
        post_gaps = analyze_gaps(conn)
        print(f"\nPost-backfill analysis:")
        print(f"  ECMWF fetch days: {post_gaps['ecmwf_total_fetch_days']}")
        print(f"  Missing dates:    {len(post_gaps['missing_dates'])}")

        run_assessment(conn)
    else:
        print("\nSkipping assessment (--skip-assessment)")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
