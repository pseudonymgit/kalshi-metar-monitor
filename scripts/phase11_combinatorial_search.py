#!/usr/bin/env python3
"""
PHASE 11: Quick combinatorial search with NWP Direct Signal.

Tests top METAR combos with and without nwp_direct to measure improvement.
Uses same walk-forward methodology as Phase 10 but only tests key combos.
"""

import sqlite3
import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.signals.nwp_direct_signal import NwpDirectSignal
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}
DEFAULT_THRESHOLD = 0.5

def load_station_days(station, conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f), MIN(temp_f),
               AVG(dewpoint_f), AVG(temp_f), AVG(wind_direction_deg),
               AVG(wind_speed_kt), AVG(pressure_mb)
        FROM metar_observations WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                     'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    return days

def load_market(conn, station):
    cur = conn.cursor()
    cur.execute("SELECT local_trading_date, settlement_bucket FROM settlement_epochs WHERE station=? AND market_type='HIGH' ORDER BY local_trading_date", (station,))
    records = cur.fetchall()
    
    market = {}
    # Go through all the records and calculate direction relative to previous non-null value
    for i, (date_str, current_value) in enumerate(records):
        if current_value is None:
            market[date_str] = 'flat'
            continue
            
        # Look backwards for the most recent non-null value
        previous_value = None
        for j in range(i-1, -1, -1):  # loop backward from i-1 to 0
            if records[j][1] is not None:
                previous_value = records[j][1]
                break
        
        # If we found a previous value, calculate direction; otherwise mark as flat
        if previous_value is not None:
            if current_value > previous_value:
                market[date_str] = 'up'
            elif current_value < previous_value:
                market[date_str] = 'down'
            else:
                market[date_str] = 'flat'
        else:
            market[date_str] = 'flat'  # Should only happen if this is the very first non-null record
    
    return market

def align_data(days, market):
    return [dict(d, market_dir=market.get(d['date'][:10], 'flat')) for d in days if d['date'][:10] in market]

def evaluate_signal(sig_obj, sig_name, idx, days, station=None, threshold=DEFAULT_THRESHOLD):
    # nwp_direct needs evaluate_for_station — it can't use evaluate(idx, days)
    if sig_name == 'nwp_direct' and station is not None:
        date = days[idx]['date'][:10] if idx < len(days) else None
        if date:
            direction, confidence = sig_obj.evaluate_for_station(station, date)
        else:
            return None, 0.0
    else:
        # All other signals use the standard evaluate(idx, days)
        direction, confidence = sig_obj.evaluate(idx, days)
    
    if direction is None or confidence <= 0:
        return None, 0.0
    if sig_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx-1].get('temp') if idx-1 < len(days) else None
        dp = days[idx-1].get('dewpoint') if idx-1 < len(days) else None
        if temp is not None and dp is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dp)
    if confidence < threshold:
        return None, 0.0
    return direction, confidence

def evaluate_ensemble(combo_list, idx, days, station=None, min_agreement=1):
    votes = []
    for sig_name, sig_obj in combo_list:
        d, c = evaluate_signal(sig_obj, sig_name, idx, days, station)
        if d is not None and c > 0:
            votes.append((d, c))
    if len(votes) < min_agreement:
        return None, 0.0
    up = sum(c for d,c in votes if d == 'up')
    down = sum(c for d,c in votes if d == 'down')
    total = up + down
    if total == 0: return None, 0.0
    if up > down: return 'up', up/total
    elif down > up: return 'down', down/total
    return None, 0.0

def walk_forward(days, sig_names, sig_objs, station=None, min_agreement=1):
    result = {'correct': 0, 'total': 0, 'trades': 0}
    # Use sig_objs directly — they may be swapped for nwp_direct
    combo_list = list(zip(sig_names, sig_objs))
    n = len(days)
    if n < 220: return result
    start_idx = 180
    while start_idx + 30 <= n:
        test_end = min(start_idx + 30, n)
        for idx in range(start_idx, test_end):
            if idx >= len(days): break
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat': continue
            pred_dir, _ = evaluate_ensemble(combo_list, idx, days, station, min_agreement)
            if pred_dir is not None:
                result['total'] += 1
                result['trades'] += 1
                if pred_dir == actual:
                    result['correct'] += 1
        start_idx += 30
    return result

def main():
    db_path = 'data/metar_backfill.db'
    output_path = 'data/phase11_combinatorial_search.json'

    # Key combos to test (with and without nwp_direct)
    key_combos = [
        # METAR-only baselines
        {'signals': ['calendar_climatology'], 'agree': 1, 'label': 'cc_only'},
        {'signals': ['forecast_disagreement', 'calendar_climatology'], 'agree': 2, 'label': 'fd+cc'},
        {'signals': ['pressure_delta', 'forecast_disagreement', 'calendar_climatology'], 'agree': 3, 'label': 'pd+fd+cc'},
        {'signals': ['pressure_delta', 'forecast_disagreement', 'calendar_climatology', 'regime'], 'agree': 3, 'label': 'pd+fd+cc+regime'},
        # NWP-only
        {'signals': ['nwp_direct'], 'agree': 1, 'label': 'nwp_direct'},
        # NWP + METAR fusions
        {'signals': ['nwp_direct', 'calendar_climatology'], 'agree': 2, 'label': 'nwp+cc'},
        {'signals': ['nwp_direct', 'forecast_disagreement', 'calendar_climatology'], 'agree': 2, 'label': 'nwp+fd+cc'},
        {'signals': ['nwp_direct', 'pressure_delta', 'forecast_disagreement', 'calendar_climatology'], 'agree': 3, 'label': 'nwp+pd+fd+cc'},
        # NWP + regime
        {'signals': ['nwp_direct', 'regime'], 'agree': 2, 'label': 'nwp+regime'},
        {'signals': ['nwp_direct', 'pressure_delta', 'regime'], 'agree': 2, 'label': 'nwp+pd+regime'},
        # NWP + gaussian
        {'signals': ['nwp_direct', 'gaussian', 'calendar_climatology'], 'agree': 2, 'label': 'nwp+gauss+cc'},
        {'signals': ['nwp_direct', 'forecast_disagreement', 'gaussian', 'calendar_climatology'], 'agree': 3, 'label': 'nwp+fd+gauss+cc'},
    ]

    # Load signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    logger.info(f"Loaded {len(all_signals)} signals: {list(all_signals.keys())}")

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
            # Add station name to each day for nwp_direct signal
            for d in station_data[s]:
                d['_station'] = s
    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations")

    results = []
    for i, combo in enumerate(key_combos):
        sig_names = combo['signals']
        agree = combo['agree']
        label = combo['label']

        sig_objs = [all_signals.get(s) for s in sig_names]
        if None in sig_objs:
            logger.warning(f"Skipping {label} — missing signal")
            continue

        # For nwp_direct-only, we need the NWP DB
        if 'nwp_direct' in sig_names:
            nwp_sig = NwpDirectSignal()

        logger.info(f"[{i+1}/{len(key_combos)}] {label} (agree={agree}) signals={sig_names}")

        combo_result = {'correct': 0, 'total': 0, 'trades': 0, 'stations': 0}

        for station in station_data:
            days = station_data[station]
            # Replace nwp_direct signal object with the working instance
            working_objs = list(sig_objs)
            for j, s_name in enumerate(sig_names):
                if s_name == 'nwp_direct':
                    working_objs[j] = nwp_sig

            res = walk_forward(days, sig_names, working_objs, station, agree)
            if res['trades'] > 0:
                combo_result['correct'] += res['correct']
                combo_result['total'] += res['total']
                combo_result['trades'] += res['trades']
                combo_result['stations'] += 1

        acc = combo_result['correct'] / combo_result['total'] if combo_result['total'] > 0 else 0
        results.append({
            'label': label,
            'signals': sig_names,
            'min_agreement': agree,
            'accuracy': round(acc, 5),
            'trades': combo_result['trades'],
            'correct': combo_result['correct'],
            'total': combo_result['total'],
            'stations': combo_result['stations'],
        })
        logger.info(f"  Accuracy: {acc:.3f} ({combo_result['correct']}/{combo_result['total']}), trades={combo_result['trades']}, stns={combo_result['stations']}")

    # Save results
    output = {
        'phase11_combinatorial_search': {
            'timestamp': datetime.utcnow().isoformat(),
            'database': db_path,
            'combos_tested': len(results),
            'stations': list(station_data.keys()),
            'description': 'Compare METAR-only vs NWP-included combos',
        },
        'results': results,
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    logger.info(f"\nResults saved to {output_path}")
    logger.info("\nSummary:")
    logger.info(f"{'Label':>25s} {'Acc':>7s} {'Trades':>7s} {'Stns':>5s}")
    logger.info("-" * 45)
    for r in sorted(results, key=lambda x: x['accuracy'], reverse=True):
        logger.info(f"{r['label']:>25s} {r['accuracy']:.3f} {r['trades']:>7d} {r['stations']:>5d}")

    # Compare NWP-included vs METAR-only
    metar_only = [r for r in results if 'nwp_direct' not in r['signals']]
    nwp_included = [r for r in results if 'nwp_direct' in r['signals']]
    if metar_only and nwp_included:
        best_metar = max(metar_only, key=lambda x: x['accuracy'])
        best_nwp = max(nwp_included, key=lambda x: x['accuracy'])
        logger.info(f"\nBest METAR-only: {best_metar['label']} @ {best_metar['accuracy']:.3f} ({best_metar['trades']} trades)")
        logger.info(f"Best NWP-included: {best_nwp['label']} @ {best_nwp['accuracy']:.3f} ({best_nwp['trades']} trades)")
        delta = (best_nwp['accuracy'] - best_metar['accuracy']) * 100
        logger.info(f"Improvement: {delta:+.2f}pp")

    return output

if __name__ == '__main__':
    start = time.time()
    main()
    elapsed = time.time() - start
    logger.info(f"Total runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")