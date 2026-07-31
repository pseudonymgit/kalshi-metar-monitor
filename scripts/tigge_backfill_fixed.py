#!/usr/bin/env python3
"""
Fixed TIGGE ECMWF 50-member ensemble backfill via ECDS.
Fixes: 1-day requests (not 1-month), eccodes-based GRIB parsing, proper station grid lookup.

Usage:
    python3 scripts/tigge_backfill_fixed.py [--start 2020-01-01] [--end 2026-06-30]
    python3 scripts/tigge_backfill_fixed.py --stations KNYC,KLAX
"""

import os, sys, json, time, sqlite3, argparse, struct, zlib
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = "e543bdb6-c396-48b4-bc1c-bebe08db954e"

STATIONS: List[Tuple[str, float, float]] = [
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
STATION_NAMES = [s[0] for s in STATIONS]

DB_PATH = str(REPO_ROOT / "data" / "tigge_archive.db")
TIGGE_STEPS = [0, 3, 6, 9, 12, 15, 18, 21, 24]
REQUEST_DELAY_S = 10.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tigge_archive (
    target_date TEXT NOT NULL, station TEXT NOT NULL, step INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'tigge_ecmwf',
    ensemble_mean REAL, ensemble_min REAL, ensemble_max REAL,
    member_values BLOB, n_members INTEGER DEFAULT 0,
    PRIMARY KEY (target_date, station, step, source)
);
CREATE TABLE IF NOT EXISTS tigge_checkpoint (
    target_date TEXT NOT NULL, step INTEGER NOT NULL,
    station TEXT, status TEXT DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target_date, step, station)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
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


def encode_member_temps(member_temps_c: List[float], mean_c: float) -> bytes:
    raw = bytearray()
    for t in member_temps_c:
        diff = int(round((t - mean_c) * 10))
        raw.extend(struct.pack("<h", diff))
    return zlib.compress(bytes(raw), 1)


def store_ensemble(conn, station, date_str, step_h, member_temps):
    if not member_temps:
        return 0
    mean_c = sum(member_temps) / len(member_temps)
    min_c = min(member_temps)
    max_c = max(member_temps)
    blob = encode_member_temps(member_temps, mean_c)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO tigge_archive
        (target_date, station, step, source, ensemble_mean, ensemble_min,
         ensemble_max, member_values, n_members)
        VALUES (?, ?, ?, 'tigge_ecmwf', ?, ?, ?, ?, ?)
    """, (date_str, station, step_h, round(mean_c, 2), round(min_c, 2),
          round(max_c, 2), blob, len(member_temps)))
    cur.execute("""
        INSERT OR REPLACE INTO tigge_checkpoint
        (target_date, step, station, status, updated_at)
        VALUES (?, ?, ?, 'done', datetime('now'))
    """, (date_str, step_h, station))
    conn.commit()
    return 1


def parse_grib_for_station(grib_path: str, station_lat: float, station_lon: float,
                            steps_wanted: List[int]) -> Dict[int, List[float]]:
    """
    Parse a GRIB file with eccodes and extract 2m temperature for each member.
    Returns {step_h: [member_temps_C]} for the given station.
    """
    import eccodes
    if not os.path.exists(grib_path) or os.path.getsize(grib_path) < 100:
        return {}

    f = open(grib_path, 'rb')
    first = eccodes.codes_grib_new_from_file(f)
    if first is None:
        f.close()
        return {}
    
    try:
        ni = eccodes.codes_get_long(first, 'Ni')
        nj = eccodes.codes_get_long(first, 'Nj')
        lats = eccodes.codes_get_array(first, 'latitudes')
        lons = eccodes.codes_get_array(first, 'longitudes')
        
        # Handle lon convention (0-360)
        adj_lon = station_lon
        if lons.max() > 180 and station_lon < 0:
            adj_lon = station_lon + 360
        
        # Reshape to 2D and find nearest grid point
        lats_2d = lats.reshape(nj, ni)
        lons_2d = lons.reshape(nj, ni)
        dist = np.sqrt((lats_2d - station_lat)**2 + (lons_2d - adj_lon)**2)
        lat_idx, lon_idx = np.unravel_index(np.argmin(dist), lats_2d.shape)
        actual_lat = lats_2d[lat_idx, lon_idx]
        actual_lon = lons_2d[lat_idx, lon_idx]
    except Exception as e:
        eccodes.codes_release(first)
        f.close()
        return {}
    
    eccodes.codes_release(first)
    f.close()

    # Re-open and parse all messages
    f = open(grib_path, 'rb')
    step_temps = {}  # step_h -> list of temps

    while True:
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None:
            break
        try:
            name = eccodes.codes_get_string(gid, 'name')
            if '2 metre temperature' not in name and '2m temperature' not in name:
                eccodes.codes_release(gid)
                continue
            
            step = eccodes.codes_get_long(gid, 'step')
            if step not in steps_wanted:
                eccodes.codes_release(gid)
                continue
            
            values = eccodes.codes_get_values(gid)
            vals_2d = values.reshape(nj, ni)
            temp_k = float(vals_2d[lat_idx, lon_idx])
            temp_c = round(temp_k - 273.15, 2)
            
            if step not in step_temps:
                step_temps[step] = []
            step_temps[step].append(temp_c)
        except Exception:
            pass
        
        eccodes.codes_release(gid)

    f.close()
    return step_temps


def request_tigge_day(date_str: str, steps: List[int], grib_out: str) -> bool:
    """Request ONE day of TIGGE data from ECDS."""
    import cdsapi
    c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY)
    request = {
        'class': 'ti', 'expver': 'prod', 'dataset': 'tigge-forecasts',
        'date': date_str, 'grid': '0.5/0.5', 'levtype': 'sfc',
        'origin': 'ecmf', 'param': '167',
        'step': '/'.join(str(s) for s in steps),
        'time': '00:00:00', 'type': 'pf', 'number': '1/to/50',
    }
    try:
        result = c.retrieve('tigge-forecasts', request, grib_out)
        return os.path.exists(grib_out) and os.path.getsize(grib_out) > 1000
    except Exception as e:
        print(f"  [ECDS ERROR] {e}")
        return False


def request_openmeteo_day(date_str: str, station_lat: float, station_lon: float,
                           steps: List[int]) -> Dict[int, List[float]]:
    """Fetch ECMWF ensemble data from Open-Meteo for a single day."""
    import requests
    # Open-Meteo uses hourly data, steps are 0-24h
    url = (f"https://ensemble-api.open-meteo.com/v1/ensemble?"
           f"latitude={station_lat}&longitude={station_lon}"
           f"&hourly=temperature_2m&models=ecmwf_ifs025"
           f"&start_date={date_str}&end_date={date_str}")
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return {}
        d = r.json()
        if 'hourly' not in d:
            return {}
        h = d['hourly']
        times = h['time']
        # Extract member columns
        mem_cols = [k for k in h.keys() if k.startswith('temperature_2m_member')]
        if not mem_cols:
            return {}
        # Group by step (hour of day)
        result = {}
        for step_h in steps:
            if step_h >= len(times):
                continue
            temps = [h[col][step_h] for col in mem_cols if step_h < len(h[col])]
            temps = [t for t in temps if t is not None]
            if temps:
                result[step_h] = [round(t, 2) for t in temps]
        return result
    except Exception as e:
        print(f"  [Open-Meteo ERROR] {e}")
        return {}


def run_backfill(args):
    os.makedirs(args.grib_dir, exist_ok=True)
    conn = init_db(args.db_path)
    
    # Parse date range
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").date()
    total_days = (end_dt - start_dt).days + 1
    
    active_stations = args.stations or STATION_NAMES
    
    # Load checkpoints
    cur = conn.cursor()
    cur.execute("SELECT target_date, step, station FROM tigge_checkpoint WHERE status='done'")
    done_set = set()
    for row in cur.fetchall():
        done_set.add((row["target_date"], row["step"], row["station"]))
    
    steps = args.steps if args.steps else TIGGE_STEPS
    
    print(f"ECMWF TIGGE Backfill (Fixed)")
    print(f"  Range: {args.start} to {args.end} ({total_days} days)")
    print(f"  Stations: {len(active_stations)}")
    print(f"  Steps: {steps}")
    print(f"  Source: {args.source}")
    
    t_start = time.time()
    total_rows = 0
    total_requests = 0
    success_count = 0
    
    for day_offset in range(total_days):
        current_dt = start_dt + timedelta(days=day_offset)
        date_str = current_dt.isoformat()
        
        if day_offset % 10 == 0:
            elapsed = time.time() - t_start
            rate = day_offset / (elapsed / 60) if elapsed > 0 else 0
            remain = (total_days - day_offset) / rate if rate > 0 else 0
            print(f"  [{day_offset+1}/{total_days}] {date_str} ({rate:.1f} days/min, ~{remain:.0f}min remain)")
        
        for station_name in active_stations:
            for step_h in steps:
                ck = (date_str, step_h, station_name)
                if ck in done_set:
                    continue
                
                lat, lon = STATION_MAP[station_name]
                
                if args.source == "openmeteo":
                    step_temps = request_openmeteo_day(date_str, lat, lon, [step_h])
                else:
                    grib_file = os.path.join(args.grib_dir, f"tigge_{date_str}_{step_h}h.grib")
                    if not os.path.exists(grib_file) or os.path.getsize(grib_file) < 1000:
                        if not request_tigge_day(date_str, [step_h], grib_file):
                            continue
                        total_requests += 1
                        time.sleep(args.request_delay)
                    
                    step_temps = parse_grib_for_station(grib_file, lat, lon, [step_h])
                
                if step_h in step_temps and step_temps[step_h]:
                    store_ensemble(conn, station_name, date_str, step_h, step_temps[step_h])
                    total_rows += 1
                    success_count += 1
                else:
                    print(f"  {date_str} {station_name} step={step_h}: no data")
    
    elapsed = time.time() - t_start
    db_rows = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    db_dates = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive").fetchone()[0]
    db_stations = conn.execute("SELECT COUNT(DISTINCT station) FROM tigge_archive").fetchone()[0]
    
    print(f"\nBACKFILL COMPLETE")
    print(f"  Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  Requests: {total_requests}, Success: {success_count}")
    print(f"  DB: {db_rows} rows, {db_dates} dates, {db_stations} stations")
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--stations", type=str)
    parser.add_argument("--grib-dir", default=str(REPO_ROOT / "data" / "tigge_grib"))
    parser.add_argument("--request-delay", type=float, default=REQUEST_DELAY_S)
    parser.add_argument("--steps", type=int, nargs="+", default=TIGGE_STEPS)
    parser.add_argument("--source", choices=["ecds", "openmeteo"], default="ecds")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
        for s in stations:
            if s not in STATION_MAP:
                print(f"Unknown station: {s}")
                sys.exit(1)
        args.stations = stations
    
    if args.dry_run:
        import calendar
        s = args.start.split("-")
        e = args.end.split("-")
        days = (date(int(e[0]),int(e[1]),int(e[2])) - date(int(s[0]),int(s[1]),int(s[2]))).days + 1
        print(f"DRY RUN: {days} days x {len(args.stations or STATION_NAMES)} stations x {len(args.steps)} steps")
        print(f"  Total rows: ~{days * len(args.stations or STATION_NAMES) * len(args.steps)}")
        print(f"  ECDS requests (1/step): {days * len(args.steps)}")
        sys.exit(0)
    
    # Verify dependencies
    import eccodes
    eccodes_ver = eccodes.__version__ if hasattr(eccodes, '__version__') else '?'
    print(f"eccodes: v{eccodes_ver}")
    
    if args.source == "ecds":
        try:
            import cdsapi
            # Don't test connection here - just proceed
            print(f"cdsapi: available")
        except ImportError:
            print("ERROR: cdsapi not installed")
            sys.exit(1)
    
    run_backfill(args)


if __name__ == "__main__":
    main()
