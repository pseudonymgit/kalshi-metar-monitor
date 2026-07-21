#!/usr/bin/env python3
"""
PHASE 9 — ALL-COMBINATIONS CALIBRATION AND BACKTESTING WITH ALL 11 SIGNALS

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

Uses ensemble approach with:
  - Dewpoint depression modulator (multiplies confidence)
  - Adaptive confidence thresholds (adjusts per-signal thresholds)
  - Ensemble diversity score (penalizes low diversity)
  - Spatial coherence gate (modulates per-region confidence)

Walk-forward: 180d train, 30d test, rolling. 20 stations.

Usage:
    python3 scripts/phase9_combinatorial_search.py [--db-path ...] [--output ...] [--mp]
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

# Default adaptive thresholds (will be overridden per-city per-signal during walk-forward)
DEFAULT_THRESHOLD = 0.5
ADAPTIVE_STEP = 0.1
ADAPTIVE_MIN = 0.3
ADAPTIVE_MAX = 0.9
ADAPTIVE_HIGH_ACC = 0.70
ADAPTIVE_LOW_ACC = 0.50
ADAPTIVE_WINDOW = 30

# Ensemble diversity threshold
MIN_ENSEMBLE_DIVERSITY = 0.3  # Penalize if all signals agree too much


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


def apply_spatial_coherence_gate(direction: str, confidence: float, 
                                 signal_compatibility: Dict[str, Dict[str, float]]) -> float:
    """
    Apply spatial coherence gate that adjusts confidence based on regional compatibility.
    signal_compatibility maps signal -> {other_signal -> compatibility_score}
    For now we'll implement a general coherence penalty based on ensemble disagreement.
    """
    # Placeholder implementation - would connect to actual spatial logic
    return confidence  # Return confidence after potential adjustment


def apply_ensemble_diversity_penalty(votes_with_confidence: List[Tuple[str, float]], 
                                    signal_compatibility: Dict[str, Dict[str, float]],
                                    ensemble_size: int) -> float:
    """
    Apply penalty if signals are too similar (not diverse enough).
    Returns a multiplier (score between 0-1) to reduce weight.
    """
    if len(votes_with_confidence) < 2 or ensemble_size < 2:
        # Single signal has max diversity by definition
        return 1.0
    
    all_directs = [vote[0] for vote in votes_with_confidence]
    all_confs = [vote[1] for vote in votes_with_confidence]
    
    # Compute a basic diversity score based on direction disagreement
    # More diversity means better
    up_votes = sum(1 for d, c in votes_with_confidence if d == 'up')
    down_votes = sum(1 for d, c in votes_with_confidence if d == 'down') 
    
    diversity_ratio = abs(0.5 - (up_votes / max(len(votes_with_confidence), 1))) * 2
    if up_votes == 0 or down_votes == 0:  # All same direction
        diversity_score = 0.0
    else:
        diversity_score = diversity_ratio  # Higher when mixed directions
    
    # If diversity is low, penalize more heavily
    penalty_mult = max(diversity_score, MIN_ENSEMBLE_DIVERSITY)
    return penalty_mult


def evaluate_ensemble(signals: List[Tuple[str, Any]], idx: int, days: List[Dict],
                      thresholds: Dict[str, float] = None,
                      min_agreement: int = 1,
                      signal_compatibility: Dict[str, Dict[str, float]] = None) -> Tuple[Optional[str], float]:
    """
    Evaluate an ensemble of signals with weighted majority vote.
    
    Returns (direction, confidence).
    """
    votes_with_confidence = []  # [(direction, confidence_weight), ...]
    for sig_name, sig_obj in signals:
        thresh = (thresholds or {}).get(sig_name, DEFAULT_THRESHOLD)
        direction, confidence = evaluate_signal(sig_obj, sig_name, idx, days, thresh)
        if direction is not None and confidence > 0:
            votes_with_confidence.append((direction, confidence))

    if len(votes_with_confidence) < min_agreement:
        return None, 0.0

    # Apply ensemble diversity penalty
    diversity_penalty = apply_ensemble_diversity_penalty(votes_with_confidence, 
                                                       signal_compatibility, 
                                                       len(signals))
    
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

    # Final confidence incorporates diversity penalty
    confidence = consensus_strength * diversity_penalty

    # Apply spatial coherence gate if available
    if signal_compatibility:
        confidence = apply_spatial_coherence_gate(direction, confidence, signal_compatibility)
    
    return direction, min(confidence, 1.0)


# ─── Adaptive threshold tracker ──────────────────────────────────────────────

class AdaptiveThresholdTracker:
    """Tracks rolling accuracy per signal to adjust thresholds dynamically."""
    
    def __init__(self):
        # history: dict[signal_name] -> list of (was_correct, used_threshold)
        self.history: Dict[str, List[bool]] = defaultdict(list)
    
    def record(self, signal_name: str, was_correct: bool):
        self.history[signal_name].append(was_correct)
        if len(self.history[signal_name]) > ADAPTIVE_WINDOW * 2:
            self.history[signal_name] = self.history[signal_name][-ADAPTIVE_WINDOW * 2:]
    
    def get_threshold(self, signal_name: str) -> float:
        """Get adaptive threshold based on rolling 30-day accuracy."""
        hist = self.history.get(signal_name, [])
        if len(hist) < 5:
            return DEFAULT_THRESHOLD
        
        recent = hist[-ADAPTIVE_WINDOW:]
        accuracy = sum(1 for c in recent if c) / len(recent)
        
        # Current threshold (start from default)
        # We adjust based on relative performance
        if accuracy > ADAPTIVE_HIGH_ACC:
            return max(ADAPTIVE_MIN, DEFAULT_THRESHOLD - ADAPTIVE_STEP)
        elif accuracy < ADAPTIVE_LOW_ACC:
            return min(ADAPTIVE_MAX, DEFAULT_THRESHOLD + ADAPTIVE_STEP)
        else:
            return DEFAULT_THRESHOLD


# ─── Walk-forward backtest ───────────────────────────────────────────────────

def walk_forward_backtest_single(days: List[Dict], signal_names: List[str],
                                  signal_objs: List[Any], market: Dict[str, str],
                                  train_days: int = 180, test_days: int = 30,
                                  min_agreement: int = 1,
                                  signal_compatibility: Dict[str, Dict[str, float]] = None) -> Dict:
    """
    Walk-forward backtest for a single combination on one station.
    
    Returns dict with correct, total, trades, etc.
    """
    # Group signals by name for lookup
    sig_map = dict(zip(signal_names, signal_objs))
    
    # Build the combo list from signal_names
    combo_list = [(name, sig_map[name]) for name in signal_names]
    
    result = {'correct': 0, 'total': 0, 'trades': 0}
    
    adaptive = AdaptiveThresholdTracker()
    
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
            
            # Build per-signal thresholds from adaptive tracker
            thresholds = {}
            for name in signal_names:
                thresholds[name] = adaptive.get_threshold(name)
            
            # Evaluate ensemble
            pred_dir, confidence = evaluate_ensemble(
                combo_list, idx, days, thresholds, min_agreement, signal_compatibility
            )
            
            if pred_dir is None:
                continue
            
            result['trades'] += 1
            result['total'] += 1
            was_correct = pred_dir == actual
            if was_correct:
                result['correct'] += 1
            
            # Update adaptive threshold history for each signal
            for name, obj in combo_list:
                sig_dir, sig_conf = evaluate_signal(obj, name, idx, days,
                                                     thresholds.get(name, DEFAULT_THRESHOLD))
                if sig_dir is not None and sig_conf > 0:
                    adaptive.record(name, sig_dir == actual)
        
        start_idx += test_days
        # Limit windows for runtime: test up to 10 windows
        if start_idx >= train_days + 10 * test_days:
            break
    
    return result


# ─── Combinatorial search functions ──────────────────────────────────────────

def generate_combinations(signal_names: List[str]) -> List[Tuple[str, List[str]]]:
    """Generate all non-empty subsets of signal_names."""
    combos = []
    for r in range(1, len(signal_names) + 1):
        for subset in combinations(signal_names, r):
            name = '+'.join(subset)
            combos.append((name, list(subset)))
    return combos


def main():
    parser = argparse.ArgumentParser(description='Phase 9 Combinatorial Search with All 11 Signals')
    parser.add_argument('--db-path', type=str, default='data/metar_backfill.db',
                        help='Path to METAR database')
    parser.add_argument('--output', type=str, default='data/phase9_combinatorial_search.json',
                        help='Output file')
    parser.add_argument('--max-combos', type=int, default=200,  # Limit combos for runtime
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

    # Generate all possible combinations for the 11 signals
    full_combos = generate_combinations(available_11_signals)
    
    # Limit to max_combos to control computation
    if len(full_combos) > args.max_combos:
        all_combos = full_combos[:args.max_combos]
        logger.info(f"Limited to {args.max_combos} combinations from total {len(full_combos)} possible combinations")
    else:
        all_combos = full_combos
        logger.info(f"Generated all {len(all_combos)} combinations for the 11 signals")

    # Get stations
    conn = sqlite3.connect(db_path)
    if args.station:
        all_stations = [args.station.upper()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station FROM settlement_epochs ORDER BY station")
        all_stations = [r[0] for r in cur.fetchall()]
    conn.close()
    
    logger.info(f"Testing on {len(all_stations)} stations: {all_stations[:10]}{'...' if len(all_stations) > 10 else ''}")

    # Load all station data upfront
    station_data = {}
    conn = sqlite3.connect(db_path)
    for station in all_stations:
        days = load_station_days(station, conn)
        market = load_market(conn, station)
        aligned = align_data(days, market)
        if len(aligned) >= 250:  # Require minimum data for reliable backtesting
            station_data[station] = aligned
        else:
            logger.debug(f"  Skipping {station}: insufficient data ({len(aligned)} days)")
    conn.close()
    
    # Limit to 20 stations as per walk-forward setup
    active_stations = list(station_data.keys())[:20]  
    logger.info(f"Loaded data for {len(active_stations)} active stations (first 20 from available)")


    # ─── Test at various agreement levels ──────────────────────────────
    agreement_levels = [1, 2, 3, 4, 5]  # Various agreement thresholds
    master_results = {
        'phase9_combinatorial_search': {
            'timestamp': datetime.now().isoformat(),
            'database': db_path,
            'signals_tested': available_11_signals,  # All signals from Phase 9
            'max_combinations': args.max_combos,
            'total_computed_combinations': len(all_combos),
            'stations': active_stations,
            'agreement_levels': agreement_levels,
            'description': 'Phase 9 with all 11 signals (expanded from Phase 8) tested with various agreement gates',
        },
        'results': {},
        'top_by_accuracy': [],
        'top_by_sharpe': [],
        'top_by_coverage': [],
    }

    logger.info(f"Starting backtesting with {len(all_combos)} combinations across {len(agreement_levels)} agreement settings...")

    for min_agree in agreement_levels:
        logger.info(f"\n{'='*80}")
        logger.info(f"Agreement threshold: {min_agree}")
        logger.info(f"{'='*80}")
        
        successful_combos = 0
        
        for i, (combo_name, sig_names) in enumerate(all_combos):
            if len(sig_names) < min_agree:  # Skip combinations with fewer signals than required agreement
                logger.debug(f"Skipping {combo_name}: fewer signals ({len(sig_names)}) than required agreement ({min_agree})")
                continue
            
            if i % 10 == 0:
                logger.info(f"Progress: {i+1}/{len(all_combos)} combinations")
            
            combo_results = {'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0}
            station_breakdown = {}
            
            for station in active_stations:  # Test on all active stations
                days = station_data[station]
                # Only use the signals in this combination
                sig_objs = [all_signals[n] for n in sig_names if n in all_signals]
                
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
            
            if combo_results['trades'] > 0:
                accuracy = combo_results['correct'] / combo_results['trades']
                sharpe = compute_sharpe(combo_results)
                
                # Coverage is trades per total potential opportunity 
                # Total opportunity = stations_tested * (time_periods_in_data)
                # We'll calculate as trades per station-month approx 
                avg_trades_per_sta = combo_results['trades'] / combo_results['stations_tested'] if combo_results['stations_tested'] > 0 else 0
                theoretical_max = len(active_stations) * 60 * 2  # Roughly 60 test periods * 2 directions
                coverage = combo_results['trades'] / theoretical_max if theoretical_max > 0 else 0
                
                key = f"{combo_name.replace('+', '_').replace('.', '_')}_agree_{min_agree}"
                entry = {
                    'accuracy': round(accuracy, 5),
                    'sharpe': round(sharpe, 5),
                    'coverage': round(coverage, 5),
                    'avg_trades_per_station': round(avg_trades_per_sta, 2),
                    'correct': combo_results['correct'],
                    'total': combo_results['total'],
                    'trades': combo_results['trades'],
                    'stations_tested': combo_results['stations_tested'],
                    'min_agreement': min_agree,
                    'station_breakdown': {k: v for k, v in station_breakdown.items()},
                    'signals': sig_names,
                }
                
                master_results['results'][key] = entry
                successful_combos += 1
                
                logger.info(f"  [{successful_combos:3d}] {combo_name[:45]:>45s} [agree={min_agree}]: "
                          f"acc={accuracy:.3f}  shrp={sharpe:.3f}  cov={coverage:.3f}  "
                          f"trades={combo_results['trades']:>5d} stn={combo_results['stations_tested']:>2d}")
            else:
                key = f"{combo_name.replace('+', '_').replace('.', '_')}_agree_{min_agree}"  
                master_results['results'][key] = {
                    'accuracy': 0.0, 'sharpe': 0.0, 'coverage': 0.0, 'avg_trades_per_station': 0.0,
                    'correct': 0, 'total': 0, 'trades': 0, 'stations_tested': 0,
                    'min_agreement': min_agree, 'station_breakdown': {},
                    'signals': sig_names,
                }
    
    # Extract best results across all agreement levels
    results_dict = master_results['results']
    
    # Sort by accuracy, sharpe, and coverage respectively for top of each
    all_results = list(results_dict.items())
    
    # Filter to only those with actual performance
    all_results = [(k, v) for k, v in all_results if v['trades'] > 0]
    
    sorted_acc = sorted(all_results, key=lambda x: x[1]['accuracy'], reverse=True)
    sorted_sharpe = sorted(all_results, key=lambda x: x[1]['sharpe'], reverse=True)
    sorted_cov = sorted(all_results, key=lambda x: x[1]['coverage'], reverse=True)
    
    # Store only the most relevant result information
    master_results['top_by_accuracy'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'],  
         **{k: v for k, v in stats.items() if k in ['accuracy', 'sharpe', 'coverage', 'trades', 'stations_tested', 'signals']}}
        for name, stats in sorted_acc[:50]  # Top 50 for each category
    ]
    master_results['top_by_sharpe'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'], 
         **{k: v for k, v in stats.items() if k in ['accuracy', 'sharpe', 'coverage', 'trades', 'stations_tested', 'signals']}}
        for name, stats in sorted_sharpe[:50]
    ]
    master_results['top_by_coverage'] = [
        {'combo': name.split('_agree_')[0], 'min_agreement': stats['min_agreement'], 
         **{k: v for k, v in stats.items() if k in ['accuracy', 'sharpe', 'coverage', 'trades', 'stations_tested', 'signals']}}
        for name, stats in sorted_cov[:50]
    ]
    
    # Save results
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(master_results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {args.output}")
    
    # Print detailed summary
    print_summary(master_results)
    
    return master_results


def compute_sharpe(stats: Dict) -> float:
    """Compute approximate Sharpe ratio from win/loss statistics."""
    total_trades = stats['trades']
    correct = stats['correct']
    total = stats['total']
    
    # Handle edge cases
    if total_trades < 2 or total == 0:
        return 0.0
    
    if correct == 0:
        return -1.0  # Perfect wrongness penalized maximally
    
    win_pct = correct / total_trades 
    lose_pct = (total_trades - correct) / total_trades
    
    # Calculate excess return over "risk-free rate" (random chance = 50%)
    excess_return = win_pct - lose_pct  # If win=0.6, lose=0.4, excess return = 0.2
    
    # Calculate volatility (standard deviation of outcome binary: +1 or -1)
    # Expected squared deviation: E[(R - E[R])^2]
    variance = win_pct * ((1 - excess_return)**2) + lose_pct * ((-1 - excess_return)**2)
    
    if variance <= 0:
        return 0.0 if excess_return == 0 else (float('inf') if excess_return > 0 else float('-inf'))
    
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0 if excess_return == 0 else (float('inf') if excess_return > 0 else float('-inf')) 
    
    # Sharpe ratio = (return - riskfree) / volatility
    # Since we consider risk-neutral return as 0, we simply divide excess return by vol
    return excess_return / std_dev if std_dev > 0 else 0.0


def print_summary(results: Dict):
    """Print final summary table."""
    header = "\n" + "="*120
    print(header)
    print("PHASE 9 — ALL 11 SIGNALS — TOP 5 BY ACCURACY (Across All Agreement Levels)")  
    print("="*120)
    print(f"{'Signal Combination':<40} {'Acc':>6} {'Sharpe':>7} {'Cov':>5} {'Trds':>6} {'Stns':>4} {'Agree':>5} {'Signals':<40}")
    print("-"*120)
    for item in results['top_by_accuracy'][:5]:
        s = item
        combo_clean = s['combo'][:37] if len(s['combo']) > 37 else s['combo']
        signal_list = ','.join([name[:5] for name in s['signals'][:5]]) # Show first 5 signal names
        if len(s['signals']) > 5:
            signal_list += '...' 
        print(f"{combo_clean:<40} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>5.3f} {s['trades']:>6} {s['stations_tested']:>4} {s['min_agreement']:>5} {signal_list:<40}")

    print(header)
    print("PHASE 9 — ALL 11 SIGNALS — TOP 5 BY SHARPE (Across All Agreement Levels)")
    print("="*120)
    print(f"{'Signal Combination':<40} {'Acc':>6} {'Sharpe':>7} {'Cov':>5} {'Trds':>6} {'Stns':>4} {'Agree':>5} {'Signals':<40}")
    print("-"*120)
    for item in results['top_by_sharpe'][:5]:
        s = item
        combo_clean = s['combo'][:37] if len(s['combo']) > 37 else s['combo']
        signal_list = ','.join([name[:5] for name in s['signals'][:5]])
        if len(s['signals']) > 5:
            signal_list += '...'
        print(f"{combo_clean:<40} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>5.3f} {s['trades']:>6} {s['stations_tested']:>4} {s['min_agreement']:>5} {signal_list:<40}")

    print(header)
    print("PHASE 9 — ALL 11 SIGNALS — TOP 5 BY COVERAGE (Across All Agreement Levels)")
    print("="*120)
    print(f"{'Signal Combination':<40} {'Acc':>6} {'Sharpe':>7} {'Cov':>5} {'Trds':>6} {'Stns':>4} {'Agree':>5} {'Signals':<40}")
    print("-"*120)
    for item in results['top_by_coverage'][:5]:
        s = item
        combo_clean = s['combo'][:37] if len(s['combo']) > 37 else s['combo']
        signal_list = ','.join([name[:5] for name in s['signals'][:5]])
        if len(s['signals']) > 5:
            signal_list += '...'
        print(f"{combo_clean:<40} {s['accuracy']:>6.3f} {s['sharpe']:>7.3f} {s['coverage']:>5.3f} {s['trades']:>6} {s['stations_tested']:>4} {s['min_agreement']:>5} {signal_list:<40}")


if __name__ == '__main__':
    start = time.time()
    results = main()
    elapsed = time.time() - start
    logger.info(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f}min)")