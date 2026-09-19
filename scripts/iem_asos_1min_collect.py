#!/usr/bin/env python3
"""
IEM ASOS TRUE 1-MINUTE Data Collection Script.

Collects genuine 1-minute resolution ASOS temperature data from the Iowa
Environmental Mesonet (IEM) NCEI 1-minute archive for all 20 Kalshi weather
stations. Supports backfill and incremental modes.

Source: https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py

WHY THIS SCRIPT EXISTS (root cause, 2026-09-19):
  The prior collector (`iem_asos_collect.py`) hit `asos.py`, which only ever
  returns temperature at ~hourly cadence (routine METAR) — even though its
  5-minute transmission grid exists, `tmpf` is null on the non-hourly rows.
  `asos.py?report_type=2` is *worse*: it filters to hourly METARs only.
  The genuine sub-hourly temperature source is `asos1min.py`, which returns
  NCEI 1-minute observations (~60 obs/hour, real temp every minute).

  Station IDs here are the 3-letter form (ATL, not KATL) and are normalized
  to 4-letter ICAO on write so the `observations` table stays compatible with
  the existing schema/consumers.

KEY CONSTRAINTS (IEM asos1min.py):
  * ~24h availability delay (NCEI collects from ASOS sites daily). Data is
    NOT live; latest complete day is ~yesterday. This is fine for backfill
    and for trajectory-nowcast testing against historical settlement truth.
  * Only 4 variables are available: tmpf, dwpf, sknt, drct.
  * Rate limit: 1 request/second (IEM policy).

Usage:
    # Full backfill from 2024-01-01 to present
    python3 scripts/iem_asos_1min_collect.py --mode backfill

    # Incremental update (last N days)
    python3 scripts/iem_asos_1min_collect.py --mode incremental --days 5

    # Specific range
    python3 scripts/iem_asos_1min_collect.py --mode backfill --start 2026-07-01 --end 2026-09-18

Output:
    data/iem_asos_1min.db  — SQLite DB (observations table, 1-minute resolution)
    data/iem_asos_1min_collect.log — timestamped log

Version: v1.0 2026-09-19
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.request import Request, urlopen

# ─── Path Setup ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = str(REPO_ROOT / "data" / "iem_asos_1min.db")
LOG_FILE = str(REPO_ROOT / "data" / "iem_asos_1min_collect.log")

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("iem_asos_1min_collect")

# ─── Constants ──────────────────────────────────────────────────────────────

# All 20 Kalshi weather stations (4-letter ICAO) and their 3-letter IEM IDs.
# The asos1min.py endpoint expects 3-letter station IDs (leading K stripped).
STATIONS_ICAO = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]
ICAO_TO_IEM3 = {icao: icao[1:] for icao in STATIONS_ICAO}
IEM3_TO_ICAO = {v: k for k, v in ICAO_TO_IEM3.items()}

# IEM 1-minute ASOS endpoint
IEM_1MIN_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"

# Only 4 variables are available on the 1-minute endpoint.
IEM_1MIN_VARS = "tmpf,dwpf,sknt,drct"

# IEM rate limit: 1 request/second
IEM_RATE_LIMIT_SEC = 1.0

# Temperature sanity filter for CONUS stations
TEMP_MIN_F = -50.0
TEMP_MAX_F = 130.0

# Consecutive failures before skipping a station
MAX_STATION_FAILURES = 5

# Backfill default start. NCEI 1-min ASOS archive coverage is uneven before
# ~2020 and varies by station; 2024-01-01 matches the prior collector's default.
DEFAULT_BACKFILL_START = "2024-01-01"

# Chunk size (days per request). 1-minute data is ~1440 rows/station/day, so
# keep chunks modest to bound memory and avoid huge single responses.
BACKFILL_CHUNK_DAYS = 30


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the IEM ASOS 1-minute database (schema-compatible with the
    prior `iem_asos_collect.py` `observations` table so consumers keep working)."""
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
            alti REAL,                  -- Altimeter (inHg)  [unavailable on 1min]
            mslp REAL,                  -- Sea level pressure [unavailable]
            gust REAL,                  -- Wind gust (knots) [unavailable]
            vsby REAL,                  -- Visibility (miles) [unavailable]
            feel REAL,                  -- Feels-like temp (°F) [unavailable]
            relh REAL,                  -- Relative humidity (%) [unavailable]
            source TEXT DEFAULT 'iem_1min',
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_station_valid
        ON observations(station, valid)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_obs_station
        ON observations(station)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_obs_valid
        ON observations(valid)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_tracking (
            station TEXT PRIMARY KEY,
            last_collected_date TEXT,
            total_observations INTEGER DEFAULT 0,
            last_error TEXT,
            last_error_time TIMESTAMP
        )
    """)

    conn.commit()
    return conn


def get_existing_count(conn: sqlite3.Connection, station: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE station = ?", (station,)
    )
    return cur.fetchone()[0]


def get_existing_date_range(conn: sqlite3.Connection, station: str) -> Tuple[Optional[str], Optional[str]]:
    cur = conn.execute(
        "SELECT MIN(DATE(valid)), MAX(DATE(valid)) FROM observations WHERE station = ?",
        (station,),
    )
    row = cur.fetchone()
    return row[0], row[1]


def get_date_gaps(conn: sqlite3.Connection, station: str,
                  start_date: str, end_date: str) -> List[Tuple[str, str]]:
    """Find date gaps (days with zero obs) for a station within [start,end]."""
    cur = conn.execute("""
        SELECT DISTINCT DATE(valid) AS d FROM observations
        WHERE station = ? AND DATE(valid) BETWEEN ? AND ?
    """, (station, start_date, end_date))
    existing = set(r[0] for r in cur.fetchall())

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    missing = []
    d = start_dt
    while d <= end_dt:
        ds = d.strftime("%Y-%m-%d")
        if ds not in existing:
            missing.append(ds)
        d += timedelta(days=1)

    gaps = []
    if not missing:
        return gaps
    gap_start = missing[0]
    prev = missing[0]
    for d in missing[1:]:
        if (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(prev, "%Y-%m-%d")).days > 1:
            gaps.append((gap_start, prev))
            gap_start = d
        prev = d
    gaps.append((gap_start, prev))
    return gaps


# ═══════════════════════════════════════════════════════════════════════════
# IEM 1-minute API
# ═══════════════════════════════════════════════════════════════════════════

def fetch_1min_data(station_3: str, sts_iso: str, ets_iso: str) -> Optional[str]:
    """Fetch 1-minute ASOS data from IEM for a 3-letter station over an ISO range.

    Returns raw CSV text (with header), or None on failure.
    """
    from urllib.parse import quote

    params = (
        f"station={station_3}"
        f"&vars={IEM_1MIN_VARS}"
        f"&sts={sts_iso}&ets={ets_iso}"
        f"&tz=UTC&format=onlycomma&what=download"
    )
    url = f"{IEM_1MIN_URL}?{quote(params, safe='=&:')}"

    try:
        req = Request(url, headers={"User-Agent": "KalshiWeatherEngine/1.0"})
        resp = urlopen(req, timeout=180)
        data = resp.read().decode("utf-8", errors="replace")
        if data.strip().startswith("<!DOCTYPE") or data.strip().startswith("<html"):
            _LOGGER.warning(f"{station_3}: IEM returned HTML (rate-limited or error)")
            return None
        return data
    except Exception as e:
        _LOGGER.warning(f"{station_3}: IEM 1-min fetch failed: {e}")
        return None


def parse_1min_csv(raw_csv: str, station_icao: str) -> List[dict]:
    """Parse asos1min.py CSV into observation dicts.

    Format (onlycomma, header present):
        station,station_name,valid(UTC),tmpf,dwpf,sknt,drct
        ATL,ATLANTA INTL ARPT,2026-09-18 00:00,83,65,2,151

    Only tmpf/dwpf/sknt/drct are populated; alti/mslp/gust/vsby/feel/relh are
    left None (unavailable on the 1-minute endpoint).
    """
    obs = []
    for line in raw_csv.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("station,"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        # parts: [station3, name, valid, tmpf, dwpf, sknt, drct]
        try:
            valid = parts[2].strip()

            def _f(s):
                s = s.strip()
                if not s or s.lower() in ("m", "null", ""):
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None

            tmpf = _f(parts[3]) if len(parts) > 3 else None
            dwpf = _f(parts[4]) if len(parts) > 4 else None
            sknt = _f(parts[5]) if len(parts) > 5 else None
            drct = _f(parts[6]) if len(parts) > 6 else None

            # Temperature sanity filter
            if tmpf is not None and (tmpf < TEMP_MIN_F or tmpf > TEMP_MAX_F):
                tmpf = None

            obs.append({
                "station": station_icao,
                "valid": valid,
                "tmpf": tmpf,
                "dwpf": dwpf,
                "sknt": sknt,
                "drct": drct,
                "alti": None, "mslp": None, "gust": None,
                "vsby": None, "feel": None, "relh": None,
            })
        except (ValueError, IndexError):
            continue
    return obs


def insert_observations(conn: sqlite3.Connection, observations: List[dict]) -> int:
    if not observations:
        return 0
    conn.executemany("""
        INSERT OR IGNORE INTO observations
        (station, valid, tmpf, dwpf, sknt, drct, alti, mslp, gust, vsby, feel, relh, source)
        VALUES (:station, :valid, :tmpf, :dwpf, :sknt, :drct, :alti, :mslp, :gust, :vsby, :feel, :relh, 'iem_1min')
    """, observations)
    conn.commit()
    return len(observations)


def update_tracking(conn: sqlite3.Connection, station: str, total_obs: int,
                    error: Optional[str] = None) -> None:
    cur = conn.execute("SELECT MAX(DATE(valid)) FROM observations WHERE station = ?", (station,))
    max_date = cur.fetchone()[0]
    conn.execute("""
        INSERT OR REPLACE INTO collection_tracking
        (station, last_collected_date, total_observations, last_error, last_error_time)
        VALUES (?, ?, ?, ?, ?)
    """, (station, max_date, total_obs, error,
          datetime.now(timezone.utc).isoformat() if error else None))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Collection Logic
# ═══════════════════════════════════════════════════════════════════════════

def collect_station_range(conn: sqlite3.Connection, station_icao: str,
                          start_date: str, end_date: str) -> Tuple[int, bool]:
    """Collect 1-min data for a station over a date range. Returns (n, success)."""
    station_3 = ICAO_TO_IEM3[station_icao]
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    total = 0
    last_error = None
    current = start_dt
    while current <= end_dt:
        chunk_end = min(current + timedelta(days=BACKFILL_CHUNK_DAYS - 1), end_dt)
        sts = current.strftime("%Y-%m-%dT00:00Z")
        ets = (chunk_end + timedelta(days=1)).strftime("%Y-%m-%dT00:00Z")

        raw = fetch_1min_data(station_3, sts, ets)
        if raw is None:
            last_error = f"Fetch failed {current.date()}..{chunk_end.date()}"
            time.sleep(IEM_RATE_LIMIT_SEC * 3)
            current = chunk_end + timedelta(days=1)
            continue

        obs = parse_1min_csv(raw, station_icao)
        if obs:
            total += insert_observations(conn, obs)
        time.sleep(IEM_RATE_LIMIT_SEC)
        current = chunk_end + timedelta(days=1)

    update_tracking(conn, station_icao, get_existing_count(conn, station_icao), last_error)
    return total, last_error is None


def backfill_all(conn: sqlite3.Connection, start_date: Optional[str] = None,
                 end_date: Optional[str] = None) -> None:
    if start_date is None:
        start_date = DEFAULT_BACKFILL_START
    if end_date is None:
        # 1-min data has ~24h delay; default end to yesterday to avoid empty tail.
        end_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    _LOGGER.info("=" * 60)
    _LOGGER.info("IEM ASOS 1-MINUTE (NCEI) Backfill — Starting")
    _LOGGER.info(f"Range: {start_date} to {end_date}")

    for icao in STATIONS_ICAO:
        failures = 0
        _LOGGER.info(f"Station: {icao} ({ICAO_TO_IEM3[icao]})")
        existing = get_existing_count(conn, icao)
        mn, mx = get_existing_date_range(conn, icao)
        if existing > 0 and mn and mx:
            _LOGGER.info(f"  Existing: {existing} obs ({mn}..{mx})")
            gaps = get_date_gaps(conn, icao, start_date, end_date)
            if gaps:
                _LOGGER.info(f"  Gaps: {len(gaps)}")
                for gs, ge in gaps:
                    n, ok = collect_station_range(conn, icao, gs, ge)
                    if not ok:
                        failures += 1
                        if failures >= MAX_STATION_FAILURES:
                            _LOGGER.error(f"{icao}: too many failures, skip")
                            break
            else:
                _LOGGER.info("  No gaps")
        else:
            _LOGGER.info("  No existing data — full backfill")
            collect_station_range(conn, icao, start_date, end_date)

        _LOGGER.info(f"  Final: {get_existing_count(conn, icao)} obs")

    _LOGGER.info("=" * 60)
    _LOGGER.info("Backfill complete")
    print_summary(conn)


def incremental_update(conn: sqlite3.Connection, days: int = 5) -> None:
    end_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    _LOGGER.info("IEM ASOS 1-MINUTE Incremental Update — Starting")
    for icao in STATIONS_ICAO:
        cur = conn.execute(
            "SELECT last_collected_date FROM collection_tracking WHERE station = ?", (icao,)
        )
        row = cur.fetchone()
        if row and row[0]:
            start_date = row[0]
        else:
            start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        n, _ = collect_station_range(conn, icao, start_date, end_date)
        _LOGGER.info(f"{icao}: +{n} new obs")
    print_summary(conn)


def print_summary(conn: sqlite3.Connection) -> None:
    print(f"\n{'=' * 60}")
    print("  IEM ASOS 1-MINUTE — SUMMARY")
    print("=" * 60)
    total = 0
    for icao in STATIONS_ICAO:
        c = get_existing_count(conn, icao)
        total += c
        print(f"  {icao}: {c:>10,}")
    print(f"  TOTAL: {total:>11,}")


# ═══════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════

def verify_subhourly(conn: sqlite3.Connection) -> dict:
    """Measure inter-observation gap distribution to confirm TRUE sub-hourly."""
    out = {}
    for icao in STATIONS_ICAO:
        rows = [r[0] for r in conn.execute(
            "SELECT valid FROM observations WHERE station=? AND tmpf IS NOT NULL ORDER BY valid",
            (icao,),
        )]
        gaps = []
        for i in range(1, len(rows)):
            g = (datetime.fromisoformat(rows[i]) - datetime.fromisoformat(rows[i - 1])).total_seconds() / 60.0
            if 0 < g < 180:
                gaps.append(g)
        if not gaps:
            out[icao] = {"n_obs": len(rows), "median_gap_min": None, "frac_lt15min": None}
            continue
        gaps.sort()
        n = len(gaps)
        out[icao] = {
            "n_obs": len(rows),
            "median_gap_min": round(gaps[n // 2], 1),
            "frac_lt15min": round(sum(1 for g in gaps if g < 15) / n, 4),
            "frac_around60min": round(sum(1 for g in gaps if 55 <= g <= 65) / n, 4),
        }
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(description="IEM ASOS 1-minute (NCEI) collector")
    p.add_argument("--mode", choices=["backfill", "incremental", "verify"], default="incremental")
    p.add_argument("--start", default=None, help="Start YYYY-MM-DD (backfill)")
    p.add_argument("--end", default=None, help="End YYYY-MM-DD (backfill)")
    p.add_argument("--days", type=int, default=5, help="Days back for incremental")
    p.add_argument("--stations", default=None, help="Comma-separated ICAO (default all 20)")
    args = p.parse_args()

    global STATIONS_ICAO, ICAO_TO_IEM3, IEM3_TO_ICAO
    if args.stations:
        STATIONS_ICAO = [s.strip().upper() for s in args.stations.split(",")]
        ICAO_TO_IEM3 = {icao: icao[1:] for icao in STATIONS_ICAO}
        IEM3_TO_ICAO = {v: k for k, v in ICAO_TO_IEM3.items()}

    conn = init_db(DB_PATH)
    try:
        if args.mode == "backfill":
            backfill_all(conn, args.start, args.end)
        elif args.mode == "incremental":
            incremental_update(conn, args.days)
        elif args.mode == "verify":
            res = verify_subhourly(conn)
            for icao, d in res.items():
                print(f"  {icao}: n={d['n_obs']} med={d['median_gap_min']}min "
                      f"<15min={d['frac_lt15min']} ~60min={d['frac_around60min']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
