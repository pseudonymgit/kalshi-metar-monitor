#!/usr/bin/env python3
"""Comprehensive settlement-truth reconciliation for the SBOX book (read-only).

Truth precedence per book row:
  1. live Kalshi API single-market  (ticker -> strike_type/floor/cap/result)
  2. local authoritative kalshi_strike_ladder.db row (by exact ticker)
Compares to the book's stored canonical_outcome / realized_pnl and classifies
the disagreement cause.

Prints a full table + writes artifacts/settlement-truth/reconcile_2026-10-14.json
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SBOX = REPO / "data" / "paper_trading_sbox.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
CACHE = Path("/tmp/kalshi_truth_cache.json")
OUT = REPO / "artifacts" / "settlement-truth"
API = "https://api.elections.kalshi.com/trade-api/v2/markets"


def api(t):
    try:
        req = urllib.request.Request(f"{API}/{t}", headers={"User-Agent": "we-audit"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)["market"]
    except Exception:
        return None


def yes_from_strike_type(st, temp, floor, cap):
    """Kalshi's OWN operator convention (strict)."""
    if st == "greater":
        return None if temp is None or floor is None else (temp > floor)
    if st == "less":
        return None if temp is None or cap is None else (temp < cap)
    if st == "between":
        if temp is None or floor is None or cap is None:
            return None
        return floor <= temp <= cap
    return None


def main():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    sbox = sqlite3.connect(f"file:{SBOX}?mode=ro", uri=True)
    sbox.row_factory = sqlite3.Row
    rows = [dict(r) for r in sbox.execute(
        "SELECT id,status,station,market_type,trade_type,signal_direction,market_ticker,"
        "canonical_settled_f,canonical_outcome,settlement_outcome,settled_value,"
        "realized_pnl,trade_price,created_at_utc FROM trades ORDER BY id")]
    lad = sqlite3.connect(f"file:{LADDER}?mode=ro", uri=True)
    lad.row_factory = sqlite3.Row
    L = {}
    for r in lad.execute("SELECT ticker,strike_type,floor_strike,cap_strike,"
                         "settlement_value_dollars,result FROM strike_ladder "
                         "WHERE ticker IS NOT NULL"):
        L.setdefault(r["ticker"], dict(r))

    out = []
    for r in rows:
        t = r["market_ticker"]
        mk = cache.get(t)
        if mk is None and t:
            mk = api(t)
            if mk is not None:
                cache[t] = mk
        src = "api" if mk else None
        if mk is None and t in L:
            mk = {"strike_type": L[t]["strike_type"], "floor_strike": L[t]["floor_strike"],
                  "cap_strike": L[t]["cap_strike"], "result": L[t]["result"]}
            src = "ladder"
        rec = dict(r)
        rec["truth_src"] = src
        if mk:
            st = mk.get("strike_type")
            res = mk.get("result")
            rec["strike_type"] = st
            rec["floor"] = mk.get("floor_strike")
            rec["cap"] = mk.get("cap_strike")
            rec["kalshi_result"] = res
            yes = (res == "yes") if res in ("yes", "no") else None
            rec["truth_yes"] = yes
            rec["truth_win"] = None if yes is None else (
                yes if r["trade_type"] == "buy_yes" else (not yes) if r["trade_type"] == "buy_no" else None)
            rec["yes_from_temp"] = yes_from_strike_type(st, r["canonical_settled_f"],
                                                       rec["floor"], rec["cap"])
            rec["book_win"] = (r["canonical_outcome"] == "correct") if r["canonical_outcome"] else None
        out.append(rec)
    CACHE.write_text(json.dumps(cache))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reconcile_2026-10-14.json").write_text(json.dumps(out, indent=2))

    res = [x for x in out if x.get("truth_win") is not None]
    mis = [x for x in res if x.get("book_win") != x["truth_win"]]
    print(f"rows={len(out)} resolved={len(res)} unresolved={len(out)-len(res)} book_mismatch={len(mis)}")
    print("--- agreement by (strike_type, book_outcome) ---")
    from collections import Counter
    agg = Counter((x.get("strike_type"), x.get("book_win"), x.get("truth_win")) for x in res)
    for k in sorted(agg, key=str):
        print("   ", k, agg[k])
    print("\n--- ladder-vs-api cross-check (rows with both) ---")
    both = 0
    for x in out:
        t = x["market_ticker"]
        if t in L and x["truth_src"] == "api" and x.get("kalshi_result") in ("yes", "no"):
            both += 1
            if L[t]["result"] != x["kalshi_result"]:
                print("   DISAGREE", x["id"], t, "api", x["kalshi_result"], "ladder", L[t]["result"])
    print(f"   cross-checked {both} rows")
    print("\n--- THE MISMATCHES ---")
    for x in mis:
        print(f"  {x['id']:4d} {x['market_type']} {x['strike_type']:7s} f={str(x['floor']):5s} "
              f"c={str(x['cap']):5s} temp={x['canonical_settled_f']} kres={x['kalshi_result']:3s} "
              f"{x['trade_type']} book={'W' if x['book_win'] else 'L'}->truth="
              f"{'W' if x['truth_win'] else 'L'} pnl={x['realized_pnl']}")
    print("\n--- UNRESOLVED ---")
    for x in out:
        if x.get("truth_win") is None:
            print(f"  {x['id']:4d} {x['status']} {x['market_ticker']} src={x['truth_src']} "
                  f"kres={x.get('kalshi_result')} temp={x.get('canonical_settled_f')} notes=?")


if __name__ == "__main__":
    main()
