#!/usr/bin/env python3
"""build_we_dashboard.py — regenerate data/we-dashboard.json (read-side only).

Reads the live SBOX book (read-only) for book counts; the honest metrics,
fusion, sizing and workstream fields are sourced from the register
(docs/plans/WE-COMPLETE-REGISTER-2026-10-13.md) and are pinned here so the
dashboard data source is reproducible rather than hand-maintained.

The output feeds dashboard(widget_put, name='we-status',
pluginKind='session:report'). This script writes ONLY data/we-dashboard.json.

Usage: .venv-gefs/bin/python scripts/build_we_dashboard.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "we-dashboard.json"


def book_counts():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        open_n = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        closed_n = c.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        c.close()
    return open_n, closed_n, total


def main():
    open_n, closed_n, total = book_counts()
    doc = {
        "schema": "we-dashboard/v1",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": "4f5c63c90",
        "generator": "scripts/build_we_dashboard.py",
        "purpose": ("Structured data source for the WE status surface. Rendered via "
                    "dashboard(widget_put, name='we-status', pluginKind='session:report')."),
        "read_side": True,
        "sections": {
            "book": {
                "db": "data/paper_trading_sbox.db",
                "open": open_n, "closed": closed_n, "total": total,
                "note": ("SBOX paper book. Constant 1-contract sizing. 82 archived bad-trade "
                         "rows (p_fused saturated 0.99) are excluded from the live book."),
            },
            "honest_metrics": {
                "rate_1h_brier_at_pin": 0.36,
                "rate_1h_brier_label": "honest, 13:30 station-local pin",
                "rate_1h_published_lookahead_brier": 0.092,
                "best_single_source": {"model": "GFS", "brier": 0.16},
                "decision_ceiling_low": 0.82, "decision_ceiling_high": 0.86,
                "om_gfs_bc_brier": 0.123,
                "rate_vs_nwp_fcerr": "0.16-0.30",
                "source": "docs/plans/HONEST-ARSENAL-REEVALUATION-2026-10-13.md",
            },
            "fusion": {
                "members": 5, "om_gfs_wired": True, "om_gfs_flag": "USE_OM_GFS_FUSION=1",
                "agreement_boost_flag": "USE_AGREEMENT_BOOST=1",
                "agreement_boost_note": ("explicit n-of-k concurrence term; >=3 concur adds up "
                                         "to +0.03 to p_core pre-modulator"),
            },
            "sizing": {
                "max_position_usd": 10,
                "bands": {">0.70": 10, ">0.50": 5, "else": 0},
                "note": "Ladder re-anchored to $10 cap (Dan 2026-10-13); $25 tier removed.",
            },
            "workstreams": [
                {"id": "V", "name": "EV-Axis Sprint", "status": "done"},
                {"id": "W", "name": "Ceiling Refit + Pins + Ladder", "status": "done"},
                {"id": "X", "name": "Honest Re-run", "status": "done"},
                {"id": "Y", "name": "Decisions Executed", "status": "done"},
                {"id": "Z", "name": "WE Dashboard", "status": "live"},
                {"id": "AA", "name": "EV Gate Fix + Sprint Remainder", "status": "done"},
                {"id": "AB", "name": "GEFSv12 Reforecast Backfill", "status": "queued-cron"},
            ],
            "blocked": [
                "ECDS second-key (e543bdb6) rotation + diagnostic-doc redaction",
                "stale opens (3 Oct-12 rows) pending truth / tie rule",
                "LOW parity not wired (holdout P2 FAILS, parked)",
            ],
            "revisit": [
                "per-station weight tuning (data-blocked)",
                "RTMA/URMA nowcast anchor (re-checked on honest axis: DO_NOT_WIRE)",
                "GEFSv12 reforecast (backfill cron live)",
                "GFS v17 rename watch (PNS 26-29/26-30; SCN not yet issued)",
                "pin sweep (15:00 local candidate)",
            ],
        },
    }
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT} (open={open_n} closed={closed_n} total={total})")


if __name__ == "__main__":
    main()
