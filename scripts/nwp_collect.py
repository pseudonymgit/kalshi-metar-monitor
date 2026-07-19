#!/usr/bin/env python3
"""
NWP Forecast Collection Script
Collects daily forecast data from Open-Meteo for all Kalshi cities.

Runs as a standalone cron job — NO AGENT INVOLVED.
Designed to run daily at 06:00 UTC (after GFS 00Z cycle).

Data stored to: prototypes/weather-engine-source/data/nwp_forecasts.db

IMPORTANT (fix 2026-07-03): Open-Meteo requires model-specific API endpoints.
The generic /v1/forecast endpoint ignores the 'model' parameter and returns
identical data for all models. Each model must be fetched from its dedicated
endpoint: /v1/gfs, /v1/ecmwf, /v1/dwd-icon, /v1/gem.
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
    parser = argparse.ArgumentParser(description="NWP Forecast Collection")
    parser.add_argument("--db-path", help="Database file path (default: data/nwp_forecasts.db)")
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

# NWP models available on Open-Meteo (free, no API key)
# Each model must be fetched from its DEDICATED API endpoint.
# The generic /v1/forecast endpoint does NOT differentiate models.
MODELS = [
    # (db_name, api_endpoint)
    ("gfs",   "https://api.open-meteo.com/v1/gfs"),
    ("ecmwf", "https://api.open-meteo.com/v1/ecmwf"),
    ("icon",  "https://api.open-meteo.com/v1/dwd-icon"),
    ("gem",   "https://api.open-meteo.com/v1/gem"),
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


def fetch_forecast(api_url, lat, lon):
    """Fetch multi-day forecast from Open-Meteo for a single model and location.

    Args:
        api_url: Full API endpoint URL for the specific model
                 (e.g. https://api.open-meteo.com/v1/ecmwf)
        lat, lon: Station coordinates
    """
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
        # NOTE: 'model' param is NOT used — each model has its own API endpoint
    }

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


def parse_and_store(conn, data, model_name, station, fetch_date, fetch_timestamp):
    """Parse Open-Meteo response and store forecasts in DB."""
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
        for i, target_date in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (fetch_date, target_date, station, model_name, var, float(values[i]), fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

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


def main():
    print(f"NWP Forecast Collection — {datetime.now(timezone.utc).isoformat()}")
    print(f"Database: {DB_PATH}")
    print()

    conn = init_db(DB_PATH)

    fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    total_stored = 0
    total_errors = []

    for model_name, api_url in MODELS:
        print(f"\n--- Model: {model_name} (endpoint: {api_url}) ---")
        model_stored = 0
        model_errors = []
        stations_ok = 0

        for station, city_name, lat, lon in CITIES:
            print(f"  {station} ({city_name})...", end=" ", flush=True)

            data = fetch_forecast(api_url, lat, lon)

            if "error" in data:
                print(f"ERROR: {data['error']}")
                model_errors.append(f"{station}: {data['error']}")
                time.sleep(1)
                continue

            stored, errors = parse_and_store(
                conn, data, model_name, station, fetch_date, fetch_timestamp)
            model_stored += stored
            model_errors.extend(errors)
            stations_ok += 1
            print(f"OK ({stored} values)")

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

    # Verify model differentiation
    c.execute("SELECT model, COUNT(DISTINCT value) FROM nwp_forecasts WHERE variable='temperature_2m_max' GROUP BY model")
    print("\nModel differentiation check (distinct temp_2m_max values per model):")
    for row in c.fetchall():
        print(f"  {row[0]}: {row[1]} distinct values")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
