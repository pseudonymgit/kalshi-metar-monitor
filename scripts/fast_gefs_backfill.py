#!/usr/bin/env python3
"""
fast_gefs_backfill.py — Parallel GEFS 31-member backfill for settlement dates.

Strategy:
- Parallel member downloads (8 concurrent HTTP)
- Process all 20 stations from one GRIB file
- Resume from checkpoint
- Only backfill dates with < 31 members that overlap settlements

Estimated: ~25 minutes with 8 workers
"""

import json, os, sqlite3, struct, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
N_WORKERS = 8

# 20 Kalshi stations with lat/lon
STATIONS = {
    "KATL":(33.64,-84.43),"KAUS":(30.19,-97.67),"KBOS":(42.36,-71.01),
    "KDCA":(38.85,-77.04),"KDEN":(39.85,-104.65),"KDFW":(32.90,-97.03),
    "KHOU":(29.65,-95.28),"KLAX":(33.94,-118.41),"KLAS":(36.08,-115.15),
    "KMIA":(25.79,-80.29),"KMDW":(41.79,-87.75),"KMSP":(44.88,-93.22),
    "KMSY":(29.99,-90.25),"KNYC":(40.78,-73.97),"KOKC":(35.39,-97.60),
    "KORD":(41.98,-87.90),"KPHL":(39.87,-75.24),"KPHX":(33.43,-112.02),
    "KSAT":(29.53,-98.47),"KSEA":(47.45,-122.31),"KSFO":(37.62,-122.38),
}

# GRIB resolution: 0.25° → lat/lon to grid index
def latlon_to_grid(lat, lon):
    """Convert lat/lon to 0.25° GRIB grid index (Gaussian grid)."""
    # GEFS 0.25° grid: lat 90 to -90, lon 0 to 360
    ni = 1440  # 360 / 0.25
    nj = 721   # 180 / 0.25 + 1
    i = int((lon + 180) / 0.25) % ni
    j = int((90 - lat) / 0.25)
    return i, j

# Pre-compute grid indices for all stations
STATION_GRID = {s: latlon_to_grid(lat, lon) for s, (lat, lon) in STATIONS.items()}


def download_grib(url):
    """Download a GRIB file to a temp file (auto-deleted after parsing)."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
    req = urllib.request.Request(url, headers={"User-Agent": "fast-backfill/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            tmp.write(chunk)
    tmp.close()
    return tmp.name


def extract_tmax_from_grib(filepath, station_lat, station_lon):
    """Extract TMAX for a specific station from a single GRIB file, then delete the file."""
    import xarray as xr
    import numpy as np
    import os as _os
    try:
        ds = xr.open_dataset(filepath, engine="cfgrib",
            filter_by_keys={"shortName": "tmax"})
        lats = ds.latitude.values
        lons = ds.longitude.values
        if np.any(lons > 180):
            lons = np.where(lons > 180, lons - 360, lons)
        lat_idx = int(np.argmin(np.abs(lats - station_lat)))
        lon_idx = int(np.argmin(np.abs(lons - station_lon)))
        tmax_k = float(ds.tmax.values[lat_idx, lon_idx])
        ds.close()
        return round(tmax_k - 273.15, 1)
    finally:
        # Delete temp file + any cfgrib index files
        for suffix in ["", ".idx", ".nx2", ".nx3"]:
            try:
                _os.unlink(filepath + suffix)
            except:
                pass


def process_date(date_str, stations_dict):
    """Download all 31 members for one date, extract TMAX for all stations."""
    date_compact = date_str.replace("-", "")
    
    members = ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]
    member_results = {}
    
    def download_member(member):
        url = f"{S3_BASE}/gefs.{date_compact}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f006"
        try:
            filepath = download_grib(url)
            result = {}
            for station, (lat, lon) in stations_dict.items():
                tmax = extract_tmax_from_grib(filepath, lat, lon)
                if tmax is not None:
                    result[station] = tmax
            return member, result
        except Exception:
            return member, None
    
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = {ex.submit(download_member, m): m for m in members}
        for future in as_completed(futures):
            member, temps = future.result()
            if temps:
                member_results[member] = temps
    
    return member_results



def backfill():
    """Main backfill — only for dates with < 31 GEFS members that overlap settlements."""
    db = sqlite3.connect(os.path.join(DATA, "gefs_archive.db"))
    db.execute("ATTACH DATABASE ? AS s", (os.path.join(DATA, "kalshi_settlements.db"),))
    
    # Find (date, station) pairs needing backfill
    rows = db.execute("""
        SELECT DISTINCT g.target_date, g.station
        FROM gefs_archive g
        INNER JOIN s.kalshi_settlements s ON g.target_date = s.target_date AND g.station = s.station
        WHERE g.n_members < 31
        ORDER BY g.target_date
    """).fetchall()
    
    # Group by date
    dates_needed = {}
    for date_str, station in rows:
        if date_str not in dates_needed:
            dates_needed[date_str] = []
        dates_needed[date_str].append(station)
    
    print(f"Dates needing backfill: {len(dates_needed)}")
    print(f"Total date-station pairs: {len(rows)}")
    
    if not dates_needed:
        print("No backfill needed!")
        return
    
    # Checkpoint
    ckpt_path = os.path.join(DATA, "gefs_backfill_ckpt.json")
    if os.path.exists(ckpt_path):
        with open(ckpt_path) as f:
            done = set(json.load(f).get("done_dates", []))
    else:
        done = set()
    
    pending = sorted(d for d in dates_needed if d not in done)
    print(f"Already done: {len(done)} dates")
    print(f"Pending: {len(pending)} dates")
    
    grib_cache = os.path.join(DATA, "gefs_grib_cache")
    t0 = time.time()
    updated = 0
    failed = 0
    
    for i, date_str in enumerate(pending):
        print(f"  [{i+1}/{len(pending)}] {date_str}...", end=" ", flush=True)
        t1 = time.time()
        
        member_results = process_date(date_str, STATIONS)
        
        if not member_results:
            print(f"FAILED (no members)")
            failed += 1
            continue
        
        # For each station on this date, compute offsets and update
        stations_for_date = dates_needed[date_str]
        for station in stations_for_date:
            all_temps = []
            for member in ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]:
                if member in member_results and station in member_results[member]:
                    all_temps.append(member_results[member][station])
            
            if len(all_temps) < 31:
                continue
            
            # Compute mean and offsets
            mean = sum(all_temps) / 31
            offsets = struct.pack(f"31b", *[max(-128, min(127, int(round(t - mean)))) for t in all_temps])
            
            db.execute("""
                UPDATE gefs_archive 
                SET member_values = ?, n_members = 31, ensemble_mean = ?
                WHERE target_date = ? AND station = ?
            """, (offsets, round(mean, 2), date_str, station))
            updated += 1
        
        db.commit()
        
        # Update checkpoint
        done.add(date_str)
        with open(ckpt_path, "w") as f:
            json.dump({"done_dates": sorted(done)}, f)
        
        elapsed = time.time() - t1
        rate = (i + 1) / (time.time() - t0)
        remaining = len(pending) - (i + 1)
        eta = remaining / rate if rate > 0 else 0
        print(f"OK ({elapsed:.0f}s, {rate:.1f}/s, ETA {eta/60:.0f}m)")
    
    db.close()
    print(f"\nDone: {updated} rows updated, {failed} dates failed in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    print(f"Fast GEFS Backfill — {datetime.now(timezone.utc).isoformat()}")
    print(f"Workers: {N_WORKERS}, GRIB cache: data/gefs_grib_cache/")
    backfill()