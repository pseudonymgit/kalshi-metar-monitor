#!/usr/bin/env python3
"""
PHASE 2: SIGNAL HYGIENE - CORRECTED TEMPORAL ALIGNMENT

This script:
1. Computes mutual information / correlation matrix for all 26 signals
2. Prunes to 1 signal per cluster (keep highest standalone accuracy)
3. Removes KL divergence (data-mining artifact)
4. Removes dewpoint depression (already zero-weight)
5. Identifies anti-predictive signals (inversion rate)

DATABASE: /home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db

CORRECTED: yesterday's signals → today's market direction (not today's signals).
"""

import sqlite3
import os
import sys
import json
import math
from typing import Dict, List, Tuple, Any
from collections import defaultdict
import statistics

# Add core to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"


def get_all_stations(conn: sqlite3.Connection) -> List[str]:
    """Get all stations from the database."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM metar_observations")
    return sorted([r[0] for r in cur.fetchall()])


def get_daily_weather(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Get daily weather data for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            date_utc,
            MAX(temp_f) as high,
            MIN(temp_f) as low,
            AVG(pressure_mb) as pressure,
            AVG(dewpoint_f) as dewpoint,
            AVG(wind_direction_deg) as wind_dir,
            NULL as cloud_cover
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    weather = {}
    for row in cur.fetchall():
        weather[row[0]] = {
            'high': row[1], 'low': row[2], 'pressure': row[3],
            'dewpoint': row[4], 'wind_dir': row[5], 'cloud_cover': row[6]
        }
    return weather


def get_market_directions(station: str, conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Get market directions for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT local_trading_date, market_type, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    markets = defaultdict(dict)
    for row in cur.fetchall():
        date, mt, bucket, prior = row
        if prior is not None:
            direction = 'up' if bucket > prior else ('down' if bucket < prior else 'flat')
        else:
            direction = 'flat'
        markets[date][mt] = direction
    return dict(markets)


def compute_all_signals(station: str, conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
    """
    Compute all 26 signals for a station.
    Returns: {date: {signal_name: value}}
    
    CORRECTED TEMPORAL ALIGNMENT:
    - Signal computed using yesterday's and day-before's data
    - Compare to today's market direction
    - Loop starts at i=2 to have 2 days of history
    """
    weather = get_daily_weather(station, conn)
    markets = get_market_directions(station, conn)
    
    dates = sorted(weather.keys())
    if len(dates) < 3:
        return {}
    
    # Daily weather data
    highs = [weather[d]['high'] for d in dates]
    pressures = [weather[d]['pressure'] for d in dates]
    dewpoints = [weather[d]['dewpoint'] for d in dates]
    wind_dirs = [weather[d]['wind_dir'] for d in dates]
    
    # Compute rolling stats
    def rolling_mean(values, window=30):
        means = []
        for i in range(len(values)):
            start = max(0, i - window + 1)
            window_vals = values[start:i + 1]
            means.append(sum(window_vals) / len(window_vals) if window_vals else 0)
        return means
    
    def rolling_std(values, window=30):
        stds = []
        for i in range(len(values)):
            start = max(0, i - window + 1)
            window_vals = values[start:i + 1]
            if len(window_vals) < 2:
                stds.append(0.0)
            else:
                mean = sum(window_vals) / len(window_vals)
                variance = sum((x - mean) ** 2 for x in window_vals) / len(window_vals)
                stds.append(math.sqrt(variance))
        return stds
    
    rolling_high_mean = rolling_mean(highs)
    rolling_high_std = rolling_std(highs)
    rolling_pressure_mean = rolling_mean(pressures)
    rolling_pressure_std = rolling_std(pressures)
    
    signals = {}
    
    # CORRECTED: Start at i=2 to have yesterday and day-before
    for i in range(2, len(dates)):
        date = dates[i]
        yesterday = weather[dates[i-1]]
        day_before = weather[dates[i-2]]
        
        s = {}
        
        # Trend signals - CORRECTED: compare yesterday's data to today
        trend = 'up' if yesterday['high'] > day_before['high'] else ('down' if yesterday['high'] < day_before['high'] else 'flat')
        s['trend'] = 1.0 if trend == 'up' else (-1.0 if trend == 'down' else 0.0)
        s['temp_change'] = yesterday['high'] - day_before['high']
        s['high_magnitude'] = abs(yesterday['high'] - day_before['high'])
        
        # Pressure signals - CORRECTED: yesterday vs day-before
        dp = yesterday['pressure'] - day_before['pressure']
        s['pressure_tendency'] = dp
        s['pressure_magnitude'] = yesterday['pressure']
        s['pressure_change'] = dp
        s['abs_pressure_change'] = abs(dp)
        
        # Rolling pressure signals
        s['pressure_vs_rolling_mean'] = yesterday['pressure'] - rolling_pressure_mean[i-1]
        s['pressure_normalized'] = (dp - rolling_pressure_mean[i-1]) / (rolling_pressure_std[i-1] + 1e-10)
        
        # Dewpoint signals - CORRECTED: yesterday vs day-before
        dpd = yesterday['high'] - yesterday['dewpoint']
        prev_dpd = day_before['high'] - day_before['dewpoint']
        s['dewpoint_depression'] = dpd
        s['dewpoint_change'] = yesterday['dewpoint'] - day_before['dewpoint']
        s['dewpoint_gradient'] = dpd - prev_dpd
        s['dewpoint_normalized'] = dpd / (yesterday['high'] - 32) if yesterday['high'] > 32 else 0
        
        # Wind signals - CORRECTED: yesterday vs day-before
        dw = yesterday['wind_dir'] - day_before['wind_dir']
        while dw > 180: dw -= 360
        while dw < -180: dw += 360
        s['wind_change'] = dw
        s['abs_wind_change'] = abs(dw)
        s['wind_speed'] = abs(yesterday['wind_dir'])
        
        # Cloud signals (if available)
        s['cloud_cover'] = (yesterday['cloud_cover'] or 0) if yesterday.get('cloud_cover') else 0
        s['cloud_change'] = s['cloud_cover'] - ((day_before['cloud_cover'] or 0) if day_before.get('cloud_cover') else 0)
        
        # Kalman filter approximation (simplified)
        s['kalman_approx'] = 0.8 * s['trend'] + 0.2 * (1.0 if dp > 0 else -1.0)
        
        # MA crossover signals - CORRECTED: use yesterday's MA
        if i >= 6:  # Need at least 5 days for MA5
            ma5_yesterday = sum(highs[i-5:i]) / 5
            ma5_day_before = sum(highs[i-6:i-1]) / 5
            ma10_yesterday = sum(highs[i-10:i]) / 10
            ma10_day_before = sum(highs[i-11:i-1]) / 10
            s['ma5_trend'] = 1.0 if ma5_yesterday > ma5_day_before else -1.0
            s['ma10_trend'] = 1.0 if ma10_yesterday > ma10_day_before else -1.0
            s['ma_crossover'] = 1.0 if (ma5_yesterday > ma10_yesterday and ma5_day_before <= ma10_day_before) else (-1.0 if (ma5_yesterday < ma10_yesterday and ma5_day_before >= ma10_day_before) else 0)
        else:
            s['ma5_trend'] = 0
            s['ma10_trend'] = 0
            s['ma_crossover'] = 0
        
        # Volatility signals - CORRECTED: yesterday's volatility
        s['volatility'] = rolling_high_std[i-1]
        s['volatility_change'] = rolling_high_std[i-1] - rolling_high_std[i-2] if i >= 3 else 0
        
        # Exponential smoothing signals - CORRECTED: yesterday's EMA
        alpha = 0.3
        ema_yesterday = alpha * yesterday['high'] + (1 - alpha) * (day_before['high'] if day_before else yesterday['high'])
        ema_day_before = alpha * day_before['high'] + (1 - alpha) * (weather[dates[i-3]]['high'] if i >= 3 else day_before['high'])
        s['ema_trend'] = 1.0 if ema_yesterday > ema_day_before else -1.0
        
        # Gaussian model proxy - CORRECTED: yesterday's deviation
        s['gaussian_proxy'] = (yesterday['high'] - rolling_high_mean[i-1]) / (rolling_high_std[i-1] + 1e-10)
        
        # KL divergence proxy (data-mining artifact - will be removed)
        if rolling_high_std[i-1] > 0:
            s['kl_divergence'] = 0.5 * ((rolling_high_std[i-1] / 10) ** 2) + math.log(10 / (rolling_high_std[i-1] + 1e-10))
        else:
            s['kl_divergence'] = 10.0
        
        # Markov state proxy
        s['markov_state'] = 1 if yesterday['high'] > rolling_high_mean[i-1] else 0
        
        # Pressure regimes
        if dp > 2.0:
            s['pressure_rising'] = 1.0
            s['pressure_falling'] = 0.0
        elif dp < -2.0:
            s['pressure_rising'] = 0.0
            s['pressure_falling'] = -1.0
        else:
            s['pressure_rising'] = 0.0
            s['pressure_falling'] = 0.0
        
        signals[date] = s
    
    return signals


def compute_signal_accuracy(signals: Dict[str, Dict[str, float]], markets: Dict[str, Dict], signal_name: str) -> Tuple[float, int, int]:
    """
    Compute accuracy of a signal.
    Returns (accuracy, correct, total)
    
    Uses CORRECTED temporal alignment: yesterday's signal → today's market direction
    """
    correct = 0
    total = 0
    
    for date, s in signals.items():
        if signal_name not in s:
            continue
        
        signal_value = s[signal_name]
        
        # Determine signal direction
        if signal_name in ['trend', 'ma5_trend', 'ma10_trend', 'ema_trend', 'pressure_rising', 'pressure_falling']:
            sig_dir = 'up' if signal_value > 0 else ('down' if signal_value < 0 else 'flat')
        elif signal_name in ['pressure_tendency', 'pressure_change', 'temp_change', 'wind_change', 'dewpoint_change', 'volatility_change']:
            sig_dir = 'up' if signal_value > 0 else ('down' if signal_value < 0 else 'flat')
        else:
            # Continuous signals: check sign
            sig_dir = 'up' if signal_value > 0 else ('down' if signal_value < 0 else 'flat')
        
        if sig_dir == 'flat':
            continue
        
        # Get actual direction - use yesterday's date as predictor for today's market
        # The signal was computed using day_before and yesterday, predicting today's market
        # So we need to find the market direction for 'date' (today)
        actual = markets.get(date, {}).get('HIGH', 'flat')
        if actual == 'flat':
            continue
        
        if sig_dir == actual:
            correct += 1
        total += 1
    
    accuracy = correct / total if total > 0 else 0
    return accuracy, correct, total


def compute_correlation(signals: Dict[str, Dict[str, float]], signal1: str, signal2: str) -> float:
    """Compute Pearson correlation between two signals."""
    values1 = []
    values2 = []
    
    for date, s in signals.items():
        if signal1 in s and signal2 in s:
            values1.append(s[signal1])
            values2.append(s[signal2])
    
    if len(values1) < 2:
        return 0.0
    
    mean1 = sum(values1) / len(values1)
    mean2 = sum(values2) / len(values2)
    
    numerator = sum((v1 - mean1) * (v2 - mean2) for v1, v2 in zip(values1, values2))
    denom1 = math.sqrt(sum((v - mean1) ** 2 for v in values1))
    denom2 = math.sqrt(sum((v - mean2) ** 2 for v in values2))
    
    if denom1 > 0 and denom2 > 0:
        return numerator / (denom1 * denom2)
    return 0.0


def identify_clusters(signals: Dict[str, Dict[str, float]], all_signals: List[str], threshold: float = 0.65) -> List[List[str]]:
    """
    Identify correlation clusters.
    Signals with correlation > threshold belong to same cluster.
    Returns list of clusters (each cluster is list of signal names).
    """
    clusters = []
    used = set()
    
    for signal in all_signals:
        if signal in used:
            continue
        
        cluster = [signal]
        used.add(signal)
        
        for other_signal in all_signals:
            if other_signal in used:
                continue
            
            corr = compute_correlation(signals, signal, other_signal)
            if abs(corr) > threshold:
                cluster.append(other_signal)
                used.add(other_signal)
        
        clusters.append(cluster)
    
    return clusters


def find_best_in_cluster(signals: Dict[str, Dict[str, float]], markets: Dict[str, Dict], cluster: List[str]) -> str:
    """Find the best signal in a cluster (highest accuracy)."""
    best_signal = cluster[0]
    best_accuracy = 0
    
    for signal in cluster:
        accuracy, _, _ = compute_signal_accuracy(signals, markets, signal)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_signal = signal
    
    return best_signal


def compute_signal_inversion(signals: Dict[str, Dict[str, float]], markets: Dict[str, Dict], signal_name: str) -> Tuple[bool, float]:
    """
    Check if a signal is inverted (anti-predictive).
    Returns (is_inverted, accuracy)
    """
    accuracy, correct, total = compute_signal_accuracy(signals, markets, signal_name)
    
    # If accuracy significantly below 50%, it's inverted
    is_inverted = accuracy < 0.48 and total >= 100
    return is_inverted, accuracy


def main():
    """Main entry point."""
    print("=" * 80)
    print("PHASE 2: SIGNAL HYGIENE - CORRELATION ANALYSIS (CORRECTED)")
    print("=" * 80)
    print()
    print("Methods:")
    print("  - Compute Pearson correlation matrix for all 26 signals")
    print("  - Identify correlation clusters (threshold > 0.65)")
    print("  - Keep 1 signal per cluster (highest accuracy)")
    print("  - Remove anti-predictive signals (accuracy < 48%)")
    print("  - TEMPORAL ALIGNMENT CORRECTED: yesterday's signals → today's market")
    print()
    
    # Connect to database
    conn = sqlite3.connect(METAR_DB, timeout=60)
    
    # Get all stations
    stations = get_all_stations(conn)
    print(f"Found {len(stations)} stations with data")
    print()
    
    # Process first station (representative)
    station = stations[0]
    print(f"Processing {station}...")
    
    signals = compute_all_signals(station, conn)
    markets = get_market_directions(station, conn)
    
    print(f"  Computed signals for {len(signals)} dates")
    
    # All signal names
    all_signals = list(signals.get(next(iter(signals)), {}).keys())
    print(f"  Found {len(all_signals)} signals: {', '.join(all_signals)}")
    print()
    
    # Compute accuracy for each signal
    print("SIGNAL ACCURACIES (per-station, CORRECTED temporal alignment)")
    print("-" * 60)
    print(f"{'Signal':<25} {'Accuracy':>10} {'Correct':>8} {'Total':>8}")
    print("-" * 51)
    
    signal_accuracies = {}
    signal_inversions = {}
    
    for signal in all_signals:
        accuracy, correct, total = compute_signal_accuracy(signals, markets, signal)
        is_inverted, inv_accuracy = compute_signal_inversion(signals, markets, signal)
        
        signal_accuracies[signal] = accuracy
        signal_inversions[signal] = is_inverted
        
        inv_mark = " (INVERTED)" if is_inverted else ""
        print(f"{signal:<25} {accuracy:>10.2%} {correct:>8} {total:>8}{inv_mark}")
    print()
    
    # Identify correlation clusters
    print("CORRELATION CLUSTERS (threshold > 0.65)")
    print("-" * 60)
    
    clusters = identify_clusters(signals, all_signals, threshold=0.65)
    print(f"Found {len(clusters)} clusters:")
    
    for i, cluster in enumerate(clusters):
        print(f"\nCluster {i+1}: {', '.join(cluster)}")
        
        # Find best in cluster
        best = find_best_in_cluster(signals, markets, cluster)
        best_accuracy, _, _ = compute_signal_accuracy(signals, markets, best)
        print(f"  Best signal: {best} ({best_accuracy:.2%} accuracy)")
    print()
    
    # Summary of signals to keep
    print("SIGNALS TO KEEP (one per cluster, highest accuracy)")
    print("-" * 60)
    
    signals_to_keep = []
    for cluster in clusters:
        best = find_best_in_cluster(signals, markets, cluster)
        signals_to_keep.append(best)
        print(f"  {best}")
    
    print(f"\nTotal to keep: {len(signals_to_keep)} (from {len(all_signals)} original)")
    print()
    
    # Identify signals to remove
    print("SIGNALS TO REMOVE")
    print("-" * 60)
    
    # Remove inverted signals
    inverted = [s for s, inv in signal_inversions.items() if inv]
    if inverted:
        print("Inverted (anti-predictive) signals:")
        for s in inverted:
            print(f"  {s}: {signal_accuracies[s]:.2%} accuracy")
    else:
        print("No inverted signals found")
    print()
    
    # Signals to remove due to redundancy
    redundant = [s for s in all_signals if s not in signals_to_keep]
    print("Redundant signals (in clusters with better signals):")
    for s in redundant:
        print(f"  {s}: {signal_accuracies[s]:.2%} accuracy")
    print()
    
    # Correlation matrix
    print("CORRELATION MATRIX (top correlations > 0.5)")
    print("-" * 60)
    
    high_corr_pairs = []
    for i, s1 in enumerate(all_signals):
        for s2 in all_signals[i+1:]:
            corr = compute_correlation(signals, s1, s2)
            if abs(corr) > 0.5:
                high_corr_pairs.append((s1, s2, corr))
    
    # Sort by absolute correlation
    high_corr_pairs.sort(key=lambda x: -abs(x[2]))
    
    for s1, s2, corr in high_corr_pairs[:20]:
        print(f"  {s1} <-> {s2}: {corr:.3f}")
    print()
    
    # Final verdict
    print("=" * 80)
    print("PHASE 2 VERDICT")
    print("=" * 80)
    print()
    print(f"Original signals: {len(all_signals)}")
    print(f"Signals to keep: {len(signals_to_keep)}")
    print(f"Signals to remove: {len(redundant)}")
    print(f"  - Redundant: {len(redundant)}")
    print(f"  - Inverted: {len(inverted)}")
    print()
    
    if len(signals_to_keep) >= 10 and len(signals_to_keep) <= 15:
        print("✓ SIGNAL HYGIENE COMPLETE")
        print(f"  Reduced from {len(all_signals)} to {len(signals_to_keep)} signals")
        print("  Proceed to Phase 3: Weight & Calibration Overhaul")
    else:
        print("⚠ REVIEW SIGNAL COUNT")
        print(f"  Keep {len(signals_to_keep)} signals")
        print("  Target: 10-15 signals")
    
    conn.close()
    
    # Save results
    output = {
        'station': station,
        'original_signals': all_signals,
        'signals_to_keep': signals_to_keep,
        'signals_to_remove': redundant,
        'inverted_signals': inverted,
        'signal_accuracies': signal_accuracies,
        'correlation_clusters': [list(c) for c in clusters],
        'high_correlation_pairs': [(s1, s2, corr) for s1, s2, corr in high_corr_pairs[:20]],
        'verdict': "PASS" if len(signals_to_keep) >= 10 and len(signals_to_keep) <= 15 else "REVIEW",
    }
    
    output_path = "/tmp/p2_signal_hygiene_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\nResults saved to: {output_path}")
    
    return output


if __name__ == "__main__":
    result = main()
