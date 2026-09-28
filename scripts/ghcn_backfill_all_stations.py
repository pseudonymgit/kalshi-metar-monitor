#!/usr/bin/env python3
"""
GHCN-Daily Backfill — All Stations (standalone deterministic rebuild)
======================================================================

Reconstructed from the deleted `scripts/ghcn_auto_refresh.py` (bytecode
recovered from scripts/__pycache__/ghcn_auto_refresh.cpython-311.pyc).
Same station mapping, same .dly parsing, same schema; one deliberate
change: UPSERT into the existing data/ghcn_settlements.db instead of
atomic full rebuild, so the existing KLAX/KNYC rows are preserved and
the remaining stations are added in place. (Rationale: the prior
rebuild-to-temp design wipes stations not included in the current run;
upsert is strictly additive and idempotent.)

Downloads NOAA GHCN-Daily .dly files per station, parses TMAX/TMIN
(tenths of degrees C -> degrees F), and upserts rows into:

    data/ghcn_settlements.db  ->  table ghcn_settlements
        station TEXT NOT NULL,
        date   TEXT NOT NULL,
        high_f REAL,
        low_f  REAL,
        source TEXT DEFAULT 'ghcn_daily',
        PRIMARY KEY (station, date)

(data/ghcn_settlement.db is a symlink to the same file.)

Usage:
    python3 scripts/ghcn_backfill_all_stations.py
        # all stations: download missing .dly, parse, upsert
    python3 scripts/ghcn_backfill_all_stations.py --stations KATL,KBOS
    python3 scripts/ghcn_backfill_all_stations.py --no-fetch   # parse local .dly only
    python3 scripts/ghcn_backfill_all_stations.py --force      # re-download cached files

Depends on: urllib.request (stdlib), sqlite3 (stdlib). No AI/ML in the loop.
"""

import argparse
import logging
import sqlite3
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RAW_DIR = REPO_ROOT / "data" / "external" / "ghcn_daily" / "raw"
META_DIR = REPO_ROOT / "data" / "external" / "ghcn_daily" / "metadata"
OUTPUT_DB = REPO_ROOT / "data" / "ghcn_settlements.db"

BASE_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily"
USER_AGENT = "weather-engine/ghcn-backfill/1.0"

# Station universe: the 20 canonical stations from ghcn_auto_refresh.py
# plus KDAL (Dallas Love Field), which appears in the engine's
# ALL_STATIONS lists (core/time_decay.py, core/forecast_disagreement.py)
# and in the 20-station task universe. KDAL -> DALLAS FAA AP (USW00013960),
# verified against official ghcnd-stations.txt (32.8383, -96.8358, TX).
# NOTE: USW00013959 is WACO RGNL AP — not KDAL. Do not use it.
ICAO_TO_GHCN = {
    "KATL": "USW00013874",  # Atlanta Hartsfield-Jackson
    "KAUS": "USW00013904",  # Austin Bergstrom
    "KBOS": "USW00014739",  # Boston Logan
    "KDAL": "USW00013960",  # Dallas Love Field (DALLAS FAA AP)
    "KDCA": "USW00013743",  # Washington DC Reagan National
    "KDEN": "USW00003017",  # Denver Stapleton/Intl
    "KDFW": "USW00003927",  # Dallas-Fort Worth
    "KHOU": "USW00012918",  # Houston Hobby
    "KLAS": "USW00023169",  # Las Vegas McCarran
    "KLAX": "USW00023174",  # Los Angeles Intl
    "KMDW": "USW00014819",  # Chicago Midway
    "KMIA": "USW00012839",  # Miami Intl
    "KMSP": "USW00014922",  # Minneapolis-St Paul
    "KMSY": "USW00012916",  # New Orleans Intl
    "KNYC": "USW00094728",  # New York Central Park
    "KOKC": "USW00013967",  # Oklahoma City Will Rogers
    "KPHL": "USW00013739",  # Philadelphia Intl
    "KPHX": "USW00023183",  # Phoenix Sky Harbor
    "KSAT": "USW00012921",  # San Antonio Intl
    "KSEA": "USW00024233",  # Seattle Tacoma
    "KSFO": "USW00023234",  # San Francisco Intl
}

GHCN_TO_ICAO = {v: k for k, v in ICAO_TO_GHCN.items()}

_LOGGER = logging.getLogger("ghcn_backfill")


def _fetch_file(url: str, dest: Path, force: bool = False, timeout: int = 120) -> bool:
    """Download url to dest. Skip if cached unless force. True = downloaded."""
    if dest.exists() and not force:
        _LOGGER.info("  Cached: %s", dest.name)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            dest.write_bytes(data)
            _LOGGER.info("  Downloaded: %s (%.0f KB)", dest.name, len(data) / 1024)
            return True
        except Exception as exc:  # noqa: BLE001 - log and retry
            last_err = exc
            _LOGGER.warning("  Fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"Fetch failed after retries: {url} -> {dest} ({last_err})")


def download_all(stations: list[str], rate_limit: float = 0.5, force: bool = False) -> tuple[int, int]:
    """Download per-station .dly files. Returns (successes, attempts).

    force=True re-downloads every file even if a local copy exists. NOAA
    publishes the same all/<id>.dly file with new rows appended through the
    current day, so a stale cache must be refreshed — a bare existence check
    would otherwise freeze the DB at whatever date the cache was first written.
    """
    ok = 0
    attempted = 0
    for i, icao in enumerate(stations):
        ghcn_id = ICAO_TO_GHCN.get(icao)
        if ghcn_id is None:
            _LOGGER.warning("  Unknown station: %s — skipping", icao)
            continue
        url = f"{BASE_URL}/all/{ghcn_id}.dly"
        dest = RAW_DIR / f"{ghcn_id}.dly"
        attempted += 1
        try:
            if _fetch_file(url, dest, force=force):
                ok += 1
            else:
                ok += 1  # cached counts as available
        except RuntimeError as exc:
            _LOGGER.error("%s", exc)
        if i < len(stations) - 1 and rate_limit > 0:
            time.sleep(rate_limit)
    return ok, attempted


def parse_dly_file(filepath: Path) -> dict[str, dict[str, float | None]]:
    """Parse a GHCN-Daily .dly file for TMAX and TMIN.

    Faithful reimplementation of the deleted ghcn_auto_refresh.py parser:
    fixed-width rows, 31 daily 8-char fields (VALUE5 + MFLAG + QFLAG + SFLAG),
    -9999 = missing, tenths of C converted with round(c10/10*9/5+32, 1).
    """
    results: dict[str, dict[str, float | None]] = {}
    with open(filepath, "r", encoding="ascii", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or len(line) < 21:
                continue
            elem_code = line[17:21].strip()
            if elem_code not in ("TMAX", "TMIN"):
                continue
            try:
                year = int(line[11:15])
                month = int(line[15:17])
            except (ValueError, IndexError):
                continue
            for day_idx in range(31):
                offset = 21 + day_idx * 8
                if offset + 5 > len(line):
                    break
                raw_val = line[offset:offset + 5].strip()
                if not raw_val:
                    continue
                try:
                    val_c10 = int(raw_val)
                except ValueError:
                    continue
                if val_c10 == -9999:
                    continue
                try:
                    d = date(year, month, day_idx + 1)
                except ValueError:
                    continue
                date_iso = d.isoformat()
                if date_iso not in results:
                    results[date_iso] = {"tmax_f": None, "tmin_f": None}
                actual_f = round((val_c10 / 10.0) * 9.0 / 5.0 + 32.0, 1)
                if elem_code == "TMAX":
                    results[date_iso]["tmax_f"] = actual_f
                else:  # TMIN
                    results[date_iso]["tmin_f"] = actual_f
    return results


def parse_all_dly(stations: list[str]) -> dict[str, dict]:
    """Parse local .dly files. Returns {icao: {"ghcn_id":..., "dates": {...}}}."""
    parsed: dict[str, dict] = {}
    for icao in stations:
        ghcn_id = ICAO_TO_GHCN.get(icao)
        if ghcn_id is None:
            continue
        filepath = RAW_DIR / f"{ghcn_id}.dly"
        if not filepath.exists():
            _LOGGER.warning("  Missing .dly for %s (%s) — run with default flags to fetch", icao, ghcn_id)
            continue
        dates = parse_dly_file(filepath)
        parsed[icao] = {"ghcn_id": ghcn_id, "dates": dates}
        _LOGGER.info("  %s (%s): %d dates parsed", icao, ghcn_id, len(dates))
    return parsed


def _build_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if absent (idempotent)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ghcn_settlements (
            station TEXT NOT NULL,
            date TEXT NOT NULL,
            high_f REAL,
            low_f REAL,
            source TEXT DEFAULT 'ghcn_daily',
            PRIMARY KEY (station, date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ghcn_settlements_date
        ON ghcn_settlements(date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ghcn_settlements_station
        ON ghcn_settlements(station)
        """
    )
    conn.commit()


def upsert_db(parsed: dict[str, dict], output_db: Path = OUTPUT_DB) -> dict:
    """Upsert parsed GHCN data into the existing ghcn_settlements.db.

    INSERT OR REPLACE on (station, date): never deletes existing rows,
    refreshes overlapping dates, adds new stations in place.
    """
    if not parsed:
        raise RuntimeError("No data rows to insert — DB left untouched")
    conn = sqlite3.connect(output_db)
    try:
        _build_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ghcn_settlements")
        rows_before = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT station) FROM ghcn_settlements")
        stations_before = cur.fetchone()[0]

        rows = []
        for icao, payload in parsed.items():
            for date_iso, temps in payload["dates"].items():
                rows.append(
                    (icao, date_iso, temps.get("tmax_f"), temps.get("tmin_f"))
                )
        cur.executemany(
            "INSERT OR REPLACE INTO ghcn_settlements (station, date, high_f, low_f)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM ghcn_settlements")
        row_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT station) FROM ghcn_settlements")
        station_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(date), MAX(date) FROM ghcn_settlements")
        date_min, date_max = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM ghcn_settlements WHERE high_f IS NOT NULL")
        with_tmax = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ghcn_settlements WHERE low_f IS NOT NULL")
        with_tmin = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "rows_inserted": len(rows),
        "rows_before": rows_before,
        "rows_after": row_count,
        "stations_before": stations_before,
        "stations_after": station_count,
        "date_min": date_min,
        "date_max": date_max,
        "with_tmax": with_tmax,
        "with_tmin": with_tmin,
    }


def per_station_stats(output_db: Path = OUTPUT_DB) -> list[tuple]:
    conn = sqlite3.connect(output_db)
    try:
        cur = conn.cursor()
        return list(
            cur.execute(
                "SELECT station, COUNT(*), MIN(date), MAX(date),"
                " SUM(high_f IS NOT NULL), SUM(low_f IS NOT NULL)"
                " FROM ghcn_settlements GROUP BY station ORDER BY station"
            )
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="GHCN-Daily backfill -> ghcn_settlements.db")
    parser.add_argument("--no-fetch", action="store_true", help="Skip download; use local .dly only")
    parser.add_argument("--force", action="store_true", help="Re-download even if cached")
    parser.add_argument("--stations", help="Comma-separated ICAO subset (default: all)")
    parser.add_argument("--rate-limit", type=float, default=0.5, help="Seconds between NOAA requests")
    args = parser.parse_args()

    stations = list(ICAO_TO_GHCN.keys())
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
        unknown = [s for s in stations if s not in ICAO_TO_GHCN]
        if unknown:
            _LOGGER.error("Unknown station codes: %s", ", ".join(unknown))
            return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    _LOGGER.info("=" * 60)
    _LOGGER.info("GHCN-Daily Backfill (standalone deterministic script)")
    _LOGGER.info("Mode: %s | stations: %d", "rebuild-only" if args.no_fetch else "fetch+upsert", len(stations))

    if not args.no_fetch:
        _LOGGER.info("Downloading .dly files from %s ...", BASE_URL)
        ok, attempted = download_all(stations, rate_limit=args.rate_limit, force=args.force)
        _LOGGER.info("Downloaded/available %d/%d files", ok, attempted)
    else:
        _LOGGER.info("Skipping download (--no-fetch)")

    _LOGGER.info("Parsing .dly files...")
    parsed = parse_all_dly(stations)
    if not parsed:
        _LOGGER.error("No .dly files parsed. Aborting without touching the DB.")
        return 1
    total_dates = sum(len(p["dates"]) for p in parsed.values())
    _LOGGER.info("Parsed %d stations, %d total date entries", len(parsed), total_dates)

    _LOGGER.info("Upserting into %s ...", OUTPUT_DB)
    stats = upsert_db(parsed, OUTPUT_DB)
    _LOGGER.info("-" * 60)
    _LOGGER.info("Rows:   %(rows_before)d -> %(rows_after)d (inserted/replaced %(rows_inserted)d)", stats)
    _LOGGER.info("Stations: %(stations_before)d -> %(stations_after)d", stats)
    _LOGGER.info("Range:  %(date_min)s -> %(date_max)s", stats)
    _LOGGER.info(
        "TMAX:   %(with_tmax)d rows | TMIN: %(with_tmin)d rows", stats
    )
    _LOGGER.info("Per-station (station, rows, min_date, max_date, tmax_rows, tmin_rows):")
    for row in per_station_stats(OUTPUT_DB):
        _LOGGER.info("  %s", row)
    _LOGGER.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())