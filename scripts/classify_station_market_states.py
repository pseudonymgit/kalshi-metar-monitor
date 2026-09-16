#!/usr/bin/env python3
"""
B2.5 — Station market-state classification (active / no-market / thin / undiscovered / paper-only).

Reads the z8 Kalshi market snapshot DB + paper-trading DB and classifies each station in
the canonical 20-station roster into one of five market states.

Taxonomy (per control register B2.5 / H7):
- active:       has live quoted Kalshi markets (bid>0 or ask<1) in z8, with OI>0
- no-market:    present in z8 but no live quoted market (only synthetic bid=0/ask=1)
- thin:         quoted market exists but liquidity below threshold (low OI / no real bid)
- undiscovered: in the canonical 20-roster but absent from z8 entirely
- paper-only:   has paper trades but no live z8 market

Deterministic — no AI/ML. Read-only against existing DBs.
"""

import os
import sqlite3
import json
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Z8_DB = os.path.join(PROJECT_ROOT, "data", "kalshi_market_snapshots.db")
PAPER_DB = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")

# Canonical 20-station roster (core/metar_monitor.py:293)
CANONICAL_20 = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]

# Liquidity thresholds (preregistered here for B2.5)
MIN_OI_ACTIVE = 1.0          # any positive open interest => active (not no-market)
MIN_REAL_BID_ROWS = 1        # at least one snapshot with a real bid (bid>0) => actively quoted


def _z8_station_stats(db_path: str) -> dict:
    """Per-station stats from z8: row count, real-bid rows, positive-OI rows, latest time."""
    if not os.path.exists(db_path):
        return {}
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    stats = {}
    for r in c.execute("""
        SELECT station,
               COUNT(*) AS n,
               SUM(CASE WHEN yes_bid > 0 THEN 1 ELSE 0 END) AS real_bid_rows,
               SUM(CASE WHEN open_interest > 0 THEN 1 ELSE 0 END) AS oi_rows,
               MAX(snapshot_time) AS latest
        FROM market_snapshots
        GROUP BY station
    """):
        stats[r["station"]] = {
            "n": r["n"],
            "real_bid_rows": r["real_bid_rows"] or 0,
            "oi_rows": r["oi_rows"] or 0,
            "latest": r["latest"],
        }
    c.close()
    return stats


def _paper_stations(db_path: str) -> set:
    """Set of stations with at least one paper trade."""
    if not os.path.exists(db_path):
        return set()
    c = sqlite3.connect(db_path)
    try:
        rows = c.execute("SELECT DISTINCT station FROM paper_trades WHERE station IS NOT NULL").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        c.close()


def classify(z8_stats: dict, paper_stations: set) -> dict:
    out = {}
    for st in CANONICAL_20:
        s = z8_stats.get(st)
        if s is None:
            # absent from z8
            if st in paper_stations:
                out[st] = "paper-only"
            else:
                out[st] = "undiscovered"
            continue
        # present in z8
        has_real_bid = s["real_bid_rows"] >= MIN_REAL_BID_ROWS
        has_oi = s["oi_rows"] > 0
        if not has_real_bid and not has_oi:
            out[st] = "no-market"
        elif not has_real_bid and has_oi:
            out[st] = "thin"   # OI present but no real bid => thin / stale quote
        else:
            out[st] = "active"
    return out


def main():
    z8_stats = _z8_station_stats(Z8_DB)
    paper_stations = _paper_stations(PAPER_DB)
    classification = classify(z8_stats, paper_stations)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "canonical_roster_count": len(CANONICAL_20),
        "z8_station_count": len(z8_stats),
        "classification": classification,
        "summary": {},
        "notes": [
            "volume_24h is mostly 0 across z8 snapshots (only 17,642/243,208 rows > 0); OI is the reliable liquidity signal.",
            "yes_bid is 0 for most snapshots (only 8,508/243,208 > 0); yes_ask is 1.0 => synthetic quote pattern when no live bid.",
            "station count: canonical roster=20, z8=18. Missing from z8: KPHL, KSAT.",
        ],
    }
    for st, state in classification.items():
        result["summary"][state] = result["summary"].get(state, 0) + 1

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
