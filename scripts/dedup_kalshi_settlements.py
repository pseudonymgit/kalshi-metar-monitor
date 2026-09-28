#!/usr/bin/env python3
"""
DEDUP — kalshi_settlements (Dan-approved, 2026-09-28).

Removes redundant (station, target_date) rows, then enforces UNIQUE(station,
target_date) so the accumulation cannot recur. Same class as GEFS accumulation:
writes APPENDING where they should UPSERT.

Keep-rule (most-informative wins):
  1. prefer the row with a non-NULL kalshi_temp (the real settled value)
  2. then the earliest id (stable / first-seen)

Backs up the DB first. Idempotent. Deterministic.

Run: .venv-gefs/bin/python scripts/dedup_kalshi_settlements.py [--apply]
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "kalshi_settlements.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually modify (default: dry-run)")
    a = ap.parse_args()

    if not DB.exists():
        print(f"DEDUP: SKIP — {DB} missing")
        return 0

    conn = sqlite3.connect(str(DB))
    total = conn.execute("SELECT COUNT(*) FROM kalshi_settlements").fetchone()[0]
    groups = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM kalshi_settlements GROUP BY station,target_date)").fetchone()[0]
    redundant = total - groups

    # choose the survivor id per (station,target_date)
    survivors = conn.execute("""
        SELECT id FROM kalshi_settlements k
        WHERE id = (
            SELECT k2.id FROM kalshi_settlements k2
            WHERE k2.station=k.station AND k2.target_date=k.target_date
            ORDER BY (CASE WHEN k2.kalshi_temp IS NOT NULL THEN 0 ELSE 1 END), k2.id
            LIMIT 1
        )
    """).fetchall()
    keep_ids = {r[0] for r in survivors}
    print(f"DEDUP: total={total} groups={groups} redundant={redundant} keep={len(keep_ids)}")

    has_unique = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='kalshi_settlements' "
        "AND name='uq_kalshi_settlements_station_date'").fetchone()[0]

    if not a.apply:
        print("DEDUP: dry-run — pass --apply to modify")
        conn.close()
        return 0

    # backup
    bkp = DB.with_name(f"{DB.name}.dedup-backup-{time.strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(DB, bkp)
    print(f"DEDUP: backup -> {bkp}")

    # delete non-survivors
    all_ids = [r[0] for r in conn.execute("SELECT id FROM kalshi_settlements").fetchall()]
    to_del = [i for i in all_ids if i not in keep_ids]
    print(f"DEDUP: deleting {len(to_del)} rows")
    conn.executemany("DELETE FROM kalshi_settlements WHERE id=?", [(i,) for i in to_del])

    # enforce uniqueness
    if not has_unique:
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_kalshi_settlements_station_date "
                         "ON kalshi_settlements(station, target_date)")
            print("DEDUP: created UNIQUE INDEX uq_kalshi_settlements_station_date")
        except Exception as e:
            print(f"DEDUP: WARN could not create unique index: {e}")
    conn.commit()

    after_total = conn.execute("SELECT COUNT(*) FROM kalshi_settlements").fetchone()[0]
    after_groups = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM kalshi_settlements GROUP BY station,target_date)").fetchone()[0]
    conn.close()
    print(f"DEDUP: DONE total {total} -> {after_total} | groups {groups} -> {after_groups} | duplicates remaining={after_total-after_groups}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
