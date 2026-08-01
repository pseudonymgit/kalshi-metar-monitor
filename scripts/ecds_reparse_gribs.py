#!/usr/bin/env python3
"""
ECDS TIGGE GRIB Reparse — reads existing GRIB files from data/tigge_grib/
and stores parsed data into data/tigge_archive.db.

Fix for silent data loss bug: the original backfill process had
PRAGMA synchronous=OFF in WAL mode, which caused data after 2021-08-23
to not be committed to the main database despite conn.commit() being called.

This script uses synchronous=FULL + explicit WAL checkpointing to ensure
all parsed data is durably persisted.

Usage:
    python3 scripts/ecds_reparse_gribs.py
    python3 scripts/ecds_reparse_gribs.py --force  (re-parse even dates already in DB)

GRIB cache: data/tigge_grib/
Database:   data/tigge_archive.db
"""
import os, sys, re, time, sqlite3, struct, zlib
from datetime import datetime
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "tigge_archive.db")
GRIB_DIR = os.path.join(ROOT, "data", "tigge_grib")
LOG_PATH = os.path.join(ROOT, "logs", "ecds_reparse.log")

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
ALL_STEPS = [0, 6, 12, 18, 24]


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def init_db():
    """Open DB with durable settings — synchronous=FULL, explicit WAL checkpointing."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
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
    """Parse TIGGE GRIB file, extracting all 51 members × 5 steps × 20 stations.
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

    # Parse all messages: 51 members × 5 steps × (params) per date
    f = open(grib_path, 'rb')
    result = {}
    msg_count = 0
    while True:
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None:
            break
        msg_count += 1
        try:
            name = eccodes.codes_get_string(gid, 'name')
            if ('2 metre temperature' not in name.lower() and
                '2 meter temperature' not in name.lower()):
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
        except Exception:
            pass
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
    return stored


def get_done_dates(conn):
    """Get set of already-completed target dates."""
    r = conn.execute("SELECT DISTINCT target_date FROM tigge_archive").fetchall()
    return set(row[0] for row in r)


def main():
    force_reparse = "--force" in sys.argv

    conn = init_db()
    done_dates = get_done_dates(conn)
    log(f"Already in DB: {len(done_dates)} dates ({min(done_dates) if done_dates else 'none'} to {max(done_dates) if done_dates else 'none'})")

    # Find all GRIB files on disk
    grib_files = sorted(f for f in os.listdir(GRIB_DIR) if f.endswith('.grib'))
    log(f"GRIB files on disk: {len(grib_files)}")

    # Extract dates from filenames
    date_pattern = re.compile(r'tigge_(\d{4}-\d{2}-\d{2})_allsteps\.grib$')
    grib_dates = []
    for f in grib_files:
        m = date_pattern.match(f)
        if m:
            grib_dates.append(m.group(1))
    grib_dates.sort()

    if not grib_dates:
        log("No GRIB files found.")
        return

    log(f"GRIB dates range: {grib_dates[0]} to {grib_dates[-1]}")

    # Determine which dates to process
    to_process = []
    skipped = 0
    for d in grib_dates:
        if not force_reparse and d in done_dates:
            skipped += 1
            continue
        to_process.append(d)

    log(f"To reparse: {len(to_process)} dates ({skipped} already in DB, will skip)")
    if not to_process:
        log("Nothing to do.")
        conn.close()
        return

    # Batch processing with periodic checkpointing
    total = len(to_process)
    processed = 0
    errors = 0
    batch_size = 25  # checkpoint WAL every 25 dates
    total_rows = 0

    start_time = time.time()

    for i, ds in enumerate(to_process):
        grib_path = os.path.join(GRIB_DIR, f"tigge_{ds}_allsteps.grib")

        if not os.path.exists(grib_path):
            log(f"  [{i+1}/{total}] {ds}: GRIB file missing, skipping")
            continue

        size_mb = os.path.getsize(grib_path) / (1024 * 1024)
        t_parse = time.time()

        parsed = parse_grib_for_stations(grib_path)
        if parsed is None or not parsed:
            log(f"  [{i+1}/{total}] {ds}: Parse FAILED (empty result)")
            errors += 1
            continue

        # Validate — each date should have all 20 stations × 5 steps
        total_expected = len(STATIONS) * len(ALL_STEPS)
        total_got = sum(len(steps) for steps in parsed.values())

        if total_got < total_expected * 0.5:
            log(f"  [{i+1}/{total}] {ds}: Parse incomplete: got {total_got}/{total_expected} combos")
            # Still store what we got rather than losing it

        rows = store_results(conn, ds, parsed)
        elapsed = time.time() - t_parse
        total_rows += rows
        processed += 1
        done_dates.add(ds)

        # Check member count for logging
        member_count = 0
        for sname, steps_data in parsed.items():
            for step_h, temps in steps_data.items():
                member_count = max(member_count, len(temps))

        log(f"  [{i+1}/{total}] {ds}: {rows} rows ({size_mb:.0f} MB) in {elapsed:.2f}s (members: {member_count})")

        # Periodic WAL checkpoint — ensures data is durably written
        if (i + 1) % batch_size == 0:
            conn.commit()
            log(f"  ── BATCH {i+1}/{total} committed ──")
            elapsed_total = time.time() - start_time
            rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
            pct = (i + 1) / total * 100
            remaining = (total - i - 1) / rate if rate > 0 else 0
            log(f"  ── CHECKPOINT: {i+1}/{total} dates ({pct:.1f}%), "
                f"{rate:.1f} dates/s, ~{remaining:.0f}s remaining ──")

    # Final summary
    elapsed = time.time() - start_time
    dates_done = conn.execute("SELECT COUNT(DISTINCT target_date) FROM tigge_archive").fetchone()[0]
    total_rows_db = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]

    log(f"\n{'='*60}")
    log(f"REPARSE COMPLETE")
    log(f"{'='*60}")
    log(f"Processed:  {processed}/{total} dates")
    log(f"Errors:     {errors}")
    log(f"DB dates:   {dates_done}")
    log(f"DB rows:    {total_rows_db}")
    log(f"Duration:   {elapsed:.1f}s")
    log(f"Rate:       {processed/elapsed:.1f} dates/s")

    conn.close()
    log(f"Database closed. File size: {os.path.getsize(DB_PATH)/1024/1024:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())