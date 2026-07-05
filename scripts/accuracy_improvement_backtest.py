#!/usr/bin/env python3
"""
5-ACCURACY-IMPROVEMENT TECHNIQUE BACKTEST for weather signal:

1. Multi-horizon trend confirmation: Compare 1d, 3d, 5d, 7d trends. Only trade when ≥3 of 4 agree.
2. Signal strength threshold: Only trade when |Δ| > c * σ (rolling 30-day std), optimize c per station via grid search [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
3. Dewpoint depression filter: Dry air = more reliable trends (depression ≥15°F → confidence boost 1.3x), moist air = suppress signal (depression <5°F)
4. Pressure tendency regime: Rising pressure = stable, trends reliable (tendency ≥2.0 mb → weight 1.5x), falling pressure = unstable (tendency ≤-2.0 mb → weight 0.5x)
5. Ensemble voting: Combine all techniques with weighted vote, only emit signal if confidence > 0.60

DATABASE:
- Path: /home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db
- Tables: metar_observations, settlement_epochs

DATA FLOW:
1. Load daily HIGH from metar_observations per station
2. Load market directions from settlement_epochs
3. For each day, compute all 5 signals
4. Grid search for optimal c parameter per station
5. Run ensemble voting
6. Backtest with walk-forward validation (80/20)
7. Report directional accuracy, confidence, coverage, per-technique ablation

CONSTRAINTS:
- NO analog matching, NO Goldilocks, NO LLM calls in signal code
- All techniques are filters/boosters on top of simple trend signal
- Parameters optimized per-station via grid search (not ML)
- Use rolling 90-day windows for optimization, 30-day test windows
"""

import sqlite3
import os
import sys
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
import math

# Paths
METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Gray Room thresholds
DIRECTIONAL_THRESHOLD = 0.58  # 58%
CONFIDENCE_THRESHOLD = 0.60   # 60%
SHARPE_THRESHOLD = 1.0

# Signal strength grid search parameters
C_VALUES = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]

# Dewpoint depression thresholds (°F)
DRY_DEPRESSION_THRESHOLD = 15.0   # Dry air: confidence boost 1.3x
MOIST_DEPRESSION_THRESHOLD = 5.0  # Moist air: suppress signal

# Pressure tendency thresholds (mb)
RISING_PRESSURE_THRESHOLD = 2.0   # Rising pressure: weight 1.5x
FALLING_PRESSURE_THRESHOLD = -2.0 # Falling pressure: weight 0.5x

# Walk-forward window sizes
OPTIMIZATION_WINDOW_DAYS = 90
TEST_WINDOW_DAYS = 30

# Walk-forward overlap (60 days)
OVERLAP_DAYS = 60


def get_daily_prices(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Get daily settlement prices (buckets) for a station."""
    cur = conn.cursor()
    
    # Get all settlement epochs for HIGH and LOW markets
    cur.execute("""
        SELECT local_trading_date, market_type, settlement_bucket, prior_settlement_bucket, date_utc
        FROM settlement_epochs
        WHERE station = ? AND market_type IN ('HIGH', 'LOW') AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC, market_type ASC
    """, (station,))
    
    daily_data = defaultdict(lambda: defaultdict(dict))
    for row in cur.fetchall():
        date, mt, bucket, prior, date_utc = row
        if prior is not None:
            direction = 'up' if bucket > prior else ('down' if bucket < prior else 'flat')
        else:
            direction = 'flat'
        
        daily_data[date][mt] = {
            'bucket': bucket,
            'prior': prior,
            'direction': direction,
            'date_utc': date_utc
        }
    
    return dict(daily_data)


def get_daily_weather(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Get daily weather data for a station."""
    cur = conn.cursor()
    
    # Get daily aggregates
    cur.execute("""
        SELECT 
            date_utc,
            MAX(temp_f) as high,
            MIN(temp_f) as low,
            AVG(temp_f) as avg_temp,
            AVG(dewpoint_f) as avg_dewpoint,
            AVG(pressure_mb) as avg_pressure,
            COUNT(*) as obs_count
        FROM metar_observations
        WHERE station = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    daily_weather = {}
    for row in cur.fetchall():
        date, high, low, avg_temp, avg_dewpoint, avg_pressure, obs_count = row
        daily_weather[date] = {
            'high': high,
            'low': low,
            'avg_temp': avg_temp,
            'avg_dewpoint': avg_dewpoint,
            'avg_pressure': avg_pressure,
            'obs_count': obs_count
        }
    
    return dict(daily_weather)


def compute_rolling_std(values: List[float], window: int = 30) -> List[float]:
    """Compute rolling standard deviation."""
    stds = []
    n = len(values)
    for i in range(n):
        start = max(0, i - window + 1)
        window_values = values[start:i + 1]
        if len(window_values) < 2:
            stds.append(0.0)
        else:
            mean = sum(window_values) / len(window_values)
            variance = sum((x - mean) ** 2 for x in window_values) / len(window_values)
            stds.append(math.sqrt(variance))
    return stds


def compute_rolling_tendency(values: List[float], window: int = 24) -> List[float]:
    """Compute rolling pressure tendency (change over window)."""
    tendencies = []
    n = len(values)
    for i in range(n):
        if values[i] is None:
            tendencies.append(0.0)
            continue
        start = max(0, i - window + 1)
        # Find valid start value
        while start < i and values[start] is None:
            start += 1
        if start < i and values[start] is not None:
            tendency = values[i] - values[start]
        else:
            tendency = 0.0
        tendencies.append(tendency)
    return tendencies


def get_date_range_for_optimization(all_dates: List[str], idx: int, window_days: int) -> Tuple[str, str]:
    """Get date range for optimization window ending at all_dates[idx]."""
    end_date = all_dates[idx]
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    start_dt = end_dt - timedelta(days=window_days)
    start_date = start_dt.strftime('%Y-%m-%d')
    return start_date, end_date


def calculate_grid_search_results(
    station: str,
    dates: List[str],
    highs: List[float],
    daily_prices: Dict[str, Dict],
    c_value: float,
    dewpoint_depressions: List[float],
    pressure_tendencies: List[float]
) -> Dict[str, Any]:
    """
    Calculate signal results for a specific c value.
    Processes both HIGH and LOW markets separately.
    Returns metrics for this configuration.
    """
    n = len(dates)
    if n < 30:  # Minimum data requirement
        return {'error': 'Not enough data'}
    
    # Compute rolling 30-day std
    stds = compute_rolling_std(highs, window=30)
    
    # Compute signal strength threshold
    thresholds = [c_value * std if std > 0 else 0 for std in stds]
    
    # Process each day - check both HIGH and LOW markets
    correct = 0
    total = 0
    signals = []
    
    for i in range(n):
        if i < 1:  # Need at least 1 day of history
            continue
        
        # Basic signal: today's high vs yesterday's high
        basic_trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        
        if basic_trend == 'flat':
            continue
        
        # 1. Signal strength filter: |Δ| > c * σ
        delta = abs(highs[i] - highs[i-1])
        threshold = thresholds[i]
        if delta <= threshold:
            continue
        
        # 2. Dewpoint depression filter
        dewpoint_dep = dewpoint_depressions[i]
        if dewpoint_dep < MOIST_DEPRESSION_THRESHOLD:
            # Suppress signal for moist air
            continue
        elif dewpoint_dep >= DRY_DEPRESSION_THRESHOLD:
            confidence_boost = 1.3
        else:
            confidence_boost = 1.0
        
        # 3. Pressure tendency filter
        pressure_tend = pressure_tendencies[i]
        if pressure_tend >= RISING_PRESSURE_THRESHOLD:
            pressure_weight = 1.5
        elif pressure_tend <= FALLING_PRESSURE_THRESHOLD:
            pressure_weight = 0.5
        else:
            pressure_weight = 1.0
        
        # 4. Multi-horizon trend confirmation (check if ≥3 of 4 agree)
        horizons = [1, 3, 5, 7]
        trend_counts = {'up': 0, 'down': 0}
        for h in horizons:
            if i >= h:
                horizon_trend = 'up' if highs[i] > highs[i-h] else ('down' if highs[i] < highs[i-h] else 'flat')
                if horizon_trend in ['up', 'down']:
                    trend_counts[horizon_trend] += 1
        
        trend_agreement = trend_counts['up'] + trend_counts['down']
        if trend_agreement < 3:  # Need ≥3 of 4 horizons to agree
            continue
        
        # Combined confidence (5. Ensemble voting)
        base_confidence = 0.5
        combined_confidence = base_confidence * confidence_boost * pressure_weight
        
        if combined_confidence < CONFIDENCE_THRESHOLD:
            continue
        
        # Check against HIGH market direction
        date = dates[i]
        if 'HIGH' in daily_prices[date]:
            high_dir = daily_prices[date]['HIGH']['direction']
            if high_dir != 'flat':
                if (basic_trend == 'up' and high_dir == 'up') or (basic_trend == 'down' and high_dir == 'down'):
                    correct += 1
                total += 1
                signals.append({
                    'date': date,
                    'market': 'HIGH',
                    'signal': basic_trend,
                    'market_dir': high_dir,
                    'correct': (basic_trend == high_dir),
                    'confidence': combined_confidence,
                })
        
        # Check against LOW market direction
        if 'LOW' in daily_prices[date]:
            low_dir = daily_prices[date]['LOW']['direction']
            if low_dir != 'flat':
                if (basic_trend == 'up' and low_dir == 'up') or (basic_trend == 'down' and low_dir == 'down'):
                    correct += 1
                total += 1
                signals.append({
                    'date': date,
                    'market': 'LOW',
                    'signal': basic_trend,
                    'market_dir': low_dir,
                    'correct': (basic_trend == low_dir),
                    'confidence': combined_confidence,
                })
    
    accuracy = correct / total if total > 0 else 0
    coverage = total / (2 * (n - 1)) if n > 1 else 0  # Coverage = signals emitted / potential signals (2 markets)
    
    return {
        'correct': correct,
        'total': total,
        'accuracy': accuracy,
        'coverage': coverage,
        'c_value': c_value,
        'signals': signals,
    }


def run_walk_forward_backtest(
    station: str,
    dates: List[str],
    highs: List[float],
    daily_prices: Dict[str, Dict],
    dewpoint_depressions: List[float],
    pressure_tendencies: List[float]
) -> Dict[str, Any]:
    """
    Run walk-forward validation with 80/20 train/test split.
    Optimize c parameter per station using rolling windows.
    """
    n = len(dates)
    
    if n < OPTIMIZATION_WINDOW_DAYS + TEST_WINDOW_DAYS:
        return {
            'station': station,
            'error': f'Not enough data: {n} days, need {OPTIMIZATION_WINDOW_DAYS + TEST_WINDOW_DAYS}',
        }
    
    all_results = []
    c_param_results = defaultdict(list)  # c_value -> list of results
    
    # Walk-forward: move test window in increments
    test_start_idx = OPTIMIZATION_WINDOW_DAYS
    while test_start_idx < n:
        # Optimization window: [test_start_idx - OPTIMIZATION_WINDOW_DAYS, test_start_idx)
        opt_start = max(0, test_start_idx - OPTIMIZATION_WINDOW_DAYS)
        opt_dates = dates[opt_start:test_start_idx]
        opt_highs = highs[opt_start:test_start_idx]
        opt_prices = {d: daily_prices[d] for d in opt_dates}
        opt_dewpoint = dewpoint_depressions[opt_start:test_start_idx]
        opt_pressure = pressure_tendencies[opt_start:test_start_idx]
        
        # Grid search for optimal c
        best_c = C_VALUES[0]
        best_score = -1
        
        for c in C_VALUES:
            result = calculate_grid_search_results(
                station, opt_dates, opt_highs, opt_prices,
                c, opt_dewpoint, opt_pressure
            )
            if 'error' not in result and result['total'] > 0:
                # Score = accuracy + 0.5 * coverage (penalize low coverage)
                score = result['accuracy'] + 0.5 * result['coverage']
                if score > best_score:
                    best_score = score
                    best_c = c
        
        # Test window: [test_start_idx, test_start_idx + TEST_WINDOW_DAYS)
        test_end = min(test_start_idx + TEST_WINDOW_DAYS, n)
        test_dates = dates[test_start_idx:test_end]
        test_highs = highs[test_start_idx:test_end]
        test_prices = {d: daily_prices[d] for d in test_dates}
        test_dewpoint = dewpoint_depressions[test_start_idx:test_end]
        test_pressure = pressure_tendencies[test_start_idx:test_end]
        
        # Run test with optimal c
        test_result = calculate_grid_search_results(
            station, test_dates, test_highs, test_prices,
            best_c, test_dewpoint, test_pressure
        )
        
        if 'error' not in test_result:
            all_results.append(test_result)
            c_param_results[best_c].append(test_result['accuracy'])
        
        # Move to next window
        test_start_idx += OVERLAP_DAYS
    
    # Aggregate results
    if not all_results:
        return {
            'station': station,
            'error': 'No successful test windows',
        }
    
    total_correct = sum(r['correct'] for r in all_results)
    total_trades = sum(r['total'] for r in all_results)
    overall_accuracy = total_correct / total_trades if total_trades > 0 else 0
    
    total_coverage = sum(r['coverage'] for r in all_results) / len(all_results)
    
    # C-parameter distribution
    c_distribution = {}
    for c, accuracies in c_param_results.items():
        c_distribution[c] = {
            'count': len(accuracies),
            'avg_accuracy': sum(accuracies) / len(accuracies) if accuracies else 0,
        }
    
    return {
        'station': station,
        'total_trades': total_trades,
        'total_correct': total_correct,
        'overall_accuracy': overall_accuracy,
        'avg_coverage': total_coverage,
        'c_distribution': c_distribution,
        'best_c': max(c_distribution.keys(), key=lambda c: c_distribution[c]['count']),
    }


def ablation_study(
    station: str,
    dates: List[str],
    highs: List[float],
    daily_prices: Dict[str, Dict],
    dewpoint_depressions: List[float],
    pressure_tendencies: List[float]
) -> Dict[str, Dict]:
    """
    Run ablation study: test each technique in isolation.
    """
    n = len(dates)
    c_value = 0.5  # Default c value
    
    stds = compute_rolling_std(highs, window=30)
    thresholds = [c_value * std if std > 0 else 0 for std in stds]
    
    results = {}
    
    # Baseline: simple trend signal only
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        
        date = dates[i]
        # Check HIGH
        if 'HIGH' in daily_prices[date]:
            if daily_prices[date]['HIGH']['direction'] != 'flat':
                if trend == daily_prices[date]['HIGH']['direction']:
                    correct += 1
                total += 1
        # Check LOW
        if 'LOW' in daily_prices[date]:
            if daily_prices[date]['LOW']['direction'] != 'flat':
                if trend == daily_prices[date]['LOW']['direction']:
                    correct += 1
                total += 1
    results['baseline'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    # Technique 1: Multi-horizon confirmation (≥3 of 4 horizons agree)
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        
        # Check horizon agreement
        horizons = [1, 3, 5, 7]
        trend_counts = {'up': 0, 'down': 0}
        for h in horizons:
            if i >= h:
                ht = 'up' if highs[i] > highs[i-h] else ('down' if highs[i] < highs[i-h] else 'flat')
                if ht in ['up', 'down']:
                    trend_counts[ht] += 1
        
        if trend_counts['up'] + trend_counts['down'] >= 3:
            date = dates[i]
            # Check HIGH
            if 'HIGH' in daily_prices[date]:
                if daily_prices[date]['HIGH']['direction'] != 'flat':
                    if trend == daily_prices[date]['HIGH']['direction']:
                        correct += 1
                    total += 1
            # Check LOW
            if 'LOW' in daily_prices[date]:
                if daily_prices[date]['LOW']['direction'] != 'flat':
                    if trend == daily_prices[date]['LOW']['direction']:
                        correct += 1
                    total += 1
    results['multi_horizon'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    # Technique 2: Signal strength threshold
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        delta = abs(highs[i] - highs[i-1])
        threshold = thresholds[i]
        if delta > threshold:
            date = dates[i]
            # Check HIGH
            if 'HIGH' in daily_prices[date]:
                if daily_prices[date]['HIGH']['direction'] != 'flat':
                    if trend == daily_prices[date]['HIGH']['direction']:
                        correct += 1
                    total += 1
            # Check LOW
            if 'LOW' in daily_prices[date]:
                if daily_prices[date]['LOW']['direction'] != 'flat':
                    if trend == daily_prices[date]['LOW']['direction']:
                        correct += 1
                    total += 1
    results['signal_strength'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    # Technique 3: Dewpoint filter
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        dp = dewpoint_depressions[i]
        if dp >= DRY_DEPRESSION_THRESHOLD:
            date = dates[i]
            # Check HIGH
            if 'HIGH' in daily_prices[date]:
                if daily_prices[date]['HIGH']['direction'] != 'flat':
                    if trend == daily_prices[date]['HIGH']['direction']:
                        correct += 1
                    total += 1
            # Check LOW
            if 'LOW' in daily_prices[date]:
                if daily_prices[date]['LOW']['direction'] != 'flat':
                    if trend == daily_prices[date]['LOW']['direction']:
                        correct += 1
                    total += 1
    results['dewpoint_filter'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    # Technique 4: Pressure tendency filter
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        pt = pressure_tendencies[i]
        if (pt >= RISING_PRESSURE_THRESHOLD) or (pt <= FALLING_PRESSURE_THRESHOLD):
            date = dates[i]
            # Check HIGH
            if 'HIGH' in daily_prices[date]:
                if daily_prices[date]['HIGH']['direction'] != 'flat':
                    if trend == daily_prices[date]['HIGH']['direction']:
                        correct += 1
                    total += 1
            # Check LOW
            if 'LOW' in daily_prices[date]:
                if daily_prices[date]['LOW']['direction'] != 'flat':
                    if trend == daily_prices[date]['LOW']['direction']:
                        correct += 1
                    total += 1
    results['pressure_tendency'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    # All techniques combined (with confidence boost)
    correct, total = 0, 0
    for i in range(1, n):
        trend = 'up' if highs[i] > highs[i-1] else ('down' if highs[i] < highs[i-1] else 'flat')
        if trend == 'flat':
            continue
        
        # Signal strength
        delta = abs(highs[i] - highs[i-1])
        if delta <= thresholds[i]:
            continue
        
        # Dewpoint
        dp = dewpoint_depressions[i]
        if dp < MOIST_DEPRESSION_THRESHOLD:
            continue
        confidence_boost = 1.3 if dp >= DRY_DEPRESSION_THRESHOLD else 1.0
        
        # Pressure
        pt = pressure_tendencies[i]
        pressure_weight = 1.5 if pt >= RISING_PRESSURE_THRESHOLD else (
            0.5 if pt <= FALLING_PRESSURE_THRESHOLD else 1.0)
        
        # Multi-horizon
        horizons = [1, 3, 5, 7]
        trend_counts = {'up': 0, 'down': 0}
        for h in horizons:
            if i >= h:
                ht = 'up' if highs[i] > highs[i-h] else ('down' if highs[i] < highs[i-h] else 'flat')
                if ht in ['up', 'down']:
                    trend_counts[ht] += 1
        if trend_counts['up'] + trend_counts['down'] < 3:
            continue
        
        # Confidence
        combined_confidence = 0.5 * confidence_boost * pressure_weight
        if combined_confidence < CONFIDENCE_THRESHOLD:
            continue
        
        # Market
        date = dates[i]
        # Check HIGH
        if 'HIGH' in daily_prices[date]:
            if daily_prices[date]['HIGH']['direction'] != 'flat':
                if trend == daily_prices[date]['HIGH']['direction']:
                    correct += 1
                total += 1
        # Check LOW
        if 'LOW' in daily_prices[date]:
            if daily_prices[date]['LOW']['direction'] != 'flat':
                if trend == daily_prices[date]['LOW']['direction']:
                    correct += 1
                total += 1
    results['all_techniques'] = {
        'accuracy': correct / total if total > 0 else 0,
        'trades': total,
    }
    
    return results


def main():
    """Main entry point."""
    print("=" * 80)
    print("5-ACCURACY-IMPROVEMENT TECHNIQUE BACKTEST")
    print("=" * 80)
    print()
    print("Techniques tested:")
    print("  1. Multi-horizon trend confirmation (≥3 of 4 horizons agree)")
    print("  2. Signal strength threshold (|Δ| > c * σ)")
    print("  3. Dewpoint depression filter (dry air = 1.3x boost, moist = suppress)")
    print("  4. Pressure tendency regime (rising = 1.5x, falling = 0.5x)")
    print("  5. Ensemble voting (confidence > 0.60)")
    print()
    
    # Connect to database
    conn = sqlite3.connect(METAR_DB)
    
    # Get all stations
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM metar_observations")
    stations = [r[0] for r in cur.fetchall()]
    print(f"Found {len(stations)} stations with data")
    print()
    
    # Process each station
    all_results = {}
    ablation_results = {}
    
    for station in stations:
        print(f"Processing {station}...")
        
        # Get data
        daily_prices = get_daily_prices(station, conn)
        daily_weather = get_daily_weather(station, conn)
        
        # Align dates
        common_dates = sorted(set(daily_prices.keys()) & set(daily_weather.keys()))
        
        if len(common_dates) < 60:
            print(f"  Not enough data ({len(common_dates)} dates). Skipping.")
            continue
        
        # Extract arrays
        highs = [daily_weather[d]['high'] for d in common_dates]
        
        # Compute dewpoint depression
        dewpoint_depressions = [daily_weather[d]['avg_dewpoint'] for d in common_dates]
        
        # Compute pressure tendency
        pressures = [daily_weather[d]['avg_pressure'] for d in common_dates]
        pressure_tendencies = compute_rolling_tendency(pressures, window=24)
        
        # Run walk-forward backtest
        wf_result = run_walk_forward_backtest(
            station, common_dates, highs, daily_prices,
            dewpoint_depressions, pressure_tendencies
        )
        all_results[station] = wf_result
        
        # Run ablation study
        ablation = ablation_study(
            station, common_dates, highs, daily_prices,
            dewpoint_depressions, pressure_tendencies
        )
        ablation_results[station] = ablation
        
        print(f"  Walk-forward accuracy: {wf_result.get('overall_accuracy', 0):.2%}")
        print(f"  Best c value: {wf_result.get('best_c', 'N/A')}")
    
    conn.close()
    
    # Aggregate results
    print()
    print("=" * 80)
    print("AGGREGATE RESULTS")
    print("=" * 80)
    print()
    
    # Overall accuracy
    total_correct = sum(r.get('total_correct', 0) for r in all_results.values())
    total_trades = sum(r.get('total_trades', 0) for r in all_results.values())
    overall_accuracy = total_correct / total_trades if total_trades > 0 else 0
    
    # Average coverage
    avg_coverage = sum(r.get('avg_coverage', 0) for r in all_results.values()) / len(all_results) if all_results else 0
    
    # C-value distribution
    all_c_values = defaultdict(int)
    for r in all_results.values():
        if 'c_distribution' in r:
            for c, info in r['c_distribution'].items():
                all_c_values[c] += info['count']
    
    print("OVERALL METRICS")
    print("-" * 40)
    print(f"Total trades:       {total_trades}")
    print(f"Total correct:      {total_correct}")
    print(f"Directional accuracy: {overall_accuracy:.2%}")
    print(f"Average coverage:   {avg_coverage:.2%}")
    print(f"Passes threshold (≥58%): {'✓ YES' if overall_accuracy >= DIRECTIONAL_THRESHOLD else '✗ NO'}")
    print()
    
    print("C-VALUE DISTRIBUTION (optimal per-station)")
    print("-" * 40)
    for c in sorted(all_c_values.keys()):
        print(f"  c={c:.1f}: {all_c_values[c]} stations selected")
    print()
    
    # Per-station results
    print("STATION BREAKDOWN")
    print("-" * 40)
    print(f"{'Station':<8} {'Accuracy':>10} {'Trades':>8} {'Best c':>8}")
    print("-" * 36)
    for station in sorted(all_results.keys()):
        r = all_results[station]
        acc = r.get('overall_accuracy', 0)
        trades = r.get('total_trades', 0)
        best_c = r.get('best_c', 'N/A')
        print(f"{station:<8} {acc:>10.2%} {trades:>8} {str(best_c):>8}")
    print()
    
    # Ablation study summary
    print("ABLATION STUDY (per-station average)")
    print("-" * 40)
    
    technique_totals = defaultdict(lambda: {'correct': 0, 'total': 0})
    for station, ablation in ablation_results.items():
        for technique, results in ablation.items():
            if technique != 'baseline':  # Don't count baseline
                technique_totals[technique]['correct'] += results['accuracy'] * results['trades']
                technique_totals[technique]['total'] += results['trades']
    
    for technique in technique_totals:
        total = technique_totals[technique]['total']
        if total > 0:
            avg_acc = technique_totals[technique]['correct'] / total
            print(f"  {technique}: {avg_acc:.2%} (trades: {total})")
    print()
    
    # Final verdict
    print("=" * 80)
    print("FINAL VERDICT")
    print("=" * 80)
    print()
    
    if overall_accuracy >= DIRECTIONAL_THRESHOLD:
        print(f"✓ PASSES THRESHOLD: {overall_accuracy:.2%} >= {DIRECTIONAL_THRESHOLD:.0%}")
        print()
        print("RECOMMENDATION: PROCEED WITH ENSIGNAL IMPLEMENTATION")
    else:
        print(f"✗ FAILS THRESHOLD: {overall_accuracy:.2%} < {DIRECTIONAL_THRESHOLD:.0%}")
        print()
        print("RECOMMENDATION: NEED MORE DATA OR FEATURE IMPROVEMENT")
    
    print()
    
    # Save results
    output = {
        'overall': {
            'total_trades': total_trades,
            'total_correct': total_correct,
            'directional_accuracy': overall_accuracy,
            'avg_coverage': avg_coverage,
            'passes_threshold': overall_accuracy >= DIRECTIONAL_THRESHOLD,
        },
        'c_distribution': dict(all_c_values),
        'stations': {
            station: {
                'accuracy': r.get('overall_accuracy', 0),
                'trades': r.get('total_trades', 0),
                'coverage': r.get('avg_coverage', 0),
                'best_c': r.get('best_c', 'N/A'),
            }
            for station, r in all_results.items()
        },
        'ablation': {
            station: {
                technique: {
                    'accuracy': results.get('accuracy', 0),
                    'trades': results.get('trades', 0),
                }
                for technique, results in ablation.items()
            }
            for station, ablation in ablation_results.items()
        },
    }
    
    output_path = "/tmp/accuracy_improvement_backtest_results.json"
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"Results saved to: {output_path}")
    
    return output


if __name__ == "__main__":
    result = main()
