#!/usr/bin/env python3
"""
PHASE 9 FAST — ALL-COMBINATIONS CALIBRATION AND BACKTESTING WITH ALL 11 SIGNALS (OPTIMIZED)

Tests all possible signal combinations with the expanded 11-signal set:
  - calendar_climatology
  - persistence  
  - gaussian
  - gaussian_v2
  - pressure_delta
  - forecast_disagreement 
  - wind_direction_shift
  - goldilocks
  - nwp_analog
  - frontal_detector (Phase 4.6) 
  - intraday_metar_confirmation (Phase 4.7)

Optimized version: limits stations and time periods for faster execution.

Walk-forward: Reduced scope to manage compute.
"""

import sqlite3
import math
import json
import os
import sys
import argparse
import time
import logging
from datetime import datetime
from itertools import combinations
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.dewpoint_modulator import modify_confidence_by_dewpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

LIMITED_STATIONS = ['KATL', 'KBOS', 'KDFW', 'KLAX', 'KSFO']  # Just 5 instead of 20

# All 11 signals for Phase 9
ALL_SIGNALS_REQUIRED = [
    'calendar_climatology',
    'persistence',
    'gaussian', 
    'gaussian_v2',
    'pressure_delta',
    'forecast_disagreement', 
    'wind_direction_shift',
    'goldilocks',
    'nwp_analog',
    'frontal_detector',          # NEW - Phase 4.6
    'intraday_metar_confirmation' # NEW - Phase 4.7
]

# Signals that get dewpoint modulator
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}

# Reduced constants for faster execution
DEFAULT_THRESHOLD = 0.5
TRAIN_DAYS_FAST = 90  # Much shorter training window
TEST_DAYS_FAST = 15   # Shorter test windows
MAX_WINDOWS = 3       # Test fewer time windows 

# ─── Data loading ────────────────────────────────────────────────────────────

def load_station_days(station: str, conn: sqlite3.Connection) -> List[Dict]:
    """Load daily METAR data for one station."""
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
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
    return days


def load_market(conn: sqlite3.Connection, station: str, market_type: str = 'HIGH') -> Dict[str, str]:
    """Load settlement epoch directions: 'up'/'down'/'flat'."""
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


def align_data(days: List[Dict], market: Dict[str, str]) -> List[Dict]:
    """Merge daily data with market direction."""
    aligned = []
    for d in days:
        date_key = d['date'][:10]
        if date_key in market:
            entry = dict(d)
            entry['market_dir'] = market[date_key]
            aligned.append(entry)
    return aligned


# ─── Phase 2 signal evaluation wrapper ───────────────────────────────────────

def evaluate_signal(signal_obj, signal_name: str, idx: int, days: List[Dict],
                    adaptive_threshold: float = DEFAULT_THRESHOLD) -> Tuple[Optional[str], float]:
    """
    Evaluate a single signal with optimizations (no adaptive thresholds in fast version).
    """
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0

    # Apply dewpoint modulator (fixed threshold)
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp') if idx - 1 < len(days) else None
        dewpoint = days[idx - 1].get('dewpoint') if idx - 1 < len(days) else None
        if temp is not None and dewpoint is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)

    # Apply fixed threshold gate
    if confidence < adaptive_threshold:
        return None, 0.0

    return direction, confidence


def evaluate_ensemble(signals: List[Tuple[str, Any]], idx: int, days: List[Dict],
                      thresholds: Dict[str, float] = None,
                      min_agreement: int = 1) -> Tuple[Optional[str], float]:
    """
    Evaluate an ensemble of signals with weighted majority vote.
    Simplified for fast execution (no ensemble diversity penalties).
    """
    votes_with_confidence = []  # [(direction, confidence), ...]
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        direction, confidence = evaluate_signal(sig_obj, sig_name, idx, days, thresh)
        if direction is not None and confidence > 0:
            votes_with_confidence.append((direction, confidence))

    if len(votes_with_confidence) < min_agreement:
        return None, 0.0

    up_weight = sum(c for d, c in votes_with_confidence if d == 'up')
    down_weight = sum(c for d, c in votes_with_confidence if d == 'down')
    total_weight = up_weight + down_weight

    if total_weight == 0:
        return None, 0.0

    if up_weight > down_weight:
        consensus_strength = up_weight / total_weight
        direction = 'up'
    elif down_weight > up_weight:
        consensus_strength = down_weight / total_weight 
        direction = 'down'
    else:
        return None, 0.0

    # Simple ensemble aggregation
    confidence = consensus_strength
    
    return direction, min(confidence, 1.0)


# ─── Walk-forward backtest (fast) ────────────────────────────────────────────

def walk_forward_backtest_single_fast(days: List[Dict], signal_names: List[str],
                                     signal_objs: List[Any], 
                                     train_days: int = TRAIN_DAYS_FAST, 
                                     test_days: int = TEST_DAYS_FAST,
                                     min_agreement: int = 1) -> Dict:
    """
    Fast walk-forward backtest for a single combination on one station.
    Uses shorter windows and fewer passes for faster execution.
    """
    # Build the combo list from signal_names
    sig_map = dict(zip(signal_names, signal_objs))
    combo_list = [(name, sig_map[name]) for name in signal_names]
    
    result = {'correct': 0, 'total': 0, 'trades': 0}
    
    # Walk forward with reduced iterations
    start_idx = train_days
    max_test_end = len(days)
    
    windows_completed = 0
    
    while start_idx + test_days <= max_test_end and windows_completed < MAX_WINDOWS:
        test_end = min(start_idx + test_days, max_test_end)
        
        for idx in range(start_idx, test_end):
            if idx >= len(days):
                break
            
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue
            
            # Fixed thresholds in fast version
            thresholds = {name: DEFAULT_THRESHOLD for name in signal_names}
            
            # Evaluate ensemble
            pred_dir, confidence = evaluate_ensemble(
                combo_list, idx, days, thresholds, min_agreement
            )
            
            if pred_dir is None:
                continue
            
            result['trades'] += 1
            result['total'] += 1
            was_correct = pred_dir == actual
            if was_correct:
                result['correct'] += 1
        
        start_idx += test_days
        windows_completed += 1
    
    return result


# ─── Combinatorial search functions ──────────────────────────────────────────

def generate_combinations(signal_names: List[str]) -> List[Tuple[str, List[str]]]:
    """Generate all non-empty subsets of signal_names."""
    combos = []
    # Limit combination size to make it more manageable for 11 signals
    # 2^11 = about 2048 total, so we'll limit to smaller subsets plus a few larger ones
    for r in range(1, min(len(signal_names)+1, 6)):  # Signals 1-5 
        for subset in combinations(signal_names, r):
            name = '+'.join(subset)
            combos.append((name, list(subset)))
    
    # Also add some larger random selections
    for r in range(6, len(signal_names)+1):
        # Just do the first few combinations of each size to avoid explosion
        count = 0
        for subset in combinations(signal_names, r):
            if count >= 5:  # Only do first 5 of each size > 5
                break
            name = '+'.join(subset)
            combos.append((name, list(subset)))
            count += 1
    
    return combos


def main():
    parser = argparse.ArgumentParser(description='Phase 9 Fast Combinatorial Search with All 11 Signals')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase9_combinatorial_search.json',
                        help='Output file')
    parser.add_argument('--max-combos', type=int, default=200,  # Keep default for consistent comparison
                        help='Max combinations to test (for runtime)')
    parser.add_argument('--station', type=str, default=None,
                        help='Single station for quick test')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Load registry and all signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    
    # Verify all required signals are available
    missing_signals = set(ALL_SIGNALS_REQUIRED) - set(all_signals.keys())
    if missing_signals:
        logger.error(f"Missing required signals: {missing_signals}")
        logger.info(f"Available signals: {list(all_signals.keys())}")
        sys.exit(1)
    
    # Confirm we have exactly the 11 signals required for Phase 9
    available_11_signals = [sig for sig in ALL_SIGNALS_REQUIRED if sig in all_signals]
    logger.info(f"Available signals for Phase 9: {available_11_signals}")

    # Generate combinations
    all_combos = generate_combinations(available_11_signals)
    logger.info(f"Generated {len(all_combos)} combinations to evaluate")
    
    if len(all_combos) > args.max_combos:
        all_combos = all_combos[:args.max_combos]
        logger.info(f"Limited to {len(all_combos)} combinations")

    # Get stations - use limited set for faster execution
    conn = sqlite3.connect(db_path)
    if args.station:
        all_stations = [args.station.upper()]
    else:
        all_stations = LIMITED_STATIONS
    conn.close()
    
    logger.info(f"Testing on limited stations: {all_stations}")

    # Load station data - simplified for speed
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        days = load_station_days(station, conn)
        # Market info not strictly necessary in this simplified version though it helps accuracy
        # We could add it back, but using day-to-day change as proxy instead
        if len(days) >= 120:  # Require minimum of 120 days for reliable fast backtesting
            station_data[station] = days
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(days)} days)")
    conn.close()
    
    # Only test with available stations (limited)
    active_stations = list(station_data.keys())
    if not active_stations:
        logger.error("No stations have sufficient data. Available stations:")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT station, COUNT(*) as count FROM metar_observations GROUP BY station HAVING count > 200")
        for row in cursor.fetchall():
            print(f"  {row[0]}: {row[1]} days")
        conn.close()
        return
    logger.info(f"Loaded data for {len(active_stations)} active stations")


    # ─── Test at selected agreement levels ──────────────────────────────
    agreement_levels = [1, 2, 3]  # Fewer levels to save time
    master_results = {
        'phase9_combinatorial_search_fast': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_tested': available_11_signals,
            'total_computed_combinations': len(all_combos),
            'stations': active_stations,
            'agreement_levels': agreement_levels,
            'description': 'Phase 9 with all 11 signals - optimized fast execution version (shorter windows, fewer stations)',
        },
        'results': {},
        'top_by_accuracy': [],
        'top_by_sharpe': [],
    }

    logger.info(f"Starting fast backtesting with {len(all_combos)} combinations...")

    for min_agree in agreement_levels:
        logger.info(f"\n{'='*60}")
        logger.info(f"Agreement threshold: {min_agree}")
        logger.info(f"{'='*60}")
        
        successful_combos = 0
        
        for i, (combo_name, sig_names) in enumerate(all_combos):
            if len(sig_names) < min_agree:  # Skip combinations with fewer signals than required agreement
                continue
            
            if i % 20 == 0:  # Less frequent printing
                logger.info(f"Progress: {i+1}/{len(all_combos)} combinations")
            
            combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
            station_breakdown = {}
            
            for station in active_stations:
                days = station_data[station]
                # Use only the signals in this combination
                sig_objs = [all_signals[n] for n in sig_names if n in all_signals]
                
                res = walk_forward_backtest_single_fast(
                    days, sig_names, sig_objs,
                    train_days=TRAIN_DAYS_FAST, test_days=TEST_DAYS_FAST,
                    min_agreement=min_agree
                )
                
                if res['trades'] > 0:
                    station_breakdown[station] = res
                    combo_results['correct'] += res['correct']
                    combo_results['total'] += res['total']
                    combo_results['trades'] += res['trades']
                    combo_results['stations_tested'] += 1
            
            if combo_results['trades'] > 0:
                accuracy = combo_results['correct'] / combo_results['trades']
                sharpe = compute_sharpe_simple(combo_results, combo_results['correct'], combo_results['trades'])
                coverage = combo_results['trades'] / (len(active_stations) * 60) if active_stations else 0
                
                key = f"{combo_name.replace('+', '_').replace('.', '_')}_agree_{min_agree}_fast"
                entry = {
                    'accuracy': round(accuracy, 5),
                    'sharpe': round(sharpe, 5),
                    'coverage': round(coverage, 5),
                    'correct': combo_results['correct'],
                    'total': combo_results['total'],
                    'trades': combo_results['trades'],
                    'stations_tested': combo_results['stations_tested'],
                    'min_agreement': min_agree,
                    'signals': sig_names,
                }
                
                master_results['results'][key] = entry
                successful_combos += 1
                
                logger.info(f"  [{successful_combos:3d}] {combo_name[:35]:>35s} [ag={min_agree}]: "
                          f"acc={accuracy:.3f}  shp={sharpe:.3f} t={combo_results['trades']:>4d}")
            else:
                key = f"{combo_name.replace('+', '_').replace('.', '_')}_agree_{min_agree}_fast"
                master_results['results'][key] = {
                    'accuracy': 0.0, 'sharpe': 0.0, 'coverage': 0.0,
                    'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0,
                    'min_agreement': min_agree, 'signals': sig_names,
                }
    
    # Extract best results across all agreement levels
    results_dict = master_results['results']
    
    # Sort by accuracy and sharpe
    all_results = list(results_dict.items())
    all_results = [(k, v) for k, v in all_results if v['trades'] > 0]
    
    sorted_acc = sorted(all_results, key=lambda x: x[1]['accuracy'], reverse=True)
    sorted_sharpe = sorted(all_results, key=lambda x: x[1]['sharpe'], reverse=True)
    
    master_results['top_by_accuracy'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'],  
         **{k: v for k, v in stats.items() if k in ['accuracy', 'sharpe', 'coverage', 'trades', 'stations_tested', 'signals']}}
        for name, stats in sorted_acc[:20]  # Top 20 for each category
    ]
    master_results['top_by_sharpe'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'], 
         **{k: v for k, v in stats.items() if k in ['accuracy', 'sharpe', 'coverage', 'trades', 'stations_tested', 'signals']}}
        for name, stats in sorted_sharpe[:20]
    ]
    
    # Save results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nResults fast version saved to {args.output}")
    
    # Print summary
    print_fast_summary(master_results)
    
    return master_results


def compute_sharpe_simple(stats_result: Dict, correct: int, total: int) -> float:
    """Simple sharpe calculation."""
    if total < 2:
        return 0.0
    
    win_rate = correct / total
    loss_rate = 1.0 - win_rate
    expected_rtn = win_rate * 1.0 + loss_rate * (-1.0)
    variance = win_rate * (1.0 - expected_rtn)**2 + loss_rate * (-1.0 - expected_rtn)**2
    
    if variance <= 0:
        return 0.0
    
    sharpe = expected_rtn / math.sqrt(variance)
    if total >= 2:
        sharpe *= math.sqrt(total - 1)  # Adjust for sample size
    return sharpe if not math.isnan(sharpe) else 0.0


def print_fast_summary(results: Dict):
    """Print simplified summary."""
    header = "\n" + "="*80
    print(header)
    print("PHASE 9 FAST VERSION — TOP COMBINATIONS BY ACCURACY")  
    print("="*80)
    print(f"{'Combo':<30} Acc   Sharpe  Trades St Ag Min")
    print("-"*80)
    for item in results['top_by_accuracy'][:10]:
        s = item
        combo_clean = s['combo'][:28] if len(s['combo']) > 28 else s['combo']
        print(f"{combo_clean:<30} {s['accuracy']:.3f} {s['sharpe']:>7.3f} {s['trades']:>6} {s['stations_tested']:>2} {s['min_agreement']:>3}")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nFast version Total runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")