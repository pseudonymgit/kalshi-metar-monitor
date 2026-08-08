#!/usr/bin/env python3
"""
run_parallel_sweep.py — Parallel signal sweep orchestrator.

Runs the Big Sweep Phase 1 in parallel across signals using multiprocessing.
Fast signals (~0.1s/config) get full 20K configs.
Slow signals (~0.5s/config) get proportionally fewer for early convergence.

Usage:
    python3 scripts/run_parallel_sweep.py [--max-workers 8]
"""

import argparse
import csv
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")


def profile_signal(signal_name: str, n_configs: int = 50) -> float:
    """Profile a single config evaluation for a signal. Returns time per config in ms."""
    import sys as _sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.big_sweep import (
        DataCache, build_signal_registry, generate_sweep_configs,
        evaluate_signal_all_stations
    )
    # Use global seed for reproducibility across workers
    import random
    random.seed(42)
    registry = build_signal_registry()
    sobj = registry.get(signal_name)
    if sobj is None:
        return 0.0
    confs = generate_sweep_configs(50)
    sett = DataCache.get_settlements()
    import time as _time
    t0 = _time.time()
    for cfg in confs[:5]:
        evaluate_signal_all_stations(signal_name, sobj, cfg, sett)
    t1 = _time.time()
    return ((t1 - t0) / 5) * 1000  # ms per config


def run_signal_sweep(signal_name: str, n_configs: int) -> Dict:
    """Run the full sweep for one signal. Returns result dict."""
    import json, os, sys, csv
    import random
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.big_sweep import (
        DataCache, build_signal_registry, generate_sweep_configs,
        evaluate_signal_all_stations
    )
    random.seed(42)
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    registry = build_signal_registry()
    sobj = registry.get(signal_name)
    if sobj is None:
        return {"signal_name": signal_name, "error": "Not in registry"}
    
    confs = generate_sweep_configs(n_configs)
    sett = DataCache.get_settlements()

    best_pnl = -1e9
    best_metrics = None
    batch_size = min(500, max(50, n_configs // 40))
    n_configs_evaluated = 0
    acc_window = []
    sharpe_window = []
    converged = False

    for i, cfg in enumerate(confs):
        result = evaluate_signal_all_stations(signal_name, sobj, cfg, sett)
        agg = result.get("aggregate", {})
        pnl = agg.get("total_pnl", -1e9)
        n_configs_evaluated = i + 1
        if pnl > best_pnl and agg.get("n_trades", 0) >= 0:
            best_pnl = pnl
            best_metrics = result

        # Early stopping every batch_size configs
        if (i + 1) % batch_size == 0:
            acc = agg.get("accuracy", 0)
            sharpe = agg.get("sharpe", 0)
            acc_window.append(acc)
            sharpe_window.append(sharpe)
            if len(acc_window) > 3:
                acc_window.pop(0)
            if len(sharpe_window) > 3:
                sharpe_window.pop(0)
            acc_converged = len(acc_window) >= 3 and (max(acc_window) - min(acc_window)) < 0.005
            sharpe_converged = len(sharpe_window) >= 3 and (max(sharpe_window) - min(sharpe_window)) < 0.1
            if acc_converged and sharpe_converged:
                converged = True
                break

    if best_metrics is None:
        best_metrics = evaluate_signal_all_stations(signal_name, sobj, confs[0], sett)
        n_configs_evaluated = 1

    result = {
        "signal_name": signal_name,
        "n_configs": n_configs,
        "n_configs_evaluated": n_configs_evaluated,
        "converged": converged,
        "config_id": best_metrics.get("config_id", 0),
        "aggregate": best_metrics.get("aggregate", {}),
        "per_station": best_metrics.get("per_station", {}),
        "per_market": best_metrics.get("per_market", {}),
        "validation": best_metrics.get("validation", {}),
    }

    # Write individual signal result
    out_path = os.path.join(data_dir, f"sweep_signal_{signal_name}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1, default=str)

    # Write convergence log entry
    conv_path = os.path.join(data_dir, "sweep_convergence_log.csv")
    try:
        agg = result.get("aggregate", {})
        with open(conv_path, "a") as f:
            f.write(f"{signal_name},{n_configs},{n_configs_evaluated},{converged},{agg.get('accuracy',0):.4f},{agg.get('sharpe',0):.4f},{agg.get('total_pnl',0):.2f},{agg.get('n_trades',0)}\n")
    except:
        pass

    return result


def main():
    parser = argparse.ArgumentParser(description="Parallel sweep — Big Sweep Phase 1")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="Max parallel workers [default: 4]")
    parser.add_argument("--n-configs", type=int, default=20000,
                        help="Config count per signal [default: 20000]")
    parser.add_argument("--min-configs", type=int, default=500,
                        help="Minimum configs for slow signals [default: 500]")
    parser.add_argument("--skip-integrity", action="store_true", default=True)
    parser.add_argument("--skip-data-check", action="store_true",
                        help="Skip data quality validation (emergency escape hatch)")
    parser.add_argument("--signals", nargs="*", default=None,
                        help="Specific signals to sweep (default: all)")
    args = parser.parse_args()

    # ── Data quality check (gated by manifest) ──
    if not args.skip_data_check:
        import subprocess as _sp
        import sys as _sys
        quality_script = os.path.join(REPO_ROOT, "scripts", "validate_data_quality.py")
        if os.path.exists(quality_script):
            print("\n[Pre-flight] Checking data quality...")
            result = _sp.run([_sys.executable, quality_script], capture_output=True, text=True)
            print(result.stdout)
            if result.returncode != 0:
                print("\n  ❌ DATA QUALITY FAILED — sweep aborted.")
                print("     Fix data issues or use --skip-data-check to force.")
                _sys.exit(1)
            print("  ✅ Data quality passed.\n")
        else:
            print(f"  ⚠️  Data quality script not found at {quality_script}")

    print(f"Parallel Sweep — {datetime.now(timezone.utc).isoformat()}")
    print(f"  Workers: {args.max_workers}")
    print(f"  Configs per signal: {args.n_configs}")

    # Profile all signals
    print("\nProfiling signals...")
    import sys as _sys
    sys.path.insert(0, REPO_ROOT)
    from scripts.big_sweep import build_signal_registry

    registry = build_signal_registry()
    if args.signals:
        signal_names = [s for s in args.signals if s in registry]
    else:
        signal_names = sorted(registry.keys())

    print(f"  {len(signal_names)} signals to sweep")

    # Assign config counts based on speed
    signal_configs = {}
    for sname in signal_names:
        t_per_config = profile_signal(sname, 50)
        # Slow signals get min_configs; fast signals get full n_configs
        if t_per_config > 500:  # >500ms per config → very slow
            assigned = max(args.min_configs, args.n_configs // 10)
        elif t_per_config > 200:  # >200ms → moderate
            assigned = max(args.min_configs, args.n_configs // 4)
        elif t_per_config > 50:  # >50ms → average
            assigned = args.n_configs // 2
        else:  # <50ms → fast
            assigned = args.n_configs
        signal_configs[sname] = assigned
        print(f"  {sname:35s} {t_per_config:5.0f}ms/config → {assigned:6d} configs")

    print(f"\nTotal config evaluations: {sum(signal_configs.values()):,}")

    # Write convergence log header
    conv_path = os.path.join(DATA_DIR, "sweep_convergence_log.csv")
    with open(conv_path, "w") as f:
        f.write("signal_name,n_configs_total,n_configs_evaluated,converged,accuracy,sharpe,total_pnl,n_trades\n")

    # Run in parallel
    print(f"\nRunning sweep with {args.max_workers} workers...")
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {}
        for sname in signal_names:
            n_conf = signal_configs[sname]
            future = executor.submit(run_signal_sweep, sname, n_conf)
            futures[future] = sname

        done_count = 0
        for future in as_completed(futures):
            sname = futures[future]
            done_count += 1
            try:
                result = future.result(timeout=3600)
                agg = result.get("aggregate", {})
                print(f"  [{done_count}/{len(signal_names)}] {sname:35s} acc={agg.get('accuracy',0):.3f}  trades={agg.get('n_trades',0):5d}  P&L=${agg.get('total_pnl',0):>8,.0f}  Sharpe={agg.get('sharpe',0):.2f}  ({result.get('n_configs_evaluated',0)}/{signal_configs[sname]} configs{' CONVERGED' if result.get('converged') else ''})")
            except Exception as e:
                print(f"  [{done_count}/{len(signal_names)}] {sname:35s} FAILED: {e}")

    elapsed = time.time() - t0
    print(f"\n=== Sweep complete in {elapsed/60:.1f}m ===")
    print(f"Collecting results...")

    # Aggregate all signal results into single file
    all_results = {}
    for sname in signal_names:
        path = os.path.join(DATA_DIR, f"sweep_signal_{sname}.json")
        if os.path.exists(path):
            with open(path) as f:
                all_results[sname] = json.load(f)

    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "n_signals": len(all_results),
            "n_configs": args.n_configs,
            "n_stations": 20,
            "parallel": True,
            "workers": args.max_workers,
            "total_time_min": round(elapsed / 60, 1),
        },
        "signals": all_results,
    }

    out_path = os.path.join(DATA_DIR, "sweep_results_v2.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=1, default=str)
    print(f"Results written to {out_path}")


if __name__ == "__main__":
    main()
