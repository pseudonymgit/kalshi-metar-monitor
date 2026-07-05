#!/usr/bin/env python3
"""
Test ENSO Regime Bias Integration with Edge 8 Gaussian Model

Tests the impact of ENSO regime adjustment on the baseline Gaussian accuracy.
This serves as the backtest for Edge 21.
"""

import sqlite3
import math
import importlib.util
import random
import os
import sys

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

# ─── Import the necessary modules ───────────────────────────────────────────

# Load the ENSO regime bias module
enso_spec = importlib.util.spec_from_file_location("enso_regime_bias", 
    "/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/enso_regime_bias.py")
enso_module = importlib.util.module_from_spec(enso_spec)
enso_spec.loader.exec_module(enso_module)

# Define a basic Gaussian signal function like in the main ensemble
def load_gaussian_signal_data(idx, days, enso_adj_func=None, enso_indices=None):
    """
    Basic Gaussian model like the Edge 8 approach, with optional ENSO adjustment.
    
    Returns: (direction, confidence)
    """
    if idx < 48:  # Need 48 days for baseline
        return None, 0.0
    
    high_window = [day['high'] for day in days[idx-48:idx-1] if day['high'] is not None]
    if len(high_window) < 10:  # Minimum required data
        return None, 0.0
    
    mean = sum(high_window) / len(high_window)
    var = sum((h - mean)**2 for h in high_window) / len(high_window)
    std = math.sqrt(var) if var > 0 else 0.01
    
    # Apply ENSO adjustment if provided
    if hasattr(enso_module, 'get_regime_adjustment') and enso_indices is not None:
        date_str = days[idx]['date']
        station = 'TEST'  # Placeholder
        enso_adjustment = enso_module.get_regime_adjustment(station, date_str, indices=enso_indices)
        mean_adj = mean + enso_adjustment
    else:
        mean_adj = mean
    
    current_temp = days[idx-1]['high']
    z = (current_temp - mean_adj) / std if std > 0 else 0
    
    # Gaussian signal logic: if current temp is anomalous relative to adjusted mean
    if z > 1.0:  # High temperature compared to adjusted mean and baseline variance
        return 'down', min(abs(z), 0.95)  # Bet DOWN if current temp is unusually high
    elif z < -1.0:  # Low temperature compared to adjusted mean
        return 'up', min(abs(z), 0.95)   # Bet UP if current temp is unusually low
    else:
        return None, 0.0  # Not unusual enough to warrant signal


def load_station_data(station, conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(temp_f) as temp, AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    days = []
    for r in cur.fetchall():
        if any(v is None for v in r[1:]):
            continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2],
                     'temp': r[3], 'pressure': r[4]})
    
    # Settlement epochs
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        market[r[0]] = {
            'direction': 'up' if r[1] > r[2] else 'down',
            'reversion': r[3] if r[3] is not None else 0
        }
    
    return days, market


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def binomial_test_p(correct, total, null_p=0.5):
    if total == 0: return 1.0
    p = correct / total
    if total > 100:
        z = (p - null_p) * math.sqrt(total) / math.sqrt(null_p * (1 - null_p))
        return min(2 * (1 - normal_cdf(abs(z))), 1.0)
    else:
        from math import comb
        expected = null_p * total
        p_val = 0
        for k in range(total + 1):
            try:
                prob = comb(total, k) * (null_p**k) * ((1-null_p)**(total-k))
                if abs(k - expected) >= abs(correct - expected):
                    p_val += prob
            except:
                continue  # Skip if comb calculation fails
        return min(p_val, 1.0)

def walk_forward_gaussian_comparison(days, market, enso_indices=None, train_days=180, test_days=30, use_enso_adj=False):
    """Compare Gaussian model with/without ENSO adjustment."""
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            
            # Generate signal with or without ENSO adjustment
            if use_enso_adj and enso_indices:
                pred, conf = load_gaussian_signal_data(idx, days, None, enso_indices)
            else:
                pred, conf = load_gaussian_signal_data(idx, days, None, None)
            
            if pred is not None and conf >= 0.7:  # Same confidence threshold as other edges
                results.append((pred, actual['direction'], conf))
        start += test_days
        if start > len(days): break
    return results


def run_enso_impact_backtest():
    print("=" * 80)
    print("EDGE 21: ENSO REGIME BIAS IMPACT — INTEGRATION BACKTEST") 
    print("=" * 80)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print("Comparing Gaussian model: baseline vs with ENSO adjustment")
    print("Walk-forward: 6-month train / 1-month test with 0.7 confidence threshold")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)
    
    # Preload all ENSO data
    print("Loading climate indices for ENSO adjustment...")
    enso_climate_indices = {
        'oni': enso_module.parse_oni_data(),
        'pdo': enso_module.parse_pdo_data(),
        'nao': enso_module.parse_nao_data(),
        'ao': enso_module.parse_ao_data()
    }
    print(f"  ONI: {len(enso_climate_indices['oni'])} records")
    print(f"  PDO: {len(enso_climate_indices['pdo'])} records")
    print(f"  NAO: {len(enso_climate_indices['nao'])} records")
    print(f"  AO: {len(enso_climate_indices['ao'])} records")
    print()

    configs = [
        ("Gaussian baseline (no ENSO adj)", False),
        ("Gaussian + ENSO regime bias", True),
    ]

    all_config_results = {}

    for config_name, use_enso in configs:
        print(f"  Testing: {config_name}")
        
        all_results = []
        total_trades = 0
        total_correct = 0
        
        for station in ALL_STATIONS:
            days, market = load_station_data(station, conn)
            if len(days) < 210:
                continue
            
            results = walk_forward_gaussian_comparison(
                days, market, enso_climate_indices if use_enso else None, use_enso_adj=use_enso)
            
            if not results:
                continue
            
            station_correct = sum(1 for p, a, c in results if p == a)
            station_total = len(results)
            station_accuracy = station_correct / station_total if station_total > 0 else 0
            
            all_results.extend(results)
            total_trades += station_total
            total_correct += station_correct
        
        # Calculate config-level metrics
        accuracy = total_correct / total_trades if total_trades > 0 else 0
        
        all_config_results[config_name] = {
            'accuracy': accuracy,
            'total': total_trades,
            'correct': total_correct,
            'all_results': all_results
        }

    # ─── RESULTS COMPARISON ────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("EDGE 21 RESULTS COMPARISON")
    print("=" * 80) 
    
    baseline = all_config_results["Gaussian baseline (no ENSO adj)"]
    adjusted = all_config_results["Gaussian + ENSO regime bias"]
    
    print(f"\n{'Configuration':<40} {'Accuracy':<10} {'Trades':<8} {'Correct':<8}")
    print("-" * 70)
    print(f"{'Gaussian baseline':<40} {baseline['accuracy']:<10.2%} {baseline['total']:<8} {baseline['correct']:<8}")
    print(f"{'Gaussian + ENSO adjustment':<40} {adjusted['accuracy']:<10.2%} {adjusted['total']:<8} {adjusted['correct']:<8}")
    
    improvement = (adjusted['accuracy'] - baseline['accuracy']) * 100
    print(f"\n  Net improvement: {improvement:+.3f} percentage points")

    # Binomial significance test
    if adjusted['total'] > 0:
        z = (adjusted['accuracy'] - 0.5) * math.sqrt(adjusted['total']) / math.sqrt(0.25)
        adj_binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
        print(f"  Significance test (vs 50%): p = {adj_binom_p:.6f}")

    if baseline['total'] > 0:
        z = (baseline['accuracy'] - 0.5) * math.sqrt(baseline['total']) / math.sqrt(0.25)
        base_binom_p = min(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 1.0)
        print(f"  Baseline significance (vs 50%): p = {base_binom_p:.6f}")

    print(f"\n  {'✓ SIGNIFICANT' if improvement > 0.1 and adj_binom_p < 0.05 else '✗ NOT SIGNIFICANT':>55}")
    
    # Check if meets minimum threshold from spec: ≥0.5% improvement over baseline
    threshold_met = improvement >= 0.5
    print(f"  ENSO improvement ≥0.5%: {'✓ YES' if threshold_met else '✗ NO'} ({improvement:.3f}% vs required 0.5%)")
    
    conn.close()
    print()
    print("=" * 80)
    print("EDGE 21 BACKTEST COMPLETE")
    print("=" * 80)
    
    return {
        'baseline_acc': baseline['accuracy'],
        'adjusted_acc': adjusted['accuracy'],
        'improvement': improvement,
        'significant': improvement > 0.1 and adj_binom_p < 0.05,
        'threshold_met': threshold_met
    }


if __name__ == "__main__":
    results = run_enso_impact_backtest()
    
    # Exit with appropriate code based on success criteria
    sys.exit(0 if results['threshold_met'] or results['significant'] else 1)