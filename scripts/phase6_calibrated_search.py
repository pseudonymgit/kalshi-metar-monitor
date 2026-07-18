#!/usr/bin/env python3
"""
PHASE 6 — TASK 6.2: Calibrated In-Sample Search

Takes the top 20 combos from Task 6.1, wires the calibration pipeline into
the search loop. Reports both uncalibrated and calibrated accuracy.

Requirements:
  - Min 50 positive examples per calibrator
  - Cap at 3 bins (isotonic regression naturally bins, but we clamp)
  - Fit isotonic calibrators on training folds, apply to test folds

Usage:
    python3 scripts/phase6_calibrated_search.py [--db-path ...] [--combo-file ...] [--output ...]
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
import pickle
import numpy as np
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import SignalRegistry
from core.dewpoint_modulator import modify_confidence_by_dewpoint
from core.calibration_pipeline import CalibrationPipeline

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

PHASE6_SIGNALS = [
    'calendar_climatology', 'gaussian', 'goldilocks',
    'pressure_delta', 'wind_direction_shift', 'regime',
    'forecast_disagreement',
]
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian'}


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
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
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
        if bucket is None:
            market[date_str] = 'flat'
            prev = bucket; continue
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
            e = dict(d)
            e['market_dir'] = market[dk]
            aligned.append(e)
    return aligned


def evaluate_signal_direct(signal_obj, signal_name, idx, days):
    """Evaluate a single signal, no adaptive threshold gate."""
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp')
        dew = days[idx - 1].get('dewpoint')
        confidence = min(1.0, confidence * (1.2 if (temp and dew and (temp - dew) > 15) else
                                            (0.8 if (temp and dew and (temp - dew) < 5) else 1.0)))
    return direction, min(confidence, 3.0)  # keep raw z-score for calibration


def evaluate_ensemble(signals, idx, days, min_agree=1):
    """Simple majority vote ensemble."""
    votes = []
    for name, obj in signals:
        d, c = evaluate_signal_direct(obj, name, idx, days)
        if d and c > 0:
            votes.append((d, c))
    if len(votes) < min_agree:
        return None, 0.0
    up = sum(c for d, c in votes if d == 'up')
    dn = sum(c for d, c in votes if d == 'down')
    if up > dn:
        return 'up', up / (up + dn)
    elif dn > up:
        return 'down', dn / (up + dn)
    return None, 0.0


def calibrate_confidence(cal_pipeline, signal_name, station, raw_conf):
    """Apply calibration pipeline with normalization."""
    norm_conf = min(1.0, raw_conf / 3.0) if raw_conf > 1.0 else raw_conf
    cal_conf = cal_pipeline.calibrate(signal_name, station, norm_conf)
    return cal_conf, norm_conf


def main():
    parser = argparse.ArgumentParser(description='Phase 6 Calibrated Search')
    parser.add_argument('--db-path', default='data/metar_backfill.db')
    parser.add_argument('--combo-file', default='data/phase6_combinatorial_search.json')
    parser.add_argument('--cal-file', default='data/calibrated_pipeline_v3.pkl')
    parser.add_argument('--output', default='data/phase6_calibrated_search.json')
    parser.add_argument('--station', default=None)
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        logger.error(f"DB not found: {args.db_path}")
        sys.exit(1)
    if not os.path.exists(args.combo_file):
        logger.error(f"Combo file not found: {args.combo_file}. Run Task 6.1 first.")
        sys.exit(1)

    # Load top combos from Task 6.1
    with open(args.combo_file) as f:
        phase6_results = json.load(f)
    
    top_acc = phase6_results.get('top_by_accuracy', [])
    top_sharpe = phase6_results.get('top_by_sharpe', [])
    
    # Merge top 10 accuracy + top 10 sharpe, deduplicate → top 20
    seen = set()
    top_combos = []
    for entry in top_acc + top_sharpe:
        name = entry['combo']
        if name not in seen:
            seen.add(name)
            top_combos.append(entry)
    top_combos = top_combos[:20]
    
    logger.info(f"Loaded {len(top_combos)} top combos from Task 6.1")

    # Load calibration pipeline
    cal_pipeline_for_2 = None
    if os.path.exists(args.cal_file):
        with open(args.cal_file, 'rb') as f:
            cal_pipeline_for_2 = pickle.load(f)
        logger.info(f"Loaded calibration: {len(cal_pipeline_for_2.calibrators)} per-city, "
                    f"{len(cal_pipeline_for_2.fallback_calibrators)} fallback, "
                    f"{'global' if cal_pipeline_for_2.global_calibrator else 'no global'}")
    else:
        logger.warning(f"Calibration file not found: {args.cal_file} — running uncalibrated only")

    # Load signals
    registry = SignalRegistry(args.db_path)
    all_signals = registry.get_all_signals()
    active_signals = {n: all_signals[n] for n in PHASE6_SIGNALS if n in all_signals}
    logger.info(f"Signals: {list(active_signals.keys())}")

    # Load station data
    conn = sqlite3.connect(args.db_path)
    if args.station:
        stations = [args.station.upper()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r[0] for r in cur.fetchall()]
    
    station_data = {}
    for station in stations:
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align(days, market)
        if len(aligned) >= 250:
            station_data[station] = aligned
    conn.close()
    logger.info(f"Loaded {len(station_data)} stations")

    # Results accumulator
    output = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'db_path': args.db_path,
            'signals': list(active_signals.keys()),
            'stations': list(station_data.keys()),
            'num_combos': len(top_combos),
        },
        'raw_results': {},
        'calibrated_results': {},
        'summary': [],
    }

    # For each top combo, run walk-forward with calibration
    for combo_entry in top_combos:
        combo_name = combo_entry['combo']
        sig_names = combo_entry.get('signals', combo_name.split('+'))
        # Filter to valid signals
        valid_sigs = [s for s in sig_names if s in active_signals]
        if not valid_sigs:
            continue
        
        logger.info(f"\n{'='*70}")
        logger.info(f"Combo: {combo_name} ({len(valid_sigs)} signals: {valid_sigs})")
        
        combo_sig_objs = [(n, active_signals[n]) for n in valid_sigs]
        
        # Per-station results
        raw_agg = {'correct': 0, 'total': 0}
        cal_agg = {'correct': 0, 'total': 0}
        
        for station in station_data:
            days = station_data[station]
            
            # Walk-forward loop using pre-loaded calibration pipeline
            train_days = 180
            test_days = 30
            
            for train_end in range(train_days, len(days) - test_days, test_days):
                test_start = train_end
                test_end = min(test_start + test_days, len(days))
                
                # Phase 1: Collect calibration data from training window
                if cal_pipeline_for_2 is not None:
                    # FIX: Wire set_window_start() into walk-forward loop.
                    # Calibrators were seeing full dataset because window_start
                    # was never updated per window. This caused data leakage:
                    # calibrators fit on data from ALL windows, not just the
                    # current training window.
                    #
                    # set_window_start(0) resets the window boundary for the
                    # current window's training data. Combined with refit()
                    # (which calls prune_history()), this ensures calibrators
                    # only fit on data from the current walk-forward window.
                    cal_pipeline_for_2.set_window_start(0)
                    for idx in range(train_days, train_end):
                        if idx >= len(days):
                            break
                        actual = days[idx].get('market_dir', 'flat')
                        if actual == 'flat':
                            continue
                        for sig_name, sig_obj in combo_sig_objs:
                            direction, raw_conf = evaluate_signal_direct(sig_obj, sig_name, idx, days)
                            if direction is None or raw_conf <= 0:
                                continue
                            norm_conf = min(1.0, raw_conf / 3.0) if raw_conf > 1.0 else raw_conf
                            was_correct = direction == actual
                            cal_pipeline_for_2.update(sig_name, station, norm_conf, was_correct)
                    cal_pipeline_for_2.refit()
                
                # Phase 2: Test on test window
                for idx in range(test_start, test_end):
                    if idx >= len(days):
                        break
                    actual = days[idx].get('market_dir', 'flat')
                    if actual == 'flat':
                        continue
                    
                    # ── Raw ensemble ──
                    pred_dir, _ = evaluate_ensemble(combo_sig_objs, idx, days)
                    if pred_dir:
                        raw_agg['total'] += 1
                        if pred_dir == actual:
                            raw_agg['correct'] += 1
                    
                    # ── Calibrated ensemble ──
                    if cal_pipeline_for_2 is not None:
                        cal_votes = []
                        for sig_name, sig_obj in combo_sig_objs:
                            direction, raw_conf = evaluate_signal_direct(sig_obj, sig_name, idx, days)
                            if direction is None or raw_conf <= 0:
                                continue
                            cal_conf, norm_conf = calibrate_confidence(cal_pipeline_for_2, sig_name, station, raw_conf)
                            if cal_conf > 0.5:
                                cal_votes.append((direction, cal_conf))
                        
                        if len(cal_votes) >= 1:
                            up = sum(c for d, c in cal_votes if d == 'up')
                            dn = sum(c for d, c in cal_votes if d == 'down')
                            if up > dn or dn > up:
                                cal_pred = 'up' if up >= dn else 'down'
                                cal_agg['total'] += 1
                                if cal_pred == actual:
                                    cal_agg['correct'] += 1
        
        raw_acc = raw_agg['correct'] / raw_agg['total'] if raw_agg['total'] > 0 else 0
        cal_acc = cal_agg['correct'] / cal_agg['total'] if cal_agg['total'] > 0 else 0
        delta = cal_acc - raw_acc
        
        entry = {
            'combo': combo_name,
            'signals': valid_sigs,
            'raw_accuracy': round(raw_acc, 5),
            'raw_trades': raw_agg['total'],
            'calibrated_accuracy': round(cal_acc, 5),
            'calibrated_trades': cal_agg['total'],
            'delta': round(delta, 5),
            'improvement_pct': round((cal_acc - raw_acc) / raw_acc * 100, 2) if raw_acc > 0 else 0,
        }
        
        output['raw_results'][combo_name] = raw_agg
        output['calibrated_results'][combo_name] = cal_agg
        output['summary'].append(entry)
        
        logger.info(f"  RAW:  {raw_acc*100:.2f}% ({raw_agg['correct']}/{raw_agg['total']})")
        logger.info(f"  CAL:  {cal_acc*100:.2f}% ({cal_agg['correct']}/{cal_agg['total']})")
        logger.info(f"  Δ:    {delta*100:+.2f}pp")
    
    # Sort summary by improvement
    output['summary'].sort(key=lambda x: x['delta'], reverse=True)
    
    # Save
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\nSaved to {args.output}")
    
    # Print summary table
    print("\n" + "="*90)
    print("CALIBRATED SEARCH — RAW vs CALIBRATED ACCURACY")
    print("="*90)
    print(f"{'Combo':<40} {'Raw Acc':>8} {'Cal Acc':>8} {'Δ':>8} {'Raw Tr':>7} {'Cal Tr':>7}")
    print("-"*78)
    for entry in output['summary']:
        print(f"{entry['combo']:<40} {entry['raw_accuracy']:>8.3f} {entry['calibrated_accuracy']:>8.3f} "
              f"{entry['delta']:>+8.3f} {entry['raw_trades']:>7} {entry['calibrated_trades']:>7}")
    
    return output


if __name__ == '__main__':
    start = time.time()
    result = main()
    elapsed = time.time() - start
    logger.info(f"Runtime: {elapsed:.1f}s")