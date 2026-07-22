#!/usr/bin/env python3
"""
Extended NWP Forecast Collection Script
Adds support for ensemble forecasts and new model types to nwp_collect.py
Based on the original nwp_collect.py script but enhanced with additional data sources
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
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"  # Relative to project root (parent of scripts/)

# Determine the script directory and project root
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # weather-engine-source/

# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Extended NWP Forecast Collection")
    parser.add_argument("--db-path", help="Database file path (default: data/nwp_forecasts.db)")
    parser.add_argument("--only-ensemble", action="store_true", help="Only collect ensemble data")
    return parser.parse_args()

# Get database path from args, then env, then default
args = parse_args()

if args.db_path:
    DB_PATH = Path(args.db_path).absolute()
elif os.environ.get('NWP_DB_PATH'):
    DB_PATH = Path(os.environ['NWP_DB_PATH']).absolute()
else:
    DB_PATH = PROJECT_ROOT / NWP_DB_PATH_DEFAULT

# Convert to absolute path
DB_PATH = Path(DB_PATH).absolute()

# Ensure parent directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

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

# Extended NWP models - Including ensemble and other available sources
# Note: Models that return null are excluded for now as they're not available
MODELS = [
    # (db_name, api_endpoint, model_param)
    ("gfs",           "https://api.open-meteo.com/v1/gfs", None),
    ("ecmwf",         "https://api.open-meteo.com/v1/ecmwf", None),
    ("icon",          "https://api.open-meteo.com/v1/dwd-icon", None),
    ("gem",           "https://api.open-meteo.com/v1/gem", None),
]

FORECAST_VARIABLES = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_850hPa",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_speed_850hPa",
    "wind_direction_850hPa",
    "cloud_cover",
    "dew_point_2m",
    "geopotential_height_500hPa",
    "precipitation_sum",
]


def init_db(db_path):
    """Initialize the NWP forecasts database."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Ensure member_index column exists for ensemble forecasts
    c.execute("PRAGMA table_info(nwp_forecasts)")
    columns = [column[1] for column in c.fetchall()]
    if 'member_index' not in columns:
        c.execute("ALTER TABLE nwp_forecasts ADD COLUMN member_index INTEGER DEFAULT NULL")

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
            member_index INTEGER DEFAULT NULL
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_lookup
        ON nwp_forecasts(target_date, station, model, variable)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_ensemble_lookup
        ON nwp_forecasts(target_date, station, model, variable, member_index)
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

    conn.commit()
    return conn


def fetch_forecast_with_model(api_url, model_param, lat, lon):
    """Fetch single model forecast with specific model parameter if needed."""
    daily_vars = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
    hourly_vars = [
        "temperature_850hPa",
        "wind_speed_10m",
        "wind_direction_10m",
        "wind_speed_850hPa",
        "wind_direction_850hPa",
        "cloud_cover",
        "dew_point_2m",
        "geopotential_height_500hPa",
    ]

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(daily_vars),
        "hourly": ",".join(hourly_vars),
        "timezone": "UTC",
        "forecast_days": 7,
    }
    
    # Add model parameter if specified
    if model_param:
        params["models"] = model_param

    url = api_url + "?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(5)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                pass
        return {"error": f"HTTP {e.code}: {e.reason}", "url": url}
    except urllib.error.URLError as e:
        time.sleep(2)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {"error": f"URL error: {e.reason}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


def fetch_ensemble_forecast(lat, lon):
    """Fetch ensemble forecast from the special ensemble endpoint."""
    daily_vars = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(daily_vars),
        "timezone": "UTC",
        "forecast_days": 7
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
            time.sleep(10)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e2:
                return {"error": f"Retry error after HTTP 429: {e2}", "url": url}
        return {"error": f"HTTP {e.code}: {e.reason}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


def parse_and_store_forecast(conn, data, model_name, station, fetch_date, fetch_timestamp):
    """Parse non-ensemble Open-Meteo response and store forecasts in DB."""
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

    for var in ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]:
        values = daily.get(var, [])
        for i, date_str in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp, member_index)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """, (fetch_date, date_str, station, model_name, var, float(values[i]), fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {date_str}): {e}")

    # Process hourly variables — aggregate to daily mean
    hourly = data.get("hourly", {})
    hourly_time = hourly.get("time", [])

    for var in ["temperature_850hPa", "wind_speed_10m", "wind_direction_10m",
                "wind_speed_850hPa", "wind_direction_850hPa",
                "cloud_cover", "dew_point_2m", "geopotential_height_500hPa"]:
        values = hourly.get(var, [])
        if not values:
            continue

        daily_means = {}
        for i, ts in enumerate(hourly_time):
            if i < len(values) and values[i] is not None:
                date = str(ts)[:10]  # Extract date portion
                if date not in daily_means:
                    daily_means[date] = []
                daily_means[date].append(float(values[i]))

        for target_date, vals in daily_means.items():
            if vals:
                daily_mean = sum(vals) / len(vals)
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp, member_index)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """, (fetch_date, target_date, station, model_name,
                          f"{var}_daily_mean", daily_mean, fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    conn.commit()
    return stored, errors


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


def fetch_and_store_ensemble_data(conn, fetch_date, fetch_timestamp, total_stored, total_errors):
    """Specifically fetch and store ensemble data for all stations."""
    print("\n--- Processing Ensemble Forecasts (HGEFS/GFS Ensemble) ---")
    
    ensemble_stored = 0
    ensemble_errors = []

    for station, city_name, lat, lon in CITIES:
        print(f"  {station} ({city_name})...", end=" ", flush=True)

        data = fetch_ensemble_forecast(lat, lon)

        if "error" in data:
            print(f"ERROR: {data['error']}")
            ensemble_errors.append(f"{station}: {data['error']}")
            time.sleep(1)
            continue

        stored, errors = parse_and_store_ensemble(
            conn, data, station, fetch_date, fetch_timestamp)
        ensemble_stored += stored
        ensemble_errors.extend(errors)
        print(f"OK ({stored} values)")

        # Be respectful to API rate limits
        time.sleep(0.5)

    total_stored += ensemble_stored
    total_errors.extend(ensemble_errors)

    print(f"\n  Ensemble: {ensemble_stored} values stored, {len(ensemble_errors)} errors")
    return total_stored, total_errors


def main():
    print(f"NWP Forecast Collection — {datetime.now(timezone.utc).isoformat()}")
    print(f"Database: {DB_PATH}")
    print()

    conn = init_db(DB_PATH)

    fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    total_stored = 0
    total_errors = []

    if args.only_ensemble:
        # Only run ensemble collection
        total_stored, total_errors = fetch_and_store_ensemble_data(
            conn, fetch_date, fetch_timestamp, total_stored, total_errors)
    else:
        # Standard processing for regular models
        for model_name, api_url, model_param in MODELS:
            print(f"\n--- Model: {model_name} (endpoint: {api_url}) --- {f'model={model_param}' if model_param else ''}")
            model_stored = 0
            model_errors = []
            stations_ok = 0

            for station, city_name, lat, lon in CITIES:
                print(f"  {station} ({city_name})...", end=" ", flush=True)

                data = fetch_forecast_with_model(api_url, model_param, lat, lon)

                if "error" in data:
                    print(f"ERROR: {data['error']}")
                    model_errors.append(f"{station}: {data['error']}")
                    time.sleep(1)
                    continue

                stored, errors = parse_and_store_forecast(
                    conn, data, model_name, station, fetch_date, fetch_timestamp)
                model_stored += stored
                model_errors.extend(errors)
                stations_ok += 1
                print(f"OK ({stored} values)")

                # Rate limiting between API calls
                time.sleep(0.5)

            total_stored += model_stored
            total_errors.extend(model_errors)

            c = conn.cursor()
            c.execute("""
                INSERT INTO fetch_log
                (fetch_date, model, stations_fetched, variables_fetched, errors, fetch_timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (fetch_date, model_name, stations_ok, model_stored,
                  "; ".join(model_errors[:10]) if model_errors else None, fetch_timestamp))
            conn.commit()

            print(f"  Model {model_name}: {stations_ok} stations, "
                  f"{model_stored} values stored, {len(model_errors)} errors")

        # Then collect ensemble data as well
        total_stored, total_errors = fetch_and_store_ensemble_data(
            conn, fetch_date, fetch_timestamp, total_stored, total_errors)

    print(f"\n=== COLLECTION COMPLETE ===")
    print(f"Total values stored: {total_stored}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        print(f"First 5 errors: {total_errors[:5]}")

    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM nwp_forecasts")
    total_rows = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT fetch_date) FROM nwp_forecasts")
    fetch_days = c.fetchone()[0]
    c.execute("SELECT MIN(fetch_date), MAX(fetch_date) FROM nwp_forecasts")
    date_range = c.fetchone()
    print(f"\nDatabase stats: {total_rows} rows, {fetch_days} fetch days, "
          f"range: {date_range[0]} to {date_range[1]}")

    # Verify model differentiation with ensemble included
    c.execute("SELECT model, COUNT(DISTINCT value) FROM nwp_forecasts WHERE variable='temperature_2m_max' GROUP BY model")
    print("\nModel differentiation check (distinct temp_2m_max values per model):")
    for row in c.fetchall():
        print(f"  {row[0]}: {row[1]} distinct values")

    # Check ensemble statistics separately
    c.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE model='ensemble'")
    ensemble_count = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT member_index) FROM nwp_forecasts WHERE model='ensemble' AND member_index IS NOT NULL")
    member_count = c.fetchone()[0]
    print(f"\nEnsemble stats: {ensemble_count} entries, {member_count} distinct members")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()