#!/usr/bin/env python3
"""
HRRR Forecast Collection Script — Stub Implementation

Collects HRRR (High Resolution Rapid Refresh) hourly forecast data from
Open-Meteo for all Kalshi cities.

Open-Meteo HRRR endpoint: https://api.open-meteo.com/v1/hrrr
3km resolution, hourly, 0-48h forecasts, US-only coverage.

This is a STUB implementation. If the Open-Meteo HRRR endpoint is not
available, this script will detect that and exit gracefully.

Usage:
    python3 scripts/hrrr_collect.py [--db-path PATH]

See also: docs/plans/HRRR-COLLECTION-SCOPE.md
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import os
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Configuration
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Open-Meteo HRRR endpoint
HRRR_API_URL = "https://api.open-meteo.com/v1/hrrr"

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

HRRR_VARIABLES = [
    "temperature_2m",
    "temperature_2m_max",
    "temperature_2m_min",
    "dewpoint_2m",
    "surface_pressure",
    "windspeed_10m",
]


def parse_args():
    parser = argparse.ArgumentParser(description="HRRR Forecast Collection")
    parser.add_argument("--db-path", help="Database file path")
    parser.add_argument("--check-endpoint", action="store_true",
                        help="Only check if HRRR endpoint is available, then exit")
    return parser.parse_args()


def get_db_path(args):
    if args.db_path:
        return Path(args.db_path).absolute()
    if os.environ.get('NWP_DB_PATH'):
        return Path(os.environ['NWP_DB_PATH']).absolute()
    return (PROJECT_ROOT / NWP_DB_PATH_DEFAULT).absolute()


def check_endpoint_available():
    """Check if the Open-Meteo HRRR endpoint is available."""
    test_url = f"{HRRR_API_URL}?latitude=33.64&longitude=-84.43&hourly=temperature_2m&forecast_hours=1&timezone=UTC"
    try:
        req = urllib.request.Request(test_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "hourly" in data or "error" not in data:
                return True, "HRRR endpoint available"
            return False, f"Unexpected response: {data.get('error', 'unknown')}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "HRRR endpoint not found (404) — Open-Meteo may not support /v1/hrrr"
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"URL error: {e.reason}"
    except Exception as e:
        return False, f"Error: {e}"


def init_db(db_path):
    """Initialize the HRRR forecasts table in the NWP database."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS hrrr_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            target_datetime TEXT NOT NULL,
            station TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            forecast_horizon_hours INTEGER,
            fetch_timestamp TEXT NOT NULL,
            UNIQUE(fetch_date, target_datetime, station, variable)
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_hrrr_lookup
        ON hrrr_forecasts(target_datetime, station, variable)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS hrrr_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            stations_fetched INTEGER,
            variables_fetched INTEGER,
            errors TEXT,
            fetch_timestamp TEXT NOT NULL
        )
    """)

    conn.commit()
    return conn


def fetch_hrrr(lat, lon):
    """Fetch hourly HRRR forecast from Open-Meteo."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(HRRR_VARIABLES),
        "timezone": "UTC",
        "forecast_hours": 48,
    }

    url = HRRR_API_URL + "?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(5)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                pass
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        time.sleep(2)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {"error": f"URL error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def parse_and_store(conn, data, station, fetch_date, fetch_timestamp):
    """Parse HRRR response and store in DB."""
    c = conn.cursor()
    stored = 0
    errors = []

    if "error" in data:
        return 0, [data["error"]]

    if "hourly" not in data:
        return 0, ["No hourly data in response"]

    hourly = data["hourly"]
    times = hourly.get("time", [])

    for var in HRRR_VARIABLES:
        values = hourly.get(var, [])
        if not values:
            continue

        for i, target_datetime in enumerate(times):
            if i < len(values) and values[i] is not None:
                try:
                    c.execute("""
                        INSERT OR REPLACE INTO hrrr_forecasts
                        (fetch_date, target_datetime, station, variable, value,
                         forecast_horizon_hours, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        fetch_date,
                        target_datetime,
                        station,
                        var,
                        float(values[i]),
                        i,  # forecast_horizon_hours = index in hourly array
                        fetch_timestamp
                    ))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_datetime}): {e}")

    conn.commit()
    return stored, errors


def main():
    args = parse_args()
    db_path = get_db_path(args)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"HRRR Collection — {datetime.now(timezone.utc).isoformat()}")
    print(f"Database: {db_path}")

    # Check endpoint availability first
    print("\nChecking HRRR endpoint availability...")
    available, message = check_endpoint_available()
    print(f"  {message}")

    if args.check_endpoint:
        return

    if not available:
        print("\nHRRR endpoint not available. Stub collector stopping.")
        print("HRRR data not collected. Signals will be built with fallback logic.")
        print("To enable HRRR collection, ensure Open-Meteo supports /v1/hrrr.")
        return

    conn = init_db(db_path)
    fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    total_stored = 0
    total_errors = []

    print(f"\n--- Collecting HRRR forecasts for {len(CITIES)} stations ---")

    for station, city_name, lat, lon in CITIES:
        print(f"  {station} ({city_name})...", end=" ", flush=True)

        data = fetch_hrrr(lat, lon)

        if "error" in data:
            print(f"ERROR: {data['error']}")
            total_errors.append(f"{station}: {data['error']}")
            time.sleep(1)
            continue

        stored, errors = parse_and_store(conn, data, station, fetch_date, fetch_timestamp)
        total_stored += stored
        total_errors.extend(errors)
        print(f"OK ({stored} values)")

        time.sleep(0.5)

    # Log fetch
    c = conn.cursor()
    c.execute("""
        INSERT INTO hrrr_fetch_log
        (fetch_date, stations_fetched, variables_fetched, errors, fetch_timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (fetch_date, len(CITIES), total_stored,
          "; ".join(total_errors[:10]) if total_errors else None, fetch_timestamp))
    conn.commit()

    print(f"\n=== COLLECTION COMPLETE ===")
    print(f"Total values stored: {total_stored}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        print(f"First 5 errors: {total_errors[:5]}")

    conn.close()
    print(f"Done at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()