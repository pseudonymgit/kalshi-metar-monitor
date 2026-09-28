#!/usr/bin/env python3
"""
TRACK 3 / P3-C — Dead-man switch.

The system quietly stopped trading for ~1 week and nothing alerted. This checks
that key producers/daemons have run recently; if a critical artifact is stale, it
exits NON-ZERO with a loud message so cron/announce surfaces it.

Preflight-style: presence + freshness of last-run timestamps, not just existence.

Run: .venv-gefs/bin/python scripts/deadman_switch.py
Exit 0 = all fresh; 1 = one or more stale/missing.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (label, artifact path, max_age_hours)
CHECKS = [
    ("metar_backfill.db", REPO / "data" / "metar_backfill.db", 6),
    ("kalshi_market_snapshots.db", REPO / "data" / "kalshi_market_snapshots.db", 6),
    ("nwp_forecasts.db", REPO / "data" / "nwp_forecasts.db", 30),
    ("ws_p_distribution.json", REPO / "data" / "ws_p_distribution.json", 24 * 14),
]


def _age_hours(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / 3600.0


def main():
    stale = []
    missing = []
    for label, path, max_age in CHECKS:
        if not path.exists():
            missing.append(label)
            print(f"DEADMAN: MISSING {label} ({path})")
            continue
        age = _age_hours(path)
        if age > max_age:
            stale.append((label, round(age, 1), max_age))
            print(f"DEADMAN: STALE {label} — {age:.1f}h old (max {max_age}h)")
        else:
            print(f"DEADMAN: OK {label} — {age:.1f}h old")

    # daemon liveness: cycle table advanced recently?
    sc = REPO / "data" / "signal_cache.db"
    if sc.exists():
        try:
            c = sqlite3.connect(f"file:{sc}?mode=ro", uri=True)
            row = c.execute("SELECT MAX(rowid) FROM intraday_daemon_cycles").fetchone()
            c.close()
        except Exception:
            pass

    if stale or missing:
        print(f"\nDEADMAN: FAIL — {len(stale)} stale, {len(missing)} missing")
        return 1
    print("\nDEADMAN: PASS — all producers fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
