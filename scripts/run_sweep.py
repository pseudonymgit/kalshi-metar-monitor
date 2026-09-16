#!/usr/bin/env python3
"""
WS5 Sweep Script: Primary Directional Sweep (Phase 2)
================================================================
PRIMARY RUN PATH — This is the sole entry point for the primary
directional sweep. All other sweep scripts (gefs_grid_sweep.py,
kalshi_sweep_eval.py) serve different purposes and their outputs
are NOT part of the primary sweep claims.

SCOPE (what this sweep WILL claim):
1. Directional accuracy (correct/incorrect UP vs DOWN) under
   temporal cross-validation — the verified core metric.
2. Per-signal accuracy and fire rates.
3. Calibration quality (Brier, ECE, Brier improvement).
4. Config ranking by directional accuracy across walk-forward folds.
5. Comparison vs identity baseline (no calibration).
6. Comparison vs all-signals default (degenerates included).

SCOPE EXCLUSIONS (what this sweep will NOT claim):
- Real P&L, Sharpe, profit factor — these are (PROXY) only
  (flat $0.0205 fee, $1/trade notional, synthetic fills).
- Kelly-optimal sizing (kelly_fraction is a linear scalar only).
- Market microstructure conclusions.
- Geographic generalization (20 US stations only).
- Intraday timing edge.
- Optimal entry prices.
- Platt-scaling calibration quality (deferred — requires market data).

Usage:
    python scripts/run_sweep.py [--fusion {llop,uwc}] [--n-configs N]
                                [--folds {1,4}] [--calibration-cutoff DATE]

Primary grid configs:
    - Fusion: llop, uwc
    - Calibration: isotonic (only; Platt deferred — see S1)
    - Confidence floors: 0.50, 0.55, 0.60
    - Kelly fractions: 0.25, 0.50, 1.0

References:
    PRE-SWEEP-IMPLEMENTATION-SPECS-2026-08-13.md (S1-S7)
"""

import argparse
import copy
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.signals import SignalRegistry, REGISTERED_SIGNAL_NAMES
from core.fusion_logic import SignalFusionEngine
from core.signal_fusion import UncertaintyWeightedCascade, FusionModeConfig
from core.calibration_pipeline import CalibrationPipeline
from scripts.sweep import config as sweep_config
from scripts.sweep.config import STATIONS, CALIBRATION_PIPELINE_OPTIONS
from scripts.ws5_common import (
    load_settlement_data,
    load_daily_stats_batched,
    compute_fusion_accuracy,
    compute_pnl_metrics,
    compute_baseline_accuracies,
)

from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES

# ── P&L keys that are (PROXY) — excluded from ranking (S7) ──
PROXY_PNL_KEYS = [
    "net_pnl", "net_pnl_per_trade", "sharpe", "profit_factor",
    "kelly_adjusted_pnl", "win_rate", "max_drawdown",
]

# ── Nested fold definitions (S2, corrected) ──
# Inner selection: 4 folds, eval windows tile [2025-05-01, 2026-05-14]
# Outer test: single holdout [2026-05-15, 2026-08-13]
NESTED_FOLDS = [
    {
        "fold": 1,
        "calibration_cutoff": "2025-04-30",
        "eval_start": "2025-05-01",
        "eval_end": "2025-08-31",
        "holdout_start": "2025-09-01",
        "holdout_end": "2025-11-30",
    },
    {
        "fold": 2,
        "calibration_cutoff": "2025-08-31",
        "eval_start": "2025-09-01",
        "eval_end": "2025-11-30",
        "holdout_start": "2025-12-01",
        "holdout_end": "2026-02-28",
    },
    {
        "fold": 3,
        "calibration_cutoff": "2025-11-30",
        "eval_start": "2025-12-01",
        "eval_end": "2026-02-28",
        "holdout_start": "2026-03-01",
        "holdout_end": "2026-05-14",
    },
    {
        "fold": 4,
        "calibration_cutoff": "2026-02-28",
        "eval_start": "2026-03-01",
        "eval_end": "2026-05-14",
        "holdout_start": "2026-05-15",
        "holdout_end": "2026-08-13",
    },
]

OUTER_HOLDOUT_START = "2026-05-15"
OUTER_HOLDOUT_END = "2026-08-13"


def generate_config_grid(n_configs):
    """
    Generate a parameter grid of (fusion, calibration, confidence_floor, kelly_fraction).

    Calibration methods drawn from config.CALIBRATION_PIPELINE_OPTIONS
    (primary grid: ["isotonic"] only). This ensures the config label always
    truthfully describes the calibration method actually used (S1).
    """
    fusion_methods = ["llop", "uwc"]
    # S1: isotonic-only primary grid; Platt deferred
    calibration_methods = list(CALIBRATION_PIPELINE_OPTIONS)
    if not calibration_methods:
        calibration_methods = ["isotonic"]
    confidence_floors = [0.50, 0.55, 0.60]
    kelly_fractions = [0.25, 0.50, 1.0]

    configs = []
    for fusion in fusion_methods:
        for cal in calibration_methods:
            for cf in confidence_floors:
                for kf in kelly_fractions:
                    if len(configs) >= n_configs:
                        return configs
                    configs.append(
                        {
                            "fusion": fusion,
                            "calibration": cal,
                            "confidence_floor": cf,
                            "kelly_fraction": kf,
                        }
                    )
    return configs


def build_fuser(fusion: str, station_list, calibration_method: str = "isotonic"):
    """Build a fusion engine for the given method. (S1)"""
    if fusion == "uwc":
        fconfig = FusionModeConfig(mode="uwc", floor_type="none")
        fuser = UncertaintyWeightedCascade(fconfig)
    else:
        fuser = SignalFusionEngine(
            signal_names=REGISTERED_SIGNAL_NAMES, city_codes=station_list
        )
    return fuser


def evaluate_config_on_fold(
    config, settlements, metar_batched, registry, station_list,
    calibration_cutoff, holdout_start, holdout_end, period, exclude_degenerate,
):
    """
    Evaluate a single config on one fold. Returns result dict or None on failure.
    """
    fusion = config["fusion"]
    cal = config["calibration"]

    fuser = build_fuser(fusion, station_list, cal)

    try:
        result = compute_fusion_accuracy(
            settlements=settlements,
            metar_batched=metar_batched,
            registry=registry,
            fuser=fuser,
            fusion_method=fusion,
            station_list=station_list,
            confidence_floor=config.get("confidence_floor", 0.0),
            kelly_fraction=config.get("kelly_fraction", 1.0),
            calibration_cutoff_date=calibration_cutoff,
            holdout_start=holdout_start,
            holdout_end=holdout_end,
            period=period,
            exclude_degenerate=exclude_degenerate,
            calibration_method=cal,
        )
    except Exception as e:
        logger.warning("Config %s failed on %s period=%s: %s", config, calibration_cutoff, period, e)
        return None

    # Compute P&L from predictions (PROXY only — S7)
    pnl = compute_pnl_metrics(
        result["predictions"],
        fee_model="taker_only",
        kelly_fraction=config.get("kelly_fraction", 1.0),
    )

    # Determine actual calibration state
    is_calibrated = hasattr(fuser, 'is_calibrated') and fuser.is_calibrated()

    return {
        "params": config,
        "accuracy": result.get("accuracy", 0.0),
        "trades": result.get("total_trades", 0),
        "net_pnl": pnl.get("net_pnl", 0.0),
        "net_pnl_per_trade": pnl.get("net_pnl_per_trade", 0.0),
        "sharpe": pnl.get("sharpe_ratio", 0.0),
        "profit_factor": pnl.get("profit_factor", 0.0),
        "kelly_adjusted_pnl": pnl.get("kelly_adjusted_pnl", 0.0),
        "win_rate": pnl.get("win_rate", 0.0),
        "max_drawdown": pnl.get("max_drawdown", 0.0),
        "is_calibrated": is_calibrated,
        "calibration_method": cal,
        "calibration_actually_applied": is_calibrated,
        "failed": "zero_trades" if result.get("total_trades", 0) == 0 else None,
        # S3 fields from compute_fusion_accuracy
        "calibration_quality": result.get("calibration_quality", {}),
        "failure_counters": result.get("failure_counters", {}),
        # GAP1/GAP7: per-signal and per-station diagnostic payload (persist
        # through the fold boundary so aggregation can surface it)
        "per_signal": result.get("per_signal", {}),
        "per_station": result.get("per_station", {}),
        # S5: successful fraction — trades vs dates considered
        "pct_successful": (
            round(result.get("total_trades", 0) / max(1, result.get("filtered_trades", 0)), 4)
            if result.get("filtered_trades", 0) else 0.0
        ),
    }


# ══════════════════════════════════════════════════════════════════════
# Aggregation helpers (GAP1/GAP2/GAP3/GAP7 — persist diagnostics through
# the fold-aggregation boundary instead of dropping them)
# ══════════════════════════════════════════════════════════════════════

def _avg_numeric_dicts(dicts):
    """Average numeric fields across a list of dicts. Non-numeric/absent fields skipped."""
    if not dicts:
        return {}
    keys = set()
    for d in dicts:
        keys.update(k for k, v in d.items() if isinstance(v, (int, float)))
    out = {}
    for k in keys:
        vals = [d.get(k, 0.0) for d in dicts if isinstance(d.get(k), (int, float))]
        if vals:
            out[k] = round(sum(vals) / len(vals), 4)
    return out


def aggregate_per_signal(fold_records):
    """
    Aggregate per-signal stats across folds.

    fold_records: list of per-fold config result dicts (each has .per_signal).
    Returns {signal: {accuracy, fire_rate, fired, total, brier_raw, brier_calibrated}}
    with fired/total summed and accuracy/brier re-derived from pooled counts.
    """
    pooled = defaultdict(lambda: {"fired": 0, "total": 0, "correct": 0,
                                  "raw_sq": 0.0, "cal_sq": 0.0, "n_eval": 0})
    for cr in fold_records:
        for sig, stats in (cr.get("per_signal") or {}).items():
            p = pooled[sig]
            p["fired"] += stats.get("fired", 0)
            p["total"] += stats.get("total", 0)
            p["correct"] += stats.get("correct", 0)
            # Reconstruct squared-error sums from per-signal Brier means
            # (brier = mean squared error over n_eval samples)
            n = stats.get("n_eval", 0)
            if n is None:
                n = 0
            p["raw_sq"] += stats.get("brier_raw", 0.0) * n
            p["cal_sq"] += stats.get("brier_calibrated", 0.0) * n
            p["n_eval"] += n

    out = {}
    for sig, p in sorted(pooled.items()):
        out[sig] = {
            "accuracy": round(p["correct"] / p["fired"], 4) if p["fired"] > 0 else 0.0,
            "fire_rate": round(p["fired"] / p["total"], 4) if p["total"] > 0 else 0.0,
            "fired": p["fired"],
            "correct": p["correct"],
            "total": p["total"],
            "brier_raw": round(p["raw_sq"] / p["n_eval"], 6) if p["n_eval"] > 0 else 0.0,
            "brier_calibrated": round(p["cal_sq"] / p["n_eval"], 6) if p["n_eval"] > 0 else 0.0,
        }
    return out


def aggregate_per_station(fold_records):
    """Sum per-station correct/trades across folds; re-derive accuracy."""
    pooled = defaultdict(lambda: {"correct": 0, "total": 0})
    for cr in fold_records:
        for st, stats in (cr.get("per_station") or {}).items():
            pooled[st]["correct"] += stats.get("correct", stats.get("trades", 0) * stats.get("accuracy", 0))
            pooled[st]["total"] += stats.get("trades", stats.get("total", 0))
    return {
        st: {
            "accuracy": round(d["correct"] / d["total"], 4) if d["total"] > 0 else 0.0,
            "trades": d["total"],
        }
        for st, d in sorted(pooled.items())
    }


def aggregate_calibration_quality(fold_records):
    """
    Weighted-average Brier/ECE across folds (weighted by eval sample count).
    Also pools reliability bins (ece_bins) by summing bin counts per bin index.
    """
    n_bins = 10
    bin_counts = [0] * n_bins
    bin_acc_sum = [0.0] * n_bins
    bin_conf_sum = [0.0] * n_bins

    tot_raw = tot_cal = tot_n = 0.0
    ece_sum = 0.0
    ece_n = 0

    for cr in fold_records:
        cq = cr.get("calibration_quality") or {}
        n = cq.get("_n_eval", 0)
        if n is None:
            n = 0
        tot_raw += cq.get("brier_raw", 0.0) * n
        tot_cal += cq.get("brier_calibrated", 0.0) * n
        tot_n += n
        if cq.get("ece") is not None:
            ece_sum += cq.get("ece", 0.0) * (n if n > 0 else 1)
            ece_n += (n if n > 0 else 1)

        for bin_row in (cq.get("ece_bins") or []):
            if len(bin_row) < 3:
                continue
            idx = min(int(bin_row[0] * n_bins), n_bins - 1)
            bin_counts[idx] += bin_row[2]
            bin_acc_sum[idx] += bin_row[3] * bin_row[2]
            bin_conf_sum[idx] += bin_row[4] * bin_row[2]

    ece_bins = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        n_k = bin_counts[i]
        if n_k == 0:
            ece_bins.append([round(lo, 4), round(hi, 4), 0, 0.0, round((lo + hi) / 2.0, 4)])
        else:
            ece_bins.append([round(lo, 4), round(hi, 4), n_k,
                             round(bin_acc_sum[i] / n_k, 4),
                             round(bin_conf_sum[i] / n_k, 4)])

    brier_raw = tot_raw / tot_n if tot_n > 0 else 0.0
    brier_cal = tot_cal / tot_n if tot_n > 0 else 0.0
    ece = ece_sum / ece_n if ece_n > 0 else 0.0

    return {
        "brier_raw": round(brier_raw, 6),
        "brier_calibrated": round(brier_cal, 6),
        "brier_improvement": round(brier_raw - brier_cal, 6),
        "ece": round(ece, 6),
        "ece_bins": ece_bins,
        "_n_eval": int(tot_n),
    }


def aggregate_failure_counters(fold_records):
    """Sum all failure counters across folds and derive pct_successful."""
    totals = defaultdict(int)
    total_trades = 0
    for cr in fold_records:
        fc = cr.get("failure_counters") or {}
        for k, v in fc.items():
            totals[k] += v or 0
        total_trades += cr.get("trades", 0)
    totals["_pct_successful"] = (
        round(total_trades / max(1, total_trades + sum(totals.values())), 4)
        if total_trades > 0 else 0.0
    )
    return dict(totals)


def _aggregate_failure_accounting(config_records):
    """
    Sum failure counters across all configs' aggregated records (GAP3).
    Each config record already carries aggregated per-config failure_counters;
    sum across configs for the sweep-level accounting block.
    """
    totals = defaultdict(int)
    for c in config_records:
        fc = c.get("failure_counters") or {}
        for k, v in fc.items():
            if k == "_pct_successful":
                continue
            totals[k] += v or 0
    totals = dict(totals)
    total_trades = sum(c.get("total_trades", c.get("trades", 0)) for c in config_records)
    failure_sum = sum(v for k, v in totals.items())
    totals["_pct_successful"] = (
        round(total_trades / max(1, total_trades + failure_sum), 4)
        if total_trades > 0 else 0.0
    )
    return totals


def run_sweep_single_split(
    settlements, metar_batched, registry, fusion_method, calibration_pipeline,
    n_configs, holdout_start, holdout_end, exclude_degenerate,
    calibration_cutoff_date,
):
    """
    Single-split sweep (--folds 1, default).
    Preserves current behavior with corrected cutoff plumbing.
    """
    station_list = [s for s in STATIONS if s in settlements and s in metar_batched]

    configs = generate_config_grid(n_configs)
    config_results = []

    for config in configs:
        cr = evaluate_config_on_fold(
            config, settlements, metar_batched, registry, station_list,
            calibration_cutoff=calibration_cutoff_date,
            holdout_start=holdout_start, holdout_end=holdout_end,
            period="eval", exclude_degenerate=exclude_degenerate,
        )
        if cr is None:
            continue

        # S5: zero-trade guard — exclude from ranking
        if cr.get("failed") == "zero_trades":
            logger.warning("Config %s scored zero trades — excluded from ranking", config)
            continue

        config_results.append(cr)

    # S7: Accuracy-only ranking assertion
    config_results.sort(key=lambda x: x["accuracy"], reverse=True)
    assert_s7_ranking_invariant(config_results)

    best = config_results[0] if config_results else None

    # Holdout evaluation
    holdout_eval = None
    if best and holdout_start:
        logger.info(
            "Evaluating best config on holdout period %s to %s",
            holdout_start, holdout_end
        )
        best_config = best["params"]
        ho_result = evaluate_config_on_fold(
            best_config, settlements, metar_batched, registry, station_list,
            calibration_cutoff=calibration_cutoff_date,
            holdout_start=holdout_start, holdout_end=holdout_end,
            period="holdout", exclude_degenerate=exclude_degenerate,
        )
        if ho_result:
            ho_cq = ho_result.get("calibration_quality") or {}
            holdout_eval = {
                "accuracy": ho_result["accuracy"],
                "trades": ho_result["trades"],
                "net_pnl": ho_result["net_pnl"],
                "sharpe": ho_result["sharpe"],
                "profit_factor": ho_result["profit_factor"],
                # GAP2: holdout-specific calibration metrics
                "brier_raw": ho_cq.get("brier_raw", 0.0),
                "brier_calibrated": ho_cq.get("brier_calibrated", 0.0),
                "brier_improvement": ho_cq.get("brier_improvement", 0.0),
                "ece": ho_cq.get("ece", 0.0),
                "per_signal": ho_result.get("per_signal", {}),
                "per_station": ho_result.get("per_station", {}),
                "failure_counters": ho_result.get("failure_counters", {}),
            }

    # Baseline accuracies
    eval_baselines = compute_baseline_accuracies(
        settlements, station_list, epsilon=0.5,
        holdout_start=holdout_start, holdout_end=holdout_end, period="eval",
    )
    holdout_baselines = compute_baseline_accuracies(
        settlements, station_list, epsilon=0.5,
        holdout_start=holdout_start, holdout_end=holdout_end, period="holdout",
    )

    # Calibration provenance
    calibration_provenance = {
        "configured_pipeline": calibration_pipeline,
        "actually_used": sweep_config.DEFAULT_CALIBRATION_PIPELINE,
        "is_calibrated_any": any(c.get("is_calibrated", False) for c in config_results),
        "failed_signal_evaluate_total": sum(
            c.get("failure_counters", {}).get("n_signal_exceptions", 0)
            for c in config_results
        ),
        "failed_fusion_total": sum(
            c.get("failure_counters", {}).get("n_fusion_exceptions", 0)
            for c in config_results
        ),
    }

    return {
        "fold_mode": "single_split",
        "fusion_method": fusion_method,
        "calibration_pipeline": calibration_pipeline,
        "calibration_provenance": calibration_provenance,
        "ranking_metric": "accuracy",
        "n_configs": n_configs,
        "n_configs_evaluated": len(config_results),
        # GAP5: honest effective directional test count
        "effective_config_scope": {
            "n_configs_requested": n_configs,
            "n_configs_evaluated": len(config_results),
            "n_unique_directional_configs": len(
                {
                    (c["params"]["fusion"], c["params"]["confidence_floor"])
                    for c in config_results
                }
            ),
            "directional_dimensions": ["fusion", "confidence_floor"],
            "cosmetic_dimensions": ["kelly_fraction", "calibration"],
            "kelly_note": (
                "kelly_fraction varies 3 ways but has zero effect on directional "
                "accuracy — it only scales (PROXY) P&L. Unique directional tests "
                "= fusion x confidence_floor."
            ),
        },
        "best_accuracy": best["accuracy"] if best else 0.0,
        "best_trades": best["trades"] if best else 0,
        "best_pnl": best["net_pnl"] if best else 0.0,
        "best_sharpe": best["sharpe"] if best else 0.0,
        "best_kelly_adjusted_pnl": best["kelly_adjusted_pnl"] if best else 0.0,
        "best_config": best["params"] if best else None,
        # GAP1/GAP2/GAP7: best-config diagnostics
        "best_per_signal": best.get("per_signal", {}) if best else {},
        "best_per_station": best.get("per_station", {}) if best else {},
        "best_calibration_quality": best.get("calibration_quality", {}) if best else {},
        "best_failure_counters": best.get("failure_counters", {}) if best else {},
        # GAP3: failure accounting summed across ALL configs
        "failure_accounting": _aggregate_failure_accounting(config_results),
        "holdout_eval": holdout_eval,
        "baselines": {
            "eval": eval_baselines,
            "holdout": holdout_baselines,
        },
        "configs": config_results,
    }


def run_sweep_nested(
    settlements, metar_batched, registry, fusion_method, calibration_pipeline,
    n_configs, exclude_degenerate,
):
    """
    True nested temporal validation (--folds 4).

    4 inner folds for config selection, 1 outer holdout for final test.
    See PRE-SWEEP-IMPLEMENTATION-SPECS-2026-08-13.md §S2 for full design.
    """
    station_list = [s for s in STATIONS if s in settlements and s in metar_batched]

    configs = generate_config_grid(n_configs)
    fold_results = {}  # {fold_idx: {config_key: result}}

    # ── Inner folds (config selection) ──
    for fold_def in NESTED_FOLDS:
        fold_idx = fold_def["fold"]
        logger.info(
            "=== Fold %d: cutoff=%s, eval=%s..%s (holdout %s..%s) ===",
            fold_idx, fold_def["calibration_cutoff"],
            fold_def["eval_start"], fold_def["eval_end"],
            fold_def["holdout_start"], fold_def["holdout_end"],
        )

        fold_config_results = []
        for config in configs:
            cr = evaluate_config_on_fold(
                config, settlements, metar_batched, registry, station_list,
                calibration_cutoff=fold_def["calibration_cutoff"],
                holdout_start=fold_def["holdout_start"],
                holdout_end=fold_def["holdout_end"],
                period="eval", exclude_degenerate=exclude_degenerate,
            )
            if cr is None:
                continue
            if cr.get("failed") == "zero_trades":
                logger.warning(
                    "Config %s zero trades on fold %d — excluded",
                    config, fold_idx
                )
                continue

            # AT2.5: every fold must report total_trades > 0
            assert cr.get("trades", 0) > 0, \
                f"Fold {fold_idx} config {config} has zero trades — guard violated"
            fold_config_results.append(cr)

        fold_results[fold_idx] = fold_config_results

    # ── Aggregate across folds: mean accuracy per config ──
    config_keys_seen = set()
    for fold_idx, results in fold_results.items():
        for cr in results:
            key = tuple(sorted(cr["params"].items()))
            config_keys_seen.add(key)

    aggregated = []
    for key in config_keys_seen:
        accuracies = []
        all_cr = None
        fold_records = []  # per-fold records for this config (GAP1/GAP2/GAP3)
        valid_folds = 0
        for fold_idx in sorted(fold_results.keys()):
            for cr in fold_results[fold_idx]:
                if tuple(sorted(cr["params"].items())) == key:
                    accuracies.append(cr["accuracy"])
                    all_cr = cr
                    fold_records.append(cr)
                    valid_folds += 1
                    break

        if valid_folds < 1:
            continue

        mean_acc = sum(accuracies) / len(accuracies)
        std_acc = (
            (sum((a - mean_acc) ** 2 for a in accuracies) / len(accuracies)) ** 0.5
            if len(accuracies) > 1 else 0.0
        )

        # GAP1/GAP2/GAP3/GAP7: aggregate diagnostics across this config's folds
        per_signal_agg = aggregate_per_signal(fold_records)
        per_station_agg = aggregate_per_station(fold_records)
        cal_quality_agg = aggregate_calibration_quality(fold_records)
        failure_agg = aggregate_failure_counters(fold_records)

        aggregated.append({
            "params": all_cr["params"],
            "mean_accuracy": mean_acc,
            "std_accuracy": std_acc,
            "min_accuracy": min(accuracies),
            "max_accuracy": max(accuracies),
            "n_folds": len(accuracies),
            "fold_accuracies": [round(a, 4) for a in accuracies],
            "total_trades": sum(cr.get("trades", 0) for cr in fold_records),
            "per_signal": per_signal_agg,
            "per_station": per_station_agg,
            "calibration_quality": cal_quality_agg,
            "failure_counters": failure_agg,
            # QC1.2: pseudo_pnl set to None — P&L is per-fold only and
            # MUST NOT be presented as a cross-fold aggregate.
            # Per-fold P&L unavailable in aggregated representation.
            "pseudo_pnl": None,
            "pseudo_pnl_note": (
                "P&L not aggregated across folds — per-fold trade P&L "
                "unavailable in aggregated representation. "
                "See holdout_eval for single synthetic P&L estimate."
            ),
            "is_calibrated": all_cr.get("is_calibrated", False),
            "calibration_method": all_cr.get("calibration_method", "isotonic"),
        })

    # Sort by mean accuracy descending (S7: accuracy-only ranking)
    aggregated.sort(key=lambda x: x["mean_accuracy"], reverse=True)
    assert_s7_ranking_invariant(aggregated)

    best_agg = aggregated[0] if aggregated else None

    # ── Outer holdout evaluation (single, true) ──
    # Evaluate best config on [2026-05-15, 2026-08-13] with calibration cutoff=2026-02-28
    holdout_eval = None
    if best_agg:
        best_config = best_agg["params"]
        logger.info(
            "=== Outer holdout: eval best config on %s..%s ===",
            OUTER_HOLDOUT_START, OUTER_HOLDOUT_END
        )
        ho_cr = evaluate_config_on_fold(
            best_config, settlements, metar_batched, registry, station_list,
            calibration_cutoff="2026-02-28",
            holdout_start=OUTER_HOLDOUT_START,
            holdout_end=OUTER_HOLDOUT_END,
            period="holdout", exclude_degenerate=exclude_degenerate,
        )
        if ho_cr:
            ho_cq = ho_cr.get("calibration_quality") or {}
            holdout_eval = {
                "accuracy": ho_cr["accuracy"],
                "trades": ho_cr["trades"],
                "net_pnl": ho_cr["net_pnl"],
                "sharpe": ho_cr["sharpe"],
                "profit_factor": ho_cr["profit_factor"],
                # GAP2: holdout-specific calibration metrics
                "brier_raw": ho_cq.get("brier_raw", 0.0),
                "brier_calibrated": ho_cq.get("brier_calibrated", 0.0),
                "brier_improvement": ho_cq.get("brier_improvement", 0.0),
                "ece": ho_cq.get("ece", 0.0),
                "per_signal": ho_cr.get("per_signal", {}),
                "per_station": ho_cr.get("per_station", {}),
                "failure_counters": ho_cr.get("failure_counters", {}),
            }

    # ── Comparison: same config with exclude_degenerate=True (S4) ──
    comparison_excluding_degenerates = None
    if best_agg and not exclude_degenerate:
        logger.info("=== Comparison run: best config with exclude_degenerate=True ===")
        # Re-run on same fold/eval population
        excl_results = []
        for fold_def in NESTED_FOLDS:
            cr_excl = evaluate_config_on_fold(
                best_config, settlements, metar_batched, registry, station_list,
                calibration_cutoff=fold_def["calibration_cutoff"],
                holdout_start=fold_def["holdout_start"],
                holdout_end=fold_def["holdout_end"],
                period="eval", exclude_degenerate=True,
            )
            if cr_excl and cr_excl.get("trades", 0) > 0:
                excl_results.append(cr_excl)

        if excl_results:
            excl_accs = [c["accuracy"] for c in excl_results]
            comparison_excluding_degenerates = {
                "mean_accuracy": sum(excl_accs) / len(excl_accs),
                "n_folds": len(excl_accs),
                "fold_accuracies": [round(a, 4) for a in excl_accs],
            }

    # ── Baseline accuracies ──
    eval_baselines = compute_baseline_accuracies(
        settlements, station_list, epsilon=0.5,
        holdout_start=None, holdout_end=None, period="eval",
    )
    holdout_baselines = compute_baseline_accuracies(
        settlements, station_list, epsilon=0.5,
        holdout_start=OUTER_HOLDOUT_START, holdout_end=OUTER_HOLDOUT_END,
        period="holdout",
    )

    return {
        "fold_mode": "nested_4fold",
        "fusion_method": fusion_method,
        "calibration_pipeline": calibration_pipeline,
        "ranking_metric": "mean_fold_directional_accuracy",
        "ranking_independence": (
            "Inner folds: each eval date scored exactly once. "
            "Expanding calibration windows mean fold-calibrators share data. "
            "Only the final holdout [2026-05-15, 2026-08-13] is an independent "
            "out-of-sample estimate."
        ),
        "nested_folds": [
            {
                "fold": fd["fold"],
                "calibration_cutoff": fd["calibration_cutoff"],
                "eval_window": f"{fd['eval_start']}..{fd['eval_end']}",
                "holdout_window": f"{fd['holdout_start']}..{fd['holdout_end']}",
            }
            for fd in NESTED_FOLDS
        ],
        "outer_holdout_window": f"{OUTER_HOLDOUT_START}..{OUTER_HOLDOUT_END}",
        "n_configs": n_configs,
        "n_configs_evaluated": len(aggregated),
        # GAP5: honest effective directional test count — Kelly is cosmetic
        # (affects only (PROXY) P&L; zero effect on directional accuracy).
        "effective_config_scope": {
            "n_configs_requested": n_configs,
            "n_configs_evaluated": len(aggregated),
            "n_unique_directional_configs": len(
                {
                    (c["params"]["fusion"], c["params"]["confidence_floor"])
                    for c in aggregated
                }
            ),
            "directional_dimensions": ["fusion", "confidence_floor"],
            "cosmetic_dimensions": ["kelly_fraction", "calibration"],
            "kelly_note": (
                "kelly_fraction varies 3 ways but has zero effect on directional "
                "accuracy — it only scales (PROXY) P&L. Unique directional tests "
                "= fusion x confidence_floor."
            ),
        },
        "best_accuracy": best_agg["mean_accuracy"] if best_agg else 0.0,
        "best_std_accuracy": best_agg["std_accuracy"] if best_agg else 0.0,
        # SMOKE-REPORTING-BLOCKER-FIX: best_trades = None for nested mode
        # (trades are per-fold, not aggregated). Use best_n_folds instead.
        "best_trades": None,
        "best_trades_note": "N/A (nested mode). Per-fold trade counts unavailable in aggregated representation. See best_n_folds.",
        "best_n_folds": best_agg["n_folds"] if best_agg else 0,
        "best_pnl": None,
        "best_pnl_note": "N/A (nested mode). P&L is per-fold only and not aggregated. See holdout_eval.",
        "best_config": best_agg["params"] if best_agg else None,
        "best_fold_accuracies": best_agg["fold_accuracies"] if best_agg else [],
        # GAP1/GAP2/GAP7: best-config aggregated diagnostics
        "best_per_signal": best_agg.get("per_signal", {}) if best_agg else {},
        "best_per_station": best_agg.get("per_station", {}) if best_agg else {},
        "best_calibration_quality": best_agg.get("calibration_quality", {}) if best_agg else {},
        "best_failure_counters": best_agg.get("failure_counters", {}) if best_agg else {},
        # GAP3: failure accounting summed across ALL configs x folds
        "failure_accounting": _aggregate_failure_accounting(aggregated),
        "holdout_eval": holdout_eval,
        "comparison_excluding_degenerates": comparison_excluding_degenerates,
        "baselines": {
            "eval": eval_baselines,
            "holdout": holdout_baselines,
        },
        "configs": aggregated,
    }


def assert_s7_ranking_invariant(config_results):
    """
    S7: Verify that zeroing all P&L fields leaves the ranking unchanged.

    Supports both single-split records (keyed by 'accuracy') and nested
    aggregated records (keyed by 'mean_accuracy'). Detection uses the
    presence of 'n_folds', which is unique to aggregated records.

    This proves config ranking is based solely on directional accuracy,
    not P&L metrics.
    """
    if not config_results:
        return

    # Detect record type: aggregated (nested) records have 'n_folds'
    # (more robust than checking 'mean_accuracy', which could plausibly
    # appear in single-split records in the future).
    is_aggregated = "n_folds" in config_results[0]
    sort_key = "mean_accuracy" if is_aggregated else "accuracy"

    # Collect proxy fields to zero.
    # Aggregated records have pseudo_pnl as the sole cost field;
    # fold-accuracy stats (n_folds, fold_accuracies, std/min/max_accuracy)
    # are accuracy-derived, not cost fields, and do not affect ranking.
    proxy_fields_to_zero = list(PROXY_PNL_KEYS)
    if is_aggregated:
        proxy_fields_to_zero.append("pseudo_pnl")

    # Record ranking before zeroing
    rank_before = [tuple(sorted(c["params"].items())) for c in config_results]

    # Zero all proxy fields on a shallow copy
    zeroed = copy.deepcopy(config_results)
    for c in zeroed:
        for k in proxy_fields_to_zero:
            if k in c:
                c[k] = 0.0

    # Re-sort by the appropriate accuracy key
    zeroed.sort(key=lambda x: x[sort_key], reverse=True)
    rank_after = [tuple(sorted(c["params"].items())) for c in zeroed]

    if rank_before != rank_after:
        logger.error(
            "S7 RANKING VIOLATION: P&L fields influenced config ranking. "
            "Before: %s, After: %s. Fix sort key to accuracy-only.",
            rank_before, rank_after,
        )
        raise AssertionError(
            "P&L fields influenced ranking — fix sort key to accuracy-only"
        )

    logger.debug("S7 ranking invariance: PASS (%d configs)", len(config_results))


def main():
    parser = argparse.ArgumentParser(
        description="Primary directional sweep — accuracy-based config ranking."
    )
    parser.add_argument("--fusion", choices=["llop", "uwc"], default="uwc")
    parser.add_argument(
        "--calibration", choices=["isotonic"], default="isotonic",
        help="Calibration pipeline (isotonic only in primary grid; Platt deferred)."
    )
    parser.add_argument("--n-configs", type=int, default=20)
    parser.add_argument(
        "--folds", type=int, default=1, choices=[1, 4],
        help="Number of folds. 1=single-split (default), 4=nested walk-forward."
    )
    parser.add_argument(
        "--calibration-cutoff",
        default="2026-04-30",
        help="Calibration cutoff date YYYY-MM-DD (default: 2026-04-30). "
             "Refreeze to 2026-04-30 recommended for accuracy improvement."
    )
    parser.add_argument(
        "--db",
        default=os.path.join(REPO_ROOT, "data", "kalshi_settlements.db"),
    )
    parser.add_argument(
        "--metar-db",
        default=os.path.join(REPO_ROOT, "data", "metar_backfill.db"),
    )
    parser.add_argument(
        "--output",
        default=os.path.join(REPO_ROOT, "data", "sweep_results.json"),
    )
    parser.add_argument(
        "--holdout-start",
        default="2026-05-15",
        help="Holdout period start (YYYY-MM-DD). Default: 2026-05-15",
    )
    parser.add_argument(
        "--holdout-end",
        default="2026-08-13",
        help="Holdout period end (YYYY-MM-DD). Default: 2026-08-13",
    )
    parser.add_argument(
        "--exclude-degenerate",
        action="store_true",
        default=False,
        help="Exclude degenerate signals from fusion (default: False — included, per Dan's requirement).",
    )
    parser.add_argument(
        "--no-integrity",
        action="store_true",
        default=False,
        help="Skip the pre-run integrity gate.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # ── S5: Registry coverage assertion ──
    logger.info("Loading settlement data from %s", args.db)
    settlements = load_settlement_data(args.db)
    station_list = [s for s in STATIONS if s in settlements]
    if not station_list:
        raise AssertionError("No stations with settlement data found — aborting sweep")

    logger.info("Loading METAR data from %s for %d stations", args.metar_db, len(station_list))
    metar_batched = load_daily_stats_batched(args.metar_db, station_list)

    registry = SignalRegistry(db_path=args.metar_db)

    # S5: Verify every REGISTERED_SIGNAL_NAMES entry exists in the registry
    missing_signals = []
    for name in REGISTERED_SIGNAL_NAMES:
        sig = registry.get_signal(name)
        if sig is None:
            missing_signals.append(name)
    if missing_signals:
        raise AssertionError(
            f"Signals in REGISTERED_SIGNAL_NAMES not found in SignalRegistry: "
            f"{missing_signals}. Aborting sweep."
        )
    logger.info(
        "Registry coverage: %d/%d signals verified OK",
        len(REGISTERED_SIGNAL_NAMES) - len(missing_signals),
        len(REGISTERED_SIGNAL_NAMES),
    )

    # ── S5: Pre-run integrity gate ──
    if not args.no_integrity:
        if not sweep_config.run_integrity_gate(skip=False):
            raise AssertionError("Integrity gate failed — aborting sweep")
        logger.info("Pre-run integrity gate: PASS")

    # ── Holdout window ──
    holdout_start = args.holdout_start
    holdout_end = args.holdout_end

    # ── Run sweep (single-split or nested) ──
    if args.folds == 1:
        result = run_sweep_single_split(
            settlements, metar_batched, registry,
            args.fusion, args.calibration, args.n_configs,
            holdout_start, holdout_end,
            args.exclude_degenerate,
            args.calibration_cutoff,
        )
    else:
        result = run_sweep_nested(
            settlements, metar_batched, registry,
            args.fusion, args.calibration, args.n_configs,
            args.exclude_degenerate,
        )

    # ── Write JSON output ──
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    # GAP8: data sources / provenance block (roadmap JSON schema §4)
    n_settlement_dates = sum(len(v) for v in settlements.values())
    n_registered = len(REGISTERED_SIGNAL_NAMES)
    n_degenerate = len(DEGENERATE_SIGNAL_NAMES)
    data_sources = {
        "settlements_db": args.db,
        "settlements_table": "kalshi_settlements",
        "metar_db": args.metar_db,
        "metar_table": "metar_observations",
        "n_stations": len(station_list),
        "n_settlement_dates": n_settlement_dates,
        "n_registered_signals": n_registered,
        "n_degenerate_signals": n_degenerate,
        "signal_registry": "core.signals.SignalRegistry",
    }

    json_output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "result": result,
        "data_sources": data_sources,
        "degenerate_signals": DEGENERATE_SIGNAL_NAMES,
        "ranking_metric": result.get("ranking_metric", "accuracy"),
        "holdout_start": holdout_start,
        "holdout_end": holdout_end,
        "exclude_degenerate_actually_used": args.exclude_degenerate,
        "effective_config_scope": result.get("effective_config_scope", {}),
        "failure_accounting": result.get("failure_accounting", {}),
        "pnl_limitations": (
            "(PROXY) Simplified P&L: canonical Kalshi fee (price-dependent $0.01-$0.02/contract), $1/trade notional. "
            "Structurally wrong for binary options — directional accuracy is the "
            "primary sweep metric. See compute_pnl_metrics docstring."
        ),
        "directional_metrics": {
            "primary": "accuracy",
            "secondary": ["calibration_quality.brier_raw",
                          "calibration_quality.brier_calibrated",
                          "calibration_quality.ece"],
            "excluded_from_ranking": [
                "net_pnl", "net_pnl_per_trade", "sharpe",
                "profit_factor", "kelly_adjusted_pnl",
            ],
        },
    }

    with open(args.output, "w") as f:
        json.dump(json_output, f, indent=2, default=str)

    # ── Console output ──
    print(f"\n{'=' * 60}")
    print(f"=== Primary Directional Sweep ===")
    print(f"Mode: {'Nested 4-fold' if args.folds == 4 else 'Single-split'}")
    print(f"Fusion: {args.fusion}  |  Calibration: isotonic")
    print(f"Ranking metric: {result.get('ranking_metric', 'accuracy')}")
    print(f"Exclude degenerate signals: {args.exclude_degenerate}")
    print(f"N configs: {result['n_configs_evaluated']}/{result['n_configs']}")
    if result.get("effective_config_scope"):
        ecs = result["effective_config_scope"]
        print(f"Unique directional tests: {ecs.get('n_unique_directional_configs', '?')} "
              f"(fusion x confidence_floor; kelly_fraction is cosmetic)")
    if result.get("calibration_provenance"):
        cp = result["calibration_provenance"]
        print(f"Calibration configured: {cp['configured_pipeline']}, used: {cp['actually_used']}")
        print(f"Calibration fitted (any): {cp.get('is_calibrated_any', '?')}")
        if cp.get("failed_signal_evaluate_total", 0) > 0:
            print(f"Signal exceptions: {cp['failed_signal_evaluate_total']}")
        if cp.get("failed_fusion_total", 0) > 0:
            print(f"Fusion exceptions: {cp['failed_fusion_total']}")

    # GAP3: Print failure accounting warnings
    if result.get("failure_accounting"):
        fa = result["failure_accounting"]
        total_trades = sum(c.get("trades", c.get("total_trades", 0)) for c in (result.get("configs") or []))
        for k, v in fa.items():
            if k == "_pct_successful":
                continue
            if isinstance(v, (int, float)) and v > 0 and total_trades > 0:
                pct = (v / total_trades) * 100
                if pct > 5:
                    print(f"⚠️  WARNING: {k}={v} ({pct:.1f}% of trades) — exceeds 5% threshold")
                elif pct > 0:
                    print(f"   {k}={v} ({pct:.1f}% of trades)")
        print(f"   Pipeline success rate: {fa.get('_pct_successful', '?'):.2%}")

    if result.get("configs"):
        is_nested = result.get("fold_mode", "") == "nested_4fold"

        # ── Best-config ECE/Brier ──
        if result.get("best_calibration_quality"):
            bcq = result["best_calibration_quality"]
            print(f"\n--- Calibration Quality (best config) ---")
            print(f"Brier raw: {bcq.get('brier_raw', 0):.6f}  |  "
                  f"Brier calibrated: {bcq.get('brier_calibrated', 0):.6f}  |  "
                  f"Improvement: {bcq.get('brier_improvement', 0):+.6f}")
            print(f"ECE: {bcq.get('ece', 0):.6f}")
            ece_bins = bcq.get("ece_bins", [])
            if ece_bins:
                print(f"Reliability bins (10):")
                for bin_row in ece_bins[:10]:
                    lo, hi, n_k, acc_k, conf_k = bin_row[:5]
                    if n_k > 0:
                        print(f"  [{lo:.2f}-{hi:.2f}): n={n_k:4d}  acc={acc_k:.4f}  mean_conf={conf_k:.4f}")

        print(f"\nBest (selection) accuracy: {result['best_accuracy']:.4f}")
        if is_nested:
            print(f"Best (selection) folds: {result['best_n_folds']}  (nested — per-fold trade counts unavailable)")
        else:
            print(f"Best (selection) trades: {result['best_trades']}")
        if "best_std_accuracy" in result and result["best_std_accuracy"] > 0:
            print(f"Best (fold) std: {result['best_std_accuracy']:.4f}")
        if "best_fold_accuracies" in result and result["best_fold_accuracies"]:
            print(f"Best fold accuracies: {result['best_fold_accuracies']}")
        if is_nested and result.get("best_pnl") is None:
            print(f"Best (selection) P&L: N/A (P&L is per-fold, not aggregated across folds)")

        if result.get("holdout_eval"):
            he = result["holdout_eval"]
            print(f"\n--- Holdout Evaluation (PROXY P&L) ---")
            print(f"Holdout period: {holdout_start} to {holdout_end}")
            print(f"Holdout accuracy: {he['accuracy']:.4f}")
            # GAP2: Print holdout calibration metrics
            print(f"Holdout Brier raw: {he.get('brier_raw', 0):.6f}  |  "
                  f"Brier calibrated: {he.get('brier_calibrated', 0):.6f}  |  "
                  f"ECE: {he.get('ece', 0):.6f}")
            print(f"Holdout PnL: ${he.get('net_pnl', 0):+.2f}  (PROXY)")
            print(f"Holdout Sharpe: {he.get('sharpe', 0):.4f}  (PROXY)")
            print(f"Holdout trades: {he['trades']}")
            acc_diff = result['best_accuracy'] - he['accuracy']
            if acc_diff > 0.02:
                print(f"⚠️  OVERFIT: holdout accuracy {he['accuracy']:.4f} is {acc_diff:.4f} below selection {result['best_accuracy']:.4f}")
            else:
                print(f"Holdout gap: {acc_diff:.4f} (within 2pp — OK)")

        # GAP4: Persistence baseline comparison
        if result.get("baselines"):
            bl = result["baselines"]
            if bl.get("holdout", {}).get("persistence_accuracy"):
                persist_acc = bl["holdout"]["persistence_accuracy"]
                h_acc = (result.get("holdout_eval") or {}).get("accuracy", 0.0)
                print(f"\n--- Baselines (holdout) ---")
                print(f"Always-UP: {bl['holdout'].get('always_up_accuracy', 0):.4f}")
                print(f"Always-DOWN: {bl['holdout'].get('always_down_accuracy', 0):.4f}")
                print(f"Persistence: {persist_acc:.4f} ({bl['holdout'].get('persistence_trades', 0)} trades)  "
                      f"— minimum viable weather forecast")
                print(f"Trend (3-day): {bl['holdout'].get('trend_accuracy', 0):.4f} "
                      f"({bl['holdout'].get('trend_trades', 0)} trades)")
                if h_acc > 0 and persist_acc > 0:
                    if h_acc <= persist_acc:
                        print(f"⚠️  System accuracy ({h_acc:.4f}) is BELOW persistence ({persist_acc:.4f}) — no edge over naive forecast")
                    else:
                        print(f"✓ System accuracy ({h_acc:.4f}) is above persistence ({persist_acc:.4f}) — edge exists")

        if result.get("comparison_excluding_degenerates"):
            cd = result["comparison_excluding_degenerates"]
            print(f"\n--- Comparison: Degenerates Excluded (same fold population) ---")
            print(f"Mean accuracy: {cd['mean_accuracy']:.4f} ({cd['n_folds']} folds)")
            print(f"Fold accuracies: {cd['fold_accuracies']}")
            delta = result['best_accuracy'] - cd['mean_accuracy']
            print(f"Delta (incl - excl): {delta:+.4f}")

        # GAP1: Per-signal table
        if result.get("best_per_signal"):
            print(f"\n--- Per-Signal Accuracy (best config, aggregated) ---")
            print(f"{'Signal':<30} {'Acc':>6} {'FireRate':>8} {'Fired':>6} {'BrierR':>8} {'BrierC':>8}")
            print(f"{'-'*30} {'-'*6} {'-'*8} {'-'*6} {'-'*8} {'-'*8}")
            for sig_name, stats in sorted(result["best_per_signal"].items()):
                acc = stats.get("accuracy", 0)
                fr = stats.get("fire_rate", 0)
                fired = stats.get("fired", 0)
                br = stats.get("brier_raw", 0)
                bc = stats.get("brier_calibrated", 0)
                print(f"{sig_name:<30} {acc:>6.4f} {fr:>8.4f} {fired:>6d} {br:>8.6f} {bc:>8.6f}")

        # GAP7: Per-station table
        if result.get("best_per_station"):
            print(f"\n--- Per-Station Accuracy (best config, aggregated) ---")
            print(f"{'Station':<8} {'Acc':>6} {'Trades':>8}")
            print(f"{'-'*8} {'-'*6} {'-'*8}")
            for st, stats in sorted(result["best_per_station"].items()):
                acc = stats.get("accuracy", 0)
                trades = stats.get("trades", 0)
                print(f"{st:<8} {acc:>6.4f} {trades:>8d}")

        print(f"\nTop 5 configs (by selection accuracy):")
        is_nested = result.get("fold_mode", "") == "nested_4fold"
        configs_to_print = result.get("configs", [])[:5]
        for i, c in enumerate(configs_to_print):
            p = c["params"]
            cal_status = "calibrated" if c.get("is_calibrated") else "identity"
            if is_nested:
                count = c.get("n_folds", 0)
                count_label = "folds"
                acc = c.get("mean_accuracy", 0.0)
                pnl_val = c.get("pseudo_pnl")
                if pnl_val is None:
                    pnl_str = "N/A"
                else:
                    pnl_str = f"${pnl_val:.2f}"
                print(
                    f"  {i+1}. fusion={p['fusion']}, cal={p['calibration']}({cal_status}), "
                    f"cf={p['confidence_floor']}, kf={p['kelly_fraction']} "
                    f"\u2192 mean_acc={acc:.4f}  pseudo_pnl={pnl_str}  (PROXY)  "
                    f"({count} {count_label})"
                )
            else:
                trades = c.get("trades", 0)
                acc = c.get("accuracy", 0.0)
                print(
                    f"  {i+1}. fusion={p['fusion']}, cal={p['calibration']}({cal_status}), "
                    f"cf={p['confidence_floor']}, kf={p['kelly_fraction']} "
                    f"\u2192 acc={acc:.4f}  pnl=${c.get('net_pnl', 0):.2f}  (PROXY)  "
                    f"({trades} trades)"
                )
    print(f"\nOutput: {args.output}")
    print(f"Ranking metric: {result.get('ranking_metric', 'accuracy')}")
    print(f"Data sources: {args.db}, {args.metar_db}")
    print(f"{'-' * 60}")
    print(f"GAP5: Effective config count: {result.get('effective_config_scope', {}).get('n_unique_directional_configs', '?')} "
          f"unique directional tests (fusion x confidence_floor). "
          f"Kelly fraction is cosmetic (affects (PROXY) P&L only).")
    print(f"GAP4: Persistence baseline is the minimum viable weather forecast — "
          f"system should beat it to claim edge.")
    print(f"{'=' * 60}")

    return result


if __name__ == "__main__":
    main()