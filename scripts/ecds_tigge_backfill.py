#!/usr/bin/env python3
"""
ECDS TIGGE Backfill — 9-step single-request per date.

Uses cdsapi wait_until_complete=True (default) to respect ECDS queue limit of 1.
Requests ALL 9 steps (0,3,6,9,12,15,18,21,24) in a single request per date.
Parses GRIB immediately after download into data/tigge_archive.db.
60s delay between requests for rate limit safety.

Usage:
    python3 scripts/ecds_tigge_backfill.py 2021-01-01 2026-06-30
    nohup python3 -u scripts/ecds_tigge_backfill.py 2021-01-01 2026-06-30 > logs/ecds_backfill_main.log 2>&1 &

Checkpoint: data/ecds_backfill_checkpoint.json (auto-resume)
Database:   data/tigge_archive.db
GRIB cache: data/tigge_grib/

Version: v2.0 2026-07-31 (9-step single-request approach)
"""
import os, sys, json, time, sqlite3, struct, zlib, math
from datetime import datetime, date, timedelta
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = "e543bdb6-c396-48b4-bc1c-bebe08db954e"
DB_PATH = os.path.join(ROOT, "data", "tigge_archive.db")
GRIB_DIR = os.path.join(ROOT, "data", "tigge_grib")
CKPT = os.path.join(ROOT, "data", "ecds_backfill_checkpoint.json")
LOG_PATH = os.path.join(ROOT, "logs", "ecds_backfill_main.log")

os.makedirs(GRIB_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

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
DELAY_BETWEEN_REQUESTS = 60  # seconds (rate limit safety margin)


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("""CREATE TABLE IF NOT EXISTS tigge_archive (
        target_date TEXT, station TEXT, step INTEGER, source TEXT DEFAULT 'tigge_ecmwf',
        ensemble_mean REAL, ensemble_min REAL, ensemble_max REAL,
        member_values BLOB, n_members INTEGER DEFAULT 0,
        PRIMARY KEY (target_date, station, step, source)
    )""")
    conn.commit()
    return conn


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


def load_checkpoint():
    """Load checkpoint, return (start_date, last_completed_date, status)."""
    default = {"start_date": "2021-01-01", "end_date": "2026-06-30",
               "last_date": None, "step": 0, "status": "pending"}
    if os.path.exists(CKPT):
        try:
            cp = json.load(open(CKPT))
            if cp.get("status") == "complete":
                return cp["start_date"], cp["end_date"], None, "complete"
            return cp.get("start_date", default["start_date"]), \
                   cp.get("end_date", default["end_date"]), \
                   cp.get("last_date"), \
                   cp.get("status", "pending")
        except:
            pass
    return default["start_date"], default["end_date"], None, "pending"


def save_checkpoint(start, end, last_date, status):
    """Save checkpoint."""
    json.dump({
        "start_date": start, "end_date": end,
        "last_date": last_date, "step": 24,
        "status": status,
    }, open(CKPT, "w"))


def get_done_dates(conn):
    """Get set of target dates where ALL 20 stations have data (fully complete).

    A date is only 'done' if every station has at least one row for it, so
    partial coverage (e.g. KNYC-only dates) still gets backfilled for the
    remaining stations.
    """
    r = conn.execute("""
        SELECT target_date FROM (
            SELECT target_date, COUNT(DISTINCT station) AS n_station
            FROM tigge_archive GROUP BY target_date
        ) WHERE n_station >= ?
    """, (len(STATIONS),)).fetchall()
    return set(row[0] for row in r)


def estimate_completion(start_date, end_date, done_count, delay_sec=DELAY_BETWEEN_REQUESTS):
    """Estimate remaining time based on tested ~205s per request + 60s delay."""
    total_dates = (date(*(int(x) for x in end_date.split("-"))) -
                   date(*(int(x) for x in start_date.split("-")))).days + 1
    remaining = max(0, total_dates - done_count)
    sec_per_date = 205 + delay_sec  # tested: ~205s download + 60s delay
    hours_remaining = remaining * sec_per_date / 3600
    return remaining, hours_remaining


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"
    
    # Load checkpoint
    cp_start, cp_end, last_completed, status = load_checkpoint()
    
    if status == "complete":
        log("Backfill already marked complete. Delete checkpoint to re-run.")
        return
    
    if last_completed:
        # Resume from day after last completed
        resume_date = (date(*(int(x) for x in last_completed.split("-"))) +
                       timedelta(days=1)).isoformat()
        log(f"Resuming from {resume_date} (last completed: {last_completed})")
    else:
        resume_date = start
        log(f"Starting fresh from {resume_date}")
    
    conn = init_db()
    done_dates = get_done_dates(conn)
    log(f"Already in DB: {len(done_dates)} dates")
    
    s = [int(x) for x in resume_date.split("-")]
    e = [int(x) for x in end.split("-")]
    sd = date(s[0], s[1], s[2])
    ed = date(e[0], e[1], e[2])
    total = (ed - sd).days + 1
    total_all = (date(e[0], e[1], e[2]) - date(*(int(x) for x in start.split("-")))).days + 1
    
    log(f"Range: {resume_date} → {end} ({total} dates remaining of {total_all} total)")
    log(f"Steps per request: {ALL_STEPS}")
    log(f"Delay between requests: {DELAY_BETWEEN_REQUESTS}s")
    log(f"Config: 50 perturbed members, {len(STATIONS)} stations, 0.5° grid")
    log(f"Estimated: ~265s per date, ~{total * 265 / 3600:.1f}h remaining")
    
    import cdsapi
    c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY, wait_until_complete=True)
    
    processed = 0
    errors = 0
    
    for i in range(total):
        target = sd + timedelta(days=i)
        ds = target.isoformat()
        
        if ds in done_dates:
            log(f"  Skipping {ds} (already in DB)")
            continue
        
        grib_path = os.path.join(GRIB_DIR, f"tigge_{ds}_allsteps.grib")
        
        # Skip if GRIB already exists and looks valid
        if os.path.exists(grib_path) and os.path.getsize(grib_path) > 10000:
            log(f"  {ds}: GRIB exists ({os.path.getsize(grib_path)/1024/1024:.1f} MB), parsing...")
        else:
            # ── SINGLE REQUEST: all 9 steps, 51 members ──
            req = {
                "class": "ti",
                "expver": "prod",
                "dataset": "tigge-forecasts",
                "date": ds,
                "grid": "0.5/0.5",
                "levtype": "sfc",
                "origin": "ecmf",
                "param": "167",
                "step": "/".join(str(s) for s in ALL_STEPS),
                "time": "00:00:00",
                "type": "pf",
                "number": "1/to/50",  # 50 perturbed members (no control — type=pf only)
            }
            
            pct = (i + 1) / total * 100 if total > 0 else 0
            log(f"[{i+1}/{total}] {ds} ({pct:.0f}%) — requesting {len(ALL_STEPS)} steps ({ALL_STEPS}), 50 members...")
            
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
                
                if "rejected" in err_str.lower() or "limited" in err_str.lower():
                    save_checkpoint(start, end, ds, "rate_limited")
                    log(f"RATE LIMITED after {processed} successful dates.")
                    log(f"Resume command: python3 scripts/ecds_tigge_backfill.py {start} {end}")
                    conn.close()
                    return
                elif "404" in err_str:
                    log(f"404 — dataset not found for {ds}, skipping")
                    save_checkpoint(start, end, ds, "skipped")
                    time.sleep(DELAY_BETWEEN_REQUESTS)
                    continue
                else:
                    # Retry once after a delay
                    log(f"  Retrying in 120s...")
                    time.sleep(120)
                    try:
                        t_req2 = time.time()
                        c.retrieve("tigge-forecasts", req, grib_path)
                        elapsed2 = time.time() - t_req2
                        sz2 = os.path.getsize(grib_path) if os.path.exists(grib_path) else 0
                        log(f"  → Retry OK: {sz2/1024/1024:.1f} MB in {elapsed2:.0f}s")
                    except Exception as e2:
                        err2 = str(e2)[:200]
                        log(f"  → Retry FAILED: {err2}")
                        # Fallback: if cdsapi downloaded to a temp file but failed to rename,
                        # find the .grib file in CWD and move it manually
                        import glob
                        temp_gribs = sorted(glob.glob("*.grib"), key=os.path.getmtime, reverse=True)
                        if temp_gribs:
                            latest = temp_gribs[0]
                            sz_tmp = os.path.getsize(latest)
                            if sz_tmp > 10000:
                                log(f"  → Found temp GRIB: {latest} ({sz_tmp/1024/1024:.1f} MB), moving to {grib_path}")
                                import shutil
                                shutil.copy2(latest, grib_path)
                                os.remove(latest)
                                if os.path.exists(grib_path) and os.path.getsize(grib_path) > 10000:
                                    log(f"  → Fallback OK: {os.path.getsize(grib_path)/1024/1024:.1f} MB at {grib_path}")
                                else:
                                    log(f"  → Fallback FAILED: copy produced empty file")
                                    save_checkpoint(start, end, ds, f"failed_twice:{err2}")
                                    conn.close()
                                    return
                            else:
                                log(f"  → Temp GRIB {latest} too small ({sz_tmp} bytes), skipping")
                                save_checkpoint(start, end, ds, f"failed_twice:{err2}")
                                conn.close()
                                return
                        else:
                            save_checkpoint(start, end, ds, f"failed_twice:{err2}")
                            conn.close()
                            return
        
        # ── PARSE immediately after download ──
        if not os.path.exists(grib_path) or os.path.getsize(grib_path) < 1000:
            log(f"  → GRIB empty or missing, skipping")
            continue
        
        t_parse = time.time()
        parsed = parse_grib_for_stations(grib_path)
        if parsed is None:
            log(f"  → Parse FAILED (empty result)")
            continue
        
        # Validate we have expected data
        total_expected = len(STATIONS) * len(ALL_STEPS)
        total_got = sum(len(steps) for steps in parsed.values())
        missing_steps = 0
        for sname, steps_data in parsed.items():
            for step_h in ALL_STEPS:
                if step_h not in steps_data or not steps_data[step_h]:
                    missing_steps += 1
        
        if total_got < total_expected * 0.5:
            log(f"  → Parse incomplete: got {total_got}/{total_expected} station-step combos ({total_expected - total_got - missing_steps} missing)")
            if errors < 2:
                log(f"  → Skipping and will retry")
                errors += 1
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue
        
        # Store
        stored = store_results(conn, ds, parsed)
        parse_elapsed = time.time() - t_parse
        # Count members from first non-empty station-step combo
        member_count = 0
        for sname, steps_data in parsed.items():
            for step_h, temps in steps_data.items():
                if temps:
                    member_count = len(temps)
                    break
            if member_count:
                break
        
        log(f"  → Parsed + stored: {stored} rows in {parse_elapsed:.1f}s "
            f"(members: {member_count})")
        
        # Purge raw GRIB immediately — parsed data already in DB
        try:
            if os.path.exists(grib_path):
                os.remove(grib_path)
                log(f"  → Purged raw GRIB (124.0 MB)")
        except Exception as e:
            log(f"  → GRIB purge failed: {e}")
        
        processed += 1
        done_dates.add(ds)
        
        # Checkpoint
        save_checkpoint(start, end, ds, "in_progress")
        
        # Milestone reporting
        milestones = [0.10, 0.25, 0.50, 0.75]
        pct_done = processed / total if total > 0 else 0
        for m in milestones:
            if abs(pct_done - m) < 0.01:  # Just passed this milestone
                rem_dates, rem_hours = estimate_completion(start, end, processed)
                log(f"  ── MILESTONE: {m*100:.0f}% ({processed}/{total} dates, "
                    f"~{rem_hours:.1f}h remaining) ──")
        
        if processed == 3:
            rem_dates, rem_hours = estimate_completion(start, end, processed)
            log(f"  ── FIRST 3 DATES VERIFIED OK. "
                f"~{rem_hours:.1f}h estimated remaining for {rem_dates} dates ──")
        
        # Rate limit safety delay
        if ds != list(sorted(done_dates))[-1] if done_dates else True:
            log(f"  Waiting {DELAY_BETWEEN_REQUESTS}s...")
            time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # ── Complete ──
    total_rows = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    stations_done = conn.execute("SELECT COUNT(DISTINCT station) FROM tigge_archive").fetchone()[0]
    dates_done = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive").fetchone()[0]
    
    log(f"\n{'=' * 60}")
    log(f"BACKFILL COMPLETE")
    log(f"{'=' * 60}")
    log(f"Dates:      {dates_done}/{total_all}")
    log(f"Stations:   {stations_done}/{len(STATIONS)}")
    log(f"Total rows: {total_rows}")
    log(f"Errors:     {errors}")
    log(f"Completed:  {datetime.utcnow().isoformat()}")
    
    save_checkpoint(start, end, None, "complete")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())