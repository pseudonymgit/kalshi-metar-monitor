#!/usr/bin/env python3
"""fix_booked_prob_invariant_2026-10-16.py — ITEM 1 audited backfill.

The booked prob (`trades.forecast_prob`/`analytical_prob`) is a DERIVED field:
the companion's YES-side `trade_forecaster_probs.calibrated_p` transformed by the
row's OWN booked leg (YES: cal; NO: 1-cal). Three day-1 open rows diverged from
that derivation because a 15:15 companion re-stamp moved `calibrated_p` while the
booked column kept its stale 14:15 value:

    id=287 KMSP buy_no  booked 0.6914 vs 0.6976  (Δ0.0062, MATERIAL)
    id=288 KMSY buy_yes booked 0.9481 vs 0.9534  (Δ0.0053, MATERIAL)
    id=285 KLAX buy_no  booked 0.9879 vs 0.9886  (Δ0.0007, same defect @ rounding scale)

This script rewrites ONLY those booked columns for rows failing the invariant
``abs(booked - (cal if YES else 1-cal)) < 1e-6`` — no other field, no other row,
never a settled row. `calibration_error` is recomputed against the row's own
`market_price`; `implied_prob` is left as-is (it is a fill-side field).

Record integrity: the rows are UNSETTLED day-1 open rows and the booked prob is a
DERIVED quantity, not a chosen decision input — the decision (side, strike, size,
price) is untouched, so correcting the derivation is not rewriting the decision.
The register AI-4 "stand as-is" note is superseded FOR THE PROB COLUMN by the
cold-eyes finding; every change is logged with before/after.

Usage:
  python3 scripts/fix_booked_prob_invariant_2026-10-16.py --dry-run
  python3 scripts/fix_booked_prob_invariant_2026-10-16.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
TOL = 1e-6


def _leg(trade_type: str):
    t = (trade_type or "").lower()
    if "yes" in t:
        return "yes"
    if "no" in t:
        return "no"
    return None


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    rows = conn.execute(
        """SELECT t.id, t.trade_uuid, t.station, t.trade_type, t.market_price,
                  t.forecast_prob, t.analytical_prob, f.calibrated_p
             FROM trades t
             JOIN trade_forecaster_probs f ON f.trade_uuid = t.trade_uuid
            WHERE t.status = 'open' AND f.calibrated_p IS NOT NULL""").fetchall()

    changes, scanned = [], 0
    for (rid, tuuid, station, tt, mkt, booked, analytical, cal) in rows:
        scanned += 1
        leg = _leg(tt)
        if leg is None:
            continue
        derived = float(cal) if leg == "yes" else 1.0 - float(cal)
        if booked is not None and abs(float(booked) - derived) < TOL:
            continue
        err = abs(derived - float(mkt)) if mkt is not None else None
        changes.append({
            "id": rid, "trade_uuid": tuuid, "station": station,
            "trade_type": tt, "leg": leg,
            "booked_before": booked, "booked_after": derived,
            "analytical_before": analytical, "calibrated_p": cal,
            "delta": (abs(float(booked) - derived) if booked is not None else None),
            "calibration_error": err,
        })
        if not dry_run:
            conn.execute(
                "UPDATE trades SET forecast_prob=?, analytical_prob=?, "
                "calibration_error=? WHERE trade_uuid=? AND status='open'",
                (derived, derived, err, tuuid))

    if not dry_run and changes:
        conn.commit()

    return {
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run, "scanned": scanned,
        "n_changed": len(changes), "changes": changes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BOOK))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-out", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        summary = run(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    if args.audit_out:
        with open(args.audit_out, "a") as fh:
            fh.write(json.dumps(summary) + "\n")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
