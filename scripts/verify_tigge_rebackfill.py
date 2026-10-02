#!/usr/bin/env python3
"""Deterministic post-run verification for the TIGGE re-backfill.

Reads data/tigge_archive.db (read-only) and reports:
  - per-station row counts (before vs after, if a before-JSON is given)
  - the four former collision pairs' identical-fraction (target 0-1%)
  - KATL coverage (was 5 rows)

No AI calls. Pure SQLite. Safe to run at any time.

Usage:
    python3 scripts/verify_tigge_rebackfill.py [--before path/to/before_counts.json]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "tigge_archive.db"

PAIRS = [("KATL", "KPHX"), ("KAUS", "KMSY"), ("KDEN", "KPHL"), ("KHOU", "KSAT")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", help="JSON {station: count} captured pre-run")
    a = ap.parse_args()

    before = {}
    if a.before and Path(a.before).exists():
        before = json.load(open(a.before))

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = c.cursor()

    print("=== per-station row counts ===")
    rows = cur.execute(
        "SELECT station, COUNT(*) FROM tigge_archive GROUP BY station ORDER BY station"
    ).fetchall()
    total = 0
    for st, n in rows:
        total += n
        b = before.get(st)
        delta = "" if b is None else f"  (before {b}, Δ{n - b:+d})"
        print(f"  {st}: {n}{delta}")
    print(f"  TOTAL: {total} rows, "
          f"{cur.execute('SELECT COUNT(DISTINCT target_date) FROM tigge_archive').fetchone()[0]} dates")
    print(f"  date range: {cur.execute('SELECT MIN(target_date), MAX(target_date) FROM tigge_archive').fetchone()}")

    print("\n=== collision identical-fraction (target 0-1% overall) ===")
    for x, y in PAIRS:
        tot = cur.execute(
            "SELECT COUNT(*) FROM tigge_archive t1 JOIN tigge_archive t2 "
            "ON t1.target_date=t2.target_date AND t1.step=t2.step "
            "WHERE t1.station=? AND t2.station=?", (x, y)).fetchone()[0]
        same = cur.execute(
            "SELECT COUNT(*) FROM tigge_archive t1 JOIN tigge_archive t2 "
            "ON t1.target_date=t2.target_date AND t1.step=t2.step "
            "WHERE t1.station=? AND t2.station=? AND t1.ensemble_mean=t2.ensemble_mean",
            (x, y)).fetchone()[0]
        pct = 100.0 * same / tot if tot else float("nan")
        print(f"  {x}/{y}: {same}/{tot} = {pct:.1f}%")

    print("\n=== pre-2026 stale check (must be ~0%, was 100% for 2021-2025) ===")
    for x, y in PAIRS:
        rows2 = cur.execute(
            "SELECT substr(t1.target_date,1,4) yr, COUNT(*) tot, "
            "SUM(CASE WHEN t1.ensemble_mean=t2.ensemble_mean THEN 1 ELSE 0 END) same "
            "FROM tigge_archive t1 JOIN tigge_archive t2 "
            "ON t1.target_date=t2.target_date AND t1.step=t2.step "
            "WHERE t1.station=? AND t2.station=? AND t1.target_date < '2026-01-01' "
            "GROUP BY yr ORDER BY yr", (x, y)).fetchall()
        tot = sum(r[1] for r in rows2)
        same = sum(r[2] for r in rows2)
        pct = 100.0 * same / tot if tot else float("nan")
        print(f"  {x}/{y}: pre-2026 {same}/{tot} = {pct:.1f}%")

    c.close()


if __name__ == "__main__":
    main()
