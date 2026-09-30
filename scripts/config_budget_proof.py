#!/usr/bin/env python3
"""
R/T (2026-09-29) — Config-budget PROOF (Dan's item 2).

Question: can we cut the ensemble sweep to 500 configs without losing the
optimal config? Dan does NOT trust "500 is enough" — we've been wrong before.

Method (single faithful pass, no duplicated work):
  For each ensemble signal, evaluate the SAME 3,500 LHS configs ONCE (seeded),
  recording each config's accuracy (and n_trades / pnl). Then, for each budget
  N in {500, 1000, 2000, 3500}, take the first N configs and compute the TOP-10
  by accuracy (among configs with >= MIN_TRADES_REPORT). Report:
    - best accuracy in top-10, mean accuracy of top-10
    - the identity (config index) of the top config
  Verdict rule (Dan): if top-10@500 is within 0.01 ACCURACY of top-10@3500 -> CUT.

Parallelism: each config eval is independent -> multiprocessing Pool over cores.
Each worker builds its own signal registry lazily (fork-safe).

Run:
  .venv-gefs/bin/python scripts/config_budget_proof.py --n 3500 --jobs 14
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "scripts"))

SIGNALS = ["eighty_two_member_ensemble", "eighty_two_member_ensemble_pooled"]
BUDGETS = [500, 1000, 2000, 3500]

_WORKER = {}


def _init_worker():
    """Build per-process registry + settlements once."""
    import sqlite3
    from sweep_engine import build_signal_registry
    reg = build_signal_registry()
    conn = sqlite3.connect(f"file:{REPO/'data'/'metar_backfill.db'}?mode=ro", uri=True)
    sett = {}
    for st, d, h in conn.execute(
        "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
        "WHERE settlement_temp_f IS NOT NULL"
    ):
        sett.setdefault(st, {})[d] = h
    _WORKER["reg"] = reg
    _WORKER["sett"] = sett


def _eval_one(args):
    """args = (signal_name, config_index, config_dict)."""
    signal_name, idx, cfg = args
    from sweep_validation import evaluate_signal_all_stations
    sig = _WORKER["reg"].get(signal_name)
    if sig is None:
        return {"idx": idx, "error": "signal missing"}
    try:
        r = evaluate_signal_all_stations(signal_name, sig, cfg, _WORKER["sett"])
        agg = r.get("aggregate", {})
        return {
            "idx": idx,
            "accuracy": float(agg.get("accuracy", 0.0) or 0.0),
            "n_trades": int(agg.get("n_trades", 0) or 0),
            "pnl": float(agg.get("total_pnl", 0.0) or 0.0),
            "sharpe": float(agg.get("sharpe", 0.0) or 0.0),
        }
    except Exception as e:
        return {"idx": idx, "error": str(e)[:120]}


def top10(rows, min_trades):
    elig = [r for r in rows if r.get("n_trades", 0) >= min_trades and "error" not in r]
    elig.sort(key=lambda r: r["accuracy"], reverse=True)
    t = elig[:10]
    return {
        "n_eligible": len(elig),
        "best_accuracy": round(t[0]["accuracy"], 6) if t else None,
        "mean_top10_accuracy": round(sum(x["accuracy"] for x in t) / len(t), 6) if t else None,
        "top10_indices": [x["idx"] for x in t],
        "top10_accuracies": [round(x["accuracy"], 6) for x in t],
        "top1_idx": t[0]["idx"] if t else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3500)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--out", default=str(REPO / "data" / "config_budget_proof.json"))
    args = ap.parse_args()

    from sweep_engine import generate_sweep_configs
    configs = generate_sweep_configs(args.n)
    configs = configs[: args.n]
    print(f"Generated {len(configs)} LHS configs (seed 42). jobs={args.jobs}")

    import multiprocessing as mp
    ctx = mp.get_context("fork")
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_configs": len(configs), "min_trades": args.min_trades,
           "budgets": BUDGETS, "signals": {}}

    for sname in SIGNALS:
        print(f"\n=== {sname} ===", flush=True)
        t0 = time.time()
        with ctx.Pool(args.jobs, initializer=_init_worker) as pool:
            rows = []
            for i, res in enumerate(pool.imap_unordered(
                    _eval_one, [(sname, i, cfg) for i, cfg in enumerate(configs)], chunksize=8)):
                rows.append(res)
                if (i + 1) % 250 == 0:
                    el = time.time() - t0
                    print(f"  {i+1}/{len(configs)} configs  ({el:.0f}s, {el/(i+1):.2f}s/cfg)", flush=True)
        rows.sort(key=lambda r: r["idx"])
        dt = time.time() - t0
        print(f"  done {len(rows)} configs in {dt:.0f}s", flush=True)

        per_budget = {}
        for b in BUDGETS:
            per_budget[b] = top10(rows[: min(b, len(rows))], args.min_trades)

        verdict = None
        t500 = per_budget.get(500, {}).get("mean_top10_accuracy")
        tmax = per_budget.get(3500, per_budget.get(BUDGETS[-1], {}), ).get("mean_top10_accuracy") \
            if 3500 in per_budget else None
        if t500 is not None and tmax is not None:
            delta = abs(tmax - t500)
            verdict = {"delta_mean_top10": round(delta, 6), "within_0.01": delta <= 0.01}

        out["signals"][sname] = {
            "runtime_seconds": round(dt, 1),
            "per_budget": {str(k): v for k, v in per_budget.items()},
            "verdict": verdict,
        }
        json.dump(out, open(args.out, "w"), indent=1)
        print(f"  verdict: {verdict}", flush=True)

    print(f"\nSaved: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
