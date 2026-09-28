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

    # REVIEW FIX (F8): daemon liveness — the previous block queried the cycle
    # table, discarded the result, and never used it (dead code that looked like a
    # check). It now actually gates: if the newest daemon cycle is older than the
    # threshold, the dead-man switch fires.
    sc = REPO / "data" / "signal_cache.db"
    if sc.exists():
        try:
            c = sqlite3.connect(f"file:{sc}?mode=ro", uri=True)
            cols = [r[1] for r in c.execute("PRAGMA table_info(intraday_daemon_cycles)")]
            tcol = next((x for x in ("cycle_time", "timestamp", "created_at", "ts") if x in cols), None)
            if tcol:
                last = c.execute(f"SELECT MAX({tcol}) FROM intraday_daemon_cycles").fetchone()[0]
            else:
                last = None
            c.close()
            if last:
                import datetime as _dt
                try:
                    ts = _dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_dt.timezone.utc)
                    age_h = (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600.0
                    if age_h > 12:
                        stale.append(("intraday_daemon_cycles", round(age_h, 1), 12))
                        print(f"DEADMAN: STALE intraday_daemon_cycles — last {age_h:.1f}h ago (max 12h)")
                    else:
                        print(f"DEADMAN: OK intraday_daemon_cycles — last {age_h:.1f}h ago")
                except Exception:
                    pass
        except Exception as e:
            print(f"DEADMAN: WARN — could not read daemon cycles: {e}")

    if stale or missing:
        print(f"\nDEADMAN: FAIL — {len(stale)} stale, {len(missing)} missing")
        return 1
    print("\nDEADMAN: PASS — all producers fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
