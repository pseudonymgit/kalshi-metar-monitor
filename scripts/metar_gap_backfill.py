#!/usr/bin/env python3
"""
METAR Gap Backfill — IEM ASOS Source

Backfills the ~330-day gap (Aug 2025 → Jul 2026) for 9 research stations
where the NWS API stopped returning data. Uses the Iowa Environmental Mesonet
(IEM) ASOS download service.

Target gap per station:
  KJFK: 2025-08-27 to 2026-07-25 (331 days)
  KORD: 2025-08-25 to 2026-07-25 (333 days)
  KBNA: 2025-08-27 to 2026-07-24 (330 days)
  KPDX: 2025-08-27 to 2026-07-25 (331 days)
  KSLC: 2025-08-25 to 2026-07-25 (333 days)
  PHNL: 2025-08-27 to 2026-07-19 (325 days)
  KTPA: 2025-08-27 to 2026-07-25 (331 days)
  KDTW: 2025-08-27 to 2026-07-25 (331 days)
  KCLT: 2025-08-27 to 2026-07-19 (325 days)

These are also the stations to add to the nowcast pipeline.

Source: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
Rate limit: 1 request/second per IEM policy

Usage:
    python3 scripts/metar_gap_backfill.py
    python3 scripts/metar_gap_backfill.py --stations KJFK,KORD  # specific stations

Output:
    data/metar_backfill.db  — appends to existing METAR DB
    data/metar_gap_backfill.log

Author: Gilfoyle (Aug 3, 2026)
"""

import logging
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
LOG_FILE = str(REPO_ROOT / "data" / "metar_gap_backfill.log")
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_DATA_FIELDS = "tmpf,dwpf,sknt,drct,alti,mslp,gust"
IEM_RATE_LIMIT_SEC = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("metar_gap_backfill")

# 9 research stations with gap
STATIONS = {
    "KJFK": ("2025-08-27", "2026-07-25"),
    "KORD": ("2025-08-25", "2026-07-25"),
    "KBNA": ("2025-08-27", "2026-07-24"),
    "KPDX": ("2025-08-27", "2026-07-25"),
    "KSLC": ("2025-08-25", "2026-07-25"),
    "PHNL": ("2025-08-27", "2026-07-19"),
    "KTPA": ("2025-08-27", "2026-07-25"),
    "KDTW": ("2025-08-27", "2026-07-25"),
    "KCLT": ("2025-08-27", "2026-07-19"),
}

PHYSICAL_BOUNDS = {
    "temp_f": (-50.0, 130.0),
    "dewpoint_f": (-50.0, 130.0),
    "pressure_mb": (800.0, 1100.0),
    "wind_speed_kt": (0.0, 200.0),
}


# ─── IEM API ────────────────────────────────────────────────────────────────

def fetch_asos_csv(station: str, yr1: int, mo1: int, dy1: int,
                   yr2: int, mo2: int, dy2: int) -> Optional[str]:
    """Fetch ASOS observations from IEM for a date range."""
    params = (
        f"station={station}&data={IEM_DATA_FIELDS}"
        f"&year1={yr1}&month1={mo1}&day1={dy1}&hour1=0&minute1=0"
        f"&year2={yr2}&month2={mo2}&day2={dy2}&hour2=23&minute2=59"
        f"&tz=UTC&format=onlycomma&missing=null&latlon=no&elev=no&direct=no"
    )
    url = f"{IEM_URL}?{params}"
    try:
        req = Request(url, headers={"User-Agent": "WeatherEngine-METAR-Backfill/1.0"})
        resp = urlopen(req, timeout=120)
        data = resp.read().decode("utf-8", errors="replace")
        if data.strip().startswith("<!DOCTYPE") or data.strip().startswith("<html"):
            _LOGGER.warning(f"{station}: IEM returned HTML (rate-limited or error)")
            return None
        return data
    except Exception as e:
        _LOGGER.warning(f"{station}: IEM fetch failed: {e}")
        return None


def parse_asos_csv(raw_csv: str, station: str) -> List[Dict]:
    """
    Parse IEM ASOS CSV into observation dicts.
    IEM onlycomma format: station,valid,tmpf,dwpf,sknt,drct,alti,mslp,gust
    Applies physical bounds check.
    """
    obs = []
    for line in raw_csv.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            obs_station = parts[0].strip().upper()
            if len(obs_station) == 3 and not obs_station.startswith("K"):
                obs_station = "K" + obs_station
            if obs_station != station:
                continue

            valid = parts[1].strip()

            # Temperature
            tmpf_raw = parts[2].strip() if len(parts) > 2 else ""
            tmpf = float(tmpf_raw) if tmpf_raw and tmpf_raw not in ("M", "null", "") else None
            if tmpf is not None and (tmpf < PHYSICAL_BOUNDS["temp_f"][0] or tmpf > PHYSICAL_BOUNDS["temp_f"][1]):
                continue

            # Dewpoint
            dwpf_raw = parts[3].strip() if len(parts) > 3 else ""
            dwpf = float(dwpf_raw) if dwpf_raw and dwpf_raw not in ("M", "null", "") else None
            if dwpf is not None and (dwpf < PHYSICAL_BOUNDS["dewpoint_f"][0] or dwpf > PHYSICAL_BOUNDS["dewpoint_f"][1]):
                continue

            # Wind speed
            sknt_raw = parts[4].strip() if len(parts) > 4 else ""
            sknt = float(sknt_raw) if sknt_raw and sknt_raw not in ("M", "null", "") else None
            if sknt is not None and (sknt < 0 or sknt > PHYSICAL_BOUNDS["wind_speed_kt"][1]):
                continue

            # Wind direction
            drct_raw = parts[5].strip() if len(parts) > 5 else ""
            drct = float(drct_raw) if drct_raw and drct_raw not in ("M", "null", "") else None

            # Altimeter → convert to pressure_mb
            alti_raw = parts[6].strip() if len(parts) > 6 else ""
            # IEM alti is in inches of Hg. 1 inHg = 33.8639 mb
            pressure_mb = float(alti_raw) * 33.8639 if alti_raw and alti_raw not in ("M", "null", "") else None
            if pressure_mb is not None and (pressure_mb < 800 or pressure_mb > 1100):
                continue

            # Sea level pressure
            mslp_raw = parts[7].strip() if len(parts) > 7 else ""
            slp = float(mslp_raw) if mslp_raw and mslp_raw not in ("M", "null", "") else None

            # Gust
            gust_raw = parts[8].strip() if len(parts) > 8 else ""
            gust = float(gust_raw) if gust_raw and gust_raw not in ("M", "null", "") else None

            o = {
                "station": station,
                "valid": valid,
                "tmpf": tmpf,
                "dwpf": dwpf,
                "sknt": sknt,
                "drct": drct,
                "pressure_mb": pressure_mb,
                "slp_mb": slp,
                "gust_kt": gust,
            }
            obs.append(o)
        except (ValueError, IndexError):
            continue
    return obs


# ─── Database ────────────────────────────────────────────────────────────────

def get_metar_db_schema(conn: sqlite3.Connection) -> Dict[str, str]:
    """Get column constraints from metar_observations. Returns {col: type}."""
    cur = conn.execute("PRAGMA table_info(metar_observations)")
    return {row[1]: row[2] for row in cur.fetchall()}


def insert_observations(conn: sqlite3.Connection, obs_list: List[Dict], station: str) -> int:
    """Bulk insert observations into metar_backfill.db matching existing schema."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0

    for o in obs_list:
        valid = o["valid"]
        tmpf = o["tmpf"]
        dwpf = o["dwpf"]
        sknt = o["sknt"]
        drct = o["drct"]
        pressure_mb = o["pressure_mb"]
        slp_mb = o["slp_mb"]
        gust_kt = o["gust_kt"]

        # Convert temp to °C
        temp_c = round((tmpf - 32) * 5 / 9, 1) if tmpf is not None else None
        dew_c = round((dwpf - 32) * 5 / 9, 1) if dwpf is not None else None

        # Extract date (YYYY-MM-DD) from valid timestamp
        date_utc = valid[:10] if len(valid) >= 10 else valid

        try:
            conn.execute("""
                INSERT OR IGNORE INTO metar_observations
                (station, date_utc, timestamp_utc, temp_f, temp_c,
                 dewpoint_f, dewpoint_c, wind_direction_deg, wind_speed_kt,
                 wind_gust_kt, pressure_mb, sea_level_pressure_mb,
                 source, ingestion_timestamp_utc, report_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                station, date_utc, valid, tmpf, temp_c,
                dwpf, dew_c, int(drct) if drct is not None else None, sknt,
                gust_kt, pressure_mb, slp_mb,
                "iem_asos_backfill", now, "METAR"
            ))
            inserted += 1
        except Exception as e:
            _LOGGER.debug(f"{station}: insert error for {valid}: {e}")

    conn.commit()
    return inserted


def get_existing_min_max(conn: sqlite3.Connection, station: str) -> Tuple[Optional[str], Optional[str]]:
    """Get min and max timestamp_utc for station."""
    cur = conn.execute(
        "SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM metar_observations WHERE station=?",
        (station,)
    )
    return cur.fetchone()


def count_existing_in_range(conn: sqlite3.Connection, station: str, start: str, end: str) -> int:
    """Count existing observations in date range."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM metar_observations WHERE station=? AND date_utc >= ? AND date_utc <= ?",
        (station, start, end)
    )
    return cur.fetchone()[0]


# ─── Backfill Logic ──────────────────────────────────────────────────────────

def backfill_station_gap(conn: sqlite3.Connection, station: str,
                         gap_start: str, gap_end: str) -> Tuple[int, int]:
    """
    Backfill METAR data for a station's gap period.
    Returns (observations_inserted, days_covered).
    
    Uses 30-day chunks to stay within IEM limits.
    """
    start_dt = datetime.strptime(gap_start, "%Y-%m-%d")
    end_dt = datetime.strptime(gap_end, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days + 1

    # Check what already exists in the gap
    existing = count_existing_in_range(conn, station, gap_start, gap_end)
    if existing > 0:
        _LOGGER.info(f"  {station}: {existing} existing obs in gap")

    # Chunk into 60-day requests
    chunk_days = 60
    total_inserted = 0
    total_requests = 0
    failures = 0

    for offset in range(0, total_days, chunk_days):
        chunk_start = start_dt + timedelta(days=offset)
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end_dt)

        _LOGGER.info(f"  {station}: chunk {offset//chunk_days+1} — "
                      f"{chunk_start.date()} to {chunk_end.date()}")

        raw_csv = fetch_asos_csv(
            station,
            chunk_start.year, chunk_start.month, chunk_start.day,
            chunk_end.year, chunk_end.month, chunk_end.day,
        )
        total_requests += 1

        if raw_csv is None:
            failures += 1
            if failures >= 3:
                _LOGGER.error(f"  {station}: Too many failures, aborting")
                break
            continue

        observations = parse_asos_csv(raw_csv, station)
        if observations:
            n = insert_observations(conn, observations, station)
            total_inserted += n
            _LOGGER.info(f"  {station}: +{n} obs ({total_inserted} total)")
        else:
            _LOGGER.info(f"  {station}: No observations in this chunk")

        # Rate limit
        time.sleep(IEM_RATE_LIMIT_SEC)

        # Reset failure count on success
        failures = 0

    days_covered = min(total_days, offset + chunk_days) if total_inserted > 0 else 0
    return total_inserted, total_requests


# ─── Settlement Station Check ────────────────────────────────────────────────

def check_settlement_stations(conn: sqlite3.Connection) -> Dict[str, bool]:
    """Check if settlement stations have data in the nowcast pipeline period."""
    settlement_9 = ["KAUS", "KMDW", "KMSY", "KOKC", "KSAT", "KLAS", "KPHL", "KPHX", "KNYC"]
    results = {}
    for s in settlement_9:
        cur = conn.execute(
            "SELECT MIN(date_utc), MAX(date_utc), COUNT(*) FROM metar_observations WHERE station=?",
            (s,)
        )
        row = cur.fetchone()
        if row[0]:
            has_recent = row[1] >= "2026-07-25"
            obs_count = row[2]
            results[s] = {"has_data": True, "has_recent": has_recent, "obs": obs_count,
                          "range": f"{row[0]} to {row[1]}"}
        else:
            results[s] = {"has_data": False}
    return results


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="METAR Gap Backfill — IEM ASOS")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated stations (default: all 9 gap stations)")
    parser.add_argument("--check-settlements", action="store_true",
                        help="Check settlement station coverage (no backfill)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")

    try:
        if args.check_settlements:
            print(f"\n{'=' * 60}")
            print("  Settlement Station Coverage Check")
            print(f"{'=' * 60}")
            results = check_settlement_stations(conn)
            for s, r in sorted(results.items()):
                status = "✅" if r.get("has_recent") else "⚠️" if r.get("has_data") else "❌"
                if r.get("has_data"):
                    print(f"  {status} {s}: {r['obs']:,} obs, {r['range']}")
                else:
                    print(f"  {status} {s}: NO DATA")
            return 0

        target_stations = STATIONS
        if args.stations:
            station_list = [s.strip().upper() for s in args.stations.split(",")]
            target_stations = {s: STATIONS[s] for s in station_list if s in STATIONS}

        print(f"\n{'=' * 72}")
        print("  METAR GAP BACKFILL — IEM ASOS Source")
        print(f"{'=' * 72}")
        print(f"  Stations: {len(target_stations)}")
        print(f"  Source:   IEM ASOS API ({IEM_URL})")

        total_obs = 0
        total_requests = 0

        for station, (gap_start, gap_end) in sorted(target_stations.items()):
            # Check existing data
            min_ts, max_ts = get_existing_min_max(conn, station)
            _LOGGER.info(f"\n{'─' * 50}")
            _LOGGER.info(f"Station: {station}")
            if min_ts and max_ts:
                _LOGGER.info(f"  Existing: {min_ts} to {max_ts}")
            else:
                _LOGGER.info(f"  Existing: NO DATA")
            _LOGGER.info(f"  Gap: {gap_start} to {gap_end}")

            obs, reqs = backfill_station_gap(conn, station, gap_start, gap_end)
            total_obs += obs
            total_requests += reqs

            # Verify result
            new_count = count_existing_in_range(conn, station, gap_start, gap_end)
            _LOGGER.info(f"  Result: {new_count} obs in gap ({obs} new inserted)")

        # Summary
        print(f"\n{'=' * 72}")
        print("  BACKFILL COMPLETE")
        print(f"{'=' * 72}")
        print(f"  Stations: {len(target_stations)}")
        print(f"  New observations: {total_obs:,}")
        print(f"  API requests: {total_requests}")
        print(f"  Database: {DB_PATH}")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())