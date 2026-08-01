#!/usr/bin/env python3
"""
ERA5 Surface Backfill — Open-Meteo Archive API

Backfills ERA5 reanalysis daily surface temperature (max/min/mean t2m)
into data/era5_archive.db for all 20 Kalshi weather stations through present.

Designed to resume from existing data. Uses era5_checkpoint table.
Only fetches missing date ranges.

Usage:
    python3 scripts/era5_backfill.py                    # Run all missing stations
    python3 scripts/era5_backfill.py --stations KSAT    # Single station
    python3 scripts/era5_backfill.py --dry-run          # Show what would be done

Version: v2.0 2026-07-31
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ERA5_DB = str(REPO_ROOT / "data" / "era5_archive.db")

# Open-Meteo Archive API (free, no key required)
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

STATIONS = [
    ("KATL", 33.64, -84.43),
    ("KAUS", 30.20, -97.67),
    ("KBOS", 42.37, -71.01),
    ("KDCA", 38.85, -77.04),
    ("KDEN", 39.86, -104.67),
    ("KDFW", 32.90, -97.04),
    ("KHOU", 29.98, -95.36),
    ("KLAS", 36.08, -115.16),
    ("KLAX", 33.94, -118.41),
    ("KMDW", 41.79, -87.75),
    ("KMIA", 25.80, -80.29),
    ("KMSP", 44.88, -93.22),
    ("KMSY", 29.99, -90.26),
    ("KNYC", 40.71, -74.01),
    ("KOKC", 35.39, -97.60),
    ("KPHL", 39.87, -75.24),
    ("KPHX", 33.43, -112.01),
    ("KSAT", 29.53, -98.47),
    ("KSEA", 47.45, -122.31),
    ("KSFO", 37.62, -122.38),
]

MAX_RETRIES = 5
DELAY_BETWEEN_STATIONS = 2  # seconds


def init_era5_db(db_path):
    """Ensure era5_archive and checkpoint tables exist."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS era5_archive (
            target_date TEXT NOT NULL,
            station TEXT NOT NULL,
            daily_max_t2m REAL,
            daily_min_t2m REAL,
            daily_mean_t2m REAL,
            n_hours INTEGER DEFAULT 24,
            source TEXT DEFAULT 'era5',
            PRIMARY KEY (target_date, station)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS era5_checkpoint (
            station TEXT PRIMARY KEY,
            last_completed_date TEXT,
            status TEXT DEFAULT 'pending',
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def get_checkpoint(conn, station):
    """Get the last completed date for a station."""
    c = conn.cursor()
    c.execute("SELECT last_completed_date, status FROM era5_checkpoint WHERE station=?", (station,))
    row = c.fetchone()
    if row:
        return row[0], row[1]
    return None, None


def update_checkpoint(conn, station, last_date, status):
    """Update checkpoint for a station."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT OR REPLACE INTO era5_checkpoint
        (station, last_completed_date, status, updated_at)
        VALUES (?, ?, ?, ?)
    """, (station, last_date or '', status, now))
    conn.commit()


def get_existing_dates(conn, station):
    """Get set of dates already in era5_archive for a station."""
    c = conn.cursor()
    c.execute("SELECT target_date FROM era5_archive WHERE station=?", (station,))
    return set(row[0] for row in c.fetchall())


def fetch_era5_openmeteo(lat, lon, start_date, end_date):
    """Fetch ERA5 daily surface data from Open-Meteo Archive API."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min,temperature_2m_mean",
        "timezone": "UTC",
    }
    url = ARCHIVE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                print(f"    Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = 5 * (attempt + 1)
                print(f"    Retry {attempt+1}/{MAX_RETRIES}: {e}, waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"error": str(e)}
    return {"error": "Max retries exceeded"}


def store_era5_data(conn, station, data):
    """Store ERA5 daily data into era5_archive."""
    c = conn.cursor()
    stored = 0
    
    if "error" in data:
        return 0, data["error"]
    
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    max_vals = daily.get("temperature_2m_max", [])
    min_vals = daily.get("temperature_2m_min", [])
    mean_vals = daily.get("temperature_2m_mean", [])
    
    if not dates:
        return 0, "No daily data in response"
    
    for i, date_str in enumerate(dates):
        try:
            c.execute("""
                INSERT OR REPLACE INTO era5_archive
                (target_date, station, daily_max_t2m, daily_min_t2m, daily_mean_t2m, n_hours, source)
                VALUES (?, ?, ?, ?, ?, 24, 'era5')
            """, (
                date_str,
                station,
                max_vals[i] if i < len(max_vals) and max_vals[i] is not None else None,
                min_vals[i] if i < len(min_vals) and min_vals[i] is not None else None,
                mean_vals[i] if i < len(mean_vals) and mean_vals[i] is not None else None,
            ))
            stored += 1
        except Exception as e:
            pass  # Skip individual date errors
    
    conn.commit()
    return stored, None


def backfill_station(conn, station, lat, lon, dry_run=False):
    """Backfill one station from its last completed date through present."""
    last_date, status = get_checkpoint(conn, station)
    existing = get_existing_dates(conn, station)
    
    if existing:
        # Find the max date in existing data
        existing_dates_sorted = sorted(existing)
        last_completed = existing_dates_sorted[-1]
    else:
        last_completed = last_date if last_date else None
    
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    end_date = today
    
    if last_completed:
        last_dt = datetime.strptime(last_completed, "%Y-%m-%d")
        next_day = (last_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        if next_day >= today:
            print(f"  {station}: already complete through {last_completed}")
            return True
        
        # Only fetch what's missing
        missing_start = next_day
    else:
        if existing:
            missing_start = list(sorted(existing))[-1]
            missing_dt = datetime.strptime(missing_start, "%Y-%m-%d")
            missing_start = (missing_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            missing_start = "2021-01-01"  # Start from beginning
    
    if missing_start >= today:
        print(f"  {station}: already complete")
        update_checkpoint(conn, station, today, 'done')
        return True
    
    print(f"  {station}: fetching {missing_start} -> {today} ...", end=" ", flush=True)
    
    if dry_run:
        print("DRY-RUN (would fetch)")
        return True
    
    data = fetch_era5_openmeteo(lat, lon, missing_start, today)
    
    if "error" in data:
        print(f"FAILED: {data['error']}")
        update_checkpoint(conn, station, last_completed or '', 'failed')
        return False
    
    stored, err = store_era5_data(conn, station, data)
    if err:
        print(f"STORE ERROR: {err}")
        update_checkpoint(conn, station, last_completed or '', 'failed')
        return False
    
    print(f"OK ({stored} rows)")
    update_checkpoint(conn, station, today, 'done')
    return True


def main():
    parser = argparse.ArgumentParser(description="ERA5 Surface Backfill")
    parser.add_argument("--stations", nargs="+", help="Specific stations to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()
    
    print("=" * 60)
    print("ERA5 Surface Backfill v2.0")
    print(f"Database: {ERA5_DB}")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    
    conn = init_era5_db(ERA5_DB)
    
    stations_to_run = args.stations if args.stations else [s[0] for s in STATIONS]
    
    success = 0
    failed = 0
    skipped = 0
    
    for idx, (station_code, lat, lon) in enumerate(STATIONS, 1):
        if station_code not in stations_to_run:
            continue
        
        print(f"\n[{idx}/{len(STATIONS)}] {station_code} (lat={lat}, lon={lon})")
        
        ok = backfill_station(conn, station_code, lat, lon, args.dry_run)
        if ok is None:
            skipped += 1
        elif ok:
            success += 1
        else:
            failed += 1
        
        if idx < len(STATIONS):
            time.sleep(DELAY_BETWEEN_STATIONS)
    
    # Summary
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM era5_archive")
    total = c.fetchone()[0]
    c.execute("SELECT station, COUNT(*) FROM era5_archive GROUP BY station ORDER BY station")
    per_station = c.fetchall()
    
    print(f"\n{'='*60}")
    print(f"Backfill complete: {success} OK, {failed} failed, {skipped} skipped")
    print(f"Total rows in era5_archive.db: {total}")
    print(f"\nPer-station counts:")
    for st, cnt in per_station:
        print(f"  {st}: {cnt}")
    
    conn.close()


if __name__ == "__main__":
    main()