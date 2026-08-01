#!/usr/bin/env python3
"""
GEFS Reforecast Backfill — Historical Ensemble Temperature Max

Downloads historical GEFS ensemble maximum 2m temperature forecasts from
NOAA's AWS Open Data program (both retrospective reforecast and operational
archives), extracts values at each of the 20 Kalshi stations, and stores
them in the existing NWP forecast database.

Sources (in priority order):
  1. GEFSv12 Reforecast (2000-2020) — noaa-gefs-retrospective S3 bucket
     • 0.25° global grid, tmax_2m files per member
     • 5 members daily (c00 + p01-p04) at 00Z init
  2. GEFS Operational Archive (2021-present) — noaa-gefs-pds S3 bucket
     • 0.25° surface fields per member, TMAX at 2m
     • 6 members (gec00 + gep01-gep05), 00Z init
  3. (Future) Open-Meteo ensemble API for the most recent ~5 days (already running)

Why not CDS: The Copernicus Data Store (CDS) does not host GEFS data.
CDS is an ECMWF/Copernicus service and hosts European model data (ERA5, seasonal).
GEFS is a NOAA/NCEP product and must be accessed via NOAA data stores.

Design:
  - Resumable: checkpoint file tracks per-station per-date progress
  - Rate-limited: polite delays between HTTP requests
  - DB schema compatible: stores to nwp_forecasts.db with model='gefs_ens'
  - NO AI model calls inside the download/processing loop

Usage:
  python3 scripts/gefs_reforecast_backfill.py
  python3 scripts/gefs_reforecast_backfill.py --station KATL --days 30
  python3 scripts/gefs_reforecast_backfill.py --source reforecast  # 2000-2020 only
  python3 scripts/gefs_reforecast_backfill.py --source operational  # 2021-present
  python3 scripts/gefs_reforecast_backfill.py --dry-run
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import numpy as np
import xarray as xr

# Suppress cfgrib warnings
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="cfgrib")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="cfgrib")
# ── Configuration ────────────────────────────────────────────────────────
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# 20 Kalshi stations from station_mapping.json
STATIONS = [
    ("KATL", 33.6407, -84.4277),
    ("KAUS", 30.1945, -97.6699),
    ("KBOS", 42.3656, -71.0096),
    ("KDCA", 38.8512, -77.0402),
    ("KDEN", 39.8561, -104.6737),
    ("KDFW", 32.8998, -97.0403),
    ("KHOU", 29.6454, -95.2789),
    ("KLAS", 36.0840, -115.1537),
    ("KLAX", 33.9425, -118.4081),
    ("KMDW", 41.7868, -87.7522),
    ("KMIA", 25.7959, -80.2870),
    ("KMSP", 44.8848, -93.2223),
    ("KMSY", 29.9934, -90.2580),
    ("KNYC", 40.6413, -73.7781),
    ("KOKC", 35.3931, -97.6007),
    ("KPHL", 39.8744, -75.2424),
    ("KPHX", 33.4342, -112.0116),
    ("KSAT", 29.5337, -98.4698),
    ("KSEA", 47.4502, -122.3088),
    ("KSFO", 37.6213, -122.3790),
]

# DB model name
MODEL_NAME = "gefs_ens"
VAR_TMAX = "temperature_2m_max"

# S3 GEFS Reforestation base URL
S3_REFORECAST_BASE = "https://noaa-gefs-retrospective.s3.amazonaws.com"
S3_REFORECAST_PREFIX = "GEFSv12/reforecast"

# S3 GEFS Operational base URL
S3_OPERATIONAL_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"

# Reforecast members (5: control + 4 perturbations)
REFORECAST_MEMBERS = ["c00", "p01", "p02", "p03", "p04"]

# Operational members available in 0.25° surface fields
OPERATIONAL_MEMBERS = ["gec00", "gep01", "gep02", "gep03", "gep04", "gep05"]

# HTTP config
HTTP_TIMEOUT = 60
RATE_LIMIT_DELAY = 0.5  # seconds between HTTP requests
MAX_RETRIES = 3
RETRY_DELAY = 5.0

# Checkpoint file
CHECKPOINT_FILE = "data/gefs_backfill_checkpoint.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description="GEFS Historical Ensemble Forecast Backfill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db-path", help="Database file path")
    parser.add_argument("--start-date", default="2021-01-01",
                        help="Start date YYYY-MM-DD (default: 2021-01-01)")
    parser.add_argument("--end-date", default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--station", default=None,
                        help="Single station code to process (e.g., KATL)")
    parser.add_argument("--source", choices=["reforecast", "operational", "auto"],
                        default="auto",
                        help="Data source: reforecast (2000-2020), operational (2021+), or auto")
    parser.add_argument("--days", type=int, default=None,
                        help="Number of days from start date")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without downloading")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if data exists in DB")
    parser.add_argument("--delay", type=float, default=RATE_LIMIT_DELAY,
                        help=f"Delay between HTTP requests (default: {RATE_LIMIT_DELAY}s)")
    return parser.parse_args()


def get_db_path(args):
    path = None
    if args.db_path:
        path = Path(args.db_path).absolute()
    elif os.environ.get("NWP_DB_PATH"):
        path = Path(os.environ["NWP_DB_PATH"]).absolute()
    else:
        path = (PROJECT_ROOT / NWP_DB_PATH_DEFAULT).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_db(db_path):
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS nwp_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            station TEXT NOT NULL,
            model TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            fetch_timestamp TEXT NOT NULL,
            UNIQUE(fetch_date, target_date, station, model, variable)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_lookup
        ON nwp_forecasts(target_date, station, model, variable)
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            model TEXT NOT NULL,
            stations_fetched INTEGER,
            variables_fetched INTEGER,
            errors TEXT,
            fetch_timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def load_checkpoint():
    ckpt_path = PROJECT_ROOT / CHECKPOINT_FILE
    if ckpt_path.exists():
        with open(ckpt_path) as f:
            return json.load(f)
    return {"stations": {}}


def save_checkpoint(checkpoint):
    ckpt_path = PROJECT_ROOT / CHECKPOINT_FILE
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ckpt_path, "w") as f:
        json.dump(checkpoint, f, indent=2)


def http_get(url, retries=MAX_RETRIES):
    """GET a URL with retry and rate limiting."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                delay = RETRY_DELAY * (attempt + 1)
                time.sleep(delay)
            else:
                raise


def http_get_iter(url, retries=MAX_RETRIES):
    """Stream download a URL to a temp file and return path. For large files."""
    import http.client
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
                while True:
                    chunk = resp.read(8192 * 1024)
                    if not chunk:
                        break
                    tmp.write(chunk)
                tmp.close()
                return tmp.name
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise


def s3_list_prefix(bucket_base, prefix, delimiter="/"):
    """List S3 keys under a prefix, returning common prefixes and contents."""
    url = f"{bucket_base}/?prefix={urllib.request.quote(prefix)}&delimiter={delimiter}"
    data = http_get(url)
    root = ET.fromstring(data)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

    prefixes = []
    for cp in root.findall("s3:CommonPrefixes", ns):
        prefixes.append(cp.find("s3:Prefix", ns).text)

    contents = []
    for cp in root.findall("s3:Contents", ns):
        key = cp.find("s3:Key", ns).text
        size = int(cp.find("s3:Size", ns).text)
        contents.append((key, size))

    return prefixes, contents


def find_nearest_grid_point(lats, lons, target_lat, target_lon):
    """Find nearest grid point to target lat/lon on a regular lat/lon grid."""
    # For 0.25° GEFS grid, lats go from 90 to -90
    lat_idx = int(np.argmin(np.abs(lats - target_lat)))
    lon_idx = int(np.argmin(np.abs(lons - target_lon)))
    return lat_idx, lon_idx


def process_reforecast_tmax(nc_path, station_lat, station_lon, member="c00"):
    """
    Extract daily tmax values from a GEFS Reforecast tmax_2m GRIB2 file.

    The reforecast tmax_2m file has tmax fields for each forecast step (3-hourly).
    Each step's value is the maximum temperature reached up to that valid time.
    We extract tmax at the nearest grid point for each valid date.

    Returns: list of (target_date, tmax_value_c) tuples
    """
    # Perturbation members (p01-p04) need dataType='pf' filter
    is_perturbation = member.startswith("p")
    filter_kwargs = {}
    if is_perturbation:
        filter_kwargs["filter_by_keys"] = {"dataType": "pf"}

    ds = xr.open_dataset(nc_path, engine="cfgrib", **filter_kwargs)
    results = []

    # Grid coordinates
    lats = ds.latitude.values
    lons = ds.longitude.values
    if np.any(lons > 180):
        lons = np.where(lons > 180, lons - 360, lons)

    lat_idx, lon_idx = find_nearest_grid_point(lats, lons, station_lat, station_lon)

    # Time coordinates
    valid_times = ds.valid_time.values
    tmax_values = ds.tmax.values  # (step, lat, lon)

    # Group valid times by date
    date_to_indices = defaultdict(list)
    for i, vt in enumerate(valid_times):
        vt_date = str(vt)[:10]  # YYYY-MM-DD
        date_to_indices[vt_date].append(i)

    for target_date, indices in sorted(date_to_indices.items()):
        best_idx = max(indices)
        tmax_k = float(tmax_values[best_idx, lat_idx, lon_idx])

        if not np.isnan(tmax_k) and not np.isinf(tmax_k):
            tmax_c = tmax_k - 273.15  # K to °C
            results.append((target_date, tmax_c))

    ds.close()
    return results


def process_operational_tmax(file_path, station_lat, station_lon):
    """
    Extract tmax from a single GEFS operational pgrb2sp25 file for one station.

    The operational GEFS stores TMAX in pgrb2s files filtered by heightAboveGround.
    Each file covers one forecast step, one member. TMAX is a 2D field (lat, lon).

    Returns: (target_date, tmax_c) or None
    """
    ds = xr.open_dataset(file_path, engine="cfgrib",
                          filter_by_keys={"shortName": "tmax"})

    lats = ds.latitude.values
    lons = ds.longitude.values
    if np.any(lons > 180):
        lons = np.where(lons > 180, lons - 360, lons)

    lat_idx, lon_idx = find_nearest_grid_point(lats, lons, station_lat, station_lon)

    valid_time = str(ds.valid_time.values)[:10]
    tmax_k = float(ds.tmax.values[lat_idx, lon_idx])
    ds.close()

    if np.isnan(tmax_k) or np.isinf(tmax_k):
        return None

    tmax_c = tmax_k - 273.15
    return (valid_time, tmax_c)


def check_reforecast_year_available(year):
    """Check if GEFS reforecast data exists for a given year."""
    year_prefix = f"{S3_REFORECAST_PREFIX}/{year}/"
    try:
        prefixes, _ = s3_list_prefix(S3_REFORECAST_BASE, year_prefix)
        return len(prefixes) > 0
    except Exception:
        return False


def list_reforecast_dates(year):
    """List all forecast initialization dates for a given year in the reforecast."""
    year_prefix = f"{S3_REFORECAST_PREFIX}/{year}/"
    try:
        prefixes, _ = s3_list_prefix(S3_REFORECAST_BASE, year_prefix)
        dates = []
        for p in prefixes:
            # Prefix format: GEFSv12/reforecast/{YYYY}/{YYYYMMDDHH}/
            parts = p.rstrip("/").split("/")
            if len(parts) >= 4:
                date_str = parts[-1]
                if len(date_str) >= 8 and date_str[:4].isdigit():
                    dates.append(date_str)
        return sorted(set(dates))
    except Exception:
        return []


def download_reforecast_tmax(year, month, day, hour, member):
    """
    Download a GEFS reforecast tmax_2m GRIB file for a specific date and member.

    Returns: local file path or None
    """
    date_str = f"{year:04d}{month:02d}{day:02d}{hour:02d}"
    key = f"{S3_REFORECAST_PREFIX}/{year:04d}/{date_str}/{member}/Days:1-10/tmax_2m_{date_str}_{member}.grib2"
    url = f"{S3_REFORECAST_BASE}/{key}"

    try:
        # Check file exists first with HEAD
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            file_size = int(resp.headers.get("Content-Length", 0))
            if file_size < 1000:
                return None
    except Exception:
        return None

    # Download
    return http_get_iter(url)


def list_operational_dates(start_date, end_date):
    """
    Check what dates are available in the operational GEFS archive.
    The bucket has data from ~2017 onwards.
    """
    available = []
    current = start_date
    while current <= end_date:
        date_prefix = f"gefs.{current.strftime('%Y%m%d')}/"
        try:
            prefixes, _ = s3_list_prefix(S3_OPERATIONAL_BASE, date_prefix)
            if len(prefixes) > 0:
                available.append(current)
        except Exception:
            pass
        current += timedelta(days=1)
        # Only sample every 30 days to check availability quickly
        if current > end_date:
            break
        current += timedelta(days=29)

    return available


def check_operational_date_available(check_date):
    """Check if a specific date has operational GEFS data."""
    date_str = check_date.strftime("%Y%m%d")
    date_prefix = f"gefs.{date_str}/"
    try:
        prefixes, _ = s3_list_prefix(S3_OPERATIONAL_BASE, date_prefix)
        return len(prefixes) > 0
    except Exception:
        return False


def download_operational_tmax(check_date, hour, member):
    """
    Download a GEFS operational pgrb2s file containing TMAX.

    The operational GEFS stores per-member TMAX in pgrb2sp25 files.
    Each file contains all surface fields for one forecast lead time.
    We download a single step file (f024 gives day 1 TMAX).

    Returns: list of (target_date, file_path) tuples
    """
    date_str = check_date.strftime("%Y%m%d")
    results = []

    # Download step=24h (day 1), 48h (day 2), 72h (day 3), 96h (day 4)
    # TMAX at these steps gives daily max temperatures
    day_steps = [24, 48, 72, 96, 120, 144, 168, 192, 216, 240]

    for step in day_steps:
        key = f"gefs.{date_str}/{hour:02d}/atmos/pgrb2sp25/{member}.t{hour:02d}z.pgrb2s.0p25.f{step:03d}"
        url = f"{S3_OPERATIONAL_BASE}/{key}"

        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                file_size = int(resp.headers.get("Content-Length", 0))
                if file_size < 1000:
                    continue
        except Exception:
            continue

        file_path = http_get_iter(url)
        target_date = (check_date + timedelta(days=step // 24)).isoformat()
        results.append((target_date, file_path))

    return results


def store_in_db(conn, records, fetch_date_str, fetch_timestamp):
    """
    Store extracted TMAX values in the NWP database.

    records: list of (target_date, station, member, value_c)
    """
    c = conn.cursor()
    stored = 0
    errors = []

    for target_date, station, member, value_c in records:
        # Create a unique variable name per ensemble member
        var_name = f"{VAR_TMAX}_member{member}"
        if value_c is None or (isinstance(value_c, float) and (math.isnan(value_c) or math.isinf(value_c))):
            continue

        try:
            c.execute(
                """
                INSERT OR REPLACE INTO nwp_forecasts
                (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fetch_date_str, target_date, station, MODEL_NAME,
                 var_name, float(value_c), fetch_timestamp),
            )
            stored += 1
        except Exception as e:
            errors.append(f"DB error ({station}, {member}, {target_date}): {e}")

    conn.commit()
    return stored, errors


def main():
    args = parse_args()
    db_path = get_db_path(args)

    # Resolve date range
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    elif args.days:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_date = start + timedelta(days=args.days - 1)
    else:
        end_date = datetime.now(timezone.utc).date()

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()

    if start_date > end_date:
        print(f"Error: start {start_date} after end {end_date}")
        sys.exit(1)

    # Station filter
    stations = STATIONS
    if args.station:
        stations = [(code, lat, lon) for code, lat, lon in stations if code == args.station.upper()]
        if not stations:
            print(f"Error: station {args.station} not found")
            sys.exit(1)

    # Determine data source
    source = args.source
    if source == "auto":
        # Determine based on date range
        if start_date >= date(2021, 1, 1):
            source = "operational"
            print("Source: auto → operational (2021+)")
        else:
            source = "reforecast"
            print("Source: auto → reforecast (2000-2020)")

    # Adjust date range by source
    if source == "reforecast":
        effective_start = max(start_date, date(2000, 1, 1))
        effective_end = min(end_date, date(2020, 12, 31))
        if effective_start > effective_end:
            print(f"Error: date range outside reforecast availability (2000-2020)")
            sys.exit(1)
        if effective_start != start_date or effective_end != end_date:
            print(f"Note: reforecast available 2000-2020. Adjusted range: {effective_start} to {effective_end}")
        sources = [("reforecast", effective_start, effective_end)]
    elif source == "operational":
        effective_start = max(start_date, date(2021, 1, 1))
        effective_end = min(end_date, datetime.now(timezone.utc).date())
        if effective_start > effective_end:
            print(f"Error: date range outside operational availability (2021+)")
            sys.exit(1)
        print(f"Note: checking operational bucket availability for {effective_start} to {effective_end}")
        sources = [("operational", effective_start, effective_end)]
    else:
        sources = [
            ("reforecast", max(start_date, date(2000, 1, 1)), min(end_date, date(2020, 12, 31))),
            ("operational", max(start_date, date(2021, 1, 1)), min(end_date, datetime.now(timezone.utc).date())),
        ]

    # Header
    print("=" * 72)
    print("  GEFS Historical Ensemble Forecast Backfill")
    print("=" * 72)
    print(f"  Database:  {db_path}")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Stations:   {len(stations)}")
    if args.dry_run:
        print(f"  Mode:       DRY RUN (no downloads, no DB writes)")
    print("=" * 72)
    print()

    # Initialize DB
    conn = init_db(db_path) if not args.dry_run else None

    fetch_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    # Load checkpoint
    checkpoint = load_checkpoint()
    if "stations" not in checkpoint:
        checkpoint["stations"] = {}

    total_stored = 0
    total_errors = []
    total_skipped = 0
    total_downloaded = 0
    total_failed = 0

    # Process each source
    for src_name, src_start, src_end in sources:
        if src_start > src_end:
            continue

        print(f"\n{'#' * 72}")
        print(f"  Source: {src_name.upper()} — {src_start} to {src_end}")
        print(f"{'#' * 72}")

        if source == "reforecast":
            # ── GEFS Reforecast (2000-2020) ──────────────────────────
            # Reforecast has daily 00Z forecasts, 5 members
            members = REFORECAST_MEMBERS
            current_year = src_start.year

            while current_year <= src_end.year:
                year_start = max(src_start, date(current_year, 1, 1))
                year_end = min(src_end, date(current_year, 12, 31))

                print(f"\n  Year {current_year}: {year_start} to {year_end}")

                # Check if year directory exists
                if not args.dry_run:
                    available = check_reforecast_year_available(current_year)
                    if not available:
                        print(f"  SKIP: year {current_year} not available in reforecast bucket")
                        current_year += 1
                        continue
                    print(f"  ✓ Year {current_year} available")

                # Process each day
                current_date = year_start
                while current_date <= year_end:
                    date_label = current_date.isoformat()
                    stn_progress = checkpoint["stations"].get("__summary__", {})

                    for station_code, station_lat, station_lon in stations:
                        stn_ckey = f"{station_code}"
                        if stn_ckey not in checkpoint["stations"]:
                            checkpoint["stations"][stn_ckey] = {"dates": []}

                        done_dates = set(checkpoint["stations"][stn_ckey].get("dates", []))

                        if date_label in done_dates and not args.force:
                            continue

                        if args.dry_run:
                            print(f"  [{station_code}] {date_label} ... (DRY RUN - would download members {','.join(members)})")
                            continue

                        for member in members:
                            time.sleep(args.delay)

                            # Download tmax_2m for this date + member
                            try:
                                file_path = download_reforecast_tmax(
                                    current_date.year, current_date.month,
                                    current_date.day, 0, member
                                )
                            except Exception:
                                continue

                            if file_path is None:
                                continue

                            total_downloaded += 1

                            # Process: extract tmax at station lat/lon
                            try:
                                results = process_reforecast_tmax(
                                    file_path, station_lat, station_lon, member=member
                                )
                            except Exception as e:
                                total_errors.append(f"{station_code}/{date_label}/{member}: {e}")
                                try:
                                    os.unlink(file_path)
                                except Exception:
                                    pass
                                continue

                            # Clean up file
                            try:
                                os.unlink(file_path)
                            except Exception:
                                pass

                            if not results:
                                continue

                            # Store in DB
                            records = []
                            for target_date, tmax_c in results:
                                records.append((target_date, station_code, member, tmax_c))

                            stored, errs = store_in_db(conn, records, fetch_date_str, fetch_timestamp)
                            total_stored += stored
                            total_errors.extend(errs)

                            if errs:
                                for e in errs[:3]:
                                    print(f"    Error: {e}")

                        # Mark date as done for this station
                        checkpoint["stations"][stn_ckey]["dates"].append(date_label)
                        save_checkpoint(checkpoint)

                    current_date += timedelta(days=1)

                    # Progress indicator
                    if (current_date - year_start).days % 10 == 0 and not args.dry_run:
                        print(f"  {current_date.isoformat()} ... ({total_stored} values stored)")

                current_year += 1

        elif source == "operational":
            # ── GEFS Operational (2021-present) ──────────────────────
            # Design: download each (date, member, step) file ONCE, extract all stations
            # This avoids downloading 20× per file
            members = OPERATIONAL_MEMBERS
            current_date = src_start
            day_steps = [24, 48, 72, 96, 120, 144, 168, 192, 216, 240]

            while current_date <= src_end:
                date_label = current_date.isoformat()

                if args.dry_run:
                    print(f"  {date_label} ... (DRY RUN)")
                    current_date += timedelta(days=1)
                    continue

                # Check if this date has operational data
                if not check_operational_date_available(current_date):
                    current_date += timedelta(days=1)
                    continue

                date_str = current_date.strftime("%Y%m%d")

                for member in members:
                    total_downloaded_per_member = 0
                    for step in day_steps:
                        # Determine target date from step
                        target_date_str = (current_date + timedelta(days=step // 24)).isoformat()

                        # Unique key for this init_date + member
                        member_key = f"{date_str}/00/{member}"
                        if "members" not in checkpoint:
                            checkpoint["members"] = {}
                        if member_key in checkpoint["members"] and not args.force:
                            continue

                        time.sleep(args.delay)

                        # Download the file
                        key = f"gefs.{date_str}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f{step:03d}"
                        url = f"{S3_OPERATIONAL_BASE}/{key}"

                        try:
                            req = urllib.request.Request(url, method="HEAD")
                            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                                file_size = int(resp.headers.get("Content-Length", 0))
                                if file_size < 1000:
                                    continue
                        except Exception:
                            continue

                        # Stream download
                        try:
                            file_path = http_get_iter(url)
                        except Exception:
                            continue

                        total_downloaded += 1
                        total_downloaded_per_member += 1

                        # Extract all stations from this file
                        step_records = []
                        for station_code, station_lat, station_lon in stations:
                            try:
                                result = process_operational_tmax(
                                    file_path, station_lat, station_lon
                                )
                            except Exception as e:
                                total_errors.append(f"{station_code}/{date_label}/{member}: {e}")
                                continue

                            if result is None:
                                continue

                            vt_date, tmax_c = result
                            step_records.append((vt_date, station_code, member, tmax_c))

                            # Mark station+date as done
                            stn_ckey = f"{station_code}"
                            if stn_ckey not in checkpoint["stations"]:
                                checkpoint["stations"][stn_ckey] = {"dates": []}
                            if vt_date not in checkpoint["stations"][stn_ckey]["dates"]:
                                checkpoint["stations"][stn_ckey]["dates"].append(vt_date)

                        # Clean up file
                        try:
                            os.unlink(file_path)
                        except Exception:
                            pass

                        # Store in DB
                        if step_records:
                            stored, errs = store_in_db(conn, step_records, fetch_date_str, fetch_timestamp)
                            total_stored += stored
                            total_errors.extend(errs)

                    # Mark member as processed
                    checkpoint["members"][member_key] = {"status": "complete", "values": total_downloaded_per_member}
                    save_checkpoint(checkpoint)

                current_date += timedelta(days=1)

                if (current_date - src_start).days % 3 == 0 and not args.dry_run:
                    print(f"  {current_date.isoformat()} ... ({total_stored} values stored)")

    # ── Summary ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  BACKFILL COMPLETE")
    print("=" * 72)
    print(f"  Values stored:   {total_stored}")
    print(f"  Downloads:       {total_downloaded}")
    print(f"  Skipped:         {total_skipped}")
    print(f"  Failed:          {total_failed}")
    print(f"  Errors:          {len(total_errors)}")

    if total_errors:
        print("\n  Recent errors:")
        for err in total_errors[-10:]:
            print(f"    - {err}")

    # Show DB stats
    if conn and not args.dry_run:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(DISTINCT station), COUNT(DISTINCT target_date), COUNT(*) "
            "FROM nwp_forecasts WHERE model = ? AND variable LIKE ?",
            (MODEL_NAME, f"{VAR_TMAX}_member%"),
        )
        stns, days, rows = c.fetchone()
        print(f"\n  GEFS ensemble data: {rows} rows, {stns} stations, {days} days")

        c.execute(
            "SELECT COUNT(*) FROM nwp_forecasts WHERE model = ? AND variable = ?",
            ("gefs_ens", "temperature_2m_max_membergec00"),
        )
        control_rows = c.fetchone()[0]
        print(f"  Control member (gec00): {control_rows} rows")

    if conn:
        conn.close()

    print()
    print(f"  Finished at {datetime.now(timezone.utc).isoformat()}")
    print()


if __name__ == "__main__":
    main()