#!/usr/bin/env python3
"""
F2 calibration migration (2026-10-13)
=====================================
One-shot, additive, idempotent migration + backfill for the F2 p_fused
calibration fix and the weighted-fusion schema.

What it does (all ADDITIVE — never mutates `trades` or existing columns):
  1. Runs `multi_forecaster_fusion.ensure_schema()`:
       - adds companion columns `calibrated_p`, `p_market`, `composite_conf`,
         `grade`, `ceiling`, `weights_applied` to `trade_forecaster_probs`;
       - creates `station_weights` (per-station weight store);
       - creates `trade_calibration` (calibration companion table).
  2. Seeds `station_weights` with EQUAL weights (0.25 each) for every booked
     station (spec §3.3: below the n>=30 floor -> equal weights).
  3. Backfills `calibrated_p` / `composite_conf` / `grade` / `ceiling` on
     `trade_forecaster_probs` and populates `trade_calibration`, by replaying
     the (already stored) p_gefs/p_analog/p_trajectory through the F2 `fuse()`.
     Recomputing from stored components means the backfill is reproducible and
     does not depend on the original run's live inputs.

Read-only on `trades`; writes only new tables/columns. `--report-only` prints
the calibration effect without writing.

CLI:
  python3 scripts/migrate_fusion_calibration_2026-10-13.py --report-only
  python3 scripts/migrate_fusion_calibration_2026-10-13.py
  python3 scripts/migrate_fusion_calibration_2026-10-13.py --db data/paper_trading_sbox.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    fuse, ensure_schema, ensure_station_weights_defaults, calibrated_ceiling,
    FUSION_RULE_ID, CALIBRATION_FLOOR, GRADE_LADDER,
)

SBOX_DB = REPO / "data" / "paper_trading_sbox.db"


def _saturation(conn, table="trade_forecaster_probs", col="p_fused"):
    row = conn.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN {col} >= 0.99 THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {col} >= 0.90 THEN 1 ELSE 0 END) "
        f"FROM {table} WHERE {col} IS NOT NULL").fetchone()
    n, ge99, ge90 = (row[0] or 0), (row[1] or 0), (row[2] or 0)
    return {"n": n, "ge_0_99": ge99, "ge_0_90": ge90,
            "pct_ge_0_99": round(100.0 * ge99 / n, 1) if n else 0.0,
            "pct_ge_0_90": round(100.0 * ge90 / n, 1) if n else 0.0}


def _grade_dist(conn, table, col):
    rows = conn.execute(
        f"SELECT CASE WHEN {col} IS NULL THEN 'NULL' "
        f"WHEN {col} >= 0.90 THEN 'S' WHEN {col} >= 0.80 THEN 'A' "
        f"WHEN {col} >= 0.70 THEN 'B' WHEN {col} >= 0.60 THEN 'C' "
        f"ELSE 'F' END g, COUNT(*) FROM {table} GROUP BY g ORDER BY g").fetchall()
    return {g: c for g, c in rows}


def _tfp_row_to_meta(r):
    """Replay the stored components through the F2 fuse() -> (p_fused, meta)."""
    pf, meta = fuse(r["p_gefs"], r["p_analog"], r["p_trajectory"])
    return pf, meta


def migrate(db_path: Path, report_only: bool, verbose: bool = True) -> dict:
    out = {"db": str(db_path), "report_only": report_only}
    if report_only:
        # Read-only on the live book: never so much as a WAL-mode flip.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
    try:
        before = _saturation(conn)
        out["before"] = before

        if not report_only:
            ensure_schema(conn)
            seeded = ensure_station_weights_defaults(conn)
            out["stations_seeded"] = seeded

        conn.row_factory = sqlite3.Row
        tfp_cols = {d[1] for d in conn.execute("PRAGMA table_info(trade_forecaster_probs)")}
        has_cal_col = "calibrated_p" in tfp_cols
        rows = conn.execute(
            "SELECT trade_uuid, station, trade_date_utc, strike, side, ticker, "
            "p_gefs, p_analog, p_trajectory FROM trade_forecaster_probs").fetchall()
        now = datetime.now(timezone.utc).isoformat()
        recalc, neutralized = [], 0
        for r in rows:
            pf, meta = _tfp_row_to_meta(r)
            recalc.append((r, pf, meta))
            neutralized += 1 if meta["amplifier_neutralized"] else 0
            if not report_only:
                if has_cal_col:
                    conn.execute(
                        "UPDATE trade_forecaster_probs SET calibrated_p=?, "
                        "composite_conf=?, grade=?, ceiling=? WHERE trade_uuid=?",
                        (meta["calibrated_p"], meta["composite_conf"],
                         meta["grade"], meta["ceiling"], r["trade_uuid"]))
                conn.execute(
                    "INSERT OR REPLACE INTO trade_calibration "
                    "(trade_uuid, station, trade_date_utc, strike, side, ticker, "
                    " p_fused_raw, calibrated_p, ceiling, p_gefs, p_analog, "
                    " p_trajectory, p_market, composite_conf, grade, "
                    " amplifier_neutralized, fusion_rule, created_at_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["trade_uuid"], r["station"], r["trade_date_utc"], r["strike"],
                     r["side"], r["ticker"], pf, meta["calibrated_p"], meta["ceiling"],
                     r["p_gefs"], r["p_analog"], r["p_trajectory"], meta["p_market"],
                     meta["composite_conf"], meta["grade"],
                     1 if meta["amplifier_neutralized"] else 0, FUSION_RULE_ID, now))
        if not report_only:
            conn.commit()

        # effect summary
        pfvals = [m["calibrated_p"] for _, _, m in recalc]
        n = len(pfvals)
        sat = sum(1 for v in pfvals if v >= 0.99)
        ge90 = sum(1 for v in pfvals if v >= 0.90)
        out["after"] = {
            "n": n,
            "ge_0_99": sat,
            "ge_0_90": ge90,
            "pct_ge_0_99": round(100.0 * sat / n, 1) if n else 0.0,
            "pct_ge_0_90": round(100.0 * ge90 / n, 1) if n else 0.0,
            "amplifier_neutralized": neutralized,
        }
        out["ceiling"] = calibrated_ceiling()
        out["calibration_floor"] = CALIBRATION_FLOOR
        # Grade the three candidate inputs to the ladder, so the saturation
        # finding is visible: raw p_fused (what booked today), the calibrated
        # p_fused (capped at the ceiling), and composite_conf (spec §2.3).
        out["grade_dist_raw_pfused"] = _grade_dist(conn, "trade_forecaster_probs", "p_fused")
        out["grade_dist_calibrated_p"] = {
            g: sum(1 for _, _, m in recalc
                   if _conf_grade(m["calibrated_p"]) == g)
            for g, _t, _c in GRADE_LADDER}
        out["grade_dist_composite_conf"] = {
            g: sum(1 for _, _, m in recalc
                   if _conf_grade(m["composite_conf"]) == g)
            for g, _t, _c in GRADE_LADDER}
        # Composite conf is still saturated because the COMPONENTS saturate
        # (p_gefs/p_analog hit 1.0). Surface the count for the report.
        out["composite_conf_ge_0_90"] = sum(
            1 for _, _, m in recalc if m["composite_conf"] >= 0.90)
        out["note"] = ("composite_conf stays high because components saturate; "
                       "de-saturation needs component-level calibration")
        out["rule"] = FUSION_RULE_ID
        if verbose:
            print(json.dumps(out, indent=2))
        return out
    finally:
        conn.close()


def _conf_grade(conf):
    for g, t, _c in GRADE_LADDER:
        if conf >= t:
            return g
    return "F"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(SBOX_DB))
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    migrate(Path(args.db), args.report_only)


if __name__ == "__main__":
    main()
