#!/usr/bin/env python3
"""
METAR Gap Detection and Automated Backfill - Weather.gov API Edition

Detects missing dates per station and automatically backfills from NOAA Weather.gov API.

Usage:
  python3 metar_gap_backfill.py --auto-gap-fill          # Detect and fill gaps
  python3 metar_gap_backfill.py --force-backfill --date 2026-07-05  # Backfill specific date
  python3 metar_gap_backfill.py --check-only            # Show gaps without filling
"""

import sqlite3
import urllib.request
import urllib.error
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import argparse
import sys
import urllib.parse

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")

# All 20 Kalshi stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KDFW", "KHOU", "KLAS", "KLAX", "KMDW",
    "KMIA", "KMSP", "KMSY", "KNYC", "KOKC",
    "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
]

# Weather.gov API endpoint (public, no key required)
BASE_URL = "https://api.weather.gov/stations/{station}/observations"

# Minimal request headers required by the API
HEADERS = {
    "Accept": "application/ld+json",
    "User-Agent": "OpenClaw-WeatherEngine-GapBackfill/1.0 (+https://openclaw.ai)"
}

# Threshold for gap detection (minimum observations per day)
MIN_OBS_THRESHOLD = 3


# ─── Database Helper Functions ────────────────────────────────────────────

def get_db_connection():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_existing_dates_by_station(conn, stations):
    """Get dates that have observations for each station."""
    c = conn.cursor()
    dates_by_station = {}
    for station in stations:
        c.execute("""
            SELECT DISTINCT date_utc 
            FROM metar_observations 
            WHERE station = ?
            ORDER BY date_utc
        """, (station,))
        dates_by_station[station] = set(row[0] for row in c.fetchall() if row[0])
    return dates_by_station


def get_observation_count(conn, station, date_str):
    """Get observation count for a station/date."""
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM metar_observations 
        WHERE station = ? AND date_utc = ?
    """, (station, date_str))
    return c.fetchone()[0]


def get_date_range(conn, station):
    """Get min and max date for a station."""
    c = conn.cursor()
    c.execute("""
        SELECT MIN(date_utc), MAX(date_utc) FROM metar_observations 
        WHERE station = ?
    """, (station,))
    row = c.fetchone()
    return (row[0], row[1]) if row and row[0] else (None, None)


def parse_weather_gov_observation(obs):
    """Parse a Weather.gov API observation into our METAR format."""
    # Properties are at the root level of the feature object
    temp = obs.get("temperature")
    if temp is None:
        return None
    temp_f = temp.get("value")
    if temp_f is None:
        return None
    temp_f = temp_f * 9/5 + 32  # Convert to Fahrenheit (from Celsius)
    
    # Extract dewpoint
    dew = obs.get("dewpoint")
    dew_f = None
    if dew and dew.get("value"):
        dew_f = dew["value"] * 9/5 + 32  # Convert to Fahrenheit
    
    # Extract wind
    wind = obs.get("windDirection")
    wind_dir = wind["value"] if wind and wind.get("value") else None
    
    wind_speed = obs.get("windSpeed")
    speed_kt = None
    if wind_speed and wind_speed.get("value"):
        speed_ms = wind_speed["value"]
        speed_kt = speed_ms * 1.94384  # m/s to knots
    
    # Extract pressure
    press = obs.get("barometricPressure")
    press_mb = None
    if press and press.get("value"):
        press_hpa = press["value"] / 100  # Pa to hPa (mb)
        press_mb = press_hpa
    
    # Extract visibility
    vis = obs.get("visibility")
    vis_mi = None
    if vis and vis.get("value"):
        vis_m = vis["value"]
        vis_mi = vis_m * 0.000621371  # meters to miles
    
    # Extract ceiling (cloud base)
    ceiling = obs.get("cloudBase")
    ceiling_ft = None
    if ceiling and ceiling.get("value"):
        ceiling_ft = ceiling["value"] * 3.28084  # meters to feet
    
    # Observation timestamp
    obs_time_str = obs.get("timestamp")
    if obs_time_str:
        obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
    else:
        obs_time = datetime.now().astimezone()
    
    return {
        'temp_f': temp_f,
        'temp_c': (temp_f - 32) * 5/9,
        'dewpoint_f': dew_f,
        'dewpoint_c': (dew_f - 32) * 5/9 if dew_f else None,
        'wind_direction_deg': wind_dir,
        'wind_speed_kt': speed_kt,
        'wind_gust_kt': None,
        'pressure_mb': press_mb,
        'visibility_mi': vis_mi,
        'ceiling_ft': ceiling_ft,
        'raw_metar': None,
        'obs_datetime_utc': obs_time,
        'source': 'weather-gov-api',
    }


# ─── Gap Detection ───────────────────────────────────────────────────────

def detect_gaps(conn, stations, lookback_days=7):
    """
    Detect dates with < MIN_OBS_THRESHOLD observations for each station.
    
    Returns dict: {station: [list of dates with gaps]}
    """
    c = conn.cursor()
    today = datetime.now().date()
    gaps = {}
    
    for station in stations:
        # Get the date range for this station
        min_date, max_date = get_date_range(conn, station)
        if not min_date:
            continue
        
        min_date = datetime.strptime(min_date, "%Y-%m-%d").date()
        # Only check last N days
        start_date = max(min_date, today - timedelta(days=lookback_days))
        
        # Get all dates in range that have any observations
        existing_dates = get_existing_dates_by_station(conn, [station])[station]
        
        # Check each date
        gaps[station] = []
        current_date = start_date
        while current_date <= today:
            date_str = current_date.strftime("%Y-%m-%d")
            if date_str not in existing_dates:
                # Completely missing
                gaps[station].append(date_str)
            else:
                # Check if below threshold
                count = get_observation_count(conn, station, date_str)
                if count < MIN_OBS_THRESHOLD:
                    gaps[station].append(date_str)
            current_date += timedelta(days=1)
    
    return gaps


def print_gaps(gaps):
    """Print detected gaps in a human-readable format."""
    print("\n" + "=" * 70)
    print("METAR GAP DETECTION RESULTS")
    print("=" * 70)
    
    total_gaps = 0
    for station, dates in sorted(gaps.items()):
        if dates:
            total_gaps += len(dates)
            print(f"\n{station} ({len(dates)} gaps):")
            for date_str in dates:
                count = get_observation_count(get_db_connection(), station, date_str)
                status = "MISSING" if count == 0 else f"ONLY {count} OBS"
                print(f"  {date_str}: {status}")
    
    if total_gaps == 0:
        print("\nNo gaps detected.")
    else:
        print(f"\nTotal gaps: {total_gaps}")
    print("=" * 70)


# ─── Weather.gov API Backfill ─────────────────────────────────────────────

def fetch_metar_observations(station, start_date, end_date):
    """
    Fetch METAR observations from Weather.gov API for a date range.
    
    Returns list of parsed observations.
    """
    url = BASE_URL.format(station=station)
    
    # Build query parameters
    params = {
        "start": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 500,  # Weather.gov API max is 500
    }
    
    # Build URL with query params using proper encoding
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    
    try:
        req = urllib.request.Request(full_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        
        features = data.get("@graph", [])
        observations = []
        for feature in features:
            obs = parse_weather_gov_observation(feature)
            if obs:
                observations.append(obs)
        
        return observations
    
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"    Station {station} not found")
            return []
        print(f"    HTTP error {e.code}: {e.reason}")
        return []
    except urllib.error.URLError as e:
        print(f"    URL error: {e.reason}")
        return []
    except Exception as e:
        print(f"    Fetch error: {e}")
        return []


def save_metar_observations(conn, station, observations):
    """Save observations to METAR database."""
    c = conn.cursor()
    saved = 0
    
    for obs in observations:
        if obs.get('temp_f') is None:
            continue
        
        obs_time = obs['obs_datetime_utc']
        date_str = obs_time.strftime('%Y-%m-%d')
        ts_str = obs_time.isoformat()
        
        try:
            c.execute("""
                INSERT OR REPLACE INTO metar_observations
                (station, date_utc, timestamp_utc, temp_f, temp_c, dewpoint_f, dewpoint_c,
                 wind_direction_deg, wind_speed_kt, wind_gust_kt, pressure_mb,
                 sea_level_pressure_mb, visibility_mi, ceiling_ft, raw_metar, source,
                 ingestion_timestamp_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                station, date_str, ts_str,
                obs['temp_f'], obs['temp_c'],
                obs['dewpoint_f'], obs['dewpoint_c'],
                obs['wind_direction_deg'], obs['wind_speed_kt'],
                obs.get('wind_gust_kt'),
                obs['pressure_mb'], None,
                obs['visibility_mi'], obs['ceiling_ft'],
                obs.get('raw_metar'),
                obs.get('source', 'weather-gov-api'),
                datetime.now().astimezone().isoformat()
            ))
            saved += 1
        except Exception as e:
            print(f"      DB save error: {e}")
    
    conn.commit()
    return saved


def backfill_date(conn, station, date_str):
    """Backfill a single date for a station."""
    print(f"  Backfilling {station} {date_str}...", end=" ", flush=True)
    
    date = datetime.strptime(date_str, "%Y-%m-%d")
    start = date
    end = date + timedelta(days=1) - timedelta(seconds=1)
    
    observations = fetch_metar_observations(station, start, end)
    
    if not observations:
        print("NO DATA")
        return 0
    
    saved = save_metar_observations(conn, station, observations)
    print(f"OK ({saved} obs)")
    return saved


def auto_backfill(conn, gaps):
    """Automatically backfill all detected gaps."""
    print("\n" + "=" * 70)
    print("AUTO-BACKFILL PROCESS (Weather.gov API)")
    print("=" * 70)
    
    total = 0
    saved = 0
    failed = 0
    
    for station, dates in sorted(gaps.items()):
        if not dates:
            continue
        
        print(f"\n{station}:")
        
        for date_str in dates:
            result = backfill_date(conn, station, date_str)
            if result > 0:
                saved += result
            else:
                failed += 1
            total += 1
            
            # Small delay between requests (rate limiting)
            import time
            time.sleep(0.3)
    
    print(f"\n{'=' * 70}")
    print(f"BACKFILL SUMMARY: {saved} observations saved, {failed} failed")
    print("=" * 70)
    
    return saved > 0


# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="METAR Gap Detection and Automated Backfill"
    )
    parser.add_argument(
        "--auto-gap-fill",
        action="store_true",
        help="Detect and automatically fill gaps"
    )
    parser.add_argument(
        "--force-backfill",
        action="store_true",
        help="Force backfill for specific date(s)"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date to backfill (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check for gaps without filling"
    )
    parser.add_argument(
        "--stations",
        type=str,
        help="Comma-separated list of stations (default: all 20)"
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=7,
        help="Number of days to check for gaps (default: 7)"
    )
    
    args = parser.parse_args()
    
    # Get stations
    if args.stations:
        stations = args.stations.split(",")
    else:
        stations = STATIONS
    
    # Connect to database
    conn = get_db_connection()
    
    try:
        if args.check_only or args.auto_gap_fill:
            # Detect gaps
            gaps = detect_gaps(conn, stations, args.lookback)
            print_gaps(gaps)
            
            if args.auto_gap_fill:
                success = auto_backfill(conn, gaps)
                return 0 if success else 1
        
        elif args.force_backfill and args.date:
            # Force backfill for specific date
            print(f"Force backfill for {args.date}")
            gaps = {s: [args.date] for s in stations}
            success = auto_backfill(conn, gaps)
            return 0 if success else 1
        
        else:
            print("Usage: metar_gap_backfill.py --auto-gap-fill | --check-only | --force-backfill --date YYYY-MM-DD")
            return 1
    
    finally:
        conn.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
