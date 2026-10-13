#!/usr/bin/env python3
"""
bullshit_crawl.py — reality-check crawl of the live paper-trading book.

READ-ONLY over `data/paper_trading_sbox.db`. For every row in `trades`, flag any
number that fails a basic reality check. No DB is modified; no temp DBs.

Rules (a row may trip several):
  R1  realized_pnl > 200 * 10                 -> "bankroll/position mismatch"
  R2  position_size_usd > 25                  -> "over max position"
  R3  position_size_usd > 0 AND p_fused < 0.6 -> "low conf but entered"
  R4  p_fused >= 0.9                          -> "S grade on flat signal"
                                                  (F4: 0.9 and 0.6 same outcome)
  R5  (p_fused - market_price)/market_price > 0.50 -> "extreme edge"

Output: data/bullshit_crawl.json  (table per flagged trade + counts).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "bullshit_crawl.json"

BANKROLL = 200.0            # the $200 book
BANKROLL_MULT = 10.0        # R1: pnl > bankroll * mult
MAX_POSITION_USD = 25.0     # R2
LOW_CONF = 0.6              # R3
S_GRADE_P = 0.9             # R4
EXTREME_EDGE = 0.50         # R5


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main() -> None:
    con = _ro(BOOK)
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    con.close()

    flagged = []
    tally = {
        "bankroll/position mismatch": 0,
        "over max position": 0,
        "low conf but entered": 0,
        "S grade on flat signal": 0,
        "extreme edge": 0,
    }
    for r in rows:
        pnl = r.get("realized_pnl")
        pos = r.get("position_size_usd") or 0.0
        pf = r.get("p_fused")
        mkt = r.get("market_price")
        reasons = []
        values = {}

        # R1 — P&L exceeds 10x the bankroll
        if pnl is not None and pnl > BANKROLL * BANKROLL_MULT:
            reasons.append("bankroll/position mismatch")
            values["realized_pnl"] = pnl
            values["bankroll_x10"] = round(BANKROLL * BANKROLL_MULT, 2)

        # R2 — position over the $25 cap
        if pos > MAX_POSITION_USD:
            reasons.append("over max position")
            values["position_size_usd"] = pos

        # R3 — size put on a low-confidence fused probability
        if pos > 0 and pf is not None and pf < LOW_CONF:
            reasons.append("low conf but entered")
            values["position_size_usd"] = pos
            values["p_fused"] = pf

        # R4 — S-grade on a flat signal (0.9 and 0.6 settle the same per F4)
        if pf is not None and pf >= S_GRADE_P:
            reasons.append("S grade on flat signal")
            values["p_fused"] = pf

        # R5 — edge so extreme it is implausible
        if pf is not None and mkt not in (None, 0):
            edge = (pf - mkt) / mkt
            if edge > EXTREME_EDGE:
                reasons.append("extreme edge")
                values["p_fused"] = pf
                values["market_price"] = mkt
                values["edge_pct"] = round(100 * edge, 1)

        for rule in reasons:
            tally[rule] += 1

        if reasons:
            flagged.append({
                "trade_uuid": r.get("trade_uuid"),
                "station": r.get("station"),
                "trade_date_utc": r.get("trade_date_utc"),
                "status": r.get("status"),
                "settlement_outcome": r.get("settlement_outcome"),
                "rules": reasons,
                "values": values,
            })

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": str(BOOK.relative_to(REPO)),
        "bankroll_usd": BANKROLL,
        "total_trades": len(rows),
        "flagged_trades": len(flagged),
        "clean_trades": len(rows) - len(flagged),
        "flagged_pct": round(100 * len(flagged) / len(rows), 1) if rows else 0.0,
        "flags_per_rule": tally,
        "rule_definitions": {
            "bankroll/position mismatch": f"realized_pnl > {BANKROLL} x {BANKROLL_MULT} = {BANKROLL * BANKROLL_MULT:.0f}",
            "over max position": f"position_size_usd > {MAX_POSITION_USD}",
            "low conf but entered": f"position_size_usd > 0 and p_fused < {LOW_CONF}",
            "S grade on flat signal": f"p_fused >= {S_GRADE_P} (F4: 0.9 and 0.6 settle the same)",
            "extreme edge": f"(p_fused - market_price)/market_price > {EXTREME_EDGE}",
        },
        "flagged": flagged,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"bullshit crawl: {len(rows)} trades, {len(flagged)} flagged "
          f"({result['flagged_pct']}%), {len(rows) - len(flagged)} clean")
    for rule, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {rule:32} {n}")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
