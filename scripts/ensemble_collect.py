#!/usr/bin/env python3
"""
Ensemble Forecast Collection Script
Collects ensemble forecast data from Open-Meteo ensemble API for all Kalshi cities.

Specifically collects HGEFS/GFS Ensemble data with probability distributions.
Each ensemble member provides a potential outcome, creating confidence intervals.

The ensemble API returns multiple members for each variable:
- temperature_2m_max_member00, temperature_2m_max_member01, ..., temperature_2m_max_member30 (31 total)
- temperature_2m_min_member00, temperature_2m_min_member01, ..., temperature_2m_min_member30 (31 total)

Runs as a standalone cron job.
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Configuration: Database path can be set via environment variable or command-line argument
ENSEMBLE_DB_PATH_DEFAULT = "data/nwp_forecasts.db"

# Determine the script directory and project root
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # weather-engine-source/

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

def get_database_path(args_from_parser=None):
    """Get database path from args, then env, then default"""
    if args_from_parser and args_from_parser.db_path:
        db_path = Path(args_from_parser.db_path).absolute()
    elif os.environ.get('ENSEMBLE_DB_PATH'):
        db_path = Path(os.environ['ENSEMBLE_DB_PATH']).absolute()
    else:
        db_path = Path(__file__).resolve().parent.parent / ENSEMBLE_DB_PATH_DEFAULT

    # Convert to absolute path
    db_path = Path(db_path).absolute()
    
    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True) 
    
    return db_path


def init_ensemble_db(db_path):
    """Initialize the ensemble forecasts table in the NWP database if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Add member_index column to nwp_forecasts table for ensemble forecasts
    # First check if member_index column exists
    c.execute("PRAGMA table_info(nwp_forecasts)")
    columns = [column[1] for column in c.fetchall()]
    
    if 'member_index' not in columns:
        c.execute("ALTER TABLE nwp_forecasts ADD COLUMN member_index INTEGER DEFAULT NULL")
    
    # Create index for faster queries on ensemble data
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_ensemble_lookup
        ON nwp_forecasts(target_date, station, model, variable, member_index)
    """)

    conn.commit()
    return conn


def fetch_ensemble_forecast(lat, lon):
    """Fetch ensemble forecast data from Open-Meteo ensemble API.
    
    Args:
        lat, lon: Station coordinates
    
    Returns:
        dict: Response data from ensemble API
    """
    daily_vars = [
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum"
    ]
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(daily_vars),
        "timezone": "UTC",
        "forecast_days": 7,
    }

    api_url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    url = api_url + "?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  Rate limiter hit (429): sleeping 10 seconds")
            time.sleep(10)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e2:
                return {"error": f"Retry error after HTTP 429: {e2}", "url": url}
        return {"error": f"HTTP {e.code}: {e.reason}", "url": url}
    except urllib.error.URLError as e:
        time.sleep(2)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e2:
            return {"error": f"URL error: {e2.reason}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


def parse_and_store_ensemble(conn, data, station, fetch_date, fetch_timestamp):
    """Parse ensemble response and store forecasts in DB with member indices."""
    c = conn.cursor()
    stored = 0
    errors = []

    if "error" in data:
        return 0, [data["error"]]

    if "daily" not in data:
        return 0, ["No forecast data in response"]

    # Parse ensemble member data
    daily_data = data["daily"]
    time_data = daily_data.get("time", [])
    
    # Identify ensemble member variables - these look like:
    # temperature_2m_max_member00, temperature_2m_max_member01, ..., temperature_2m_max_member30
    ensemble_variables = []
    for key in daily_data.keys():
        if "_member" in key and key.lower().startswith(("temperature_", "precipitation_")):
            ensemble_variables.append(key)
    
    print(f"  Found {len(set(var.replace('_member', '|').split('|')[0] for var in ensemble_variables))} ensemble variables across ~{len(set(var.split('_member')[-1] for var in ensemble_variables))} members")
    
    for var_key in ensemble_variables:
        # Extract base variable name and member index
        # Example: temperature_2m_max_member05 -> base_var=temperature_2m_max, member_idx=5
        parts = var_key.split('_member')
        if len(parts) != 2:
            continue
            
        base_variable = parts[0]
        member_index_str = parts[1]  # This might be '05', '15', etc
        
        try:
            member_index = int(member_index_str)
        except ValueError:
            continue  # Skip malformed member index
        
        values = daily_data.get(var_key, [])
        
        # Insert each target date with its corresponding member value
        for i, target_date in enumerate(time_data):
            if i < len(values) and values[i] is not None:
                try:
                    val = float(values[i])
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp, member_index)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        fetch_date, target_date, station, "ensemble", 
                        base_variable, val, fetch_timestamp, member_index
                    ))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var_key}, member {member_index}, {target_date}): {e}")

    conn.commit()
    return stored, errors


def test_ensemble_api():
    """Test function to verify the ensemble API is accessible and returns valid data.
    
    Returns:
        tuple: (success: bool, test_data: dict)
    """
    print("Testing ensemble API connection...")
    
    # Use Atlanta latitude/longitude for testing
    sample_lat = 33.64
    sample_lon = -84.43
    
    daily_vars = ["temperature_2m_max", "temperature_2m_min"]
    
    params = {
        "latitude": sample_lat,
        "longitude": sample_lon,
        "daily": ",".join(daily_vars),
        "timezone": "UTC",
        "forecast_days": 3,
        # No models parameter needed for ensemble API
    }

    api_url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    url = api_url + "?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )
    
    print(f"Request: {url}")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, data
    except Exception as e:
        print(f"Test failed: {e}")
        return False, {"error": str(e)}


def collect_ensemble_data(db_path=None):
    """Main collection function"""
    if db_path is None:
        args = argparse.Namespace(db_path=None)  # Default case
        db_path = get_database_path(args)
    
    print(f"Ensemble Forecast Collection — {datetime.now(timezone.utc).isoformat()}")
    print(f"Database: {db_path}")
    print()

    conn = init_ensemble_db(db_path)

    fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    total_stored = 0
    total_errors = []

    print("--- Processing Ensemble Forecasts (HGEFS/GFS Ensemble) ---")
    
    for station, city_name, lat, lon in CITIES:
        print(f"  {station} ({city_name})...", end=" ", flush=True)

        data = fetch_ensemble_forecast(lat, lon)

        if "error" in data:
            print(f"ERROR: {data['error']}")
            total_errors.append(f"{station}: {data['error']}")
            time.sleep(1)
            continue

        stored, errors = parse_and_store_ensemble(
            conn, data, station, fetch_date, fetch_timestamp)
        total_stored += stored
        total_errors.extend(errors)
        print(f"OK ({stored} values)")

        # Be respectful to API rate limits
        time.sleep(0.75) 

    print(f"\n=== ENSEMBLE COLLECTION COMPLETE ===")
    print(f"Total ensemble values stored: {total_stored}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        print(f"First 5 errors: {total_errors[:5]}")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")
    
    return len(total_errors) == 0


def main():
    parser = argparse.ArgumentParser(description="Ensemble Forecast Collection")
    parser.add_argument("--test", action="store_true", help="Test API connection only")
    parser.add_argument("--db-path", help="Database file path (default: data/nwp_forecasts.db)")
    
    args = parser.parse_args()
    
    if args.test:
        success, test_data = test_ensemble_api()
        if success:
            print("\n=== Ensemble API Test Result ===")
            print("Success! Sample data retrieved.")
            
            # Show sample of available ensemble members
            print("\nAvailable daily variables (sample of first few):")
            if "daily" in test_data:
                daily_data = test_data["daily"]
                member_vars = [k for k in daily_data.keys() if "_member" in k]
                
                if member_vars:
                    print(f"  Found {len(member_vars)} ensemble member variables")
                    for var in sorted(member_vars)[:10]:  # Show first 10
                        values_sample = daily_data[var][:5] if len(daily_data[var]) > 5 else daily_data[var]
                        print(f"    {var}: {values_sample}")
                    
                    if len(member_vars) > 10:
                        print(f"    ... and {len(member_vars)-10} more")
                else:
                    print("  No ensemble member variables found in response")
                    print(f"  Available daily keys: {list(daily_data.keys()) if 'daily' in test_data else 'None'}")
            else:
                print("  No daily data in response")
        else:
            print(f"\n=== Ensemble API Test Failed ===")
        return success
    else:
        # Run the actual collection
        return collect_ensemble_data(get_database_path(args))


if __name__ == "__main__":
    main()