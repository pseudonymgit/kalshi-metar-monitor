#!/usr/bin/env python3
"""
sweep_validation
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
    compute_metrics, simulate_trade, calibrate_confidence,
    safe_signal_evaluate, kalshi_fee, compute_correlation_matrix, compute_differential_results
)
from core.trajectory_confirmation_gate import TrajectoryConfirmationGate
from core.trajectory_lane import TrajectoryLane
from core.settlement_execution_gate import SettlementExecutionGate
from core.liquidity_gate import LiquidityGate
from core.production_gate import ProductionGate, LossLimiter
from core.station_skill_gate import StationSkillGate
from core.agreement_gate import AgreementGate
from core.signal_fusion import UncertaintyWeightedCascade
from core.adaptive_thresholds import AdaptiveThresholdRegistry
from core.db_utils import query_db, with_db

# Need imports from sweep_engine for some functions
from scripts.sweep_engine import (
    DataCache, STATIONS, get_trajectory_gate, get_trajectory_lane,
    get_settlement_gate, _get_loss_limiter, _record_trade_outcome,
    get_agreement_gate, get_adaptive_registry, get_spatial_coherence,
    get_station_skill_gate_enabled, get_calibration_mode, get_platt_pipeline,
    get_bias_corrections, MIN_TRADES_REPORT
)

def evaluate_signal_on_station(signal_name, signal_obj, station, config, settlements):
    if signal_obj is None:
        return []
    days = DataCache.get_metar_data(station)
    if len(days) < 5:
        return []
    station_s = settlements.get(station, {})
    if not station_s:
        return []
    min_lb = 2
    if hasattr(signal_obj, 'min_lookback') and signal_obj.min_lookback is not None:
        try:
            min_lb = max(1, int(signal_obj.min_lookback))
        except (ValueError, TypeError):
            pass
    trades = []
    for idx in range(min_lb, len(days)):
        date_str = days[idx]['date']
        if date_str not in station_s:
            continue
        # Add station context for signals that need it (e.g., 82-member ensemble)
        days[idx]['station'] = station
        actual_temp = station_s[date_str]
        prev_date = days[idx-1]['date']
        if prev_date not in station_s:
            continue
        prev_temp = station_s[prev_date]
        diff = actual_temp - prev_temp
        if diff == 0:
            continue
        actual_dir = 1 if diff > 0 else -1
        pd, cf = safe_signal_evaluate(signal_obj, idx, days)
        if pd is None:
            continue
        
        # Calibrate confidence using Platt calibrator
        cal_result = calibrate_confidence(station, pd, signal_name, cf, date_str)
        if cal_result is None:
            continue  # skip this trade — calibrator says P(correct) < 50%
        cal_conf, raw_conf = cal_result
        
        # Compute dual brier contributions
        outcome = 1.0 if pd == actual_dir else 0.0
        brier_raw = (raw_conf - outcome) ** 2
        brier_cal = (cal_conf - outcome) ** 2
        
        # Apply Trajectory Confirmation Gate (if enabled)
        mod_conf = cal_conf
        traj_verdict = "NEUTRAL"
        if _TRAJECTORY_GATE_ENABLED and _TRAJECTORY_GATE is not None:
            try:
                traj_verdict, mod_conf = evaluate_gate_for_station_date(
                    station, date_str, pd, cal_conf, _TRAJECTORY_GATE)[:2]
            except Exception:
                mod_conf = cal_conf
                traj_verdict = "NEUTRAL"

        # Apply Trajectory Lane modulation (heavy informant — if enabled)
        lane_dir = None
        lane_prob = 0.5
        lane_weight = 0.0
        lane_quality = 0.0
        lane_divergence = False
        if _TRAJECTORY_LANE_ENABLED and _TRAJECTORY_LANE is not None:
            try:
                lane_result = evaluate_lane_for_station_date(
                    station, date_str, cal_conf, pd, _TRAJECTORY_LANE
                )
                lane_dir, lane_prob, lane_weight, lane_quality, lane_divergence = lane_result
                mod_conf = apply_trajectory_lane_to_probability(
                    mod_conf, lane_dir, lane_prob, lane_weight
                )
                mod_conf = max(0.501, min(0.999, mod_conf))
            except Exception:
                pass

        # Apply Station Skill Gate (if enabled)
        if _STATION_SKILL_GATE_ENABLED and _STATION_SKILL_GATE is not None:
            market_type = DataCache.get_market_type(station, date_str)
            bss_matrix = _STATION_SKILL_GATE.get_bss_matrix()
            bss_val = bss_matrix.get(station, {}).get(market_type, -999.0)
            if bss_val <= _STATION_SKILL_GATE_BSS_THRESHOLD:
                logger.debug(f"Station Skill Gate: BLOCKED {station} {market_type} (BSS={bss_val:.4f} <= {_STATION_SKILL_GATE_BSS_THRESHOLD})")
                continue
        trade = simulate_trade(pd, mod_conf, actual_dir, config, station, date_str, signal_name, loss_limiter=_get_loss_limiter(signal_name, station))
        if trade:
            trade["raw_confidence"] = raw_conf
            trade["calibrated_confidence"] = cal_conf
            trade["trajectory_verdict"] = traj_verdict
            trade["trajectory_modulated_confidence"] = mod_conf
            trade["raw_brier_contrib"] = brier_raw
            trade["calibrated_brier_contrib"] = brier_cal
            trade["trajectory_lane_dir"] = lane_dir
            trade["trajectory_lane_prob"] = lane_prob
            trade["trajectory_lane_weight"] = lane_weight
            trade["trajectory_lane_quality"] = lane_quality
            trade["trajectory_lane_divergence"] = lane_divergence
            trades.append(trade)
            __record_trade_outcome(signal_name, station, trade)

        # Record outcome for adaptive threshold learning
        if _ADAPTIVE_THRESHOLDS_ENABLED and _ADAPTIVE_REGISTRY is not None:
            was_correct = pd == actual_dir
            _ADAPTIVE_REGISTRY.record_outcome(signal_name, station, cal_conf, was_correct)

    return trades
def split_discovery_holdout(settlements, holdout_frac=0.7):
    all_dates = sorted(set(d for sd in settlements.values() for d in sd))
    if not all_dates:
        return {}, {}
    si = max(1, int(len(all_dates) * holdout_frac))
    disc_dates = set(all_dates[:si])
    hold_dates = set(all_dates[si:])
    discovery = {}
    holdout = {}
    for station, date_temps in settlements.items():
        d = {k: v for k, v in date_temps.items() if k in disc_dates}
        h = {k: v for k, v in date_temps.items() if k in hold_dates}
        if d:
            discovery[station] = d
        if h:
            holdout[station] = h
    return discovery, holdout
def split_geo_holdout(settlements, holdout_stations=None, seed=42):
    if holdout_stations is None:
        rng = random.Random(seed)
        all_st = sorted(settlements.keys())
        rng.shuffle(all_st)
        n_h = max(1, len(all_st) // 4)
        holdout_stations = all_st[-n_h:]
    discovery = {}
    holdout = {}
    for st, date_temps in settlements.items():
        if st in holdout_stations:
            holdout[st] = date_temps
        else:
            discovery[st] = date_temps
    return discovery, holdout

def _eval_wrapper(signal_obj, signal_name, settlements_subset, config):
    all_trades = []
    for station in STATIONS:
        if station not in settlements_subset:
            continue
        days = DataCache.get_metar_data(station)
        if len(days) < 5:
            continue
        station_s = settlements_subset[station]
        min_lb = 2
        if hasattr(signal_obj, 'min_lookback') and signal_obj.min_lookback is not None:
            try:
                min_lb = max(1, int(signal_obj.min_lookback))
            except (ValueError, TypeError):
                pass
        for idx in range(min_lb, len(days)):
            date_str = days[idx]['date']
            if date_str not in station_s:
                continue
            actual_temp = station_s[date_str]
            prev_date = days[idx-1]['date']
            if prev_date not in station_s:
                continue
            diff = actual_temp - station_s[prev_date]
            if diff == 0:
                continue
            actual_dir = 1 if diff > 0 else -1
            pd, cf = safe_signal_evaluate(signal_obj, idx, days)
            if pd is None:
                continue
            # Calibrate confidence using Platt calibrator
            cal_result = calibrate_confidence(station, pd, signal_name, cf, date_str)
            if cal_result is None:
                continue  # skip this trade — calibrator says P(correct) < 50%
            cal_conf, raw_conf = cal_result
            
            # Compute dual brier contributions
            outcome = 1.0 if pd == actual_dir else 0.0
            brier_raw = (raw_conf - outcome) ** 2
            brier_cal = (cal_conf - outcome) ** 2
            
            # Apply Trajectory Confirmation Gate (if enabled)
            mod_conf = cal_conf
            traj_verdict = "NEUTRAL"
            if _TRAJECTORY_GATE_ENABLED and _TRAJECTORY_GATE is not None:
                try:
                    traj_verdict, mod_conf = evaluate_gate_for_station_date(
                        station, date_str, pd, cal_conf, _TRAJECTORY_GATE)[:2]
                except Exception:
                    mod_conf = cal_conf
                    traj_verdict = "NEUTRAL"

            # Apply Trajectory Lane modulation (heavy informant — if enabled)
            lane_dir = None
            lane_prob = 0.5
            lane_weight = 0.0
            lane_quality = 0.0
            lane_divergence = False
            if _TRAJECTORY_LANE_ENABLED and _TRAJECTORY_LANE is not None:
                try:
                    lane_result = evaluate_lane_for_station_date(
                        station, date_str, cal_conf, pd, _TRAJECTORY_LANE
                    )
                    lane_dir, lane_prob, lane_weight, lane_quality, lane_divergence = lane_result
                    mod_conf = apply_trajectory_lane_to_probability(
                        mod_conf, lane_dir, lane_prob, lane_weight
                    )
                    mod_conf = max(0.501, min(0.999, mod_conf))
                except Exception:
                    pass

            # Apply Station Skill Gate (if enabled)
            if _STATION_SKILL_GATE_ENABLED and _STATION_SKILL_GATE is not None:
                market_type = DataCache.get_market_type(station, date_str)
                bss_matrix = _STATION_SKILL_GATE.get_bss_matrix()
                bss_val = bss_matrix.get(station, {}).get(market_type, -999.0)
                if bss_val <= _STATION_SKILL_GATE_BSS_THRESHOLD:
                    logger.debug(f"Station Skill Gate: BLOCKED {station} {market_type} (BSS={bss_val:.4f} <= {_STATION_SKILL_GATE_BSS_THRESHOLD})")
                    continue
            trade = simulate_trade(pd, mod_conf, actual_dir, config, station, date_str, signal_name, loss_limiter=_get_loss_limiter(signal_name, station))
            if trade:
                trade["raw_confidence"] = raw_conf
                trade["calibrated_confidence"] = cal_conf
                trade["trajectory_verdict"] = traj_verdict
                trade["trajectory_modulated_confidence"] = mod_conf
                trade["raw_brier_contrib"] = brier_raw
                trade["calibrated_brier_contrib"] = brier_cal
                trade["trajectory_lane_dir"] = lane_dir
                trade["trajectory_lane_prob"] = lane_prob
                trade["trajectory_lane_weight"] = lane_weight
                trade["trajectory_lane_quality"] = lane_quality
                trade["trajectory_lane_divergence"] = lane_divergence
                all_trades.append(trade)
                __record_trade_outcome(signal_name, station, trade)

            # Record outcome for adaptive threshold learning
            if _ADAPTIVE_THRESHOLDS_ENABLED and _ADAPTIVE_REGISTRY is not None:
                was_correct = pd == actual_dir
                _ADAPTIVE_REGISTRY.record_outcome(signal_name, station, cal_conf, was_correct)

    return all_trades
def run_three_stage_validation(signal_obj, signal_name, config, settlements_all):
    discovery, time_holdout = split_discovery_holdout(
        settlements_all, holdout_frac=config.get("holdout_start_frac", 0.7))
    disc_trades = _eval_wrapper(signal_obj, signal_name, discovery, config)
    time_trades = _eval_wrapper(signal_obj, signal_name, time_holdout, config)
    _, geo_holdout = split_geo_holdout(settlements_all)
    geo_trades = _eval_wrapper(signal_obj, signal_name, geo_holdout, config)
    return {
        "discovery": compute_metrics(disc_trades, min_trades=3),
        "time_holdout": compute_metrics(time_trades, min_trades=3),
        "geo_holdout": compute_metrics(geo_trades, min_trades=3),
        "consolidated": compute_metrics(disc_trades + time_trades + geo_trades,
                                         min_trades=MIN_TRADES_REPORT)}
def evaluate_signal_all_stations(signal_name, signal_obj, config, settlements):
    all_trades = []
    per_station = defaultdict(list)
    per_market = defaultdict(list)
    for station in STATIONS:
        st = evaluate_signal_on_station(signal_name, signal_obj, station, config, settlements)
        all_trades.extend(st)
        if st:
            per_station[station] = st
            for t in st:
                per_market[t.get("market_type", "HIGH")].append(t)
    station_metrics = {s: compute_metrics(t, 3) for s, t in per_station.items()
                       if compute_metrics(t, 3)["n_trades"] > 0}
    market_metrics = {m: compute_metrics(t, 3) for m, t in per_market.items()
                      if compute_metrics(t, 3)["n_trades"] > 0}

    # Apply spatial coherence modulation (if enabled)
    if _SPATIAL_COHERENCE_ENABLED:
        all_trades = apply_spatial_coherence_to_trades(all_trades)
        # Recompute per-station and per-market metrics with adjusted trades
        per_station_adj = defaultdict(list)
        per_market_adj = defaultdict(list)
        for t in all_trades:
            per_station_adj[t['station']].append(t)
            per_market_adj[t.get('market_type', 'HIGH')].append(t)
        station_metrics = {s: compute_metrics(t, 3) for s, t in per_station_adj.items()
                           if compute_metrics(t, 3)["n_trades"] > 0}
        market_metrics = {m: compute_metrics(t, 3) for m, t in per_market_adj.items()
                          if compute_metrics(t, 3)["n_trades"] > 0}

    # Apply Agreement Gate filtering (if enabled)
    if _AGREEMENT_GATE_ENABLED:
        all_trades = apply_agreement_gate_to_trades(all_trades, signal_name)
        # Recompute per-station and per-market metrics with filtered trades
        per_station_adj = defaultdict(list)
        per_market_adj = defaultdict(list)
        for t in all_trades:
            per_station_adj[t['station']].append(t)
            per_market_adj[t.get('market_type', 'HIGH')].append(t)
        station_metrics = {s: compute_metrics(t, 3) for s, t in per_station_adj.items()
                           if compute_metrics(t, 3)["n_trades"] > 0}
        market_metrics = {m: compute_metrics(t, 3) for m, t in per_market_adj.items()
                          if compute_metrics(t, 3)["n_trades"] > 0}

    aggregate = compute_metrics(all_trades, MIN_TRADES_REPORT)
    val = run_three_stage_validation(signal_obj, signal_name, config, settlements)
    return {"signal_name": signal_name, "config_id": config.get("config_id", -1),
            "aggregate": aggregate, "per_station": station_metrics,
            "per_market": market_metrics, "validation": val,
            "n_stations_active": len(station_metrics), "total_trades": len(all_trades)}
def apply_agreement_gate_to_trades(all_trades, signal_name):
    """
    Apply N-of-M agreement filtering to collected trades for a single signal.

    Builds direction-vote tuples from all trades produced by this signal,
    groups them by (station, market_type), and filters out trades for
    (station, market_type) combos where fewer than N of M signals agree.

    Returns:
        Filtered list of trades where direction consensus was met.
    """
    if not all_trades or not _AGREEMENT_GATE_ENABLED:
        return all_trades

    gate = _AGREEMENT_GATE
    if gate is None:
        return all_trades

    # Build signal tuples: (station, market_type, direction, reason)
    signals = []
    for t in all_trades:
        direction = 'UP' if t['predicted'] == 1 else 'DOWN'
        market_type = t.get('market_type', 'HIGH')
        signals.append((t['station'], market_type, direction, signal_name))

    if len(signals) < _AGREEMENT_M:
        # Not enough signals to form proper consensus — leave intact
        return all_trades

    filtered = gate.filter_signals(signals)

    # Build set of (station, market_type, direction) combos that passed
    approved = set()
    for station, market_type, direction, _ in filtered:
        approved.add((station, market_type, direction))

    # Keep only trades matching approved combos
    result = []
    for t in all_trades:
        direction = 'UP' if t['predicted'] == 1 else 'DOWN'
        market_type = t.get('market_type', 'HIGH')
        if (t['station'], market_type, direction) in approved:
            result.append(t)

    return result
def apply_spatial_coherence_to_trades(all_trades):
    """
    Apply spatial coherence modulation to collected trades.

    Builds a cross-station signal map per date, then modulates confidence
    using SpatialCoherenceGate.modulate_confidence().

    Returns:
        Modified list of trade dicts with adjusted confidence.
    """
    if not all_trades:
        return all_trades
    if not _SPATIAL_COHERENCE_ENABLED:
        return all_trades

    gate = _SPATIAL_COHERENCE_GATE
    if gate is None:
        return all_trades

    # Build date -> {station: signal_info} map
    date_signals: Dict[str, Dict[str, dict]] = {}
    for t in all_trades:
        d = t['date']
        s = t['station']
        if d not in date_signals:
            date_signals[d] = {}
        date_signals[d][s] = {
            'direction': 'up' if t['predicted'] == 1 else 'down',
            'confidence': t['confidence'],
            'anomaly': t.get('anomaly', 1.0 if t['predicted'] == 1 else -1.0),
            'signal_name': t.get('signal_name', 'unknown'),
        }

    adjusted = []
    global _SPATIAL_COHERENCE_TRACKER
    _SPATIAL_COHERENCE_TRACKER = {}

    for t in all_trades:
        d = t['date']
        s = t['station']
        direction = 'up' if t['predicted'] == 1 else 'down'
        nearby = {st: info for st, info in date_signals.get(d, {}).items()
                  if st != s}

        old_conf = t['confidence']
        new_conf = gate.modulate_confidence(
            station=s,
            signal_name=t.get('signal_name', 'unknown'),
            direction=direction,
            confidence=old_conf,
            nearby_signals=nearby,
            date_str=d,
        )

        # Track spatial coherence effect
        _SPATIAL_COHERENCE_TRACKER[s] = {
            'before': old_conf,
            'after': new_conf,
            'delta': new_conf - old_conf,
            'applied': True,
        }

        # Build minimal config from trade metadata
        cfg = {
            'fee_type': t.get('fee_type', 2),
            'edge_threshold': t.get('edge_threshold', 0.02),
            'kelly_fraction': t.get('kelly_fraction', 0.5),
            'entry_price_min': t.get('entry_price_min', 0.01),
            'entry_price_max': t.get('entry_price_max', 0.95),
            'max_contracts': t.get('max_contracts', 100),
            'slippage_budget': t.get('slippage_budget', 0.005),
            'fee_deduction': t.get('fee_deduction', 1.0),
            'position_sizing_model': t.get('position_sizing_model', 1),
            'confidence_floor': t.get('confidence_floor', 0.5),
        }

        adj_trade = simulate_trade(
            t['predicted'], new_conf,
            t['actual'], cfg, s, d,
            signal_name=t.get('signal_name', '')
        )
        if adj_trade is None:
            continue  # filtered by spatial coherence

        # Preserve metadata from original trade
        adj_trade['raw_confidence'] = t.get('raw_confidence', old_conf)
        adj_trade['calibrated_confidence'] = t.get('calibrated_confidence', old_conf)
        adj_trade['raw_brier_contrib'] = t.get('raw_brier_contrib', 0.0)
        adj_trade['calibrated_brier_contrib'] = t.get('calibrated_brier_contrib', 0.0)
        adj_trade['spatial_modulation'] = new_conf - old_conf
        adj_trade['spatial_old_conf'] = old_conf
        adj_trade['spatial_new_conf'] = new_conf
        adj_trade['signal_name'] = t.get('signal_name', 'unknown')
        adj_trade['fee_type'] = t.get('fee_type', 2)
        adj_trade['edge_threshold'] = t.get('edge_threshold', 0.02)
        adj_trade['confidence_floor'] = t.get('confidence_floor', 0.5)

        adjusted.append(adj_trade)

    n_filtered = len(all_trades) - len(adjusted)
    if n_filtered > 0:
        logger.info(
            "Spatial coherence filtered %d trades", n_filtered
        )

    return adjusted
def run_best_config_for_signal(signal_name, signal_obj, configs, settlements):
    """
    Evaluate all configs for a signal, with early stopping on convergence.

    Processes configs in batches of 500. After each batch, checks if the
    rolling accuracy, Sharpe, and top-config selection have converged.
    If all 3 conditions are met, stops early and returns the best config so far.
    """
    best_pnl = -1e9
    best_metrics = None
    convergence_log = []
    batch_size = 500
    acc_window = []
    sharpe_window = []

    for i, cfg in enumerate(configs):
        result = evaluate_signal_all_stations(signal_name, signal_obj, cfg, settlements)
        agg = result.get("aggregate", {})
        pnl = agg.get("total_pnl", -1e9)
        if pnl > best_pnl and agg.get("n_trades", 0) >= MIN_TRADES_REPORT:
            best_pnl = pnl
            best_metrics = result

        # Early stopping check every batch_size configs
        if (i + 1) % batch_size == 0:
            acc = agg.get("accuracy", 0)
            sharpe = agg.get("sharpe", 0)
            acc_window.append(acc)
            sharpe_window.append(sharpe)
            if len(acc_window) > 3:
                acc_window.pop(0)
            if len(sharpe_window) > 3:
                sharpe_window.pop(0)

            # Convergence conditions:
            # 1. Accuracy stable within ±0.5pp over last 3 batches
            # 2. Sharpe stable within ±0.1 over last 3 batches
            acc_converged = len(acc_window) >= 3 and (max(acc_window) - min(acc_window)) < 0.005
            sharpe_converged = len(sharpe_window) >= 3 and (max(sharpe_window) - min(sharpe_window)) < 0.1

            convergence_log.append({
                "batch": (i + 1) // batch_size,
                "configs_evaluated": i + 1,
                "best_pnl": best_pnl,
                "best_accuracy": best_metrics.get("aggregate", {}).get("accuracy", 0) if best_metrics else 0,
                "current_accuracy": acc,
                "current_sharpe": sharpe,
                "acc_converged": acc_converged,
                "sharpe_converged": sharpe_converged,
            })

            if acc_converged and sharpe_converged:
                print(f"    Convergence at batch {(i + 1) // batch_size} ({i + 1}/{len(configs)} configs) — acc window: {[f'{a:.3f}' for a in acc_window]}, sharpe window: {[f'{s:.2f}' for s in sharpe_window]}")
                break

    if best_metrics is None:
        best_metrics = evaluate_signal_all_stations(signal_name, signal_obj, configs[0], settlements)

    # Log convergence data
    import json
    import os
    conv_path = os.path.join(str(REPO_ROOT), "data", "sweep_convergence_log.csv")
    try:
        with open(conv_path, "a") as f:
            for entry in convergence_log:
                f.write(f"{signal_name},{entry['batch']},{entry['configs_evaluated']},{entry['best_pnl']:.2f},{entry['best_accuracy']:.4f},{entry['current_accuracy']:.4f},{entry['current_sharpe']:.4f},{entry['acc_converged']},{entry['sharpe_converged']}\n")
    except Exception:
        pass

    return best_metrics
