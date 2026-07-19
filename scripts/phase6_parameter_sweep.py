#!/usr/bin/env python3
"""
PHASE 6 — TASK 6.5: Two-Stage Parameter Sweep

Stage 1: Global sweep on 50% of data (random split)
Stage 2: Per-city refinement on held-out 50%
Nested validation: final test on data not used in either sweep

Parameters to sweep:
  - calendar_climatology window: [30, 45, 60, 90, 120]
  - gaussian window: [30, 48, 60, 90]
  - gaussian z-score threshold: [0.5, 1.0, 1.5, 2.0]
  - pressure_delta window: [2, 3, 4, 5]
  - pressure_delta threshold: [2.0, 3.0, 4.0]
  - wind_direction_shift threshold: [30°, 45°, 60°]
  - regime volatility window: [10, 15, 20, 30]
  - regime slope window: [10, 15, 20, 30]
  - forecast_disagreement window: [5, 7, 10, 14]

Usage:
    python3 scripts/phase6_parameter_sweep.py [--db-path ...] [--output ...]
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
import random
from datetime import datetime
from collections import defaultdict
from itertools import product
from typing import Optional, Tuple, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import SignalRegistry
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']


def load_station_days(station, conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                      'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    return days


def load_market(conn, station, market_type='HIGH'):
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type=?
        ORDER BY local_trading_date ASC
    """, (station, market_type))
    rows = cur.fetchall()
    market = {}
    prev = None
    for date_str, bucket in rows:
        if bucket is None: market[date_str] = 'flat'; prev = bucket; continue
        if prev is not None:
            market[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
        else:
            market[date_str] = 'flat'
        prev = bucket
    return market


def align(days, market):
    aligned = []
    for d in days:
        dk = d['date'][:10]
        if dk in market:
            e = dict(d); e['market_dir'] = market[dk]; aligned.append(e)
    return aligned


# ─── Signal evaluation with parameter overrides ─────────────────────────────

def evaluate_override(name, idx, days, params):
    """Evaluate a signal with parameter overrides."""
    if name == 'calendar_climatology':
        return eval_calendar_climatology(idx, days, params.get('calendar_climatology_window', 60))
    elif name == 'gaussian':
        return eval_gaussian(idx, days,
                              params.get('gaussian_window', 48),
                              params.get('gaussian_z_threshold', 1.0))
    elif name == 'goldilocks':
        return eval_goldilocks(idx, days)
    elif name == 'pressure_delta':
        return eval_pressure_delta(idx, days,
                                    params.get('pressure_delta_window', 3),
                                    params.get('pressure_delta_threshold', 3.0))
    elif name == 'wind_direction_shift':
        return eval_wind_shift(idx, days, params.get('wind_direction_shift_threshold', 45.0))
    elif name == 'regime':
        return eval_regime(idx, days,
                            params.get('regime_vol_window', 15),
                            params.get('regime_slope_window', 15))
    elif name == 'forecast_disagreement':
        return eval_forecast_disagreement(idx, days, params.get('forecast_disagreement_window', 7))
    return None, 0.0


def _window(days, idx, n, offset=1):
    start = idx - n - offset
    end = idx - offset
    if start < 0: return None
    return days[start:end]


def _safe_get(days, idx, field, default=None):
    if idx < 0 or idx >= len(days): return default
    val = days[idx].get(field)
    return val if val is not None else default


def eval_calendar_climatology(idx, days, window=60, z_thresh=1.5):
    if idx < window + 1: return None, 0.0
    w = _window(days, idx, window, offset=1)
    if w is None: return None, 0.0
    highs = [d.get('high') for d in w if d.get('high') is not None]
    if len(highs) < 2: return None, 0.0
    mean = sum(highs) / len(highs)
    var = sum((h - mean) ** 2 for h in highs) / (len(highs) - 1) if len(highs) > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    current = _safe_get(days, idx - 1, 'high')
    if current is None: return None, 0.0
    z = (current - mean) / std
    if z > z_thresh: return 'down', min(abs(z) / 3.0, 0.6)
    elif z < -z_thresh: return 'up', min(abs(z) / 3.0, 0.6)
    return None, 0.0


def eval_gaussian(idx, days, window=48, z_thresh=1.0):
    if idx < window + 1: return None, 0.0
    w = _window(days, idx, window, offset=1)
    if w is None: return None, 0.0
    highs = [d.get('high') for d in w if d.get('high') is not None]
    if len(highs) < window: return None, 0.0
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    current = _safe_get(days, idx - 1, 'high')
    if current is None: return None, 0.0
    z = (current - mean) / std
    if z > z_thresh: return 'down', abs(z)
    elif z < -z_thresh: return 'up', abs(z)
    return None, 0.0


def eval_goldilocks(idx, days):
    if idx < 1: return None, 0.0
    today = _safe_get(days, idx, 'high')
    yesterday = _safe_get(days, idx - 1, 'high')
    if today is None or yesterday is None: return None, 0.0
    tc = today - yesterday
    if abs(tc) < 2.0: return None, 0.0
    if tc > 0:
        return 'down', min(0.40 + min(tc / 5.0, 1.0) * 0.15, 1.0)
    else:
        return 'up', min((0.25 + min(abs(tc) / 5.0, 1.0) * 0.10) * 0.85, 1.0)


def eval_pressure_delta(idx, days, window=3, threshold=3.0):
    if idx < window + 1: return None, 0.0
    recent = [_safe_get(days, i, 'pressure') for i in range(idx - window, idx)]
    recent = [p for p in recent if p is not None]
    if len(recent) < 2: return None, 0.0
    baseline = recent[0]
    avg = sum(recent) / len(recent)
    dp = avg - baseline
    if abs(dp) < threshold: return None, 0.0
    direction = 'up' if dp > 0 else 'down'
    return direction, min(abs(dp) / 5.0, 0.8)


def eval_wind_shift(idx, days, threshold=45.0):
    if idx < 4: return None, 0.0
    wind_data = []
    for i in range(idx, max(idx - 4, -1), -1):
        if i < 0: break
        d = days[i]
        if d.get('wind_dir') is not None and d.get('wind_speed') is not None:
            wind_data.append((d['date'], float(d['wind_dir']), float(d['wind_speed'])))
    if len(wind_data) < 2: return None, 0.0
    old_dir, new_dir = wind_data[-1][1], wind_data[0][1]
    diff = min(abs(old_dir - new_dir), 360 - abs(old_dir - new_dir))
    if diff < threshold: return None, 0.0
    avg_speed = sum(d[2] for d in wind_data) / len(wind_data)
    if avg_speed < 10.0: return None, 0.0
    if 0 <= new_dir < 45 or 315 <= new_dir <= 360: direction = 'down'
    elif 135 <= new_dir <= 225: direction = 'up'
    else: direction = 'down' if new_dir > 180 else 'up'
    conf = min(0.45 + min(diff / 90.0, 0.25) + min((avg_speed - 10) / 20, 0.2), 0.85)
    return direction, conf


def eval_regime(idx, days, vol_window=15, slope_window=15):
    lookback = max(vol_window, slope_window) + 1
    if idx < lookback: return None, 0.0
    vol_w = _window(days, idx, vol_window, offset=1)
    if vol_w is None: return None, 0.0
    vh = [d.get('high') for d in vol_w if d.get('high') is not None]
    if len(vh) < 2: return None, 0.0
    vm = sum(vh) / len(vh)
    vv = sum((h - vm) ** 2 for h in vh) / (len(vh) - 1) if len(vh) > 1 else 0.01
    vol = math.sqrt(vv) if vv > 0 else 0.01
    slope = (vh[-1] - vh[0]) / len(vh) if len(vh) >= 2 else 0
    if vol >= 1.0 or abs(slope) >= 0.5: return None, 0.0
    mw = _window(days, idx, 30, offset=1)
    if mw is None: return None, 0.0
    mh = [d.get('high') for d in mw if d.get('high') is not None]
    if not mh: return None, 0.0
    m30 = sum(mh) / len(mh)
    cur = _safe_get(days, idx - 1, 'high')
    if cur is None: return None, 0.0
    dist = cur - m30
    if dist > 1.0: return 'down', min(max(dist / 3.0, 0.1), 0.8)
    elif dist < -1.0: return 'up', min(max(abs(dist) / 3.0, 0.1), 0.8)
    return None, 0.0


def eval_forecast_disagreement(idx, days, window=7, thresh=5.0):
    if idx < window + 1: return None, 0.0
    yd = _safe_get(days, idx - 1, 'high')
    if yd is None: return None, 0.0
    w = _window(days, idx, window, offset=1)
    if w is None: return None, 0.0
    wh = [d.get('high') for d in w if d.get('high') is not None]
    if len(wh) < 3: return None, 0.0
    wm = sum(wh) / len(wh)
    d = yd - wm
    if abs(d) < thresh: return None, 0.0
    direction = 'down' if d > 0 else 'up'
    conf = 1.0 / (1.0 + math.exp(-(abs(d) - thresh) / 3.0))
    return direction, conf


# ─── Backtest engine ────────────────────────────────────────────────────────

def evaluate_ensemble_with_params(signal_names, idx, days, params, min_agree=1):
    votes = []
    for name in signal_names:
        d, c = evaluate_override(name, idx, days, params)
        if d and c > 0:
            votes.append((d, c))
    if len(votes) < min_agree:
        return None, 0.0
    up = sum(c for d, c in votes if d == 'up')
    dn = sum(c for d, c in votes if d == 'down')
    if up > dn: return 'up', up / (up + dn)
    elif dn > up: return 'down', dn / (up + dn)
    return None, 0.0


def backtest_params(signal_names, days, params, train_days=180, test_days=30,
                    max_windows=6, min_agree=1):
    """Run walk-forward backtest with given parameters."""
    correct = 0
    total = 0
    trades = 0
    
    start_idx = train_days
    windows_run = 0
    
    while start_idx + test_days <= len(days) and windows_run < max_windows:
        test_end = min(start_idx + test_days, len(days))
        
        for idx in range(start_idx, test_end):
            if idx >= len(days):
                break
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue
            
            pred_dir, conf = evaluate_ensemble_with_params(signal_names, idx, days, params, min_agree)
            if pred_dir is None:
                continue
            
            trades += 1
            total += 1
            if pred_dir == actual:
                correct += 1
        
        start_idx += test_days
        windows_run += 1
    
    return {'correct': correct, 'total': total, 'trades': trades}


# ─── Parameter spaces ───────────────────────────────────────────────────────

PARAM_SPACE = {
    'calendar_climatology_window': [30, 45, 60, 90, 120],
    'gaussian_window': [30, 48, 60, 90],
    'gaussian_z_threshold': [0.5, 1.0, 1.5, 2.0],
    'pressure_delta_window': [2, 3, 4, 5],
    'pressure_delta_threshold': [2.0, 3.0, 4.0],
    'wind_direction_shift_threshold': [30.0, 45.0, 60.0],
    'regime_vol_window': [10, 15, 20, 30],
    'regime_slope_window': [10, 15, 20, 30],
    'forecast_disagreement_window': [5, 7, 10, 14],
}


def get_default_params():
    return {
        'calendar_climatology_window': 60,
        'gaussian_window': 48,
        'gaussian_z_threshold': 1.0,
        'pressure_delta_window': 3,
        'pressure_delta_threshold': 3.0,
        'wind_direction_shift_threshold': 45.0,
        'regime_vol_window': 15,
        'regime_slope_window': 15,
        'forecast_disagreement_window': 7,
    }


def main():
    parser = argparse.ArgumentParser(description='Phase 6 Parameter Sweep')
    parser.add_argument('--db-path', default='data/metar_backfill.db')
    parser.add_argument('--output', default='data/phase6_parameter_sweep.json')
    parser.add_argument('--station', default=None)
    parser.add_argument('--quick', action='store_true',
                        help='Quick mode: skip per-city refinement, single station')
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        logger.error(f"DB not found: {args.db_path}")
        sys.exit(1)

    # Load data
    conn = sqlite3.connect(args.db_path)
    stations = [args.station.upper()] if args.station else ALL_STATIONS
    
    all_days = {}
    for station in stations:
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align(days, market)
        if len(aligned) >= 300:
            all_days[station] = aligned
    conn.close()
    logger.info(f"Loaded {len(all_days)} stations (min 300 days)")

    # Determine best combo from prior task (for sweep, use the best 3-signal combo)
    # Default to gaussian+pressure_delta+forecast_disagreement (strong Sharpe performer)
    signal_names = ['gaussian', 'pressure_delta', 'forecast_disagreement']
    
    # ── Stage 0: Random split data ──
    # For each station, split data into 3 parts:
    #   S1: 50% — Stage 1 global sweep
    #   S2: 25% — Stage 2 per-city refinement
    #   S3: 25% — Nested validation (final test)
    
    stage1_data = {}
    stage2_data = {}
    stage3_data = {}
    
    for station, days in all_days.items():
        n = len(days)
        s1_end = int(n * 0.50)
        s2_end = int(n * 0.75)
        stage1_data[station] = days[:s1_end]
        stage2_data[station] = days[s1_end:s2_end]
        stage3_data[station] = days[s2_end:]
    
    logger.info(f"Stage 1 (global sweep): {sum(len(d) for d in stage1_data.values())} total days")
    logger.info(f"Stage 2 (per-city):     {sum(len(d) for d in stage2_data.values())} total days")
    logger.info(f"Stage 3 (validation):   {sum(len(d) for d in stage3_data.values())} total days")

    # ── Stage 1: Global sweep ──
    logger.info(f"\n{'='*70}")
    logger.info(f"STAGE 1: GLOBAL PARAMETER SWEEP")
    logger.info(f"{'='*70}")
    
    # For each parameter, sweep one at a time (keep others at default)
    best_params = get_default_params()
    stage1_results = {}
    
    for param_name, param_values in PARAM_SPACE.items():
        logger.info(f"\nSweeping: {param_name}")
        param_results = []
        
        for value in param_values:
            params = dict(best_params)
            params[param_name] = value
            
            # Evaluate on all stations in stage 1
            agg = {'correct': 0, 'total': 0, 'trades': 0}
            stations_used = 0
            
            for station, days in stage1_data.items():
                if len(days) < 250:
                    continue
                res = backtest_params(signal_names, days, params, train_days=180, test_days=30)
                if res['trades'] > 0:
                    agg['correct'] += res['correct']
                    agg['total'] += res['total']
                    agg['trades'] += res['trades']
                    stations_used += 1
            
            acc = agg['correct'] / agg['total'] if agg['total'] > 0 else 0
            param_results.append({
                'value': value,
                'accuracy': round(acc, 4),
                'trades': agg['trades'],
                'stations': stations_used,
            })
            logger.info(f"  {value:>5}: acc={acc:.3f}  trades={agg['trades']}")
        
        # Pick best value
        best_val = max(param_results, key=lambda x: x['accuracy'])
        best_params[param_name] = best_val['value']
        stage1_results[param_name] = {
            'best_value': best_val['value'],
            'best_accuracy': best_val['accuracy'],
            'all_results': param_results,
        }
        logger.info(f"  → Best: {best_val['value']} (acc={best_val['accuracy']:.3f})")
    
    logger.info(f"\nStage 1 best global params: {best_params}")

    # ── Stage 2: Per-city refinement ──
    logger.info(f"\n{'='*70}")
    logger.info(f"STAGE 2: PER-CITY REFINEMENT")
    logger.info(f"{'='*70}")
    
    per_city_params = {}
    
    if not args.quick:
        for station, days in stage2_data.items():
            if len(days) < 120:
                per_city_params[station] = dict(best_params)
                continue
            
            city_params = dict(best_params)
            city_improvements = {}
            
            for param_name in PARAM_SPACE:
                best_city_val = city_params[param_name]
                best_city_acc = 0
                
                for value in PARAM_SPACE[param_name]:
                    params = dict(city_params)
                    params[param_name] = value
                    res = backtest_params(signal_names, [days], params,
                                          train_days=60, test_days=20, max_windows=3)
                    acc = res['correct'] / res['total'] if res['total'] > 0 else 0
                    if acc > best_city_acc and res['trades'] >= 10:
                        best_city_acc = acc
                        best_city_val = value
                
                if best_city_val != city_params[param_name] and best_city_acc > 0:
                    city_improvements[param_name] = {
                        'old': city_params[param_name],
                        'new': best_city_val,
                        'old_acc': best_city_acc,  # approximate
                    }
                    city_params[param_name] = best_city_val
            
            per_city_params[station] = city_params
            n_changes = len(city_improvements)
            if n_changes > 0:
                logger.info(f"  {station}: {n_changes} parameter changes from global optimum")
            else:
                logger.info(f"  {station}: no changes (global optimum holds)")
    else:
        # Quick mode: same params for all cities
        for station in stage2_data:
            per_city_params[station] = dict(best_params)
        logger.info("  Quick mode: using global params for all cities")
    
    # ── Stage 3: Nested validation ──
    logger.info(f"\n{'='*70}")
    logger.info(f"STAGE 3: NESTED VALIDATION (held-out data)")
    logger.info(f"{'='*70}")
    
    # Global params on validation data
    global_val = {'correct': 0, 'total': 0, 'trades': 0}
    # Per-city params on validation data
    refined_val = {'correct': 0, 'total': 0, 'trades': 0}
    
    for station, days in stage3_data.items():
        if len(days) < 60:
            continue
        
        # Global params
        res_g = backtest_params(signal_names, days, best_params, train_days=30, test_days=15, max_windows=4)
        if res_g['trades'] > 0:
            global_val['correct'] += res_g['correct']
            global_val['total'] += res_g['total']
            global_val['trades'] += res_g['trades']
        
        # Per-city params
        city_p = per_city_params.get(station, best_params)
        res_c = backtest_params(signal_names, days, city_p, train_days=30, test_days=15, max_windows=4)
        if res_c['trades'] > 0:
            refined_val['correct'] += res_c['correct']
            refined_val['total'] += res_c['total']
            refined_val['trades'] += res_c['trades']
    
    global_acc = global_val['correct'] / global_val['total'] if global_val['total'] > 0 else 0
    refined_acc = refined_val['correct'] / refined_val['total'] if refined_val['total'] > 0 else 0
    
    logger.info(f"Global params validation: acc={global_acc:.3f} ({global_val['correct']}/{global_val['total']})")
    logger.info(f"Refined params validation: acc={refined_acc:.3f} ({refined_val['correct']}/{refined_val['total']})")
    logger.info(f"Improvement from per-city: {(refined_acc - global_acc) * 100:+.2f}pp")

    # ── Save results ──
    output = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'db_path': args.db_path,
            'stations_tested': list(all_days.keys()),
            'combo_used': signal_names,
            'parameter_space': {k: list(v) for k, v in PARAM_SPACE.items()},
        },
        'stage1_global_sweep': stage1_results,
        'stage1_best_params': best_params,
        'stage2_per_city_params': per_city_params,
        'stage3_validation': {
            'global_params': {
                'params': best_params,
                'accuracy': round(global_acc, 5),
                'correct': global_val['correct'],
                'total': global_val['total'],
                'trades': global_val['trades'],
            },
            'refined_params': {
                'accuracy': round(refined_acc, 5),
                'correct': refined_val['correct'],
                'total': refined_val['total'],
                'trades': refined_val['trades'],
            },
            'improvement_pp': round((refined_acc - global_acc) * 100, 3),
        },
        'final_params': best_params,
        'per_city_params_summary': {
            station: p for station, p in per_city_params.items()
        },
    }
    
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\nSaved to {args.output}")
    
    # Print final parameter summary
    print(f"\n{'='*70}")
    print("PHASE 6 — FINAL OPTIMIZED PARAMETERS")
    print(f"{'='*70}")
    print(f"Combo: {' + '.join(signal_names)}")
    print(f"\nParameter           Default     Optimized")
    print("-"*45)
    defaults = get_default_params()
    for param, val in best_params.items():
        def_val = defaults.get(param, '-')
        marker = '✓' if val == def_val else '→'
        print(f"{param:<25s} {str(def_val):>8} {marker} {str(val):>8}")
    
    print(f"\nValidation Results:")
    print(f"  Global params accuracy: {global_acc*100:.2f}% ({global_val['correct']}/{global_val['total']})")
    print(f"  Refined params accuracy: {refined_acc*100:.2f}% ({refined_val['correct']}/{refined_val['total']})")
    print(f"  Improvement: {(refined_acc - global_acc) * 100:+.2f}pp")
    
    return output


if __name__ == '__main__':
    start = time.time()
    result = main()
    elapsed = time.time() - start
    logger.info(f"Runtime: {elapsed:.1f}s")