#!/usr/bin/env python3
"""
GS-D2 — Re-fetch collapsed gefs_archive rows using the CORRECT forecast step (f024).

Defect: fast_gefs_backfill.py downloaded f006 (overnight max) with a whole-°C
offset convention, producing collapsed/incorrect blobs. This script re-downloads
the day-1 DAILY MAX (f024) per member and rewrites the blob in the canonical
0.1°C offset convention, recomputing mean/min/max.

Bounded + checkpointed. Dry-run by default. Only touches blob_ok=0 rows.

Usage:
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py --dry-run
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py --apply --limit 20
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py --apply   # full
"""
import argparse
import json
import os
import sqlite3
import struct
import sys
import tempfile
import urllib.request
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = str(ROOT / "data" / "gefs_archive.db")
CKPT = str(ROOT / "data" / "gs_d2_refetch_ckpt.json")
S3 = "https://noaa-gefs-pds.s3.amazonaws.com"
STATIONS = {
    "KATL": (33.64, -84.43), "KAUS": (30.19, -97.67), "KBOS": (42.36, -71.01),
    "KDCA": (38.85, -77.04), "KDEN": (39.85, -104.65), "KDFW": (32.90, -97.03),
    "KHOU": (29.65, -95.28), "KLAX": (33.94, -118.41), "KLAS": (36.08, -115.15),
    "KMIA": (25.79, -80.29), "KMDW": (41.79, -87.75), "KMSP": (44.88, -93.22),
    "KMSY": (29.99, -90.25), "KNYC": (40.78, -73.97), "KOKC": (35.39, -97.60),
    "KPHL": (39.87, -75.24), "KPHX": (33.43, -112.02), "KSAT": (29.53, -98.47),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.38),
}
MEMBERS = ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]
WORKERS = 8
# NOTE: f024 = day-1 daily max. Do NOT use f006 (the original defect).
FORECAST_STEP = 24


def extract_station_tmax(path, lat, lon):
    import xarray as xr
    ds = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"shortName": "tmax"})
    try:
        lats = ds.latitude.values
        lons = ds.longitude.values
        if np.any(lons > 180):
            lons = np.where(lons > 180, lons - 360, lons)
        arr = ds.tmax.values
        li = int(np.argmin(np.abs(lats - lat)))
        lo = int(np.argmin(np.abs(lons - lon)))
        v = float(arr[li, lo])
        return None if (np.isnan(v) or np.isinf(v)) else v - 273.15
    finally:
        ds.close()


def refetch_station(target_date, station):
    """Return list of 31 member TMAX (C) using f024, or None."""
    init = date.fromisoformat(target_date) - timedelta(days=1)
    lat, lon = STATIONS[station]
    temps = []
    for member in MEMBERS:
        url = f"{S3}/gefs.{init:%Y%m%d}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f{FORECAST_STEP:03d}"
        tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
        tmp.close()
        try:
            urllib.request.urlretrieve(url, tmp.name)
            v = extract_station_tmax(tmp.name, lat, lon)
            if v is not None:
                temps.append(v)
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp.name)
                os.unlink(tmp.name + ".idx")
            except OSError:
                pass
    return temps if len(temps) >= 3 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(ARCHIVE)
    rows = conn.execute(
        "SELECT DISTINCT target_date, station FROM gefs_archive "
        "WHERE step=24 AND blob_ok=0 ORDER BY target_date").fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"collapsed (blob_ok=0) station-days to refetch: {len(rows)}")

    done = set()
    if os.path.exists(CKPT):
        done = set(tuple(x) for x in json.load(open(CKPT)).get("done", []))
    pending = [r for r in rows if r not in done]
    print(f"pending: {len(pending)} (already done: {len(done)})")
    if not args.apply:
        print("DRY-RUN: no downloads.")
        return 0

    fixed = failed = 0
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(refetch_station, d, s): (d, s) for d, s in pending}
        for i, f in enumerate(as_completed(futs)):
            d, s = futs[f]
            temps = f.result()
            if not temps:
                failed += 1
                continue
            mean = sum(temps) / len(temps)
            offs = [max(-128, min(127, int(round((t - mean) * 10)))) for t in temps]
            offsets = struct.pack(f"{len(offs)}b", *offs)  # signed int8 (negatives OK)
            results[(d, s)] = (offsets, round(mean, 2), round(min(temps), 2), round(max(temps), 2))
            fixed += 1
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(pending)}] fixed={fixed} failed={failed}")

    for (d, s), (offsets, mean, mn, mx) in results.items():
        conn.execute(
            "UPDATE gefs_archive SET member_values=?, n_members=?, ensemble_mean=?, "
            "ensemble_min=?, ensemble_max=?, blob_ok=1, source='grib_backfill_f024' "
            "WHERE target_date=? AND station=? AND step=24",
            (offsets, len(offsets), mean, mn, mx, d, s))
        done.add((d, s))
    conn.commit()
    json.dump({"done": [list(x) for x in done]}, open(CKPT, "w"))
    print(f"\nrefetched+fixed: {fixed} | failed: {failed}")
    return 0 if fixed > 0 or not pending else 1


if __name__ == "__main__":
    sys.exit(main())
