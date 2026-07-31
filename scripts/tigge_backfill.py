#!/usr/bin/env python3
"""
ECMWF TIGGE Ensemble Backfill via ECDS

Downloads ECMWF 50-member ensemble reforecast data (2m temperature) from
the TIGGE dataset via the ECMWF Data Store (ECDS). Data available from
2006-10 to present.

Process:
  1. Request TIGGE data via cdsapi (one month at a time, all stations + members)
  2. Wait for server-side staging (typically 30-120s)
  3. Download GRIB file
  4. Parse with cfgrib to extract per-member 2m temperature
  5. Store in gefs-compatible format (one row per station-date-step)

ECDS credentials: ~/.cdsapirc or env vars CDSAPI_URL / CDSAPI_KEY
CDS key: e543bd…954e (same for CDS and ECDS).

NOTE: TIGGE migrated to ECDS (https://ecds.ecmwf.int) in 2026-05.
The script now uses the ECDS API with MARS format parameters.

Usage:
    python3 scripts/tigge_backfill.py
    python3 scripts/tigge_backfill.py --start 2020-01 --end 2026-06
    python3 scripts/tigge_backfill.py --stations KNYC,KLAX --dry-run

B-Mode compliant. No AI/ML.
"""

import os
import sys
import json
import time
import math
import sqlite3
import argparse
import struct
import zlib
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ECDS API URL (TIGGE migrated here in 2026-05)
_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = "e543bdb6-c396-48b4-bc1c-bebe08db954e"

# ── Paths ──
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ── Constants ──
STATIONS: List[Tuple[str, float, float]] = [
    ("KATL", 33.6407, -84.4277),
    ("KAUS", 30.1945, -97.6699),
    ("KBOS", 42.3656, -71.0096),
    ("KDCA", 38.8521, -77.0377),
    ("KDEN", 39.8617, -104.6732),
    ("KDFW", 32.8968, -97.038),
    ("KHOU", 29.6454, -95.2789),
    ("KLAS", 36.0804, -115.1523),
    ("KLAX", 33.9425, -118.4081),
    ("KMDW", 41.786, -87.7525),
    ("KMIA", 25.7959, -80.287),
    ("KMSP", 44.8805, -93.2169),
    ("KMSY", 29.9933, -90.2581),
    ("KNYC", 40.7128, -74.006),
    ("KOKC", 35.3931, -97.6007),
    ("KPHL", 39.8729, -75.2425),
    ("KPHX", 33.4484, -112.0773),
    ("KSAT", 29.4241, -98.4936),
    ("KSEA", 47.4489, -122.3093),
    ("KSFO", 37.6194, -122.3748),
]

STATION_NAMES = [s[0] for s in STATIONS]
STATION_MAP = {s[0]: (s[1], s[2]) for s in STATIONS}

DB_PATH = str(REPO_ROOT / "data" / "tigge_archive.db")
GRIB_DIR = str(REPO_ROOT / "data" / "tigge_grib")

TIGGE_ORIGINS = ["ecmf"]
TIGGE_VARIABLE = "167"  # MARS param code for 2m temperature
TIGGE_LEVEL_TYPE = "sfc"
TIGGE_TYPE = "pf"  # perturbed forecast (members 1-50)
TIGGE_STEPS = [0, 3, 6, 9, 12, 15, 18, 21, 24]

DEFAULT_START = "2020-01"
DEFAULT_END = "2026-06"
REQUEST_DELAY_S = 10.0
TIGGE_N_MEMBERS = 50

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tigge_archive (
    target_date TEXT NOT NULL,
    station TEXT NOT NULL,
    step INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'tigge_ecmwf',
    ensemble_mean REAL,
    ensemble_min REAL,
    ensemble_max REAL,
    member_values BLOB,
    n_members INTEGER DEFAULT 0,
    PRIMARY KEY (target_date, station, step, source)
);
CREATE TABLE IF NOT EXISTS tigge_checkpoint (
    year_month TEXT NOT NULL,
    station TEXT NOT NULL,
    last_target_date TEXT,
    status TEXT DEFAULT 'pending',
    grib_file TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (year_month, station)
);
"""


# ═══════════════════════════════════════════════════════════════════════════
# GRIB Parsing
# ═══════════════════════════════════════════════════════════════════════════

def grib_to_member_temps(
    grib_path: str, station_lat: float, station_lon: float, step_hours: int = 24
) -> Optional[Dict[int, List[float]]]:
    """
    Parse a GRIB file and extract 2m temperature for each ensemble member
    at the given station lat/lon, grouped by lead step (hour).
    Returns {step_h: [member_temps_c]} or None on failure.
    """
    try:
        import cfgrib
        import xarray as xr
        import numpy as np
    except ImportError:
        return None

    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"errors": "ignore"})
    except Exception as e:
        print(f"  [GRIB] Failed to open: {e}")
        return None

    try:
        if "t2m" not in ds.data_vars:
            print(f"  [GRIB] No t2m variable in dataset. Variables: {list(ds.data_vars)}")
            ds.close()
            return None

        t2m = ds["t2m"]
        dims = t2m.dims
        print(f"  [GRIB] t2m dims: {dims}, shape: {t2m.shape}")

        has_latlon = "latitude" in dims and "longitude" in dims
        has_station = "station" in dims or "station_id" in dims

        if has_latlon:
            lats = ds.coords["latitude"].values
            lons = ds.coords["longitude"].values

            if lats.ndim == 1 and lons.ndim == 1:
                lat_idx = int(np.argmin(np.abs(lats - station_lat)))
                lon_idx = int(np.argmin(np.abs(lons - station_lon)))
                # Handle longitude convention
                station_lon_adj = station_lon
                if np.any(lons > 180) and station_lon < 0:
                    station_lon_adj = station_lon + 360
                lon_idx = int(np.argmin(np.abs(lons - station_lon_adj)))
                isel = {"latitude": lat_idx, "longitude": lon_idx}
            elif lats.ndim == 2:
                dist = np.sqrt(
                    (lats - station_lat) ** 2 + (lons - station_lon_adj) ** 2
                )
                lat_idx, lon_idx = np.unravel_index(np.argmin(dist), lats.shape)
                isel = {"latitude": lat_idx, "longitude": lon_idx}
            else:
                print(f"  [GRIB] Unexpected lat/lon dims: {lats.ndim}")
                ds.close()
                return None
        elif has_station:
            # Station dimension — find nearest
            station_name = STATION_MAP.get(station_lat, "")
            # This is a simplified approach; actual implementation may vary
            ds.close()
            return None
        else:
            print(f"  [GRIB] No lat/lon or station dims found")
            ds.close()
            return None

        # Extract data at this grid point
        sel = t2m.isel(**isel)

        # Determine step dimension
        steps_found = None
        if "step" in sel.dims:
            steps_found = sel["step"].values
        elif "time" in sel.dims:
            steps_found = sel["time"].values
        elif "forecast_period" in sel.dims:
            steps_found = sel["forecast_period"].values

        if steps_found is None:
            # Single step — try to get the time
            try:
                steps_found = [sel["time"].values.item()]
            except Exception:
                print(f"  [GRIB] Could not determine step dimension")
                ds.close()
                return None

        numbers = None
        if "number" in sel.dims:
            numbers = sel["number"].values
        elif "ensemble_member" in sel.dims:
            numbers = sel["ensemble_member"].values

        if numbers is None:
            print(f"  [GRIB] No ensemble member dimension found")
            ds.close()
            return None

        # Extract member temperatures per step
        results = {}
        for step_idx, step_val in enumerate(steps_found):
            # Convert step to hours
            step_h = 0
            try:
                if hasattr(step_val, "item"):
                    step_h = int(step_val.item())
                elif hasattr(step_val, "total_seconds"):
                    step_h = int(step_val.total_seconds() / 3600)
                else:
                    step_h = int(step_val)
            except (ValueError, TypeError):
                continue

            member_temps = []
            for mem_idx in range(len(numbers)):
                try:
                    val = sel.isel(step=step_idx, number=mem_idx).values.item()
                    # Convert from Kelvin to Celsius
                    temp_c = val - 273.15
                    member_temps.append(round(temp_c, 2))
                except (ValueError, TypeError, IndexError):
                    continue

            if member_temps:
                results[step_h] = member_temps

        ds.close()
        return results

    except Exception as e:
        print(f"  [GRIB] Parse error: {e}")
        try:
            ds.close()
        except Exception:
            pass
        return None


def encode_member_temps(member_temps_c: List[float], mean_c: float) -> bytes:
    """
    Encode member temperatures as a compact binary blob.
    Stores signed 16-bit integer differences from mean (scaled by 10).
    """
    raw = bytearray()
    for t in member_temps_c:
        diff = int(round((t - mean_c) * 10))
        raw.extend(struct.pack("<h", diff))
    # Compress
    return zlib.compress(bytes(raw), 1)


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=OFF")
    cur.execute("PRAGMA cache_size=-64000")
    for stmt in SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)
    conn.commit()
    return conn


def store_ensemble(
    conn: sqlite3.Connection,
    station: str,
    date_str: str,
    step_h: int,
    member_temps: List[float],
) -> int:
    """Store one station-date-step row with compact member blob."""
    if not member_temps:
        return 0
    mean_c = sum(member_temps) / len(member_temps)
    min_c = min(member_temps)
    max_c = max(member_temps)
    blob = encode_member_temps(member_temps, mean_c)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO tigge_archive
            (target_date, station, step, source, ensemble_mean, ensemble_min,
             ensemble_max, member_values, n_members)
        VALUES (?, ?, ?, 'tigge_ecmwf', ?, ?, ?, ?, ?)
        """,
        (date_str, station, step_h, round(mean_c, 2), round(min_c, 2),
         round(max_c, 2), blob, len(member_temps)),
    )
    conn.commit()
    return 1


# ═══════════════════════════════════════════════════════════════════════════
# ECDS Request
# ═══════════════════════════════════════════════════════════════════════════

def parse_tigge_grib(grib_path: str, station_lat: float, station_lon: float) -> Optional[Dict[int, List[float]]]:
    """
    Parse TIGGE GRIB and return {step_h: [member_temps_c]}.
    Tries cfgrib first, falls back to pygrib.
    """
    # Try cfgrib approach
    try:
        result = grib_to_member_temps(grib_path, station_lat, station_lon)
        if result:
            return result
    except Exception as e:
        print(f"  [PARSE] cfgrib error: {e}")

    # Fallback: try pygrib
    try:
        import pygrib
        grbs = pygrib.open(grib_path)
        results = {}
        for grb in grbs:
            try:
                name = grb.name
                if "2m temperature" not in name and "2 metre temperature" not in name:
                    continue
                step_h = 0
                try:
                    step_val = grb.step
                    step_h = int(step_val)
                except (ValueError, TypeError):
                    continue
                if step_h not in TIGGE_STEPS:
                    continue
                lats, lons = grb.latlons()
                import numpy as np
                if lats.ndim == 2:
                    dist = np.sqrt(
                        (lats - station_lat) ** 2 + (lons - station_lon) ** 2
                    )
                    lat_idx, lon_idx = np.unravel_index(np.argmin(dist), lats.shape)
                    temp_k = grb.values[lat_idx, lon_idx]
                else:
                    lat_idx = int(np.argmin(np.abs(lats - station_lat)))
                    lon_idx = int(np.argmin(np.abs(lons - station_lon)))
                    temp_k = grb.values[lat_idx, lon_idx]
                temp_c = float(temp_k) - 273.15
                member = getattr(grb, "perturbationNumber", 0)
                if step_h not in results:
                    results[step_h] = []
                results[step_h].append(temp_c)
            except Exception:
                continue
        grbs.close()
        return results if results else None
    except ImportError:
        print("  [PARSE] pygrib not available either")
        return None
    except Exception as e:
        print(f"  [PARSE] pygrib error: {e}")
        return None


def request_tigge_month(year: int, month: int, grib_out: str) -> bool:
    """
    Request one month of TIGGE data from ECDS and save to GRIB file.
    Returns True if download succeeded.
    """
    try:
        import cdsapi
    except ImportError:
        print("  [ERROR] cdsapi not installed. Install with: pip install cdsapi")
        return False

    c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY)
    year_str = str(year)
    month_str = str(month).zfill(2)

    print(f"  Requesting TIGGE {year_str}-{month_str} (50 members)...")

    # Generate day list for the month
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    day_list = [str(d).zfill(2) for d in range(1, days_in_month + 1)]

    # Use MARS format for ECDS (TIGGE migrated to ECDS in 2026-05)
    request = {
        "class": "ti",
        "expver": "prod",
        "dataset": "tigge",
        "date": f"{year_str}-{month_str}-01/{year_str}-{month_str}-{days_in_month:02d}",
        "grid": "0.5/0.5",
        "levtype": "sfc",
        "origin": "ecmf",
        "param": TIGGE_VARIABLE,
        "step": "/".join(str(s) for s in TIGGE_STEPS),
        "stream": "enfo",
        "time": "00:00:00",
        "type": "pf",
        "number": "1/to/50",
    }

    print(f"  Request: {request}")

    try:
        result = c.retrieve("tigge-forecasts", request, grib_out)
        print(f"  Response: {result}")
        return True
    except Exception as e:
        print(f"  [ECDS ERROR] {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Backfill Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_backfill(args):
    """Run the full TIGGE backfill."""
    os.makedirs(GRIB_DIR, exist_ok=True)
    conn = init_db(args.db_path)

    start_parts = args.start.split("-")
    end_parts = args.end.split("-")
    start_ym = int(start_parts[0]) * 12 + int(start_parts[1])
    end_ym = int(end_parts[0]) * 12 + int(end_parts[1])

    year_months = []
    for ym in range(start_ym, end_ym + 1):
        y = ym // 12
        m = ym % 12
        if m == 0:
            y -= 1
            m = 12
        year_months.append((y, m))

    active_stations = args.stations or STATION_NAMES

    # Load already-done checkpoints
    cur = conn.cursor()
    cur.execute("SELECT year_month, station, status FROM tigge_checkpoint WHERE status='done'")
    done_set = set()
    for row in cur.fetchall():
        done_set.add((row["year_month"], row["station"]))

    total_downloads = 0
    total_gribs = 0
    total_rows = 0
    t_start = time.time()

    print("=" * 72)
    print("  TIGGE ECMWF Ensemble Backfill via ECDS")
    print("  Dataset:   tigge-forecasts (ECMWF, 50 members)")
    print(f"  Stations:  {len(active_stations)}")
    print(f"  Months:    {year_months[0][0]}-{year_months[0][1]:02d} to {year_months[-1][0]}-{year_months[-1][1]:02d}")
    print(f"  Steps:     {', '.join(str(s) for s in TIGGE_STEPS)}h")
    print(f"  GRIB dir:  {GRIB_DIR}")
    print(f"  DB:        {args.db_path}")
    print("=" * 72)

    for ym_idx, (y, m) in enumerate(year_months):
        ym_key = f"{y}-{m:02d}"
        ym_grib = os.path.join(GRIB_DIR, f"tigge_ecmwf_{ym_key}.grib")

        # Check which stations need processing this month
        pending_stations = [
            s for s in active_stations
            if (ym_key, s) not in done_set
        ]

        if not pending_stations:
            print(f"  [{ym_idx + 1}/{len(year_months)}] {ym_key}: all stations done, skipping")
            continue

        print(f"\n  [{ym_idx + 1}/{len(year_months)}] {ym_key} — {len(pending_stations)} stations pending")

        for station_name in pending_stations:
            lat, lon = STATION_MAP[station_name]
            success = False

            # Download GRIB if needed
            if not os.path.exists(ym_grib) or os.path.getsize(ym_grib) < 1000:
                print(f"    Downloading {ym_key}...")
                success = request_tigge_month(y, m, ym_grib)
                total_downloads += 1
                if success:
                    total_gribs += 1
                # Cool down
                delay = args.request_delay
                print(f"  Cooling down {delay}s...")
                time.sleep(delay)
            else:
                grib_size = os.path.getsize(ym_grib)
                print(f"  GRIB already exists ({grib_size / 1024:.1f} KB)")
                success = True

            if not success:
                print(f"  Skipping {station_name} ({ym_key} — download failed)")
                continue

            # Parse GRIB for this station
            grib_size = os.path.getsize(ym_grib)
            print(f"    Parsing GRIB ({grib_size / 1024:.1f} KB) for {station_name}...")

            station_results = parse_tigge_grib(ym_grib, lat, lon)
            if station_results is None:
                print(f"    {station_name}: no data extracted")
                continue

            # Store per-step results
            rows_added = 0
            days_in_month = 31  # TIGGE provides daily data

            for step_h in sorted(station_results.keys()):
                member_temps = station_results[step_h]
                if not member_temps:
                    continue

                # Store for each day of the month
                for day in range(1, days_in_month + 1):
                    try:
                        dt = date(y, m, day)
                        target_date = dt.isoformat()
                        store_ensemble(conn, station_name, target_date, step_h, member_temps)
                        rows_added += 1
                    except ValueError:
                        continue

            total_rows += rows_added
            print(f"    {station_name}: {rows_added} rows added")

            # Mark checkpoint
            cur.execute(
                """
                INSERT OR REPLACE INTO tigge_checkpoint
                    (year_month, station, last_target_date, status, grib_file, updated_at)
                VALUES (?, ?, ?, 'done', ?, datetime('now'))
                """,
                (ym_key, station_name, f"{y}-{m:02d}-{days_in_month}", ym_grib),
            )
            conn.commit()

        # Progress
        elapsed = time.time() - t_start
        rate = (ym_idx + 1) / (elapsed / 60) if elapsed > 0 else 0
        remaining = (len(year_months) - ym_idx - 1) / rate if rate > 0 else 0
        print(f"  Progress: {ym_idx + 1}/{len(year_months)} months ({rate:.1f}/min, ~{remaining:.0f}s remaining)")

    # Summary
    db_rows = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    db_dates = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive").fetchone()[0]
    db_stations = conn.execute("SELECT COUNT(DISTINCT station) FROM tigge_archive").fetchone()[0]
    elapsed = time.time() - t_start

    print("\n" + "=" * 72)
    print("  BACKFILL COMPLETE")
    print(f"  GRIB files: {total_gribs}")
    print(f"  Total rows: {total_rows}")
    print(f"  Time:       {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"  DB: {db_rows} rows, {db_dates} dates, {db_stations} stations")
    print("=" * 72)

    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="TIGGE ECMWF ensemble backfill via ECDS")
    parser.add_argument("--start", default=DEFAULT_START, help=f"Start YYYY-MM (default: {DEFAULT_START})")
    parser.add_argument("--end", default=DEFAULT_END, help=f"End YYYY-MM (default: {DEFAULT_END})")
    parser.add_argument("--db-path", default=DB_PATH, help="SQLite DB path")
    parser.add_argument("--stations", type=str, help="Comma-separated stations (default: all 20)")
    parser.add_argument("--grib-dir", default=GRIB_DIR, help="GRIB file cache directory")
    parser.add_argument("--request-delay", type=float, default=REQUEST_DELAY_S, help="Delay between ECDS requests (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args()

    # Resolve stations
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
        for s in stations:
            if s not in STATION_MAP:
                print(f"Unknown station: {s}")
                sys.exit(1)
        args.stations = stations

    # Verify ECDS connection
    try:
        import cdsapi
        c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY)
        print(f"ECDS: connected to {_ECDS_URL}")
    except Exception as e:
        print(f"ECDS: {e}")
        print("Make sure ~/.cdsapirc is configured with ECDS URL and key")
        sys.exit(1)

    # Verify cfgrib
    try:
        import cfgrib
        v = getattr(cfgrib, "__version__", "?")
        print(f"cfgrib: available (v{v})")
    except ImportError:
        print("WARNING: cfgrib not installed. GRIB parsing will be limited.")

    start_parts = args.start.split("-")
    end_parts = args.end.split("-")
    y1, m1 = int(start_parts[0]), int(start_parts[1])
    y2, m2 = int(end_parts[0]), int(end_parts[1])
    months = ((y2 - y1) * 12 + m2 - m1 + 1)

    stations = args.stations or STATION_NAMES
    est_grib = months * 1  # 1 grib per month (all stations combined)
    est_size = est_grib * 500  # ~500 MB per grib

    print(f"Plan: {months} months × {len(stations)} stations")
    print(f"  ECDS requests: {est_grib}")
    print(f"  Est. download: ~{est_size} MB")
    print(f"  Est. DB rows: ~{months * 31 * len(stations) * len(TIGGE_STEPS)}")
    print(f"  GRIB cache: {args.grib_dir}")

    if args.dry_run:
        print("\nDRY RUN: exiting")
        sys.exit(0)

    run_backfill(args)


if __name__ == "__main__":
    main()