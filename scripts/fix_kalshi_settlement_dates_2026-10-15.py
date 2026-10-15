#!/usr/bin/env python3
"""fix_kalshi_settlement_dates_2026-10-15.py — repair the 623-row date-offset
defect in data/kalshi_settlements.db (Dan-approved fix, 2026-10-15).

DEFECT 1 — date offset (623 rows)
---------------------------------
The Kalshi truth-switch analysis (`scripts/kalshi_truth_switch_2026-10-15.py`,
commit 1da1e4ea0) found that in `data/kalshi_settlements.db` **623 rows** carry a
`target_date` one day LATER than the contract's own `-YYMMMDD` ticker date. All
623 are `source_type='historical_api'` (the Jan–May 2026 backfill). Joined on
`target_date`, those rows inject ~7°F of error and ~6.5% label flips versus a
clean truth source (GHCN/METAR). Keyed by the ticker's own date they agree with
GHCN to ~0.05°F.

DEFECT 2 — stale unique index (blocks the repair, and is itself wrong)
---------------------------------------------------------------------
`uq_kalshi_settlements_station_date` is UNIQUE(station, target_date). It was
created by `scripts/dedup_kalshi_settlements.py` on 2026-09-28 when the table
held HIGH series only. The LOW series (`KXLOWT…`, 787 rows) arrived later, so
station+date is no longer a valid key: shifting a mis-dated HIGH row back one
day can land on the same (station, date) as a LOW row. There are exactly 3 such
legitimate HIGH/LOW pairs (KLAX/KNYC 2026-01-13, KSEA 2026-04-16). The correct
key is (station, **series**, target_date), which has zero collisions both before
and after the repair. This script drops the stale index and rebuilds it on the
correct key, then applies the date fix — all in one transaction.

REPAIR
------
1. `target_date` := the ticker's own `-YYMMMDD` date, for every mismatched row.
2. drop `uq_kalshi_settlements_station_date` (station, target_date);
   create `uq_kalshi_settlements_station_series_date` (station, series, target_date).
Idempotent: a second run finds 0 rows to fix and leaves the index as-is.

SAFETY
------
* Private backup beside the DB before any write (md5 logged).
* Single transaction; rollback on any error.
* Verifies afterwards: 0 rows remain where ticker-date != target_date.
* `PRAGMA integrity_check` + `PRAGMA foreign_key_check`.
* Emits a JSON artifact for the record.

usage: python3 scripts/fix_kalshi_settlement_dates_2026-10-15.py [--apply]
(default is a dry run that reports what WOULD change)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_settlements.db"
OUT = REPO / "attention" / "kalshi_datefix_2026-10-15.json"

OLD_INDEX = "uq_kalshi_settlements_station_date"
NEW_INDEX = "uq_kalshi_settlements_station_series_date"

_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
        "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")


def ticker_date(event_ticker: str):
    """Parse the contract's own target day from `...-YYMMMDD`. None if absent."""
    m = _TICKER_RE.search(event_ticker or "")
    if not m:
        return None
    yy, mo, dd = m.groups()
    return f"20{int(yy):02d}-{_MON[mo]:02d}-{int(dd):02d}"


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the fix (default: dry run)")
    a = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: {DB} missing", file=sys.stderr)
        return 2

    before_md5 = md5(DB)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")

    try:
        rows = list(conn.execute(
            "SELECT id, station, series, event_ticker, target_date, kalshi_temp, "
            "source_type FROM kalshi_settlements"))
        n_total = len(rows)

        to_fix = []
        by_src = {}
        for r in rows:
            tdt = ticker_date(r["event_ticker"])
            if tdt is None:
                continue
            if tdt != r["target_date"]:
                to_fix.append((r["id"], tdt))
                by_src[r["source_type"]] = by_src.get(r["source_type"], 0) + 1

        idx_rows = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='kalshi_settlements'")}
        old_sql = idx_rows.get(OLD_INDEX)
        has_new = NEW_INDEX in idx_rows

        print(f"[kdf] rows={n_total} need_fix={len(to_fix)} "
              f"by_source_type={by_src}")
        print(f"[kdf] index {OLD_INDEX!r}: {'present' if old_sql else 'absent'}; "
              f"new_index_present={has_new}")

        backup = None
        if a.apply:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = DB.parent / f"_backup_pre_datefix_{ts}.db"
            conn.commit()          # flush read txn before copying the file
            conn.execute("BEGIN")
            shutil.copy2(DB, backup)
            print(f"[kdf] backup -> {backup} (md5={md5(backup)})")

            # 1) drop the stale narrow index (it blocks the legitimate fix)
            if old_sql:
                conn.execute(f"DROP INDEX {OLD_INDEX}")
                print(f"[kdf] dropped stale index {OLD_INDEX}")

            # 2) apply the date correction
            if to_fix:
                conn.executemany(
                    "UPDATE kalshi_settlements SET target_date=? WHERE id=?",
                    [(tdt, rid) for (rid, tdt) in to_fix])
                print(f"[kdf] UPDATE applied to {len(to_fix)} rows")

            # 3) rebuild uniqueness on the correct key
            if not has_new:
                conn.execute(
                    f"CREATE UNIQUE INDEX {NEW_INDEX} ON kalshi_settlements"
                    "(station, series, target_date)")
                print(f"[kdf] created unique index {NEW_INDEX} "
                      "(station, series, target_date)")
            conn.commit()
        else:
            conn.rollback()

        # ---- verify -----------------------------------------------------
        still = 0
        samples = []
        for r in conn.execute(
                "SELECT id, station, event_ticker, target_date FROM kalshi_settlements"):
            tdt = ticker_date(r["event_ticker"])
            if tdt is None:
                continue
            if tdt != r["target_date"]:
                still += 1
                if len(samples) < 5:
                    samples.append(dict(r))
        # index set after repair
        final_idx = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='kalshi_settlements'"))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        # confirm the correct key is still unique
        dup = conn.execute(
            "SELECT COUNT(*) FROM (SELECT station, series, target_date "
            "FROM kalshi_settlements GROUP BY station, series, target_date "
            "HAVING COUNT(*) > 1)").fetchone()[0]
        conn.close()

        after_md5 = md5(DB) if a.apply else before_md5
        art = {
            "generated_by": "scripts/fix_kalshi_settlement_dates_2026-10-15.py",
            "applied": bool(a.apply),
            "db": str(DB.relative_to(REPO)),
            "md5_before": before_md5,
            "md5_after": after_md5,
            "backup": str(backup.relative_to(REPO)) if backup else None,
            "rows_total": n_total,
            "rows_fixed": len(to_fix) if a.apply else 0,
            "rows_that_would_change": len(to_fix),
            "fixed_by_source_type": by_src,
            "rows_still_mismatched": still,
            "index_before": old_sql,
            "index_after": final_idx,
            "unique_key_duplicates": dup,
            "integrity_check": integrity,
            "foreign_key_check_errors": len(fk),
            "sample_remaining": samples,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(art, indent=2))
        print(f"[kdf] still_mismatched={still} key_dups={dup} "
              f"integrity={integrity} fk_errors={len(fk)}")
        print(f"[kdf] indexes now: {final_idx}")
        print(f"[kdf] artifact -> {OUT}")
        if not a.apply:
            print("[kdf] DRY RUN — nothing written. Re-run with --apply to fix.")
        return 0
    except Exception:
        conn.rollback()
        conn.close()
        raise


if __name__ == "__main__":
    sys.exit(main())
