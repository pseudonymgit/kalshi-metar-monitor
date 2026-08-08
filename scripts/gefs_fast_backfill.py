#!/usr/bin/env python3
"""
gefs_fast_backfill.py — Parallel GEFS 31-member backfill.

Downloads GRIB files to a work directory (auto-cleaned per date),
extracts TMAX for 20 stations, updates gefs_archive.db.

~3.5 hours for 1,732 dates with 8 workers.
"""

import json, os, sqlite3, struct, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
WORK = os.path.join(DATA, "gefs_work")  # Auto-cleaned per-date
S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
N_WORKERS = 16

STATIONS = {
    "KATL":(33.64,-84.43),"KAUS":(30.19,-97.67),"KBOS":(42.36,-71.01),
    "KDCA":(38.85,-77.04),"KDEN":(39.85,-104.65),"KDFW":(32.90,-97.03),
    "KHOU":(29.65,-95.28),"KLAX":(33.94,-118.41),"KLAS":(36.08,-115.15),
    "KMIA":(25.79,-80.29),"KMDW":(41.79,-87.75),"KMSP":(44.88,-93.22),
    "KMSY":(29.99,-90.25),"KNYC":(40.78,-73.97),"KOKC":(35.39,-97.60),
    "KORD":(41.98,-87.90),"KPHL":(39.87,-75.24),"KPHX":(33.43,-112.02),
    "KSAT":(29.53,-98.47),"KSEA":(47.45,-122.31),"KSFO":(37.62,-122.38),
}


def process_date(date_str):
    """Download 31 GEFS members for one date, return {member: {station: temp_c}}."""
    date_compact = date_str.replace("-", "")
    workdir = os.path.join(WORK, date_compact)
    os.makedirs(workdir, exist_ok=True)

    members = ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]

    def fetch_member(member):
        import xarray as xr, numpy as np
        url = f"{S3_BASE}/gefs.{date_compact}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f006"
        path = os.path.join(workdir, f"{member}.grib2")
        try:
            # Download
            req = urllib.request.Request(url, headers={"User-Agent": "gefs-backfill/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(path, "wb") as f:
                    f.write(resp.read())
            # Parse
            ds = xr.open_dataset(path, engine="cfgrib",
                filter_by_keys={"shortName": "tmax"},
                backend_kwargs={"errors": "ignore"})
            lats = ds.latitude.values
            lons = ds.longitude.values
            if np.any(lons > 180):
                lons = np.where(lons > 180, lons - 360, lons)
            result = {}
            for st, (lat, lon) in STATIONS.items():
                li = int(np.argmin(np.abs(lats - lat)))
                loi = int(np.argmin(np.abs(lons - lon)))
                val = float(ds.tmax.values[li, loi])
                result[st] = round(val - 273.15, 1)
            ds.close()
            return member, result
        except Exception:
            return member, None
        finally:
            # Clean up this member's file
            for ext in ["", ".idx", ".nx2", ".nx3"]:
                try:
                    os.unlink(path + ext)
                except:
                    pass

    member_results = {}
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = {ex.submit(fetch_member, m): m for m in members}
        for future in as_completed(futures):
            member, data = future.result()
            if data:
                member_results[member] = data

    # Clean up workdir
    try:
        os.rmdir(workdir)
    except:
        pass

    return member_results


def backfill():
    db = sqlite3.connect(os.path.join(DATA, "gefs_archive.db"))

    # Find dates needing backfill (n_members < 31, overlapping settlements)
    db.execute(f"ATTACH DATABASE '{os.path.join(DATA, 'kalshi_settlements.db')}' AS s")
    rows = db.execute("""
        SELECT DISTINCT g.target_date, g.station
        FROM gefs_archive g
        INNER JOIN s.kalshi_settlements s ON g.target_date = s.target_date AND g.station = s.station
        WHERE g.n_members < 31
        ORDER BY g.target_date
    """).fetchall()

    dates_needed = {}
    for date_str, station in rows:
        dates_needed.setdefault(date_str, []).append(station)

    print(f"Dates needing backfill: {len(dates_needed)}")
    print(f"Total date-station pairs: {len(rows)}")
    if not dates_needed:
        print("All done!")
        db.close()
        return

    ckpt_path = os.path.join(DATA, "gefs_backfill_ckpt.json")
    done = set()
    if os.path.exists(ckpt_path):
        with open(ckpt_path) as f:
            done = set(json.load(f).get("done_dates", []))
    pending = sorted(d for d in dates_needed if d not in done)
    print(f"Already done: {len(done)}, Pending: {len(pending)}")

    t0 = time.time()
    updated = 0
    failed = 0

    for i, date_str in enumerate(pending):
        t1 = time.time()
        member_results = process_date(date_str)
        stations = dates_needed[date_str]

        if not member_results or len(member_results) < 10:
            print(f"  [{i+1}/{len(pending)}] {date_str} FAILED ({len(member_results)} members)")
            failed += 1
            continue

        for station in stations:
            all_temps = []
            for m in ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]:
                if m in member_results and station in member_results[m]:
                    all_temps.append(member_results[m][station])
            if len(all_temps) < 31:
                continue
            mean = sum(all_temps) / 31
            offsets = struct.pack("31b", *[max(-128, min(127, int(round(t - mean)))) for t in all_temps])
            db.execute("""
                UPDATE gefs_archive SET member_values=?, n_members=31, ensemble_mean=?
                WHERE target_date=? AND station=?
            """, (offsets, round(mean, 2), date_str, station))
            updated += 1

        db.commit()
        done.add(date_str)
        with open(ckpt_path, "w") as f:
            json.dump({"done_dates": sorted(done)}, f)

        elapsed = time.time() - t1
        rate = (i + 1) / (time.time() - t0)
        remaining = len(pending) - (i + 1)
        eta = remaining / rate if rate > 0 else 0
        print(f"  [{i+1}/{len(pending)}] {date_str} OK ({elapsed:.0f}s, ETA {eta/60:.0f}m)")

    db.close()
    print(f"\nDone: {updated} rows, {failed} failed in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    print(f"GEFS Fast Backfill — {datetime.now(timezone.utc).isoformat()}")
    backfill()