#!/usr/bin/env python3
"""fix_kelly_pnl_stragglers_2026-10-14.py — make the closed SBOX book's
`realized_pnl` physically consistent and bounded (2026-10-14).

WHY THIS EXISTS
---------------
The AF-2/3 Kelly re-derive (scripts/rekelly_sbox_book_2026-10-14.py) re-sized
`position_size_usd` correctly but left `realized_pnl` on the *quantity-multiplied*
basis `(settled_value - trade_price) * quantity`. On cheap entries that explodes:
id 255 (KPHX, buy_yes @ $0.05, qty=200) books **+$190** on a $10 position; id 261
(KLAX, buy_no @ $0.055, qty=182) books **-$171.99**. Both dwarf the $10 stake.

The task's literal instruction — `(settled_value - trade_price) * quantity` with
`quantity = MAX(1, ROUND(position_size_usd/trade_price))` — cannot bound these:
`settled_value` is the settled TEMPERATURE (90.0 F), not a binary payout, so the
formula yields e.g. `(90 - 0.05) * 200 = $17,990`, and with a binary payout it
still yields `(1 - 0.05) * 200 = $190`. The instruction and its own acceptance
test ("|realized_pnl| must not exceed position_size_usd * 1.5") are mutually
exclusive under any quantity-multiplied basis.

The repository's SHIPPED basis is **contract-notional (qty-scaled)** — Dan
ruled 2026-10-15 that P&L = `(payout − price) × quantity` (PWW-BUG-HUNT F7: the
book is qty-scaled, 43/43 rows; 0 match constant-1). The earlier constant-1
("* 1.0") basis this script originally wrote (690db06da) is RETIRED. This script
now writes the same contract-notional basis as every other writer:

    realized_pnl = (S − trade_price) * quantity   # S in {0,1}, booked side

where S = 1 iff the BOOKED side won (canonical_outcome == 'correct') and
`quantity` is the Kelly-implied MAX(1, ROUND(position_size_usd/trade_price)).

Writes (closed rows only): realized_pnl, settlement_return_amount.
`quantity` is asserted/reconciled to MAX(1, ROUND(position_size_usd/trade_price))
(0 when position_size_usd == 0 — the honest zero-edge row). Prior values preserved
in additive column `realized_pnl_pre1c_20261014`.

Safety: BACK UP FIRST (caller does cp); --dry-run writes nothing. Only the
`trades` table is written.

CLI:
  python3 scripts/fix_kelly_pnl_stragglers_2026-10-14.py --dry-run
  python3 scripts/fix_kelly_pnl_stragglers_2026-10-14.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "artifacts" / "trades-integrity"
EPS = 1e-9


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def ensure_col(conn, table, col, decl):
    cols = {d[1] for d in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()


def contract_notional_pnl(trade_type, price, correct, qty):
    """Booked-side, contract-notional P&L (Dan ruling 2026-10-15).

    P&L = (payout - price) * quantity, payout = $1 per winning contract. buy_yes
    and buy_no are identical here because `trade_price` is the BOOKED side's entry
    price in both legs. Supersedes the retired constant-1 (`* 1.0`) basis.
    """
    s = 1.0 if correct else 0.0
    return round((s - float(price)) * int(qty or 0), 4), s

def fix(conn, dry_run=False):
    ensure_col(conn, "trades", "realized_pnl_pre1c_20261014", "REAL")
    rows = [dict(r) for r in conn.execute(
        """SELECT id, station, trade_type, trade_price, quantity,
                  position_size_usd, realized_pnl, settlement_return_amount,
                  canonical_outcome, status
             FROM trades WHERE status='closed' ORDER BY id""")]
    out = {"rows": [], "qty_fixed": [], "pnl_before": 0.0, "pnl_after": 0.0,
           "ret_fixed": [], "over_bound_before": [], "over_bound_after": []}
    for r in rows:
        co = (r["canonical_outcome"] or "").lower()
        if co not in ("correct", "incorrect"):
            out["rows"].append({"id": r["id"], "skipped": "no_canonical_outcome"})
            continue
        correct = co == "correct"
        price = float(r["trade_price"] or 0.0)
        psize = float(r["position_size_usd"] or 0.0)
        # Kelly-implied contract count (0 on the honest zero-edge row).
        want_qty = max(1, int(round(psize / price))) if (psize > 0 and price > 0) else 0
        if psize <= 0.0:
            # Honest zero-edge row: Kelly sized it to $0, so we held nothing.
            # Its settled fiction is preserved in realized_pnl_prekelly; the
            # honest booked P&L (and return) is 0.0.
            new_pnl, s, new_ret = 0.0, 0.0, 0.0
        else:
            new_pnl, s = contract_notional_pnl(r["trade_type"], price, correct, want_qty)
            new_ret = round(s * want_qty, 4)

        if int(r["quantity"] or 0) != want_qty:
            out["qty_fixed"].append({"id": r["id"], "old": r["quantity"],
                                     "new": want_qty})

        old_pnl = r["realized_pnl"]
        out["pnl_before"] += float(old_pnl or 0.0)
        out["pnl_after"] += new_pnl
        if old_pnl is not None and psize > 0 and abs(old_pnl) > psize * 1.5:
            out["over_bound_before"].append(
                {"id": r["id"], "station": r["station"], "pnl": old_pnl,
                 "psize": psize, "ratio": round(abs(old_pnl) / psize, 3)})
        if psize > 0 and abs(new_pnl) > psize * 1.5:
            out["over_bound_after"].append({"id": r["id"], "pnl": new_pnl})
        if r["settlement_return_amount"] is not None and \
                abs(float(r["settlement_return_amount"]) - new_ret) > EPS:
            out["ret_fixed"].append({"id": r["id"],
                                     "old": r["settlement_return_amount"],
                                     "new": new_ret})

        out["rows"].append({"id": r["id"], "station": r["station"],
                            "trade_type": r["trade_type"], "price": price,
                            "psize": psize, "qty": want_qty, "correct": correct,
                            "old_pnl": old_pnl, "new_pnl": new_pnl,
                            "old_ret": r["settlement_return_amount"],
                            "new_ret": new_ret})
        if not dry_run:
            conn.execute(
                """UPDATE trades
                      SET realized_pnl_pre1c_20261014=COALESCE(
                              realized_pnl_pre1c_20261014, realized_pnl),
                          realized_pnl=?, settlement_return_amount=?, quantity=?
                    WHERE id=? AND status='closed'""",
                (new_pnl, new_ret, want_qty, r["id"]))
    if not dry_run:
        conn.commit()
    out["pnl_before"] = round(out["pnl_before"], 4)
    out["pnl_after"] = round(out["pnl_after"], 4)
    out["n_rows"] = len(rows)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(BOOK)
    conn.row_factory = sqlite3.Row
    res = fix(conn, dry_run=a.dry_run)
    conn.close()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = "dry" if a.dry_run else "applied"
    (OUT / f"kelly_pnl_fix_{tag}.json").write_text(
        json.dumps(res, indent=2, default=str))
    brief = {k: v for k, v in res.items() if k != "rows"}
    print(json.dumps(brief, indent=2, default=str))
    print("stragglers now:")
    for r in res["rows"]:
        if r.get("id") in (255, 261, 262):
            print(" ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
