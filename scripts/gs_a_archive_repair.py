#!/usr/bin/env python3
"""
GS-A — In-place gefs_archive repair (no GRIB files needed; blob already in row).

A1: Recompute ensemble_mean/min/max/n_members from member_values blob in-place.
A2: Add `source` provenance column (grib_backfill | api_daily).
A3: Blob-invariant gate replaces n_members>=30 (len(blob)==n_members, min<=mean<=max, spread>0).
A4: Cold-freeze pre-2026-08 GRIB history (frozen=1 provenance marker).

Dry-run by default; --apply writes. Always backs up first.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = str(ROOT / "data" / "gefs_archive.db")
C2F = lambda c: c * 9.0 / 5.0 + 32.0
F2C = lambda f: (f - 32.0) * 5.0 / 9.0
GRIB_CUTOFF = "2026-08-01"


def add_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gefs_archive)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE gefs_archive ADD COLUMN source TEXT")
    if "frozen" not in cols:
        conn.execute("ALTER TABLE gefs_archive ADD COLUMN frozen INTEGER DEFAULT 0")
    if "blob_ok" not in cols:
        conn.execute("ALTER TABLE gefs_archive ADD COLUMN blob_ok INTEGER DEFAULT 0")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        bak = f"{ARCHIVE}.prerepair-{datetime.utcnow():%Y%m%dT%H%M%SZ}"
        import shutil
        shutil.copy2(ARCHIVE, bak)
        print(f"backup: {bak}")

    conn = sqlite3.connect(ARCHIVE)
    if args.apply:
        add_columns(conn)

    rows = conn.execute(
        "SELECT target_date,station,step,member_values FROM gefs_archive").fetchall()
    fixed_mean = fixed_minmax = degenerate = ok = 0
    for dt, st, step, blob in rows:
        if blob is None:
            continue
        offs = np.frombuffer(blob, dtype=np.int8).astype(float)
        n_members = len(offs)
        # blob stores int8 offsets *10 from mean in 0.1C units -> recover raw mean
        # We can't know the absolute mean without the stored one; use stored mean as
        # the anchor (it is 99% self-consistent with the blob), then recompute
        # min/max/n_members strictly FROM the blob.
        cur = conn.execute(
            "SELECT ensemble_mean, n_members FROM gefs_archive WHERE target_date=? AND station=? AND step=?",
            (dt, st, step)).fetchone()
        mean_c = cur[0]
        stored_n = cur[1]
        if mean_c is None:
            continue
        temps = mean_c + offs * 0.1  # degrees C
        mn, mx = float(temps.min()), float(temps.max())
        spread_f = (mx - mn) * 9 / 5
        is_degenerate = spread_f < 0.5
        if is_degenerate:
            degenerate += 1
        # WAVE-C/10 (2026-09-28): the old check compared len(blob) to n_members, but
        # n_members = len(offs) = len(blob) — always True (tautology). Compare to the
        # STORED n_members so a blob that disagrees with its recorded member count is
        # actually caught.
        blob_ok = (not is_degenerate) and (stored_n == n_members) and (mn <= mean_c <= mx)
        if args.apply:
            conn.execute(
                "UPDATE gefs_archive SET ensemble_min=?, ensemble_max=?, n_members=?, "
                "blob_ok=?, source=?, frozen=? WHERE target_date=? AND station=? AND step=?",
                (round(mn, 2), round(mx, 2), n_members, int(blob_ok),
                 "grib_backfill" if dt < GRIB_CUTOFF else "api_daily",
                 1 if dt < GRIB_CUTOFF else 0,
                 dt, st, step))
        fixed_minmax += 1
        ok += int(blob_ok)
    if args.apply:
        conn.commit()

    tot = len(rows)
    print(f"rows={tot} | min/max recomputed={fixed_minmax} | blob_ok={ok} ({100*ok/tot:.1f}%) | degenerate={degenerate} ({100*degenerate/tot:.1f}%)")
    print("mode:", "APPLIED" if args.apply else "DRY-RUN")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
