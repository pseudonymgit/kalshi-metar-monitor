#!/usr/bin/env python3
"""
Manual TIGGE date collection and database repair for missing dates.
Direct API calls to bypass broken checkpoint system.
"""
import os, sys, json, time, sqlite3, struct, zlib
from datetime import datetime, date, timedelta
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = os.environ.get("ECDS_KEY", "")  # GS-C5: secret-free; read from env
DB_PATH = os.path.join(ROOT, "data", "tigge_archive.db")
GRIB_DIR = os.path.join(ROOT, "data", "tigge_grib")

os.makedirs(GRIB_DIR, exist_ok=True)

# All 20 Kalshi stations
STATIONS = [
    ("KNYC", 40.71, -74.01), ("KLAX", 33.94, -118.41), ("KATL", 33.64, -84.43),
    ("KBOS", 42.37, -71.01), ("KDEN", 39.86, -104.67), ("KDFW", 32.90, -97.04),
    ("KHOU", 29.65, -95.28), ("KLAS", 36.08, -115.15), ("KMDW", 41.79, -87.75),
    ("KMIA", 25.80, -80.29), ("KMSP", 44.88, -93.22), ("KMSY", 29.99, -90.26),
    ("KOKC", 35.39, -97.60), ("KPHL", 39.87, -75.24), ("KPHX", 33.45, -112.08),
    ("KSAT", 29.42, -98.49), ("KSEA", 47.45, -122.31), ("KSFO", 37.62, -122.37),
    ("KDCA", 38.85, -77.04), ("KAUS", 30.19, -97.67),
]
ALL_STEPS = [0, 6, 12, 18, 24]  # TIGGE ECMWF only provides 6-hourly


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)


def encode_member_temps(temps, mean_c):
    """Encode member temperatures as compressed diffs from mean."""
    raw = bytearray()
    for t in temps:
        diff = int(round((t - mean_c) * 10))
        raw.extend(struct.pack("<h", diff))
    return zlib.compress(bytes(raw), 1)


def parse_grib_for_stations(grib_path):
    """Parse TIGGE GRIB file, extracting all 51 members × 9 steps × 20 stations.
    
    Returns {station: {step_h: [temp_c_values]}} or None on failure.
    """
    import eccodes
    if not os.path.exists(grib_path) or os.path.getsize(grib_path) < 100:
        return None
    
    f = open(grib_path, 'rb')
    first = eccodes.codes_grib_new_from_file(f)
    if first is None:
        f.close()
        return None
    
    try:
        ni = eccodes.codes_get_long(first, 'Ni')
        nj = eccodes.codes_get_long(first, 'Nj')
        lats = eccodes.codes_get_array(first, 'latitudes')
        lons = eccodes.codes_get_array(first, 'longitudes')
        lats_2d = lats.reshape(nj, ni)
        lons_2d = lons.reshape(nj, ni)
    except Exception as e:
        eccodes.codes_release(first)
        f.close()
        return None
    eccodes.codes_release(first)
    f.close()
    
    # For each station, find nearest grid point
    station_indices = {}
    for name, s_lat, s_lon in STATIONS:
        adj_lon = s_lon + 360 if lons_2d.max() > 180 and s_lon < 0 else s_lon
        dist = np.sqrt((lats_2d - s_lat)**2 + (lons_2d - adj_lon)**2)
        li, loi = np.unravel_index(np.argmin(dist), lats_2d.shape)
        station_indices[name] = (li, loi)
    
    # Parse all messages: 51 members × 9 steps × (params) per date
    # Only extract 2 metre temperature (param=167)
    f = open(grib_path, 'rb')
    result = {}  # station -> {step_h: [temp_c]}
    msg_count = 0
    while True:
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None:
            break
        msg_count += 1
        try:
            name = eccodes.codes_get_string(gid, 'name')
            if '2 metre temperature' not in name.lower() and '2 meter temperature' not in name.lower():
                eccodes.codes_release(gid)
                continue
            step = eccodes.codes_get_long(gid, 'step')
            vals = eccodes.codes_get_values(gid)
            v2d = vals.reshape(nj, ni)
            
            for sname, (li, loi) in station_indices.items():
                temp_k = float(v2d[li, loi])
                temp_c = round(temp_k - 273.15, 2)
                if sname not in result:
                    result[sname] = {}
                if step not in result[sname]:
                    result[sname][step] = []
                result[sname][step].append(temp_c)
        except Exception as e:
            pass  # Skip corrupt messages
        eccodes.codes_release(gid)
    f.close()
    
    return result


def store_results(conn, target_date, parsed):
    """Store parsed GRIB data into tigge_archive.db."""
    stored = 0
    for sname, steps_data in parsed.items():
        for step_h, temps in steps_data.items():
            if not temps:
                continue
            mn = round(sum(temps) / len(temps), 2)
            mi = round(min(temps), 2)
            mx = round(max(temps), 2)
            blob = encode_member_temps(temps, mn)
            conn.execute("""INSERT OR REPLACE INTO tigge_archive
                (target_date, station, step, source, ensemble_mean, ensemble_min,
                 ensemble_max, member_values, n_members)
                VALUES (?,?,?,'tigge_ecmwf',?,?,?,?,?)""",
                (target_date, sname, step_h, mn, mi, mx, blob, len(temps)))
            stored += 1
    conn.commit()
    return stored


def main():
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    
    # Define missing dates to collect
    missing_dates = ['2026-09-20', '2026-09-21']
    
    log(f"Starting manual TIGGE repair for missing dates: {missing_dates}")
    
    import cdsapi
    c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY, wait_until_complete=True)
    
    processed = 0
    errors = 0
    
    for target_date in missing_dates:
        log(f"Processing date: {target_date}")
        
        # Check if date already exists in database
        cursor = conn.execute("SELECT COUNT(*) FROM tigge_archive WHERE target_date = ?", (target_date,))
        existing_count = cursor.fetchone()[0]
        
        if existing_count > 0:
            log(f"  Date {target_date} already has {existing_count} records, skipping")
            continue
        
        grib_path = os.path.join(GRIB_DIR, f"tigge_{target_date}_manual.grib")
        
        # ── REQUEST DATA FOR THE DATE ──
        req = {
            "class": "ti",
            "expver": "prod",
            "dataset": "tigge-forecasts",
            "date": target_date,
            "grid": "0.5/0.5",
            "levtype": "sfc",
            "origin": "ecmf",
            "param": "167",  # 2 metre temperature
            "step": "/".join(str(s) for s in ALL_STEPS),
            "time": "00:00:00",
            "type": "pf",  # Perturbed forecast
            "number": "1/to/50",  # 50 perturbed members
        }
        
        log(f"  Requesting data for {target_date}...")
        t_req = time.time()
        try:
            c.retrieve("tigge-forecasts", req, grib_path)
            elapsed = time.time() - t_req
            sz = os.path.getsize(grib_path) if os.path.exists(grib_path) else 0
            log(f"  → Downloaded {sz/1024/1024:.1f} MB in {elapsed:.0f}s")
        except Exception as e:
            err_str = str(e)[:200]
            log(f"  → FAILED: {err_str}")
            errors += 1
            # Try alternative approach with different time setting
            try:
                log("  → Trying alternative request with different time setting...")
                req_alt = req.copy()
                req_alt['time'] = '12:00:00'
                c.retrieve("tigge-forecasts", req_alt, grib_path)
                elapsed = time.time() - t_req
                sz = os.path.getsize(grib_path) if os.path.exists(grib_path) else 0
                log(f"  → Alternative request succeeded: {sz/1024/1024:.1f} MB in {elapsed:.0f}s")
            except Exception as e2:
                err_str2 = str(e2)[:200]
                log(f"  → Alternative request also FAILED: {err_str2}")
                continue  # Skip this date and continue with others
        
        # ── PARSE AND STORE DATA ──
        if not os.path.exists(grib_path) or os.path.getsize(grib_path) < 1000:
            log(f"  → GRIB file empty or missing, skipping {target_date}")
            continue
        
        t_parse = time.time()
        parsed = parse_grib_for_stations(grib_path)
        if parsed is None:
            log(f"  → Parse FAILED for {target_date}")
            continue
        
        # Store the parsed data
        stored = store_results(conn, target_date, parsed)
        parse_elapsed = time.time() - t_parse
        
        log(f"  → Parsed + stored {stored} records for {target_date} in {parse_elapsed:.1f}s")
        
        # Clean up the temporary GRIB file
        try:
            if os.path.exists(grib_path):
                os.remove(grib_path)
                log(f"  → Purged raw GRIB file for {target_date}")
        except Exception as e:
            log(f"  → Failed to delete GRIB file: {e}")
        
        processed += 1
        
        # Add a small delay between requests to be respectful to the API
        time.sleep(2)
    
    # Final verification
    log(f"\nRepair process completed: {processed} dates processed, {errors} errors")
    
    for target_date in missing_dates:
        cursor = conn.execute("SELECT COUNT(*) FROM tigge_archive WHERE target_date = ?", (target_date,))
        count = cursor.fetchone()[0]
        log(f"Final check - Date {target_date}: {count} records")
    
    # Check total unique dates in the database now
    cursor = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive")
    total_dates = cursor.fetchone()[0]
    log(f"Total unique dates in database: {total_dates}")
    
    conn.close()
    log("Manual TIGGE repair completed.")


if __name__ == "__main__":
    main()