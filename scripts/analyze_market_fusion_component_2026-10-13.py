#!/usr/bin/env python3
"""
Market-mid fusion component analysis (2026-10-13)
=================================================
Answers the F2/weighted-fusion question: does adding the market mid as a 4th
fusion component change the composite-confidence distribution, and how many
booked trades re-grade?

For each booked trade with a stored p_gefs/p_analog/p_trajectory and a ticker:
  p_market  = YES mid at the snapshot nearest the 13:30 station-local decision pin
  conf_3    = composite confidence over {gefs, analog, traj}   (equal weights)
  conf_4    = composite confidence over {gefs, analog, traj, market}
  grade     = Dan 2026-10-12 ladder on the composite confidence

Read-only. Done off `paper_trading_sbox.db` + `kalshi_market_snapshots.db`.

CLI: python3 scripts/analyze_market_fusion_component_2026-10-13.py [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    composite_confidence, grade_from_confidence, GRADE_LADDER,
)

SBOX = REPO / "data" / "paper_trading_sbox.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
GRADES = [g for g, _t, _c in GRADE_LADDER]


def pin(station, day):
    try:
        from scripts.sbox_intraday_ev_bridge import pinned_decision_utc
        return pinned_decision_utc(station, day)
    except Exception:
        return None


def p_market(snaps, ticker, pin_iso):
    if not ticker or not pin_iso:
        return None
    row = snaps.execute(
        "SELECT mid_price FROM market_snapshots WHERE market_ticker=? "
        "AND mid_price IS NOT NULL "
        "ORDER BY ABS(julianday(snapshot_time)-julianday(?)) ASC LIMIT 1",
        (ticker, pin_iso)).fetchone()
    if not row or row[0] is None:
        return None
    v = float(row[0])
    return v if 0.0 <= v <= 1.0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sb = sqlite3.connect(f"file:{SBOX}?mode=ro", uri=True)
    sn = sqlite3.connect(f"file:{SNAPS}?mode=ro", uri=True)

    rows = sb.execute(
        "SELECT t.trade_uuid, t.station, t.trade_date_utc, t.market_ticker, "
        "t.p_gefs, t.p_analog, t.p_trajectory, t.p_fused, t.canonical_outcome "
        "FROM trades t WHERE t.p_gefs IS NOT NULL").fetchall()

    per = []
    grade_3 = {g: 0 for g in GRADES}
    grade_4 = {g: 0 for g in GRADES}
    re_graded = 0
    have_market = 0
    conf3_sum = conf4_sum = 0.0
    for (uuid, st, day, tk, pg, pa, pt, pf, outcome) in rows:
        pi = pin(st, day)
        pm = p_market(sn, tk, pi)
        c3 = composite_confidence(pg, pa, pt, None, {"gefs": 1, "analog": 1, "traj": 1})
        c4 = composite_confidence(pg, pa, pt, pm, {k: 0.25 for k in
                                                   ("gefs", "analog", "traj", "market")})
        g3, g4 = grade_from_confidence(c3), grade_from_confidence(c4)
        grade_3[g3] += 1
        grade_4[g4] += 1
        conf3_sum += c3
        conf4_sum += c4
        if pm is not None:
            have_market += 1
        if g3 != g4:
            re_graded += 1
        per.append({"trade_uuid": uuid, "station": st, "day": day, "ticker": tk,
                    "p_market": pm, "conf_3comp": c3, "conf_4comp": c4,
                    "grade_3comp": g3, "grade_4comp": g4})

    n = len(rows)
    out = {
        "n_trades": n,
        "trades_with_market_mid": have_market,
        "trades_missing_market_mid": n - have_market,
        "mean_composite_conf_3comp": round(conf3_sum / n, 4) if n else None,
        "mean_composite_conf_4comp": round(conf4_sum / n, 4) if n else None,
        "grade_dist_3comp": grade_3,
        "grade_dist_4comp": grade_4,
        "trades_re_graded": re_graded,
        "pct_re_graded": round(100.0 * re_graded / n, 1) if n else 0.0,
        "changed_ids": [r["trade_uuid"] for r in per if r["grade_3comp"] != r["grade_4comp"]],
    }
    print(json.dumps(out, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": out, "per_trade": per}, indent=2))
        print(f"\nwrote {args.out}")
    sb.close()
    sn.close()


if __name__ == "__main__":
    main()
