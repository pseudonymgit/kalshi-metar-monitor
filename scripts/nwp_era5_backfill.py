#!/usr/bin/env python3
"""
ERA5 Historical Backfill Script (2025-05-01 → 2025-08-27)

Backfills ERA5 reanalysis data from Open-Meteo Archive API for all 20 Kalshi stations.
This provides historical atmospheric data that overlaps with the settlement_epochs
data (which ends 2025-08-27), enabling the NWP analog ensemble to function.

⚠️  STANDALONE SCRIPT — DO NOT RUN VIA AI/AGENT.
    Run manually: python3 scripts/nwp_era5_backfill.py

Data stored to: prototypes/weather-engine-source/data/nwp_forecasts.db
Model label: 'era5' (to distinguish from GFS/ECMWF/ICON/GEM forecast data)

Version: v1.0 2026-07-06
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import sys
import argparse
import os
from pathlib import Path
from datetime import datetime, timezone

# Configuration: Database path can be set via environment variable or CLI argument
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"  # Relative to script execution directory

# Determine the script directory
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[1]

# Parse command line arguments for optional db-path
parser = argparse.ArgumentParser(description='ERA5 Historical Backfill', add_help=False)
parser.add_argument('--db-path', help='Database file path (default: data/nwp_forecasts.db)')

# Parse only known args
args, _ = parser.parse_known_args()

if args.db_path:
    DB_PATH = Path(args.db_path).absolute()
elif os.environ.get('NWP_DB_PATH'):
    DB_PATH = Path(os.environ['NWP_DB_PATH']).absolute()
else:
    DB_PATH = SCRIPT_DIR / NWP_DB_PATH_DEFAULT

# Convert to absolute path and ensure directory exists
DB_PATH = Path(DB_PATH).absolute()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# All 20 Kalshi cities: (ICAO, City, lat, lon)
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

# ERA5 Archive API endpoint
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Date range for backfill
START_DATE = "2025-05-01"
END_DATE = "2025-08-27"

# Daily and hourly variables matching the NWP schema
DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
HOURLY_VARS = [
    "temperature_850hPa",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
    "dew_point_2m",
    "geopotential_height_500hPa",
]

SCRIPT_VERSION = "v1.0"
MODEL_NAME = "era5"


def init_db(db_path):
    """Initialize DB schema (same as nwp_backfill_30d.py)."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS nwp_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            station TEXT NOT NULL,
            model TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            fetch_timestamp TEXT NOT NULL,
            UNIQUE(fetch_date, target_date, station, model, variable)
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_lookup
        ON nwp_forecasts(target_date, station, model, variable)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_fetch_date
        ON nwp_forecasts(fetch_date, model)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            model TEXT NOT NULL,
            stations_fetched INTEGER,
            variables_fetched INTEGER,
            errors TEXT,
            fetch_timestamp TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS backfill_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            station TEXT NOT NULL,
            status TEXT NOT NULL,
            attempted_at TEXT,
            completed_at TEXT,
            error_msg TEXT,
            UNIQUE(model, station)
        )
    """)

    conn.commit()
    return conn


def get_resume_state(conn, model_name):
    """Check which stations are already done for this model."""
    c = conn.cursor()
    c.execute(
        "SELECT station, status FROM backfill_progress WHERE model = ? AND status = 'complete'",
        (model_name,)
    )
    done = set()
    for row in c.fetchall():
        done.add(row[0])
    return done


def mark_progress(conn, model, station, status, error_msg=None):
    """Record progress for resume capability."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT OR REPLACE INTO backfill_progress
        (model, station, status, attempted_at, completed_at, error_msg)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        model, station, status,
        now, now if status == 'complete' else None,
        error_msg
    ))
    conn.commit()


def fetch_era5(lat, lon, start_date, end_date, max_retries=3):
    """Fetch ERA5 reanalysis data from Open-Meteo Archive API."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
    }
    url = ARCHIVE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"OpenClaw-WeatherEngine/{SCRIPT_VERSION}"
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"    Rate limited (429), waiting {wait}s... (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            body = e.read().decode()[:500]
            return {"error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 3 * (attempt + 1)
                print(f"    Retry {attempt+1}/{max_retries}: {e}, waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": str(e)}

    return {"error": "Max retries exceeded"}


def store_forecast(conn, data, station, fetch_timestamp):
    """Parse and store ERA5 data in DB, matching the NWP schema."""
    c = conn.cursor()
    stored = 0
    errors = []

    if "error" in data:
        return 0, [data["error"]]

    if "daily" not in data and "hourly" not in data:
        return 0, ["No forecast data in response"]

    # Process daily variables
    daily = data.get("daily", {})
    daily_time = daily.get("time", [])

    for var in DAILY_VARS:
        values = daily.get(var, [])
        for i, target_date in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (START_DATE, target_date, station, MODEL_NAME,
                          var, float(values[i]), fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    # Process hourly variables — aggregate to daily mean (matching nwp_backfill_30d.py approach)
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
                    """, (START_DATE, target_date, station, MODEL_NAME,
                          f"{var}_daily_mean", daily_mean, fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    conn.commit()
    return stored, errors


def main():
    now = datetime.now(timezone.utc)
    print(f"ERA5 Historical Backfill {SCRIPT_VERSION} — {now.isoformat()}")
    print(f"Date range: {START_DATE} → {END_DATE}")
    print(f"Database: {DB_PATH}")
    print(f"Model label: {MODEL_NAME}")
    print(f"Stations: {len(CITIES)}")
    print()

    conn = init_db(DB_PATH)

    done_set = get_resume_state(conn, MODEL_NAME)
    if done_set:
        print(f"Skipping {len(done_set)} already-complete stations")

    fetch_timestamp = now.isoformat()
    total_stored = 0
    total_errors = []
    stations_ok = 0

    for idx, (station, city_name, lat, lon) in enumerate(CITIES, 1):
        if station in done_set:
            print(f"  [{idx}/{len(CITIES)}] {station} ({city_name})... SKIPPED (already complete)")
            continue

        print(f"  [{idx}/{len(CITIES)}] {station} ({city_name})...", end=" ", flush=True)

        mark_progress(conn, MODEL_NAME, station, 'pending')

        data = fetch_era5(lat, lon, START_DATE, END_DATE)

        if "error" in data:
            print(f"ERROR: {data['error']}")
            total_errors.append(f"{station}: {data['error']}")
            mark_progress(conn, MODEL_NAME, station, 'failed', data['error'])
            time.sleep(2)
            continue

        stored, errors = store_forecast(conn, data, station, fetch_timestamp)

        total_stored += stored
        total_errors.extend(errors)
        stations_ok += 1
        print(f"OK ({stored} values)")

        mark_progress(conn, MODEL_NAME, station, 'complete')

        # Rate limit — be nice to the API
        time.sleep(1.5)

    # Log to fetch_log
    c = conn.cursor()
    c.execute("""
        INSERT INTO fetch_log
        (fetch_date, model, stations_fetched, variables_fetched, errors, fetch_timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (fetch_timestamp[:10], MODEL_NAME, stations_ok, total_stored,
          "; ".join(total_errors[:10]) if total_errors else None, fetch_timestamp))
    conn.commit()

    print(f"\n{'='*60}")
    print(f"BACKFILL COMPLETE")
    print(f"{'='*60}")
    print(f"Total values stored: {total_stored}")
    print(f"Stations OK: {stations_ok}/{len(CITIES)}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        for err in total_errors[:5]:
            print(f"  • {err}")

    # Summary stats
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE model = ?", (MODEL_NAME,))
    total_rows = c.fetchone()[0]
    c.execute(
        "SELECT MIN(target_date), MAX(target_date) FROM nwp_forecasts WHERE model = ?",
        (MODEL_NAME,)
    )
    date_range = c.fetchone()

    print(f"\nERA5 data in database:")
    print(f"  Total rows: {total_rows}")
    print(f"  Date range: {date_range[0]} → {date_range[1]}")

    # Check overlap with settlement data
    metar_db = str(REPO_ROOT / "data" / "metar_backfill.db")
    try:
        conn2 = sqlite3.connect(metar_db)
        c2 = conn2.cursor()
        c2.execute("""
            SELECT MIN(local_trading_date), MAX(local_trading_date)
            FROM settlement_epochs WHERE epoch_status = 'closed'
        """)
        settle_range = c2.fetchone()
        print(f"\nSettlement data range: {settle_range[0]} → {settle_range[1]}")
        print(f"ERA5 range:           {date_range[0]} → {date_range[1]}")

        if date_range[0] and date_range[1] and settle_range[1]:
            if date_range[0] <= settle_range[1]:
                print(f"✅ OVERLAP CONFIRMED: ERA5 data ({date_range[0]} → {date_range[1]}) overlaps with settlement data (up to {settle_range[1]})")
            else:
                print(f"❌ NO OVERLAP: ERA5 data starts after settlement data ends")
        conn2.close()
    except Exception as e:
        print(f"Could not check settlement data: {e}")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
