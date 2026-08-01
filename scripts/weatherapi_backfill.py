#!/usr/bin/env python3
"""
WeatherAPI.com Historical Backfill — Resume Mode

Backfills daily/hourly data for remaining stations (KSAT, KSEA, KSFO)
and fixes KPHX (stopped at 2016). Uses existing DB with 17/20 stations done.

Usage:
    python3 scripts/weatherapi_backfill.py --stations KSAT,KSEA,KSFO
    python3 scripts/weatherapi_backfill.py --stations KPHX --start 2010-01-01
    python3 scripts/weatherapi_backfill.py          # resume all incomplete

Data source: weatherapi.com Business trial (10M calls/month).
~200K calls total. One HTTP call per station-date.
"""

import os, sys, json, time, sqlite3, argparse, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Dict, List, Tuple, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

HISTORY_URL = 'https://api.weatherapi.com/v1/history.json'
FORECAST_URL = 'https://api.weatherapi.com/v1/forecast.json'

STATIONS = [
    ("KATL", 33.6407, -84.4277), ("KAUS", 30.1945, -97.6699),
    ("KBOS", 42.3656, -71.0096), ("KDCA", 38.8521, -77.0377),
    ("KDEN", 39.8617, -104.6732), ("KDFW", 32.8968, -97.038),
    ("KHOU", 29.6454, -95.2789), ("KLAS", 36.0804, -115.1523),
    ("KLAX", 33.9425, -118.4081), ("KMDW", 41.786, -87.7525),
    ("KMIA", 25.7959, -80.287), ("KMSP", 44.8805, -93.2169),
    ("KMSY", 29.9933, -90.2581), ("KNYC", 40.7128, -74.006),
    ("KOKC", 35.3931, -97.6007), ("KPHL", 39.8729, -75.2425),
    ("KPHX", 33.4484, -112.0773), ("KSAT", 29.4241, -98.4936),
    ("KSEA", 47.4489, -122.3093), ("KSFO", 37.6194, -122.3748),
]
STATION_MAP = {s[0]: (s[1], s[2]) for s in STATIONS}

DB_PATH = str(REPO_ROOT / 'data' / 'weatherapi_archive.db')
DEFAULT_START = '2010-01-01'
DEFAULT_END = '2026-08-01'
RATE_LIMIT_S = 0.15

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily (
    station TEXT NOT NULL, date TEXT NOT NULL,
    max_temp_c REAL, min_temp_c REAL, avg_temp_c REAL,
    max_temp_f REAL, min_temp_f REAL, avg_temp_f REAL,
    condition_text TEXT, humidity REAL, pressure_mb REAL,
    wind_kph REAL, precip_mm REAL,
    source TEXT DEFAULT 'weatherapi_history',
    PRIMARY KEY (station, date)
);
CREATE TABLE IF NOT EXISTS hourly (
    station TEXT NOT NULL, date TEXT NOT NULL, hour INTEGER NOT NULL,
    temp_c REAL, temp_f REAL, condition_text TEXT,
    humidity REAL, pressure_mb REAL, wind_kph REAL,
    precip_mm REAL, cloud INTEGER, feelslike_c REAL,
    source TEXT DEFAULT 'weatherapi_history',
    PRIMARY KEY (station, date, hour)
);
CREATE TABLE IF NOT EXISTS checkpoint (
    station TEXT NOT NULL, last_date TEXT NOT NULL,
    status TEXT DEFAULT 'done', updated_at TEXT NOT NULL,
    PRIMARY KEY (station)
);
"""

# Get API key
WEATHERAPI_KEY = os.environ.get('WEATHERAPI_KEY', '')
if not WEATHERAPI_KEY:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('WEATHERAPI_KEY='):
                    WEATHERAPI_KEY = line.split('=', 1)[1].strip()
    except FileNotFoundError:
        pass
if not WEATHERAPI_KEY:
    print("ERROR: WEATHERAPI_KEY not found in env or .env")
    sys.exit(1)


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite DB with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def fetch_history(station: str, lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Fetch one day of history from WeatherAPI.com."""
    params = {
        'key': WEATHERAPI_KEY,
        'q': f'{lat},{lon}',
        'dt': date_str,
    }
    url = f"{HISTORY_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if 'error' in data:
            print(f"  API error for {station} {date_str}: {data['error']}")
            return None
        return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  429 rate limit on {station} {date_str}, waiting 5s...")
            time.sleep(5)
            return None
        print(f"  HTTP {e.code} for {station} {date_str}")
        return None
    except Exception as e:
        print(f"  Error {station} {date_str}: {e}")
        return None


def store_day(conn: sqlite3.Connection, station: str, date_str: str, data: dict) -> bool:
    """Store one day of daily + hourly data. Returns True if data was stored."""
    if 'forecast' not in data and 'history' not in data:
        return False
    day_data = None
    hour_data = []
    source_data = data.get('forecast', data.get('history'))

    if source_data and 'forecastday' in source_data and source_data['forecastday']:
        fday = source_data['forecastday'][0]
        day_data = fday.get('day', {})
        hour_data = fday.get('hour', [])

    stored = False
    # Store daily
    if day_data:
        try:
            maxt_c = day_data.get('maxtemp_c')
            mint_c = day_data.get('mintemp_c')
            avg_c = day_data.get('avgtemp_c')
            maxt_f = maxt_c * 9/5 + 32 if maxt_c is not None else None
            mint_f = mint_c * 9/5 + 32 if mint_c is not None else None
            avg_f = avg_c * 9/5 + 32 if avg_c is not None else None
            conn.execute("""
                INSERT OR REPLACE INTO daily
                (station, date, max_temp_c, min_temp_c, avg_temp_c,
                 max_temp_f, min_temp_f, avg_temp_f,
                 condition_text, humidity, pressure_mb, wind_kph, precip_mm)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (station, date_str, maxt_c, mint_c, avg_c,
                  maxt_f, mint_f, avg_f,
                  day_data.get('condition', {}).get('text'),
                  day_data.get('avghumidity'),
                  day_data.get('pressure_mb'),
                  day_data.get('maxwind_kph'),
                  day_data.get('totalprecip_mm')))
            stored = True
        except Exception as e:
            print(f"  DB error daily {station} {date_str}: {e}")

    # Store hourly
    for h in hour_data:
        try:
            temp_c = h.get('temp_c')
            temp_f = temp_c * 9/5 + 32 if temp_c is not None else None
            hour = int(h.get('time', '00:00')[:2])
            conn.execute("""
                INSERT OR REPLACE INTO hourly
                (station, date, hour, temp_c, temp_f, condition_text,
                 humidity, pressure_mb, wind_kph, precip_mm, cloud, feelslike_c)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (station, date_str, hour, temp_c, temp_f,
                  h.get('condition', {}).get('text'),
                  h.get('humidity'), h.get('pressure_mb'),
                  h.get('wind_kph'), h.get('precip_mm'),
                  h.get('cloud'), h.get('feelslike_c')))
        except Exception as e:
            print(f"  DB error hourly {station} {date_str} h{hour}: {e}")

    return stored


def run_backfill(conn: sqlite3.Connection, stations: List[Tuple[str, float, float]],
                 start: str, end: str, skip_existing: bool = True) -> None:
    """Run backfill for specified stations and date range."""
    start_dt = date.fromisoformat(start)
    end_dt = date.fromisoformat(end)
    total_dates = (end_dt - start_dt).days + 1

    print(f"Backfill: {len(stations)} stations, {start} to {end} ({total_dates} days)")
    print(f"Rate limit: {RATE_LIMIT_S}s between calls\n")

    for sidx, (station, lat, lon) in enumerate(stations):
        print(f"[{sidx+1}/{len(stations)}] {station} ({lat:.2f}, {lon:.2f})")

        # Check existing progress
        row = conn.execute(
            "SELECT last_date FROM checkpoint WHERE station=? AND status='done'",
            (station,)).fetchone()
        resume_from = start
        if row and skip_existing:
            last = row[0]
            if last >= end:
                print(f"  Already complete through {last}, skipping")
                continue
            resume_from = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            print(f"  Resuming from {resume_from} (was at {last})")

        stored_count = 0
        error_count = 0
        skip_count = 0
        current_dt = date.fromisoformat(resume_from)
        end_dt_local = date.fromisoformat(end)

        while current_dt <= end_dt_local:
            ds = current_dt.isoformat()

            if skip_existing:
                exists = conn.execute(
                    "SELECT 1 FROM daily WHERE station=? AND date=?",
                    (station, ds)).fetchone()
                if exists:
                    skip_count += 1
                    current_dt += timedelta(days=1)
                    continue

            data = fetch_history(station, lat, lon, ds)
            if data:
                ok = store_day(conn, station, ds, data)
                conn.commit()
                if ok:
                    stored_count += 1
                else:
                    error_count += 1
            else:
                error_count += 1

            # Update checkpoint every 10 days
            if stored_count % 10 == 0 and stored_count > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO checkpoint (station, last_date, status, updated_at) VALUES (?,?,?,?)",
                    (station, ds, 'done', datetime.now(timezone.utc).isoformat()))
                conn.commit()

            if (stored_count + skip_count) % 50 == 0 and (stored_count + skip_count) > 0:
                pct = (stored_count + skip_count) / total_dates * 100
                print(f"  {station}: {stored_count} stored, {skip_count} skipped, "
                      f"{error_count} errors ({pct:.0f}%)")

            current_dt += timedelta(days=1)
            time.sleep(RATE_LIMIT_S)

        # Final checkpoint
        conn.execute(
            "INSERT OR REPLACE INTO checkpoint (station, last_date, status, updated_at) VALUES (?,?,?,?)",
            (station, end, 'done', datetime.now(timezone.utc).isoformat()))
        conn.commit()
        print(f"  {station} done: {stored_count} stored, {skip_count} skipped, "
              f"{error_count} errors")
        print()


def main():
    parser = argparse.ArgumentParser(description='WeatherAPI Historical Backfill')
    parser.add_argument('--stations', help='Comma-separated station codes (default: resume incomplete)')
    parser.add_argument('--start', default=DEFAULT_START, help=f'Start date (default: {DEFAULT_START})')
    parser.add_argument('--end', default=DEFAULT_END, help=f'End date (default: {DEFAULT_END})')
    parser.add_argument('--force', action='store_true', help='Re-download existing data')
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.stations:
        codes = [s.strip().upper() for s in args.stations.split(',')]
        stations = [(s, STATION_MAP[s][0], STATION_MAP[s][1]) for s in codes if s in STATION_MAP]
        missing = [s for s in codes if s not in STATION_MAP]
        if missing:
            print(f"Unknown stations: {missing}")
    else:
        # Auto-detect incomplete stations
        all_done = {r[0] for r in conn.execute("SELECT station FROM checkpoint WHERE status='done'").fetchall()}
        incomplete = [(s, STATION_MAP[s][0], STATION_MAP[s][1])
                      for s, _ in STATION_MAP.items()
                      if s not in all_done or args.force]
        if not incomplete:
            print("All 20 stations are complete! Nothing to do.")
            conn.close()
            return
        stations = incomplete
        print(f"Resuming {len(stations)} incomplete stations")

    run_backfill(conn, stations, args.start, args.end, skip_existing=not args.force)
    conn.close()


if __name__ == '__main__':
    main()