#!/usr/bin/env python3
"""
rebuild_sbox_book_resized.py — purge the historical $50 positions from the SBOX
book by building a CLEAN COPY with the 2026-10-12 sizing rules applied.

Dan's rules ($200 bankroll, 2026-10-12):
  - regular trades        $5-10
  - high-confidence       $25 max (sure things)

The old book rows were booked with the retired 2026-10-01 ladder
(>0.85 -> $50, >0.7 -> $25, >0.5 -> $10), which put 25% of a $200 bankroll on a
single trade 46 times. This script does NOT mutate the source book: it derives a
new DB and writes it next to the source.

Reduction is size-preserving in P&L terms: each trade's contract count is scaled
by new_size/old_size, and per-contract cash flows are recomputed from the
unchanged per-contract economics (settlement value and trade price). P&L per
contract stays constant, so the expected-value *rate* of the book is unchanged —
only the capital-at-risk (position size) is reduced to the new cap.

CLI:
    python3 scripts/rebuild_sbox_book_resized.py            # write the new book
    python3 scripts/rebuild_sbox_book_resized.py --report   # distribution only
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from sbox_intraday_ev_bridge import kelly_position_size  # noqa: E402

SBOX_DB = REPO / "data" / "paper_trading_sbox.db"


def _new_size(our_prob: float, market_price: float) -> float:
    """Kelly size for the booked side, exposed for tests/import (AF 2026-10-14)."""
    return kelly_position_size(our_prob, market_price)


def rebuild(src: Path, dst: Path) -> dict:
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)  # 1:1 clone, then rewrite only the sizing fields

    c = sqlite3.connect(str(dst))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, position_size_usd, quantity, trade_price, settled_value, "
        "realized_pnl, confidence_indicator, status, market_type, trade_type, "
        "forecast_prob, market_price "
        "FROM trades"
    ).fetchall()

    dist: dict = {}
    resized = 0
    pnl_before = pnl_after = 0.0
    for r in rows:
        p, mp = r["forecast_prob"], r["market_price"]
        if p is None or mp is None:
            continue
        new_size = _new_size(float(p), float(mp))
        dist[new_size] = dist.get(new_size, 0) + 1
        old_size = float(r["position_size_usd"])
        qty = int(r["quantity"] or 0)
        price = float(r["trade_price"] or 0.0)
        if old_size > 0 and qty > 0:
            new_qty = int(round(qty * new_size / old_size))
        else:
            new_qty = qty

        # Per-contract cash flows recomputed from unchanged per-contract economics.
        new_cost = round(new_qty * price, 4)
        ret = None
        pnl = r["realized_pnl"]
        if pnl is not None and qty > 0:
            pnl_before += pnl
            per_contract = pnl / qty
            pnl = round(per_contract * new_qty, 4)
            pnl_after += pnl
            settled = r["settled_value"]
            if r["status"] == "closed" and settled is not None:
                if pnl > 0:
                    # winning long-YES payoff: qty * (1 - price)
                    ret = round(new_qty * (1.0 - price), 4)
                else:
                    ret = 0.0

        c.execute(
            "UPDATE trades SET quantity=?, position_size_usd=?, trade_cost=?, "
            "realized_pnl=?, settlement_return_amount=? WHERE id=?",
            (new_qty, new_size, new_cost, pnl, ret, r["id"]),
        )
        resized += 1

    c.commit()
    c.close()
    return {"dst": str(dst), "rows": len(rows), "resized": resized,
            "distribution": dist,
            "pnl_before": round(pnl_before, 2), "pnl_after": round(pnl_after, 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SBOX_DB))
    ap.add_argument("--dst", default=str(SBOX_DB.with_name("paper_trading_sbox_resized.db")))
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if args.report:
        c = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT forecast_prob, market_price FROM trades").fetchall()
        c.close()
        dist: dict = {}
        for p, mp in rows:
            s = _new_size(float(p), float(mp)) if (p is not None and mp is not None) else 0.0
            dist[s] = dist.get(s, 0) + 1
        print("new-size distribution:", dict(sorted(dist.items(), reverse=True)))
        return 0

    res = rebuild(src, Path(args.dst))
    print(f"wrote {res['dst']}  ({res['resized']}/{res['rows']} rows resized)")
    print("new-size distribution:", dict(sorted(res["distribution"].items(), reverse=True)))
    print(f"book P&L: ${res['pnl_before']:.2f} -> ${res['pnl_after']:.2f} "
          f"(per-contract rate preserved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
