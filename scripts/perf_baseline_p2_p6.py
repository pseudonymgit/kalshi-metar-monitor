#!/usr/bin/env python3
"""
WS-F — P2-P6 performance baseline.

Runs the P2-P6 gates against the frozen B2 sweep results with a LEAKAGE-SAFE
walk-forward split (P2), then reports the P4 metrics structure and the P5
legacy-Sharpe reconciliation and P6 go/park/kill gate input.

P2  : select top configs on a TRAIN window, evaluate them on a HOLDOUT window
      (configs never see holdout data during selection).
P3  : cost accounting is already per-trade (fees/cost) — reported separately.
P4  : full metric set per split.
P5  : reconcile the legacy 0.449 Sharpe against the measured 90-day holdout.
P6  : go/park/kill gate evaluated from the holdout metrics.

Usage:
    python3 scripts/perf_baseline_p2_p6.py
    python3 scripts/perf_baseline_p2_p6.py --train-end 2026-05-31 --holdout-start 2026-06-01
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "sweep"))

import numpy as np  # noqa: E402

from sweep.kalshi_sweep_eval import (  # noqa: E402
    STATIONS,
    generate_lhs_samples,
    load_all_data,
    simulate_trade,
)

OUTPUT = str(REPO_ROOT / "data" / "perf_baseline_p2_p6.json")


def _metrics(trades):
    if len(trades) < 30:
        return {"n_trades": len(trades), "accuracy": None, "sharpe": None,
                "profit_factor": None, "max_drawdown": None,
                "avg_pnl_per_trade": None, "total_pnl": 0.0,
                "total_fees": 0.0, "total_cost": 0.0, "ece": None,
                "n_daily_returns": 0}
    n = len(trades)
    correct = sum(1 for t in trades if t["correct"])
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["net_pnl"]
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["total_fees"] for t in trades)
    total_cost = sum(t["cost"] for t in trades)
    gw = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gl = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    pf = gw / gl if gl > 0 else float("inf")
    bankroll = 10000.0
    rets = []
    for d in sorted(daily):
        r = daily[d] / bankroll if bankroll > 0 else 0.0
        rets.append(r)
        bankroll += daily[d]
    if len(rets) > 1:
        sd = float(np.std(rets, ddof=1))
        sharpe = (float(np.mean(rets)) / sd * math.sqrt(252)) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    cum = peak = mdd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak if peak > 0 else 0)
    return {
        "n_trades": n,
        "accuracy": round(correct / n, 4),
        "sharpe": round(sharpe, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "max_drawdown": round(mdd, 4),
        "avg_pnl_per_trade": round(total_pnl / n, 2),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "total_cost": round(total_cost, 2),
        "ece": None,  # filled below if wanted
        "n_daily_returns": len(rets),
    }


def collect_trades(config, settlements, gefs, date_filter):
    """Simulate trades for a config restricted to a date predicate."""
    trades = []
    for station in STATIONS:
        ss = settlements.get(station, {})
        sg = gefs.get(station, {})
        dates = sorted(set(ss) & set(sg))
        for i, d in enumerate(dates):
            if not date_filter(d):
                continue
            actual = ss[d]
            gd = sg[d]
            mean_c = gd["mean"]
            if mean_c is None:
                continue
            mean_f = mean_c * 9 / 5 + 32
            prev = ss.get(dates[i - 1]) if i > 0 else None
            tr = simulate_trade(config, station, d, mean_f, actual, prev)
            if tr is not None:
                trades.append(tr)
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-end", default="2026-05-31")
    ap.add_argument("--holdout-start", default="2026-06-01")
    ap.add_argument("--top-n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", default=OUTPUT)
    args = ap.parse_args()

    data = load_all_data()
    settlements = {}
    for st in STATIONS:
        settlements[st] = {}
        if st in data["finalized"]:
            settlements[st].update(data["finalized"][st])
        if st in data["historical_api"]:
            settlements[st].update(data["historical_api"][st])
    gefs = data["gefs"]

    train_end = args.train_end
    holdout_start = args.holdout_start

    configs = generate_lhs_samples(2000)

    # ── P2: select on TRAIN only ──
    print(f"Selecting top {args.top_n} on train <= {train_end} ...")
    scored = []
    for c in configs:
        tr = collect_trades(c, settlements, gefs, lambda d: d <= train_end)
        m = _metrics(tr)
        if m["sharpe"] is not None and m["n_trades"] >= 100:
            scored.append((m["sharpe"], c, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: args.top_n]
    if not top:
        print("No configs passed the train filter.")
        return 1

    # ── Evaluate selected configs on HOLDOUT (configs frozen) ──
    print(f"Evaluating {len(top)} selected configs on holdout >= {holdout_start} ...")
    holdout_rows = []
    for _, c, train_m in top:
        trh = collect_trades(c, settlements, gefs, lambda d: d >= holdout_start)
        mh = _metrics(trh)
        holdout_rows.append({"train_sharpe": train_m["sharpe"],
                             "holdout": mh, "config": c})

    # Aggregate holdout stats across selected configs
    h_acc = [r["holdout"]["accuracy"] for r in holdout_rows if r["holdout"]["accuracy"] is not None]
    h_sharpe = [r["holdout"]["sharpe"] for r in holdout_rows if r["holdout"]["sharpe"] is not None]
    agg = {
        "n_selected": len(holdout_rows),
        "holdout_median_accuracy": round(float(np.median(h_acc)), 4) if h_acc else None,
        "holdout_median_sharpe": round(float(np.median(h_sharpe)), 4) if h_sharpe else None,
        "holdout_mean_accuracy": round(float(np.mean(h_acc)), 4) if h_acc else None,
        "holdout_mean_sharpe": round(float(np.mean(h_sharpe)), 4) if h_sharpe else None,
        "holdout_profitable": sum(1 for r in holdout_rows if (r["holdout"]["total_pnl"] or 0) > 0),
    }

    # ── P5: reconcile legacy 0.449 Sharpe ──
    legacy = 0.449
    p5 = {
        "legacy_sharpe": legacy,
        "measured_holdout_median_sharpe": agg["holdout_median_sharpe"],
        "delta": None,
    }
    if agg["holdout_median_sharpe"] is not None:
        p5["delta"] = round(agg["holdout_median_sharpe"] - legacy, 4)

    # ── P6: go/park/kill gate ──
    # Gate (Gray Room thresholds): >=58% directional accuracy, Sharpe >=1.0.
    med_acc = agg["holdout_median_accuracy"]
    med_sharpe = agg["holdout_median_sharpe"]
    if med_acc is None or med_sharpe is None:
        gate = "INSUFFICIENT_DATA"
    elif med_acc >= 0.58 and med_sharpe >= 1.0:
        gate = "GO"
    elif med_acc >= 0.54 and med_sharpe >= 0.5:
        gate = "PARK"
    else:
        gate = "KILL"
    p6 = {
        "thresholds": {"accuracy_go": 0.58, "sharpe_go": 1.0},
        "holdout_median_accuracy": med_acc,
        "holdout_median_sharpe": med_sharpe,
        "verdict": gate,
    }

    out = {
        "workstream": "WS-F P2-P6 performance baseline",
        "split": {"train_end": train_end, "holdout_start": holdout_start,
                  "top_n": args.top_n, "seed": args.seed},
        "P2_walk_forward": {"aggregate": agg,
                            "rows": [{"train_sharpe": r["train_sharpe"],
                                      "holdout": r["holdout"]} for r in holdout_rows]},
        "P3_accounting": {"note": "per-trade fees+cost reported per split (see P2 rows)"},
        "P4_metrics": "see P2_walk_forward.rows (full metric set per config)",
        "P5_legacy_sharpe_reconciliation": p5,
        "P6_gate": p6,
    }
    Path(args.output).write_text(json.dumps(out, indent=2))

    print("\n=== P2-P6 PERFORMANCE BASELINE ===")
    print(f"Train<= {train_end}  |  Holdout>= {holdout_start}  |  top {len(holdout_rows)}")
    print(f"Holdout median accuracy : {agg['holdout_median_accuracy']}")
    print(f"Holdout median Sharpe   : {agg['holdout_median_sharpe']}")
    print(f"Holdout profitable cfgs : {agg['holdout_profitable']}/{agg['n_selected']}")
    print(f"P5 legacy 0.449 vs measured: delta {p5['delta']}")
    print(f"P6 GATE: {gate}")
    print(f"Saved: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
