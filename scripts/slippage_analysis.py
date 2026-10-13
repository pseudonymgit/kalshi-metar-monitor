#!/usr/bin/env python3
"""Slippage / break-even analysis over the live SBOX paper book (READ-ONLY).

Reads:
  data/kalshi_market_snapshots.db   -> top-of-book (yes_bid/yes_ask/mid) snapshots
  data/paper_trading_sbox.db        -> closed trades

For every CLOSED trade:
  1. nearest snapshot <= entry_time (created_at_utc) for its market_ticker
  2. side-adjusted entry_mid / entry_bid / entry_ask / entry_spread
     (buy_yes -> YES side; buy_no -> NO side = 1 - yes)
  3. price tier bucket (by entry_mid)
  4. slippage per contract = half the spread (long the side -> cross to the ask)
  5. break-even revaluation = probability points the mid must move to cover
     slippage + canonical Kalshi taker fee at that tier

Also computes the book's paper P&L vs a slippage+fee-adjusted P&L and the
entry-gate survivor counts if EV (net of cost) were required to be >= 0.

No DB writes. No temp dirs. Output < 2MB.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAP_DB = REPO / "data" / "kalshi_market_snapshots.db"
BOOK_DB = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "slippage_analysis.json"

TIERS = [
    ("0.01-0.05", 0.01, 0.05),
    ("0.05-0.10", 0.05, 0.10),
    ("0.10-0.25", 0.10, 0.25),
    ("0.25-0.50", 0.25, 0.50),
    ("0.50+", 0.50, 1.01),
]

# EV-net gates for sensitivity (per contract, probability points)
EV_GATES = [0.0, 0.02, 0.05, 0.10, 0.20]


def tier_of(mid: float) -> str:
    for name, lo, hi in TIERS:
        if lo <= mid < hi:
            return name
    return TIERS[-1][0]


def fee_per_contract(price: float) -> float:
    """Canonical Kalshi taker fee for one contract, dollars (core/fees.py)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def avg(xs):
    return sum(xs) / len(xs) if xs else None


def main() -> int:
    snap = open_ro(SNAP_DB)
    book = open_ro(BOOK_DB)

    trades = book.execute(
        """SELECT trade_uuid, station, market_type, signal_direction, trade_type,
                  market_ticker, created_at_utc, trade_date_utc,
                  market_price, trade_price, quantity, trade_cost,
                  realized_pnl, settlement_outcome, canonical_outcome,
                  analytical_prob, p_fused, forecast_prob, price_source
           FROM trades
           WHERE status='closed'
           ORDER BY created_at_utc"""
    ).fetchall()

    records = []
    unmatched = []
    for t in trades:
        ticker = t["market_ticker"]
        entry_time = t["created_at_utc"]
        qty = int(t["quantity"])
        paper_pnl = float(t["realized_pnl"]) if t["realized_pnl"] is not None else 0.0

        snap_row = snap.execute(
            """SELECT snapshot_time, yes_bid, yes_ask, mid_price
               FROM market_snapshots
               WHERE market_ticker = ? AND snapshot_time <= ?
               ORDER BY snapshot_time DESC LIMIT 1""",
            (ticker, entry_time),
        ).fetchone()
        if snap_row is None or snap_row["yes_bid"] is None or snap_row["yes_ask"] is None:
            unmatched.append(ticker)
            continue

        yes_bid = float(snap_row["yes_bid"])
        yes_ask = float(snap_row["yes_ask"])
        yes_mid = float(snap_row["mid_price"])

        is_yes = str(t["trade_type"]) == "buy_yes"
        # side-adjusted top of book
        if is_yes:
            side_bid, side_ask, side_mid = yes_bid, yes_ask, yes_mid
        else:  # buy_no -> NO side
            side_bid, side_ask, side_mid = 1.0 - yes_ask, 1.0 - yes_bid, 1.0 - yes_mid

        spread = side_ask - side_bid
        # long the side contract -> cross to the ask
        slippage_pc = max(side_ask - side_mid, 0.0)      # == spread/2
        fee_pc = fee_per_contract(side_ask)
        break_even_reval = slippage_pc + fee_pc

        # model probability of the trade winning
        p = t["p_fused"] if t["p_fused"] is not None else t["analytical_prob"]
        if p is None:
            p = t["forecast_prob"]
        p = float(p) if p is not None else float("nan")
        p_win = p if is_yes else (1.0 - p)

        ev_net_pc = p_win - side_ask - fee_pc          # hold-to-settlement EV/contract net of cost
        ev_mid_pc = p_win - side_mid                   # naive mid-fill EV/contract

        cost_total = (slippage_pc + fee_pc) * qty
        adj_pnl = paper_pnl - cost_total

        correct = str(t["canonical_outcome"] or t["settlement_outcome"]).lower() == "correct"

        records.append({
            "trade_uuid": t["trade_uuid"],
            "station": t["station"],
            "market_type": t["market_type"],
            "signal_direction": t["signal_direction"],
            "trade_type": t["trade_type"],
            "market_ticker": ticker,
            "entry_time": entry_time,
            "trade_date": t["trade_date_utc"],
            "snapshot_time": snap_row["snapshot_time"],
            "entry_mid": round(side_mid, 4),
            "entry_bid": round(side_bid, 4),
            "entry_ask": round(side_ask, 4),
            "entry_spread": round(spread, 4),
            "yes_mid": round(yes_mid, 4),
            "tier": tier_of(side_mid),
            "quantity": qty,
            "slippage_per_contract": round(slippage_pc, 4),
            "slippage_pct_of_entry": round(slippage_pc / side_mid * 100.0, 2) if side_mid else None,
            "fee_per_contract": round(fee_pc, 4),
            "break_even_reval_points": round(break_even_reval, 4),
            "p_win": round(p_win, 4),
            "ev_net_per_contract": round(ev_net_pc, 4),
            "ev_mid_per_contract": round(ev_mid_pc, 4),
            "paper_pnl": round(paper_pnl, 2),
            "slippage_adj_pnl": round(adj_pnl, 2),
            "pnl_delta": round(cost_total, 2),
            "correct": correct,
        })

    # ---------------- tier aggregation ----------------
    tiers_out = {}
    for name, _, _ in TIERS:
        grp = [r for r in records if r["tier"] == name]
        if not grp:
            tiers_out[name] = {"n_trades": 0}
            continue
        tiers_out[name] = {
            "n_trades": len(grp),
            "avg_entry_mid": round(avg([r["entry_mid"] for r in grp]), 4),
            "avg_entry_spread": round(avg([r["entry_spread"] for r in grp]), 4),
            "avg_slippage_per_contract": round(avg([r["slippage_per_contract"] for r in grp]), 4),
            "avg_slippage_pct_of_entry": round(sum(r["slippage_pct_of_entry"] for r in grp) / len(grp), 2),
            "avg_fee_per_contract": round(avg([r["fee_per_contract"] for r in grp]), 4),
            "avg_break_even_reval_points": round(avg([r["break_even_reval_points"] for r in grp]), 4),
            "total_contracts": sum(r["quantity"] for r in grp),
            "win_rate_pct": round(sum(1 for r in grp if r["correct"]) / len(grp) * 100.0, 1),
            "paper_pnl": round(sum(r["paper_pnl"] for r in grp), 2),
            "slippage_adj_pnl": round(sum(r["slippage_adj_pnl"] for r in grp), 2),
        }

    # ---------------- book-level P&L ----------------
    paper_total = sum(r["paper_pnl"] for r in records)
    adj_total = sum(r["slippage_adj_pnl"] for r in records)
    delta_total = sum(r["pnl_delta"] for r in records)

    # ---------------- entry-gate simulation ----------------
    gate_sensitivity = []
    for thr in EV_GATES:
        surv = [r for r in records if r["ev_net_per_contract"] >= thr]
        killed = [r for r in records if r["ev_net_per_contract"] < thr]
        gate_sensitivity.append({
            "threshold_ev_net": thr,
            "survivors": len(surv),
            "killed": len(killed),
            "survivor_rate_pct": round(len(surv) / len(records) * 100.0, 1) if records else None,
            "survivors_paper_pnl": round(sum(r["paper_pnl"] for r in surv), 2),
            "killed_paper_pnl": round(sum(r["paper_pnl"] for r in killed), 2),
            "survivors_slippage_adj_pnl": round(sum(r["slippage_adj_pnl"] for r in surv), 2),
            "survivor_win_rate_pct": round(sum(1 for r in surv if r["correct"]) / len(surv) * 100.0, 1) if surv else None,
        })

    survivors0 = [r for r in records if r["ev_net_per_contract"] >= 0.0]
    gate = {
        "rule": "ev_net_per_contract >= 0  (p_win - side_ask - fee >= 0)",
        "total_trades": len(records),
        "survivors": len(survivors0),
        "killed": len(records) - len(survivors0),
        "survivor_rate_pct": round(len(survivors0) / len(records) * 100.0, 1) if records else None,
        "survivors_paper_pnl": round(sum(r["paper_pnl"] for r in survivors0), 2),
        "survivors_slippage_adj_pnl": round(sum(r["slippage_adj_pnl"] for r in survivors0), 2),
    }

    out = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_snapshot_db": str(SNAP_DB.name),
            "source_book_db": str(BOOK_DB.name),
            "n_closed_trades": len(records),
            "n_unmatched": len(unmatched),
            "fee_model": "core/fees.py fee_per_contract = ceil(0.07*P*(1-P)*100)/100 (per contract, taker, at fill/ask)",
            "slippage_model": "half-spread; long side entry crosses to ask (buy_yes->YES ask, buy_no->NO ask=1-yes_bid)",
            "break_even_reval_definition": "probability points mid must move = slippage (half-spread) + entry taker fee",
            "paper_pnl_source": "book realized_pnl (recorded; mid-fill, zero fees, payoff {0,1})",
            "note": "Book records mid-fill with no fees; slippage-adjusted subtracts spread crossing + taker fee per contract.",
        },
        "tier_summary": tiers_out,
        "book_pnl": {
            "paper_pnl": round(paper_total, 2),
            "slippage_adj_pnl": round(adj_total, 2),
            "pnl_delta": round(delta_total, 2),
            "pnl_delta_pct_of_paper": round(delta_total / paper_total * 100.0, 1) if paper_total else None,
            "total_contracts": sum(r["quantity"] for r in records),
        },
        "entry_gate": {"primary": gate, "sensitivity": gate_sensitivity},
        "trades": records,
    }

    OUT.write_text(json.dumps(out, indent=2))
    size_mb = OUT.stat().st_size / 1e6
    print(f"wrote {OUT} ({size_mb:.3f} MB), {len(records)} trades, unmatched={len(unmatched)}")
    print(json.dumps({"book_pnl": out["book_pnl"], "entry_gate": out["entry_gate"]}, indent=2))
    print(json.dumps(tiers_out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
