#!/usr/bin/env python3
"""
WS-S — Rebuild gefs_archive.db from the CLEAN raw source (single fetch cycle).

Uses the fixed populate.get_nwp_data() (latest fetch_date per station/variable)
to write one coherent ensemble per (target_date, station, step). Writes to a NEW
file first, verifies, then swaps. The old (corrupt) archive is preserved.

Usage: .venv-gefs/bin/python scripts/rebuild_gefs_archive.py
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NWP_DB = str(ROOT / "data" / "nwp_forecasts.db")
ARCHIVE = str(ROOT / "data" / "gefs_archive.db")
NEW = str(ROOT / "data" / "gefs_archive.rebuilt.db")

spec = importlib.util.spec_from_file_location("pop", str(ROOT / "scripts" / "populate_gefs_archive.py"))
pop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pop)

STEPS = [0, 3, 6, 9, 12, 15, 18, 21, 24]

def main():
    nwp = sqlite3.connect(NWP_DB)
    dates = sorted(r[0] for r in nwp.execute(
        "SELECT DISTINCT target_date FROM nwp_forecasts WHERE model='gefs_ens'"))

    if os.path.exists(NEW):
        os.remove(NEW)
    conn = sqlite3.connect(NEW)
    conn.execute("""CREATE TABLE gefs_archive (
        target_date TEXT NOT NULL, station TEXT NOT NULL, step INTEGER NOT NULL,
        ensemble_mean REAL, ensemble_min REAL, ensemble_max REAL,
        member_values BLOB, n_members INTEGER, fetch_timestamp TEXT,
        init_cycle TEXT DEFAULT '12Z',
        PRIMARY KEY (target_date, station, step))""")

    now = datetime.utcnow().isoformat()
    rows_written = 0
    bad = 0
    for i, dt in enumerate(dates):
        stations = pop.get_nwp_data(dt)
        for stn, vals in sorted(stations.items()):
            if len(vals) < 3:
                continue
            vals = [v for v in vals if v is not None]
            if len(vals) < 3:
                continue
            mean_c = sum(vals) / len(vals)
            mn_c, mx_c = min(vals), max(vals)
            # invariant: mean MUST lie within [min,max]
            assert mn_c <= mean_c <= mx_c, f"mean out of range {dt} {stn}"
            blob = pop.encode_blob(vals, mean_c)
            for step in STEPS:
                conn.execute(
                    "INSERT OR REPLACE INTO gefs_archive VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (dt, stn, step, round(mean_c, 2), round(mn_c, 2), round(mx_c, 2),
                     blob, len(vals), now, '12Z'))
                rows_written += 1
        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(dates)}] {dt} ({rows_written} rows)")
    conn.commit()

    # verify
    tot = conn.execute("SELECT COUNT(*) FROM gefs_archive").fetchone()[0]
    bad = conn.execute("""SELECT COUNT(*) FROM gefs_archive
        WHERE ensemble_mean < ensemble_min - 0.01 OR ensemble_mean > ensemble_max + 0.01""").fetchone()[0]
    nmax = conn.execute("SELECT MAX(n_members) FROM gefs_archive WHERE step=24").fetchone()[0]
    print(f"\nRebuilt: {tot} rows | mean-outside-[min,max]: {bad} | max n_members: {nmax}")
    print(f"dates: {len(dates)}")
    conn.close()

    if bad != 0:
        print("ABORT: rebuild still has invariant violations; not swapping.")
        return 1

    shutil.copy2(NEW, ARCHIVE)
    print(f"Swapped corrected archive into {ARCHIVE} (backup preserved).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
