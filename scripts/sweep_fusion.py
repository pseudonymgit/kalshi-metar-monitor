#!/usr/bin/env python3
"""
sweep_fusion
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

from scripts.sweep_metrics import (
    compute_metrics, simulate_trade, calibrate_confidence, kalshi_fee
)
from scripts.sweep_validation import (
    split_discovery_holdout, split_geo_holdout, evaluate_signal_on_station,
    _eval_wrapper, evaluate_signal_all_stations
)
from scripts.sweep_engine import DataCache, STATIONS
from core.signal_fusion import UncertaintyWeightedCascade
from core.adaptive_thresholds import AdaptiveThresholdRegistry

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

