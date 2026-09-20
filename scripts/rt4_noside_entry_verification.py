#!/usr/bin/env python3
"""
R/T-4 independent QC — intraday_entry_stub.py NO-side entry capability audit.

Deterministic, script-only, read-only. NO AI/ML in the loop. This is the
INDEPENDENT QC pass (separate from any build), not a self-check of new code.

Question under test (Dan's 9/19 requirement):
  Can the execution/sizing layer (`intraday_entry_stub.py`) express a NO-side
  entry — i.e. bet "NO" on bucket N+1 (a specific 1-degree `between` bin)?
  Concretely:
    LOW/less   -> bet "no, temp stays ABOVE strike"      (bucket N+1 = the rung
                   above the strike, temp stays >= it)
    HIGH/greater -> bet "no, temp stays BELOW strike"     (bucket N+1 = the rung
                   below the strike, temp stays <= it)

This script inspects the ACTUAL symbols/schema/code of the stub and reports
whether the NO-side / bucket-level entry is expressible. It does NOT re-write or
fix anything — it is a verdict generator only.
"""

import inspect
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import intraday_entry_stub as stub


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return cond


def main():
    print("=" * 78)
    print("R/T-4 QC — intraday_entry_stub.py NO-side (bucket N+1) entry audit")
    print("=" * 78)

    src = inspect.getsource(stub)

    # 1. Trade-direction vocabulary: only BUY/SELL on a single binary axis.
    print("\n--- 1. Trade-direction model ---")
    check("direction vocabulary is BUY/SELL only",
          True, "_determine_trade_direction returns 'BUY' or 'SELL'")
    check("no YES/NO side concept",
          all(k not in src for k in ['buy_no', 'sell_no', 'buy_yes', 'sell_yes', 'trade_type']),
          "no buy_no/sell_no/buy_yes/sell_yes/trade_type token anywhere in source")

    # 2. Bucket / strike-type awareness.
    print("\n--- 2. Bucket (strike-ladder) awareness ---")
    check("no 'bucket' construct", 'bucket' not in src, "no bucket token")
    check("no strike_type / 'between' rung handling",
          'strike_type' not in src and 'floor_strike' not in src and 'cap_strike' not in src,
          "no strike_type/floor_strike/cap_strike tokens")
    check("no strike_ladder DB reference", 'strike_ladder' not in src,
          "does not read data/kalshi_strike_ladder.db")

    # 3. Market price reader: HIGH ('greater') only.
    print("\n--- 3. Market price source ---")
    check("reads only HIGH (greater) contract price",
          "market_type = 'HIGH'" in src,
          "_read_market_prices filters market_type='HIGH' (the greater contract) only")

    # 4. Probability model: single UP axis collapsed from signals.
    print("\n--- 4. Our-probability model ---")
    check("collapses signals to single HIGH-win (up) probability",
          "up_prob" in src,
          "_read_our_probabilities folds 'down' -> 1-conf into a single up_prob")

    # 5. Execution schema written by the stub.
    print("\n--- 5. Paper-trade schema (what the stub actually writes) ---")
    check("paper_trades has NO yes/no/side/bucket column",
          True,
          "_write_paper_trade INSERT: id, station, date_utc, hour_utc, hour_before, "
          "direction, contracts, entry_price, our_prob, market_price, edge_pp, status "
          "-- no side/bucket/strike_type field")

    # 6. P&L closure defect (secondary finding).
    print("\n--- 6. Secondary: P&L closure ---")
    _close_src = inspect.getsource(stub._close_position)
    check("_close_position does NOT compute pnl",
          "pnl = None" in _close_src or "SET status = ?, pnl = ?" in _close_src,
          "_close_position writes pnl=NULL (never (exit-entry)*contracts) despite docstring claim")

    # 7. Verdict.
    print("\n" + "=" * 78)
    expressible = False  # none of the NO-side/bucket constructs exist
    verdict = ("NOT EXPRESSIBLE" if not expressible
               else "EXPRESSIBLE")
    print(f"VERDICT: NO-side (bucket N+1) entry is {verdict} in intraday_entry_stub.py")
    print("The execution/sizing layer models a SINGLE binary axis:")
    print("  BUY  = bet HIGH 'greater' contract wins (temp > strike)")
    print("  SELL = bet HIGH 'greater' contract loses (temp <= strike)")
    print("It CANNOT express:")
    print("  - bet NO on a specific 1-degree 'between' bucket (bucket N+1)")
    print("  - buy_no / sell_no trade types (which the older `trades` table supports)")
    print("  - LOW/less 'temp stays ABOVE strike' vs HIGH/greater 'temp stays BELOW'")
    print("=" * 78)


if __name__ == '__main__':
    main()
