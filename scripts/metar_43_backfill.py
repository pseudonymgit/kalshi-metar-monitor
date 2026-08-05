#!/usr/bin/env python3
"""
METAR 43 Stations Backfill — IEM ASOS Source

Backfills the 43 extra METAR stations from 2021-01-01 to today.
These stations currently only have Jul 22-30 data (8 days).

Target stations:
KACT, KACY, KAHN, KAXH, KAXN, KBED, KBLH, KBLI, KBTR, KBWI,
KCHO, KCLM, KCMA, KCOS, KCSG, KCXO, KEED, KEWR, KFLL, KFSM,
KGPT, KHDC, KIFP, KIGM, KLAR, KLAW, KMCN, KMKE, KMKT, KMRY,
KNTD, KORH, KPIA, KPUB, KPVD, KRIC, KRSW, KSBA, KSPS, KSQL,
KSTP, KSTS, KTIK

Source: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
Rate limit: 1 request/second per IEM policy

Usage:
    python3 scripts/metar_43_backfill.py
    python3 scripts/metar_43_backfill.py --stations KACT,KACY

Output:
    data/metar_backfill.db  — appends to existing METAR DB
    /tmp/metar_extra_backfill.log

Author: Donna Subagent (Aug 5, 2026)
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
LOG_FILE = "/tmp/metar_extra_backfill.log"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_DATA_FIELDS = "tmpf,dwpf,sknt,drct,alti,mslp,gust"
IEM_RATE_LIMIT_SEC = 1.0

# 43 stations to backfill
STATIONS = [
    "KACT", "KACY", "KAHN", "KAXH", "KAXN", "KBED", "KBLH", "KBLI", "KBTR", "KBWI",
    "KCHO", "KCLM", "KCMA", "KCOS", "KCSG", "KCXO", "KEED", "KEWR", "KFLL", "KFSM",
    "KGPT", "KHDC", "KIFP", "KIGM", "KLAR", "KLAW", "KMCN", "KMKE", "KMKT", "KMRY",
    "KNTD", "KORH", "KPIA", "KPUB", "KPVD", "KRIC", "KRSW", "KSBA", "KSPS", "KSQL",
    "KSTP", "KSTS", "KTIK"
]

# Backfill date range: 2021-01-01 to today
TODAY = datetime.now(timezone.utc).date()
START_DATE = "2021-01-01"
END_DATE = TODAY.isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("metar_43_backfill")


def fetch_asos_csv(station: str, yr1: int, mo1: int, dy1: int,
                   yr2: int, mo2: int, dy2: int):
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


def parse_asos_csv(raw_csv: str, station: str):
    """Parse IEM ASOS CSV into observation dicts."""
    obs = []
    for line in raw_csv.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("station"):
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
            tmpf_raw = parts[2].strip() if len(parts) > 2 else ""
            dwpf_raw = parts[3].strip() if len(parts) > 3 else ""
            sknt_raw = parts[4].strip() if len(parts) > 4 else ""
            drct_raw = parts[5].strip() if len(parts) > 5 else ""
            alti_raw = parts[6].strip() if len(parts) > 6 else ""
            mslp_raw = parts[7].strip() if len(parts) > 7 else ""
            gust_raw = parts[8].strip() if len(parts) > 8 else ""

            # Parse values
            tmpf = float(tmpf_raw) if tmpf_raw and tmpf_raw not in ("M", "null", "") else None
            dwpf = float(dwpf_raw) if dwpf_raw and dwpf_raw not in ("M", "null", "") else None
            sknt = float(sknt_raw) if sknt_raw and sknt_raw not in ("M", "null", "") else None
            drct = float(drct_raw) if drct_raw and drct_raw not in ("M", "null", "") else None
            pressure_mb = float(alti_raw) * 33.8639 if alti_raw and alti_raw not in ("M", "null", "") else None
            slp = float(mslp_raw) if mslp_raw and mslp_raw not in ("M", "null", "") else None
            gust = float(gust_raw) if gust_raw and gust_raw not in ("M", "null", "") else None

            # Basic validation
            if tmpf is not None and (tmpf < -50 or tmpf > 130):
                continue
            if pressure_mb is not None and (pressure_mb < 800 or pressure_mb > 1100):
                continue

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


def insert_observations(conn, obs_list, station):
    """Bulk insert observations into metar_backfill.db."""
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

        # Extract date from valid timestamp
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
                "iem_asos_backfill_43", now, "METAR"
            ))
            inserted += 1
        except Exception as e:
            _LOGGER.debug(f"{station}: insert error for {valid}: {e}")

    conn.commit()
    return inserted


def backfill_station(conn, station, start_date, end_date):
    """Backfill a single station."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days + 1
    
    _LOGGER.info(f"{station}: Backfilling {total_days} days ({start_date} to {end_date})")
    
    # Use 60-day chunks to stay within IEM limits
    chunk_days = 60
    total_inserted = 0
    failures = 0
    
    for offset in range(0, total_days, chunk_days):
        chunk_start = start_dt + timedelta(days=offset)
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end_dt)
        
        _LOGGER.info(f"{station}: Chunk {offset//chunk_days+1} — {chunk_start.date()} to {chunk_end.date()}")
        
        raw_csv = fetch_asos_csv(
            station,
            chunk_start.year, chunk_start.month, chunk_start.day,
            chunk_end.year, chunk_end.month, chunk_end.day,
        )
        
        if raw_csv is None:
            failures += 1
            if failures >= 3:
                _LOGGER.error(f"{station}: Too many failures, aborting")
                break
            continue
        
        observations = parse_asos_csv(raw_csv, station)
        if observations:
            n = insert_observations(conn, observations, station)
            total_inserted += n
            _LOGGER.info(f"{station}: +{n} obs (total: {total_inserted})")
        else:
            _LOGGER.info(f"{station}: No observations in this chunk")
        
        time.sleep(IEM_RATE_LIMIT_SEC)
        failures = 0  # Reset on success
    
    return total_inserted


def main():
    import argparse
    parser = argparse.ArgumentParser(description="METAR 43 Stations Backfill")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated stations (default: all 43)")
    args = parser.parse_args()
    
    if not Path(DB_PATH).exists():
        _LOGGER.error(f"Database not found: {DB_PATH}")
        return 1
    
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    
    target_stations = STATIONS
    if args.stations:
        station_list = [s.strip().upper() for s in args.stations.split(",")]
        target_stations = [s for s in station_list if s in STATIONS]
    
    _LOGGER.info(f"Starting 43 stations backfill")
    _LOGGER.info(f"Date range: {START_DATE} to {END_DATE}")
    _LOGGER.info(f"Stations: {len(target_stations)}")
    
    total_obs = 0
    start_time = time.time()
    
    try:
        for i, station in enumerate(target_stations):
            _LOGGER.info(f"[{i+1}/{len(target_stations)}] Processing {station}")
            obs = backfill_station(conn, station, START_DATE, END_DATE)
            total_obs += obs
            _LOGGER.info(f"[{i+1}/{len(target_stations)}] {station}: {obs} observations inserted")
    
    finally:
        conn.close()
    
    elapsed = time.time() - start_time
    _LOGGER.info(f"Backfill complete. Total observations inserted: {total_obs}")
    _LOGGER.info(f"Elapsed time: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())