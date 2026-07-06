#!/usr/bin/env python3
"""
NWP Backfill — Pull maximum historical forecast data from Open-Meteo.
Open-Meteo caps past_days at ~92 for forecast endpoints.
We already have June 4 – July 5. This backfills April 7 – June 3.

Runs standalone — no AI, no agent. Pure script.
"""

import sqlite3
import urllib.request
import json
import time
from datetime import datetime, timezone

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/nwp_forecasts.db"

CITIES = [
    ("KATL", 33.64, -84.43), ("KAUS", 30.20, -97.67), ("KBOS", 42.37, -71.01),
    ("KDCA", 38.85, -77.04), ("KDEN", 39.86, -104.67), ("KDFW", 32.90, -97.04),
    ("KHOU", 29.98, -95.36), ("KLAS", 36.08, -115.16), ("KLAX", 33.94, -118.41),
    ("KMDW", 41.79, -87.75), ("KMIA", 25.80, -80.29), ("KMSP", 44.88, -93.22),
    ("KMSY", 29.99, -90.26), ("KNYC", 40.71, -74.01), ("KOKC", 35.39, -97.60),
    ("KPHL", 39.87, -75.24), ("KPHX", 33.43, -112.01), ("KSAT", 29.53, -98.47),
    ("KSEA", 47.45, -122.31), ("KSFO", 37.62, -122.38),
]

MODELS = [
    ("gfs",   "https://api.open-meteo.com/v1/gfs"),
    ("ecmwf", "https://api.open-meteo.com/v1/ecmwf"),
    ("icon",  "https://api.open-meteo.com/v1/dwd-icon"),
    ("gem",   "https://api.open-meteo.com/v1/gem"),
]

DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
HOURLY_VARS = [
    "temperature_850hPa", "wind_speed_10m", "wind_direction_10m",
    "cloud_cover", "dew_point_2m", "geopotential_height_500hPa",
]


def fetch_with_past_days(api_url, lat, lon, past_days):
    """Fetch forecast data including past_days of history."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
        "forecast_days": 7,
        "past_days": past_days,
    }
    url = api_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def store_forecast(conn, data, model_name, station, fetch_timestamp):
    """Store forecast data in DB. Uses the date from the forecast as both fetch_date and target_date."""
    c = conn.cursor()
    stored = 0
    errors = []

    if "error" in data:
        return 0, [data["error"]]

    daily = data.get("daily", {})
    daily_time = daily.get("time", [])

    for var in DAILY_VARS:
        values = daily.get(var, [])
        for i, target_date in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                # Use target_date as fetch_date for historical data
                # (the forecast was "made" on that date)
                try:
                    c.execute("""
                        INSERT OR IGNORE INTO nwp_forecasts
                        (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (target_date, target_date, station, model_name, var, float(values[i]), fetch_timestamp))
                    stored += 1
                except Exception as e:
                    errors.append(f"DB error ({var}, {target_date}): {e}")

    # Process hourly variables → daily aggregates
    hourly = data.get("hourly", {})
    hourly_time = hourly.get("time", [])

    for var in HOURLY_VARS:
        values = hourly.get(var, [])
        # Group by date and average
        daily_values = {}
        for i, ts in enumerate(hourly_time):
            if i < len(values) and values[i] is not None:
                date = ts[:10]  # Extract date from ISO timestamp
                if date not in daily_values:
                    daily_values[date] = []
                daily_values[date].append(float(values[i]))

        for date, vals in daily_values.items():
            avg_val = sum(vals) / len(vals)
            try:
                c.execute("""
                    INSERT OR IGNORE INTO nwp_forecasts
                    (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (date, date, station, model_name, var, avg_val, fetch_timestamp))
                stored += 1
            except Exception as e:
                errors.append(f"DB error ({var}, {date}): {e}")

    return stored, errors


def main():
    conn = sqlite3.connect(DB_PATH)
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    # Open-Meteo caps at ~92 past_days
    PAST_DAYS = 92

    total_stored = 0
    total_errors = []

    for model_name, api_url in MODELS:
        print(f"\n=== {model_name.upper()} ===")
        for station, lat, lon in CITIES:
            data = fetch_with_past_days(api_url, lat, lon, PAST_DAYS)
            stored, errors = store_forecast(conn, data, model_name, station, fetch_timestamp)
            conn.commit()
            total_stored += stored
            total_errors.extend(errors)
            status = f"  {station}: {stored} rows" if stored else f"  {station}: 0 rows"
            if errors:
                status += f" ({len(errors)} errors)"
            print(status)
            time.sleep(0.5)  # Rate limit courtesy

    conn.close()
    print(f"\nTotal: {total_stored} rows stored, {len(total_errors)} errors")

    # Summary
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*), MIN(fetch_date), MAX(fetch_date) FROM nwp_forecasts")
    row = cur.fetchone()
    print(f"DB now has {row[0]} rows, fetch dates {row[1]} to {row[2]}")
    conn.close()


if __name__ == "__main__":
    main()
