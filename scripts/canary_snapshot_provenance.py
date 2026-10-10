#!/usr/bin/env python3
"""
canary_snapshot_provenance.py — standing canary: no non-canonical row may sit in
the immutable `settlement_snapshot` (governance action A5).

Ruling: `.meta/continuity/weather-engine/governance-provenance-drift-disposition-2026-10-10.md`.

Root defect: the backfill's last-resort path froze PROVISIONAL values
(weatherapi/GHCN) into an immutable FINAL-truth store. The class must never be
able to hide behind a clean default label again, so this check flags **any**
`settlement_snapshot` row whose `temp_source` is not in the canonical set
({kalshi_live_api, kalshi_finalized}).

Read-only. Deterministic. Exit codes (gate-hook convention):
    0  clean   — every frozen row is canonically sourced
    3  flagged — one or more non-canonical rows present (list printed)
    4  missing — the snapshot table is absent (cannot certify clean)

    python3 scripts/canary_snapshot_provenance.py            # check metar_backfill.db
    python3 scripts/canary_snapshot_provenance.py --json
    python3 scripts/canary_snapshot_provenance.py --db /path/to/other.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT_DB = REPO / "data" / "metar_backfill.db"

# The ONLY sources allowed in the immutable final-truth store (mirrors
# core.canonical_settlement.CANONICAL_TEMP_SOURCE and the upkeep keep-fresh
# canonical set). Anything else is non-canonical by construction.
CANONICAL_SNAPSHOT_SOURCES = frozenset({"kalshi_live_api", "kalshi_finalized"})

EXIT_CLEAN = 0
EXIT_FLAGGED = 3
EXIT_MISSING = 4


def scan(db: str) -> Dict[str, Any]:
    """Return {present, total, flagged:[...], by_source:{...}} for the snapshot."""
    if not Path(db).exists():
        return {"present": False, "total": 0, "flagged": [], "by_source": {},
                "db": db}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        try:
            rows = c.execute(
                "SELECT station, market_type, local_trading_date, "
                "settlement_temp_f, temp_source, frozen_at_utc "
                "FROM settlement_snapshot "
                "ORDER BY local_trading_date, station").fetchall()
        except sqlite3.OperationalError:
            return {"present": False, "total": 0, "flagged": [], "by_source": {},
                    "db": db}
    finally:
        c.close()
    by_source: Dict[str, int] = {}
    flagged: List[Dict[str, Any]] = []
    for r in rows:
        src = r["temp_source"]
        by_source[src] = by_source.get(src, 0) + 1
        if src not in CANONICAL_SNAPSHOT_SOURCES:
            flagged.append({
                "station": r["station"], "market_type": r["market_type"],
                "local_trading_date": r["local_trading_date"],
                "settlement_temp_f": r["settlement_temp_f"], "temp_source": src,
                "frozen_at_utc": r["frozen_at_utc"],
            })
    return {"present": True, "total": len(rows), "flagged": flagged,
            "by_source": by_source, "db": db}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(SNAPSHOT_DB))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    res = scan(a.db)
    if a.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        print(f"[canary] snapshot db: {res['db']}")
        if not res["present"]:
            print("[canary] settlement_snapshot table ABSENT — cannot certify clean")
        else:
            print(f"[canary] frozen rows={res['total']} by_source={res['by_source']}")
            print(f"[canary] canonical sources={sorted(CANONICAL_SNAPSHOT_SOURCES)}")

    if not res["present"]:
        return EXIT_MISSING
    if not res["flagged"]:
        if not a.json:
            print("[canary] CLEAN — every frozen row is canonically sourced")
        return EXIT_CLEAN

    if not a.json:
        print(f"[canary] FLAGGED {len(res['flagged'])} non-canonical row(s) in the "
              f"immutable snapshot:")
        for r in res["flagged"]:
            print(f"  {r['station']:<5} {r['local_trading_date']} "
                  f"frozen={r['settlement_temp_f']} source={r['temp_source']} "
                  f"(frozen_at={r['frozen_at_utc']})")
        print("[canary] provisional values must not sit in the final-truth store; "
              "see governance disposition A1/A2 annotation + A4 promotion")
    return EXIT_FLAGGED


if __name__ == "__main__":
    sys.exit(main())
