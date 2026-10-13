#!/usr/bin/env python3
"""dual_grade.py — dual-axis grade for each SETTLED trade.

Two INDEPENDENT axes; the composite is the MIN of the two, so a trade strong on
one axis can never mask weakness on the other. READ-ONLY.

Axis 1 — FINANCIAL: spread/entry_mid from data/kalshi_market_snapshots.db
    F > 0.25 | C 0.15-0.25 | B 0.10-0.15 | A 0.05-0.10 | S < 0.05
  also position/bankroll: > 10% -> F (worst of the two sub-grades)

Axis 2 — METEOROLOGICAL: station rate_1h separation (pp) from the ASOS cohort
    (scripts/_rate1h_sep.py, mirrors sweep_intraday_rate1h_crossing.json)
    F < 5 | C 5-15 | B 15-30 | A 30-50 | S > 50
  downgrade one step when |p_gefs - p_analog| > 0.10

Composite = MIN(fin, met).  Met=A + Fin=F = no-go.

Output: data/dual_grade.json
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rate1h_sep as R  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SNAP = REPO / "data" / "kalshi_market_snapshots.db"
OUT = REPO / "data" / "dual_grade.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
DOWNGRADE_GAP = 0.10
DEPTH_F = 0.10

ORDER = ["F", "C", "B", "A", "S"]     # worst -> best
DOWN = {"F": "F", "C": "F", "B": "C", "A": "B", "S": "A"}

_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def worst(a, b):
    return a if ORDER.index(a) <= ORDER.index(b) else b


def nearest_grid_hour(h):
    c = [g for g in GRID_HOURS if g <= h]
    return c[-1] if c else GRID_HOURS[0]


def _ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def grade_spread(sr):
    if sr is None:
        return "F"
    if sr > 0.25:
        return "F"
    if sr >= 0.15:
        return "C"
    if sr >= 0.10:
        return "B"
    if sr >= 0.05:
        return "A"
    return "S"


def grade_depth(df):
    return "F" if df > DEPTH_F else "S"


def grade_met(sep_pp, gap):
    if sep_pp is None:
        g = "F"
    elif sep_pp < 5:
        g = "F"
    elif sep_pp < 15:
        g = "C"
    elif sep_pp < 30:
        g = "B"
    elif sep_pp <= 50:
        g = "A"
    else:
        g = "S"
    downgraded = False
    if gap is not None and gap > DOWNGRADE_GAP:
        g = DOWN[g]
        downgraded = True
    return g, downgraded


def main():
    idx = R.build()
    bank = float(_ro(BOOK).execute(
        "SELECT closing_balance FROM daily_balances ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()[0])

    book = _ro(BOOK)
    trades = [dict(r) for r in book.execute(
        "SELECT * FROM trades WHERE settlement_confirmed=1 AND canonical_outcome IS NOT NULL "
        "ORDER BY trade_date_utc, id")]
    snp = _ro(SNAP)

    per_trade = []
    gate = Counter()
    for t in trades:
        st = t["station"]
        ts = datetime.fromisoformat(t["created_at_utc"])
        win = nearest_grid_hour(ts.hour)

        # Axis 1 — financial
        row = snp.execute(
            "SELECT snapshot_time, yes_bid, yes_ask, mid_price FROM market_snapshots "
            "WHERE market_ticker=? AND snapshot_time<=? ORDER BY snapshot_time DESC LIMIT 1",
            (t["market_ticker"], t["created_at_utc"])).fetchone()
        spread_ratio = None
        if row and row["mid_price"]:
            mid = float(row["mid_price"])
            if mid > 0:
                spread_ratio = (float(row["yes_ask"]) - float(row["yes_bid"])) / mid
        pos = t["position_size_usd"] or 0.0
        depth_frac = pos / bank if bank else 0.0
        g_spread = grade_spread(spread_ratio)
        g_depth = grade_depth(depth_frac)
        fin = worst(g_spread, g_depth)

        # Axis 2 — meteorological
        sep_pp, sep_src = R.sep_for_trade(idx, st, win)
        gap = None
        if t["p_gefs"] is not None and t["p_analog"] is not None:
            gap = abs(float(t["p_gefs"]) - float(t["p_analog"]))
        met, downgraded = grade_met(sep_pp, gap)

        composite = worst(fin, met)
        if composite == "F":
            g = "skip"
        elif composite == "C":
            g = "reduce"
        else:
            g = "enter"
        gate[g] += 1

        per_trade.append({
            "trade_uuid": t["trade_uuid"],
            "station": st,
            "date": t["trade_date_utc"],
            "entry_window": f"{win:02d}Z",
            "spread_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
            "depth_frac": round(depth_frac, 4),
            "grade_fin_spread": g_spread,
            "grade_fin_depth": g_depth,
            "grade_financial": fin,
            "rate1h_sep_pp": sep_pp,
            "sep_source": sep_src,
            "gefs_analog_gap": round(gap, 4) if gap is not None else None,
            "met_downgraded": downgraded,
            "grade_meteorological": met,
            "composite": composite,
            "gate": g,
            "actual_outcome": t["canonical_outcome"],
            "realized_pnl": t["realized_pnl"],
        })
    snp.close()

    comp_dist = Counter(r["composite"] for r in per_trade)
    fin_dist = Counter(r["grade_financial"] for r in per_trade)
    met_dist = Counter(r["grade_meteorological"] for r in per_trade)

    skip_bad = sum(1 for r in per_trade if r["gate"] == "skip" and r["actual_outcome"] == "incorrect")
    skip_n = gate["skip"]
    enter_ok = sum(1 for r in per_trade if r["gate"] == "enter" and r["actual_outcome"] == "correct")
    enter_n = gate["enter"]
    # P&L retained by the gate
    total_pnl = sum(r["realized_pnl"] or 0 for r in per_trade)
    gated_pnl = sum(r["realized_pnl"] or 0 for r in per_trade if r["gate"] != "skip")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "data/paper_trading_sbox.db (settled only, read-only)",
        "bankroll_usd": round(bank, 2),
        "settled_trades": len(per_trade),
        "axes": {
            "financial": {
                "spread_ratio": {"F": ">0.25", "C": "0.15-0.25", "B": "0.10-0.15",
                                 "A": "0.05-0.10", "S": "<0.05"},
                "position_over_bankroll_gt": DEPTH_F,
            },
            "meteorological": {
                "rate1h_sep_pp": {"F": "<5", "C": "5-15", "B": "15-30",
                                  "A": "30-50", "S": ">50"},
                "downgrade_when_gefs_analog_gap_gt": DOWNGRADE_GAP,
            },
            "composite": "MIN(financial, meteorological)",
        },
        "composite_distribution": dict(comp_dist),
        "financial_distribution": dict(fin_dist),
        "meteorological_distribution": dict(met_dist),
        "gate": {"enter": gate["enter"], "reduce": gate["reduce"], "skip": gate["skip"]},
        "gate_quality": {
            "entered_that_were_correct": f"{enter_ok}/{enter_n}",
            "skipped_that_were_incorrect": f"{skip_bad}/{skip_n}",
        },
        "pnl_all_settled": round(total_pnl, 2),
        "pnl_after_gate_skip": round(gated_pnl, 2),
        "pnl_delta_from_gate": round(gated_pnl - total_pnl, 2),
        "per_trade": per_trade,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"settled={len(per_trade)} bankroll=${bank:.2f}")
    print("composite dist:", dict(comp_dist), "| fin:", dict(fin_dist), "| met:", dict(met_dist))
    print("gate:", dict(gate),
          f"| enter&correct {enter_ok}/{enter_n} | skip&incorrect {skip_bad}/{skip_n}")
    print(f"P&L all ${total_pnl:.2f} -> after skip ${gated_pnl:.2f} (delta ${gated_pnl-total_pnl:+.2f})")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
