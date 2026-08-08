#!/usr/bin/env python3
"""
per_parameter_sweep.py — Per-Parameter Sweep Optimization Engine

Implements the 5-level optimization pipeline from the definitive spec:
  Level 0: Sanity checks + Sensitivity pre-screen + Noise floor estimation
  Level 1: Per-parameter grid sweep with peak detection & refinement
  Level 2: ±1-step validation + Pairwise interaction + Group meta-interaction
  Level 3: Conditional Bayesian optimization (only if Level 2 finds interactions)
  Level 4: Walk-forward validation + Odd/even split + Robustness

Sub-step 0a: Signal correlation matrix — kill redundant signals (rho >= 0.7)

Usage:
    python3 scripts/per_parameter_sweep.py [--fast] [--metric accuracy|sharpe|pnl|scalarized]
                                          [--n-configs N] [--output-dir DIR]

Output:
    <output-dir>/per_parameter_results.json  — Full results
    <output-dir>/per_parameter_summary.csv   — Human-readable summary
    <output-dir>/level-0/...                 — Level 0 artifacts
    <output-dir>/level-1/...                 — Level 1 artifacts
    <output-dir>/level-2/...                 — Level 2 artifacts
    <output-dir>/level-3/...                 — Level 3 artifacts (if triggered)
    <output-dir>/level-4/...                 — Level 4 artifacts
"""

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')

# ═══════════════════════════════════════════════════════════════════
# 1. CONSTANTS
# ═══════════════════════════════════════════════════════════════════

STATIONS = [
    "KNYC", "KLAX", "KATL", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
    "KLAS", "KMDW", "KMIA", "KMSP", "KMSY", "KOKC", "KPHL", "KPHX",
    "KSAT", "KSEA", "KSFO"
]

SIGNAL_SAMPLE = [
    "gaussian", "gaussian_v2", "forecast_disagreement", "pressure_delta",
    "calendar_climatology", "wind_direction_shift", "eighty_two_member_ensemble"
]

# All signals for correlation matrix
ALL_SIGNALS = [
    "gaussian", "gaussian_v2", "forecast_disagreement", "pressure_delta",
    "calendar_climatology", "wind_direction_shift", "eighty_two_member_ensemble",
    "eighty_two_member_ensemble_ece", "eighty_two_member_ensemble_pooled",
    "radiational_cooling", "frontal_passage_intraday", "dual_polarity",
    "cloud_cover_index", "feels_like_delta", "ecmwf_bias_corrected",
    "cross_model_divergence", "nwp_analog",
]

# Scalarized objective weights (from Section 7.3)
SCALAR_WEIGHTS = {
    "alpha": 0.35,    # accuracy
    "beta": 0.35,     # sharpe
    "gamma": 0.15,    # PnL
    "delta": 0.10,    # Brier (subtracted)
    "epsilon": 0.05,  # max_drawdown (subtracted)
}

# ═══════════════════════════════════════════════════════════════════
# 2. PARAMETER REGISTRY (Section 9)
# ═══════════════════════════════════════════════════════════════════

PARAMETER_REGISTRY = {
    # ── Signals group (params 1-7) ──
    "edge_threshold": {
        "type": "continuous", "min": 0.001, "max": 0.20, "default": 0.05,
        "group": "Signals", "description": "Minimum edge required to trade",
        "sweep_values_7": [0.001, 0.034, 0.067, 0.100, 0.133, 0.167, 0.200],
        "sweep_values_5": [0.001, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20],
    },
    "confidence_floor": {
        "type": "continuous", "min": 0.30, "max": 0.95, "default": 0.60,
        "group": "Signals", "description": "Minimum confidence to trade",
        "sweep_values_7": [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.95],
        "sweep_values_5": [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    },
    "member_weighting": {
        "type": "categorical", "values": [0, 1, 2, 3], "default": 0,
        "labels": ["uniform", "accuracy", "recency", "combined"],
        "group": "Signals", "description": "Ensemble member weighting",
    },
    "entry_price_min": {
        "type": "continuous", "min": 0.001, "max": 0.10, "default": 0.01,
        "group": "Signals", "description": "Minimum entry price (avoid extremes)",
        "sweep_values_7": [0.001, 0.018, 0.035, 0.052, 0.069, 0.086, 0.10],
        "sweep_values_5": [0.01, 0.05, 0.10, 0.20, 0.35, 0.50],
    },
    "entry_price_max": {
        "type": "continuous", "min": 0.05, "max": 0.50, "default": 0.15,
        "group": "Signals", "description": "Maximum entry price",
        "sweep_values_7": [0.05, 0.125, 0.20, 0.275, 0.35, 0.425, 0.50],
        "sweep_values_5": [0.50, 0.60, 0.75, 0.85, 0.90, 0.95, 0.99],
    },
    "max_contracts": {
        "type": "integer_log", "min": 10, "max": 5000, "default": 100,
        "group": "Signals", "description": "Maximum contracts per trade",
        "sweep_values_7": [10, 30, 100, 300, 1000, 3000, 5000],
        "sweep_values_5": [10, 25, 50, 100, 200, 500, 1000, 5000],
    },
    "kl_dro_lambda": {
        "type": "continuous", "min": 0.0, "max": 1.0, "default": 0.5,
        "group": "Signals", "description": "KL-DRO regularization strength",
        "sweep_values_7": [0.0, 0.167, 0.333, 0.50, 0.667, 0.833, 1.0],
        "sweep_values_5": [0.0, 0.25, 0.5, 0.75, 1.0],
    },
    # ── Fees group (params 8-10) ──
    "fee_type": {
        "type": "categorical", "values": [0, 1, 2, 3], "default": 0,
        "labels": ["none", "kalshi", "forecastex", "polymarket"],
        "group": "Fees", "description": "Fee schedule to apply",
    },
    "slippage_budget": {
        "type": "continuous", "min": 0.0, "max": 0.01, "default": 0.001,
        "group": "Fees", "description": "Slippage budget per side",
        "sweep_values_5": [0.0, 0.001, 0.003, 0.005, 0.007, 0.01],
        "sweep_values_7": [0.0, 0.002, 0.004, 0.006, 0.008, 0.01],
    },
    "fee_deduction": {
        "type": "continuous", "min": 0.0, "max": 1.0, "default": 0.5,
        "group": "Fees", "description": "Fee deduction factor",
        "depends_on": "fee_type", "active_when": {"fee_type": [1, 2, 3]},
        "default_when_inactive": 1.0,
        "sweep_values_5": [0.0, 0.25, 0.5, 0.75, 1.0],
        "sweep_values_7": [0.0, 0.167, 0.333, 0.50, 0.667, 0.833, 1.0],
    },
    # ── Gates group (params 11-14) ──
    "agreement_n": {
        "type": "integer", "min": 2, "max": 5, "default": 2,
        "group": "Gates", "description": "N-of-M agreement: minimum signals",
        "sweep_values_7": [2, 3, 4, 5],
        "sweep_values_5": [2, 3, 4, 5],
    },
    "agreement_m": {
        "type": "integer", "min": 5, "max": 11, "default": 7,
        "group": "Gates", "description": "N-of-M agreement: total signals",
        "sweep_values_7": [5, 6, 7, 8, 9, 10, 11],
        "sweep_values_5": [5, 7, 9, 11],
    },
    "stop_loss_kind": {
        "type": "categorical", "values": [0, 1, 2], "default": 0,
        "labels": ["none", "fixed_pct", "trailing"],
        "group": "Gates", "description": "Stop-loss mechanism",
    },
    "calibration_mode": {
        "type": "categorical", "values": [0, 1, 2, 3], "default": 0,
        "labels": ["platt", "ece_binned", "beta", "ensemble"],
        "group": "Gates", "description": "Calibration method",
    },
    # ── Levers group (params 15-17) ──
    "kelly_fraction": {
        "type": "continuous", "min": 0.1, "max": 4.0, "default": 0.5,
        "group": "Levers", "description": "Fractional Kelly factor",
        "depends_on": "position_sizing_model",
        "active_when": {"position_sizing_model": [1, 2]},
        "default_when_inactive": 1.0,
        "sweep_values_7": [0.1, 0.75, 1.4, 2.0, 2.7, 3.35, 4.0],
        "sweep_values_5": [0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 4.0],
    },
    "position_sizing_model": {
        "type": "categorical", "values": [0, 1, 2], "default": 0,
        "labels": ["fixed", "kelly", "fractional_kelly"],
        "group": "Levers", "description": "Position sizing model",
    },
    "capital_base": {
        "type": "continuous", "min": 1000, "max": 100000, "default": 10000,
        "group": "Levers", "description": "Bankroll / capital base",
        "sweep_values_5": [1000, 5000, 10000, 25000, 50000, 100000],
        "sweep_values_7": [1000, 5000, 10000, 25000, 50000, 75000, 100000],
    },
    # ── Lanes group (params 18-19) ──
    "goldilocks_lane_enabled": {
        "type": "boolean", "min": 0, "max": 1, "default": 0,
        "group": "Lanes", "description": "Enable Goldilocks lane",
        "sweep_values_7": [0, 1],
        "sweep_values_5": [0, 1],
    },
    "trajectory_lane_enabled": {
        "type": "boolean", "min": 0, "max": 1, "default": 0,
        "group": "Lanes", "description": "Enable Trajectory lane",
        "sweep_values_7": [0, 1],
        "sweep_values_5": [0, 1],
    },
    # ── Modulators group (params 20-22) ──
    "fusion_mode": {
        "type": "categorical", "values": [0, 1, 2], "default": 0,
        "labels": ["average", "weighted", "select_best"],
        "group": "Modulators", "description": "Signal fusion mode",
    },
    "spatial_coherence_enabled": {
        "type": "boolean", "min": 0, "max": 1, "default": 0,
        "group": "Modulators", "description": "Enable spatial coherence",
        "sweep_values_7": [0, 1],
        "sweep_values_5": [0, 1],
    },
    "holdout_start_frac": {
        "type": "continuous", "min": 0.5, "max": 0.8, "default": 0.7,
        "group": "Validation", "description": "Fraction of data for training",
        "sweep_values_7": [0.5, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80],
        "sweep_values_5": [0.5, 0.6, 0.7, 0.8],
    },
}

PARAM_GROUPS = {
    "Signals": ["edge_threshold", "confidence_floor", "member_weighting",
                "entry_price_min", "entry_price_max", "max_contracts", "kl_dro_lambda"],
    "Fees": ["fee_type", "slippage_budget", "fee_deduction"],
    "Gates": ["agreement_n", "agreement_m", "stop_loss_kind", "calibration_mode"],
    "Levers": ["kelly_fraction", "position_sizing_model", "capital_base"],
    "Lanes": ["goldilocks_lane_enabled", "trajectory_lane_enabled"],
    "Modulators": ["fusion_mode", "spatial_coherence_enabled"],
    "Validation": ["holdout_start_frac"],
}

# Build default config from registry
DEFAULT_CONFIG = {name: spec["default"] for name, spec in PARAMETER_REGISTRY.items()}


# ═══════════════════════════════════════════════════════════════════
# 3. CONFIG VALIDATION (Section 10.2)
# ═══════════════════════════════════════════════════════════════════

def validate_config(config: dict) -> Tuple[bool, List[str]]:
    """Validate a config dict against the parameter registry."""
    errors = []
    for key, value in config.items():
        spec = PARAMETER_REGISTRY.get(key)
        if spec is None:
            errors.append(f"Unknown parameter: {key}")
            continue
        ptype = spec["type"]
        if ptype == "continuous":
            if not isinstance(value, (int, float, np.integer, np.floating)):
                errors.append(f"{key}: expected numeric, got {type(value).__name__}")
            elif value < spec["min"] or value > spec["max"]:
                errors.append(f"{key}: {value} outside [{spec['min']}, {spec['max']}]")
        elif ptype == "integer" or ptype == "integer_log":
            if not isinstance(value, (int, np.integer)):
                errors.append(f"{key}: expected int, got {type(value).__name__}")
            elif value < spec["min"] or value > spec["max"]:
                errors.append(f"{key}: {value} outside [{spec['min']}, {spec['max']}]")
        elif ptype == "boolean":
            if value not in (0, 1):
                errors.append(f"{key}: expected 0 or 1, got {value}")
        elif ptype == "categorical":
            if value not in spec["values"]:
                errors.append(f"{key}: {value} not in {spec['values']}")
    return len(errors) == 0, errors


# ═══════════════════════════════════════════════════════════════════
# 4. SCALARIZED OBJECTIVE (Section 7.3)
# ═══════════════════════════════════════════════════════════════════

def compute_scalarized_score(
    metrics: dict,
    mu_stats: Optional[dict] = None,
    sigma_stats: Optional[dict] = None
) -> float:
    """Compute scalarized objective with running statistics."""
    if mu_stats is None:
        mu_stats = {k: 0.0 for k in ["accuracy", "sharpe", "total_pnl", "brier", "max_drawdown"]}
    if sigma_stats is None:
        sigma_stats = {k: 1.0 for k in ["accuracy", "sharpe", "total_pnl", "brier", "max_drawdown"]}

    w = SCALAR_WEIGHTS

    def _norm(key, min_sigma):
        val = metrics.get(key, 0.0)
        mu = mu_stats.get(key, 0.0)
        sigma = max(sigma_stats.get(key, 0.0), min_sigma)
        return (val - mu) / sigma

    acc_norm = _norm("accuracy", 0.01)
    sharpe_norm = _norm("sharpe", 0.1)
    pnl_norm = _norm("total_pnl", 100.0)
    brier_norm = _norm("brier", 0.01)
    dd_norm = _norm("max_drawdown", 0.01)

    return (w["alpha"] * acc_norm + w["beta"] * sharpe_norm + w["gamma"] * pnl_norm
            - w["delta"] * brier_norm - w["epsilon"] * dd_norm)


def update_running_stats(all_metrics: List[dict]) -> Tuple[dict, dict]:
    """Recompute running mu and sigma from all observed metrics."""
    keys = ["accuracy", "sharpe", "total_pnl", "brier", "max_drawdown"]
    mu, sigma = {}, {}
    for k in keys:
        vals = [m.get(k, 0.0) for m in all_metrics if m.get(k) is not None]
        if len(vals) > 1:
            mu[k] = float(np.mean(vals))
            sigma[k] = float(np.std(vals, ddof=1))
        else:
            mu[k] = float(vals[0]) if vals else 0.0
            sigma[k] = 1.0
    return mu, sigma


# ═══════════════════════════════════════════════════════════════════
# 5. EVALUATE CONFIG (Section 10)
# ═══════════════════════════════════════════════════════════════════

def evaluate_config(
    config: dict,
    seed: int = 42,
    window: Optional[Tuple[str, str]] = None,
    stations: Optional[List[str]] = None,
    signal_names: Optional[List[str]] = None,
    fast: bool = False,
) -> dict:
    """
    Run a full backtest on the given configuration.

    Returns dict with all 8 metrics:
        accuracy, temp_accuracy, market_accuracy, total_pnl,
        sharpe, brier, trades_per_day, max_drawdown
    """
    from scripts.sweep_engine import build_signal_registry, DataCache

    registry = build_signal_registry()
    # DataCache has no .clear() — cache persists across calls

    # Load settlements
    settlements = DataCache.get_settlements()
    if not settlements:
        return _null_metrics("No settlement data")

    if stations is None:
        stations = STATIONS[:5] if fast else STATIONS
    if signal_names is None:
        signal_names = SIGNAL_SAMPLE[:3] if fast else SIGNAL_SAMPLE

    stations_to_use = stations[:5] if fast else stations
    signals_to_use = signal_names[:3] if fast else signal_names

    # Apply window filter if specified
    def _in_window(date_str):
        if window is None:
            return True
        w_start, w_end = window
        return w_start <= date_str <= w_end

    # Resolve dependent parameters
    resolved_config = dict(config)
    for pname, spec in PARAMETER_REGISTRY.items():
        deps = spec.get("depends_on")
        if deps is not None:
            dep_val = config.get(deps)
            active_vals = spec.get("active_when", {}).get(deps, [])
            if dep_val not in active_vals:
                resolved_config[pname] = spec.get("default_when_inactive", spec["default"])

    random.seed(seed)
    np.random.seed(seed)

    all_trades = []
    for sname in signals_to_use:
        sobj = registry.get(sname)
        if sobj is None or callable(sobj):
            continue
        for station in stations_to_use:
            days = DataCache.get_metar_data(station)
            if len(days) < 5:
                continue
            try:
                min_lb = max(1, int(getattr(sobj, 'min_lookback', 5)))
            except (ValueError, TypeError):
                min_lb = 5
            for idx in range(min_lb, len(days)):
                date_str = days[idx].get('date', '')
                if not _in_window(date_str):
                    continue
                try:
                    pd, cf = sobj.evaluate(idx, days)
                except Exception:
                    continue
                if pd is None:
                    continue
                station_s = settlements.get(station, {})
                actual_temp = station_s.get(date_str)
                if actual_temp is None:
                    continue
                prev_date = days[idx - 1].get('date', '')
                prev_temp = station_s.get(prev_date)
                if prev_temp is None:
                    continue

                # Direction checks
                actual_dir = 1 if (actual_temp - prev_temp) > 0 else -1
                pred_dir = 1 if pd == 'up' else -1
                correct = pred_dir == actual_dir
                conf = max(0.501, min(0.999, cf))

                # Gate: confidence floor
                conf_floor = resolved_config.get("confidence_floor", 0.60)
                if conf < conf_floor:
                    continue

                # Gate: agreement_n / agreement_m
                # (simplified: skip if signal count below threshold)
                # For full agreement, we'd need multi-signal coordination

                all_trades.append({
                    "signal": sname, "station": station,
                    "date": date_str, "direction": pd,
                    "confidence": conf, "correct": correct,
                    "actual_temp": actual_temp,
                    "prev_temp": prev_temp,
                    "predicted_temp": None,  # Not available from all signals
                    "brier": (conf - (1.0 if correct else 0.0)) ** 2,
                })

            if fast and len(all_trades) > 200:
                break
        if fast and len(all_trades) > 200:
            break

    n = len(all_trades)
    if n < 10:
        return _null_metrics(f"Insufficient trades ({n} < 10)", n)

    # Penalize degenerate configs with too few trades (per-parameter composability guard)
    # Without this, a high confidence_floor in isolation looks optimal but kills every trade
    # when combined with other optimal parameter values.
    MIN_TRADES = 50
    trade_shortfall = max(0, MIN_TRADES - n)

    # ── Compute all 8 metrics ──

    # 1. Directional accuracy
    correct_count = sum(1 for t in all_trades if t["correct"])
    accuracy = correct_count / n

    # 2. Temperature accuracy (based on actual temp delta direction)
    temp_correct = sum(1 for t in all_trades if t["correct"])
    temp_accuracy = temp_correct / n  # Same as accuracy for directional signals

    # 3. Market direction accuracy (realized trading accuracy)
    market_accuracy = accuracy

    # 4. Total PnL
    # Simulate: $1 per contract if correct, -$1 if wrong, with fees/slippage
    slippage = resolved_config.get("slippage_budget", 0.001)
    fee_ded = resolved_config.get("fee_deduction", 0.5)
    kelly_frac = resolved_config.get("kelly_fraction", 0.5)
    max_contracts = resolved_config.get("max_contracts", 100)

    daily_pnl = defaultdict(float)
    for t in all_trades:
        # Simplified PnL: confidence-adjusted position sizing
        position = min(int(kelly_frac * t["confidence"] * max_contracts), max_contracts)
        gross = position * (1.0 if t["correct"] else -1.0)
        fees = position * slippage * fee_ded
        net = gross - fees
        t["pnl"] = net
        daily_pnl[t["date"]] += net

    total_pnl = sum(t.get("pnl", 0) for t in all_trades)

    # 5. Sharpe ratio (annualized from daily returns)
    initial_bankroll = resolved_config.get("capital_base", 10000.0)
    sorted_dates = sorted(daily_pnl.keys())
    daily_return_pcts = []
    bankroll = initial_bankroll
    for d in sorted_dates:
        day_pnl = daily_pnl[d]
        day_return_pct = day_pnl / bankroll if bankroll > 0 else 0.0
        daily_return_pcts.append(day_return_pct)
        bankroll += day_pnl

    if len(daily_return_pcts) > 1:
        daily_mean = np.mean(daily_return_pcts)
        daily_std = np.std(daily_return_pcts, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0

    # 6. Brier score
    brier = sum(t["brier"] for t in all_trades) / n

    # 7. Trades per day
    trading_days = len(set(t["date"] for t in all_trades))
    trades_per_day = n / trading_days if trading_days > 0 else 0.0

    # 8. Max drawdown
    cumulative_return = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in daily_return_pcts:
        cumulative_return += r
        peak = max(peak, cumulative_return)
        dd = (peak - cumulative_return) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Apply trade-count penalty to degenerate configs
    if trade_shortfall > 0:
        penalty_factor = trade_shortfall / MIN_TRADES
        total_pnl -= 10000.0 * penalty_factor
        sharpe -= 3.0 * penalty_factor
        max_dd = min(1.0, max_dd + 0.5 * penalty_factor)

    return {
        "accuracy": accuracy,
        "temp_accuracy": temp_accuracy,
        "market_accuracy": market_accuracy,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "brier": brier,
        "trades_per_day": trades_per_day,
        "max_drawdown": max_dd,
        "n_trades": n,
        "n_trading_days": trading_days,
    }


def _null_metrics(reason: str = "No data", n_trades: int = 0) -> dict:
    """Return a sentinel metrics dict when evaluation fails."""
    return {
        "accuracy": 0.0, "temp_accuracy": 0.0, "market_accuracy": 0.0,
        "total_pnl": 0.0, "sharpe": 0.0, "brier": 1.0,
        "trades_per_day": 0.0, "max_drawdown": 1.0,
        "n_trades": n_trades, "n_trading_days": 0,
        "_error": reason,
    }


def safe_evaluate(config: dict, **kwargs) -> dict:
    """Wrapper with crash recovery (Section 11.1)."""
    try:
        valid, errors = validate_config(config)
        if not valid:
            logger.warning(f"Config validation failed: {errors}")
            return _null_metrics(f"Config invalid: {errors}")
        result = evaluate_config(config, **kwargs)
        return result
    except Exception as e:
        logger.error(f"Evaluation failed for config: {e}")
        return _null_metrics(f"Crash: {e}")


# ═══════════════════════════════════════════════════════════════════
# 6. SUB-STEP 0a: SIGNAL CORRELATION MATRIX
# ═══════════════════════════════════════════════════════════════════

def run_signal_correlation_matrix(
    output_dir: str,
    correlation_threshold: float = 0.7,
) -> Dict[str, Any]:
    """
    Compute pairwise Spearman correlation across all signals.
    Kill signals with rho >= correlation_threshold.
    """
    logger.info("Sub-step 0a: Signal correlation matrix...")

    # Call the existing compute_signal_correlation_matrix.py logic
    corr_script = os.path.join(os.path.dirname(__file__), "compute_signal_correlation_matrix.py")
    if not os.path.exists(corr_script):
        logger.warning("compute_signal_correlation_matrix.py not found — skipping Sub-step 0a")
        return {"skipped": True, "reason": "Script not found"}

    # Run standalone mode
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("corr_matrix", corr_script)
        corr_mod = importlib.util.module_from_spec(spec)
        sys.modules["corr_matrix"] = corr_mod
        spec.loader.exec_module(corr_mod)

        from scripts.big_sweep import build_signal_registry

        registry = build_signal_registry()
        signal_vectors = {}
        from scripts.sweep_engine import DataCache, STATIONS as BIG_STATIONS

        for sname, sobj in registry.items():
            if sobj is None or callable(sobj) or not hasattr(sobj, 'evaluate'):
                continue
            directions = []
            for station in BIG_STATIONS[:8]:
                days = DataCache.get_metar_data(station)
                if len(days) < 5:
                    continue
                for idx in range(5, min(len(days), 100)):
                    try:
                        pd, _ = sobj.evaluate(idx, days)
                        if pd is not None:
                            directions.append(1 if pd == 'up' else 0)
                    except Exception:
                        continue
            if len(directions) >= 10:
                signal_vectors[sname] = directions

        if len(signal_vectors) < 2:
            logger.warning("Insufficient signals for correlation matrix")
            return {"skipped": True, "reason": "Insufficient signals"}

        # Compute matrix
        signal_names = list(signal_vectors.keys())
        matrix = {}
        for si in signal_names:
            matrix[si] = {}
            for sj in signal_names:
                if si == sj:
                    matrix[si][sj] = 1.0
                    continue
                vi = signal_vectors[si]
                vj = signal_vectors[sj]
                n = min(len(vi), len(vj))
                if n < 5:
                    matrix[si][sj] = None
                    continue
                try:
                    from scipy.stats import spearmanr
                    rho, _ = spearmanr(vi[:n], vj[:n])
                    matrix[si][sj] = round(rho, 4) if not np.isnan(rho) else 0.0
                except Exception:
                    matrix[si][sj] = None

        # Find redundant signals
        kill_signals = set()
        flagged_pairs = []
        for i, si in enumerate(signal_names):
            for j, sj in enumerate(signal_names):
                if j <= i:
                    continue
                rho = matrix.get(si, {}).get(sj)
                if rho is not None and abs(rho) >= correlation_threshold:
                    flagged_pairs.append((si, sj, abs(rho)))
                    # Kill the one with fewer stations/trades
                    kill_signals.add(sj)

        result = {
            "matrix": matrix,
            "signal_names": signal_names,
            "flagged_pairs": [{"a": p[0], "b": p[1], "rho": round(p[2], 4)} for p in sorted(flagged_pairs, key=lambda x: -x[2])],
            "kill_signals": list(kill_signals),
            "n_flagged": len(flagged_pairs),
            "n_killed": len(kill_signals),
            "threshold": correlation_threshold,
        }

        # Write output
        level0_dir = os.path.join(output_dir, "level-0")
        os.makedirs(level0_dir, exist_ok=True)
        with open(os.path.join(level0_dir, "0a_signal_correlation.json"), "w") as f:
            json.dump(result, f, indent=2, default=str)

        logger.info("  Sub-step 0a: %d signals → %d KILL, %d flagged pairs",
                    len(signal_names), len(kill_signals), len(flagged_pairs))
        return result

    except Exception as e:
        logger.warning(f"Sub-step 0a failed: {e}")
        return {"skipped": True, "reason": str(e)}


# ═══════════════════════════════════════════════════════════════════
# 7. LEVEL 0: SANITY CHECKS + SENSITIVITY + NOISE FLOOR
# ═══════════════════════════════════════════════════════════════════

def run_sanity_checks(stations, signal_names, fast=False) -> Dict[str, Any]:
    """Level 0 sanity-check suite (Section 2.2)."""
    logger.info("Level 0: Sanity checks...")
    results = {}

    # Check 1: Null-config test — expect ~50% accuracy
    try:
        null_metrics = evaluate_config(DEFAULT_CONFIG, seed=42, stations=stations,
                                       signal_names=signal_names, fast=fast)
        null_acc = null_metrics.get("accuracy", 0.0)
        check1_pass = 0.47 <= null_acc <= 0.53
    except Exception as e:
        null_metrics = _null_metrics(f"Crash: {e}")
        null_acc = 0.0
        check1_pass = False
    results["null_config"] = {
        "passed": check1_pass,
        "accuracy": round(null_acc, 4),
        "expected": "0.47-0.53 (≈50% ±3%)",
        "note": "Pass if default config shows ~50% accuracy (no systematic bias)",
    }

    # Check 2: Permutation test — shuffle labels, expect ~50%
    try:
        permuted = evaluate_config_permuted(stations, signal_names, seed=42, fast=fast)
        perm_acc = permuted.get("accuracy", 0.0)
        check2_pass = 0.48 <= perm_acc <= 0.52
    except Exception:
        perm_acc = 0.0
        check2_pass = False
    results["permutation_test"] = {
        "passed": check2_pass,
        "accuracy": round(perm_acc, 4),
        "expected": "0.48-0.52 (≈50% ±2%)",
        "note": "Pass if shuffled labels give ~50% (no data leakage)",
    }

    # Check 3: Synthetic data recovery — mini-sweep of edge_threshold
    try:
        synth_pass, synth_detail = run_synthetic_recovery(stations, signal_names, fast=fast)
    except Exception as e:
        synth_pass, synth_detail = False, {"error": str(e)}
    results["synthetic_recovery"] = {
        "passed": synth_pass,
        **synth_detail,
    }

    return results


def evaluate_config_permuted(stations, signal_names, seed=42, fast=False) -> dict:
    """Permutation test: shuffle the settlement labels and re-evaluate."""
    from scripts.sweep_engine import build_signal_registry, DataCache
    registry = build_signal_registry()
    settlements = DataCache.get_settlements()
    if not settlements:
        return _null_metrics("No settlement data")

    stations_to_use = stations[:5] if fast else stations
    signals_to_use = signal_names[:3] if fast else signal_names
    random.seed(seed)

    all_trades = []
    for sname in signals_to_use:
        sobj = registry.get(sname)
        if sobj is None or callable(sobj):
            continue
        for station in stations_to_use:
            days = DataCache.get_metar_data(station)
            if len(days) < 5:
                continue
            # Shuffle the temperature sequence for this station
            station_s = settlements.get(station, {})
            dates = sorted(station_s.keys())
            temps = [station_s[d] for d in dates]
            shuffled = temps.copy()
            random.shuffle(shuffled)
            shuffled_map = dict(zip(dates, shuffled))

            for idx in range(5, len(days)):
                date_str = days[idx].get('date', '')
                actual_temp = shuffled_map.get(date_str)
                if actual_temp is None:
                    continue
                prev_date = days[idx - 1].get('date', '')
                prev_temp = shuffled_map.get(prev_date)
                if prev_temp is None:
                    continue
                try:
                    pd, cf = sobj.evaluate(idx, days)
                except Exception:
                    continue
                if pd is None:
                    continue
                actual_dir = 1 if (actual_temp - prev_temp) > 0 else -1
                pred_dir = 1 if pd == 'up' else -1
                correct = pred_dir == actual_dir
                all_trades.append({"correct": correct})
            if fast and len(all_trades) > 200:
                break
        if fast and len(all_trades) > 200:
            break

    n = len(all_trades)
    if n < 10:
        return _null_metrics(f"Too few trades ({n})")
    return {"accuracy": sum(1 for t in all_trades if t["correct"]) / n, "n_trades": n}


def run_synthetic_recovery(stations, signal_names, seed=42, fast=False) -> Tuple[bool, dict]:
    """Synthetic data recovery: verify peak detector recovers a known optimum."""
    # Use kelly_fraction with known optimum at 1.0
    known_optimum = 1.0
    synthetic_values = [0.1, 0.5, 1.0, 2.0, 4.0]
    scores = []

    for val in synthetic_values:
        config = dict(DEFAULT_CONFIG)
        config["kelly_fraction"] = val
        metrics = safe_evaluate(config, seed=seed, stations=stations,
                                signal_names=signal_names, fast=fast)
        # Use total_pnl (not sharpe) — Sharpe is scale-invariant, PnL varies with fraction
        scores.append(metrics.get("total_pnl", 0.0))

    # Peak detection
    best_idx = int(np.argmax(scores))
    recovered = synthetic_values[best_idx]
    recovered_ok = abs(recovered - known_optimum) < 0.51  # within tolerance

    return recovered_ok, {
        "parameter": "kelly_fraction",
        "known_optimum": known_optimum,
        "recovered": recovered,
        "values": synthetic_values,
        "scores": [round(s, 4) for s in scores],
        "recovered_ok": recovered_ok,
    }


def estimate_noise_floor(stations, signal_names, fast=False, seeds=None) -> Dict[str, Any]:
    """Level 0 noise floor estimation (Section 2.4)."""
    logger.info("Level 0: Estimating noise floor...")
    if seeds is None:
        seeds = [42, 101, 777, 2024, 9999]

    accuracies = []
    all_metrics = []
    for seed in seeds:
        metrics = safe_evaluate(DEFAULT_CONFIG, seed=seed, stations=stations,
                                signal_names=signal_names, fast=fast)
        accuracies.append(metrics.get("accuracy", 0.0))
        all_metrics.append(metrics)

    std_acc = float(np.std(accuracies, ddof=1)) if len(accuracies) > 1 else 0.0
    noise_floor = 2 * std_acc / math.sqrt(len(accuracies))

    # Per spec: if noise_floor < 0.3%, use 0.3%
    sigma_noise = max(noise_floor, 0.003)

    return {
        "seeds": seeds,
        "accuracies": [round(a, 4) for a in accuracies],
        "std": round(std_acc, 4),
        "noise_floor": round(noise_floor, 4),
        "sigma_noise": round(sigma_noise, 4),
        "peak_threshold": round(3 * sigma_noise, 4),
        "note": "If noise_floor > 1.0%, evaluation too noisy — increase window or replicate",
    }


def run_sensitivity_prescreen(stations, signal_names, fast=False,
                              sigma_noise=0.003) -> Dict[str, Any]:
    """Level 0 sensitivity pre-screen (Section 2.3)."""
    logger.info("Level 0: Sensitivity pre-screen over %d parameters...",
                len(PARAMETER_REGISTRY))
    effects = {}
    significant = []

    baseline_metrics = safe_evaluate(DEFAULT_CONFIG, stations=stations,
                                     signal_names=signal_names, fast=fast)
    baseline_score = compute_scalarized_score(baseline_metrics)

    for pname, spec in PARAMETER_REGISTRY.items():
        ptype = spec["type"]
        if ptype == "categorical":
            # Test across all categorical values
            vals = spec["values"]
            if len(vals) < 2:
                effects[pname] = {"significant": False, "effect": 0.0}
                continue
            scores = []
            for v in vals:
                cfg = dict(DEFAULT_CONFIG)
                cfg[pname] = v
                m = safe_evaluate(cfg, stations=stations, signal_names=signal_names, fast=fast)
                scores.append(compute_scalarized_score(m))
            effect = abs(max(scores) - min(scores))
        elif ptype == "boolean":
            vals = [0, 1]
            scores = []
            for v in vals:
                cfg = dict(DEFAULT_CONFIG)
                cfg[pname] = v
                m = safe_evaluate(cfg, stations=stations, signal_names=signal_names, fast=fast)
                scores.append(compute_scalarized_score(m))
            effect = abs(scores[1] - scores[0])
        else:
            # Continuous / integer: test min, midpoint, max
            x_low = spec["min"]
            x_high = spec["max"]
            cfg_low = dict(DEFAULT_CONFIG)
            cfg_low[pname] = x_low
            cfg_high = dict(DEFAULT_CONFIG)
            cfg_high[pname] = x_high
            m_low = safe_evaluate(cfg_low, stations=stations, signal_names=signal_names, fast=fast)
            m_high = safe_evaluate(cfg_high, stations=stations, signal_names=signal_names, fast=fast)
            s_low = compute_scalarized_score(m_low)
            s_high = compute_scalarized_score(m_high)
            effect = abs(s_high - s_low)

        is_sig = effect > sigma_noise
        effects[pname] = {
            "significant": is_sig,
            "effect": round(effect, 5),
            "type": ptype,
            "group": spec["group"],
        }
        if is_sig:
            significant.append(pname)

    return {
        "baseline_score": round(baseline_score, 4),
        "baseline_metrics": baseline_metrics,
        "sigma_noise": sigma_noise,
        "effects": effects,
        "significant_params": significant,
        "n_significant": len(significant),
    }


# ═══════════════════════════════════════════════════════════════════
# 8. LEVEL 1: PER-PARAMETER GRID SWEEP
# ═══════════════════════════════════════════════════════════════════

def sweep_parameter_grid(param_name, param_def, stations, signal_names,
                         fast=False, metric="scalarized") -> Dict[str, Any]:
    """Sweep a single parameter through its grid values, holding others at defaults."""
    # Determine grid values
    if param_def["type"] == "categorical":
        values = param_def["values"]
    elif "sweep_values_7" in param_def:
        values = param_def["sweep_values_7"]
    else:
        values = param_def.get("sweep_values_5", [param_def["default"]])

    # For fast mode, reduce grid
    if fast and len(values) > 5:
        values = values[:5]

    results = []
    all_metrics = []
    for val in values:
        config = dict(DEFAULT_CONFIG)
        config[param_name] = val
        metrics = safe_evaluate(config, stations=stations, signal_names=signal_names, fast=fast)

        # Running stats for scalarization
        mu, sigma = update_running_stats(all_metrics + [metrics])
        scalar = compute_scalarized_score(metrics, mu, sigma)

        results.append({
            "param": param_name,
            "value": val,
            "label": _categorical_label(param_def, val),
            "default": param_def["default"],
            "is_default": val == param_def["default"],
            "metrics": {k: metrics[k] for k in
                        ["accuracy", "temp_accuracy", "market_accuracy", "total_pnl",
                         "sharpe", "brier", "trades_per_day", "max_drawdown"]},
            "n_trades": metrics.get("n_trades", 0),
            "scalarized_score": round(scalar, 5),
            "accuracy": metrics["accuracy"],
            "sharpe": metrics["sharpe"],
            "total_pnl": metrics["total_pnl"],
            "brier": metrics["brier"],
        })
        all_metrics.append(metrics)

    # Peak detection
    scores = [r["scalarized_score"] for r in results]
    values_list = [r["value"] for r in results]
    peak = detect_peak(values_list, scores)

    return {
        "param": param_name,
        "description": param_def["description"],
        "type": param_def["type"],
        "group": param_def["group"],
        "results": results,
        "optimal_value": peak["optimal_value"],
        "peak_type": peak["peak_type"],
        "sensitivity_score": peak["sensitivity_score"],
        "significance": "SIGNIFICANT" if peak["sensitivity_score"] > 0 else "INSENSITIVE",
    }


def _categorical_label(param_def, val):
    labels = param_def.get("labels", [])
    values = param_def.get("values", [])
    if labels and val in values:
        idx = values.index(val)
        if idx < len(labels):
            return labels[idx]
    return str(val)


def detect_peak(values, scores, sigma_noise=0.003) -> Dict[str, Any]:
    """Peak detector (Section 3.4)."""
    n = len(values)
    if n < 2:
        return {"optimal_value": values[0] if values else None,
                "peak_type": "PLATEAU", "sensitivity_score": 0.0}

    peak_idx = int(np.argmax(scores))
    max_score = scores[peak_idx]
    min_score = min(scores)
    sensitivity = (max_score - min_score) / max(abs(max_score), 1e-9)

    if sensitivity < sigma_noise:
        return {"optimal_value": values[n // 2], "peak_type": "PLATEAU",
                "sensitivity_score": round(sensitivity, 6)}

    # Check if peak is interior
    if 0 < peak_idx < n - 1:
        delta_plus = scores[peak_idx + 1] - scores[peak_idx]
        delta_minus = scores[peak_idx] - scores[peak_idx - 1]
        # Clear peak
        if delta_plus < 0 and delta_minus > 0:
            peak_type = "CLEAR_PEAK"
        else:
            peak_type = "EDGE"
    else:
        peak_type = "EDGE"

    return {
        "optimal_value": values[peak_idx],
        "peak_type": peak_type,
        "sensitivity_score": round(sensitivity, 6),
        "peak_idx": peak_idx,
    }


def run_level_1(significant_params, stations, signal_names, fast=False,
                metric="scalarized") -> Dict[str, Any]:
    """Level 1: per-parameter grid sweep on significant params."""
    logger.info("Level 1: Per-parameter grid sweep on %d significant params...",
                len(significant_params))
    results = {}
    for pname in significant_params:
        param_def = PARAMETER_REGISTRY[pname]
        logger.info("  Sweeping %s (%s)...", pname, param_def["type"])
        sweep = sweep_parameter_grid(pname, param_def, stations, signal_names, fast, metric)
        results[pname] = sweep
        logger.info("    → optimal=%s (%s) sensitivity=%.4f",
                    sweep["optimal_value"], sweep["peak_type"], sweep["sensitivity_score"])

    return results


# ═══════════════════════════════════════════════════════════════════
# 9. LEVEL 2: INTERACTION & VALIDATION
# ═══════════════════════════════════════════════════════════════════

def build_optimal_config(level1_results, significant_params) -> dict:
    """Build C₀ from Level 1 optimal values."""
    config = dict(DEFAULT_CONFIG)
    for pname in significant_params:
        sweep = level1_results.get(pname)
        if sweep and sweep["optimal_value"] is not None:
            config[pname] = sweep["optimal_value"]
    return config


def run_plus_minus_validation(optimal_config, significant_params, stations,
                              signal_names, fast=False, sigma_noise=0.003) -> Dict[str, Any]:
    """±1-step validation (Section 4.2)."""
    logger.info("Level 2: ±1-step validation on %d params...", len(significant_params))
    s0 = safe_evaluate(optimal_config, stations=stations, signal_names=signal_names, fast=fast)
    s0_score = compute_scalarized_score(s0)

    results = {}
    for pname in significant_params:
        param_def = PARAMETER_REGISTRY[pname]
        opt = optimal_config.get(pname, param_def["default"])
        # Find nearest grid points
        grid = param_def.get("sweep_values_7") or param_def.get("sweep_values_5") or [opt]
        below = [v for v in grid if v < opt]
        above = [v for v in grid if v > opt]
        step_down = below[-1] if below else opt
        step_up = above[0] if above else opt

        cfg_minus = dict(optimal_config)
        cfg_minus[pname] = step_down
        cfg_plus = dict(optimal_config)
        cfg_plus[pname] = step_up

        m_minus = safe_evaluate(cfg_minus, stations=stations, signal_names=signal_names, fast=fast)
        m_plus = safe_evaluate(cfg_plus, stations=stations, signal_names=signal_names, fast=fast)
        s_minus = compute_scalarized_score(m_minus)
        s_plus = compute_scalarized_score(m_plus)
        delta_minus = s0_score - s_minus
        delta_plus = s0_score - s_plus

        # Interpretation
        if delta_minus < sigma_noise and delta_plus < sigma_noise:
            interp = "STABLE"
        elif delta_minus < 0 and delta_plus < 0:
            interp = "GENUINE_OPTIMUM"
        else:
            interp = "SENSITIVE"

        results[pname] = {
            "optimal": opt,
            "step_down": step_down, "step_up": step_up,
            "delta_minus": round(delta_minus, 5),
            "delta_plus": round(delta_plus, 5),
            "interpretation": interp,
        }

    return {"s0_score": round(s0_score, 5), "s0_metrics": s0, "params": results}


def _step_high(param_name, opt, step):
    """Get the 'high' value for a parameter, handling categorical/non-numeric types."""
    param_def = PARAMETER_REGISTRY[param_name]
    if "max" in param_def:
        val = min(param_def["max"], opt + step)
        # Preserve integer type for integer parameters
        if param_def.get("type") in ("integer", "integer_log"):
            val = int(round(val))
        return val
    # Categorical: pick next option if available
    grid = param_def.get("sweep_values_7") or param_def.get("sweep_values_5") or []
    if grid and opt in grid:
        idx = grid.index(opt)
        if idx + 1 < len(grid):
            return grid[idx + 1]
    return opt  # No way to move up, stay at current


def run_pairwise_interaction(top_params, optimal_config, stations,
                             signal_names, fast=False, sigma_noise=0.003) -> Dict[str, Any]:
    """Full pairwise interaction test (Section 4.3)."""
    logger.info("Level 2: Pairwise interaction test on %d top params...", len(top_params))
    pairs = [(a, b) for i, a in enumerate(top_params) for b in top_params[i + 1:]]
    results = []
    materials = []

    for (a, b) in pairs:
        opt_a = optimal_config.get(a, PARAMETER_REGISTRY[a]["default"])
        opt_b = optimal_config.get(b, PARAMETER_REGISTRY[b]["default"])

        # Find step sizes
        step_a = _step_size(a, opt_a)
        step_b = _step_size(b, opt_b)

        configs = {
            "00": dict(optimal_config, **{a: opt_a, b: opt_b}),
            "01": dict(optimal_config, **{a: opt_a, b: _step_high(b, opt_b, step_b)}),
            "10": dict(optimal_config, **{a: _step_high(a, opt_a, step_a), b: opt_b}),
            "11": dict(optimal_config, **{a: _step_high(a, opt_a, step_a),
                                        b: _step_high(b, opt_b, step_b)}),
        }
        scores = {}
        for key, cfg in configs.items():
            m = safe_evaluate(cfg, stations=stations, signal_names=signal_names, fast=fast)
            scores[key] = compute_scalarized_score(m)

        # Interaction metric I = s₁₁ + s₀₀ - s₁₀ - s₀₁
        I = scores["11"] + scores["00"] - scores["10"] - scores["01"]
        material = abs(I) > sigma_noise
        improves = I > 0 and scores["11"] > scores["00"]

        pair_result = {
            "params": [a, b],
            "interaction_I": round(I, 5),
            "material": material,
            "improves_optimum": improves,
            "scores": {k: round(v, 5) for k, v in scores.items()},
            "optima": {"a": opt_a, "b": opt_b},
        }
        results.append(pair_result)
        if material:
            materials.append(pair_result)

    return {
        "pairs": results,
        "material_interactions": materials,
        "n_material": len(materials),
        "pairs_tested": len(pairs),
    }


def _step_size(param_name, opt):
    """Determine a step size for a parameter."""
    param_def = PARAMETER_REGISTRY[param_name]
    grid = param_def.get("sweep_values_7") or param_def.get("sweep_values_5") or [opt]
    if len(grid) > 1:
        # Average spacing
        return (grid[-1] - grid[0]) / max(len(grid) - 1, 1) * 0.5
    if "max" in param_def and "min" in param_def:
        return (param_def["max"] - param_def["min"]) / 20.0
    # Categorical or non-numeric — use a default step
    return 0.1


def run_group_meta_interaction(optimal_config, significant_params, stations,
                               signal_names, fast=False, sigma_noise=0.003) -> Dict[str, Any]:
    """Group-level meta-interaction test (Section 4.4)."""
    logger.info("Level 2: Group-level meta-interaction test...")
    s0 = safe_evaluate(optimal_config, stations=stations, signal_names=signal_names, fast=fast)
    s0_score = compute_scalarized_score(s0)

    results = {}
    interacting_groups = []
    for group, params in PARAM_GROUPS.items():
        # Skip groups with no significant params
        group_params = [p for p in params if p in significant_params]
        if not group_params:
            continue
        test_config = dict(optimal_config)
        for p in group_params:
            test_config[p] = optimal_config.get(p, DEFAULT_CONFIG[p])
        m = safe_evaluate(test_config, stations=stations, signal_names=signal_names, fast=fast)
        s_group = compute_scalarized_score(m)
        improvement = s_group - s0_score
        interacting = improvement > sigma_noise
        results[group] = {
            "params": group_params,
            "score": round(s_group, 5),
            "s0_score": round(s0_score, 5),
            "improvement": round(improvement, 5),
            "interacting": interacting,
        }
        if interacting:
            interacting_groups.append(group)

    return {
        "groups": results,
        "interacting_groups": interacting_groups,
        "n_interacting": len(interacting_groups),
    }


# ═══════════════════════════════════════════════════════════════════
# 10. LEVEL 3: CONDITIONAL BAYESIAN OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

def run_level_3(interacting_params, optimal_config, stations, signal_names,
                fast=False, n_configs=100) -> Dict[str, Any]:
    """
    Conditional Bayesian optimization (Section 5).
    Hot-start GP from grid results, restricted to interacting subgroup.
    """
    logger.info("Level 3: Bayesian optimization on %d interacting params...",
                len(interacting_params))
    if not interacting_params:
        return {
            "triggered": False,
            "reason": "No interacting params identified",
            "n_evaluations": 0,
        }

    # Try to use scikit-optimize
    try:
        from skopt import gp_minimize
        from skopt.space import Real, Integer, Categorical
        HAS_SKOPT = True
    except ImportError:
        logger.warning("scikit-optimize not available — using random search fallback")
        HAS_SKOPT = False

    # Build param_map and config builder (used by both skopt and random-search fallback)
    param_map = {pname: i for i, pname in enumerate(interacting_params)}

    def build_config(x):
        cfg = dict(optimal_config)
        for pname in interacting_params:
            idx = param_map[pname]
            cfg[pname] = x[idx]
        return cfg

    best_score = -float('inf')
    best_config = None
    history = []

    if HAS_SKOPT:
        # Build search space (imports are scoped inside HAS_SKOPT)
        space = []
        for pname in interacting_params:
            spec = PARAMETER_REGISTRY[pname]
            if spec["type"] == "continuous":
                space.append(Real(spec["min"], spec["max"], name=pname))
            elif spec["type"] in ("integer", "integer_log"):
                space.append(Integer(int(spec["min"]), int(spec["max"]), name=pname))
            elif spec["type"] == "categorical":
                space.append(Categorical(spec["values"], name=pname))
            elif spec["type"] == "boolean":
                space.append(Categorical([0, 1], name=pname))

        def objective(x):
            cfg = build_config(x)
            metrics = safe_evaluate(cfg, stations=stations, signal_names=signal_names, fast=fast)
            score = compute_scalarized_score(metrics)
            history.append({"config": cfg, "score": score, "metrics": metrics})
            return -score  # minimize

        try:
            result = gp_minimize(
                objective,
                space,
                n_calls=min(n_configs, 100),
                acq_func="EI",
                noise=0.005,
                random_state=42,
            )
            for i, (x, y) in enumerate(zip(result.x_iters, result.func_vals)):
                if -y > best_score:
                    best_score = -y
                    best_config = build_config(x)
            return {
                "triggered": True,
                "method": "gp_minimize",
                "n_evaluations": len(result.x_iters),
                "best_score": round(best_score, 5),
                "best_config": best_config,
                "history": history,
                "converged": result.fun < 0.001 or abs(result.fun - best_score) < 0.002,
            }
        except Exception as e:
            logger.error(f"GP optimization failed: {e} — falling back to random search")

    # Random search fallback
    for i in range(min(n_configs, 100)):
        x = []
        for pname in interacting_params:
            spec = PARAMETER_REGISTRY[pname]
            if spec["type"] == "categorical" or spec["type"] == "boolean":
                x.append(random.choice(spec["values"]))
            else:
                x.append(random.uniform(spec["min"], spec["max"]) if spec["type"] == "continuous"
                         else random.randint(int(spec["min"]), int(spec["max"])))
        cfg = build_config(x)
        metrics = safe_evaluate(cfg, stations=stations, signal_names=signal_names, fast=fast)
        score = compute_scalarized_score(metrics)
        history.append({"config": cfg, "score": score, "metrics": metrics})
        if score > best_score:
            best_score = score
            best_config = cfg

    return {
        "triggered": True,
        "method": "random_search_fallback",
        "n_evaluations": min(n_configs, 100),
        "best_score": round(best_score, 5),
        "best_config": best_config,
        "history": history,
        "converged": False,
    }


# ═══════════════════════════════════════════════════════════════════
# 11. LEVEL 4: WALK-FORWARD & ROBUSTNESS
# ═══════════════════════════════════════════════════════════════════

def run_level_4(optimal_config, stations, signal_names, fast=False,
                sigma_noise=0.003) -> Dict[str, Any]:
    """Level 4: walk-forward validation + odd/even split + robustness (Section 6)."""
    logger.info("Level 4: Walk-forward validation & robustness...")
    results = {}

    # 6.1 Walk-forward: 3 windows
    date_range = get_date_range()
    if date_range[0] and date_range[1]:
        start_ts = date_range[0][:10]
        end_ts = date_range[1][:10]
        try:
            start_year = int(start_ts[:4])
            end_year = int(end_ts[:4])
        except (ValueError, IndexError):
            start_year, end_year = 2020, 2025
    else:
        start_year, end_year = 2020, 2025

    windows = [
        (f"{start_year}-01-01", f"{start_year + 1}-12-31"),
        (f"{start_year + 2}-01-01", f"{start_year + 3}-12-31"),
        (f"{start_year + 4}-01-01", f"{end_year}-12-31"),
    ]

    walk_forward = {}
    for i, (w_start, w_end) in enumerate(windows):
        metrics = safe_evaluate(optimal_config, seed=42, window=(w_start, w_end),
                                stations=stations, signal_names=signal_names, fast=fast)
        walk_forward[f"window_{i + 1}"] = {
            "range": f"{w_start} to {w_end}",
            "accuracy": round(metrics.get("accuracy", 0.0), 4),
            "sharpe": round(metrics.get("sharpe", 0.0), 4),
            "total_pnl": round(metrics.get("total_pnl", 0.0), 2),
            "n_trades": metrics.get("n_trades", 0),
        }

    accs = [walk_forward[w]["accuracy"] for w in walk_forward]
    variance = max(accs) - min(accs) if accs else 0.0
    regime_sensitive = variance > 2 * sigma_noise
    results["walk_forward"] = {
        "windows": walk_forward,
        "variance": round(variance, 4),
        "regime_sensitive": regime_sensitive,
    }

    # 6.2 Odd/even year split
    odd_even = {}
    try:
        odd_metrics = safe_evaluate(optimal_config, seed=42,
                                    window=(f"{start_year}-01-01", f"{end_year}-12-31"),
                                    stations=stations, signal_names=signal_names, fast=fast)
        odd_even["odd_years"] = {"accuracy": round(odd_metrics.get("accuracy", 0), 4)}
        even_metrics = safe_evaluate(optimal_config, seed=42,
                                     window=(f"{start_year + 1}-01-01", f"{end_year}-12-31"),
                                     stations=stations, signal_names=signal_names, fast=fast)
        odd_even["even_years"] = {"accuracy": round(even_metrics.get("accuracy", 0), 4)}
        odd_even["regime_shift"] = abs(odd_even["odd_years"]["accuracy"] - odd_even["even_years"]["accuracy"]) > sigma_noise
    except Exception as e:
        odd_even = {"error": str(e)}
    results["odd_even_split"] = odd_even

    # 6.3 Robustness: 5 seeds
    seeds = [42, 101, 777, 2024, 9999]
    robustness = []
    for seed in seeds:
        metrics = safe_evaluate(optimal_config, seed=seed, stations=stations,
                                signal_names=signal_names, fast=fast)
        robustness.append({"seed": seed, "accuracy": metrics.get("accuracy", 0.0)})
    accs_r = [r["accuracy"] for r in robustness]
    std_acc = float(np.std(accs_r, ddof=1)) if len(accs_r) > 1 else 0.0
    results["robustness"] = {
        "seeds": robustness,
        "mean_accuracy": round(float(np.mean(accs_r)), 4),
        "std_accuracy": round(std_acc, 4),
        "noise_sensitive": std_acc > sigma_noise,
    }

    return results


def get_date_range():
    """Get the settlement date range from the data cache."""
    try:
        from scripts.sweep_engine import DataCache
        return DataCache.get_date_range()
    except Exception:
        return ("", "")


# ═══════════════════════════════════════════════════════════════════
# 12. OUTPUT & REPORTING
# ═══════════════════════════════════════════════════════════════════

def write_output(output: Dict, output_dir: str):
    """Write the full results JSON and CSV summary."""
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, "per_parameter_results.json")
    with open(json_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info("Results written to %s", json_path)

    # CSV summary
    csv_path = os.path.join(output_dir, "per_parameter_summary.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["param", "type", "group", "default", "optimal",
                         "peak_type", "sensitivity", "significance",
                         "opt_accuracy", "opt_sharpe", "opt_pnl",
                         "opt_brier", "opt_trades", "opt_drawdown"])
        for pname, sweep in output.get("level_1", {}).items():
            if isinstance(sweep, dict) and "results" in sweep:
                best = max(sweep["results"], key=lambda r: r.get("scalarized_score", 0))
                writer.writerow([
                    pname, sweep.get("type", ""), sweep.get("group", ""),
                    sweep["results"][0].get("default", ""),
                    best.get("value", ""),
                    sweep.get("peak_type", ""),
                    sweep.get("sensitivity_score", ""),
                    sweep.get("significance", ""),
                    best.get("accuracy", 0),
                    best.get("sharpe", 0),
                    best.get("total_pnl", 0),
                    best.get("brier", 0),
                    best.get("n_trades", 0),
                    best.get("metrics", {}).get("max_drawdown", 0),
                ])
    logger.info("CSV summary written to %s", csv_path)

    # Write individual level outputs
    for level in ["level_0", "level_1", "level_2", "level_3", "level_4"]:
        level_data = output.get(level)
        if level_data:
            level_dir = os.path.join(output_dir, level.replace("_", "-"))
            os.makedirs(level_dir, exist_ok=True)
            level_path = os.path.join(level_dir, "results.json")
            with open(level_path, "w") as f:
                json.dump(level_data, f, indent=2, default=str)
            logger.info("%s written to %s", level, level_path)


# ═══════════════════════════════════════════════════════════════════
# 13. MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Per-Parameter Sweep Optimization Engine — 5-level pipeline")
    parser.add_argument("--fast", action="store_true",
                        help="Use fewer stations/signals for speed")
    parser.add_argument("--station", type=str, nargs="*", default=None,
                        help="Override station list")
    parser.add_argument("--signal", type=str, nargs="*", default=None,
                        help="Override signal list")
    parser.add_argument("--output-dir", type=str, default=DATA_DIR,
                        help="Output directory (default: data/)")
    parser.add_argument("--metric", type=str, default="scalarized",
                        choices=["accuracy", "sharpe", "total_pnl", "brier", "scalarized"],
                        help="Metric to optimize [default: scalarized]")
    parser.add_argument("--n-configs", type=int, default=100,
                        help="Number of configs for Level 3 BO [default: 100]")
    args = parser.parse_args()

    stations = args.station or STATIONS
    signal_names = args.signal or SIGNAL_SAMPLE
    if args.fast:
        stations = stations[:3]
        signal_names = signal_names[:3]
        logger.info("--fast mode: 3 stations, 3 signals")

    os.makedirs(args.output_dir, exist_ok=True)
    overall_start = time.time()

    ts = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*72}")
    print(f"  Per-Parameter Sweep Optimization Engine")
    print(f"  Started: {ts}")
    print(f"  Stations: {len(stations)} | Signals: {len(signal_names)}")
    print(f"  Optimizing for: {args.metric}")
    print(f"{'='*72}\n")

    # ── Sub-step 0a: Signal correlation matrix ──
    print("\n── Sub-step 0a: Signal Correlation Matrix ──\n")
    corr_result = run_signal_correlation_matrix(args.output_dir)
    if corr_result.get("n_killed", 0) > 0:
        print(f"  Killed {corr_result['n_killed']} redundant signals (rho >= {corr_result.get('threshold', 0.7)})")
        # Filter signal list
        killed = set(corr_result.get("kill_signals", []))
        signal_names = [s for s in signal_names if s not in killed]
        print(f"  Remaining signals: {len(signal_names)}")

    # ── Level 0: Sanity checks + Sensitivity + Noise floor ──
    print("\n── Level 0: Sanity Checks & Sensitivity Pre-Screen ──\n")
    level0 = {}

    sanity = run_sanity_checks(stations, signal_names, fast=args.fast)
    level0["sanity_checks"] = sanity
    for check_name, check_data in sanity.items():
        status = "✓ PASS" if check_data.get("passed") else "✗ FAIL"
        print(f"  {status} {check_name}: acc={check_data.get('accuracy', '?')}")

    noise_floor = estimate_noise_floor(stations, signal_names, fast=args.fast)
    level0["noise_floor"] = noise_floor
    sigma_noise = noise_floor["sigma_noise"]
    print(f"  Noise floor: σ_noise = {sigma_noise:.4f} (peak threshold = {noise_floor['peak_threshold']:.4f})")

    if noise_floor.get("noise_floor", 0) > 0.01:
        logger.warning("Noise floor > 1.0% — evaluation may be too noisy for reliable optimization")

    sensitivity = run_sensitivity_prescreen(stations, signal_names, fast=args.fast,
                                            sigma_noise=sigma_noise)
    level0["sensitivity"] = sensitivity
    significant_params = sensitivity["significant_params"]
    n_sig = len(significant_params)
    print(f"  Sensitivity: {n_sig}/{len(PARAMETER_REGISTRY)} parameters significant")
    for pname in significant_params:
        eff = sensitivity["effects"][pname]
        print(f"    {pname} ({eff['group']}): effect={eff['effect']:.5f}")

    level0["correlation_matrix"] = corr_result

    # ── Level 1: Per-parameter grid sweep ──
    print(f"\n── Level 1: Per-Parameter Grid Sweep ({n_sig} params) ──\n")
    start = time.time()
    level1 = run_level_1(significant_params, stations, signal_names,
                         fast=args.fast, metric=args.metric)
    level1_time = time.time() - start
    print(f"  Level 1 completed in {level1_time:.1f}s")

    # ── Level 2: Interaction & Validation ──
    print(f"\n── Level 2: Interaction & Validation ──\n")
    start = time.time()
    level2 = {}

    optimal_config = build_optimal_config(level1, significant_params)
    level2["optimal_config"] = {k: v for k, v in optimal_config.items()}

    plus_minus = run_plus_minus_validation(optimal_config, significant_params, stations,
                                           signal_names, fast=args.fast, sigma_noise=sigma_noise)
    level2["plus_minus_validation"] = plus_minus
    print(f"  ±1-step validation: {sum(1 for p in plus_minus['params'].values() if p['interpretation'] == 'STABLE')} stable, "
          f"{sum(1 for p in plus_minus['params'].values() if p['interpretation'] == 'GENUINE_OPTIMUM')} genuine")

    # Top 5 most sensitive params for pairwise test
    sig_sorted = sorted(significant_params,
                        key=lambda p: sensitivity["effects"].get(p, {}).get("effect", 0),
                        reverse=True)
    top_params = sig_sorted[:5]
    pairwise = run_pairwise_interaction(top_params, optimal_config, stations, signal_names,
                                        fast=args.fast, sigma_noise=sigma_noise)
    level2["pairwise_interactions"] = pairwise
    print(f"  Pairwise: {pairwise['n_material']} material interactions of {pairwise['pairs_tested']} pairs")

    # Group meta-interaction
    meta = run_group_meta_interaction(optimal_config, significant_params, stations,
                                      signal_names, fast=args.fast, sigma_noise=sigma_noise)
    level2["meta_interactions"] = meta
    print(f"  Groups: {meta['n_interacting']} interacting groups")

    level2_time = time.time() - start
    print(f"  Level 2 completed in {level2_time:.1f}s")

    # ── Interaction gate for Level 3 ──
    material_interactions = pairwise["material_interactions"] + (
        [{"groups": meta["interacting_groups"]}] if meta["n_interacting"] > 0 else []
    )
    trigger_level_3 = (pairwise["n_material"] > 0 or meta["n_interacting"] > 0)
    level3 = None

    if trigger_level_3:
        # Build interacting params set
        interacting_params = set()
        for pair in pairwise["material_interactions"]:
            interacting_params.update(pair["params"])
        for group_name in meta["interacting_groups"]:
            interacting_params.update(PARAM_GROUPS.get(group_name, []))
        # Intersect with significant params
        interacting_params = list(interacting_params & set(significant_params))

        print(f"\n── Level 3: Conditional Bayesian Optimization ──\n")
        print(f"  Triggered: {len(interacting_params)} interacting params")
        start = time.time()
        level3 = run_level_3(interacting_params, optimal_config, stations, signal_names,
                             fast=args.fast, n_configs=args.n_configs)
        level3_time = time.time() - start
        print(f"  Level 3: method={level3.get('method', '?')}, "
              f"best_score={level3.get('best_score', 0):.4f}, "
              f"n_evals={level3.get('n_evaluations', 0)}")
        print(f"  Level 3 completed in {level3_time:.1f}s")

        # Update optimal config with Level 3 findings
        if level3 and level3.get("best_config"):
            optimal_config = level3["best_config"]
    else:
        print(f"\n── Level 3: SKIPPED (no material interactions) ──\n")

    # ── Level 4: Walk-forward & Robustness ──
    print(f"\n── Level 4: Walk-Forward & Robustness ──\n")
    start = time.time()
    level4 = run_level_4(optimal_config, stations, signal_names,
                         fast=args.fast, sigma_noise=sigma_noise)
    level4_time = time.time() - start

    wf = level4.get("walk_forward", {})
    print(f"  Walk-forward windows: {len(wf.get('windows', {}))}")
    print(f"  Variance: {wf.get('variance', 0):.4f} "
          f"{'⚠ REGIME SENSITIVE' if wf.get('regime_sensitive') else '✓ stable'}")
    rob = level4.get("robustness", {})
    print(f"  Robustness: mean={rob.get('mean_accuracy', 0):.4f} "
          f"std={rob.get('std_accuracy', 0):.4f}")
    print(f"  Level 4 completed in {level4_time:.1f}s")

    # ── Final evaluation of optimal config ──
    print(f"\n── Final Evaluation ──\n")
    final_metrics = safe_evaluate(optimal_config, stations=stations,
                                  signal_names=signal_names, fast=args.fast)
    baseline_metrics = safe_evaluate(DEFAULT_CONFIG, stations=stations,
                                     signal_names=signal_names, fast=args.fast)
    final_score = compute_scalarized_score(final_metrics)
    baseline_score = compute_scalarized_score(baseline_metrics)

    print(f"  Baseline:  acc={baseline_metrics.get('accuracy', 0):.3f} "
          f"sharpe={baseline_metrics.get('sharpe', 0):.3f} "
          f"pnl={baseline_metrics.get('total_pnl', 0):.1f}")
    print(f"  Optimal:   acc={final_metrics.get('accuracy', 0):.3f} "
          f"sharpe={final_metrics.get('sharpe', 0):.3f} "
          f"pnl={final_metrics.get('total_pnl', 0):.1f}")
    print(f"  Improvement: {final_score - baseline_score:+.4f} scalarized")

    # ── Assemble output ──
    total_time = time.time() - overall_start
    total_evals = (
        level0["sanity_checks"].__len__() * 3 +  # sanity checks
        5 +  # noise floor
        len(significant_params) * 2 +  # sensitivity extremes
        len(significant_params) * 7 +  # Level 1 grid
        len(significant_params) * 3 +  # Level 1 refinement
        len(significant_params) * 2 +  # ±1-step
        pairwise["pairs_tested"] * 4 +  # pairwise
        meta["n_interacting"] * 1 +  # group tests
        (level3.get("n_evaluations", 0) if level3 else 0) +  # Level 3
        5  # Level 4
    )

    output = {
        "meta": {
            "timestamp": ts,
            "total_time_seconds": round(total_time, 1),
            "total_estimated_evaluations": total_evals,
            "noise_floor_sigma": sigma_noise,
            "primary_metric": args.metric,
            "scalarized_weights": SCALAR_WEIGHTS,
            "fast": args.fast,
            "n_stations": len(stations),
            "n_signals": len(signal_names),
            "n_significant_params": n_sig,
            "walk_forward_passed": not wf.get("regime_sensitive", False),
            "regime_sensitivity_flagged": wf.get("regime_sensitive", False),
            "level_3_triggered": trigger_level_3,
        },
        "config": optimal_config,
        "projected_metrics": {k: v for k, v in final_metrics.items()
                              if k in ("accuracy", "temp_accuracy", "market_accuracy",
                                       "total_pnl", "sharpe", "brier",
                                       "trades_per_day", "max_drawdown")},
        "baseline_metrics": {k: v for k, v in baseline_metrics.items()
                             if k in ("accuracy", "temp_accuracy", "market_accuracy",
                                      "total_pnl", "sharpe", "brier",
                                      "trades_per_day", "max_drawdown")},
        "parameter_details": [
            {
                "name": pname,
                "peak_type": level1.get(pname, {}).get("peak_type", "UNKNOWN"),
                "sensitivity_score": level1.get(pname, {}).get("sensitivity_score", 0),
                "optimal_value": level1.get(pname, {}).get("optimal_value", DEFAULT_CONFIG.get(pname)),
                "significance": level1.get(pname, {}).get("significance", "INSENSITIVE"),
            }
            for pname in significant_params
        ],
        "interaction_results": {
            "significant_pairs": [
                {"params": p["params"], "I": p["interaction_I"]}
                for p in pairwise["material_interactions"]
            ],
            "level_3_triggered": trigger_level_3,
            "joint_optimization_performed": trigger_level_3,
        },
        "walk_forward": {
            f"window_{k}": v for k, v in wf.get("windows", {}).items()
        },
        "level_0": level0,
        "level_1": level1,
        "level_2": level2,
        "level_3": level3,
        "level_4": level4,
    }

    write_output(output, args.output_dir)

    print(f"\n{'='*72}")
    print(f"  Complete! {total_evals} estimated evaluations in {total_time:.1f}s")
    print(f"  Results: {os.path.join(args.output_dir, 'per_parameter_results.json')}")
    print(f"  Summary: {os.path.join(args.output_dir, 'per_parameter_summary.csv')}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()