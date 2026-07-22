#!/usr/bin/env python3
"""
PHASE 10.3 — CALIBRATED SEARCH: Rolling walk-forward with per-signal calibration

Uses the SAME rolling walk-forward methodology as combinatorial search (180d train, 30d test, rolling),
but adds per-signal per-station calibration via CalibrationPipeline.

Tests the top 10 combos from the combinatorial search on all 20 stations.
"""

import sqlite3
import json
import os
import sys
import time
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional, Tuple, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.dewpoint_modulator import modify_confidence_by_dewpoint
from core.calibration_pipeline import CalibrationPipeline

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}
DEFAULT_THRESHOLD = 0.5


def load_station_days(station: str, conn: sqlite3.Connection) -> List[Dict]:
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
        if any(v is None for v in r[1:]):
            continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                     'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    return days


def load_market(conn: sqlite3.Connection, station: str) -> Dict[str, str]:
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, settlement_bucket
        FROM settlement_epochs WHERE station=? AND market_type='HIGH'
        ORDER BY local_trading_date ASC
    """, (station,))
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


def align_data(days, market):
    aligned = []
    for d in days:
        date_key = d['date'][:10]
        if date_key in market:
            entry = dict(d)
            entry['market_dir'] = market[date_key]
            aligned.append(entry)
    return aligned


def evaluate_signal(signal_obj, signal_name, idx, days, threshold=DEFAULT_THRESHOLD):
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx-1].get('temp') if idx-1 < len(days) else None
        dewpoint = days[idx-1].get('dewpoint') if idx-1 < len(days) else None
        if temp is not None and dewpoint is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)
    if confidence < threshold:
        return None, 0.0
    return direction, confidence


def evaluate_ensemble(combo_list, idx, days, calibrator=None, station=None, min_agreement=1):
    votes = []
    for sig_name, sig_obj in combo_list:
        direction, raw_conf = evaluate_signal(sig_obj, sig_name, idx, days)
        if direction is not None and raw_conf > 0:
            if calibrator is not None and station is not None:
                cal_conf = calibrator.calibrate(sig_name, station, min(1.0, max(0.0, raw_conf)))
                votes.append((direction, cal_conf))
            else:
                votes.append((direction, raw_conf))
    if len(votes) < min_agreement:
        return None, 0.0
    up = sum(c for d,c in votes if d == 'up')
    down = sum(c for d,c in votes if d == 'down')
    total = up + down
    if total == 0:
        return None, 0.0
    if up > down:
        return 'up', up / total
    elif down > up:
        return 'down', down / total
    return None, 0.0


def walk_forward_rolling(days, sig_names, sig_objs, market, min_agreement=1, station='UNKNOWN'):
    """
    Rolling walk-forward: train on 180 days, test on next 30 days, slide forward.
    Same methodology as combinatorial search but with calibration.
    """
    combo_list = list(zip(sig_names, sig_objs))
    result = {'correct': 0, 'total': 0, 'trades': 0,
              'cal_correct': 0, 'cal_total': 0, 'cal_trades': 0}

    n = len(days)
    if n < 220:
        return result

    start_idx = 180
    while start_idx + 30 <= n:
        test_end = min(start_idx + 30, n)

        # Train calibrator on data before this window
        cal = CalibrationPipeline(sig_names, [station], max_history=2000, window_start=0)
        for train_idx in range(180, start_idx):
            if train_idx >= len(days):
                break
            actual = days[train_idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue
            for sig_name, sig_obj in combo_list:
                direction, raw_conf = evaluate_signal(sig_obj, sig_name, train_idx, days)
                if direction is not None and raw_conf > 0:
                    pred_correct = (direction == actual)
                    cal.update(sig_name, station, raw_conf, pred_correct)
        cal.refit()

        # Test on this window
        for idx in range(start_idx, test_end):
            if idx >= len(days):
                break
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue

            # Raw (no calibration)
            pred_dir, _ = evaluate_ensemble(combo_list, idx, days, calibrator=None, station=None, min_agreement=min_agreement)
            if pred_dir is not None:
                result['total'] += 1
                result['trades'] += 1
                if pred_dir == actual:
                    result['correct'] += 1

            # Calibrated
            cal_dir, _ = evaluate_ensemble(combo_list, idx, days, calibrator=cal, station=station, min_agreement=min_agreement)
            if cal_dir is not None:
                result['cal_total'] += 1
                result['cal_trades'] += 1
                if cal_dir == actual:
                    result['cal_correct'] += 1

        start_idx += 30

    return result


def main():
    db_path = 'data/metar_backfill.db'
    output_path = 'data/phase10_3_calibrated_search.json'
    combo_input = 'data/phase10_combinatorial_search.json'

    if not os.path.exists(db_path) or not os.path.exists(combo_input):
        logger.error("Required files not found")
        sys.exit(1)

    # Load top 10 combos
    with open(combo_input) as f:
        search_data = json.load(f)
    top_by_acc = search_data.get('top_by_accuracy', [])
    top_combos = top_by_acc[:10]
    logger.info(f"Loaded {len(top_combos)} top combos")

    # Load signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()

    # Load station data
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    available = [r[0] for r in cur.fetchall()]
    stations = [s for s in ALL_STATIONS if s in available]
    conn.close()

    station_data = {}
    conn = sqlite3.connect(db_path)
    for s in stations:
        days = load_station_days(s, conn)
        market = load_market(conn, s)
        aligned = align_data(days, market)
        if len(aligned) >= 250:
            station_data[s] = aligned
    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations")

    results = []
    for i, combo in enumerate(top_combos):
        combo_str = combo.get('combo', '')
        min_agree = combo.get('min_agreement', 1)
        signals = combo_str.split('+') if combo_str else []
        if not signals or not all(s in all_signals for s in signals):
            continue

        sig_objs = [all_signals[s] for s in signals]
        logger.info(f"[{i+1}/{len(top_combos)}] {combo_str} (agree={min_agree})")

        combo_result = {'correct': 0, 'total': 0, 'trades': 0,
                        'cal_correct': 0, 'cal_total': 0, 'cal_trades': 0,
                        'stations': 0, 'station_breakdown': {}}

        for station in station_data:
            days = station_data[station]
            res = walk_forward_rolling(days, signals, sig_objs, None, min_agree, station)
            if res['trades'] > 0:
                for k in ['correct', 'total', 'trades', 'cal_correct', 'cal_total', 'cal_trades']:
                    combo_result[k] += res[k]
                combo_result['stations'] += 1
                combo_result['station_breakdown'][station] = {
                    'raw_accuracy': res['correct'] / res['total'] if res['total'] > 0 else 0,
                    'cal_accuracy': res['cal_correct'] / res['cal_total'] if res['cal_total'] > 0 else 0,
                    'raw_trades': res['trades'],
                    'cal_trades': res['cal_trades'],
                }

        raw_acc = combo_result['correct'] / combo_result['total'] if combo_result['total'] > 0 else 0
        cal_acc = combo_result['cal_correct'] / combo_result['cal_total'] if combo_result['cal_total'] > 0 else 0
        coverage = 0.0  # approximate coverage doesn't matter for this test

        results.append({
            'combo': combo_str,
            'min_agreement': min_agree,
            'raw_accuracy': round(raw_acc, 5),
            'calibrated_accuracy': round(cal_acc, 5),
            'raw_trades': combo_result['trades'],
            'cal_trades': combo_result['cal_trades'],
            'stations_tested': combo_result['stations'],
            'station_breakdown': combo_result['station_breakdown'],
        })

        logger.info(f"  Raw: {raw_acc:.3f} ({combo_result['correct']}/{combo_result['total']}) "
                    f"Cal: {cal_acc:.3f} ({combo_result['cal_correct']}/{combo_result['cal_total']})")

    results.sort(key=lambda x: x['calibrated_accuracy'], reverse=True)

    output = {
        'phase10_3_calibrated_search': {
            'timestamp': datetime.utcnow().isoformat(),
            'combinatorial_source': combo_input,
            'database': db_path,
            'combinations_tested': len(results),
            'stations': stations,
            'description': 'Rolling walk-forward (180d train, 30d test) with per-signal per-station calibration.',
        },
        'calibrated_walkforward_results': results,
        'top_20_by_calibrated': results[:20],
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    logger.info(f"\nResults saved to {output_path}")
    for i, r in enumerate(results[:5]):
        logger.info(f"  {i+1}. {r['combo']:45s} (agree={r['min_agreement']}) "
                    f"raw={r['raw_accuracy']:.3f} cal={r['calibrated_accuracy']:.3f} "
                    f"trades={r['raw_trades']} stns={r['stations_tested']}")

    return output


if __name__ == '__main__':
    start = time.time()
    main()
    elapsed = time.time() - start
    logger.info(f"Total runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")