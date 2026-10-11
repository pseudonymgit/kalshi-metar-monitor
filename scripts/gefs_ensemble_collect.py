#!/usr/bin/env python3
"""
gefs_ensemble_collect.py — GEFS 31-member ensemble temperature collector.

THE MISSING UPSTREAM PRODUCER. The daily "WE-GEFS-daily-backfill" cron used to
only *transform* already-fetched `gefs_ens` rows (via populate_gefs_archive.py)
and never fetched anything from upstream, so GEFS went stale whenever a manual
backfill stopped running. This script closes that gap with a deterministic,
dependency-free daily collection.

Route: Open-Meteo ensemble API (https://ensemble-api.open-meteo.com/v1/ensemble)
  - Model name is `ncep_gefs025` (NOT `ncep_gefs` — that returns HTTP 400).
  - 31 members: control `temperature_2m` + `temperature_2m_member01..30`.
  - We fetch the 7-day forward forecast and store the per-member daily MAX.
  - Retrospective (start_date/end_date) only reaches ~4 days back, so this is a
    forward collector; the deep history is owned by gefs_reforecast_backfill.py.

Writes flat rows to data/nwp_forecasts.db (model='gefs_ens'), the same shape
consumed by populate_gefs_archive.py and every downstream GEFS signal.

  variable = temperature_2m_max_membergec00   (control)
             temperature_2m_max_membergepNN    (NN = 01..30)

NO AI/ML calls. NO third-party dependencies (stdlib urllib only).

Usage:
    python3 scripts/gefs_ensemble_collect.py
    python3 scripts/gefs_ensemble_collect.py --forecast-days 7 --stations KATL,KNYC
    python3 scripts/gefs_ensemble_collect.py --dry-run
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "nwp_forecasts.db"

# Sprint B / I4: run-provenance seam (run_init_utc + fetched_at).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.nwp_provenance import (  # noqa: E402
    ensure_provenance_columns,
    run_init_utc_from_cycle,
)

ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODEL_NAME = "gefs_ens"
OPEN_METEO_MODEL = "ncep_gefs025"
CONTROL_VAR = "temperature_2m"

# 20 Kalshi cities — matches nwp_collect.py / station registry
CITIES = [
    ("KATL", 33.64, -84.43), ("KAUS", 30.20, -97.67), ("KBOS", 42.37, -71.01),
    ("KDCA", 38.85, -77.04), ("KDEN", 39.86, -104.67), ("KDFW", 32.90, -97.04),
    ("KHOU", 29.98, -95.36), ("KLAS", 36.08, -115.16), ("KLAX", 33.94, -118.41),
    ("KMDW", 41.79, -87.75), ("KMIA", 25.80, -80.29), ("KMSP", 44.88, -93.22),
    ("KMSY", 29.99, -90.26), ("KNYC", 40.71, -74.01), ("KOKC", 35.39, -97.60),
    ("KPHL", 39.87, -75.24), ("KPHX", 33.43, -112.01), ("KSAT", 29.53, -98.47),
    ("KSEA", 47.45, -122.31), ("KSFO", 37.62, -122.38),
]

# Map Open-Meteo ensemble member keys -> existing DB variable suffix.
# Open-Meteo: 'temperature_2m' (control), 'temperature_2m_member01..30'
# DB convention: 'gec00' (control), 'gep01..gep30'
def member_suffix(open_meteo_key: str) -> str:
    if open_meteo_key == CONTROL_VAR:
        return "gec00"
    tail = open_meteo_key.rsplit("_member", 1)[-1]  # e.g. '01'
    return f"gep{tail}"


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nwp_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            station TEXT NOT NULL,
            model TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            fetch_timestamp TEXT NOT NULL,
            run_init_utc TEXT,
            fetched_at TEXT,
            UNIQUE(fetch_date, target_date, station, model, variable)
        )
    """)
    # Sprint B / I4: migrate pre-existing tables in place (no row rewrite).
    ensure_provenance_columns(conn, "nwp_forecasts")
    conn.commit()


def fetch_ensemble(lat, lon, forecast_days, start_date=None, end_date=None, timeout=45):
    params = {
        "latitude": lat, "longitude": lon,
        "models": OPEN_METEO_MODEL,
        "hourly": CONTROL_VAR,
        "ensemble_members": "all",
        "timezone": "UTC",
    }
    if start_date and end_date:
        # Retrospective window (~4 days back on this endpoint).
        params["start_date"] = start_date
        params["end_date"] = end_date
    else:
        params["forecast_days"] = forecast_days
    url = f"{ENSEMBLE_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {e.reason} {body}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def parse_member_daily_max(data):
    """Return {target_date: {member_suffix: max_temp_c}} from an API response."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return {}
    member_keys = [k for k in hourly if k == CONTROL_VAR or k.startswith(CONTROL_VAR + "_member")]

    out = defaultdict(dict)
    for key in member_keys:
        vals = hourly.get(key) or []
        suffix = member_suffix(key)
        by_day = defaultdict(list)
        for ts, v in zip(times, vals):
            if v is not None:
                by_day[ts[:10]].append(float(v))
        for day, arr in by_day.items():
            if arr:
                out[day][suffix] = max(arr)
    return out


def main():
    ap = argparse.ArgumentParser(description="GEFS 31-member ensemble collector")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--forecast-days", type=int, default=7)
    ap.add_argument("--start-date", help="retrospective start (YYYY-MM-DD)")
    ap.add_argument("--end-date", help="retrospective end (YYYY-MM-DD)")
    ap.add_argument("--stations", help="comma-separated station codes (default: all 20)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if bool(args.start_date) != bool(args.end_date):
        print("ERROR: --start-date and --end-date must be given together")
        return 2

    db_path = Path(args.db_path)
    cities = CITIES
    if args.stations:
        wanted = {s.strip().upper() for s in args.stations.split(",")}
        cities = [c for c in CITIES if c[0] in wanted]
        if not cities:
            print(f"ERROR: no known stations in {args.stations}")
            return 2

    now = datetime.now(timezone.utc)
    fetch_date = now.strftime("%Y-%m-%d")
    fetch_ts = now.isoformat()
    run_init = run_init_utc_from_cycle(now)  # GEFS cycle the data came from

    print(f"GEFS Ensemble Collection — {fetch_ts}")
    print(f"Model: {MODEL_NAME} via {OPEN_METEO_MODEL} | stations: {len(cities)} | days: {args.forecast_days}")
    print(f"Database: {db_path}")
    if args.dry_run:
        print("Mode: DRY RUN (no DB writes)")

    conn = None
    if not args.dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        init_db(conn)

    stations_ok = 0
    stations_failed = 0
    total_rows = 0
    errors = []
    day_members = defaultdict(int)

    for station, lat, lon in cities:
        data = fetch_ensemble(lat, lon, args.forecast_days,
                              start_date=args.start_date, end_date=args.end_date)
        if "error" in data:
            print(f"  {station}: ERROR {data['error']}")
            errors.append(f"{station}: {data['error']}")
            stations_failed += 1
            time.sleep(1)
            continue

        per_day = parse_member_daily_max(data)
        if not per_day:
            print(f"  {station}: no data in response")
            errors.append(f"{station}: empty response")
            stations_failed += 1
            continue

        rows = 0
        for target_date, members in sorted(per_day.items()):
            for suffix, val in members.items():
                var = f"temperature_2m_max_member{suffix}"
                if conn is not None:
                    conn.execute(
                        """INSERT OR REPLACE INTO nwp_forecasts
                           (fetch_date, target_date, station, model, variable, value, fetch_timestamp,
                            run_init_utc, fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (fetch_date, target_date, station, MODEL_NAME, var, float(val), fetch_ts,
                         run_init, fetch_ts),
                    )
                rows += 1
                day_members[target_date] += 1
        if conn is not None:
            conn.commit()
        stations_ok += 1
        total_rows += rows
        print(f"  {station}: OK ({rows} rows, {len(per_day)} days)")
        time.sleep(0.4)

    if conn is not None:
        conn.close()

    print(f"\n=== COLLECTION COMPLETE ===")
    print(f"Stations OK: {stations_ok} | failed: {stations_failed} | rows stored: {total_rows}")
    if errors:
        print(f"Errors ({len(errors)}): {errors[:5]}")
    if day_members:
        ds = sorted(day_members)
        print(f"Target dates: {ds[0]} .. {ds[-1]}")

    # A healthy run needs every station to succeed; partial runs are surfaced.
    return 1 if stations_failed else 0


if __name__ == "__main__":
    sys.exit(main())
