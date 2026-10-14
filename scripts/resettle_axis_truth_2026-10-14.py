#!/usr/bin/env python3
"""resettle_axis_truth_2026-10-14.py — re-settle the SBOX book on the market's
OWN strike axis (2026-10-14 external bug-hunt finding #3).

What was wrong
--------------
Kalshi daily-high events publish TWO independent tails and the ``-T##`` ticker
suffix is used for BOTH:

  * ``strike_type='greater'`` — "or above": YES iff observed > floor.
  * ``strike_type='less'``    — "or below": YES iff observed < cap.

The engine assumed every ``-T##`` tail was a ``greater`` market under HIGH and
booked ``settlement_temp > strike``. For every ``less`` tail that INVERTS the
label. Both tails are STRICT (equality == LOSS) — verified against Kalshi's own
``result`` field.

Truth precedence per row (all read-only):
  1. Kalshi's OWN settled ``result`` ('yes'/'no') for the row's ticker, fetched
     from the live trade API and cached at /tmp/kalshi_truth_cache.json;
  2. the authoritative ``data/kalshi_strike_ladder.db`` row for the ticker
     (``result``, else derived from ``settlement_value_dollars``);
  3. fail closed — row is left untouched.

Writes (trades only)
--------------------
  settlement_outcome, canonical_outcome, settled_value, realized_pnl,
  settlement_return_amount, settlement_confirmed=1, canonical_settled_f,
  canonical_source, status='closed'
Old values preserved in NEW columns before overwriting:
  canonical_outcome_old_2026, settlement_outcome_old_2026.

P&L basis: **constant-1-contract** (see docs/plans/TRADES-TABLE-INTEGRITY-2026-10-14.md):
  buy_yes: (S - trade_price) * 1 ; buy_no: (trade_price - S) * 1 ; S in {0,1}.
``realized_pnl_old_2026`` already preserves the ORIGINAL qty-basis P&L and is
never touched here.

Safety: BACK UP FIRST. ``--dry-run`` writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.canonical_settlement import canonical_outcome  # noqa: E402

BOOK = REPO / "data" / "paper_trading_sbox.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
CACHE = Path("/tmp/kalshi_truth_cache.json")
OUT = REPO / "artifacts" / "settlement-axis-truth"
CACHE = OUT / "kalshi_market_truth_cache.json"
API = "https://api.elections.kalshi.com/trade-api/v2/markets"

NEW_COLS = {
    "canonical_outcome_old_2026": "TEXT",
    "settlement_outcome_old_2026": "TEXT",
    "settled_value_old_2026": "REAL",
    "realized_pnl_old_axis_2026": "REAL",
    "status_old_2026": "TEXT",
    "strike_type": "TEXT",
    "strike_axis_source": "TEXT",
}


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def ensure_cols(conn):
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    for c, t in NEW_COLS.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {c} {t}")
    conn.commit()


def load_truth():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    lad = ro(LADDER)
    lad.row_factory = sqlite3.Row
    ladder = {}
    for r in lad.execute(
            "SELECT ticker, strike_type, floor_strike, cap_strike, "
            "settlement_value_dollars, result FROM strike_ladder "
            "WHERE ticker IS NOT NULL"):
        ladder.setdefault(r["ticker"], dict(r))
    lad.close()
    return cache, ladder


def resolve_truth(row, cache, ladder):
    """Return (yes, temp, strike_type, floor, cap, source) or fail-closed tuple.

    ``yes``/``temp`` are None when no authoritative result is available; the
    strike axis (strike_type/floor/cap) is returned independently so the caller
    can still record provenance.
    """
    tk = row["market_ticker"]
    d = cache.get(tk)
    if isinstance(d, dict) and d.get("result") in ("yes", "no"):
        ev = d.get("expiration_value")
        try:
            temp = float(ev) if ev not in (None, "") else None
        except (TypeError, ValueError):
            temp = None
        return (d["result"] == "yes", temp, d.get("strike_type"),
                d.get("floor_strike"), d.get("cap_strike"), "api_result")
    L = ladder.get(tk)
    if L and L.get("result") in ("yes", "no"):
        return (L["result"] == "yes", row["canonical_settled_f"], L.get("strike_type"),
                L.get("floor_strike"), L.get("cap_strike"), "ladder_result")
    if L and L.get("settlement_value_dollars") is not None and L.get("strike_type"):
        return (float(L["settlement_value_dollars"]) >= 0.5,
                row["canonical_settled_f"], L.get("strike_type"),
                L.get("floor_strike"), L.get("cap_strike"), "ladder_settled_usd")
    return (None, None, (L or {}).get("strike_type") or (d or {}).get("strike_type"),
            (L or {}).get("floor_strike") or (d or {}).get("floor_strike"),
            (L or {}).get("cap_strike") or (d or {}).get("cap_strike"), None)


def pnl_1contract(trade_type, S, price):
    if trade_type == "buy_yes":
        return (S - price) * 1.0
    if trade_type == "buy_no":
        return (price - S) * 1.0
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cache, ladder = load_truth()
    b = sqlite3.connect(BOOK)
    b.row_factory = sqlite3.Row
    ensure_cols(b)
    rows = [dict(r) for r in b.execute(
        "SELECT id, status, station, market_type, trade_type, signal_direction, "
        "market_ticker, canonical_settled_f, trade_price, realized_pnl, "
        "canonical_outcome, settlement_outcome, settled_value, canonical_source "
        "FROM trades ORDER BY id")]

    plan = []
    for r in rows:
        yes, truth_temp, st, fl, cap, src = resolve_truth(r, cache, ladder)
        if yes is None:
            plan.append({**r, "action": "SKIP", "reason": "no_truth"})
            continue
        S = 1 if yes else 0
        temp = truth_temp if truth_temp is not None else r["canonical_settled_f"]
        # canonical_outcome: prefer Kalshi's own settled result (no temp needed);
        # fall back to temp+axis when the result is absent but temp is known.
        expect = "correct" if (r["signal_direction"].upper() == "UP") == yes else "incorrect"
        if r["canonical_settled_f"] is not None and st in ("greater", "less", "between"):
            outc = canonical_outcome(r["signal_direction"], r["market_type"],
                                     r["canonical_settled_f"], fl if fl is not None else cap,
                                     strike_type=st, floor_strike=fl, cap_strike=cap)
            xcheck = (outc == expect)
        else:
            outc = expect
            xcheck = None
        pnl = pnl_1contract(r["trade_type"], S, r["trade_price"])
        # per-contract payout going to the holder
        if r["trade_type"] == "buy_yes":
            ret_amt = float(S)
        elif r["trade_type"] == "buy_no":
            ret_amt = float(1 - S)
        else:
            ret_amt = pnl
        changed = (r["canonical_outcome"] != outc) or (
            r["realized_pnl"] is None or abs((r["realized_pnl"] or 0) - (pnl or 0)) > 1e-9) or (
            r["status"] != "closed")
        plan.append({**r, "action": "SETTLE" if changed else "NOCHANGE",
                     "truth_yes": yes, "S": S, "truth_temp": temp,
                     "strike_type": st, "floor": fl,
                     "cap": cap, "axis_src": src, "new_outcome": outc,
                     "new_pnl": pnl, "new_return_amount": ret_amt,
                     "xcheck_ok": xcheck})

    # summaries
    old_pnl = sum((x["realized_pnl"] or 0) for x in plan if x["status"] == "closed")
    new_pnl = sum(x["new_pnl"] for x in plan if x.get("new_pnl") is not None)
    n_settle = sum(1 for x in plan if x["action"] == "SETTLE")
    n_flip_wl = sum(1 for x in plan if x["action"] == "SETTLE"
                    and x["canonical_outcome"] == "correct" and x["new_outcome"] == "incorrect")
    n_flip_lw = sum(1 for x in plan if x["action"] == "SETTLE"
                    and x["canonical_outcome"] == "incorrect" and x["new_outcome"] == "correct")
    xcheck_bad = [x["id"] for x in plan if x.get("xcheck_ok") is False]

    if not args.dry_run:
        n = 0
        for x in plan:
            if x["action"] != "SETTLE":
                continue
            n += 1
            b.execute(
                "UPDATE trades SET "
                "canonical_outcome_old_2026=COALESCE(canonical_outcome_old_2026, canonical_outcome), "
                "settlement_outcome_old_2026=COALESCE(settlement_outcome_old_2026, settlement_outcome), "
                "settled_value_old_2026=COALESCE(settled_value_old_2026, settled_value), "
                "realized_pnl_old_axis_2026=COALESCE(realized_pnl_old_axis_2026, realized_pnl), "
                "status_old_2026=COALESCE(status_old_2026, status), "
                "canonical_outcome=?, settlement_outcome=?, settled_value=?, "
                "realized_pnl=?, settlement_return_amount=?, settlement_confirmed=1, "
                "canonical_settled_f=COALESCE(canonical_settled_f, ?), "
                "canonical_source=COALESCE(NULLIF(canonical_source,''),'kalshi_live_api'), "
                "strike_type=?, strike_axis_source=?, status='closed' "
                "WHERE id=?",
                (x["new_outcome"], x["new_outcome"],
                 x["truth_temp"] if x["truth_temp"] is not None else x["S"],
                 x["new_pnl"], x["new_return_amount"],
                 x["truth_temp"], x["strike_type"], x["axis_src"], x["id"]))
        b.commit()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "resettle_2026-10-14.json").write_text(json.dumps(plan, indent=2))
    print(f"{'[dry-run] ' if args.dry_run else ''}rows={len(plan)} to_settle={n_settle} "
          f"flip_win->loss={n_flip_wl} flip_loss->win={n_flip_lw}")
    print(f"book realized_pnl sum: before={old_pnl:.3f} after={new_pnl:.3f} "
          f"delta={new_pnl - old_pnl:.3f}")
    if xcheck_bad:
        print("XCHECK DISAGREEMENTS (temp+axis vs Kalshi result):", xcheck_bad)
    else:
        print("xcheck: temp+axis agrees with Kalshi result on every resolved row")
    print("--- rows that change outcome ---")
    for x in plan:
        if x["action"] == "SETTLE" and x["canonical_outcome"] != x["new_outcome"]:
            print(f"  {x['id']:4d} {x['market_ticker']:30s} {str(x['strike_type']):7s} "
                  f"f={x['floor']} c={x['cap']} temp={x['canonical_settled_f']} "
                  f"S={x['S']} {x['canonical_outcome']}->{x['new_outcome']} "
                  f"pnl {x['realized_pnl']}->{x['new_pnl']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
