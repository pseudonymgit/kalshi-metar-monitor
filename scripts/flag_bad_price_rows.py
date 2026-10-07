#!/usr/bin/env python3
"""
flag_bad_price_rows.py — historical bad-price annotation (Sprint F, Item 2)
===========================================================================
E1 (commit 4a21c931) fixed the stale-walk-back price bug going forward, and F2
(17a2fd703) fixed the side-inversion label bug. Both are frozen pre-fix data:
the historical `trades` rows that carry the old signatures are NOT retroactively
mutated. But those rows must not silently poison downstream stats.

This tool annotates them, without destroying the immutable P&L record:

  * Adds an additive `reconciliation_log` table recording, per row, WHICH check
    failed and the exact evidence (ticker, decision snapshot, expected band,
    booked price). The numeric columns of `trades` are NEVER modified.
  * Sets `trades.status = 'flagged'` on the offending rows so aggregate queries
    keyed on `status='closed'` (the stats convention) exclude them by default.
  * Prepends `[E1-FLAG <date>] <verdict>` to `trades.notes`, preserving the
    original note text after a ` | ` separator.

Classification is imported from `scripts/verify_price_provenance.py` so the
annotation and the gate can never disagree about what "bad" means.

Usage:
  python3 scripts/flag_bad_price_rows.py            # dry-run (default)
  python3 scripts/flag_bad_price_rows.py --apply    # write status + log + notes

Deterministic. Read-only unless --apply. Idempotent on re-run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from scripts.verify_price_provenance import (  # noqa: E402
    TRADES_DB, SNAPS_DB, _ticker_from_notes, _snap_at, _expected_band,
    _stale_source,
)

FLAG_DATE = "2026-10-07"
CHECK_NAME = "E1_price_provenance"

_LOG_DDL = """
CREATE TABLE IF NOT EXISTS reconciliation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uuid TEXT NOT NULL,
    check_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT,
    evidence TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(trade_uuid, check_name)
)
"""


def _classify(s, ticker, created, booked, trade_type):
    """Return (verdict, evidence_dict) — identical logic to the gate."""
    snap = _snap_at(s, ticker, created)
    if snap is None:
        return "NO_SNAPSHOT", {}
    snap_time, bid, ask, mid = snap
    mid = float(mid) if mid is not None else None
    lo, hi = _expected_band(trade_type, bid, ask, mid)
    if mid is not None and hi is not None and lo <= booked <= hi:
        return "MATCH", {}
    verdict = "MISMATCH"
    stale = _stale_source(s, ticker, created, booked)
    if stale is not None and stale[0] != snap_time:
        verdict = "STALE_WALK"
    return verdict, {
        "ticker": ticker, "created_at_utc": created,
        "snap_time": snap_time, "mid": mid, "band": [lo, hi],
        "booked": booked, "walked_to": stale[0] if stale else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write status/log/notes (default: dry-run)")
    args = ap.parse_args()

    t = sqlite3.connect(str(TRADES_DB))
    t.row_factory = sqlite3.Row
    s = sqlite3.connect(f"file:{SNAPS_DB}?mode=ro", uri=True)

    rows = t.execute(
        "SELECT trade_uuid, station, created_at_utc, market_price, trade_type, "
        "status, notes FROM trades ORDER BY created_at_utc").fetchall()

    flagged, already, clean = [], [], 0
    for r in rows:
        ticker = _ticker_from_notes(r["notes"])
        if not ticker:
            continue
        booked = float(r["market_price"])
        verdict, ev = _classify(s, ticker, r["created_at_utc"], booked,
                                r["trade_type"])
        if verdict not in ("STALE_WALK", "MISMATCH", "NO_SNAPSHOT"):
            clean += 1
            continue
        if r["status"] == "flagged" or "[E1-FLAG" in (r["notes"] or ""):
            already.append((r["trade_uuid"], verdict))
            continue
        flagged.append((r["trade_uuid"], verdict, ev, r["notes"] or ""))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] rows={len(rows)} clean={clean} "
          f"to_flag={len(flagged)} already_flagged={len(already)}")

    if args.apply:
        t.execute(_LOG_DDL)
        now = datetime.now(timezone.utc).isoformat()
        for tuuid, verdict, ev, notes in flagged:
            detail = f"{verdict}: booked {ev['booked']:.3f} outside " \
                     f"band {ev['band']} (decision mid {ev['mid']})"
            t.execute(
                "INSERT OR REPLACE INTO reconciliation_log "
                "(trade_uuid, check_name, verdict, detail, evidence, "
                "created_at_utc) VALUES (?,?,?,?,?,?)",
                (tuuid, CHECK_NAME, verdict, detail, repr(ev), now))
            stamped = f"[E1-FLAG {FLAG_DATE}] {verdict}"
            new_notes = f"{stamped} | {notes}" if notes else stamped
            t.execute(
                "UPDATE trades SET status='flagged', notes=? "
                "WHERE trade_uuid=?", (new_notes, tuuid))
        t.commit()

    for tuuid, verdict, ev, _ in flagged:
        print(f"  {'FLAG' if args.apply else 'would-flag'} "
              f"{tuuid[:8]} {verdict:10s} {ev.get('ticker')} "
              f"booked={ev.get('booked')} band={ev.get('band')}")
    print(f"[{mode}] flagged={len(flagged)} "
          f"(26 stale-walk + 2 side-inversion expected from F1 gate)")
    t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
