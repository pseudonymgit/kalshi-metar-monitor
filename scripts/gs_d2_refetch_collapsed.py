#!/usr/bin/env python3
"""
GS-D2 / W1-A — Re-fetch collapsed gefs_archive rows using the CORRECT forecast step (f024).

Defect (GS-D2): fast_gefs_backfill.py downloaded f006 (overnight max) with a whole-°C
offset convention, producing collapsed/incorrect blobs. This re-downloads the day-1
DAILY MAX (f024) per member and rewrites blobs in the canonical 0.1°C offset convention.

DESIGN (W1-A fix): GRIB files are per (init_date, member) and cover ALL stations. So we
download each (date, member) GRIB ONCE and extract every station we need from it — NOT
once per station-day (that was a 3x redundancy bug).

Bounded + checkpointed. Dry-run by default. Only touches blob_ok=0 rows.

Usage:
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py                 # dry-run
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py --apply --limit 5
  .venv-gefs/bin/python scripts/gs_d2_refetch_collapsed.py --apply         # full
"""
import argparse
import json
import os
import sqlite3
import struct
import sys
import tempfile
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
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
WORKERS = 16
FORECAST_STEP = 24  # f024 = day-1 daily max. NEVER f006 (the original defect).


def extract_all_stations(path, stations):
    """Open one GRIB, return {station: tmax_C} for every requested station."""
    import xarray as xr
    ds = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"shortName": "tmax"})
    try:
        lats = ds.latitude.values
        lons = ds.longitude.values
        if np.any(lons > 180):
            lons = np.where(lons > 180, lons - 360, lons)
        arr = ds.tmax.values
        out = {}
        for code in stations:
            lat, lon = STATIONS[code]
            li = int(np.argmin(np.abs(lats - lat)))
            lo = int(np.argmin(np.abs(lons - lon)))
            v = float(arr[li, lo])
            if not (np.isnan(v) or np.isinf(v)):
                out[code] = v - 273.15
        return out
    finally:
        ds.close()


def fetch_member(init_compact, member, stations):
    """Download ONE (init, member) GRIB, return {station: tmax_C} or None."""
    url = f"{S3}/gefs.{init_compact}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f{FORECAST_STEP:03d}"
    tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
    tmp.close()
    try:
        urllib.request.urlretrieve(url, tmp.name)
        return extract_all_stations(tmp.name, stations)
    except Exception:
        return None
    finally:
        for p in (tmp.name, tmp.name + ".idx"):
            try:
                os.unlink(p)
            except OSError:
                pass


def process_date(target_date, stations):
    """Download all 31 members for one target date ONCE; return {station: [temps]}."""
    init = date.fromisoformat(target_date) - timedelta(days=1)
    init_c = f"{init:%Y%m%d}"
    member_temps = defaultdict(dict)  # station -> member -> temp
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_member, init_c, m, stations): m for m in MEMBERS}
        for f in as_completed(futs):
            res = f.result()
            if res:
                for st, t in res.items():
                    member_temps[st][futs[f]] = t
    return {st: list(md.values()) for st, md in member_temps.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="limit number of DATES")
    args = ap.parse_args()

    conn = sqlite3.connect(ARCHIVE)
    # date -> set of stations needing repair
    need = defaultdict(set)
    for d, s in conn.execute("SELECT target_date, station FROM gefs_archive WHERE step=24 AND blob_ok=0"):
        need[d].add(s)
    dates = sorted(need)
    if args.limit:
        dates = dates[:args.limit]
    total_sd = sum(len(need[d]) for d in dates)
    print(f"dates to process: {len(dates)} | station-days: {total_sd}")

    done = set()
    if os.path.exists(CKPT):
        done = set(json.load(open(CKPT)).get("done_dates", []))
    pending = [d for d in dates if d not in done]
    print(f"pending dates: {len(pending)} (done: {len(done)})")
    if not args.apply:
        print("DRY-RUN: no downloads.")
        return 0

    fixed_sd = failed_dates = partial_dates = 0
    stuck = set()
    for i, d in enumerate(pending):
        stations = sorted(need[d])
        per_st = process_date(d, stations)
        ok_this = 0
        for st, temps in per_st.items():
            if len(temps) < 3:
                continue
            mean = sum(temps) / len(temps)
            offs = [max(-128, min(127, int(round((t - mean) * 10)))) for t in temps]
            blob = struct.pack(f"{len(offs)}b", *offs)
            conn.execute(
                "UPDATE gefs_archive SET member_values=?, n_members=?, ensemble_mean=?, "
                "ensemble_min=?, ensemble_max=?, blob_ok=1, source='grib_backfill_f024' "
                "WHERE target_date=? AND station=? AND step=24",
                (blob, len(offs), round(mean, 2), round(min(temps), 2), round(max(temps), 2), d, st))
            ok_this += 1
        conn.commit()
        fixed_sd += ok_this
        # WAVE-C/11 (2026-09-28): a date is only DONE if every target row was
        # repaired. Previously every date was marked done even when 0 rows were
        # fixed, so the checkpoint lied and unrepaired rows were never retried.
        if ok_this >= len(stations):
            done.add(d)
        elif ok_this == 0:
            failed_dates += 1
            stuck.add(d)
        else:
            partial_dates += 1
            stuck.add(d)
        # Recompute blob_ok HONESTLY from the DB for the rows still failing:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM gefs_archive WHERE target_date=? AND step=24 AND blob_ok=0",
            (d,)).fetchone()[0]
        json.dump({"done_dates": sorted(done),
                   "still_degenerate_dates": sorted(stuck),
                   "blob_ok_remaining_for_date": remaining}, open(CKPT, "w"))
        if (i + 1) % 25 == 0 or (i + 1) == len(pending):
            print(f"  [{i+1}/{len(pending)}] {d}: fixed {ok_this}/{len(stations)} | total {fixed_sd}")

    total_stuck = conn.execute(
        "SELECT COUNT(*) FROM gefs_archive WHERE step=24 AND blob_ok=0").fetchone()[0]
    print(f"\nfixed station-days: {fixed_sd} | dates no-data: {failed_dates} | "
          f"partial: {partial_dates} | blob_ok=0 remaining (honest): {total_stuck}")
    return 0 if fixed_sd > 0 or not pending else 1


if __name__ == "__main__":
    sys.exit(main())