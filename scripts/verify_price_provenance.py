#!/usr/bin/env python3
"""
verify_price_provenance.py — E1 price-integrity reconciliation (2026-10-05)
==========================================================================
For every SBOX trade, recompute the DECISION-TIME market price from
`kalshi_market_snapshots.db` and compare it to the `market_price` booked in
`paper_trading_sbox.db.trades`.

Decision-time price = the most recent `market_snapshots` row for that exact
market_ticker with `snapshot_time <= trade.created_at_utc` (bid/ask/mid).

Output per trade: trade_uuid, ticker, created_at, snapshot_time, mid, booked
price, and a verdict:
  MATCH        booked within [yes_bid, yes_ask] of decision snapshot (or ==mid)
  STALE_WALK   booked price is NOT the decision-time snapshot — it equals an
               OLDER snapshot's price (the E1 bug)
  MISMATCH     neither matches
  NO_SNAPSHOT  no snapshot at/before decision time (cannot verify)

Deterministic, read-only. Exits non-zero if any STALE_WALK/MISMATCH found,
so it can gate a pipeline.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TRADES_DB = REPO / "data" / "paper_trading_sbox.db"
SNAPS_DB = REPO / "data" / "kalshi_market_snapshots.db"

_TICKER_RE = re.compile(r"(KXHI\w*?-\d\d[A-Z]{3}\d\d-[BT][\d.]+)")


def _ticker_from_notes(notes: str):
    m = _TICKER_RE.search(notes or "")
    return m.group(1) if m else None


def _snap_at(conn, ticker, when_iso):
    """Most recent snapshot row for `ticker` at/before `when_iso`."""
    return conn.execute(
        "SELECT snapshot_time, yes_bid, yes_ask, mid_price FROM market_snapshots "
        "WHERE market_ticker=? AND snapshot_time <= ? "
        "ORDER BY snapshot_time DESC LIMIT 1",
        (ticker, when_iso)).fetchone()


def _expected_band(trade_type, bid, ask, mid):
    """Fair-price band for the trade's bought side.

    buy_yes  -> long YES: fair in [yes_bid, yes_ask]
    sell_yes -> short YES = long NO: fair in [1-yes_ask, 1-yes_bid]
    """
    if mid is None:
        return None, None
    if trade_type == "sell_yes":
        lo = 1.0 - ask if ask is not None else 1.0 - mid
        hi = 1.0 - bid if bid is not None else 1.0 - mid
        return lo, hi
    lo = bid if bid is not None else mid
    hi = ask if ask is not None else mid
    return lo, hi


def _stale_source(conn, ticker, when_iso, booked):
    """Find a snapshot at/before decision time whose mid equals `booked`.

    Returns the NEWEST such snapshot (the walk-back source) or None. This is the
    precise E1 signature: the booked price matches an OLDER snapshot, not the
    decision-time one.
    """
    rows = conn.execute(
        "SELECT snapshot_time, mid_price FROM market_snapshots "
        "WHERE market_ticker=? AND snapshot_time <= ? AND mid_price IS NOT NULL "
        "ORDER BY snapshot_time DESC",
        (ticker, when_iso)).fetchall()
    for snap_time, mid in rows:
        if mid is not None and abs(float(mid) - booked) < 1e-9:
            return (snap_time, mid)
    return None


def main():
    t = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    t.row_factory = sqlite3.Row
    s = sqlite3.connect(f"file:{SNAPS_DB}?mode=ro", uri=True)
    s.row_factory = sqlite3.Row

    rows = t.execute(
        "SELECT trade_uuid, station, trade_date_utc, created_at_utc, "
        "market_price, trade_type, status, notes FROM trades ORDER BY created_at_utc").fetchall()

    counts = {"MATCH": 0, "STALE_WALK": 0, "MISMATCH": 0, "NO_TICKER": 0,
              "NO_SNAPSHOT": 0}
    bad = []

    for r in rows:
        ticker = _ticker_from_notes(r["notes"])
        if not ticker:
            counts["NO_TICKER"] += 1
            continue
        snap = _snap_at(s, ticker, r["created_at_utc"])
        booked = float(r["market_price"])
        if snap is None:
            counts["NO_SNAPSHOT"] += 1
            bad.append((r["trade_uuid"], ticker, r["created_at_utc"], None,
                        None, None, booked, "NO_SNAPSHOT"))
            continue
        snap_time, bid, ask, mid = snap
        mid = float(mid) if mid is not None else None
        lo, hi = _expected_band(r["trade_type"], bid, ask, mid)
        if mid is not None and hi is not None and lo <= booked <= hi:
            counts["MATCH"] += 1
            continue
        # Not the decision-time price. Check if it's a stale older snapshot.
        verdict = "MISMATCH"
        stale = _stale_source(s, ticker, r["created_at_utc"], booked)
        if stale is not None and stale[0] != snap_time:
            verdict = "STALE_WALK"
            snap_time = f"{stale[0]}(walked)"
        counts[verdict] += 1
        bad.append((r["trade_uuid"], ticker, r["created_at_utc"], snap_time,
                    mid, f"[{lo},{hi}]", booked, verdict))

    print("=" * 100)
    print("E1 PRICE PROVENANCE RECONCILIATION — paper_trading_sbox.db vs "
          "kalshi_market_snapshots.db")
    print("=" * 100)
    print(f"trades scanned: {len(rows)}")
    for k, v in counts.items():
        print(f"  {k:12s}: {v}")
    print("-" * 100)
    print(f"{'verdict':10s} {'ticker':28s} {'created_at':26s} {'snap_time':26s} "
          f"{'mid':>6s} {'booked':>6s}")
    for tuuid, ticker, created, snap_time, mid, rng, booked, verdict in bad:
        print(f"{verdict:10s} {ticker:28s} {created:26s} {str(snap_time):26s} "
              f"{str(mid):>6s} {booked:>6.3f}  {rng or ''}")
    print("-" * 100)
    n_bad = counts["STALE_WALK"] + counts["MISMATCH"]
    if n_bad:
        print(f"RESULT: {n_bad} trade(s) priced off a non-decision-time snapshot "
              f"-> E1 bug PRESENT (pre-fix data).")
        return 1
    print("RESULT: all trades priced within their decision-time snapshots.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
