#!/usr/bin/env python3
"""
analyze_ladder_ceiling.py — quantify the Kalshi strike-ladder ceiling for the
intraday (13:30 station-local) decision window, Aug 1 – Sep 30 2026.

WHY THIS EXISTS
---------------
`sbox_historical_replay.py` admits one candidate per (station, event-date) whose
intraday strike ladder is non-empty at the pinned entry. The active blocker on
reaching the N=200 stopping rule is therefore LADDER COVERAGE, not methodology.
This script measures that ceiling directly and prints the per-station-day ladder
depth, so the gap is a measured number rather than an estimate.

WHAT IT MEASURES (read-only)
----------------------------
For each (station, event-date) with a pinned entry, reconstruct the -T strike
ladder with the *same* dedup-first / price / volume semantics as
`sbox_intraday_ev_bridge.strike_ladder`, bounded strictly before the entry
instant. Report:
  * station-days with a usable ladder (the hard ceiling on admitted candidates)
  * ladder depth distribution (1, 2, 3 ... strikes)
  * per-station and per-month breakdown

CONTEXT (the finding)
---------------------
Kalshi's historical `KXHIGH*` event for Aug–Sep 2026 exposes exactly 6 contracts
per event: 2 threshold (-T) strikes + 4 bracket (-B) strikes. The -B brackets
are not selectable by the bridge (it matches `LIKE '%-<yymonDD>-%T<strike>'`).
So the ceiling is 2 selectable -T strikes/event-date — verified against the
authoritative Kalshi endpoint
`GET /trade-api/v2/markets?event_ticker=KXHIGH<T>-<YYmonDD>` (returns 6).
A denser historical ladder does not exist to backfill from the API.

Usage:
    .venv-gefs/bin/python scripts/analyze_ladder_ceiling.py \
        --start 2026-08-01 --end 2026-09-30

Deterministic. Read-only. No writes to any DB.
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import scripts.sbox_intraday_ev_bridge as B  # noqa: E402
from core.booking_admission import pinned_entry_time_utc  # noqa: E402

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
MIN_VOLUME = 50.0  # mirrors B.strike_ladder default


def _entry(station: str, code: str) -> datetime | None:
    y, mon, d = 2000 + int(code[:2]), MON[code[2:5]], int(code[5:7])
    iso = pinned_entry_time_utc(station, f"{y:04d}-{mon:02d}-{d:02d}")
    return datetime.fromisoformat(iso) if iso else None


def measure(start: str, end: str, stations: list[str]) -> None:
    con = sqlite3.connect(f"file:{B.SNAPS}?mode=ro", uri=True)
    lo, hi = start[:7], end[:7]
    rows = con.execute(
        "SELECT station, market_ticker, mid_price, yes_ask, volume_24h, snapshot_time "
        "FROM market_snapshots WHERE market_ticker LIKE 'KXHIGH%-T%' "
        "AND (substr(snapshot_time,1,7) >= ? AND substr(snapshot_time,1,7) <= ?)",
        (lo, hi)).fetchall()
    con.close()

    # group candidate rows by (station, event-date-code)
    grp: dict[tuple[str, str], list] = collections.defaultdict(list)
    for st, tk, mid, ask, vol, snap in rows:
        parts = tk.split("-")
        if len(parts) < 3 or not parts[-1].startswith("T"):
            continue
        grp[(st, parts[-2])].append((parts[-1], mid, ask, vol, snap))

    usable, sizes = [], []
    for (st, code), lst in grp.items():
        e = _entry(st, code)
        if e is None:
            continue
        # newest snapshot strictly before entry, per strike (dedup-first)
        best: dict[float, tuple] = {}
        for tail, mid, ask, vol, snap in lst:
            try:
                stime = datetime.fromisoformat(snap)
            except ValueError:
                continue
            if stime >= e:
                continue
            try:
                strike = float(tail[1:])
            except ValueError:
                continue
            if strike not in best or stime > best[strike][0]:
                best[strike] = (stime, mid, ask, vol)
        depth = 0
        for _, (_, mid, ask, vol) in best.items():
            px = mid if mid is not None else ask
            if px is None:
                continue
            px = float(px)
            if px <= 0.02 or px >= 0.98 or (vol or 0) < MIN_VOLUME:
                continue
            depth += 1
        if depth > 0:
            usable.append((st, code, depth))
            sizes.append(depth)

    print(f"window: {start} .. {end} | stations: {len(stations)}")
    print(f"usable station-event-days (ladder depth >= 1): {len(usable)}")
    print(f"ladder depth distribution: {sorted(collections.Counter(sizes).items())}")
    print(f"per station: {dict(collections.Counter(u[0] for u in usable))}")
    sp = collections.Counter(u[1][2:5] for u in usable)
    print(f"per month: {dict(sp)}")
    print("\nNOTE: the API exposes only 2 -T strikes/event (6 contracts total);")
    print("the ceiling is 2 selectable strikes per station-day, not a data gap.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--stations", nargs="*", default=None)
    a = ap.parse_args()
    measure(a.start, a.end, a.stations or B.STATIONS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
