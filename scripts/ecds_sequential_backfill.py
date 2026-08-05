#!/usr/bin/env python3
"""
ECDS Sequential Backfill — OPTIMIZED

Optimizations vs original:
  1. Parse GRIB ONCE for all 20 stations (was 20× per date)
  2. Batch dates: request 3 dates per CDSAPI call (was 1)
  3. Remove 60s delay between requests (was necessary for concurrent keys)
  4. GRIB deleted after parsing

Usage:
    python3 scripts/ecds_sequential_backfill.py --start 2021-08-29 --end 2023-09-27
    python3 scripts/ecds_sequential_backfill.py --start 2026-01-10 --end 2026-05-03
    python3 scripts/ecds_sequential_backfill.py --resume
    python3 scripts/ecds_sequential_backfill.py --resume --batch-days 5
"""

import os, sys, time, sqlite3, json, struct
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Tuple, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_ECDS_URL = "https://ecds.ecmwf.int/api"
# Key loaded from .env at runtime (see main())
_ECDS_KEY = ""

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
GRIB_DIR = str(REPO_ROOT / "data" / "tigge_grib")
# Request 0-24h steps — the historical TIGGE archive only has 6-hourly
# data (0, 6, 12, 18, 24). The 3-hourly steps (3, 9, 15, 21) only exist
# in the live feed (2026-08-01+), which is already in the DB. So we only
# request the 6-hourly steps for the historical backfill to avoid ~44%
# wasted download on empty steps.
TIGGE_STEPS = [0, 6, 12, 18, 24]
BATCH_DAYS_DEFAULT = 5  # Default: 5 dates per CDSAPI call
DELAY_S = 5  # Small delay to respect ECDS queue

CHECKPOINT_FILE = str(REPO_ROOT / "data" / "ecds_checkpoint.json")
os.makedirs(GRIB_DIR, exist_ok=True)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tigge_archive (
            target_date TEXT NOT NULL, station TEXT NOT NULL,
            step INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'tigge_ecmwf',
            ensemble_mean REAL, ensemble_min REAL, ensemble_max REAL,
            member_values BLOB, n_members INTEGER DEFAULT 0,
            PRIMARY KEY (target_date, station, step, source)
        )
    """)
    conn.commit()
    return conn


def get_dates_to_backfill(conn, start: str, end: str) -> List[str]:
    """Return all dates in [start, end] that are NOT in the archive."""
    start_dt = date.fromisoformat(start)
    end_dt = date.fromisoformat(end)
    done = set()
    cur = conn.execute("SELECT DISTINCT target_date FROM tigge_archive WHERE source='tigge_ecmwf'")
    for row in cur.fetchall():
        done.add(row[0])
    result = []
    d = start_dt
    while d <= end_dt:
        if d.isoformat() not in done:
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def save_checkpoint(date_str: str):
    data = {"last_date": date_str, "updated_at": datetime.utcnow().isoformat()}
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(data, f)


def load_checkpoint():
    try:
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def store_ensemble(conn, station, date_str, step, temps):
    if not temps:
        return
    n_members = len(temps)
    mean = round(sum(temps) / n_members, 2)
    mn = min(temps)
    mx = max(temps)
    values_blob = struct.pack(f'<{n_members}f', *temps)
    conn.execute("""
        INSERT OR REPLACE INTO tigge_archive
        (target_date, station, step, source, ensemble_mean, ensemble_min, ensemble_max, member_values, n_members)
        VALUES (?, ?, ?, 'tigge_ecmwf', ?, ?, ?, ?, ?)
    """, (date_str, station, step, mean, mn, mx, values_blob, n_members))


def parse_tigge_grib_all_stations(grib_file: str) -> Dict[str, Dict[int, List[float]]]:
    """
    Parse GRIB in ONE PASS for ALL 20 stations.
    
    Returns: {station_code: {step_hour: [temp_c_1, temp_c_2, ...]}}
    
    Key optimization: pre-compute nearest grid point indices on first message,
    then extract at those indices for all subsequent messages.
    """
    import eccodes
    
    # Result structure: station → step → list of member temperatures
    result: Dict[str, Dict[int, List[float]]] = {s: {} for s in STATION_NAMES}
    station_indices = None  # {station_code: flat_index}
    grib_msg_count = 0
    station_pulls = 0
    
    if not os.path.exists(grib_file):
        return result
    
    with open(grib_file, 'rb') as f:
        while True:
            try:
                gid = eccodes.codes_grib_new_from_file(f)
            except Exception:
                break
            if gid is None:
                break
            
            grib_msg_count += 1
            
            try:
                # Get step — skip if not in our target list
                step = eccodes.codes_get_long(gid, 'step')
                if step not in TIGGE_STEPS:
                    eccodes.codes_release(gid)
                    continue
                
                # Initialize step containers on first encounter
                if station_indices is None:
                    # First message: extract grid and compute nearest-point indices
                    lats = eccodes.codes_get_array(gid, 'latitudes')
                    lons = eccodes.codes_get_array(gid, 'longitudes')
                    station_indices = {}
                    for station in STATION_NAMES:
                        st_lat, st_lon = STATION_MAP[station]
                        best_dist = float('inf')
                        best_idx = 0
                        for i in range(len(lats)):
                            dlat = lats[i] - st_lat
                            dlon = lons[i] - st_lon
                            dist = dlat * dlat + dlon * dlon
                            if dist < best_dist:
                                best_dist = dist
                                best_idx = i
                        station_indices[station] = best_idx
                
                # Initialize step for all stations
                for s in STATION_NAMES:
                    if step not in result[s]:
                        result[s][step] = []
                
                # Extract temperature at all station grid points
                values = eccodes.codes_get_values(gid)
                for station in STATION_NAMES:
                    temp_k = float(values.flat[station_indices[station]])
                    temp_c = round(temp_k - 273.15, 2)
                    result[station][step].append(temp_c)
                    station_pulls += 1
                    
            except Exception as e:
                pass
            
            eccodes.codes_release(gid)
    
    print(f"    GRIB parsed: {grib_msg_count} msgs, {station_pulls} station-pulls, "
          f"{sum(len(result[s][step]) for s in STATION_NAMES for step in result[s])} total values")
    return result


def process_batch(date_batch: List[str], conn: sqlite3.Connection) -> bool:
    """
    Download and process a batch of dates in a single CDSAPI call.
    Returns True if all dates in batch succeeded.
    """
    import cdsapi
    
    if not date_batch:
        return True
    
    batch_start = date_batch[0]
    batch_end = date_batch[-1]
    n_dates = len(date_batch)
    
    # Single GRIB for the whole batch
    batch_str = f"{batch_start.replace('-', '')}/{batch_end.replace('-', '')}"
    grib_file = os.path.join(GRIB_DIR, f"tigge_{batch_start}_to_{batch_end}.grib")
    
    # Skip download if GRIB already cached
    if not os.path.exists(grib_file) or os.path.getsize(grib_file) < 1000:
        c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY)
        step_str = "/".join(str(s) for s in TIGGE_STEPS)
        request = {
            'class': 'ti', 'expver': 'prod', 'dataset': 'tigge-forecasts',
            'date': batch_str, 'grid': '0.5/0.5', 'levtype': 'sfc',
            'origin': 'ecmf', 'param': '167', 'step': step_str,
            'time': '00:00:00', 'type': 'pf', 'number': '1/to/50',
        }
        # Retry loop: up to MAX_RETRIES attempts with exponential backoff.
        # ECDS is prone to timeouts on large downloads; retrying with a patient
        # backoff avoids flooding the queue while surviving transient failures.
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            # Clean up any partial download from a previous attempt
            if os.path.exists(grib_file):
                try:
                    os.remove(grib_file)
                except OSError:
                    pass
            print(f"  ECDS batch request [{batch_start} → {batch_end}] (attempt {attempt}/{max_retries})...", end=" ", flush=True)
            t0 = time.time()
            try:
                c.retrieve('tigge-forecasts', request, grib_file)
                elapsed = time.time() - t0
                size = os.path.getsize(grib_file)
                rate = size / 1024 / elapsed if elapsed > 0 else 0
                print(f"({elapsed:.0f}s, {size/1024/1024:.0f}MB, {rate:.0f}KB/s)")
                break  # success
            except Exception as e:
                elapsed = time.time() - t0
                print(f"FAILED ({elapsed:.0f}s): {e}")
                if attempt < max_retries:
                    # Exponential backoff: 30s, 120s, then give up
                    backoff = 30 * (2 ** (attempt - 1))
                    print(f"    Retrying in {backoff}s...")
                    time.sleep(backoff)
                else:
                    print(f"    Giving up after {max_retries} attempts.")
                    return False
    else:
        size = os.path.getsize(grib_file)
        print(f"  GRIB cached ({size/1024/1024:.0f}MB), parsing...")
    
    # Parse the multi-date GRIB once — GRIB messages have dataDate field
    # to distinguish which date each forecast is for
    import eccodes
    
    # Structure: {date: {station: {step: [temps]}}}
    batch_data: Dict[str, Dict[str, Dict[int, List[float]]]] = {}
    for ds in date_batch:
        batch_data[ds] = {s: {} for s in STATION_NAMES}
    
    station_indices = None
    total_msgs = 0
    
    with open(grib_file, 'rb') as f:
        while True:
            try:
                gid = eccodes.codes_grib_new_from_file(f)
            except Exception:
                break
            if gid is None:
                break
            
            total_msgs += 1
            
            try:
                # Get date from GRIB message
                msg_date_int = eccodes.codes_get_long(gid, 'dataDate')
                msg_date = f"{msg_date_int // 10000}-{(msg_date_int // 100) % 100:02d}-{msg_date_int % 100:02d}"
                
                # Skip if not in our batch
                if msg_date not in batch_data:
                    eccodes.codes_release(gid)
                    continue
                
                step = eccodes.codes_get_long(gid, 'step')
                if step not in TIGGE_STEPS:
                    eccodes.codes_release(gid)
                    continue
                
                # Initialize step containers
                for s in STATION_NAMES:
                    if step not in batch_data[msg_date][s]:
                        batch_data[msg_date][s][step] = []
                
                # Pre-compute grid indices on first message
                if station_indices is None:
                    lats = eccodes.codes_get_array(gid, 'latitudes')
                    lons = eccodes.codes_get_array(gid, 'longitudes')
                    station_indices = {}
                    for station in STATION_NAMES:
                        st_lat, st_lon = STATION_MAP[station]
                        best_dist = float('inf')
                        best_idx = 0
                        for i in range(len(lats)):
                            dlat = lats[i] - st_lat
                            dlon = lons[i] - st_lon
                            dist = dlat * dlat + dlon * dlon
                            if dist < best_dist:
                                best_dist = dist
                                best_idx = i
                        station_indices[station] = best_idx
                
                values = eccodes.codes_get_values(gid)
                for station in STATION_NAMES:
                    temp_k = float(values.flat[station_indices[station]])
                    temp_c = round(temp_k - 273.15, 2)
                    batch_data[msg_date][station][step].append(temp_c)
                    
            except Exception:
                pass
            
            eccodes.codes_release(gid)
    
    # Write all data to DB
    inserts = 0
    for date_str in date_batch:
        for station in STATION_NAMES:
            for step_h, temps in batch_data[date_str][station].items():
                store_ensemble(conn, station, date_str, step_h, temps)
                inserts += 1
        save_checkpoint(date_str)  # Checkpoint per-date within batch
    
    conn.commit()
    print(f"    DB: {inserts} inserts across {n_dates} dates ({total_msgs} GRIB msgs parsed)")
    
    # Clean up GRIB file
    if os.path.exists(grib_file):
        os.remove(grib_file)
        print(f"    GRIB cleaned up")
    
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ECDS TIGGE backfill (optimized)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--batch-days", type=int, default=BATCH_DAYS_DEFAULT,
                        help=f"Dates per CDSAPI call (default: {BATCH_DAYS_DEFAULT})")
    args = parser.parse_args()
    
    # Load .env at runtime
    _env_path = REPO_ROOT / '.env'
    if _env_path.exists():
        for _line in open(_env_path):
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                if _k == 'ECDS_KEY':
                    globals()['_ECDS_KEY'] = _v
                if _k == 'ECDS_URL':
                    globals()['_ECDS_URL'] = _v
    
    if not _ECDS_KEY:
        print("ERROR: ECDS_KEY not found in .env")
        sys.exit(1)
    
    conn = init_db()
    batch_size = max(1, args.batch_days)
    
    if args.resume:
        cp = load_checkpoint()
        if not cp:
            print("No checkpoint found. Use --start/--end.")
            conn.close()
            return
        resume_from = date.fromisoformat(cp["last_date"]) + timedelta(days=1)
        cur = conn.execute("SELECT MAX(target_date) FROM tigge_archive")
        max_d = cur.fetchone()[0]
        end = date.fromisoformat(max_d) if max_d else date.today()
        if resume_from > end:
            print("All dates up to checkpoint are filled.")
            conn.close()
            return
        dates = get_dates_to_backfill(conn, resume_from.isoformat(), end.isoformat())
        print(f"Resuming from {cp['last_date']} → {end.isoformat()}")
    elif args.start and args.end:
        dates = get_dates_to_backfill(conn, args.start, args.end)
    else:
        print("Need --start/--end or --resume")
        conn.close()
        return
    
    if not dates:
        print("No missing dates to backfill. All done!")
        conn.close()
        return
    
    # Group into batches
    batches = [dates[i:i + batch_size] for i in range(0, len(dates), batch_size)]
    
    print(f"ECDS Sequential Backfill (OPTIMIZED)")
    print(f"  Range: {dates[0]} to {dates[-1]} ({len(dates)} dates)")
    print(f"  Steps: {len(TIGGE_STEPS)} ({','.join(str(s) for s in TIGGE_STEPS)})")
    print(f"  Stations: {len(STATION_NAMES)}")
    print(f"  Batch size: {batch_size} dates/request ({len(batches)} batches)")
    print(f"  Delay: {DELAY_S}s between batches")
    
    # Estimated time: ~80s per batch download + ~15s parse per batch
    est_per_batch = 95  # seconds
    est_total = len(batches) * (est_per_batch + DELAY_S)
    print(f"  Est. time: {est_total:.0f}s ({est_total/60:.0f}min, ~{est_total/3600:.1f}h)")
    print()
    
    t_start = time.time()
    failed_batches = 0
    
    for bi, batch in enumerate(batches):
        elapsed = time.time() - t_start
        done_dates = bi * batch_size
        rate = done_dates / (elapsed / 3600) if elapsed > 0 else 0
        remain_dates = len(dates) - done_dates
        remain_hours = remain_dates / rate if rate > 0 else 0
        
        batch_range = f"{batch[0]}..{batch[-1]}"
        print(f"[{bi+1}/{len(batches)}] batch {batch_range} "
              f"({done_dates}/{len(dates)} dates, {rate:.1f}/h, ~{remain_hours:.0f}h remain)")
        
        ok = process_batch(batch, conn)
        if not ok:
            failed_batches += 1
            print(f"  BATCH FAILED. Trying individual fallback...")
            # Fall back to individual dates
            for ds in batch:
                ok_single = process_batch([ds], conn)
                if ok_single:
                    print(f"  {ds}: OK (after batch fallback)")
                else:
                    print(f"  {ds}: FAILED. Skipping.")
        
        if bi < len(batches) - 1 and DELAY_S > 0:
            time.sleep(DELAY_S)
    
    elapsed = time.time() - t_start
    db_rows = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    db_dates = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive").fetchone()[0]
    print(f"\n{'='*60}")
    print(f"BACKFILL COMPLETE")
    print(f"  Time: {elapsed:.0f}s ({elapsed/60:.1f}min, {elapsed/3600:.1f}h)")
    print(f"  Dates processed: {len(dates)}")
    print(f"  Failed batches: {failed_batches}")
    print(f"  DB: {db_rows} rows, {db_dates} dates")
    print(f"  Rate: {len(dates)/(elapsed/3600):.1f} dates/hour")
    print(f"{'='*60}")
    conn.close()


if __name__ == "__main__":
    main()