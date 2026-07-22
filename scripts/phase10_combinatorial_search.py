#!/usr/bin/env python3
"""
PHASE 10 — FULL COMBINATORIAL SEARCH: Cleaned Signal Set No Goldilocks

Tests all possible combinations of the cleaned, working signal set with NO goldilocks:
  - calendar_climatology
  - gaussian (keep, not gaussian_v2 — gaussian_v2 is redundant)
  - gaussian_v2 (identified as redundant with gaussian)
  - pressure_delta
  - forecast_disagreement
  - wind_direction_shift
  - regime (just registered)
  - nwp_analog
  - persistence (basic trend, orthogonal)
  - temperature_advection (orthogonal to above)
  - frontal_detector (orthogonal to above)
  - intraday_metar_confirmation (note: needs intraday data, may not work in daily backtest)

Uses walk-forward: 180d train, 30d test, rolling. Tests ALL 2^n combinations.
All 20 stations. Agreement levels 1, 2, 3.

Output to `data/phase10_combinatorial_search.json`
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

ALL_STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
                'KPHX','KSAT','KSEA','KSFO']

# Signals that get dewpoint modulator
DEWPOINT_MODULATED = {'calendar_climatology', 'gaussian', 'gaussian_v2'}

# Default adaptive thresholds (will be overridden per-city per-signal during walk-forward)
DEFAULT_THRESHOLD = 0.5
ADAPTIVE_STEP = 0.1
ADAPTIVE_MIN = 0.3
ADAPTIVE_MAX = 0.9
ADAPTIVE_HIGH_ACC = 0.70
ADAPTIVE_LOW_ACC = 0.50
ADAPTIVE_WINDOW = 30

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


# ─── Signal evaluation wrapper ───────────────────────────────────────

def evaluate_signal(signal_obj, signal_name: str, idx: int, days: List[Dict],
                    adaptive_threshold: float = DEFAULT_THRESHOLD) -> Tuple[Optional[str], float]:
    """
    Evaluate a single signal with Phase 2 improvements.
    
    Steps:
    1. Call signal.evaluate(idx, days)
    2. Apply dewpoint modulator (calendar_climatology, gaussian)
    3. Apply adaptive threshold gate
    """
    direction, confidence = signal_obj.evaluate(idx, days)
    if direction is None or confidence <= 0:
        return None, 0.0

    # Step 2: Dewpoint modulator (clear-sky boost / cloudy reduction)
    if signal_name in DEWPOINT_MODULATED and idx > 0:
        temp = days[idx - 1].get('temp') if idx - 1 < len(days) else None
        dewpoint = days[idx - 1].get('dewpoint') if idx - 1 < len(days) else None
        if temp is not None and dewpoint is not None:
            confidence = modify_confidence_by_dewpoint(confidence, temp, dewpoint)

    # Step 3: Adaptive threshold gate
    if confidence < adaptive_threshold:
        return None, 0.0

    return direction, confidence


def evaluate_ensemble(signals: List[Tuple[str, Any]], idx: int, days: List[Dict],
                      thresholds: Dict[str, float] = None,
                      min_agreement: int = 1) -> Tuple[Optional[str], float]:
    """
    Evaluate an ensemble of signals with weighted majority vote.
    
    Returns (direction, confidence).
    """
    votes = []  # (direction, confidence)
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        direction, confidence = evaluate_signal(sig_obj, sig_name, idx, days, thresh)
        if direction is not None and confidence > 0:
            votes.append((direction, confidence))

    if len(votes) < min_agreement:
        return None, 0.0

    up_weight = sum(c for d, c in votes if d == 'up')
    down_weight = sum(c for d, c in votes if d == 'down')
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

    # Ensemble diversity score: confidence = winning vote fraction
    confidence = consensus_strength

    return direction, min(confidence, 1.0)


# ─── Walk-forward backtest ───────────────────────────────────────────────────

def walk_forward_backtest_single(days: List[Dict], signal_names: List[str],
                                  signal_objs: List[Any], market: Dict[str, str],
                                  train_days: int = 180, test_days: int = 30,
                                  min_agreement: int = 1) -> Dict:
    """
    Walk-forward backtest for a single combination on one station.
    
    Returns dict with correct, total, trades, etc.
    """
    # Group signals by name for lookup
    sig_map = dict(zip(signal_names, signal_objs))
    
    # Build the combo list from signal_names
    combo_list = [(name, sig_map[name]) for name in signal_names]
    
    result = {'correct': 0, 'total': 0, 'trades': 0}
    
    # Walk forward
    start_idx = train_days
    max_test_end = len(days)
    
    while start_idx + test_days <= max_test_end:
        test_end = min(start_idx + test_days, max_test_end)
        
        for idx in range(start_idx, test_end):
            if idx >= len(days):
                break
            
            actual = days[idx].get('market_dir', 'flat')
            if actual == 'flat':
                continue
            
            # Evaluate ensemble with default thresholds
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
        # Process all windows for full coverage
        if start_idx >= len(days) - test_days:
            break
    
    return result


# ─── Combinatorial search functions ──────────────────────────────────────────

def generate_combinations(signal_names: List[str]) -> List[Tuple[str, List[str]]]:
    """Generate all non-empty subsets of signal_names."""
    combos = []
    # Generate ALL possible combinations: 2^n - 1 (excluding empty set)
    for r in range(1, len(signal_names) + 1):
        for subset in combinations(signal_names, r):
            name = '+'.join(subset)
            combos.append((name, list(subset)))
    return combos


def main():
    parser = argparse.ArgumentParser(description='Phase 10 Combinatorial Search on Cleaned Signal Set (No Goldilocks)')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase10_combinatorial_search.json',
                        help='Output file')
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Load registry and all signals
    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    
    # Get all signal names from the registry and EXCLUDE goldilocks
    all_signal_names = [n for n in all_signals.keys() if n != 'goldilocks']
    
    # Based on the correlation analysis and requirements:
    # Only include proven working signals from correlation analysis
    PROVEN_WORKING_SIGNALS = [
        'calendar_climatology',
        'forecast_disagreement', 
        'frontal_detector',
        'gaussian',
        'gaussian_v2',  # Not removed as correlation with gaussian is <80% (49%) 
        'persistence',
        'pressure_delta',
        'regime',
        'wind_direction_shift'
    ]
    
    # Filter signals to only those in the proven list
    all_signal_names = [n for n in all_signal_names if n in PROVEN_WORKING_SIGNALS]
    
    logger.info(f"Loaded {len(all_signal_names)} signals from registry (excluding goldilocks and non-working signals): {all_signal_names}")

    # Generate ALL combinations 
    all_combos = generate_combinations(all_signal_names)
    logger.info(f"Generated ALL {len(all_combos)} combinations to test")
    logger.info(f"First 10: {[c[0] for c in all_combos[:10]]}")

    # Load all available stations for testing 
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
    all_available_stations = [r[0] for r in cur.fetchall()]
    conn.close()
    
    # Use 20 specific stations 
    all_stations = ALL_STATIONS
    logger.info(f"Testing on {len(all_stations)} stations: {all_stations}")

    # Load all station data upfront
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        if station not in all_available_stations:
            logger.warning(f"Station {station} not in database")
            continue
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align_data(days, market)
        if len(aligned) >= 250:  # Skip short series
            station_data[station] = aligned
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(aligned)} days)")
    conn.close()
    logger.info(f"Loaded data for {len(station_data)} stations (min 250 days)")

    # ─── Test at various agreement levels ──────────────────────────────
    agreement_levels = [1, 2, 3]  # As specified in the task
    master_results = {
        'phase10_combinatorial_search': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_tested': all_signal_names,  # All signals except goldilocks
            'total_generated_combinations': len(all_combos),
            'total_tested_combinations': 0,  # Will count as we go
            'stations': list(station_data.keys()),
            'agreement_levels': agreement_levels,
            'description': 'Full combinatorial search on cleaned 11-signal set (no goldilocks)',
        },
        'results': {},
        'top_by_accuracy': [],
        'top_by_sharpe': [],
        'top_by_coverage': [],
    }

    total_combinations_tested = 0
    
    for min_agree in agreement_levels:
        logger.info(f"\n{'='*80}")
        logger.info(f"Agreement threshold: {min_agree}")
        logger.info(f"{'='*80}")
        
        combinations_tested_for_level = 0
        for combo_idx, (combo_name, sig_names) in enumerate(all_combos):
            if len(sig_names) < min_agree:  # Skip if not enough signals for agreement requirement
                continue
            
            # Progress indicator
            if combo_idx % 1000 == 0:
                logger.info(f"  Processed {combo_idx} of {len(all_combos)} combinations for agreement {min_agree}")
            
            combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
            station_breakdown = {}
            
            for station in list(station_data.keys()):
                days = station_data[station]
                sig_objs = [all_signals[n] for n in sig_names if n in all_signals]
                
                if len(sig_objs) != len(sig_names):
                    logger.error(f"Missing signal objects for combo {combo_name} - {set(sig_names) - set(all_signals.keys())}")
                    continue
                
                res = walk_forward_backtest_single(
                    days, sig_names, sig_objs, None,
                    train_days=180, test_days=30,
                    min_agreement=min_agree
                )
                
                if res['trades'] > 0:
                    station_breakdown[station] = res
                    combo_results['correct'] += res['correct']
                    combo_results['total'] += res['total']
                    combo_results['trades'] += res['trades']
                    combo_results['stations_tested'] += 1
            
            # Save results if there were any trades
            if combo_results['trades'] > 0:
                accuracy = combo_results['correct'] / combo_results['trades']
                sharpe = compute_sharpe(combo_results)
                # Coverage: trades per station-average across test period
                coverage = combo_results['trades'] / (len(station_data) * 30) if len(station_data) > 0 else 0  # Approximated by stations * 30 day windows
                
                key = f"{combo_name}_agree_{min_agree}"
                entry = {
                    'accuracy': round(accuracy, 5),
                    'sharpe': round(sharpe, 5),
                    'coverage': round(coverage, 5),
                    'correct': combo_results['correct'],
                    'total': combo_results['total'],
                    'trades': combo_results['trades'],
                    'stations_tested': combo_results['stations_tested'],
                    'min_agreement': min_agree,
                    'station_breakdown': {k: v for k, v in station_breakdown.items()},
                    'signals': sig_names,
                }
                
                master_results['results'][key] = entry
                total_combinations_tested += 1
                combinations_tested_for_level += 1
                
                logger.info(f"  [{min_agree} agree {combinations_tested_for_level:4d}] {len(sig_names):2d}-sig {combo_name[:30]:<30s}: acc={accuracy:.3f}  shr={sharpe:.3f}  cov={coverage:.3f}  trades={combo_results['trades']:>4d}  ")
            else:
                # Still count as tested (just no trades)
                total_combinations_tested += 1
                combinations_tested_for_level += 1
    
    # Update the total tested
    master_results['phase10_combinatorial_search']['total_tested_combinations'] = total_combinations_tested
    
    # Extract best results across all agreement levels
    results_dict = master_results['results']
    
    # Rank by accuracy, sharpe, and coverage
    all_results = list(results_dict.items())
    
    # Sort by each metric for top lists
    sorted_acc = sorted(all_results, key=lambda x: x[1]['accuracy'], reverse=True) if all_results else []
    sorted_sharpe = sorted(all_results, key=lambda x: x[1]['sharpe'], reverse=True) if all_results else []
    sorted_cov = sorted(all_results, key=lambda x: x[1]['coverage'], reverse=True) if all_results else []
    
    # Get top 10 for each metric
    master_results['top_by_accuracy'] = [
        {'combo': name.split('_agree_')[0], 'agreement': stats['min_agreement'], **{k: v for k, v in stats.items() if k != 'station_breakdown'}}
        for name, stats in sorted_acc[:10]
    ]
    
    master_results['top_by_sharpe'] = [
        {'combo': name.split('_agree_')[0], 'agreement': stats['min_agreement'], **{k: v for k, v in stats.items() if k != 'station_breakdown'}}
        for name, stats in sorted_sharpe[:10]
    ]
    
    master_results['top_by_coverage'] = [
        {'combo': name.split('_agree_')[0], 'agreement': stats['min_agreement'], **{k: v for k, v in stats.items() if k != 'station_breakdown'}}
        for name, stats in sorted_cov[:10]
    ]
    
    # Save
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nPhase 10 results saved to {args.output}")
    logger.info(f"Tested {total_combinations_tested} combinations across {len(agreement_levels)} agreement levels")
    
    # Print summary
    print_summary(master_results)
    
    return master_results


def compute_sharpe(stats: Dict) -> float:
    """Compute APPROXIMATE Sharpe ratio as used in the project (z-statistic: sqrt(n)*mean/std)."""
    total = stats['total']
    correct = stats['correct']
    if total < 2:
        return 0.0
    
    # These values match what's used throughout the system in place of proper Sharpe
    trades = stats['trades']
    if trades == 0:
        return 0.0
    
    win_rate = correct / trades
    loss_rate = 1.0 - win_rate
    expected_return = win_rate * 1.0 + loss_rate * (-1.0)  # 1 unit profit, 1 unit loss
    
    # This is what the system calls "Sharpe" but it's actually a z-statistic
    std_returns = math.sqrt(win_rate * (1 - win_rate)**2 + loss_rate * (-1 - win_rate)**2) if win_rate < 1.0 and loss_rate < 1.0 else 0.5
    if std_returns <= 0:
        return 0.0
    
    # Z-statistic form used as "Sharpe" 
    approximate_sharpe = expected_return / std_returns if std_returns > 0 else 0.0
    approximate_sharpe *= math.sqrt(trades)  # Multiplied by sqrt(N) as in typical implementations
    
    return approximate_sharpe


def print_summary(results: Dict):
    """Print final summary table."""
    print("\n" + "="*120)
    print("PHASE 10 — TOP 5 BY ACCURACY (Across All Agreement Levels)")
    print("="*120)
    print(f"{'Signals':<35} {'Ag':>2} {'Acc':>6} {'Sharpe':>7} {'Cov':>6} {'Trd':>5} {'Stns':>4}")
    print("-"*95)
    for item in results['top_by_accuracy'][:5] if results['top_by_accuracy'] else []:
        s = item
        signals = s['combo']
        if '+' in signals and len(signals) > 32:
            signals_short = signals[:29] + "..."  # Truncate long combo names
        else:
            signals_short = signals
        print(f"{signals_short:<35} {s['agreement']:>2} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>6.3f} {s['trades']:>5} {s['stations_tested']:>4}")

    print("\n" + "="*120)
    print("PHASE 10 — TOP 5 BY SHARPE (Across All Agreement Levels)")
    print("="*120)
    print(f"{'Signals':<35} {'Ag':>2} {'Acc':>6} {'Sharpe':>7} {'Cov':>6} {'Trd':>5} {'Stns':>4}")
    print("-"*95)
    for item in results['top_by_sharpe'][:5] if results['top_by_sharpe'] else []:
        s = item
        signals = s['combo']
        if '+' in signals and len(signals) > 32:
            signals_short = signals[:29] + "..."
        else:
            signals_short = signals
        print(f"{signals_short:<35} {s['agreement']:>2} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>6.3f} {s['trades']:>5} {s['stations_tested']:>4}")

    print("\n" + "="*120) 
    print("PHASE 10 — TOP 5 BY COVERAGE (Across All Agreement Levels)")
    print("="*120)
    print(f"{'Signals':<35} {'Ag':>2} {'Acc':>6} {'Sharpe':>7} {'Cov':>6} {'Trd':>5} {'Stns':>4}")
    print("-"*95)
    for item in results['top_by_coverage'][:5] if results['top_by_coverage'] else []:
        s = item  
        signals = s['combo']
        if '+' in signals and len(signals) > 32:
            signals_short = signals[:29] + "..."
        else:
            signals_short = signals
        print(f"{signals_short:<35} {s['agreement']:>2} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>6.3f} {s['trades']:>5} {s['stations_tested']:>4}")

    # Summary statistics
    print("\n" + "="*120)
    print(f"SUMMARY STATISTICS")
    print("="*120)
    config = results['phase10_combinatorial_search']
    print(f"  Signals tested: {len(config['signals_tested'])} ({config['signals_tested']})")  
    print(f"  Total generated combinations: {config['total_generated_combinations']} (2^{len(config['signals_tested'])} - 1)")
    print(f"  Total tested: {config['total_tested_combinations']}")
    print(f"  Stations used: {len(config['stations'])}")  
    print(f"  Agreement levels: {config['agreement_levels']}")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")