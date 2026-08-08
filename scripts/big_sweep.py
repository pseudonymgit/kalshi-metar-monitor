#!/usr/bin/env python3
"""
big_sweep.py — Big Sweep: Phase 1 (Signal-Only) + Phase 2 (Meta-Sweep)

Phase 1: All 36 Signals × Full Parameter Sweep (extended)
Phase 2: Meta-Sweep over gate/lever/lane/modulator combinations
         using top signal configs from Phase 1.

Usage:
    python3 scripts/big_sweep.py                     # Phase 1 (default)
    python3 scripts/big_sweep.py --phase 1           # Explicit Phase 1
    python3 scripts/big_sweep.py --phase 2           # Phase 2 meta-sweep
    python3 scripts/big_sweep.py --phase 1 --fast    # Phase 1 fast mode

Phase 1 outputs:
  data/sweep_results_v1.json          — Raw per-signal config results
  data/sweep_signal_summary.csv       — Per-signal aggregate metrics
  data/sweep_correlation_matrix.csv   — Cross-signal correlation
  data/sweep/differential_results.json— All-pairs differential analysis
  data/sweep_phase1_top_configs.json  — Top 5 configs per signal (NEW)

Phase 2 outputs:
  data/sweep_phase2_results.json      — Per-meta-config results
  data/sweep_phase2_summary.csv       — Best meta-config summary
  data/sweep_phase2_gate_stats.json   — Per-gate pass/fail counts
  data/sweep_phase2_luck_stats.json   — Luck-adjusted metrics
  data/sweep_phase2_portfolio.csv     — Portfolio-level metrics

B-Mode compliant. No AI/ML inside the sweep loop.
"""

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.sweep import config as sweep_config
from scripts.sweep.config import STATIONS as _DEF_STATIONS
from scripts.sweep.config import SLIPPAGE_BUDGET
from scripts.sweep.tiers import get_tier, get_tier_info
from core.platt_calibration import PlattCalibrationPipeline
from core.bma_emos import bma_calibrate, emos_calibrate
from core.trajectory_confirmation_gate import (
    TrajectoryConfirmationGate,
    evaluate_gate_for_station_date,
)
from core.trajectory_lane import (
    TrajectoryLane,
    evaluate_lane_for_station_date,
    apply_trajectory_lane_to_probability,
)
from core.settlement_execution_gate import (
    SettlementExecutionGate,
    GateVerdict,
)
from core.db_utils import query_db, with_db
from core.continuous_kelly import fee_aware_kelly, KellyState, kalshi_fee as ck_fee
from core.liquidity_gate import LiquidityGate
from core.ensemble_fraction import load_bias_corrections
from core.production_gate import ProductionGate, LossLimiter
from core.station_skill_gate import StationSkillGate
from core.agreement_gate import AgreementGate
from core.signal_fusion import (
    UncertaintyWeightedCascade,
    FusionModeConfig,
    fuse_majority_vote,
    fuse_weighted_vote,
    DEFAULT_FUSION_CONFIG,
)
from core.signal_families import get_active_signals, POOL_NAMES
from core.adaptive_thresholds import AdaptiveThresholdRegistry
from core.variance_weighted_sizing import (
    variance_weighted_blend,
    variance_adjusted_kelly,
    variance_weighted_pipeline,
    compute_signal_variance,
)
from core.luck_elimination import (
    wire_dependencies as luck_wire_dependencies,
    run_luck_elimination,
    print_luck_report,
)

METAR_DB = os.path.join(str(REPO_ROOT), "data", "metar_backfill.db")
SETTLEMENTS_DB = sweep_config.DB_PATH
SWEEP_DIR = sweep_config.SWEEP_DIR
RESULTS_DIR = Path(str(REPO_ROOT)) / "data"
os.makedirs(SWEEP_DIR, exist_ok=True)

KALSHI_REAL_FEE_RATE = 0.07
MIN_TRADES_REPORT = 10
MIN_TRADES_CALIBRATE = 20

# --- Re-export functions from split modules ---
from scripts.sweep_engine import (
    set_trajectory_gate, get_trajectory_gate,
    set_trajectory_lane, get_trajectory_lane,
    set_settlement_gate, get_settlement_gate,
    set_agreement_gate, get_agreement_gate,
    set_adaptive_thresholds, get_adaptive_registry,
    set_spatial_coherence, get_spatial_coherence,
    set_station_skill_gate, get_station_skill_gate_enabled,
    set_calibration_mode, get_calibration_mode,
    _get_loss_limiter, _record_trade_outcome,
    _safe_import_signal, _try_instantiate,
    build_signal_registry, DataCache,
    _latin_hypercube, generate_sweep_configs, generate_meta_configs,
    get_platt_pipeline, get_bias_corrections,
    _TRAJECTORY_GATE_ENABLED, _TRAJECTORY_GATE,
    _SETTLEMENT_GATE_ENABLED, _SETTLEMENT_GATE,
    _TRAJECTORY_LANE_ENABLED, _TRAJECTORY_LANE,
)
from scripts.sweep_metrics import (
    calibrate_confidence, simulate_trade,
    safe_signal_evaluate, compute_metrics,
    compute_correlation_matrix, compute_differential_results,
)
from scripts.sweep_validation import (
    evaluate_signal_on_station, split_discovery_holdout,
    split_geo_holdout, _eval_wrapper,
    run_three_stage_validation, evaluate_signal_all_stations,
    apply_agreement_gate_to_trades, apply_spatial_coherence_to_trades,
    run_best_config_for_signal,
)
from scripts.sweep_fusion import (
    get_variance_sizing_enabled, get_fusion_mode,
    evaluate_fusion, _evaluate_fusion_station,
    _run_fusion_validation,
)

def main():
    parser = argparse.ArgumentParser(description="Big Sweep — Phase 1: Signal-Only Parameter Sweep")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2],
                        help="Phase: 1=signal-only sweep, 2=meta-sweep [default: 1]")
    parser.add_argument("--stations", nargs="*", default=None)
    parser.add_argument("--n-configs", type=int, default=20000)
    parser.add_argument("--n-meta-configs", type=int, default=2000,
                        help="Number of meta-configs for Phase 2 [default: 300]")
    parser.add_argument("--signals", nargs="*", default=None)
    parser.add_argument("--skip-integrity", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--calibration", type=str, default="platt", choices=["platt", "bma", "emos", "both"], help="Calibration method: platt, bma, emos, both")
    parser.add_argument("--fusion", type=str, default="none", choices=["none", "uwc", "majority", "weighted"],
                        help="Fusion mode: none (baseline), uwc (Uncertainty-Weighted Cascade), majority, weighted")
    parser.add_argument("--trajectory-gate", action="store_true", default=False,
                        help="Enable Trajectory Confirmation Gate (epoch-based analog matching)")
    parser.add_argument("--trajectory-lane", action="store_true", default=False,
                        help="Enable Trajectory Lane (heavy informant — modulates fused probability via analog matching)")
    parser.add_argument("--spatial-coherence", action="store_true", default=False,
                        help="Enable Spatial Coherence Verification (cross-station confidence modulation)")
    parser.add_argument("--settlement-gate", action="store_true", default=False,
                        help="Enable Settlement Execution Gate (market open, cooldowns, approved stations)")
    parser.add_argument("--production-gate", action="store_true", default=False,
                        help="Enable Production Gate check: >=58%% acc, >=100 trades, <=30%% per-station concentration")
    parser.add_argument("--liquidity-gate", action="store_true", default=False, help="Enable liquidity gate: reject low-liquidity markets")
    parser.add_argument("--adaptive-thresholds", action="store_true", default=False,
                        help="Enable Bayesian Beta-Bernoulli adaptive confidence thresholds per (signal, station)")
    parser.add_argument("--agreement-gate", action="store_true", default=False,
                        help="Enable Agreement Gate (N-of-M consensus filtering)")
    parser.add_argument("--station-skill-gate", action="store_true", default=False,
                        help="Enable Station Skill Gate (filters stations with poor Brier Skill Score)")
    parser.add_argument("--bss-threshold", type=float, default=0.0,
                        help="Minimum Brier Skill Score threshold (BSS must be > this) [default: 0.0]")
    parser.add_argument("--variance-sizing", action="store_true", default=False,
                        help="Enable Variance-Weighted Sizing: blend signals by inverse variance and adjust Kelly by variance factor")
    parser.add_argument("--luck-test", action="store_true", default=False,
                        help="Run Luck Elimination Protocol on final results (null distribution, p-value, bootstrap CI)")
    parser.add_argument("--luck-shuffles", type=int, default=1000,
                        help="Number of Monte Carlo shuffles for luck test [default: 1000]")
    parser.add_argument("--goldilocks-lane", action="store_true", default=False,
                        help="Enable Goldilocks Lane (microstructure transient spike detection — separate sweep lane, not a signal)")
    args = parser.parse_args()


    # --fast mode: reduce stations and configs for quick tests
    if args.fast:
        global STATIONS
        STATIONS[:] = STATIONS[:3]
        args.n_configs = min(args.n_configs, 500)
        print("  --fast mode: 3 stations, 500 configs")

    # Set fusion mode for the run
    global _FUSION_MODE
    _FUSION_MODE = args.fusion
    # Phase 2 dispatch — meta-sweep over gates/levers/lanes/modulators
    if args.phase == 2:
        phase2_results = phase2_sweep(args)
        print("\n[Phase 2 complete] Results at data/sweep_phase2_results.json")
        return

    # Phase 1 signal-only sweep (default)

        print(f"  Fusion mode: {args.fusion}")

    # Variance-Weighted Sizing
    global _VARIANCE_SIZING_ENABLED
    _VARIANCE_SIZING_ENABLED = args.variance_sizing
    if args.variance_sizing:
        print(f"  Variance-Weighted Sizing: ENABLED")
        print(f"  Blending: inverse-variance-squared weighting")
        print(f"  Kelly: hyperbolic penalty 1/(1+k*σ²) with k=2.0")
        STATIONS[:] = STATIONS[:3]
    if args.stations:
        STATIONS = args.stations

    set_calibration_mode(getattr(args, "calibration", "platt"))

    print("=" * 72)
    print("  BIG SWEEP — Full Parameter x All-Signal Validation")
    print(f"  Started: {datetime.now(timezone.utc).isoformat()}")
    print(f"  N configs: {args.n_configs}")
    print(f"  Stations: {len(STATIONS)}")
    print("=" * 72)

    if not args.skip_integrity:
        print("\n[Phase 0] Data integrity gate...")
        if not sweep_config.run_integrity_gate(skip=False):
            print("  INTEGRITY GATE FAILED. Aborting.")
            sys.exit(1)

    print("\n[Phase 1] Loading data...")
    t0 = time.time()
    settlements = DataCache.get_settlements()
    n_records = sum(len(v) for v in settlements.values())
    dr = DataCache.get_date_range()
    print(f"  Kalshi settlements: {n_records} records ({dr[0]} -> {dr[1]})")
    avail = sum(1 for s in STATIONS if len(DataCache.get_metar_data(s)) > 10)
    print(f"  METAR stations available: {avail}/{len(STATIONS)}")
    print(f"  Loaded in {time.time() - t0:.1f}s")

    # Pre-load bias corrections
    bias_data = get_bias_corrections()

    # Initialize Trajectory Confirmation Gate if enabled
    if args.trajectory_gate:
        print("\n[Gate] Initializing Trajectory Confirmation Gate...")
        set_trajectory_gate(True)
        print(f"  Trajectory Gate: ENABLED")
        print(f"  Gate evaluates epoch-based analog matching before trade generation")
    else:
        set_trajectory_gate(False)

    # Initialize Trajectory Lane (heavy informant) if enabled
    if args.trajectory_lane:
        print("\n[Lane] Initializing Trajectory Lane (heavy informant)...")
        set_trajectory_lane(True)
        print(f"  Trajectory Lane: ENABLED")
        print(f"  Lane produces secondary probability estimate weighted into fusion layer")
    else:
        set_trajectory_lane(False)

    # Initialize Spatial Coherence if enabled
    if args.spatial_coherence:
        print("\n[Gate] Initializing Spatial Coherence Verification...")
        set_spatial_coherence(True)
        print(f"  Spatial Coherence: ENABLED")
        print(f"  Gate modulates confidence using cross-station agreement")
        print(f"  Clusters: NE(4), SE(3), SC(5), MW(2), RW(3), PAC(3)")
    else:
        set_spatial_coherence(False)

    # Initialize Settlement Execution Gate if enabled
    if args.settlement_gate:
        print("\n[Gate] Initializing Settlement Execution Gate...")
        set_settlement_gate(True)
        print(f"  Settlement Gate: ENABLED")
        print(f"  Gate evaluates market open, station cooldown, boundary cooldown, approved stations")
    else:
        set_settlement_gate(False)

    # Initialize Adaptive Threshold Registry if enabled
    if args.adaptive_thresholds:
        print("\n[Gate] Initializing Adaptive Threshold Registry...")
        set_adaptive_thresholds(True)
        print(f"  Adaptive Thresholds: ENABLED")
        print(f"  Bayesian Beta-Bernoulli per (signal, station) confidence floors")
    else:
        set_adaptive_thresholds(False)

    # Initialize Agreement Gate if enabled
    if args.agreement_gate:
        print("\n[Gate] Initializing Agreement Gate (N-of-M consensus)...")
        set_agreement_gate(True, n_required=args.agreement_n, m_total=args.agreement_m)
        print(f"  Agreement Gate: ENABLED")
        print(f"  N={args.agreement_n}, M={args.agreement_m}")
        print(f"  Filters trades where fewer than N of M top signals agree on direction")
    else:
        set_agreement_gate(False)

    # Initialize Station Skill Gate if enabled
    if args.station_skill_gate:
        print("\n[Gate] Initializing Station Skill Gate...")
        set_station_skill_gate(True, args.bss_threshold)
        print(f"  Station Skill Gate: ENABLED")
        print(f"  BSS Threshold: {args.bss_threshold}")
        print(f"  Gate filters stations where Brier Skill Score <= {args.bss_threshold}")
    else:
        set_station_skill_gate(False)

    # Initialize Goldilocks Lane if enabled
    if args.goldilocks_lane:
        print("\n[Lane] Initializing Goldilocks Lane (microstructure transient spike detection)...")
        try:
            from core.lane_goldilocks import GoldilocksLane, get_goldilocks_lane
            goldilocks_lane = get_goldilocks_lane()
            print(f"  Goldilocks Lane: ENABLED")
            print(f"  Lane monitors METAR observations at Kalshi bucket boundaries")
            print(f"  Detects transient microstructure spikes via DualHypothesisEngine")
            print(f"  Station exclusion: KNYC, stations with <100000 obs")
            print(f"  Max alerts/station/day: 3")
            _GOLDILOCKS_LANE_ENABLED = True
            _GOLDILOCKS_LANE = goldilocks_lane
        except Exception as e:
            print(f"  \u26a0\ufe0f Could not initialize Goldilocks Lane: {e}")
            _GOLDILOCKS_LANE_ENABLED = False
            _GOLDILOCKS_LANE = None
    else:
        _GOLDILOCKS_LANE_ENABLED = False
        _GOLDILOCKS_LANE = None

    print("\n[Phase 2] Building signal registry (36 signals)...")
    registry = build_signal_registry()
    active = {k: v for k, v in registry.items() if v is not None}
    print(f"  Total: {len(registry)} | Loaded: {len(active)} | Failed: {len(registry)-len(active)}")
    if len(active) < len(registry):
        print(f"  Failed: {[k for k,v in registry.items() if v is None]}")

    signal_names = list(args.signals) if args.signals else list(active.keys())
    if args.signals:
        signal_names = [s for s in args.signals if s in active]
    print(f"  Sweeping: {len(signal_names)} signals")

    # Count actual sweep params (continuous + discrete + categorical choices)
    n_total_params = 8 + 1 + 8 + 2  # cont + log + cat + agreement_n/m
    print(f"\n[Phase 3] Generating {args.n_configs} LHS configs with {n_total_params} parameters...")
    configs = generate_sweep_configs(args.n_configs)
    print(f"  Generated {len(configs)} configs")

    print("\n[Phase 4] Per-signal evaluation...")
    signal_results = {}

    for si, sname in enumerate(signal_names):
        sobj = active.get(sname)
        if sobj is None:
            print(f"  [{si+1}/{len(signal_names)}] {sname}: SKIP")
            continue
        print(f"\n[{si+1}/{len(signal_names)}] {sname}:")
        result = run_best_config_for_signal(sname, sobj, configs, settlements)
        signal_results[sname] = result
        agg = result.get("aggregate", {})
        print(f"  -> Trades: {agg.get('n_trades', 0)} | "
              f"Acc: {agg.get('accuracy', 0)*100:.2f}% | "
              f"PnL: ${agg.get('total_pnl', 0):+.0f} | "
              f"Sharpe: {agg.get('sharpe', 0):.4f} | "
              f"Brier: {agg.get('brier_score', 0):.4f} | "
              f"ECE: {agg.get('ece', 0):.4f}")

    # ── Goldilocks Lane Evaluation (if enabled) ──
    goldilocks_results = {}
    if args.goldilocks_lane and _GOLDILOCKS_LANE is not None:
        print("\n[Goldilocks Lane] Evaluating microstructure transient spike detection...")
        try:
            g_lane = _GOLDILOCKS_LANE
            available_stations = [s for s in STATIONS if not g_lane.is_station_excluded(s)]
            print(f"  Active stations: {len(available_stations)}/{len(STATIONS)}")

            # Get all unique dates from settlements
            all_dates = set()
            for station_s in settlements.values():
                all_dates.update(station_s.keys())
            all_dates = sorted(all_dates)
            print(f"  Date range: {all_dates[0] if all_dates else 'N/A'} to {all_dates[-1] if all_dates else 'N/A'}")
            print(f"  Total dates: {len(all_dates)}")

            goldilocks_trades = []
            goldilocks_by_station = defaultdict(list)

            for station in available_stations:
                station_trades = 0
                for date_utc in all_dates:
                    results = g_lane.evaluate_day(station, date_utc)
                    for r in results:
                        if r.should_trade:
                            trade = {
                                "station": r.station,
                                "date": date_utc,
                                "bucket_boundary": r.bucket_boundary,
                                "direction": r.direction,
                                "confidence": r.confidence,
                                "hypothesis": r.hypothesis,
                                "correct": None,  # Needs settlement data to evaluate
                            }
                            goldilocks_trades.append(trade)
                            goldilocks_by_station[r.station].append(trade)
                            station_trades += 1

                print(f"    {station}: {station_trades} Goldilocks signals")

            goldilocks_results = {
                "total_signals": len(goldilocks_trades),
                "by_station": {s: len(t) for s, t in goldilocks_by_station.items()},
                "total_trades": len(goldilocks_trades),
            }
            print(f"  Goldilocks Lane: {len(goldilocks_trades)} total signals generated")

        except Exception as e:
            print(f"  \u26a0\ufe0f Goldilocks Lane evaluation failed: {e}")
            import traceback
            traceback.print_exc()
            goldilocks_results = {"error": str(e)}

    # Production Gate check (if enabled)
    if args.production_gate:
        pg = ProductionGate()
        station_trades = result.get("per_station", {})
        station_counts = {st: m.get("n_trades", 0) for st, m in station_trades.items()}
        passes, failures = pg.meets_requirements(
            accuracy=agg.get("accuracy", 0),
            total_trades=agg.get("n_trades", 0),
            station_trade_counts=station_counts,
            sharpe=agg.get("sharpe", 0),
        )
        if passes:
            print(f"    \U0001f4c8 Production Gate: PASS (all requirements met)")
        else:
            print(f"    \U0001f6ab Production Gate: FAIL")
            for f in failures:
                print(f"      - {f}")

    print("\n[Phase 5] Correlation & differential analysis...")
    corr_matrix, corr_names = compute_correlation_matrix(signal_results)
    differentials = compute_differential_results(signal_results)
    print(f"  Correlation: {corr_matrix.shape} | Differentials: {len(differentials)} pairs")

    print("\n[Phase 6] Writing output files...")
    raw_output = {
        "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(),
                     "n_signals": len(signal_results), "n_configs": args.n_configs,
                     "n_stations": len(STATIONS), "date_range": list(dr) if dr else [],
                     "fee_model": "kalshi_real",
                     "validation": "3-stage: discovery -> time-holdout -> geo-holdout",
                     "bias_correction": {
                         "enabled": True,
                         "layer": "member-level (pre-fraction)",
                         "source": "ensemble_fraction_bias_corrections.json",
                         "mode": "always-on for ensemble-fraction signals",
                         "stations_in_table": len(get_bias_corrections().get("bias_table", {})),
                         "matched_pairs": get_bias_corrections().get("matched_pairs", 0),
                     }},
        "signals": signal_results,
        "correlation_matrix": {"names": corr_names, "matrix": corr_matrix.tolist()},
        "differential_results": differentials}

    json_path = os.path.join(str(RESULTS_DIR), "sweep_results_v1.json")
    with open(json_path, "w") as f:
        json.dump(raw_output, f, indent=1, default=str)
    print(f"  Wrote: {json_path}")

    csv_path = os.path.join(str(RESULTS_DIR), "sweep_signal_summary.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal", "n_trades", "accuracy", "total_pnl", "sharpe",
                     "profit_factor", "max_drawdown", "brier_score", "ece",
                     "total_fees", "avg_confidence", "n_stations_active",
                     "val_disc_n", "val_disc_acc", "val_time_n", "val_time_acc",
                     "val_geo_n", "val_geo_acc"])
        for sname in sorted(signal_results):
            r = signal_results[sname]
            agg = r.get("aggregate", {})
            val = r.get("validation", {})
            disc = val.get("discovery", {})
            th = val.get("time_holdout", {})
            gh = val.get("geo_holdout", {})
            w.writerow([sname, agg.get("n_trades",0),
                        f"{agg.get('accuracy',0):.6f}", f"{agg.get('total_pnl',0):.2f}",
                        f"{agg.get('sharpe',0):.6f}", f"{agg.get('profit_factor',0):.6f}",
                        f"{agg.get('max_drawdown',0):.6f}", f"{agg.get('brier_score',0):.6f}",
                        f"{agg.get('ece',0):.6f}", f"{agg.get('total_fees',0):.2f}",
                        f"{agg.get('avg_confidence',0):.6f}", r.get("n_stations_active",0),
                        disc.get("n_trades",0), f"{disc.get('accuracy',0):.6f}",
                        th.get("n_trades",0), f"{th.get('accuracy',0):.6f}",
                        gh.get("n_trades",0), f"{gh.get('accuracy',0):.6f}"])
    print(f"  Wrote: {csv_path}")

    corr_csv = os.path.join(str(RESULTS_DIR), "sweep_correlation_matrix.csv")
    with open(corr_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([""] + corr_names)
        for i, name in enumerate(corr_names):
            w.writerow([name] + [f"{corr_matrix[i,j]:.4f}" for j in range(len(corr_names))])
    print(f"  Wrote: {corr_csv}")

    diff_json = os.path.join(SWEEP_DIR, "differential_results.json")
    with open(diff_json, "w") as f:
        json.dump(differentials, f, indent=2, default=str)
    print(f"  Wrote: {diff_json}")

    print(f"\n  SIGNAL LEADERBOARD (by P&L):")
    leaders = sorted(
        [(s, r) for s, r in signal_results.items()
         if r.get("aggregate", {}).get("n_trades", 0) >= MIN_TRADES_REPORT],
        key=lambda x: x[1].get("aggregate", {}).get("total_pnl", -99999), reverse=True)
    hdr = f"  {'Rank':<5} {'Signal':<30} {'Trades':>7} {'Acc':>7} {'PnL':>10} {'Sharpe':>8} {'Brier':>7} {'ECE':>7}"
    print(hdr)
    print(f"  {'-'*5} {'-'*30} {'-'*7} {'-'*7} {'-'*10} {'-'*8} {'-'*7} {'-'*7}")
    for rank, (sname, result) in enumerate(leaders[:15], 1):
        agg = result.get("aggregate", {})
        print(f"  {rank:<5} {sname:<30} {agg.get('n_trades',0):>7} "
              f"{agg.get('accuracy',0)*100:>6.2f}% "
              f"${agg.get('total_pnl',0):>+8.0f} "
              f"{agg.get('sharpe',0):>8.4f} "
              f"{agg.get('brier_score',0):>7.4f} "
              f"{agg.get('ece',0):>7.4f}")

    # Include Goldilocks results in output
    if args.goldilocks_lane and goldilocks_results:
        raw_output["goldilocks_lane"] = goldilocks_results

    # ── Phase 7: Luck Elimination Protocol (if --luck-test) ──
    luck_results = {}
    if args.luck_test:
        print(f"\n[Phase 7] Luck Elimination Protocol...")
        print(f"  Shuffles per signal: {args.luck_shuffles}")

        # Wire luck-elimination dependencies
        luck_wire_dependencies(
            stations=STATIONS,
            metar_db=METAR_DB,
            safe_signal_evaluate_fn=safe_signal_evaluate,
            compute_metrics_fn=compute_metrics,
        )

        for si, sname in enumerate(signal_names):
            sobj = active.get(sname)
            if sobj is None:
                continue
            sig_result = signal_results.get(sname)
            if sig_result is None:
                continue
            agg = sig_result.get("aggregate", {})
            n_trades = agg.get("n_trades", 0)
            if n_trades < MIN_TRADES_REPORT:
                print(f"\n  [{si+1}/{len(signal_names)}] {sname}: SKIP (only {n_trades} trades)")
                continue

            print(f"\n  [{si+1}/{len(signal_names)}] {sname} ({n_trades} trades)...")

            # Re-run the signal with best config to get full trade list for bootstrap
            best_config = {
                "holdout_start_frac": 0.7,
                "fee_type": 2,
                "edge_threshold": 0.02,
                "kelly_fraction": 0.5,
                "entry_price_min": 0.01,
                "entry_price_max": 0.95,
                "max_contracts": 100,
                "slippage_budget": SLIPPAGE_BUDGET,
                "fee_deduction": 1.0,
                "confidence_floor": 0.5,
                "position_sizing_model": 1,
            }
            # Merge any stored best config if available
            best_config_override = sig_result.get("best_config", {})
            best_config.update(best_config_override)

            # Collect trades for this signal
            luck_trades = []
            for station in STATIONS:
                st_trades = evaluate_signal_on_station(
                    sname, sobj, station, best_config, settlements
                )
                luck_trades.extend(st_trades)

            luck_result = run_luck_elimination(
                signal_obj=sobj,
                signal_name=sname,
                settlements=settlements,
                config=best_config,
                data_cache_get_metar_data=DataCache.get_metar_data,
                trades=luck_trades,
                n_shuffles=args.luck_shuffles,
            )
            luck_results[sname] = luck_result
            print_luck_report(luck_result)

        # Augment raw_output and append luck CSV
        raw_output["luck_elimination"] = luck_results

        luck_csv_path = os.path.join(str(RESULTS_DIR), "sweep_luck_elimination.csv")
        with open(luck_csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "signal", "n_trades", "raw_accuracy", "luck_adjusted_accuracy",
                "p_value", "significant_05", "significant_01", "percentile_in_null",
                "bootstrap_ci_lower", "bootstrap_ci_upper",
                "null_mean", "null_median", "null_std", "null_n_shuffles",
            ])
            for sname in sorted(luck_results):
                lr = luck_results[sname]
                if lr is None:
                    continue
                w.writerow([
                    sname,
                    lr["n_trades"],
                    f"{lr['raw_accuracy']:.6f}",
                    f"{lr['luck_adjusted_accuracy']:.6f}",
                    f"{lr['p_value']:.6f}",
                    "YES" if lr["p_value_significant"] else "NO",
                    "YES" if lr["p_value_highly_significant"] else "NO",
                    f"{lr['percentile_in_null']:.6f}",
                    f"{lr['bootstrap_ci_lower']:.6f}",
                    f"{lr['bootstrap_ci_upper']:.6f}",
                    f"{lr['null_mean']:.6f}",
                    f"{lr['null_median']:.6f}",
                    f"{lr['null_std']:.6f}",
                    lr["null_n_shuffles"],
                ])
        print(f"\n  Wrote: {luck_csv_path}")

        # Re-persist raw output with luck data
        with open(json_path, "w") as f:
            json.dump(raw_output, f, indent=1, default=str)
        print(f"  Updated: {json_path}")

    # ── Phase 8: Fusion Evaluation (if --fusion enabled) ──
    if _FUSION_MODE != "none":
        label = "Fusion + Variance-Weighted Sizing" if _VARIANCE_SIZING_ENABLED else "Fusion"
        print(f"\n[Phase 7] {label} evaluation (mode={_FUSION_MODE})...")
        fusion_results = evaluate_fusion(
            signal_results, signal_names, active, settlements, configs
        )
        if fusion_results:
            raw_output["fusion"] = fusion_results
            agg = fusion_results.get("aggregate", {})
            print(f"  FUSION -> Trades: {agg.get('n_trades', 0)} | "
                  f"Acc: {agg.get('accuracy', 0)*100:.2f}% | "
                  f"PnL: ${agg.get('total_pnl', 0):+.0f} | "
                  f"Sharpe: {agg.get('sharpe', 0):.4f}")

    print(f"\n{'=' * 72}")
# Fusion mode (set from --fusion arg)
_FUSION_MODE: str = 'none'

# Variance-Weighted Sizing
_VARIANCE_SIZING_ENABLED: bool = False
def get_variance_sizing_enabled() -> bool:
    return _VARIANCE_SIZING_ENABLED

def get_fusion_mode() -> str:
    return _FUSION_MODE
# ═══════════════════════════════════════════════════════════════════
# 9. FUSION EVALUATION
# ═══════════════════════════════════════════════════════════════════

def evaluate_fusion(
    signal_results: Dict[str, Any],
    signal_names: List[str],
    registry: Dict[str, Any],
    settlements: Dict[str, Any],
    configs: List[Dict],
) -> Dict[str, Any]:
    """
    Evaluate the fusion layer across all stations and dates.

    Collects predictions from all active signals at each (station, date),
    applies the selected fusion mode, and produces a unified trade signal.
    """
    mode = get_fusion_mode()
    if mode == "none":
        return {}

    fusion_cfg = FusionModeConfig(mode=mode)
    cascade = UncertaintyWeightedCascade(fusion_cfg)

    # Use the best config from the first active signal as a representative
    rep_config = {}
    for sname in signal_names:
        sr = signal_results.get(sname, {})
        best = sr.get("best_config")
        if best:
            rep_config = best
            break

    if not rep_config:
        rep_config = configs[0] if configs else generate_sweep_configs(1)[0]

    # Build fusion signal predictions for each (station, day)
    all_trades = []
    for station in STATIONS:
        station_result = _evaluate_fusion_station(
            station, registry, settlements, rep_config, cascade, mode
        )
        all_trades.extend(station_result)

    all_metrics = compute_metrics(all_trades, min_trades=20)
    validation = _run_fusion_validation(registry, settlements, rep_config, cascade, mode)

    return {
        "aggregate": all_metrics,
        "validation": validation,
        "n_signals_fused": len(registry),
    }
def _evaluate_fusion_station(
    station: str,
    registry: Dict[str, Any],
    settlements: Dict[str, Any],
    config: Dict,
    cascade: UncertaintyWeightedCascade,
    mode: str,
) -> List[Dict]:
    """Evaluate fusion for a single station."""
    days = DataCache.get_metar_data(station)
    if len(days) < 5:
        return []
    station_s = settlements.get(station, {})
    if not station_s:
        return []

    trades = []
    min_lb = 2
    for idx in range(min_lb, len(days)):
        date_str = days[idx]["date"]
        if date_str not in station_s:
            continue
        actual_temp = station_s[date_str]
        prev_date = days[idx - 1]["date"]
        if prev_date not in station_s:
            continue
        diff = actual_temp - station_s[prev_date]
        if diff == 0:
            continue
        actual_dir = 1 if diff > 0 else -1

        # Collect predictions from all active signals
        signal_predictions: Dict[str, Tuple[Union[str, int], float]] = {}
        for sig_name, sig_obj in registry.items():
            if sig_obj is None:
                continue
            try:
                pd, cf = safe_signal_evaluate(sig_obj, idx, days)
                if pd is not None and cf is not None and cf >= 0.5:
                    signal_predictions[sig_name] = (pd, cf)
            except Exception:
                continue

        if len(signal_predictions) < 2:
            continue

        if _VARIANCE_SIZING_ENABLED:
            # Variance-weighted blending
            sig_variances: Dict[str, float] = {}
            for sig_name in signal_predictions:
                sig_variances[sig_name] = 0.1  # default moderate variance

            direction, confidence, weight_details = variance_weighted_blend(
                signal_predictions, sig_variances,
            )
            if direction is None:
                continue

            # Determine market price for this station/date
            market_type = DataCache.get_market_type(station, date_str)
            market_price = 0.5  # Default midpoint
            try:
                from core.kalshi_price_fetcher import KalshiPriceFetcher
                pricer = KalshiPriceFetcher()
                mp = pricer.get_price(station, date_str, market_type)
                if mp is not None:
                    market_price = mp
            except Exception:
                pass

            fee_per_contract = 0.02
            edge = confidence - market_price - fee_per_contract
            if edge <= 0:
                continue

            # Apply variance-adjusted Kelly sizing via position_sizing_model override
            # Signal passes through to simulate_trade with adjusted confidence
            # The variance multiplier will be applied in the trade sizing step
            config_override = dict(config)
            config_override['position_sizing_model'] = 5  # variance-sized
            config_override['variance_multiplier'] = weight_details.get('directional_strength', 0.5)

        elif mode == "uwc":
            result = cascade.fuse(
                signal_predictions=signal_predictions,
                market_price=0.5,
                fee_rate=KALSHI_REAL_FEE_RATE,
                bankroll=10000.0,
                hours_to_settlement=24.0,
            )
            if result["verdict"] != "TRADE" or result["direction"] is None:
                continue
            direction = result["direction"]
            confidence = result["confidence"]
        elif mode == "majority":
            direction, confidence, _ = fuse_majority_vote(signal_predictions)
            if direction is None:
                continue
        elif mode == "weighted":
            direction, confidence, _ = fuse_weighted_vote(signal_predictions)
            if direction is None:
                continue
        else:
            continue

        # Calibrate
        cal_result = calibrate_confidence(station, direction, "fusion", confidence, date_str)
        if cal_result is None:
            continue
        cal_conf, raw_conf = cal_result

        # Use variance-weighted config override when variance sizing is active
        effective_config = config
        if _VARIANCE_SIZING_ENABLED:
            try:
                effective_config = config_override
            except NameError:
                effective_config = config

        # Trade
        trade = simulate_trade(direction, cal_conf, actual_dir, effective_config, station, date_str, signal_name='fusion')
        if trade:
            trade["raw_confidence"] = raw_conf
            trade["calibrated_confidence"] = cal_conf
            trades.append(trade)

        # Record outcome for adaptive threshold learning
        if _ADAPTIVE_THRESHOLDS_ENABLED and _ADAPTIVE_REGISTRY is not None:
            was_correct = direction == actual_dir
            _ADAPTIVE_REGISTRY.record_outcome('fusion', station, cal_conf, was_correct)

    return trades
def _run_fusion_validation(
    registry: Dict[str, Any],
    settlements: Dict[str, Any],
    config: Dict,
    cascade: UncertaintyWeightedCascade,
    mode: str,
) -> Dict[str, Any]:
    """Run 3-stage validation for fusion mode."""
    # Fixed chronological holdout (no longer a tunable parameter)
    discovery, time_holdout = split_discovery_holdout(
        settlements, holdout_frac=0.7
    )
    # Fixed-seed random geographic holdout (not alphabetical)
    _, geo_holdout = split_geo_holdout(settlements)

    disc_trades = []
    for st in sorted(discovery.keys()):
        disc_trades.extend(_evaluate_fusion_station(st, registry, discovery, config, cascade, mode))
    time_trades = []
    for st in sorted(time_holdout.keys()):
        time_trades.extend(_evaluate_fusion_station(st, registry, time_holdout, config, cascade, mode))
    geo_trades = []
    for st in sorted(geo_holdout.keys()):
        geo_trades.extend(_evaluate_fusion_station(st, registry, geo_holdout, config, cascade, mode))

    return {
        "discovery": compute_metrics(disc_trades, min_trades=20),
        "time_holdout": compute_metrics(time_trades, min_trades=20),
        "geo_holdout": compute_metrics(geo_trades, min_trades=20),
    }

def phase2_sweep(args):
    """Phase 2: Meta-sweep over gates/levers/lanes/modulators."""
    sep = "=" * 72
    print()
    print(sep)
    print("  BIG SWEEP - Phase 2: Meta-Sweep")
    print("  Testing gate/lever/lane/modulator combinations")
    print(sep)

    top_cfg = os.path.join(str(REPO_ROOT), "data", "sweep_phase1_top_configs.json")
    if not os.path.exists(top_cfg):
        print("  Error: Phase 1 results not found.")
        print("  Run Phase 1 first: python3 scripts/big_sweep.py --phase 1")
        return {}
    with open(top_cfg) as f:
        top_configs = json.load(f)
    print(f"  Loaded top configs for {len(top_configs)} signals")

    from core.gate_pipeline import GatePipeline
    from core.lever_manager import LeverManager
    from core.lane_manager_v2 import LaneManagerV2
    from core.modulator_stack import ModulatorStack

    print()
    print("[Phase 2] Generating meta-configs...")
    meta_configs = generate_meta_configs(args.n_meta_configs)
    print(f"  Generated {len(meta_configs)} meta-configs")

    results = []
    for i, mc in enumerate(meta_configs):
        if i > 0 and i % 50 == 0:
            print(f"  [{i}/{len(meta_configs)}] Processed {i}")

        gp = GatePipeline(mc)
        lm = LeverManager(mc)
        lane = LaneManagerV2({})
        mod = ModulatorStack(mc)

        trades = []
        for sname in list(top_configs.keys())[:10]:
            for station in STATIONS[:5]:
                try:
                    mod_r = mod.apply(station, sname, None, 0.5)
                    g_r = gp.evaluate(sname, station, "", None, 0.5)
                    if not g_r.get("pass", True):
                        continue
                    nc = lm.compute_position(sname, station, None, 0.5, 0.5, None)
                    if nc > 0:
                        trades.append({"station": station, "signal": sname, "contracts": nc})
                except Exception:
                    pass
        results.append({"config_index": i, "n_trades": len(trades)})

    out = {
        "metadata": {"phase": 2, "timestamp": datetime.now(timezone.utc).isoformat(),
                     "n_meta_configs": len(meta_configs)},
        "results": results,
        "best_result": max(results, key=lambda r: r["n_trades"]) if results else {},
    }
    out_path = os.path.join(str(RESULTS_DIR), "sweep_phase2_results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"  Results written to {out_path}")
    return out


if __name__ == "__main__":
    main()
