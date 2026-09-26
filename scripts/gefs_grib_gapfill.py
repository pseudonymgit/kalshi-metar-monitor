#!/usr/bin/env python3
"""Bounded GEFS GRIB gap-fill for an explicit target-date list.

Purpose: fill specific historical gaps in the GEFS ensemble record using
NOAA's public S3 GRIB archive (noaa-gefs-pds), the same transform-only route
that gefs_reforecast_backfill.py uses for deep history.

Design (deliberately minimal, unlike the full backfill):
  * One init day -> one forecast step (f024) yields exactly the day-1 TMAX for
    the target date. init D + f024 -> target D+1 (validated against Open-Meteo).
  * 31 members: gec00 (control) + gep01..gep30, matching the DB variable
    convention `temperature_2m_max_membergecNN`.
  * Download each (init, member) file ONCE, extract all 20 stations, store to
    nwp_forecasts.db (model='gefs_ens'), then DELETE the GRIB immediately.
  * fetch_date is recorded as the init date (honest provenance).
  * No AI/ML calls. Requires xarray+cfgrib+eccodes (run under .venv-gefs).

Usage:
    .venv-gefs/bin/python scripts/gefs_grib_gapfill.py --targets 2026-09-17 2026-09-18
    .venv-gefs/bin/python scripts/gefs_grib_gapfill.py --targets 2026-09-20 --dry-run
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import xarray as xr  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
NWP_DB = PROJECT_ROOT / "data" / "nwp_forecasts.db"
WORK_DIR = PROJECT_ROOT / "data" / "gefs_work"

S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
MODEL_NAME = "gefs_ens"
MEMBERS = ["gec00"] + [f"gep{d:02d}" for d in range(1, 31)]
STEP = 24  # f024 = day-1 max
HTTP_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_DELAY = 5.0

# 20 Kalshi stations (matches scripts/gefs_reforecast_backfill.py STATIONS)
STATIONS = [
    ("KATL", 33.6407, -84.4277), ("KAUS", 30.1945, -97.6699),
    ("KBOS", 42.3656, -71.0096), ("KDCA", 38.8512, -77.0402),
    ("KDEN", 39.8561, -104.6737), ("KDFW", 32.8998, -97.0403),
    ("KHOU", 29.6454, -95.2789), ("KLAS", 36.0840, -115.1537),
    ("KLAX", 33.9425, -118.4081), ("KMDW", 41.7868, -87.7522),
    ("KMIA", 25.7959, -80.2870), ("KMSP", 44.8848, -93.2223),
    ("KMSY", 29.9934, -90.2580), ("KNYC", 40.6413, -73.7781),
    ("KOKC", 35.3931, -97.6007), ("KPHL", 39.8744, -75.2424),
    ("KPHX", 33.4342, -112.0116), ("KSAT", 29.5337, -98.4698),
    ("KSEA", 47.4502, -122.3088), ("KSFO", 37.6213, -122.3790),
]


def key_for(init_date: date, member: str, step: int) -> str:
    ds = init_date.strftime("%Y%m%d")
    return f"gefs.{ds}/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f{step:03d}"


def download(url: str, dst: Path) -> int:
    """Stream a URL to dst with retries. Returns bytes written."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url)
            n = 0
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(dst, "wb") as f:
                while True:
                    chunk = resp.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
            return n
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


def extract_stations(path: Path) -> str:
    """Open one GRIB, return (valid_date, {station: tmax_c}) for all stations."""
    ds = xr.open_dataset(path, engine="cfgrib", filter_by_keys={"shortName": "tmax"})
    try:
        lats = ds.latitude.values
        lons = ds.longitude.values
        if np.any(lons > 180):
            lons = np.where(lons > 180, lons - 360, lons)
        valid = str(ds.valid_time.values)[:10]
        arr = ds.tmax.values  # (lat, lon)
        out = {}
        for code, lat, lon in STATIONS:
            li = int(np.argmin(np.abs(lats - lat)))
            lo = int(np.argmin(np.abs(lons - lon)))
            v = float(arr[li, lo])
            if not (np.isnan(v) or np.isinf(v)):
                out[code] = v - 273.15
        return valid, out
    finally:
        ds.close()


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nwp_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL, target_date TEXT NOT NULL,
            station TEXT NOT NULL, model TEXT NOT NULL, variable TEXT NOT NULL,
            value REAL, fetch_timestamp TEXT NOT NULL,
            UNIQUE(fetch_date, target_date, station, model, variable)
        )
    """)


def main():
    ap = argparse.ArgumentParser(description="Bounded GEFS GRIB gap-fill")
    ap.add_argument("--targets", nargs="+", required=True, help="target dates YYYY-MM-DD")
    ap.add_argument("--db-path", default=str(NWP_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = sorted(date.fromisoformat(t) for t in args.targets)
    now_ts = datetime.now(timezone.utc).isoformat()

    print(f"GEFS GRIB gap-fill — {now_ts}")
    print(f"Targets: {[t.isoformat() for t in targets]}")
    print(f"Members: {len(MEMBERS)} | step: f{STEP:03d} | stations: {len(STATIONS)}")
    print(f"DB: {args.db_path}")

    plans = []
    for t in targets:
        init = t - timedelta(days=1)
        plans.append((t, init, key_for(init, "gec00", STEP)))
    for t, init, k in plans:
        print(f"  target {t.isoformat()} <- init {init.isoformat()} ({k})")

    if args.dry_run:
        print("DRY RUN — no downloads, no writes.")
        return 0

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # Record pre-existing state of the work dir for deletion proof.
    pre = sorted(p.name for p in WORK_DIR.iterdir())
    print(f"\nWork dir before: {WORK_DIR} ({len(pre)} entries)")

    conn = sqlite3.connect(args.db_path)
    init_db(conn)

    total_bytes = 0
    total_rows = 0
    downloaded = 0
    deleted = 0
    failures = []
    per_target = {}

    for target, init, _ in plans:
        tgt_ok = 0
        for member in MEMBERS:
            key = key_for(init, member, STEP)
            url = f"{S3_BASE}/{key}"
            dst = WORK_DIR / f"{init.strftime('%Y%m%d')}_{member}_f{STEP:03d}.grib2"
            try:
                nb = download(url, dst)
                total_bytes += nb
                downloaded += 1
                valid, vals = extract_stations(dst)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{target.isoformat()}/{member}: {e}")
                continue
            finally:
                # ALWAYS delete the GRIB (and any cfgrib .idx) after parse.
                for p in (dst, Path(str(dst) + ".idx")):
                    try:
                        if p.exists():
                            p.unlink()
                            deleted += 1
                    except Exception:
                        pass

            if valid != target.isoformat():
                failures.append(f"{target.isoformat()}/{member}: valid_time={valid} mismatch")
                continue

            var = f"temperature_2m_max_member{member}"
            for code, v in vals.items():
                conn.execute(
                    """INSERT OR REPLACE INTO nwp_forecasts
                       (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (init.isoformat(), target.isoformat(), code, MODEL_NAME, var, v, now_ts),
                )
                total_rows += 1
                tgt_ok += 1
        conn.commit()
        per_target[target.isoformat()] = tgt_ok
        print(f"  {target.isoformat()}: {tgt_ok} rows stored")

    conn.close()

    # Final sweep: remove anything left in the work dir created by this run.
    leftovers = sorted(p.name for p in WORK_DIR.iterdir())
    for p in WORK_DIR.iterdir():
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        except Exception:
            pass
    post = sorted(p.name for p in WORK_DIR.iterdir())

    print("\n=== GAP-FILL COMPLETE ===")
    print(f"GRIB files downloaded: {downloaded} ({total_bytes/1e9:.2f} GB)")
    print(f"GRIB files deleted:    {deleted}")
    print(f"Rows stored:           {total_rows}")
    print(f"Work dir before: {len(pre)} entries; leftovers after run: {leftovers}")
    print(f"Work dir after:  {len(post)} entries -> {post}")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures[:20]:
            print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())