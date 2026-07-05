#!/usr/bin/env python3
"""
NWP 30-Day Historical Backfill Script (v2.0)

Pulls 30 days of historical forecasts from Open-Meteo's model-specific endpoints
for all 4 models (GFS, ECMWF, ICON, GEM) and all 20 Kalshi stations.

IMPORTANT: Open-Meteo's `past_days` parameter returns the forecast that was
ISSUED on each past day, not the actual observed weather. This is exactly what
we need for backtesting — we compare what the models forecasted vs what
actually happened.

⚠️  STANDALONE SCRIPT — DO NOT RUN VIA AI/AGENT.
    Run manually: python3 scripts/nwp_backfill_30d.py
    Or via host cron (one-time bootstrap, commented out in crontab).

Data stored to: prototypes/weather-engine-source/data/nwp_forecasts.db

Version History:
  v1.0  2026-07-03  Initial version
  v2.0  2026-07-03  Added retry logic, progress tracking, resume capability, rate limiting
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = str(REPO_ROOT / "data" / "nwp_forecasts.db")

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

# Model-specific endpoints (NOT the generic /v1/forecast)
MODELS = [
    ("gfs",   "https://api.open-meteo.com/v1/gfs"),
    ("ecmwf", "https://api.open-meteo.com/v1/ecmwf"),
    ("icon",  "https://api.open-meteo.com/v1/dwd-icon"),
    ("gem",   "https://api.open-meteo.com/v1/gem"),
]

DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
HOURLY_VARS = [
    "temperature_850hPa",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
    "dew_point_2m",
    "geopotential_height_500hPa",
]

SCRIPT_VERSION = "v2.0"


def init_db(db_path):
    """Initialize DB schema (same as nwp_collect.py)."""
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

    # Resume tracking table
    c.execute("""
        CREATE TABLE IF NOT EXISTS backfill_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            station TEXT NOT NULL,
            status TEXT NOT NULL,  -- pending, complete, failed
            attempted_at TEXT,
            completed_at TEXT,
            error_msg TEXT,
            UNIQUE(model, station)
        )
    """)

    conn.commit()
    return conn


def get_resume_state(conn):
    """Check which model/station combos are already done."""
    c = conn.cursor()
    c.execute("SELECT model, station, status FROM backfill_progress WHERE status = 'complete'")
    done = set()
    for row in c.fetchall():
        done.add((row[0], row[1]))
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


def fetch_forecast(api_url, lat, lon, past_days=30, max_retries=3):
    """Fetch historical + future forecasts from model-specific endpoint."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
        "past_days": past_days,
    }
    url = api_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"OpenClaw-WeatherEngine/{SCRIPT_VERSION}"
            })
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"    Rate limited (429), waiting {wait}s... (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 3 * (attempt + 1)
                print(f"    Retry {attempt+1}/{max_retries}: {e}, waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": str(e)}

    return {"error": "Max retries exceeded"}


def store_forecast(conn, data, model_name, station, fetch_date, fetch_timestamp):
    """Parse and store forecast data in DB."""
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


def main():
    parser = argparse.ArgumentParser(description='NWP 30-day historical backfill')
    parser.add_argument('--past-days', type=int, default=30, help='Number of past days to backfill (default: 30)')
    parser.add_argument('--models', type=str, help='Comma-separated model names (default: all)')
    parser.add_argument('--stations', type=str, help='Comma-separated station codes (default: all)')
    parser.add_argument('--resume', action='store_true', help='Skip already-completed model/station pairs')
    args = parser.parse_args()

    past_days = args.past_days
    models = [(m, u) for m, u in MODELS if not args.models or m in args.models.split(',')]
    cities = [c for c in CITIES if not args.stations or c[0] in args.stations.split(',')]

    now = datetime.now(timezone.utc)
    print(f"NWP {past_days}-Day Historical Backfill {SCRIPT_VERSION} — {now.isoformat()}")
    print(f"Database: {DB_PATH}")
    print(f"Models: {[m[0] for m in models]}")
    print(f"Stations: {len(cities)}")
    print(f"Resume mode: {'ON' if args.resume else 'OFF'}")
    print()

    conn = init_db(DB_PATH)

    done_set = get_resume_state(conn) if args.resume else set()
    if done_set:
        print(f"Skipping {len(done_set)} already-complete model/station pairs")

    fetch_timestamp = now.isoformat()
    total_stored = 0
    total_errors = []
    total_combos = len(models) * len(cities)
    current_combo = 0

    for model_name, api_url in models:
        print(f"\n{'='*60}")
        print(f"Model: {model_name} ({api_url})")
        print(f"{'='*60}")

        model_stored = 0
        model_errors = []
        stations_ok = 0

        for station, city_name, lat, lon in cities:
            current_combo += 1

            if (model_name, station) in done_set:
                print(f"  [{current_combo}/{total_combos}] {station} ({city_name})... SKIPPED (already complete)")
                continue

            print(f"  [{current_combo}/{total_combos}] {station} ({city_name})...", end=" ", flush=True)

            # Mark as attempted
            mark_progress(conn, model_name, station, 'pending')

            data = fetch_forecast(api_url, lat, lon, past_days=past_days)

            if "error" in data:
                print(f"ERROR: {data['error']}")
                model_errors.append(f"{station}: {data['error']}")
                mark_progress(conn, model_name, station, 'failed', data['error'])
                time.sleep(2)
                continue

            stored, errors = store_forecast(
                conn, data, model_name, station,
                fetch_date=data.get("daily", {}).get("time", ["unknown"])[0],
                fetch_timestamp=fetch_timestamp)

            model_stored += stored
            model_errors.extend(errors)
            stations_ok += 1
            print(f"OK ({stored} values)")

            mark_progress(conn, model_name, station, 'complete')

            # Rate limit — Open-Meteo free tier allows ~10k calls/day
            time.sleep(1.5)

        total_stored += model_stored
        total_errors.extend(model_errors)

        c = conn.cursor()
        c.execute("""
            INSERT INTO fetch_log
            (fetch_date, model, stations_fetched, variables_fetched, errors, fetch_timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fetch_timestamp[:10], model_name, stations_ok, model_stored,
              "; ".join(model_errors[:10]) if model_errors else None, fetch_timestamp))
        conn.commit()

        print(f"\n  Model {model_name}: {stations_ok}/{len(cities)} stations, "
              f"{model_stored} values stored, {len(model_errors)} errors")

    print(f"\n{'='*60}")
    print(f"BACKFILL COMPLETE")
    print(f"{'='*60}")
    print(f"Total values stored: {total_stored}")
    print(f"Total errors: {len(total_errors)}")
    if total_errors:
        print(f"First 5 errors:")
        for err in total_errors[:5]:
            print(f"  • {err}")

    # Summary stats
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM nwp_forecasts")
    total_rows = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT fetch_date) FROM nwp_forecasts")
    fetch_days = c.fetchone()[0]
    c.execute("SELECT MIN(fetch_date), MAX(fetch_date) FROM nwp_forecasts")
    date_range = c.fetchone()

    print(f"\nDatabase stats:")
    print(f"  Total rows: {total_rows}")
    print(f"  Fetch days: {fetch_days}")
    print(f"  Date range: {date_range[0]} → {date_range[1]}")

    # Per-model breakdown
    c.execute("""
        SELECT model, COUNT(*) as rows, COUNT(DISTINCT station) as stations,
               COUNT(DISTINCT variable) as variables
        FROM nwp_forecasts
        GROUP BY model
        ORDER BY model
    """)
    print(f"\n  {'Model':<8} {'Rows':>8} {'Stations':>8} {'Variables':>10}")
    print(f"  {'-'*36}")
    for row in c.fetchall():
        print(f"  {row[0]:<8} {row[1]:>8} {row[2]:>8} {row[3]:>10}")

    conn.close()
    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
