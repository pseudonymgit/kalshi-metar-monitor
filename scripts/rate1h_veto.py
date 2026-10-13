#!/usr/bin/env python3
"""rate1h_veto.py — would a rate_1h separation veto have helped or hurt?

For each usable (settled) trade we look up the station's rate_1h separation (pp)
from the ASOS cohort (scripts/_rate1h_sep.py, mirrors the published
sweep_intraday_rate1h_crossing.json), then classify the trade into a 2x2
contingency against its realised outcome:

    sep < 5pp  and outcome=incorrect  -> "helped"     (veto would have skipped a loss)
    sep > 30pp and outcome=correct    -> "fine"       (veto correctly left it alone)
    sep > 30pp and outcome=incorrect  -> "missed"     (veto let a loser through)
    sep < 5pp  and outcome=correct    -> "cost_win"   (veto would have skipped a winner)
    otherwise (5-30pp)                -> "neutral"

Veto rule: SKIP when the station's separation is below the median station
separation (the veto threshold). We report the net P&L delta of applying it.

Two variants are reported so the level is unambiguous:
  A) station separation = per-station overall sep_pp
  B) station separation = station-window sep_pp at the trade's decision window
READ-ONLY.

Output: data/rate1h_veto.json
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
OUT = REPO / "data" / "rate1h_veto.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
LOW_PP = 5.0
HIGH_PP = 30.0

_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def nearest_grid_hour(h):
    c = [g for g in GRID_HOURS if g <= h]
    return c[-1] if c else GRID_HOURS[0]


def classify(sep_pp, outcome):
    if sep_pp is None:
        return "unknown"
    if sep_pp < LOW_PP and outcome == "incorrect":
        return "helped"
    if sep_pp > HIGH_PP and outcome == "correct":
        return "fine"
    if sep_pp > HIGH_PP and outcome == "incorrect":
        return "missed"
    if sep_pp < LOW_PP and outcome == "correct":
        return "cost_win"
    return "neutral"


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main():
    idx = R.build()
    snap = _ro(BOOK)
    trades = [dict(r) for r in snap.execute(
        "SELECT * FROM trades WHERE settlement_confirmed=1 AND canonical_outcome IS NOT NULL "
        "ORDER BY trade_date_utc, id")]

    st_med = median(v["sep_pp"] for v in idx["per_station"].values())

    rows = []
    for t in trades:
        st = t["station"]
        ts = datetime.fromisoformat(t["created_at_utc"])
        win = nearest_grid_hour(ts.hour)
        st_sep = idx["per_station"].get(st, {}).get("sep_pp")
        w_sep, w_src = R.sep_for_trade(idx, st, win)
        outcome = t["canonical_outcome"]
        pnl = t["realized_pnl"] or 0.0
        rows.append({
            "trade_uuid": t["trade_uuid"],
            "station": st,
            "date": t["trade_date_utc"],
            "entry_window": f"{win:02d}Z",
            "outcome": outcome,
            "realized_pnl": round(pnl, 4),
            "station_sep_pp": st_sep,
            "window_sep_pp": w_sep,
            "window_sep_source": w_src,
            "contingency_station": classify(st_sep, outcome),
            "contingency_window": classify(w_sep, outcome),
            "veto_station": st_sep is not None and st_sep < st_med,
            "veto_window": w_sep is not None and w_sep < st_med,
        })

    def summarize(vsep_key, vcon_key, vveto_key):
        cont = Counter(r[vcon_key] for r in rows)
        skipped = [r for r in rows if r[vveto_key]]
        kept = [r for r in rows if not r[vveto_key]]
        total_pnl = sum(r["realized_pnl"] for r in rows)
        kept_pnl = sum(r["realized_pnl"] for r in kept)
        skipped_pnl = sum(r["realized_pnl"] for r in skipped)
        return {
            "contingency": {k: cont.get(k, 0) for k in
                            ("helped", "fine", "missed", "cost_win", "neutral", "unknown")},
            "veto_threshold_pp": st_med,
            "trades_skipped": len(skipped),
            "trades_kept": len(kept),
            "pnl_all": round(total_pnl, 2),
            "pnl_after_veto": round(kept_pnl, 2),
            "pnl_delta": round(kept_pnl - total_pnl, 2),
            "skipped_pnl": round(skipped_pnl, 2),
            "skipped_correct": sum(1 for r in skipped if r["outcome"] == "correct"),
            "skipped_incorrect": sum(1 for r in skipped if r["outcome"] == "incorrect"),
        }

    variant_a = summarize("station_sep_pp", "contingency_station", "veto_station")
    variant_b = summarize("window_sep_pp", "contingency_window", "veto_window")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "data/paper_trading_sbox.db (settled only, read-only)",
        "rate1h_source": "scripts/_rate1h_sep.py (ASOS cohort, mirrors sweep_intraday_rate1h_crossing.json)",
        "thresholds_pp": {"low": LOW_PP, "high": HIGH_PP},
        "contingency_definitions": {
            "helped": f"sep < {LOW_PP} and outcome=incorrect -> veto would have helped",
            "fine": f"sep > {HIGH_PP} and outcome=correct -> veto fine",
            "missed": f"sep > {HIGH_PP} and outcome=incorrect -> veto missed",
            "cost_win": f"sep < {LOW_PP} and outcome=correct -> veto cost a win",
            "neutral": f"{LOW_PP} <= sep <= {HIGH_PP}",
        },
        "usable_trades": len(rows),
        "median_station_sep_pp": st_med,
        "variant_A_separation_equals_per_station": variant_a,
        "variant_B_separation_equals_station_window": variant_b,
        "per_station_sep_pp": {k: v["sep_pp"] for k, v in idx["per_station"].items()},
        "per_trade": rows,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"usable settled trades: {len(rows)} | median station sep {st_med} pp")
    for name, v in (("A per-station", variant_a), ("B station-window", variant_b)):
        print(f"[{name}] contingency {v['contingency']}")
        print(f"   skip {v['trades_skipped']} (correct {v['skipped_correct']}, "
              f"incorrect {v['skipped_incorrect']}) | P&L ${v['pnl_all']} -> "
              f"${v['pnl_after_veto']} (delta ${v['pnl_delta']:+.2f})")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
