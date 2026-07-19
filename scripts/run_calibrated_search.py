#!/usr/bin/env python3
"""
Calibrated Combinatorial Search — applies isotonic calibration to signal confidence
before ensemble voting. Compares calibrated vs raw results for top combinations.

Usage:
    python3 scripts/run_calibrated_search.py --db-path data/metar_backfill.db
    python3 scripts/run_calibrated_search.py --station KNYC  # single station
"""

import sqlite3
import os
import sys
import argparse
import pickle
from collections import defaultdict
from typing import List, Dict, Optional, Tuple
from itertools import combinations
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signals import SignalRegistry


def get_season(date_str):
    m = int(date_str[5:7])
    if m in (12, 1, 2): return 'DJF'
    elif m in (3, 4, 5): return 'MAM'
    elif m in (6, 7, 8): return 'JJA'
    else: return 'SON'


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
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
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
            prev = bucket
            continue
        if prev is not None:
            market[date_str] = 'up' if bucket > prev else ('down' if bucket < prev else 'flat')
        else:
            market[date_str] = 'flat'
        prev = bucket
    return market


def align(days, market):
    aligned = []
    for d in days:
        date_key = d['date'][:10]
        if date_key in market:
            entry = dict(d)
            entry['market_dir'] = market[date_key]
            aligned.append(entry)
    return aligned


def evaluate_ensemble_calibrated(signals, signal_names, idx, days, calibrator, station,
                                  min_agreement=1):
    """Evaluate ensemble with calibration applied."""
    predictions = []
    for sig_name, sig_obj in zip(signal_names, signals):
        direction, raw_conf = sig_obj.evaluate(idx, days)
        if direction is not None and raw_conf > 0:
            # Apply calibration if available
            if calibrator is not None:
                cal_conf = calibrator.calibrate(sig_name, station, min(raw_conf, 1.0))
                # Calibrated confidence: if > 0.5, signal is bullish; < 0.5, bearish
                # Map back to directional confidence
                if cal_conf > 0.5:
                    conf = cal_conf  # P(correct | signal says UP)
                elif cal_conf < 0.5:
                    conf = 1.0 - cal_conf  # P(correct | signal says DOWN), invert
                    direction = 'down' if direction == 'up' else 'up'  # Flip direction
                else:
                    continue  # 0.5 = no signal
            else:
                conf = raw_conf
            predictions.append((direction, conf))

    if len(predictions) < min_agreement:
        return None, 0.0

    up_w = sum(c for d, c in predictions if d == 'up')
    down_w = sum(c for d, c in predictions if d == 'down')
    total_votes = up_w + down_w

    if total_votes == 0:
        return None, 0.0

    if up_w >= down_w and up_w >= min_agreement:
        return 'up', up_w / total_votes
    elif down_w > up_w and down_w >= min_agreement:
        return 'down', down_w / total_votes
    return None, 0.0


def walk_forward_backtest(days, market, signal_names, signal_objs, calibrator, station,
                          train_days=180, test_days=30, min_agreement=1):
    """Walk-forward with calibrated confidence."""
    results = {'trades': 0, 'correct': 0}
    num_windows = max(1, (len(days) - train_days - test_days) // test_days + 1)

    for w in range(num_windows):
        train_end = train_days + w * test_days
        test_end = min(train_end + test_days, len(days))
        if train_end >= len(days) - 1 or test_end > len(days):
            break

        for idx in range(train_end, test_end):
            if idx >= len(days):
                break
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue

            pred_dir, conf = evaluate_ensemble_calibrated(
                signal_objs, signal_names, idx, days, calibrator, station, min_agreement
            )
            if pred_dir is None:
                continue

            results['trades'] += 1
            if pred_dir == actual:
                results['correct'] += 1

    return results


def main():
    parser = argparse.ArgumentParser(description='Calibrated combinatorial search')
    parser.add_argument('--db-path', default='data/metar_backfill.db')
    parser.add_argument('--station', default=None)
    parser.add_argument('--calibration', default='data/calibrated_pipeline.pkl')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found: {db_path}")
        sys.exit(1)

    # Load calibration pipeline
    calibrator = None
    if os.path.exists(args.calibration):
        with open(args.calibration, 'rb') as f:
            calibrator = pickle.load(f)
        print(f"Loaded calibration pipeline: {len(calibrator.calibrators)} per-city calibrators, "
              f"{len(calibrator.fallback_calibrators)} global, "
              f"{'global' if calibrator.global_calibrator else 'no'} global")
    else:
        print("WARNING: No calibration file found — running uncalibrated")

    # Load signals
    registry = SignalRegistry(db_path)
    all_signals = registry.get_all_signals()
    desired = ['persistence', 'simple_trend', 'gaussian', 'gaussian_v2',
               'pressure_delta', 'goldilocks', 'wind_direction_shift']
    active = {k: v for k, v in all_signals.items() if k in desired}
    signal_names = list(active.keys())
    signal_objs = list(active.values())
    print(f"Loaded {len(active)} signals: {signal_names}")

    # Get stations
    conn = sqlite3.connect(db_path)
    if args.station:
        stations = [args.station.upper()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        stations = [r[0] for r in cur.fetchall()]

    print(f"Testing {len(stations)} stations: {stations}")

    # Test top combinations at min_agreement=1
    # Focus on: single signals + top pairs
    top_combos = {
        'gaussian': ['gaussian'],
        'gaussian_v2': ['gaussian_v2'],
        'gaussian+pressure_delta': ['gaussian', 'pressure_delta'],
        'persistence+gaussian': ['persistence', 'gaussian'],
        'wind_direction_shift+gaussian': ['wind_direction_shift', 'gaussian'],
        'gaussian+gaussian_v2': ['gaussian', 'gaussian_v2'],
        'all': signal_names,  # All 7
    }

    print(f"\nTesting {len(top_combos)} combinations across {len(stations)} stations\n")

    for combo_name, sigs in top_combos.items():
        names = [s for s in signal_names if s in sigs]
        idxs = [signal_names.index(n) for n in names]
        objs = [signal_objs[i] for i in idxs]

        raw_total = {'trades': 0, 'correct': 0}
        cal_total = {'trades': 0, 'correct': 0}

        for station in stations:
            local_conn = sqlite3.connect(db_path)
            days = load_station_days(station, local_conn)
            market = load_market(local_conn, station, 'HIGH')
            local_conn.close()

            aligned_days = align(days, market)
            if len(aligned_days) < 250:
                continue

            # Find start index (need max lookback + 180 train)
            start = 180 + max(s.min_lookback for s in objs)
            test_end = min(start + 120, len(aligned_days))
            if test_end <= start:
                continue

            for idx in range(start, test_end):
                actual = aligned_days[idx].get('market_dir', 'flat')
                if actual == 'flat':
                    continue

                # Raw evaluation (no calibration)
                raw_preds = []
                for sig_name, sig_obj in zip(names, objs):
                    dir_, conf = sig_obj.evaluate(idx, aligned_days)
                    if dir_ and conf > 0:
                        raw_preds.append((dir_, conf))

                if len(raw_preds) >= 1:
                    up_w = sum(c for d, c in raw_preds if d == 'up')
                    down_w = sum(c for d, c in raw_preds if d == 'down')
                    if up_w >= 1 or down_w >= 1:
                        pred = 'up' if up_w >= down_w else 'down'
                        raw_total['trades'] += 1
                        if pred == actual:
                            raw_total['correct'] += 1

                # Calibrated evaluation
                cal_preds = []
                for sig_name, sig_obj in zip(names, objs):
                    dir_, raw_conf = sig_obj.evaluate(idx, aligned_days)
                    if dir_ and raw_conf > 0:
                        if calibrator is not None:
                            # Normalize z-score based confidences to [0,1] before calibration
                            norm_conf = min(1.0, raw_conf / 3.0) if raw_conf > 1.0 else raw_conf
                            cal_conf = calibrator.calibrate(sig_name, station, norm_conf)
                            # Only use calibration if it improves confidence
                            # If cal_conf < 0.5, the signal is unreliable — skip, don't invert
                            if cal_conf > 0.5:
                                cal_preds.append((dir_, cal_conf))
                            else:
                                continue
                        else:
                            cal_preds.append((dir_, raw_conf))

                if len(cal_preds) >= 1:
                    up_w = sum(c for d, c in cal_preds if d == 'up')
                    down_w = sum(c for d, c in cal_preds if d == 'down')
                    if up_w >= 1 or down_w >= 1:
                        pred = 'up' if up_w >= down_w else 'down'
                        cal_total['trades'] += 1
                        if pred == actual:
                            cal_total['correct'] += 1

        raw_acc = raw_total['correct'] / raw_total['trades'] if raw_total['trades'] > 0 else 0
        cal_acc = cal_total['correct'] / cal_total['trades'] if cal_total['trades'] > 0 else 0

        raw_label = f"RAW:  {raw_acc*100:.2f}% ({raw_total['correct']}/{raw_total['trades']})"
        cal_label = f"CAL:  {cal_acc*100:.2f}% ({cal_total['correct']}/{cal_total['trades']})"
        diff = cal_acc - raw_acc
        diff_label = f"Δ={'+' if diff >= 0 else ''}{diff*100:.2f}pp"
        print(f"  {combo_name:>35s}  |  {raw_label:>25s}  |  {cal_label:>25s}  |  {diff_label}")


if __name__ == '__main__':
    main()