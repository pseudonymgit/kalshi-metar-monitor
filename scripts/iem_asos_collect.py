#!/usr/bin/env python3
"""
IEM ASOS 1-Minute Data Collection Script

Collects 1-minute resolution ASOS data from the Iowa Environmental Mesonet (IEM)
for all 20 Kalshi weather stations. Supports backfill and incremental modes.

Source: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
Rate limit: 1 request/second per IEM policy (as of 2026-04-21)

Usage:
    # Full backfill from 2024-01-01 to present
    python3 scripts/iem_asos_collect.py --mode backfill

    # Incremental update (last 3 days)
    python3 scripts/iem_asos_collect.py --mode incremental

    # Specific date range
    python3 scripts/iem_asos_collect.py --mode backfill --start 2026-07-01 --end 2026-08-03

Output:
    data/iem_asos_1min.db  — SQLite database with 1-minute resolution data
    data/iem_asos_collect.log — Timestamped log

Version: v1.0 2026-08-03
"""

import csv
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request

# ─── Path Setup ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = str(REPO_ROOT / "data" / "iem_asos_1min.db")
LOG_FILE = str(REPO_ROOT / "data" / "iem_asos_collect.log")

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("iem_asos_collect")

# ─── Constants ──────────────────────────────────────────────────────────────

# All 20 Kalshi weather stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# IEM ASOS download endpoint
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Data fields to request from IEM
# tmpf=dry bulb temp (°F), dwpf=dewpoint (°F), sknt=wind speed (knots),
# drct=wind direction (°), alti=altimeter (inHg), mslp=sea level pressure (mb),
# gust=wind gust (knots), vsby=visibility (miles), feel=feels like (°F),
# relh=relative humidity (%)
IEM_DATA_FIELDS = "tmpf,dwpf,sknt,drct,alti,mslp,gust,vsby,feel,relh"

# IEM rate limit: 1 request/second
IEM_RATE_LIMIT_SEC = 1.0

# Temperature sanity filter for CONUS stations
# Rejects obviously corrupted values (e.g., 39,678.8°F from parser bugs)
TEMP_MIN_F = -50.0
TEMP_MAX_F = 130.0

# Number of consecutive failures before giving up on a station
MAX_STATION_FAILURES = 5

# Backfill default: start from this date
DEFAULT_BACKFILL_START = "2024-01-01"

# Chunk size for backfill (days per request)
# IEM limit: 1,000 station-years
# 20 stations × 365 days = 7,300 station-days. Chunk at 90 days = 1,800 station-days
BACKFILL_CHUNK_DAYS = 90


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the IEM ASOS 1-minute database."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            valid TIMESTAMP NOT NULL,  -- ISO-8601 UTC timestamp
            tmpf REAL,                  -- Temperature (°F)
            dwpf REAL,                  -- Dewpoint (°F)
            sknt REAL,                  -- Wind speed (knots)
            drct REAL,                  -- Wind direction (°)
            alti REAL,                  -- Altimeter (inHg)
            mslp REAL,                  -- Sea level pressure (mb)
            gust REAL,                  -- Wind gust (knots)
            vsby REAL,                  -- Visibility (miles)
            feel REAL,                  -- Feels-like temp (°F)
            relh REAL,                  -- Relative humidity (%)
            source TEXT DEFAULT 'iem',   -- Data source identifier
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Unique index: one observation per station per timestamp
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_station_valid
        ON observations(station, valid)
    """)
    
    # Index for fast station queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_obs_station
        ON observations(station)
    """)
    
    # Index for date range queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_obs_valid
        ON observations(valid)
    """)
    
    # Tracking table for collection progress
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_tracking (
            station TEXT PRIMARY KEY,
            last_collected_date TEXT,  -- YYYY-MM-DD
            total_observations INTEGER DEFAULT 0,
            last_error TEXT,
            last_error_time TIMESTAMP
        )
    """)
    
    conn.commit()
    return conn


def get_existing_date_range(conn: sqlite3.Connection, station: str) -> Tuple[Optional[str], Optional[str]]:
    """Get the min and max valid dates for a station in the DB."""
    cur = conn.execute(
        "SELECT MIN(DATE(valid)), MAX(DATE(valid)) FROM observations WHERE station = ?",
        (station,)
    )
    row = cur.fetchone()
    return row[0], row[1]


def get_existing_count(conn: sqlite3.Connection, station: str) -> int:
    """Count existing observations for a station."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE station = ?",
        (station,)
    )
    return cur.fetchone()[0]


def get_date_gaps(conn: sqlite3.Connection, station: str, 
                  start_date: str, end_date: str) -> List[Tuple[str, str]]:
    """
    Find date gaps for a station between start_date and end_date.
    Returns list of (gap_start, gap_end) tuples.
    """
    cur = conn.execute("""
        SELECT DISTINCT DATE(valid) as obs_date
        FROM observations
        WHERE station = ? AND DATE(valid) BETWEEN ? AND ?
        ORDER BY obs_date
    """, (station, start_date, end_date))
    
    existing_dates = set(row[0] for row in cur.fetchall())
    
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    all_dates = set()
    d = start_dt
    while d <= end_dt:
        all_dates.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    
    missing = sorted(all_dates - existing_dates)
    
    # Group consecutive missing dates into gaps
    gaps = []
    if not missing:
        return gaps
    
    gap_start = missing[0]
    prev = missing[0]
    for d in missing[1:]:
        prev_dt = datetime.strptime(prev, "%Y-%m-%d")
        curr_dt = datetime.strptime(d, "%Y-%m-%d")
        if (curr_dt - prev_dt).days > 1:
            gaps.append((gap_start, prev))
            gap_start = d
        prev = d
    gaps.append((gap_start, prev))
    
    return gaps


# ═══════════════════════════════════════════════════════════════════════════
# IEM API
# ═══════════════════════════════════════════════════════════════════════════

def fetch_asos_data(station: str, yr1: int, mo1: int, dy1: int,
                    yr2: int, mo2: int, dy2: int,
                    hr1: int = 0, mi1: int = 0,
                    hr2: int = 23, mi2: int = 59) -> Optional[str]:
    """
    Fetch ASOS data from IEM for a station and date range.
    
    Returns raw CSV text, or None on failure.
    """
    params = (
        f"station={station}"
        f"&data={IEM_DATA_FIELDS}"
        f"&year1={yr1}&month1={mo1}&day1={dy1}&hour1={hr1}&minute1={mi1}"
        f"&year2={yr2}&month2={mo2}&day2={dy2}&hour2={hr2}&minute2={mi2}"
        f"&tz=UTC&format=onlycomma&missing=null&latlon=no&elev=no&direct=no"
    )
    url = f"{IEM_ASOS_URL}?{params}"
    
    try:
        req = Request(url, headers={"User-Agent": "KalshiWeatherEngine/1.0"})
        resp = urlopen(req, timeout=120)
        data = resp.read().decode("utf-8", errors="replace")
        
        # Check for HTML error response
        if data.strip().startswith("<!DOCTYPE") or data.strip().startswith("<html"):
            _LOGGER.warning(f"{station}: IEM returned HTML (probably rate-limited or error)")
            return None
        
        return data
    except Exception as e:
        _LOGGER.warning(f"{station}: IEM fetch failed: {e}")
        return None


def parse_asos_csv(raw_csv: str, station: str) -> List[Dict]:
    """
    Parse IEM ASOS CSV into observation dicts.
    IEM format (onlycomma): station,valid,tmpf,dwpf,... (no header)
    
    Temperature sanity filter: rejects values outside [-50, 130]°F for CONUS
    """
    observations = []
    lines = raw_csv.strip().split("\n")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        parts = line.split(",")
        if len(parts) < 4:
            continue
        
        # IEM onlycomma format: station,valid,tmpf,dwpf,sknt,drct,alti,mslp,gust,vsby,feel,relh
        try:
            # IEM strips leading 'K' from US ICAO codes (KATL → ATL)
            obs_station = parts[0].strip()
            if len(obs_station) == 3 and not obs_station.startswith('K'):
                obs_station = 'K' + obs_station
            valid = parts[1].strip()
            
            # Parse temperature with sanity check
            tmpf_raw = parts[2].strip() if len(parts) > 2 else ""
            tmpf = float(tmpf_raw) if tmpf_raw and tmpf_raw.lower() not in ("m", "null", "") else None
            
            # Sanity check: reject obviously corrupted temperatures
            if tmpf is not None and (tmpf < TEMP_MIN_F or tmpf > TEMP_MAX_F):
                _LOGGER.debug(f"{station}: Rejected corrupted temp {tmpf}°F at {valid}")
                tmpf = None
            
            dwpf_raw = parts[3].strip() if len(parts) > 3 else ""
            dwpf = float(dwpf_raw) if dwpf_raw and dwpf_raw.lower() not in ("m", "null", "") else None
            
            sknt_raw = parts[4].strip() if len(parts) > 4 else ""
            sknt = float(sknt_raw) if sknt_raw and sknt_raw.lower() not in ("m", "null", "") else None
            
            drct_raw = parts[5].strip() if len(parts) > 5 else ""
            drct = float(drct_raw) if drct_raw and drct_raw.lower() not in ("m", "null", "") else None
            
            alti_raw = parts[6].strip() if len(parts) > 6 else ""
            alti = float(alti_raw) if alti_raw and alti_raw.lower() not in ("m", "null", "") else None
            
            mslp_raw = parts[7].strip() if len(parts) > 7 else ""
            mslp = float(mslp_raw) if mslp_raw and mslp_raw.lower() not in ("m", "null", "") else None
            
            gust_raw = parts[8].strip() if len(parts) > 8 else ""
            gust = float(gust_raw) if gust_raw and gust_raw.lower() not in ("m", "null", "") else None
            
            vsby_raw = parts[9].strip() if len(parts) > 9 else ""
            vsby = float(vsby_raw) if vsby_raw and vsby_raw.lower() not in ("m", "null", "") else None
            
            feel_raw = parts[10].strip() if len(parts) > 10 else ""
            feel = float(feel_raw) if feel_raw and feel_raw.lower() not in ("m", "null", "") else None
            
            relh_raw = parts[11].strip() if len(parts) > 11 else ""
            relh = float(relh_raw) if relh_raw and relh_raw.lower() not in ("m", "null", "") else None
            
            obs = {
                "station": obs_station,
                "valid": valid,
                "tmpf": tmpf,
                "dwpf": dwpf,
                "sknt": sknt,
                "drct": drct,
                "alti": alti,
                "mslp": mslp,
                "gust": gust,
                "vsby": vsby,
                "feel": feel,
                "relh": relh,
            }
            observations.append(obs)
        except (ValueError, IndexError) as e:
            _LOGGER.debug(f"{station}: Parse error on line: {line[:80]}... -> {e}")
            continue
    
    return observations


# ═══════════════════════════════════════════════════════════════════════════
# Data Storage
# ═══════════════════════════════════════════════════════════════════════════

def insert_observations(conn: sqlite3.Connection, observations: List[Dict], 
                        station: str) -> int:
    """Bulk insert observations. Returns count of inserted rows."""
    if not observations:
        return 0
    
    # Use INSERT OR IGNORE to handle duplicates gracefully
    conn.executemany("""
        INSERT OR IGNORE INTO observations
        (station, valid, tmpf, dwpf, sknt, drct, alti, mslp, gust, vsby, feel, relh, source)
        VALUES (:station, :valid, :tmpf, :dwpf, :sknt, :drct, :alti, :mslp, :gust, :vsby, :feel, :relh, 'iem')
    """, observations)
    conn.commit()
    
    return len(observations)


def update_tracking(conn: sqlite3.Connection, station: str, 
                    total_obs: int, error: Optional[str] = None) -> None:
    """Update collection tracking for a station."""
    cur = conn.execute(
        "SELECT MAX(DATE(valid)) FROM observations WHERE station = ?",
        (station,)
    )
    max_date = cur.fetchone()[0]
    
    conn.execute("""
        INSERT OR REPLACE INTO collection_tracking
        (station, last_collected_date, total_observations, last_error, last_error_time)
        VALUES (?, ?, ?, ?, ?)
    """, (
        station,
        max_date,
        total_obs,
        error,
        datetime.now(timezone.utc).isoformat() if error else None,
    ))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Collection Logic
# ═══════════════════════════════════════════════════════════════════════════

def collect_station_range(conn: sqlite3.Connection, station: str,
                           start_date: str, end_date: str) -> Tuple[int, bool]:
    """
    Collect ASOS data for a station over a date range.
    Returns (observations_collected, success).
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    total_collected = 0
    last_error = None
    
    # Chunk the request to avoid IEM's 1,000 station-year limit
    current_start = start_dt
    while current_start <= end_dt:
        chunk_end = min(
            current_start + timedelta(days=BACKFILL_CHUNK_DAYS - 1),
            end_dt
        )
        
        _LOGGER.info(f"{station}: Fetching {current_start.date()} to {chunk_end.date()}")
        
        raw_csv = fetch_asos_data(
            station,
            current_start.year, current_start.month, current_start.day,
            chunk_end.year, chunk_end.month, chunk_end.day,
        )
        
        if raw_csv is None:
            last_error = f"Fetch failed for {current_start.date()} to {chunk_end.date()}"
            _LOGGER.warning(f"{station}: {last_error}")
            time.sleep(IEM_RATE_LIMIT_SEC * 3)  # Wait longer on failure
            current_start = chunk_end + timedelta(days=1)
            continue
        
        observations = parse_asos_csv(raw_csv, station)
        
        if observations:
            count = insert_observations(conn, observations, station)
            total_collected += count
            _LOGGER.info(f"{station}: {count} observations ({total_collected} total so far)")
        else:
            _LOGGER.info(f"{station}: No observations in range {current_start.date()} to {chunk_end.date()}")
        
        # IEM rate limit: 1 req/sec
        time.sleep(IEM_RATE_LIMIT_SEC)
        
        current_start = chunk_end + timedelta(days=1)
    
    total_existing = get_existing_count(conn, station)
    update_tracking(conn, station, total_existing, last_error)
    
    return total_collected, last_error is None


def backfill_all(conn: sqlite3.Connection, start_date: Optional[str] = None,
                 end_date: Optional[str] = None) -> None:
    """Backfill all stations from start_date to end_date."""
    if start_date is None:
        start_date = DEFAULT_BACKFILL_START
    if end_date is None:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    _LOGGER.info("=" * 60)
    _LOGGER.info("IEM ASOS 1-Minute Backfill — Starting")
    _LOGGER.info(f"Range: {start_date} to {end_date}")
    _LOGGER.info(f"Stations: {len(STATIONS)}")
    
    for station in STATIONS:
        station_failures = 0
        
        _LOGGER.info(f"\n{'─' * 50}")
        _LOGGER.info(f"Station: {station}")
        
        # Check existing data
        existing_count = get_existing_count(conn, station)
        min_date, max_date = get_existing_date_range(conn, station)
        
        if existing_count > 0 and min_date and max_date:
            _LOGGER.info(f"  Existing: {existing_count} obs ({min_date} to {max_date})")
            
            # Find and fill gaps
            gaps = get_date_gaps(conn, station, start_date, end_date)
            if gaps:
                _LOGGER.info(f"  Gaps to fill: {len(gaps)}")
                for gap_start, gap_end in gaps:
                    collected, success = collect_station_range(conn, station, gap_start, gap_end)
                    if not success:
                        station_failures += 1
                        if station_failures >= MAX_STATION_FAILURES:
                            _LOGGER.error(f"{station}: Too many failures, skipping")
                            break
            else:
                _LOGGER.info(f"  No gaps — station is complete")
        else:
            _LOGGER.info(f"  No existing data — full backfill")
            collected, success = collect_station_range(conn, station, start_date, end_date)
            if not success:
                _LOGGER.warning(f"{station}: Backfill had some failures")
        
        final_count = get_existing_count(conn, station)
        _LOGGER.info(f"  Final: {final_count} total observations")
    
    _LOGGER.info(f"\n{'=' * 60}")
    _LOGGER.info("IEM ASOS Backfill — Complete")
    
    # Print summary
    print(f"\n{'=' * 72}")
    print(f"  IEM ASOS 1-MINUTE DATA — COLLECTION SUMMARY")
    print(f"{'=' * 72}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Database: {DB_PATH}")
    total = 0
    for station in STATIONS:
        count = get_existing_count(conn, station)
        total += count
        print(f"  {station}: {count:>8,} observations")
    print(f"  {'─' * 42}")
    print(f"  TOTAL:  {total:>8,} observations")


def incremental_update(conn: sqlite3.Connection, days: int = 3) -> None:
    """
    Incremental update: collect recent data for all stations.
    Uses the last_collected_date as starting point, or N days back.
    """
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    _LOGGER.info("=" * 60)
    _LOGGER.info("IEM ASOS 1-Minute Incremental Update — Starting")
    
    for station in STATIONS:
        # Check tracking table for last collected date
        cur = conn.execute(
            "SELECT last_collected_date FROM collection_tracking WHERE station = ?",
            (station,)
        )
        row = cur.fetchone()
        
        if row and row[0]:
            start_date = row[0]
            _LOGGER.info(f"{station}: Updating from {start_date}")
        else:
            # No tracking data — use N days back
            start_dt = datetime.now(timezone.utc) - timedelta(days=days)
            start_date = start_dt.strftime("%Y-%m-%d")
            _LOGGER.info(f"{station}: No tracking, fetching last {days} days from {start_date}")
        
        collected, success = collect_station_range(conn, station, start_date, end_date)
        
        if collected > 0:
            _LOGGER.info(f"{station}: +{collected} new observations")
        else:
            _LOGGER.info(f"{station}: No new data")
    
    _LOGGER.info(f"\n{'=' * 60}")
    _LOGGER.info("IEM ASOS Incremental Update — Complete")


# ═══════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════

def verify_data_quality(conn: sqlite3.Connection) -> Dict:
    """Run data quality checks on the collected data."""
    results = {}
    
    for station in STATIONS:
        station_result = {"errors": [], "warnings": []}
        
        # Check for corrupted temps
        bad_temps = conn.execute("""
            SELECT COUNT(*) FROM observations
            WHERE station = ? AND (tmpf < ? OR tmpf > ?)
        """, (station, TEMP_MIN_F, TEMP_MAX_F)).fetchone()[0]
        
        if bad_temps > 0:
            station_result["errors"].append(f"{bad_temps} corrupted temperature values")
        
        # Check for data gaps
        cur = conn.execute("""
            SELECT DATE(valid) as d, COUNT(*) as cnt
            FROM observations WHERE station = ?
            GROUP BY d ORDER BY d
        """, (station,))
        
        dates = cur.fetchall()
        if len(dates) < 2:
            station_result["warnings"].append("Less than 2 days of data")
        else:
            # Check for gaps > 1 day
            prev_date = None
            gaps = 0
            for row in dates:
                curr = datetime.strptime(row[0], "%Y-%m-%d")
                if prev_date and (curr - prev_date).days > 1:
                    gaps += 1
                prev_date = curr
            if gaps > 0:
                station_result["warnings"].append(f"{gaps} date gaps found")
        
        # Check observation density
        total = get_existing_count(conn, station)
        if total > 0:
            cur = conn.execute("""
                SELECT MIN(DATE(valid)), MAX(DATE(valid)) FROM observations WHERE station = ?
            """, (station,))
            min_d, max_d = cur.fetchone()
            if min_d and max_d:
                days = (datetime.strptime(max_d, "%Y-%m-%d") - 
                       datetime.strptime(min_d, "%Y-%m-%d")).days + 1
                obs_per_day = total / max(days, 1)
                station_result["total_obs"] = total
                station_result["days"] = days
                station_result["obs_per_day"] = round(obs_per_day, 1)
                if obs_per_day < 100:
                    station_result["warnings"].append(
                        f"Low density: {obs_per_day:.1f} obs/day (expected ~1440 for 1-min)"
                    )
        
        results[station] = station_result
    
    return results


def print_quality_report(results: Dict) -> None:
    """Print data quality report."""
    print(f"\n{'=' * 72}")
    print(f"  DATA QUALITY REPORT")
    print(f"{'=' * 72}")
    
    for station in sorted(results.keys()):
        r = results[station]
        status = "✅" if not r["errors"] and not r["warnings"] else "⚠️" if r["warnings"] else "❌"
        
        details = []
        if "total_obs" in r:
            details.append(f"{r['total_obs']:,} obs")
            details.append(f"{r['days']} days")
            details.append(f"{r['obs_per_day']:.1f}/day")
        
        print(f"  {status} {station}: {'; '.join(details)}")
        
        for w in r.get("warnings", []):
            print(f"     ⚠ {w}")
        for e in r.get("errors", []):
            print(f"     ❌ {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="IEM ASOS 1-Minute Data Collection for Kalshi Weather Stations"
    )
    parser.add_argument(
        "--mode", choices=["backfill", "incremental", "verify"],
        default="incremental",
        help="Collection mode (default: incremental)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date YYYY-MM-DD (backfill mode)"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date YYYY-MM-DD (backfill mode)"
    )
    parser.add_argument(
        "--days", type=int, default=3,
        help="Days to look back for incremental update (default: 3)"
    )
    parser.add_argument(
        "--stations", type=str, default=None,
        help="Comma-separated station list (default: all 20)"
    )
    
    args = parser.parse_args()
    
    global STATIONS
    if args.stations:
        STATIONS = [s.strip().upper() for s in args.stations.split(",")]
    
    conn = init_db(DB_PATH)
    
    try:
        if args.mode == "backfill":
            backfill_all(conn, args.start, args.end)
        elif args.mode == "incremental":
            incremental_update(conn, args.days)
        elif args.mode == "verify":
            results = verify_data_quality(conn)
            print_quality_report(results)
    finally:
        conn.close()
    
    return 0


def get_observations_for_date(conn: sqlite3.Connection, station: str, date: str):
    """
    Get all 1-minute observations for a station on a given date.
    Useful for backtesting Goldilocks lane with IEM data.
    """
    import pandas as pd
    
    query = """
        SELECT valid, tmpf, dwpf, sknt, drct, alti, mslp, gust, vsby, feel, relh
        FROM observations
        WHERE station = ? AND DATE(valid) = ?
        ORDER BY valid ASC
    """
    df = pd.read_sql_query(query, conn, params=(station, date))
    df["valid"] = pd.to_datetime(df["valid"])
    return df


if __name__ == "__main__":
    sys.exit(main())