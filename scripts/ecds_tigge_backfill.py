#!/usr/bin/env python3
"""
ECDS TIGGE Backfill: 1 day at a time with proper parsing.
Resumes from checkpoint. Handles rate limits.
"""
import os, sys, json, time, sqlite3, struct, zlib
from datetime import datetime, date, timedelta
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ECDS_URL = "https://ecds.ecmwf.int/api"
_ECDS_KEY = "e543bdb6-c396-48b4-bc1c-bebe08db954e"
DB_PATH = os.path.join(ROOT, "data", "tigge_archive.db")
GRIB_DIR = os.path.join(ROOT, "data", "tigge_grib")
CKPT = os.path.join(ROOT, "data", "ecds_backfill_checkpoint.json")

STATIONS = [
    ("KNYC",40.71,-74.01), ("KLAX",33.94,-118.41), ("KATL",33.64,-84.43),
    ("KBOS",42.37,-71.01), ("KDEN",39.86,-104.67), ("KDFW",32.90,-97.04),
    ("KHOU",29.65,-95.28), ("KLAS",36.08,-115.15), ("KMDW",41.79,-87.75),
    ("KMIA",25.80,-80.29), ("KMSP",44.88,-93.22), ("KMSY",29.99,-90.26),
    ("KOKC",35.39,-97.60), ("KPHL",39.87,-75.24), ("KPHX",33.45,-112.08),
    ("KSAT",29.42,-98.49), ("KSEA",47.45,-122.31), ("KSFO",37.62,-122.37),
    ("KDCA",38.85,-77.04), ("KAUS",30.19,-97.67),
]
STEPS = [0, 3, 6, 9, 12, 15, 18, 21, 24]

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
    raw = bytearray()
    for t in temps:
        diff = int(round((t - mean_c) * 10))
        raw.extend(struct.pack("<h", diff))
    return zlib.compress(bytes(raw), 1)

def parse_grib_for_stations(grib_path):
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
    
    # Re-open and parse all messages
    f = open(grib_path, 'rb')
    result = {}  # station -> {step_h: [temps]}
    while True:
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None: break
        try:
            name = eccodes.codes_get_string(gid, 'name')
            if '2 metre temperature' not in name:
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
        except: pass
        eccodes.codes_release(gid)
    f.close()
    return result

def main():
    import cdsapi
    
    start = sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 15
    
    # Load checkpoint
    checkpoint = {"start_date": start, "end_date": end, "last_date": None, "step": 0}
    if os.path.exists(CKPT):
        try:
            checkpoint = json.load(open(CKPT))
            if checkpoint.get("status") == "complete":
                print(f"Backfill already complete ({checkpoint['status']})")
                return
            start = checkpoint.get("last_date", start)
        except: pass
    
    conn = init_db()
    done_dates = set(r[0] for r in conn.execute("SELECT DISTINCT target_date FROM tigge_archive").fetchall())
    
    s = [int(x) for x in start.split("-")]
    e = [int(x) for x in end.split("-")]
    sd = date(s[0], s[1], s[2])
    ed = date(e[0], e[1], e[2])
    total = (ed - sd).days + 1
    
    c = cdsapi.Client(url=_ECDS_URL, key=_ECDS_KEY)
    processed = 0
    
    for i in range(total):
        target = sd + timedelta(days=i)
        ds = target.isoformat()
        if ds in done_dates:
            continue
        
        # Request 1 day x 1 step (f024 only for now)
        for step_h in [24]:
            grib = os.path.join(GRIB_DIR, f"tigge_{ds}_{step_h}h.grib")
            if os.path.exists(grib) and os.path.getsize(grib) > 1000:
                continue
            
            print(f"[{i+1}/{total}] {ds} step={step_h}... ", end="", flush=True)
            req = {
                "class": "ti", "expver": "prod", "dataset": "tigge-forecasts",
                "date": ds, "grid": "0.5/0.5", "levtype": "sfc",
                "origin": "ecmf", "param": "167", "step": str(step_h),
                "time": "00:00:00", "type": "pf", "number": "1/to/50",
            }
            try:
                c.retrieve("tigge-forecasts", req, grib)
                sz = os.path.getsize(grib)
                print(f"OK ({sz/1024/1024:.1f} MB)")
                processed += 1
            except Exception as e:
                err = str(e)
                print(f"FAIL: {err[:60]}")
                if "rejected" in err or "limited" in err:
                    json.dump({"start_date": start, "end_date": end,
                              "last_date": ds, "step": step_h, "status": "rate_limited"},
                             open(CKPT, "w"))
                    print(f"Rate limited. Resume from {ds}")
                    conn.close()
                    return
                elif "404" in err:
                    print("Dataset not found.")
                    conn.close()
                    return
        
        # Parse GRIB
        for step_h in [24]:
            grib = os.path.join(GRIB_DIR, f"tigge_{ds}_{step_h}h.grib")
            if not os.path.exists(grib) or os.path.getsize(grib) < 1000:
                continue
            
            parsed = parse_grib_for_stations(grib)
            if parsed is None:
                continue
            
            for sname in [s[0] for s in STATIONS]:
                if sname not in parsed or step_h not in parsed[sname]:
                    continue
                temps = parsed[sname][step_h]
                if not temps:
                    continue
                mn = round(sum(temps)/len(temps), 2)
                mi = round(min(temps), 2)
                mx = round(max(temps), 2)
                blob = encode_member_temps(temps, mn)
                conn.execute("""INSERT OR REPLACE INTO tigge_archive
                    (target_date, station, step, source, ensemble_mean, ensemble_min, ensemble_max, member_values, n_members)
                    VALUES (?,?,?,'tigge_ecmwf',?,?,?,?,?)""",
                    (ds, sname, step_h, mn, mi, mx, blob, len(temps)))
            conn.commit()
        
        # Checkpoint every 10 days
        if processed > 0 and processed % 10 == 0:
            json.dump({"start_date": start, "end_date": end,
                      "last_date": ds, "step": 24, "status": "in_progress"},
                     open(CKPT, "w"))
            print(f"  Checkpoint: {ds} ({processed} days)")
        
        time.sleep(delay)
    
    json.dump({"start_date": start, "end_date": end,
              "last_date": None, "step": 24, "status": "complete"},
             open(CKPT, "w"))
    
    rows = conn.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    print(f"Complete: {rows} rows across {len(STATIONS)} stations")
    conn.close()

if __name__ == "__main__":
    main()