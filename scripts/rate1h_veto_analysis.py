#!/usr/bin/env python3
"""
rate1h_veto_analysis.py — evaluate the rate_1h separation as a PRE-TRADE veto
for Track B, using the 68 settled trades in the live book.

For each settled trade:
  * decision window = the fixed UTC grid hour nearest the entry time
  * station-window rate_1h separation (pp) at that window, derived from the
    ASOS 1-minute archive with the SAME method as
    scripts/sweep_intraday_rate1h_crossing.py (+-0.5 degF trigger)
  * outcome = canonical_outcome (correct / incorrect)

Contingency (per spec):
  sep <  5 pp & incorrect  -> "veto would have helped avoid a loss"
  sep > 30 pp & correct    -> "veto would have been fine, trade was good"
  sep > 30 pp & incorrect  -> "veto would have missed a loser that looked good"
  sep <  5 pp & correct    -> "veto would have skipped a winner (cost)"

Net-P&L veto rule (per spec): skip any trade whose station-window sep is below
that STATION's median sep (across the 9 decision windows). Compare realised P&L
of the surviving book against the full book.

Read-only. Writes data/rate1h_veto.json.

Usage:
    python3 scripts/rate1h_veto_analysis.py
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SWEEP = REPO / "scripts" / "sweep_intraday_rate1h_crossing.py"
OUT = REPO / "data" / "rate1h_veto.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
SEP_THRESHOLD = 0.5
LOW_SEP = 5.0     # spec bucket boundary
HIGH_SEP = 30.0   # spec bucket boundary


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def load_sweep_module():
    spec = importlib.util.spec_from_file_location("sweep_rate1h", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def per_station_separation(mod) -> dict:
    buckets = mod.load_bucket_means()
    samples = mod.evaluate(buckets, GRID_HOURS)
    acc = defaultdict(lambda: {"up": [0, 0], "dn": [0, 0]})
    for st, _ld, h, rate, _hs, _k, crossed in samples:
        if rate >= SEP_THRESHOLD:
            acc[(st, h)]["up"][0] += crossed
            acc[(st, h)]["up"][1] += 1
        elif rate <= -SEP_THRESHOLD:
            acc[(st, h)]["dn"][0] += crossed
            acc[(st, h)]["dn"][1] += 1
    sep = {}
    for key, d in acc.items():
        if d["up"][1] and d["dn"][1]:
            sep[key] = 100.0 * (d["up"][0] / d["up"][1] - d["dn"][0] / d["dn"][1])
    return sep


def nearest_window(entry_dt: datetime) -> int:
    h = entry_dt.hour + entry_dt.minute / 60.0
    return min(GRID_HOURS, key=lambda g: (abs(g - h), -g))


def build():
    gen = datetime.now(timezone.utc).isoformat()
    con = _ro(BOOK)
    cur = con.cursor()
    trades = cur.execute(
        """SELECT id, trade_date_utc, station, market_ticker, created_at_utc,
                  canonical_outcome, realized_pnl, p_fused, market_price,
                  position_size_usd, trade_cost
           FROM trades WHERE status='closed' ORDER BY id"""
    ).fetchall()
    con.close()

    print("deriving per-station rate_1h separation from ASOS (this takes ~90s) ...")
    mod = load_sweep_module()
    sep = per_station_separation(mod)
    med = {}
    for st in {k[0] for k in sep}:
        med[st] = statistics.median(v for (s, _h), v in sep.items() if s == st)

    rows = []
    buckets = {
        "veto would have helped avoid a loss": [],
        "veto would have been fine, trade was good": [],
        "veto would have missed a loser that looked good": [],
        "veto would have skipped a winner (cost)": [],
        "mid-band (no veto call)": [],
    }
    for (i, tdate, stn, ticker, created, outcome, pnl, pfused,
         mkt, pos, cost) in trades:
        entry_dt = datetime.fromisoformat(created)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        win = nearest_window(entry_dt)
        sep_pp = sep.get((stn, win))
        station_med = med.get(stn)

        if sep_pp is None:
            bucket = "mid-band (no veto call)"
        elif sep_pp < LOW_SEP and outcome == "incorrect":
            bucket = "veto would have helped avoid a loss"
        elif sep_pp > HIGH_SEP and outcome == "correct":
            bucket = "veto would have been fine, trade was good"
        elif sep_pp > HIGH_SEP and outcome == "incorrect":
            bucket = "veto would have missed a loser that looked good"
        elif sep_pp < LOW_SEP and outcome == "correct":
            bucket = "veto would have skipped a winner (cost)"
        else:
            bucket = "mid-band (no veto call)"

        kept = (station_med is None or sep_pp is None or sep_pp >= station_med)
        buckets[bucket].append(i)
        rows.append({
            "id": i,
            "trade_date_utc": tdate,
            "station": stn,
            "market_ticker": ticker,
            "entry_time_utc": entry_dt.isoformat(),
            "decision_window_utc": f"{win:02d}Z",
            "station_window_sep_pp": round(sep_pp, 3) if sep_pp is not None else None,
            "station_median_sep_pp": round(station_med, 3) if station_med is not None else None,
            "above_station_median": kept,
            "outcome": outcome,
            "realized_pnl": pnl,
            "position_size_usd": pos,
            "trade_cost": cost,
            "veto_bucket": bucket,
        })

    total_pnl = sum((r["realized_pnl"] or 0.0) for r in rows)
    kept_pnl = sum((r["realized_pnl"] or 0.0) for r in rows if r["above_station_median"])
    skipped_pnl = sum((r["realized_pnl"] or 0.0) for r in rows if not r["above_station_median"])
    n_kept = sum(1 for r in rows if r["above_station_median"])
    n_skipped = len(rows) - n_kept

    def _cnt(ids):
        return {
            "count": len(ids),
            "pnl": round(sum((next(r for r in rows if r["id"] == x)["realized_pnl"] or 0.0)
                             for x in ids), 2),
        }

    contingency = {
        "veto would have helped avoid a loss": _cnt(buckets["veto would have helped avoid a loss"]),
        "veto would have been fine, trade was good": _cnt(buckets["veto would have been fine, trade was good"]),
        "veto would have missed a loser that looked good": _cnt(buckets["veto would have missed a loser that looked good"]),
        "veto would have skipped a winner (cost)": _cnt(buckets["veto would have skipped a winner (cost)"]),
        "mid-band (no veto call)": _cnt(buckets["mid-band (no veto call)"]),
    }

    saved = len(buckets["veto would have helped avoid a loss"])
    missed = len(buckets["veto would have skipped a winner (cost)"])

    report = {
        "generated_at_utc": gen,
        "source_book": "data/paper_trading_sbox.db:trades (status=closed, n=68)",
        "source_separation": "derived from data/iem_asos_1min.db via "
                             "scripts/sweep_intraday_rate1h_crossing.py method",
        "read_only": True,
        "parameters": {
            "sep_threshold_degf": SEP_THRESHOLD,
            "low_sep_pp": LOW_SEP,
            "high_sep_pp": HIGH_SEP,
            "veto_rule": "skip trade when station-window sep < station median sep",
        },
        "station_median_sep_pp": {k: round(v, 3) for k, v in med.items()},
        "summary": {
            "settled_trades": len(rows),
            "contingency": contingency,
            "saved_vs_missed": {
                "losses_avoided": saved,
                "winners_skipped": missed,
                "net_trade_count": saved - missed,
                "losers_looking_good (sep>30 & incorrect)": len(buckets["veto would have missed a loser that looked good"]),
                "good_trades_kept (sep>30 & correct)": len(buckets["veto would have been fine, trade was good"]),
            },
            "net_pnl_veto": {
                "rule": "skip station-window sep < station median sep",
                "full_book_trades": len(rows),
                "full_book_pnl": round(total_pnl, 2),
                "kept_trades": n_kept,
                "kept_pnl": round(kept_pnl, 2),
                "skipped_trades": n_skipped,
                "skipped_pnl": round(skipped_pnl, 2),
                "veto_delta_pnl": round(kept_pnl - total_pnl, 2),
                "vetoed_winners_pnl": round(sum((r["realized_pnl"] or 0.0)
                                                for r in rows
                                                if not r["above_station_median"]
                                                and r["outcome"] == "correct"), 2),
                "vetoed_losers_pnl": round(sum((r["realized_pnl"] or 0.0)
                                               for r in rows
                                               if not r["above_station_median"]
                                               and r["outcome"] == "incorrect"), 2),
            },
        },
        "trades": rows,
    }
    return report, rows


def print_report(report, rows):
    print(f"\n{'id':>4} {'stn':<5} {'win':<4} {'sep_pp':>7} {'stn_med':>8} "
          f"{'keep':<5} {'outcome':<10} {'pnl':>10}  bucket")
    print("-" * 120)
    for r in rows:
        sep_s = f"{r['station_window_sep_pp']:.2f}" if r["station_window_sep_pp"] is not None else "n/a"
        med_s = f"{r['station_median_sep_pp']:.2f}" if r["station_median_sep_pp"] is not None else "n/a"
        print(f"{r['id']:>4} {r['station']:<5} {r['decision_window_utc']:<4} "
              f"{sep_s:>7} {med_s:>8} "
              f"{('Y' if r['above_station_median'] else 'N'):<5} "
              f"{str(r['outcome']):<10} "
              f"{(r['realized_pnl'] if r['realized_pnl'] is not None else 0.0):>10.2f}  "
              f"{r['veto_bucket']}")
    print("-" * 120)
    s = report["summary"]
    print("contingency:", json.dumps(s["contingency"], indent=2))
    print("saved vs missed:", json.dumps(s["saved_vs_missed"], indent=2))
    print("net pnl veto:", json.dumps(s["net_pnl_veto"], indent=2))


def main():
    report, rows = build()
    OUT.write_text(json.dumps(report, indent=2))
    print_report(report, rows)
    print(f"\nwrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
