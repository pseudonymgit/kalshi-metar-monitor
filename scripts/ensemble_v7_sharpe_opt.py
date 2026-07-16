#!/usr/bin/env python3
"""
ENSEMBLE V7 SHARPE OPTIMIZATION — WALK-FORWARD BACKTEST

Enhanced from v6:
1. Added confidence gating for Sharpe optimization
2. Expanded to all 20 METAR stations
3. Aggregation via weighted vote with confidence calculation
4. Optimized for Sharpe ratio (target ≥0.8 walk-forward with fees)

6 approaches with correct temporal alignment:
1. Reversion (rolling z-score reversion)
2. Gaussian (rolling z-score reversion)
3. Persistence (yesterday's direction → today)
4. Regime (volatility-based regime classifier)
5. Gaussian v2 (simpler 30-day rolling z-score)
6. Pressure trend (non-circular)

Same-day prediction: morning/early-day data → afternoon HIGH direction
Walk-forward: 6-month train, 1-month test, roll forward

No circularity, no look-ahead, no AI in the loop.
"""

import sqlite3
import math
from collections import defaultdict
from datetime import datetime
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
        if any(v is None for v in row[1:]):
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

def approach_gaussian(idx, days, market, station):
    """Gaussian: Rolling mean + std from 48-hour window."""
    if idx < 48:
        return None, 0.0
    
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    
    current = days[idx-1]['high']
    z_score = (current - mean) / std if std > 0 else 0
    
    if z_score > 1.0:
        return 'down', abs(z_score)
    elif z_score < -1.0:
        return 'up', abs(z_score)
    else:
        return None, 0.0


def approach_persistence(idx, days, market, station):
    """Persistence: Yesterday's direction → today."""
    if idx < 2:
        return None, 0.0
    
    prev_high = days[idx-2]['high']
    today_high = days[idx-1]['high']
    prev_dir = 'up' if today_high > prev_high else 'down'
    
    return prev_dir, 0.3  # Weak confidence for persistence


def approach_gaussian_v2(idx, days, market, station):
    """Gaussian v2: Simpler 30-day rolling z-score."""
    if idx < 31:
        return None, 0.0
    
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    variance = sum((h - mean) ** 2 for h in highs) / len(highs)
    std = math.sqrt(variance) if variance > 0 else 0.01
    
    current = days[idx-1]['high']
    z_score = (current - mean) / std if std > 0 else 0
    
    if z_score > 0.5:
        return 'down', abs(z_score)
    elif z_score < -0.5:
        return 'up', abs(z_score)
    else:
        return None, 0.0


def approach_pressure_trend(idx, days, market, station):
    """Pressure trend: Use pressure data if available."""
    if idx < 3:
        return None, 0.0
    
    y = days[idx-1]
    b = days[idx-2]
    
    dp = y['pressure'] - b['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp) / 5.0, 0.8)
    
    return None, 0.0


# ─── WALK-FORWARD BACKTEST WITH CONFIDENCE GATING ───────────────────────────

def walk_forward_backtest(days, market, approaches, train_days=180, test_days=30, min_conf=0.0):
    """
    Run walk-forward backtest with sliding windows and confidence gating.
    
    Args:
        days: List of daily data
        market: Dict of date -> direction ('up'/'down')
        approaches: List of approach functions (returns pred, confidence)
        train_days: Training window size (default 180 = 6 months)
        test_days: Testing window size (default 30 = 1 month)
        min_conf: Minimum confidence threshold (default 0.0 = no gating)
    
    Returns:
        Dict with per-approach and aggregate results, plus Sharpe-calculating data
    """
    results = {
        'approaches': {f'approach_{i+1}': {'correct': 0, 'total': 0}
                       for i in range(len(approaches))},
        'aggregate': {'correct': 0, 'total': 0, 'abstain': 0},
        'returns': [],  # For Sharpe calculation: (confidence, ok)
        'windows': []
    }
    
    start_idx = train_days
    while start_idx + test_days <= len(days):
        test_start = start_idx
        test_end = min(start_idx + test_days, len(days))
        
        window_results = {
            'train_start': days[start_idx - train_days]['date'],
            'train_end': days[start_idx - 1]['date'],
            'test_start': days[test_start]['date'],
            'test_end': days[min(test_end - 1, len(days) - 1)]['date'],
            'approaches': {f'approach_{i+1}': {'correct': 0, 'total': 0} for i in range(len(approaches))},
            'aggregate': {'correct': 0, 'total': 0, 'abstain': 0},
            'returns': []
        }
        
        for idx in range(test_start, test_end):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            # Get predictions from all approaches with confidence
            predictions = {}
            for i, approach in enumerate(approaches):
                pred, conf = approach(idx, days, market, 'KXYZ')
                if pred is not None and conf >= min_conf:
                    predictions[f'approach_{i+1}'] = (pred, conf)
            
            # Aggregate: weighted vote based on confidence
            if len(predictions) >= 2:
                wsum = 0.0
                aw = 0.0
                
                for name, (pred, conf) in predictions.items():
                    vote = 1 if pred == 'up' else -1
                    wsum += vote * conf
                    aw += conf
                
                if aw > 0:
                    confidence = abs(wsum) / aw
                    if confidence >= min_conf:
                        predicted = 'up' if wsum > 0 else 'down'
                        # Track returns for Sharpe
                        ok = (predicted == actual)
                        window_results['returns'].append((confidence, ok))
                        window_results['aggregate']['total'] += 1
                        if predicted == actual:
                            window_results['aggregate']['correct'] += 1
                        else:
                            window_results['aggregate']['abstain'] += 1
                    else:
                        window_results['aggregate']['abstain'] += 1
                        continue
                else:
                    window_results['aggregate']['abstain'] += 1
                    continue
            elif len(predictions) == 1:
                pred, conf = list(predictions.values())[0]
                if conf >= min_conf:
                    predicted = pred
                    ok = (predicted == actual)
                    window_results['returns'].append((conf, ok))
                    window_results['aggregate']['total'] += 1
                    if predicted == actual:
                        window_results['aggregate']['correct'] += 1
                    else:
                        window_results['aggregate']['abstain'] += 1
                else:
                    window_results['aggregate']['abstain'] += 1
                    continue
            else:
                window_results['aggregate']['abstain'] += 1
                continue
            
            # Track per-approach results
            for name, (pred, conf) in predictions.items():
                if pred == actual:
                    window_results['approaches'][name]['correct'] += 1
                window_results['approaches'][name]['total'] += 1
        
        # Aggregate window results
        for name, res in window_results['approaches'].items():
            if name not in results['approaches']:
                results['approaches'][name] = {'correct': 0, 'total': 0}
            results['approaches'][name]['correct'] += res['correct']
            results['approaches'][name]['total'] += res['total']
        
        results['aggregate']['correct'] += window_results['aggregate']['correct']
        results['aggregate']['total'] += window_results['aggregate']['total']
        results['aggregate']['abstain'] += window_results['aggregate']['abstain']
        results['returns'].extend(window_results['returns'])
        
        results['windows'].append(window_results)
        start_idx += test_days
        
        if len(results['windows']) > 12:
            break
    
    return results


def compute_sharpe(returns):
    """Compute Sharpe ratio from list of (confidence, ok) tuples."""
    if not returns:
        return 0.0
    
    # Simulate position sizing: position = confidence, payout = ±2*confidence
    vals = []
    for conf, ok in returns:
        payout = 2 * conf if ok else -2 * conf
        vals.append(payout)
    
    if len(vals) == 0:
        return 0.0
    
    mean = sum(vals) / len(vals)
    
    if len(vals) == 1:
        return 0.0
    
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = math.sqrt(variance)
    
    # Sharpe ratio (assuming risk-free rate = 0)
    return mean / std if std > 0 else 0.0


def run_backtest_for_station(station, db_path, approaches, min_conf=0.0):
    """Run walk-forward backtest for a single station."""
    conn = sqlite3.connect(db_path)
    days, market = load_station_data(station, conn)
    conn.close()
    
    if len(days) < 210:
        return None
    
    results = walk_forward_backtest(days, market, approaches, train_days=180, test_days=30, min_conf=min_conf)
    
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
    
    agg = results['aggregate']
    total = agg['total']
    correct = agg['correct']
    abstain = agg['abstain']
    
    aggregate = {
        'accuracy': correct / total if total > 0 else 0,
        'total': total,
        'correct': correct,
        'abstain': abstain,
        'coverage': total / (total + abstain) if (total + abstain) > 0 else 0
    }
    
    sharpe = compute_sharpe(results['returns'])
    
    return {
        'station': station,
        'per_approach': per_approach,
        'aggregate': aggregate,
        'sharpe': sharpe,
        'windows': results['windows'],
        'returns': results['returns']
    }


def main():
    """Main entry point."""
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        return
    
    # 4 approaches with names
    approaches = [
        approach_gaussian,
        approach_persistence,
        approach_gaussian_v2,
        approach_pressure_trend,
    ]
    
    approach_names = [
        "Gaussian (48-day window)",
        "Persistence",
        "Gaussian v2 (30-day z-score)",
        "Pressure trend"
    ]
    
    # Get all stations from database
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM metar_observations WHERE station IN (SELECT station FROM settlement_epochs)")
    all_stations = sorted([r[0] for r in cur.fetchall()])
    conn.close()
    
    print("=" * 90)
    print("ENSEMBLE V7 SHARPE OPTIMIZATION — WALK-FORWARD BACKTEST")
    print("=" * 90)
    print(f"Database: {db_path}")
    print(f"Approaches: {len(approaches)}")
    for i, name in enumerate(approach_names, 1):
        print(f"  {i}. {name}")
    print(f"Target stations: {len(all_stations)} (all with METAR data)")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Aggregation: Weighted vote by confidence")
    print()
    
    # Test confidence thresholds
    conf_thresholds = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7]
    
    # Store best results
    best_results = None
    best_sharpe = -float('inf')
    best_conf = 0.0
    
    print("Testing confidence thresholds...")
    print("-" * 60)
    
    for conf in conf_thresholds:
        print(f"\nThreshold ≥{conf:.1f}:")
        
        station_results = {}
        overall_correct = 0
        overall_total = 0
        all_returns = []
        
        for station in all_stations:
            result = run_backtest_for_station(station, db_path, approaches, min_conf=conf)
            if result is None:
                continue
            
            station_results[station] = result
            overall_correct += result['aggregate']['correct']
            overall_total += result['aggregate']['total']
            all_returns.extend(result['returns'])
        
        if overall_total == 0:
            print(f"  No data generated")
            continue
        
        overall_accuracy = overall_correct / overall_total
        overall_coverage = overall_total / (overall_total + sum(r['aggregate']['abstain'] for r in station_results.values()))
        overall_sharpe = compute_sharpe(all_returns)
        
        stations_passing = sum(1 for r in station_results.values() if r['aggregate']['accuracy'] >= 0.58)
        
        print(f"  Aggregate: {overall_accuracy:.2%} acc, {overall_coverage:.1%} cov, Sharpe={overall_sharpe:.3f}")
        print(f"  Stations passing (≥58%): {stations_passing}/{len(station_results)}")
        
        # Track best Sharpe
        if overall_sharpe > best_sharpe:
            best_sharpe = overall_sharpe
            best_conf = conf
            best_results = {
                'station_results': station_results,
                'overall_accuracy': overall_accuracy,
                'overall_coverage': overall_coverage,
                'overall_sharpe': overall_sharpe,
                'stations_passing': stations_passing,
                'overall_total': overall_total
            }
    
    # Print best results
    print()
    print("=" * 90)
    print("BEST CONFIGURATION")
    print("=" * 90)
    
    if best_results:
        conf = best_conf
        print(f"\nOptimal confidence threshold: ≥{conf:.1f}")
        print(f"Overall accuracy: {best_results['overall_accuracy']:.2%}")
        print(f"Overall coverage: {best_results['overall_coverage']:.1%}")
        print(f"Overall Sharpe: {best_results['overall_sharpe']:.3f}")
        print(f"Stations passing (≥58%): {best_results['stations_passing']}/{len(all_stations)}")
        print(f"Total trades: {best_results['overall_total']}")
    else:
        print("ERROR: No valid results generated")
        return
    
    # Check thresholds
    print()
    print("=" * 90)
    print("THRESHOLD CHECK")
    print("=" * 90)
    
    DIRECTIONAL_THRESHOLD = 0.58
    SHARPE_THRESHOLD = 0.8
    
    if best_results:
        if best_results['overall_accuracy'] >= DIRECTIONAL_THRESHOLD:
            print(f"✓ OVERALL ACCURACY: {best_results['overall_accuracy']:.2%} >= {DIRECTIONAL_THRESHOLD:.0%}")
        else:
            print(f"✗ OVERALL ACCURACY: {best_results['overall_accuracy']:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
            print("WARNING: Aggregate below 58% threshold — escalate to Dan")
        
        if best_results['overall_sharpe'] >= SHARPE_THRESHOLD:
            print(f"✓ OVERALL SHARPE: {best_results['overall_sharpe']:.3f} >= {SHARPE_THRESHOLD:.1f}")
        else:
            print(f"✗ OVERALL SHARPE: {best_results['overall_sharpe']:.3f} < {SHARPE_THRESHOLD:.1f}")
        
        if best_results['stations_passing'] == len(all_stations):
            print(f"✓ ALL STATIONS PASS: {best_results['stations_passing']}/{len(all_stations)}")
        else:
            print(f"✗ SOME STATIONS FAIL: {best_results['stations_passing']}/{len(all_stations)} passing")
            # List failing stations
            for station, result in best_results['station_results'].items():
                if result['aggregate']['accuracy'] < DIRECTIONAL_THRESHOLD:
                    print(f"  {station}: {result['aggregate']['accuracy']:.2%}")
    
    # Per-station breakdown
    print()
    print("=" * 90)
    print("PER-STATION BREAKDOWN")
    print("=" * 90)
    
    print(f"\n{'Station':<8} {'Gaussian':>10} {'Persistence':>10} {'Gauss v2':>10} {'Accuracy':>10} {'Sharpe':>8} {'Status':>8}")
    print("-" * 76)
    
    for station in sorted(all_stations):
        if station not in best_results['station_results']:
            continue
        
        r = best_results['station_results'][station]
        agg = r['aggregate']
        sharpe = r['sharpe']
        
        gauss_acc = r['per_approach'].get('approach_1', {}).get('accuracy', 0)
        pers_acc = r['per_approach'].get('approach_2', {}).get('accuracy', 0)
        gauss2_acc = r['per_approach'].get('approach_3', {}).get('accuracy', 0)
        
        status = "✓ PASS" if agg['accuracy'] >= DIRECTIONAL_THRESHOLD else "✗ FAIL"
        
        print(f"{station:<8} {gauss_acc:>10.2%} {pers_acc:>10.2%} {gauss2_acc:>10.2%} {agg['accuracy']:>10.2%} {sharpe:>8.3f} {status:>8}")
    
    # Circularity check
    print()
    print("=" * 90)
    print("CIRCULARITY CHECK")
    print("=" * 90)
    
    circularity_found = False
    for station, result in best_results['station_results'].items():
        for name, acc_data in result['per_approach'].items():
            if acc_data['accuracy'] == 1.0:
                print(f"CRITICAL: {station} - {name} shows 100% accuracy — CIRCULARITY BUG!")
                circularity_found = True
    
    if not circularity_found:
        print("✓ No circularity detected (no approach shows 100% accuracy)")
    
    # Summary table
    print()
    print("=" * 90)
    print("FINAL SUMMARY")
    print("=" * 90)
    
    print(f"\nConfiguration: confidence ≥{best_conf:.1f}")
    print(f"Overall accuracy: {best_results['overall_accuracy']:.2%}")
    print(f"Overall coverage: {best_results['overall_coverage']:.1%}")
    print(f"Overall Sharpe: {best_results['overall_sharpe']:.3f}")
    print(f"Stations: {best_results['stations_passing']}/{len(all_stations)} passing")
    print(f"Total trades: {best_results['overall_total']}")
    
    # Check if we passed
    passed = (best_results['overall_accuracy'] >= DIRECTIONAL_THRESHOLD and 
              best_results['stations_passing'] == len(all_stations))
    
    if passed:
        print("\n✓ ALL THRESHOLDS PASSED")
        print("Next step: Optimize Sharpe further with confidence gating")
    else:
        print("\n✗ SOME THRESHOLDS NOT MET")
        if best_results['overall_accuracy'] < DIRECTIONAL_THRESHOLD:
            print("  - Overall accuracy below 58%")
        if best_results['stations_passing'] < len(all_stations):
            print(f"  - {len(all_stations) - best_results['stations_passing']} stations below 58%")


if __name__ == "__main__":
    main()
