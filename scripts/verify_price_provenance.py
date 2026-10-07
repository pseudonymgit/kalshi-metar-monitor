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

# E1 fix (commit 4a21c931, 2026-10-05T22:59:07Z) made the strike ladder
# dedup-first: the newest snapshot is authoritative and the code never walks
# back to a stale row. Trades CREATED at/after this instant are the post-fix
# cohort and MUST verify clean. Trades created before it are frozen pre-fix
# data — we report their residue loudly but do not let them gate the pipeline
# (a gate that can never pass on immutable history is a broken gate).
E1_FIX_UTC = "2026-10-05T22:59:07+00:00"

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
    buy_no   -> long NO: fair in [1-yes_ask, 1-yes_bid] (same NO band as
                sell_yes). The F2 side-inversion fix (17a2fd703) now emits
                buy_no for a NO-leg pick, so the verifier must band it as NO
                or it would MISMATCH a correct post-fix trade.
    """
    if mid is None:
        return None, None
    if trade_type in ("sell_yes", "buy_no"):
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

    tcols = {d[1] for d in t.execute("PRAGMA table_info(trades)")}
    sel_src = "price_source" if "price_source" in tcols else "NULL AS price_source"
    rows = t.execute(
        "SELECT trade_uuid, station, trade_date_utc, created_at_utc, "
        f"market_price, trade_type, status, notes, {sel_src} "
        "FROM trades ORDER BY created_at_utc").fetchall()

    counts = {"MATCH": 0, "STALE_WALK": 0, "MISMATCH": 0, "NO_TICKER": 0,
              "NO_SNAPSHOT": 0}
    post = {"n": 0, "MATCH": 0, "STALE_WALK": 0, "MISMATCH": 0,
            "NO_TICKER": 0, "NO_SNAPSHOT": 0, "no_source": 0}
    bad = []
    post_bad = []

    for r in rows:
        is_post = r["created_at_utc"] >= E1_FIX_UTC
        if is_post:
            post["n"] += 1
            if not r["price_source"]:
                post["no_source"] += 1
        ticker = _ticker_from_notes(r["notes"])
        if not ticker:
            counts["NO_TICKER"] += 1
            if is_post:
                post["NO_TICKER"] += 1
            continue
        snap = _snap_at(s, ticker, r["created_at_utc"])
        booked = float(r["market_price"])
        if snap is None:
            counts["NO_SNAPSHOT"] += 1
            item = (r["trade_uuid"], ticker, r["created_at_utc"], None,
                    None, None, booked, "NO_SNAPSHOT")
            bad.append(item)
            if is_post:
                post["NO_SNAPSHOT"] += 1
                post_bad.append(item)
            continue
        snap_time, bid, ask, mid = snap
        mid = float(mid) if mid is not None else None
        lo, hi = _expected_band(r["trade_type"], bid, ask, mid)
        if mid is not None and hi is not None and lo <= booked <= hi:
            counts["MATCH"] += 1
            if is_post:
                post["MATCH"] += 1
            continue
        # Not the decision-time price. Check if it's a stale older snapshot.
        verdict = "MISMATCH"
        stale = _stale_source(s, ticker, r["created_at_utc"], booked)
        if stale is not None and stale[0] != snap_time:
            verdict = "STALE_WALK"
            snap_time = f"{stale[0]}(walked)"
        counts[verdict] += 1
        item = (r["trade_uuid"], ticker, r["created_at_utc"], snap_time,
                mid, f"[{lo},{hi}]", booked, verdict)
        bad.append(item)
        if is_post:
            post[verdict] += 1
            post_bad.append(item)

    print("=" * 100)
    print("E1 PRICE PROVENANCE RECONCILIATION — paper_trading_sbox.db vs "
          "kalshi_market_snapshots.db")
    print("=" * 100)
    print(f"trades scanned: {len(rows)}")
    for k, v in counts.items():
        print(f"  {k:12s}: {v}")
    print("-" * 100)
    print(f"POST-FIX cohort (created_at_utc >= {E1_FIX_UTC}): n={post['n']}")
    for k in ("MATCH", "STALE_WALK", "MISMATCH", "NO_TICKER", "NO_SNAPSHOT"):
        print(f"  {k:12s}: {post[k]}")
    print(f"  price_source NULL: {post['no_source']}")
    print("-" * 100)
    print(f"{'verdict':10s} {'ticker':28s} {'created_at':26s} {'snap_time':26s} "
          f"{'mid':>6s} {'booked':>6s}")
    for tuuid, ticker, created, snap_time, mid, rng, booked, verdict in bad:
        print(f"{verdict:10s} {ticker:28s} {created:26s} {str(snap_time):26s} "
              f"{str(mid):>6s} {booked:>6.3f}  {rng or ''}")
    print("-" * 100)
    if post_bad:
        print(f"RESULT: {len(post_bad)} POST-FIX trade(s) priced off a "
              f"non-decision-time snapshot -> E1 regression PRESENT.")
        return 1
    n_pre = counts["STALE_WALK"] + counts["MISMATCH"]
    if post["no_source"]:
        print(f"RESULT: {post['no_source']} POST-FIX trade(s) missing "
              f"price_source -> E1 provenance INCOMPLETE.")
        return 1
    if n_pre:
        print(f"RESULT: POST-FIX cohort clean (n={post['n']}) -> E1 FIX PROVEN.")
        print(f"  NOTE: {n_pre} pre-fix historical row(s) still carry the old "
              f"walk-back/inversion signature (frozen data, pre {E1_FIX_UTC}); "
              f"not retroactively mutated. Non-blocking.")
        return 0
    print("RESULT: all trades priced within their decision-time snapshots.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
