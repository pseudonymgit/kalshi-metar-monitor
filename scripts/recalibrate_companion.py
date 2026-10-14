#!/usr/bin/env python3
"""recalibrate_companion.py — write the HONEST `calibrated_p` into the
`trade_forecaster_probs` companion table (2026-10-14).

Problem this fixes
------------------
The companion's `calibrated_p` was a bare ceiling clamp — `min(p_fused, 0.7519)`
— so the live table held exactly two values (0.2481 / 0.7519). That is a clamp,
not a calibration. The honest re-run (Workstream X) measured the decision-time
signal at the 13:30 pin: ECE ≈ 0.47, honest ceiling 0.82–0.86, base rate ≈ 0.80.

Fix
---
Recompute `calibrated_p` from the row's RAW `p_fused` via
`core.intraday.honest_calibration.honest_calibrated_p` (one source of truth,
shared with the entry gate), then write back `calibrated_p`, `ceiling`,
`composite_conf`, and a NEW `calibrated_at` timestamp column so old and new
rows are distinguishable.

Safety
------
  * READ-ONLY on `trades` — never touches the live book.
  * Only UPDATEs `trade_forecaster_probs`.
  * `--dry-run` prints the plan and writes nothing.

Usage
-----
  python3 scripts/recalibrate_companion.py --dry-run
  python3 scripts/recalibrate_companion.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.honest_calibration import (  # noqa: E402
    honest_calibrated_p, station_ceiling, composite_conf_from_p,
    POOLED_CEILING, ECE, BASE_RATE,
)

BOOK = REPO / "data" / "paper_trading_sbox.db"
CALIBRATED_AT_COLUMN = "calibrated_at"


def _ensure_calibrated_at(conn: sqlite3.Connection) -> None:
    """Add the `calibrated_at` TEXT column (idempotent)."""
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trade_forecaster_probs)")}
    if CALIBRATED_AT_COLUMN not in cols:
        conn.execute(
            f"ALTER TABLE trade_forecaster_probs ADD COLUMN {CALIBRATED_AT_COLUMN} TEXT")
        conn.commit()


def recalibrate(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Rewrite calibrated_p/ceiling/composite_conf/calibrated_at for every row.

    Returns a summary dict. Never writes to `trades`.
    """
    _ensure_calibrated_at(conn)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Raw p_fused may live on the companion row; fall back to the trades row.
    trades_pf = {r[0]: r[1] for r in conn.execute(
        "SELECT trade_uuid, p_fused FROM trades")}

    rows = list(conn.execute(
        "SELECT id, trade_uuid, station, p_fused, calibrated_p FROM "
        "trade_forecaster_probs"))
    updates = []
    skipped = []
    for rid, tuuid, station, p_fused, old_cal in rows:
        pf = p_fused if p_fused is not None else trades_pf.get(tuuid)
        if pf is None or pf != pf:
            skipped.append((tuuid, "no_p_fused"))
            continue
        ce = station_ceiling(station)
        try:
            new_cal = honest_calibrated_p(float(pf), ce)
        except ValueError as e:
            skipped.append((tuuid, str(e)))
            continue
        conf = composite_conf_from_p(new_cal)
        updates.append((new_cal, round(ce, 4), conf, now_iso, rid))

    if not dry_run:
        conn.executemany(
            "UPDATE trade_forecaster_probs SET calibrated_p=?, ceiling=?, "
            "composite_conf=?, calibrated_at=? WHERE id=?", updates)
        conn.commit()

    return {
        "calibrated_at": now_iso,
        "n_rows": len(rows),
        "n_updated": len(updates),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "formula": "calibrated_p = min(ceiling, max(0.01, p_fused*(1-ECE/2)+base*ECE/2))",
        "ece": ECE,
        "base_rate": BASE_RATE,
        "pooled_ceiling": POOLED_CEILING,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Write honest calibrated_p into the companion table.")
    ap.add_argument("--db", default=str(BOOK))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        summary = recalibrate(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    if args.json:
        import json
        print(json.dumps(summary, indent=2))
    else:
        print(f"{'[dry-run] ' if args.dry_run else ''}recalibrated companion table")
        print(f"  rows: {summary['n_rows']}  updated: {summary['n_updated']}  "
              f"skipped: {summary['n_skipped']}")
        print(f"  formula: {summary['formula']}")
        print(f"  ECE={summary['ece']} base_rate={summary['base_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
