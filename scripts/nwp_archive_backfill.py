#!/usr/bin/env python3
"""
NWP Historical Backfill — Archive API (ERA5 Reanalysis)

Backfills historical NWP data from Open-Meteo's ARCHIVE API.
The archive API provides ERA5 reanalysis data going back years, which overlaps
with our settlement_epochs data (ends 2025-08-27).

The live forecast API only goes back ~92 days. The archive API is separate
and has data from 1940 onward.

NOTE: ERA5 reanalysis = what actually happened (not forecast). For analog
matching purposes, this is the weather feature data we need. The NWP analog
ensemble uses these features to find similar historical days.

Usage:
    python3 scripts/nwp_archive_backfill.py --start 2025-05-01 --end 2025-08-27
    python3 scripts/nwp_archive_backfill.py --start 2025-01-01 --end 2025-08-27 --resume
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

# Configuration: Database path can be set via environment variable or command-line argument
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"  # Relative to script execution directory

# Determine the script directory
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[1]

# Parse command line arguments
parser = argparse.ArgumentParser(description='NWP Archive Historical Backfill', add_help=False)
parser.add_argument('--db-path', help='Database file path (default: data/nwp_forecasts.db)')
parser.add_argument('--start', help='Start date (YYY-MM-DD)')
parser.add_argument('--end', help='End date (YYY-MM-DD)')
parser.add_argument('--resume', action='store_true', help='Resume from last successful day')

# Parse known args
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

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = [
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
    "dew_point_2m",
    "geopotential_height_500hPa",
    "temperature_850hPa",
]

DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]

# Match the existing schema variables
VAR_MAPPING = {
    # daily vars stored directly
    "temperature_2m_max": "temperature_2m_max",
    "temperature_2m_min": "temperature_2m_min",
    "precipitation_sum": "precipitation_sum",
    # hourly vars stored as daily mean
    "temperature_850hPa": "temperature_850hPa_daily_mean",
    "wind_speed_10m": "wind_speed_10m_daily_mean",
    "wind_direction_10m": "wind_direction_10m_daily_mean",
    "cloud_cover": "cloud_cover_daily_mean",
    "dew_point_2m": "dew_point_2m_daily_mean",
    "geopotential_height_500hPa": "geopotential_height_500hPa_daily_mean",
}


def fetch_archive(lat, lon, start_date, end_date, max_retries=3):
    """Fetch historical data from Open-Meteo Archive API (ERA5 reanalysis)."""
    hourly_str = ",".join(HOURLY_VARS)
    daily_str = ",".join(DAILY_VARS)
    
    url = (
        f"{ARCHIVE_URL}?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&hourly={hourly_str}"
        f"&daily={daily_str}"
        f"&timezone=UTC"
    )
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherEngine/1.0"})
            resp = urllib.request.urlopen(req, timeout=60)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(60, 10 * (attempt + 1))
                print(f"  Rate limited, waiting {wait}s...", end=" ", flush=True)
                time.sleep(wait)
            elif e.code == 400:
                print(f"  BAD REQUEST: {e.read().decode()[:200]}")
                return None
            else:
                print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(5)
        except Exception as e:
            print(f"  Error: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    return None


def store_data(conn, station, data):
    """Store fetched data into nwp_forecasts table using existing long-format schema."""
    if not data:
        return 0
    
    daily = data.get("daily", {})
    hourly = data.get("hourly", {})
    
    daily_time = daily.get("time", [])
    daily_vars = {
        "temperature_2m_max": daily.get("temperature_2m_max", []),
        "temperature_2m_min": daily.get("temperature_2m_min", []),
        "precipitation_sum": daily.get("precipitation_sum", []),
    }
    
    hourly_time = hourly.get("time", [])
    hourly_vars = {
        "temperature_850hPa": hourly.get("temperature_850hPa", []),
        "wind_speed_10m": hourly.get("wind_speed_10m", []),
        "wind_direction_10m": hourly.get("wind_direction_10m", []),
        "cloud_cover": hourly.get("cloud_cover", []),
        "dew_point_2m": hourly.get("dew_point_2m", []),
        "geopotential_height_500hPa": hourly.get("geopotential_height_500hPa", []),
    }
    
    # Aggregate hourly to daily means
    hourly_by_date = {}
    for i, ts in enumerate(hourly_time):
        date = ts[:10]
        if date not in hourly_by_date:
            hourly_by_date[date] = {k: [] for k in hourly_vars}
        for var_name, values in hourly_vars.items():
            if i < len(values) and values[i] is not None:
                hourly_by_date[date][var_name].append(values[i])
    
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    
    for i, date in enumerate(daily_time):
        # Insert daily variables
        for var_name, values in daily_vars.items():
            val = values[i] if i < len(values) else None
            if val is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (date, date, station, "era5", var_name, val, now))
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
        
        # Insert hourly aggregates as daily mean variables
        h = hourly_by_date.get(date, {})
        for var_name, values_list in h.items():
            if values_list:
                daily_mean = sum(values_list) / len(values_list)
                db_var_name = VAR_MAPPING.get(var_name, f"{var_name}_daily_mean")
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (date, date, station, "era5", db_var_name, daily_mean, now))
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
    
    conn.commit()
    return inserted


def check_existing(conn, station, start_date, end_date):
    """Check if data already exists for this station/date range."""
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(DISTINCT target_date) FROM nwp_forecasts
        WHERE station=? AND model='era5' AND target_date BETWEEN ? AND ?
    """, (station, start_date, end_date))
    return c.fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description="NWP Historical Archive Backfill (ERA5)")
    parser.add_argument("--start", default="2025-05-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-08-27", help="End date YYYY-MM-DD")
    parser.add_argument("--resume", action="store_true", help="Skip already-completed stations")
    parser.add_argument("--stations", type=str, help="Comma-separated station codes (default: all)")
    args = parser.parse_args()
    
    start_date = args.start
    end_date = args.end
    
    cities = CITIES
    if args.stations:
        wanted = set(args.stations.split(","))
        cities = [c for c in CITIES if c[0] in wanted]
    
    expected_days = (datetime.strptime(end_date, "%Y-%m-%d") - 
                    datetime.strptime(start_date, "%Y-%m-%d")).days + 1
    
    print(f"NWP Archive Backfill (ERA5) — {start_date} to {end_date}")
    print(f"Stations: {len(cities)} | Expected days per station: {expected_days}")
    print(f"Database: {DB_PATH}")
    print()
    
    conn = sqlite3.connect(DB_PATH)
    # Ensure table exists (it should from the existing script)
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_nwp_station_target ON nwp_forecasts(station, target_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_nwp_station_model_target ON nwp_forecasts(station, model, target_date)")
    conn.commit()
    
    total_inserted = 0
    total_skipped = 0
    errors = []
    
    for idx, (station, city_name, lat, lon) in enumerate(cities):
        if args.resume:
            existing = check_existing(conn, station, start_date, end_date)
            if existing >= expected_days * 0.9:
                print(f"[{idx+1}/{len(cities)}] {station} ({city_name}): SKIP ({existing}/{expected_days} days exist)")
                total_skipped += existing
                continue
        
        print(f"[{idx+1}/{len(cities)}] {station} ({city_name}): fetching...", end=" ", flush=True)
        
        data = fetch_archive(lat, lon, start_date, end_date)
        if data:
            inserted = store_data(conn, station, data)
            print(f"OK ({inserted} records)")
            total_inserted += inserted
        else:
            print("FAILED")
            errors.append(station)
        
        # Rate limit: 1 request per second
        time.sleep(1.0)
    
    conn.close()
    
    print()
    print(f"Done: {total_inserted} records inserted, {total_skipped} skipped")
    if errors:
        print(f"Errors: {errors}")
    
    # Verify overlap with settlement data
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT MIN(target_date), MAX(target_date), COUNT(DISTINCT target_date) 
        FROM nwp_forecasts WHERE model='era5'
    """)
    nwp_range = c.fetchone()
    print(f"\nNWP era5 data: {nwp_range[0]} to {nwp_range[1]}, {nwp_range[2]} dates")
    
    # Check overlap with settlement
    metar_db = str(REPO_ROOT / "data" / "metar_backfill.db")
    c.execute("ATTACH DATABASE ? AS metar", (metar_db,))
    c.execute("""
        SELECT MIN(m.local_trading_date), MAX(m.local_trading_date), COUNT(DISTINCT m.local_trading_date)
        FROM nwp_forecasts n
        JOIN metar.settlement_epochs m ON n.target_date = m.local_trading_date
        WHERE n.model='era5' AND m.epoch_status='closed'
        AND m.station = n.station
    """)
    overlap = c.fetchone()
    print(f"Overlap with settlement: {overlap[0]} to {overlap[1]}, {overlap[2]} overlapping dates")
    conn.close()
    
    print("\nBackfill complete!")


if __name__ == "__main__":
    main()
