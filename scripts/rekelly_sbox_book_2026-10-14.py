#!/usr/bin/env python3
"""rekelly_sbox_book_2026-10-14.py — re-derive SBOX book sizing under KELLY
(Workstream AF, Item 2).

The retired $/price ladder posed `quantity = position_size_usd / trade_price`,
and sized off `confidence = |p-0.5|*2` — producing absurd contract counts and a
19:1 win/loss asymmetry on cheap entries ($0.05 -> 200 contracts -> +$190 win vs
-$10 loss). Sizing is now standard binary Kelly on the CALIBRATED probability of
the BOOKED side vs the price paid:

    f* = (p - market_price) / market_price          (edge / market_price)
    position_size_usd = min(bankroll * f*, max_position)     ($200 / $10)

Same entries, honest sizing: this script does NOT add or drop trades. For every
row it re-derives position_size_usd, quantity, trade_cost and (for CLOSED rows)
realized_pnl / settlement_return_amount from the Kelly size, and preserves the
prior values in `*_prekelly` columns.

  * our_prob  = companion calibrated_p  (P(high>=strike), the YES side)
                -> booked side = buy_yes ? calibrated_p : 1 - calibrated_p
  * market_price = the booked side's entry price (trades.trade_price)
  * won       = canonical_outcome == 'correct' (fallback: signal vs settled)

Safety: BACK UP FIRST (the caller does cp); --dry-run writes nothing. Only the
`trades` table is written (plus additive `*_prekelly` columns).

CLI:
  python3 scripts/rekelly_sbox_book_2026-10-14.py --dry-run
  python3 scripts/rekelly_sbox_book_2026-10-14.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from sbox_intraday_ev_bridge import kelly_position_size  # noqa: E402

BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "artifacts" / "trades-integrity"


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def ensure_col(conn, table, col, decl):
    cols = {d[1] for d in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()


def _leg(trade_type, signal_direction):
    tt = (trade_type or "").lower()
    if tt == "buy_yes":
        return "yes"
    if tt == "buy_no":
        return "no"
    return "yes" if str(signal_direction).upper() == "UP" else "no"


def _calibrated_by_uuid(conn):
    """trade_uuid -> P(high>=strike) from the honest companion tables.

    `trade_forecaster_probs` carries the CURRENT honest recalibration; the older
    `trade_calibration` holds the retired bare-clamp values (0.2481/0.7519) and
    is only a fallback — same precedence as fix_trades_table_integrity.problem2.
    """
    out = {}
    for tbl in ("trade_forecaster_probs", "trade_calibration"):
        try:
            for tuuid, cp in conn.execute(
                    f"SELECT trade_uuid, calibrated_p FROM {tbl} "
                    "WHERE calibrated_p IS NOT NULL"):
                if tuuid not in out and cp is not None:
                    out[tuuid] = float(cp)
        except sqlite3.Error:
            continue
    return out


def rekelly(conn, dry_run=False):
    for col, decl in (("position_size_usd_prekelly", "REAL"),
                      ("realized_pnl_prekelly", "REAL")):
        ensure_col(conn, "trades", col, decl)
    calib = _calibrated_by_uuid(conn)
    rows = [dict(r) for r in conn.execute(
        """SELECT id, trade_uuid, station, market_type, signal_direction,
                  trade_type, quantity, trade_price, position_size_usd,
                  realized_pnl, status, canonical_outcome, settlement_outcome
             FROM trades ORDER BY id""")]

    out = {"rows": [], "missing_calibrated_p": [], "size_changed": 0,
           "size_unchanged": 0, "zero_edge": 0, "zero_edge_rows": [],
           "pnl_before": 0.0, "pnl_after": 0.0,
           "size_before": {}, "size_after": {}}
    for r in rows:
        price = float(r["trade_price"] or 0.0)
        cp = calib.get(r["trade_uuid"])
        old_size = float(r["position_size_usd"] or 0.0)
        before_sz = out["size_before"]
        before_sz[old_size] = before_sz.get(old_size, 0) + 1
        if cp is None or not (0.0 < price < 1.0):
            out["missing_calibrated_p"].append(r["id"])
            after_sz = out["size_after"]
            after_sz[old_size] = after_sz.get(old_size, 0) + 1
            continue
        leg = _leg(r["trade_type"], r["signal_direction"])
        our_prob = cp if leg == "yes" else 1.0 - cp
        new_size = kelly_position_size(our_prob, price)
        after_sz = out["size_after"]
        after_sz[new_size] = after_sz.get(new_size, 0) + 1
        if new_size == 0.0:
            out["zero_edge"] += 1
        if abs(new_size - old_size) < 1e-9:
            out["size_unchanged"] += 1
        else:
            out["size_changed"] += 1

        new_qty = max(1, int(round(new_size / price))) if new_size > 0 else 0
        new_cost = round(new_qty * price, 4)
        won = (str(r["canonical_outcome"] or "").lower() == "correct"
               or str(r["settlement_outcome"] or "").lower() == "correct")
        new_pnl = None
        new_ret = None
        if r["status"] == "closed":
            if new_size <= 0.0:
                # Kelly says: no edge at this price -> no position. Honest P&L is
                # 0.0 (we would not have held it), not the settled fiction.
                new_pnl = 0.0
                new_ret = 0.0
                out["zero_edge_rows"].append(
                    {"id": r["id"], "station": r["station"],
                     "calibrated_p": round(cp, 4), "our_prob": round(our_prob, 4),
                     "price": price, "leg": leg, "won": won,
                     "settled_pnl_preserved_in": "realized_pnl_prekelly"})
            elif leg == "yes":
                new_pnl = round(new_qty * (1.0 - price) if won else -new_qty * price, 4)
                new_ret = round(new_qty * (1.0 - price), 4) if won else 0.0
            else:
                new_pnl = round(new_qty * price if won else -new_qty * (1.0 - price), 4)
                new_ret = round(new_qty * price, 4) if won else 0.0
        old_pnl = r["realized_pnl"]
        out["pnl_before"] += float(old_pnl or 0.0)
        if new_pnl is not None:
            out["pnl_after"] += new_pnl

        rec = {"id": r["id"], "station": r["station"], "status": r["status"],
               "leg": leg, "calibrated_p": round(cp, 4),
               "our_prob": round(our_prob, 4), "price": price,
               "old_size": old_size, "new_size": new_size,
               "old_qty": r["quantity"], "new_qty": new_qty,
               "old_pnl": old_pnl, "new_pnl": new_pnl, "won": won}
        out["rows"].append(rec)
        if not dry_run:
            conn.execute(
                """UPDATE trades SET position_size_usd_prekelly=COALESCE(
                       position_size_usd_prekelly, position_size_usd),
                   realized_pnl_prekelly=COALESCE(realized_pnl_prekelly, realized_pnl),
                   position_size_usd=?, quantity=?, trade_cost=?,
                   realized_pnl=COALESCE(?, realized_pnl),
                   settlement_return_amount=COALESCE(?, settlement_return_amount)
                 WHERE id=?""",
                (new_size, new_qty, new_cost, new_pnl, new_ret, r["id"]))
    if not dry_run:
        conn.commit()
    out["pnl_before"] = round(out["pnl_before"], 2)
    out["pnl_after"] = round(out["pnl_after"], 2)
    out["size_before"] = {str(k): v for k, v in sorted(out["size_before"].items())}
    out["size_after"] = {str(k): v for k, v in sorted(out["size_after"].items())}
    out["n_rows"] = len(rows)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(BOOK)
    conn.row_factory = sqlite3.Row
    res = rekelly(conn, dry_run=a.dry_run)
    conn.close()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = "dry" if a.dry_run else "applied"
    (OUT / f"rekelly_{tag}.json").write_text(json.dumps(res, indent=2, default=str))
    brief = {k: v for k, v in res.items() if k != "rows"}
    print(json.dumps(brief, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
