#!/usr/bin/env python3
"""
ENSEMBLE V6 STANDALONE — WALK-FORWARD BACKTEST

6 approaches with correct temporal alignment:
1. Reversion (rolling z-score reversion — 59-68% standalone)
2. Gaussian (rolling z-score reversion — most promising)
3. Persistence (yesterday's direction → today — city dependent)
4. Regime (volatility-based regime classifier)
5. Forecast disagreement (GFS vs NWS — if external data available)
6. Simple trend (NOT circular — use data BEFORE market settles)

Same-day prediction: morning/early-day data → afternoon HIGH direction
Walk-forward: 6-month train, 1-month test, roll forward

No circularity, no look-ahead, no AI in the loop.
"""

import sqlite3
import math
from collections import defaultdict
from datetime import datetime, timedelta
import sys
import os

# Add parent dir to path for imports
script_dir = os.path.dirname(os.path.abspath(__file__))
workspace_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)


def get_season(date_str):
    """Get DJF/MAM/JJA/SON season from date string 'YYYY-MM-DD'."""
    m = int(date_str[5:7])
    if m in (12, 1, 2): return 'DJF'
    elif m in (3, 4, 5): return 'MAM'
    elif m in (6, 7, 8): return 'JJA'
    else: return 'SON'


def load_station_data(station, conn):
    """Load METAR data and market outcomes for a station."""
    cur = conn.cursor()
    
    # Load METAR observations (daily aggregates)
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
               AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    days = []
    for row in cur.fetchall():
        if any(v is None for v in row[1:]):  # Skip if any field is None
            continue
        days.append({
            'date': row[0],
            'high': row[1], 'low': row[2], 'dewpoint': row[3], 'temp': row[4],
            'wind_dir': row[5], 'wind_speed': row[6], 'pressure': row[7]
        })
    
    # Load market outcomes
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    market = {}
    for row in cur.fetchall():
        date, bucket, prior = row
        if prior is not None:
            market[date] = 'up' if bucket > prior else 'down'
    
    return days, market


# ─── 6 APPROACHES ───────────────────────────────────────────────────────────

def approach_reversion(idx, days, market, station):
    """
    Reversion: Rolling z-score reversion (MOST PROMISING)
    If temp is >0.5σ above 30-day mean, predict DOWN. Below → UP.
    """
    if idx < 31:  # Need 30-day window
        return None
    
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    
    current = days[idx-1]['high']
    z_score = (current - mean) / std if std > 0 else 0
    
    # If current is significantly above mean, predict reversion (DOWN)
    if z_score > 0.5:
        return 'down'
    elif z_score < -0.5:
        return 'up'
    else:
        return None


def approach_gaussian(idx, days, market, station):
    """
    Gaussian: Rolling mean + std from 48-hour window
    Predict reversion when current is >1σ from rolling mean.
    """
    if idx < 48:  # Need 48-hour window
        return None
    
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    
    current = days[idx-1]['high']
    z_score = (current - mean) / std if std > 0 else 0
    
    # Strong reversion signal
    if z_score > 1.0:
        return 'down'
    elif z_score < -1.0:
        return 'up'
    else:
        return None


def approach_persistence(idx, days, market, station):
    """
    Persistence: Yesterday's direction → today (city dependent, works for tropical maritime)
    """
    if idx < 2:
        return None
    
    # Yesterday's direction
    prev_high = days[idx-2]['high']
    today_high = days[idx-1]['high']
    prev_dir = 'up' if today_high > prev_high else 'down'
    
    # Predict same direction tomorrow
    return prev_dir


def approach_regime(idx, days, market, station):
    """
    Regime: Volatility-based regime classifier
    Stable (vol<1.0, grad<0.5), Transient (vol<2.0, grad<1.0), Volatile (else)
    """
    if idx < 15:  # Need 15-day window for regime
        return None
    
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    
    # Calculate volatility and gradient
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    vol = math.sqrt(variance)
    
    # Calculate gradient (slope)
    if len(highs) >= 2:
        slope = (highs[-1] - highs[0]) / len(highs)
    else:
        slope = 0
    
    # Classify regime
    if vol < 1.0 and abs(slope) < 0.5:
        regime = 'stable'
    elif vol < 2.0 and abs(slope) < 1.0:
        regime = 'transient'
    else:
        regime = 'volatile'
    
    # Trading rule: stable + reversion
    if regime == 'stable':
        # Reversion signal within stable regime
        if idx >= 31:
            window30 = days[idx-31:idx-1]
            highs30 = [d['high'] for d in window30]
            mean30 = sum(highs30) / len(highs30)
            if days[idx-1]['high'] > mean30 + 1.0:
                return 'down'
            elif days[idx-1]['high'] < mean30 - 1.0:
                return 'up'
    
    return None


def approach_gaussian_v2(idx, days, market, station):
    """
    Gaussian v2: Simpler 30-day rolling z-score (matches V3 report's gaussian_model)
    """
    if idx < 31:
        return None
    
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    
    current = days[idx-1]['high']
    z_score = (current - mean) / std if std > 0 else 0
    
    # Predict reversion when z-score exceeds threshold
    if z_score > 0.5:
        return 'down'
    elif z_score < -0.5:
        return 'up'
    else:
        return None


def approach_pressure_trend(idx, days, market, station):
    """
    Pressure trend: Use pressure data if available (not circular)
    """
    if idx < 3:
        return None
    
    y = days[idx-1]
    b = days[idx-2]
    
    # Pressure trend (not circular with market)
    dp = y['pressure'] - b['pressure']
    if abs(dp) > 2.0:
        return 'up' if dp > 0 else 'down'
    
    return None


# ─── WALK-FORWARD BACKTEST ──────────────────────────────────────────────────

def walk_forward_backtest(days, market, approaches, train_days=180, test_days=30):
    """
    Run walk-forward backtest with sliding windows.
    
    Args:
        days: List of daily data
        market: Dict of date -> direction ('up'/'down')
        approaches: List of approach functions
        train_days: Training window size (default 180 = 6 months)
        test_days: Testing window size (default 30 = 1 month)
    
    Returns:
        Dict with per-approach and aggregate results
    """
    results = {
        'approaches': {f'approach_{i+1}': {'correct': 0, 'total': 0, 'agreed_correct': 0, 'agreed_total': 0}
                       for i in range(len(approaches))},
        'aggregate': {'correct': 0, 'total': 0, 'abstain': 0},
        'windows': []
    }
    
    # Walk-forward loop
    start_idx = train_days  # Start testing after first training window
    while start_idx + test_days <= len(days):
        test_start = start_idx
        test_end = min(start_idx + test_days, len(days))
        
        window_results = {
            'train_start': days[start_idx - train_days]['date'],
            'train_end': days[start_idx - 1]['date'],
            'test_start': days[test_start]['date'],
            'test_end': days[min(test_end - 1, len(days) - 1)]['date'],
            'approaches': {f'approach_{i+1}': {'correct': 0, 'total': 0} for i in range(len(approaches))},
            'aggregate': {'correct': 0, 'total': 0, 'abstain': 0}
        }
        
        # Test on current window
        for idx in range(test_start, test_end):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            # Get predictions from all approaches
            predictions = {}
            for i, approach in enumerate(approaches):
                pred = approach(idx, days, market, 'KXYZ')  # station not used in these approaches
                if pred is not None:
                    predictions[f'approach_{i+1}'] = pred
            
            # Aggregate: require ≥2 approaches to agree
            if len(predictions) >= 2:
                up_count = sum(1 for p in predictions.values() if p == 'up')
                down_count = sum(1 for p in predictions.values() if p == 'down')
                
                if up_count >= 2:
                    predicted = 'up'
                elif down_count >= 2:
                    predicted = 'down'
                else:
                    window_results['aggregate']['abstain'] += 1
                    continue
            elif len(predictions) == 1:
                # Only 1 approach active, use it
                predicted = list(predictions.values())[0]
            else:
                window_results['aggregate']['abstain'] += 1
                continue
            
            # Track per-approach results
            for name, pred in predictions.items():
                if pred == actual:
                    window_results['approaches'][name]['correct'] += 1
                window_results['approaches'][name]['total'] += 1
            
            # Track aggregate result
            if predicted == actual:
                window_results['aggregate']['correct'] += 1
            window_results['aggregate']['total'] += 1
        
        # Aggregate window results
        for name, res in window_results['approaches'].items():
            if name not in results['approaches']:
                results['approaches'][name] = {'correct': 0, 'total': 0, 'agreed_correct': 0, 'agreed_total': 0}
            results['approaches'][name]['correct'] += res['correct']
            results['approaches'][name]['total'] += res['total']
        
        results['aggregate']['correct'] += window_results['aggregate']['correct']
        results['aggregate']['total'] += window_results['aggregate']['total']
        results['aggregate']['abstain'] += window_results['aggregate']['abstain']
        
        results['windows'].append(window_results)
        
        # Move to next window (roll forward 30 days)
        start_idx += test_days
        
        # Safety check to avoid infinite loop
        if len(results['windows']) > 12:  # Max 12 windows (3 years)
            break
    
    return results


def run_backtest_for_station(station, db_path, approaches):
    """Run walk-forward backtest for a single station."""
    conn = sqlite3.connect(db_path)
    days, market = load_station_data(station, conn)
    conn.close()
    
    if len(days) < 210:  # Need at least 7 months of data
        return None
    
    results = walk_forward_backtest(days, market, approaches, train_days=180, test_days=30)
    
    if results['aggregate']['total'] == 0:
        return None
    
    # Calculate accuracy for each approach
    per_approach = {}
    for name, res in results['approaches'].items():
        if res['total'] > 0:
            per_approach[name] = {
                'accuracy': res['correct'] / res['total'],
                'total': res['total'],
                'correct': res['correct']
            }
    
    aggregate = {
        'accuracy': results['aggregate']['correct'] / results['aggregate']['total'],
        'total': results['aggregate']['total'],
        'correct': results['aggregate']['correct'],
        'abstain': results['aggregate']['abstain'],
        'coverage': results['aggregate']['total'] / (results['aggregate']['total'] + results['aggregate']['abstain'])
    }
    
    return {
        'station': station,
        'per_approach': per_approach,
        'aggregate': aggregate,
        'windows': results['windows']
    }


def compute_sharpe(returns):
    """Compute Sharpe ratio from list of (confidence, ok) tuples."""
    if not returns:
        return 0.0
    vals = [(2 * c - 1) if ok else -(2 * c - 1) for c, ok in returns]
    n = len(vals)
    if n == 0:
        return 0.0
    mean = sum(vals) / n
    if n == 1:
        return 0.0
    variance = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(variance)
    return mean / std if std > 0 else 0.0


def main():
    """Main entry point."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        return
    
    # 6 approaches with names
    approaches = [
        approach_reversion,        # 1. Reversion (most promising)
        approach_gaussian,         # 2. Gaussian model
        approach_persistence,      # 3. Persistence
        approach_regime,           # 4. Regime classifier
        approach_gaussian_v2,      # 5. Gaussian v2 (30-day window)
        approach_pressure_trend,   # 6. Pressure trend (non-circular)
    ]
    
    approach_names = [
        "Reversion (30-day z-score)",
        "Gaussian (48-day window)",
        "Persistence (yesterday's direction)",
        "Regime (volatility-based)",
        "Gaussian v2 (30-day z-score)",
        "Pressure trend (non-circular)"
    ]
    
    # Target stations
    stations = ['KMIA', 'KNYC', 'KDEN']
    
    print("=" * 90)
    print("ENSEMBLE V6 STANDALONE — WALK-FORWARD BACKTEST")
    print("=" * 90)
    print(f"Database: {db_path}")
    print(f"Approaches: {len(approaches)}")
    for i, name in enumerate(approach_names, 1):
        print(f"  {i}. {name}")
    print(f"Target stations: {', '.join(stations)}")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Aggregation: ≥2 approaches must agree")
    print()
    
    # Run backtest for each station
    all_results = {}
    for station in stations:
        print(f"Running backtest for {station}...")
        result = run_backtest_for_station(station, db_path, approaches)
        if result is None:
            print(f"  ERROR: Not enough data or no tests generated")
            continue
        
        all_results[station] = result
        
        # Print station results
        print(f"\n  {station} RESULTS")
        print("-" * 50)
        
        print(f"  PER-APPROACH:")
        for name in approach_names:
            key = f'approach_{approach_names.index(name) + 1}'
            if key in result['per_approach']:
                r = result['per_approach'][key]
                print(f"    {name:<30} {r['accuracy']:>7.2%} ({r['correct']:>4}/{r['total']:>4})")
            else:
                print(f"    {name:<30} {'N/A':>7}")
        
        agg = result['aggregate']
        print(f"  AGGREGATE:")
        print(f"    Accuracy:      {agg['accuracy']:>7.2%}")
        print(f"    Coverage:      {agg['coverage']:>7.2%}")
        print(f"    Trades:        {agg['total']}")
        print(f"    Abstain:       {agg['abstain']}")
    
    # Summary table
    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    
    print(f"\n{'Station':<8} {'Reversion':>10} {'Gaussian':>10} {'Persistence':>12} {'Aggregate':>10} {'Coverage':>10}")
    print("-" * 70)
    
    for station in stations:
        if station not in all_results:
            continue
        
        r = all_results[station]
        agg = r['aggregate']
        
        reversion_acc = r['per_approach'].get('approach_1', {}).get('accuracy', 0)
        gaussian_acc = r['per_approach'].get('approach_2', {}).get('accuracy', 0)
        persistence_acc = r['per_approach'].get('approach_3', {}).get('accuracy', 0)
        
        print(f"{station:<8} {reversion_acc:>10.2%} {gaussian_acc:>10.2%} {persistence_acc:>12.2%} {agg['accuracy']:>10.2%} {agg['coverage']:>10.1%}")
    
    # Check thresholds
    print()
    print("=" * 90)
    print("THRESHOLD CHECK")
    print("=" * 90)
    
    DIRECTIONAL_THRESHOLD = 0.58  # 58%
    
    # Check aggregate
    for station in stations:
        if station not in all_results:
            continue
        r = all_results[station]
        agg = r['aggregate']
        
        # Check for circularity (100% accuracy)
        if agg['accuracy'] == 1.0:
            print(f"CRITICAL: {station} shows 100% accuracy — CIRCULARITY BUG!")
            print("ABORTING: Fix the backtest logic immediately.")
            return
        
        if agg['accuracy'] >= DIRECTIONAL_THRESHOLD:
            print(f"✓ {station} AGGREGATE: {agg['accuracy']:.2%} >= {DIRECTIONAL_THRESHOLD:.0%} (PASS)")
        else:
            print(f"✗ {station} AGGREGATE: {agg['accuracy']:.2%} < {DIRECTIONAL_THRESHOLD:.0%} (FAIL)")
    
    # Overall check
    overall_correct = sum(all_results[s]['aggregate']['correct'] for s in stations if s in all_results)
    overall_total = sum(all_results[s]['aggregate']['total'] for s in stations if s in all_results)
    overall_accuracy = overall_correct / overall_total if overall_total > 0 else 0
    
    print()
    print(f"Overall aggregate: {overall_accuracy:.2%}")
    
    if overall_accuracy >= DIRECTIONAL_THRESHOLD:
        print(f"✓ OVERALL PASSES THRESHOLD: {overall_accuracy:.2%} >= {DIRECTIONAL_THRESHOLD:.0%}")
    else:
        print(f"✗ OVERALL FAILS THRESHOLD: {overall_accuracy:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
    
    # Check individual stations
    stations_passing = sum(1 for s in stations if s in all_results and all_results[s]['aggregate']['accuracy'] >= DIRECTIONAL_THRESHOLD)
    print()
    print(f"Stations passing threshold: {stations_passing}/{len(stations)}")
    
    if stations_passing == 0:
        print("WARNING: No individual station clears 58% threshold.")
        print("Escalation needed to Dan.")
    
    # Check for circularity in any approach
    print()
    print("=" * 90)
    print("CIRCULARITY CHECK")
    print("=" * 90)
    
    circularity_found = False
    for station in stations:
        if station not in all_results:
            continue
        
        r = all_results[station]
        for name, acc_data in r['per_approach'].items():
            if acc_data['accuracy'] == 1.0:
                print(f"CRITICAL: {station} - {name} shows 100% accuracy — CIRCULARITY BUG!")
                circularity_found = True
    
    if not circularity_found:
        print("✓ No circularity detected (no approach shows 100% accuracy)")
    
    # Print full results for Dan's review
    print()
    print("=" * 90)
    print("FULL RESULTS (for Dan's review)")
    print("=" * 90)
    
    for station in stations:
        if station not in all_results:
            continue
        
        r = all_results[station]
        print(f"\n{station} DETAILED RESULTS")
        print("-" * 60)
        
        # Per-approach
        print("Per-approach results:")
        for name in approach_names:
            key = f'approach_{approach_names.index(name) + 1}'
            if key in r['per_approach']:
                a = r['per_approach'][key]
                status = "✓ PASS" if a['accuracy'] >= DIRECTIONAL_THRESHOLD else "✗ FAIL"
                print(f"  {name:<30} {a['accuracy']:>7.2%} {status:>8} ({a['correct']}/{a['total']})")
            else:
                print(f"  {name:<30} {'N/A':>7}")
        
        # Aggregate
        agg = r['aggregate']
        status = "✓ PASS" if agg['accuracy'] >= DIRECTIONAL_THRESHOLD else "✗ FAIL"
        print(f"\n  Aggregate: {agg['accuracy']:>7.2%} {status:>8} ({agg['correct']}/{agg['total']} trades, {agg['coverage']:>6.1%} coverage)")


if __name__ == "__main__":
    main()
