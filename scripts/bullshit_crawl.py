#!/usr/bin/env python3
"""bullshit_crawl.py — crawl the LIVE paper-trading book for numbers that fail a
basic reality check. READ-ONLY.

Every row in data/paper_trading_sbox.db is inspected; a row may trip several
rules. Reported per flagged trade: rule, value.

Rules (exactly as specified):
  R1  realized_pnl > 200 * 10                              -> "pnl over cap"
  R2  position_size_usd > 25                               -> "over max position"
  R3  position_size_usd > 0 and p_fused < 0.6              -> "low conf but entered"
  R4  p_fused >= 0.9                                       -> "S grade on flat signal"
  R5  (p_fused - market_price)/market_price > 0.50         -> "extreme edge"
  R6  position (quantity) > 25% of the strike open_interest
      (latest snapshot <= entry, data/kalshi_market_snapshots.db)
                                                           -> "over-market position"

Output: data/bullshit_crawl.json
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SNAP = REPO / "data" / "kalshi_market_snapshots.db"
OUT = REPO / "data" / "bullshit_crawl.json"

PNL_CAP = 200 * 10        # 2000
MAX_POSITION_USD = 25.0
LOW_CONF = 0.6
GRADE_S_P = 0.9
EXTREME_EDGE = 0.50
OI_FRACTION = 0.25


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def bankroll() -> float:
    con = _ro(BOOK)
    r = con.execute(
        "SELECT closing_balance FROM daily_balances ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()
    con.close()
    return float(r[0]) if r else 0.0


def snapshot_at_or_before(con, ticker, entry_iso):
    if not ticker or not entry_iso:
        return None
    r = con.execute(
        "SELECT snapshot_time, yes_bid, yes_ask, mid_price, open_interest "
        "FROM market_snapshots WHERE market_ticker=? AND snapshot_time<=? "
        "ORDER BY snapshot_time DESC LIMIT 1",
        (ticker, entry_iso),
    ).fetchone()
    return dict(r) if r else None


def main():
    bank = bankroll()
    con = _ro(BOOK)
    rows = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY id")]
    con.close()
    snap = _ro(SNAP)

    flagged = []
    for r in rows:
        rules = []
        values = {}

        pnl = r.get("realized_pnl")
        pos = r.get("position_size_usd") or 0.0
        pf = r.get("p_fused")
        price = r.get("market_price")
        qty = r.get("quantity") or 0

        # R1
        if pnl is not None and pnl > PNL_CAP:
            rules.append("pnl over cap")
            values["realized_pnl"] = round(pnl, 2)

        # R2
        if pos > MAX_POSITION_USD:
            rules.append("over max position")
            values["position_size_usd"] = pos

        # R3
        if pos > 0 and pf is not None and pf < LOW_CONF:
            rules.append("low conf but entered")
            values["p_fused"] = pf
            values["position_size_usd"] = pos

        # R4
        if pf is not None and pf >= GRADE_S_P:
            rules.append("S grade on flat signal")
            values["p_fused"] = pf

        # R5
        if pf is not None and price not in (None, 0):
            edge = (pf - price) / price
            if edge > EXTREME_EDGE:
                rules.append("extreme edge")
                values["edge_pct"] = round(100 * edge, 1)
                values["market_price"] = price

        # R6
        srow = snapshot_at_or_before(snap, r.get("market_ticker"), r.get("created_at_utc"))
        if srow and srow.get("open_interest"):
            oi = float(srow["open_interest"])
            if oi > 0 and qty > OI_FRACTION * oi:
                rules.append("over-market position")
                values["quantity_pct_of_oi"] = round(100 * qty / oi, 2)
                values["open_interest"] = oi

        if rules:
            flagged.append({
                "trade_uuid": r.get("trade_uuid"),
                "station": r.get("station"),
                "trade_date_utc": r.get("trade_date_utc"),
                "market_ticker": r.get("market_ticker"),
                "rules": rules,
                "values": values,
            })
    snap.close()

    total = len(rows)
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "data/paper_trading_sbox.db (read-only)",
        "bankroll_usd": round(bank, 2),
        "total_trades": total,
        "flagged": len(flagged),
        "passed": total - len(flagged),
        "flag_pct": round(100 * len(flagged) / total, 1) if total else 0.0,
        "rule_definitions": {
            "pnl over cap": f"realized_pnl > {PNL_CAP} (200 x 10)",
            "over max position": f"position_size_usd > {MAX_POSITION_USD}",
            "low conf but entered": f"position_size_usd > 0 and p_fused < {LOW_CONF} (per grade F)",
            "S grade on flat signal": f"p_fused >= {GRADE_S_P} (F4: 0.9 and 0.6 same outcome)",
            "extreme edge": f"(p_fused - market_price)/market_price > {EXTREME_EDGE}",
            "over-market position": f"quantity > {OI_FRACTION:.0%} of strike open_interest",
        },
        "rule_hit_counts": {k: sum(1 for f in flagged if k in f["rules"])
                            for k in ("pnl over cap", "over max position",
                                      "low conf but entered", "S grade on flat signal",
                                      "extreme edge", "over-market position")},
        "flagged_trades": flagged,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"bankroll ${bank:.2f} | {total} book rows | {len(flagged)} flagged, "
          f"{total - len(flagged)} passed ({result['flag_pct']}% flagged)")
    print("rule hit counts:", result["rule_hit_counts"])
    print(f"{'trade_uuid':38} {'station':6} {'date':10} {'rules'}")
    for f in flagged:
        print(f"{f['trade_uuid'][:36]:38} {f['station']:6} {f['trade_date_utc']:10} "
              f"{', '.join(f['rules'])}  {json.dumps(f['values'])}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
