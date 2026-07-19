#!/usr/bin/env python3
"""
ERA5 Upper-Air Field Backfill for NWP Analog Signal

Backfills ERA5 temperature_850hPa and geopotential_height_500hPa fields 
for historical dates (2020-01-01 to 2026-06-01) to enable meaningful 
NWP k-NN analog signal operation.

Collects data from Open-Meteo Archive API and stores in the existing 
nwp_forecasts.db database in the same schema as forecast data.

Rate limits to stay under 10,000 requests/day cap.
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import os
from pathlib import Path
from datetime import datetime, timedelta
import argparse 

# Configuration: Database path can be set via environment variable or command-line argument
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"  

# Determine the script directory and project root
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # weather-engine-source/

# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="ERA5 Upper-Air Backfill")
    parser.add_argument("--db-path", help="Database file path (default: data/nwp_forecasts.db)")
    parser.add_argument("--start-date", default="2020-01-01", 
                        help="Start date (YYYY-MM-DD, default: 2020-01-01)")
    parser.add_argument("--end-date", default="2026-06-01",
                        help="End date (YYYY-MM-DD, default: 2026-06-01)")  
    parser.add_argument("--station", help="Specific station code (e.g., KNYC) to fill")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Number of days per batch (default: 50)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Delay between requests seconds (default: 0.2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    return parser.parse_args()

# Get database path from args, then env, then default
args = parse_args()

if args.db_path:
    DB_PATH = Path(args.db_path).absolute()
elif os.environ.get('NWP_DB_PATH'):
    DB_PATH = Path(os.environ['NWP_DB_PATH']).absolute()
else:
    DB_PATH = PROJECT_ROOT / NWP_DB_PATH_DEFAULT

START_DATE = datetime.strptime(args.start_date, "%Y-%m-%d").date()
END_DATE = datetime.strptime(args.end_date, "%Y-%m-%d").date()

# All 20 Kalshi cities — VERIFIED against live Kalshi API 2026-07-02
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

# Filter to single station if specified
if args.station:
    CITIES = [c for c in CITIES if c[0] == args.station]
    if not CITIES:
        print(f"Error: Station {args.station} not found")
        exit(1)

UPPER_AIR_VARIABLES = [
    "temperature_850hPa",
    "geopotential_height_500hPa",
]

def init_db(db_path):
    """Initialize the NWP forecasts database and verify table structure."""
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

    # Add fetch_log table if it doesn't exist
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

    conn.commit()
    return conn
    
def fetch_era5_archive(lat, lon, start_date, end_date):
    """Fetch ERA5 archive data for specific location/date range.
    
    Args:
        lat, lon: Location coordinates
        start_date, end_date: Date range (as ISO format strings)
    
    Returns:
        Response data dict or None on error
    """
    hourly_vars = UPPER_AIR_VARIABLES.copy()

    # Construct archive API URL - uses Open-Meteo Archive API
    url_params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ",".join(hourly_vars),
        "timezone": "UTC"
    }

    url = "https://archive-api.open-meteo.com/v1/archive?" + "&".join(
        f"{k}={v}" for k, v in url_params.items()
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        response = urllib.request.urlopen(req, timeout=30)
        data = json.loads(response.read().decode("utf-8"))

        # If hourly section exists and has actual data
        if "hourly" in data and "time" in data["hourly"] and data["hourly"]["time"]:
            return data
            
        print(f"No data found: {lat}, {lon}, {start_date}-{end_date}")
        return None
        
    except urllib.error.HTTPError as e:
        if e.code == 429:  # Rate limit exceeded
            print(f"Rate limit hit. Response: {e}")
            time.sleep(10)  # Wait longer for rate limit reset
            return fetch_era5_archive(lat, lon, start_date, end_date)  # Retry
        else:
            print(f"HTTP Error {e.code}: {e.reason} at latitude {lat}, longitude {lon}")
            return None
    except urllib.error.URLError as e:
        print(f"URL Error fetching archive data: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"JSON decode error for {lat}, {lon}: {e}")
        return None
    except Exception as e:
        print(f"Generic error fetching archive for {lat}, {lon}: {e}")
        return None


def store_archive_data(conn, data, model_name, station, fetch_date, fetch_timestamp):
    """Parse archive response and store hourly values for upper-air fields."""
    if not data or "hourly" not in data:
        return 0, []

    c = conn.cursor()
    hourly = data.get("hourly", {})
    hourly_time = hourly.get("time", [])
    
    if not hourly_time:
        return 0, ["No hourly timestamps found after parsing response"]

    stored = 0
    errors = []

    # Process hourly variables
    for var_name in UPPER_AIR_VARIABLES:
        values = hourly.get(var_name, [])
        if not values:
            continue

        # Group hourly values by date to calculate daily mean
        daily_aggregates = {}
        for i, ts in enumerate(hourly_time):
            if i < len(values) and values[i] is not None:
                date_key = ts.split("T")[0]  # Extract date from "YYYY-MM-DDTHH:MM"
                if date_key not in daily_aggregates:
                    daily_aggregates[date_key] = []
                daily_aggregates[date_key].append(float(values[i]))

        # Store daily means
        for target_date, var_values in daily_aggregates.items():
            if var_values:
                daily_mean = sum(var_values) / len(var_values)
                var_field_name = f"{var_name}_daily_mean"                
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (fetch_date, target_date, station, model_name,
                          var_field_name, daily_mean, fetch_timestamp))
                    stored += 1
                except Exception as e:
                    error_msg = f"DB error ({var_field_name}, {target_date}): {e}"
                    errors.append(error_msg)

    conn.commit()
    return stored, errors


def batch_dates(start_date, end_date, batch_size):
    """Split date range into batches of specified size."""
    current = start_date
    batches = []
    
    while current <= end_date:
        batch_end = min(current + timedelta(days=batch_size - 1), end_date)
        batches.append((current, batch_end))
        current = batch_end + timedelta(days=1)
        
    return batches


def get_existing_era5_data(conn, station, start, end):
    """Check for already filled ERA5 upper-air dates for this station."""
    c = conn.cursor()
    
    # Check which dates already have upper-air fields for this station
    placeholders = ','.join('?' * len(UPPER_AIR_VARIABLES))
    c.execute(f"""
        SELECT DISTINCT target_date, variable 
        FROM nwp_forecasts 
        WHERE station=? AND model='era5' 
        AND target_date BETWEEN ? AND ?
        AND variable IN ({placeholders})
    """, (station, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')) + tuple(f"{var}_daily_mean" for var in UPPER_AIR_VARIABLES))
    
    existing_entries = set()
    for date, var in c.fetchall():
        existing_entries.add(date)
    
    return existing_entries


def main():
    print(f"ERA5 Upper-Air Backfill — {datetime.now()}")
    print(f"Database: {DB_PATH}")
    print(f"Date Range: {args.start_date} to {args.end_date}")
    print(f"Batch Size: {args.batch_size} days")
    print(f"Stations: {[c[0] for c in CITIES]}")
    
    if args.dry_run:
        print("** DRY RUN MODE **")
    
    if not args.dry_run:
        conn = init_db(DB_PATH)
    
    fetch_date = datetime.now().strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now().isoformat()
    
    total_stored = 0
    total_errors = []
    
    # Compute batches
    batches = batch_dates(START_DATE, END_DATE, args.batch_size)
    print(f"\nProcessing {len(batches)} date batches...")
    
    batch_count = 0
    for start_batch, end_batch in batches:
        # Format as YYYY-MM-DD
        start_str = start_batch.strftime("%Y-%m-%d")
        end_str = end_batch.strftime("%Y-%m-%d")
        batch_count += 1
        print(f"\nBatch {batch_count}/{len(batches)}: {start_str} to {end_str}")
        
        for station_code, station_name, lat, lon in CITIES:
            print(f"  Station: {station_code} ({station_name})", end="... ", flush=True)

            # Skip if dry run
            if args.dry_run:
                print("(DRY-RUN)")
                continue
                
            # Check what already exists
            existing_dates = get_existing_era5_data(conn, station_code, start_batch, end_batch) 
            if len(existing_dates) > 0:
                print(f"({len(existing_dates)} dates already exist, skipping)")
                continue

            # Fetch and store data for this station/batch
            data = fetch_era5_archive(lat, lon, start_str, end_str)
            
            if data:
                stored, errors = store_archive_data(
                    conn, data, "era5", station_code, fetch_date, fetch_timestamp)
                print(f"{stored} values stored, {len(errors)} errors")
                total_stored += stored
                total_errors.extend(errors)
                
                # Show errors if any occurred
                if errors:
                    for err in errors[:3]:  # Show first 3 errors
                        print(f"    Error: {err}")
            else:
                print("No data")
            
            # Respect rate limits
            time.sleep(args.delay)
    
    if not args.dry_run:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE model='era5'")
        total_era5_rows = c.fetchone()[0]
        conn.close()
    
        print(f"\n=== BACKFILL COMPLETE ===")
        print(f"Total values stored: {total_stored}")
        print(f"Total errors: {len(total_errors)}")
        print(f"After this run, ERA5 has: {total_era5_rows} total rows in DB")
        
        if total_errors:
            print(f"Sample errors: {total_errors[:5]}")


if __name__ == "__main__":
    main()