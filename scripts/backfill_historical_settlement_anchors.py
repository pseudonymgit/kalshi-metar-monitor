#!/usr/bin/env python3
"""
backfill_historical_settlement_anchors.py — backfill the canonical settlement
anchor for a historical window (Aug 1 – Sep 30 2026) into the frozen
`settlement_snapshot`, so the historical replay's admitted candidates settle.

Why this exists
---------------
`sbox_historical_replay.py` admits historical Aug/Sep station-days, but they
cannot settle: `settlement_snapshot` (the canonical anchor the settle path reads)
only covers Oct 1–9. The admitted rows stay open, failing closed on
`missing finalization_utc`. This script backfills the missing canonical anchors
for the historical window.

Canonical source (same as the forward path)
-------------------------------------------
Kalshi live API `expiration_value` (api.elections.kalshi.com/trade-api/v2). Where
the live market does not report, fall back to kalshi_settlements(finalized) ->
GHCN — canonical-equivalent cross-checks. **weatherapi is deliberately EXCLUDED**
(non-canonical blend; the governing ruling forbids pinning a settlement on it).

Reuse, not a parallel path
--------------------------
All fetch/freeze primitives come from the existing modules:
  * `core.canonical_settlement`  — snapshot DDL, `freeze_snapshot`, immutability
  * `scripts.kalshi_settlement_upkeep` — `fetch_expiration_value_with_anchor`,
    `event_ticker`, `STATION_TO_KALSHI_CODE`, `_snapshot_fallback_value`

Immutability
------------
`freeze_snapshot(conflict_raises=True)` is write-once: an identical existing row
is skipped; a DIFFERENT value for an already-frozen row FAILS LOUD
(`ImmutableSettlementError`, exit 3) and never overwrites. The write-once
triggers on `settlement_snapshot` also block UPDATE/DELETE outright.

Safety
------
* Dry-run by default; only `--execute` writes, and only to `--snapshot-db`.
* `--snapshot-db` lets you target a COPY first (task requirement) before the
  real DB.
* No live trading. No P&L mutation. ≤20 stations. Deterministic. No AI/ML.

Usage:
    # dry-run against the real DB (no writes)
    python3 scripts/backfill_historical_settlement_anchors.py --start 2026-08-01 --end 2026-09-30
    # rehearse against a throwaway copy
    python3 scripts/backfill_historical_settlement_anchors.py --execute --snapshot-db /tmp/copy.db
    # write the real frozen snapshot
    python3 scripts/backfill_historical_settlement_anchors.py --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core.canonical_settlement import (  # noqa: E402
    CANONICAL_TEMP_SOURCE,
    ImmutableSettlementError,
    ensure_snapshot_table,
    freeze_snapshot,
    read_snapshot,
)
from scripts.kalshi_settlement_upkeep import (  # noqa: E402
    STATION_TO_KALSHI_CODE,
    _snapshot_fallback_value,
    event_ticker,
    fetch_expiration_value_with_anchor,
)

DATA = REPO / "data"
METAR_SNAPSHOT_DB = str(DATA / "metar_backfill.db")
MARKET_TYPE = "HIGH"
# weatherapi is intentionally NOT referenced anywhere in this module.

# Sources that are canonical-equivalent and may seed the frozen snapshot.
CANONICAL_SOURCES = frozenset({CANONICAL_TEMP_SOURCE, "kalshi_finalized"})


def _dates(start: str, end: str) -> List[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    d = d0
    while d <= d1:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _synthetic_anchor(date_str: str) -> str:
    """T+1 ~05:00Z fallback anchor (Kalshi HIGH finalizes the day after)."""
    nxt = (datetime.fromisoformat(date_str) + timedelta(days=1)).date().isoformat()
    return f"{nxt}T05:00:00Z"


def build_anchor_rows(stations: List[str], start: str, end: str, *,
                      offline: bool = False,
                      log=print) -> Tuple[List[dict], List[dict]]:
    """Return (canonical_rows, provisional_rows) for the window.

    canonical_rows carry `temp_source` in CANONICAL_SOURCES (live API or the
    finalized cross-check) — only these are safe to freeze. Any value sourced
    from anything else is refused here (weatherapi is never consulted at all).
    """
    canonical_rows: List[dict] = []
    provisional_rows: List[dict] = []

    days = _dates(start, end)
    total = len(stations) * len(days)
    done = 0
    for st in stations:
        for d in days:
            done += 1
            if done % 50 == 0 or done == total:
                log(f"  [build] {done}/{total} "
                    f"canonical={len(canonical_rows)} "
                    f"refused={len(provisional_rows)}")
            temp: Optional[float] = None
            anchor: Optional[str] = None
            source: Optional[str] = None

            if not offline:
                e_tkr = event_ticker(st, datetime.strptime(d, "%Y-%m-%d"), MARKET_TYPE)
                if e_tkr is not None:
                    fetched = fetch_expiration_value_with_anchor(e_tkr)
                    if fetched is not None:
                        temp = fetched[0]
                        anchor = fetched[1]
                        source = CANONICAL_TEMP_SOURCE

            if temp is None:
                # canonical-equivalent cross-check: kalshi_settlements(finalized)
                # -> GHCN. NO weatherapi.
                fb = _snapshot_fallback_value(st, d)
                if fb is not None:
                    temp = fb
                    source = "kalshi_finalized"
                    anchor = _synthetic_anchor(d)

            if temp is None:
                continue

            row = {
                "station": st,
                "market_type": MARKET_TYPE,
                "local_trading_date": d,
                "settlement_temp_f": float(temp),
                "temp_source": source,
                "settled_at_utc": anchor,
            }
            (canonical_rows if source in CANONICAL_SOURCES
             else provisional_rows).append(row)

    log(f"[build] canonical rows={len(canonical_rows)} "
        f"provisional(refused)={len(provisional_rows)}")
    return canonical_rows, provisional_rows


def run(start: str, end: str, *, execute: bool, snapshot_db: str,
        stations: Optional[List[str]], offline: bool) -> int:
    cohort = sorted(stations or STATION_TO_KALSHI_CODE.keys())
    assert len(cohort) <= 20, f"cohort must be <=20 stations, got {len(cohort)}"
    frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[anchor] window {start}..{end} cohort={len(cohort)} "
          f"db={snapshot_db} execute={execute} offline={offline}", flush=True)
    canonical_rows, provisional_rows = build_anchor_rows(
        cohort, start, end, offline=offline)

    if provisional_rows:
        # Nothing should land here: live API + finalized/GHCN are all canonical-
        # equivalent. Refuse rather than freeze a non-canonical proxy.
        print(f"[anchor] REFUSING {len(provisional_rows)} non-canonical row(s) "
              f"(never frozen into the immutable snapshot)")

    if not os.path.exists(snapshot_db):
        print(f"[anchor] snapshot db does not exist: {snapshot_db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(snapshot_db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    stats = {"inserted": 0, "skipped": 0, "conflicts": 0, "blocked": len(provisional_rows)}
    try:
        if execute:
            ensure_snapshot_table(conn)

        if execute:
            try:
                ins, skip, conflicts = freeze_snapshot(
                    conn, canonical_rows, frozen_at_utc=frozen_at,
                    conflict_raises=True)
                stats.update(inserted=ins, skipped=skip, conflicts=len(conflicts))
            except ImmutableSettlementError as exc:
                print(f"\nIMMUTABILITY VIOLATION — no overwrite performed:\n{exc}",
                      file=sys.stderr)
                return 3
        else:
            # dry-run: read-only plan
            try:
                for r in canonical_rows:
                    ex = read_snapshot(conn, r["station"], r["market_type"],
                                       r["local_trading_date"])
                    if ex is None:
                        stats["inserted"] += 1
                    elif ex["settlement_temp_f"] == r["settlement_temp_f"]:
                        stats["skipped"] += 1
                    else:
                        stats["conflicts"] += 1
                        print(f"  CONFLICT {r['station']}/{r['local_trading_date']} "
                              f"frozen={ex['settlement_temp_f']} "
                              f"new={r['settlement_temp_f']}")
            except sqlite3.OperationalError:
                stats["inserted"] = len(canonical_rows)
    finally:
        conn.close()

    verb = "inserted" if execute else "would-insert"
    print(f"[anchor] {verb}={stats['inserted']} identical={stats['skipped']} "
          f"conflicts={stats['conflicts']} refused={stats['blocked']}")
    if not execute:
        print("[anchor] dry-run: no writes performed; re-run with --execute")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--snapshot-db", default=METAR_SNAPSHOT_DB,
                    help="target snapshot DB (default: data/metar_backfill.db)")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--execute", action="store_true",
                    help="write (default: dry-run)")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live API; use finalized/GHCN cross-checks only")
    a = ap.parse_args(argv)
    return run(a.start, a.end, execute=a.execute, snapshot_db=a.snapshot_db,
               stations=a.stations, offline=a.offline)


if __name__ == "__main__":
    raise SystemExit(main())
