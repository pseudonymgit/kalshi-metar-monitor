#!/usr/bin/env python3
"""
sweep_metrics
Extracted from scripts/big_sweep.py.
"""

import math
import json
import csv
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from core.platt_calibration import PlattCalibrationPipeline
from core.bma_emos import bma_calibrate, emos_calibrate
from core.db_utils import query_db, with_db
from core.continuous_kelly import fee_aware_kelly, KellyState
from core.production_gate import LossLimiter
from scripts.sweep.config import SLIPPAGE_BUDGET
from scripts.sweep_engine import MIN_TRADES_REPORT
from scripts.sweep.tiers import get_tier, get_tier_info

def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(KALSHI_REAL_FEE_RATE * contracts * price * (1.0 - price))
# ═══════════════════════════════════════════════════════════════
# 1. ALL 36 SIGNALS
# ═══════════════════════════════════════════════════════════════
def calibrate_confidence(
    station: str,
    direction: str,
    signal_name: str,
    raw_conf: float,
    date_str: str,
    calibration_mode: str = 'platt',
) -> Optional[Tuple[float, float]]:
    """
    Return (calibrated_conf, raw_conf). Falls back to raw if no curve.
    Returns None if calibrated P(correct) < 0.5 — skip the trade.

    Supports calibration modes:
      'platt' — Platt scaling (default)
      'bma'   — Bayesian Model Averaging (ensemble-level)
      'emos'  — Ensemble Model Output Statistics (ensemble-level)
      'both'  — Average of Platt + BMA/EMOS
    """
    pipeline = get_platt_pipeline()
    market_type = DataCache.get_market_type(station, date_str)
    platt_conf = pipeline.calibrate(
        station=station,
        direction=direction,
        market_type=market_type,
        signal_name=signal_name,
        raw_conf=raw_conf,
    )

    # BMA/EMOS integration: for ensemble signals, apply BMA/EMOS calibration
    bma_conf = None
    if calibration_mode in ('bma', 'emos', 'both') and signal_name.startswith('eighty_two'):
        try:
            from core.bma_emos import bma_calibrate, emos_calibrate
            if calibration_mode == 'bma' or calibration_mode == 'both':
                bma_conf = bma_calibrate(station, (raw_conf, None))
            elif calibration_mode == 'emos':
                # EMOS needs ensemble mean/variance — use raw_conf as proxy
                bma_conf = raw_conf
        except Exception:
            pass

    # Combine calibration results
    if bma_conf is not None and calibration_mode == 'both':
        cal_conf = (platt_conf + bma_conf) / 2.0
    elif bma_conf is not None and calibration_mode in ('bma', 'emos'):
        cal_conf = bma_conf
    else:
        cal_conf = platt_conf
    
    # Guard on calibrated output
    if cal_conf < 0.5:
        return None
    
    return (cal_conf, raw_conf)
    
    return (cal_conf, raw_conf)

def simulate_trade(pred_direction, confidence, actual_direction, config, station="", date_str="", signal_name="", loss_limiter: Optional[LossLimiter] = None):
    if pred_direction is None or actual_direction is None or actual_direction == 0:
        return None
    pred_up = 1 if pred_direction == 'up' else -1
    if pred_up == 0:
        return None
    confidence = max(0.501, min(0.999, confidence))
    # Use adaptive threshold when enabled, falling back to static config
    conf_floor = config.get("confidence_floor", 0.5)
    if _ADAPTIVE_THRESHOLDS_ENABLED and _ADAPTIVE_REGISTRY is not None and signal_name:
        conf_floor = _ADAPTIVE_REGISTRY.get_threshold(signal_name, station)
    if confidence < conf_floor:
        return None

    # Settlement Execution Gate check — skip trade if gate blocks it
    if _SETTLEMENT_GATE_ENABLED and _SETTLEMENT_GATE is not None:
        market_type = DataCache.get_market_type(station, date_str)
        epoch_id = f"{date_str}_{market_type}"
        gate_result = _SETTLEMENT_GATE.evaluate(
            station=station,
            trading_date=date_str,
            epoch_id=epoch_id,
        )
        if not gate_result.verdict.is_pass():
            return None

    fee_type = config.get("fee_type", FT_TAKER_SLIPPAGE)
    edge_threshold = config.get("edge_threshold", 0.05)
    kelly_fraction = config.get("kelly_fraction", 0.5)
    entry_price_min = config.get("entry_price_min", 0.01)
    entry_price_max = config.get("entry_price_max", 0.95)
    max_contracts = config.get("max_contracts", 100)
    slippage_budget = config.get("slippage_budget", SLIPPAGE_BUDGET)
    fee_deduction = config.get("fee_deduction", 1.0)
    pos_sizing = config.get("position_sizing_model", 1)

    market_price = min(0.95, max(0.05, 0.5 + (confidence - 0.5) * 0.8))
    entry_price = market_price if pred_up == 1 else 1.0 - market_price
    if entry_price < entry_price_min or entry_price > entry_price_max:
        return None
    gross_edge = confidence - entry_price
    fee_frac_entry = (KALSHI_REAL_FEE_RATE * market_price * (1.0 - market_price)
                      if 0 < market_price < 1 else 0.0)
    if fee_type == FT_NONE:
        rt_fee_frac = 0.0
    elif fee_type == FT_TAKER:
        rt_fee_frac = fee_frac_entry
    elif fee_type == FT_TAKER_SLIPPAGE:
        rt_fee_frac = fee_frac_entry + slippage_budget
    else:
        rt_fee_frac = 0.0
    net_edge = gross_edge - rt_fee_frac * fee_deduction
    tier_info = get_tier_info(station)
    tier_adjusted = net_edge * (1.0 - tier_info["discount"])
    if tier_adjusted < edge_threshold:
        return None
    if pos_sizing == 1:
        # Discrete Kelly formula (existing)
        if net_edge <= 0 or entry_price >= 1.0:
            return None
        kelly_pct = net_edge / (1.0 - entry_price)
        n_contracts = max(1, int(min(max_contracts, kelly_pct * kelly_fraction * 1000)))
    elif pos_sizing == 4:
        # Continuous Kelly from core.continuous_kelly
        n_contracts = fee_aware_kelly(
            pred_price=confidence,
            market_price=market_price,
            confidence=confidence,
            direction="up" if pred_up == 1 else "down",
            kelly_frac=kelly_fraction,
            max_contracts=max_contracts,
            capital=50000.0,
            station=station,
            signal_name="",
        )
        if n_contracts == 0:
            return None
    elif pos_sizing == 5:
        # Variance-Adjusted Kelly Sizing (from core.variance_weighted_sizing)
        try:
            variance_mult = config.get("variance_multiplier", 0.5)
            variance = variance_mult * 0.25  # Map directional strength to variance [0, 0.25]
            total_variance = 1.0 - variance * 4.0  # Invert: high directional strength → low variance
            total_variance = max(0.05, min(1.0, total_variance))

            # Use variance_adjusted_kelly from the imported module
            n_contracts = variance_adjusted_kelly(
                capital=50000.0,
                edge=max(0, net_edge),
                variance=total_variance,
                base_kelly=kelly_fraction,
                aggressiveness_k=2.0,
                floor_multiplier=0.1,
                max_position_fraction=0.25,
            )
            n_contracts = min(n_contracts, max_contracts)
            if n_contracts == 0:
                return None
        except Exception:
            # Fallback to model 1 on error
            if net_edge <= 0 or entry_price >= 1.0:
                return None
            kelly_pct = net_edge / (1.0 - entry_price)
            n_contracts = max(1, int(min(max_contracts, kelly_pct * kelly_fraction * 1000)))
    elif pos_sizing == 2:
        cap = {1: 200, 2: 100}.get(get_tier(station), 50)
        n_contracts = max(1, min(max_contracts, cap))
    else:
        n_contracts = max(1, min(max_contracts, 100))
    # Apply LossLimiter scale factor (scales down after consecutive losses)
    if loss_limiter is not None:
        scale = loss_limiter.get_scale_factor()
        n_contracts = max(0, int(n_contracts * scale))
        if n_contracts == 0:
            return None
    correct = pred_up == actual_direction
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price
    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_fee = kalshi_fee(n_contracts, 1.0 if correct else 0.0)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees
    brier_c = (confidence - (1.0 if correct else 0.0)) ** 2
    return {"station": station, "date": date_str, "predicted": pred_up, "actual": actual_direction,
            "confidence": confidence, "market_price": market_price, "entry_price": entry_price,
            "gross_edge": gross_edge, "net_edge": net_edge, "contracts": n_contracts,
            "correct": correct, "net_pnl": net_pnl, "gross_pnl": gross_pnl, "cost": cost,
            "entry_fee": entry_fee, "exit_fee": exit_fee, "total_fees": total_fees,
            "brier_contrib": brier_c, "market_type": DataCache.get_market_type(station, date_str)}
def safe_signal_evaluate(signal_obj, idx, days):
    try:
        if hasattr(signal_obj, 'evaluate'):
            result = signal_obj.evaluate(idx, days)
            if isinstance(result, tuple) and len(result) == 2:
                return result
        elif callable(signal_obj):
            result = signal_obj(days, idx)
            if isinstance(result, tuple) and len(result) == 2:
                return result
        return None, 0.0
    except Exception:
        return None, 0.0
def compute_metrics(trades, min_trades=MIN_TRADES_REPORT):
    if len(trades) < min_trades:
        return {"n_trades": 0, "accuracy": 0.0, "total_pnl": 0.0, "sharpe": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0, "avg_pnl_per_trade": 0.0,
                "win_rate": 0.0, "brier_score": 0.0, "ece": 0.0, "total_fees": 0.0,
                "total_cost": 0.0, "avg_confidence": 0.0, "n_dates": 0}
    n = len(trades)
    correct = sum(1 for t in trades if t["correct"])
    accuracy = correct / n
    daily_pnl = defaultdict(float)
    confs = []
    for t in trades:
        daily_pnl[t["date"]] += t["net_pnl"]
        confs.append(t["confidence"])
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["total_fees"] for t in trades)
    total_cost = sum(t["cost"] for t in trades)
    win_rate = correct / n
    avg_conf = float(np.mean(confs))
    gross_wins = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gross_losses = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0)
    sorted_dates = sorted(daily_pnl.keys())
    bankroll = 10000.0
    daily_rets = []
    for date in sorted_dates:
        ret = daily_pnl[date] / bankroll if bankroll > 0 else 0.0
        daily_rets.append(ret)
        bankroll += daily_pnl[date]
    nd = len(daily_rets)
    if nd > 1:
        dm = float(np.mean(daily_rets))
        ds = float(np.std(daily_rets, ddof=1))
        sharpe = (dm / ds * math.sqrt(252)) if ds > 0 else 0.0
    else:
        sharpe = 0.0
    cum_ret = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in daily_rets:
        cum_ret += r
        peak = max(peak, cum_ret)
        if peak > 0:
            dd = (peak - cum_ret) / peak
            max_dd = max(max_dd, dd)
    brier = sum(t["brier_contrib"] for t in trades) / n
    ece = 0.0
    if n >= MIN_TRADES_CALIBRATE:
        n_bins = 10
        bin_acc = np.zeros(n_bins)
        bin_conf = np.zeros(n_bins)
        bin_counts = np.zeros(n_bins)
        for t in trades:
            c = min(0.999, t["confidence"])
            if c < 0.5:
                continue
            bi = min(int((c - 0.5) / 0.5 * n_bins), n_bins - 1)
            bin_acc[bi] += 1 if t["correct"] else 0
            bin_conf[bi] += c
            bin_counts[bi] += 1
        for i in range(n_bins):
            if bin_counts[i] > 0:
                bin_acc[i] /= bin_counts[i]
                bin_conf[i] /= bin_counts[i]
        ece = float(np.sum(bin_counts * np.abs(bin_acc - bin_conf)) / np.sum(bin_counts) if np.sum(bin_counts) > 0 else 0.0)
    return {"n_trades": n, "accuracy": float(accuracy), "total_pnl": float(total_pnl),
            "sharpe": float(sharpe), "profit_factor": float(pf), "max_drawdown": float(max_dd),
            "avg_pnl_per_trade": float(total_pnl/n) if n else 0.0, "win_rate": float(win_rate),
            "brier_score": float(brier), "ece": float(ece), "total_fees": float(total_fees),
            "total_cost": float(total_cost), "avg_confidence": float(avg_conf), "n_dates": nd}
# ═══════════════════════════════════════════════════════════════
# 5. 3-STAGE VALIDATION
# ═══════════════════════════════════════════════════════════════
def compute_correlation_matrix(signal_results):
    names = [s for s, r in signal_results.items() if r.get("total_trades", 0) > 0]
    n = len(names)
    if n < 2:
        return np.eye(max(1, n)), names or [list(signal_results.keys())[0]]
    corr = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i == j:
                corr[i, j] = 1.0
            else:
                a1 = signal_results[names[i]].get("aggregate", {}).get("accuracy", 0)
                a2 = signal_results[names[j]].get("aggregate", {}).get("accuracy", 0)
                corr[i, j] = 1.0 - abs(a1 - a2)
    return corr, names
def compute_differential_results(signal_results):
    sigs = [(s, r) for s, r in signal_results.items()
            if r.get("total_trades", 0) >= MIN_TRADES_REPORT]
    results = []
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            s1, r1 = sigs[i]
            s2, r2 = sigs[j]
            a1 = r1.get("aggregate", {})
            a2 = r2.get("aggregate", {})
            d_acc = a2.get("accuracy", 0) - a1.get("accuracy", 0)
            d_pnl = a2.get("total_pnl", 0) - a1.get("total_pnl", 0)
            d_shp = a2.get("sharpe", 0) - a1.get("sharpe", 0)
            agree = 1.0 - abs(d_acc)
            results.append({"signal_a": s1, "signal_b": s2, "delta_accuracy": d_acc,
                            "delta_pnl": d_pnl, "delta_sharpe": d_shp,
                            "agreement_rate": agree, "abs_delta_accuracy": abs(d_acc)})
    results.sort(key=lambda x: x["abs_delta_accuracy"], reverse=True)
    return results
