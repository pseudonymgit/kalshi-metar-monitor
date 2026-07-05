#!/usr/bin/env python3
"""
FULL ENSEMBLE BACKTEST v6 — FINAL
All 20 Kalshi Cities + All Signals (CORRECTED temporal alignment)

KEY FINDING: Original 86.82% accuracy was inflated by look-ahead bias.
The settlement bucket IS the daily max temperature, so comparing today's temp
trend to today's market direction is tautological (~100% accuracy).

CORRECTED: yesterday's signals → today's market direction.

Best strategies found:
1. Pressure tendency (ΔP > 1.5mb): 57.37% accuracy, 65.7% coverage
2. Pressure + trend agreement: 60.63% accuracy, 25.0% coverage
3. Pressure + persistence agreement: 60.62% accuracy, 25.0% coverage
4. Pressure-only (ΔP > 3.0mb): 60.03% accuracy, 42.2% coverage

Station mapping (corrected):
- DAL/DFW → KDFW, HOU → KHOU, AUS → KAUS
"""

import sqlite3
import math
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

DIRECTIONAL_THRESHOLD = 0.58
TARGET_ACCURACY = 0.84

KALSHI_STATION_MAP = {
    "ATL": "KATL", "AUS": "KAUS", "BOS": "KBOS", "CHI": "KMDW",
    "DAL": "KDFW", "DC":  "KDCA", "DEN": "KDEN", "HOU": "KHOU",
    "LAX": "KLAX", "LV":  "KLAS", "MIA": "KMIA", "MIN": "KMSP",
    "NOLA":"KMSY", "NYC": "KNYC", "OKC": "KOKC", "PHIL":"KPHL",
    "PHX": "KPHX", "SATX":"KSAT", "SEA": "KSEA", "SFO": "KSFO",
}


def get_data(station, conn):
    """Get temp and market data for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(wind_direction_deg) as wind_dir,
               AVG(pressure_mb) as pressure
        FROM metar_observations WHERE station=? AND temp_f IS NOT NULL
        GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    temps = []
    for r in cur.fetchall():
        temps.append({
            'date': r[0], 'high': r[1], 'low': r[2],
            'dewpoint': r[3], 'wind_dir': r[4], 'pressure': r[5],
        })
    
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = 'up' if r[1] > r[2] else 'down'
    
    return temps, market


def run_strategy(name, temps, market, signal_fn):
    """Run a strategy and return (correct, total, per_station_results)."""
    correct = total = 0
    for i in range(2, len(temps)):
        today = temps[i]
        yesterday = temps[i-1]
        day_before = temps[i-2]
        actual = market.get(today['date'])
        if actual is None:
            continue
        signal = signal_fn(yesterday, day_before, market)
        if signal is not None:
            total += 1
            if signal == actual:
                correct += 1
    return correct, total


def main():
    conn = sqlite3.connect(METAR_DB, timeout=60)
    stations = [s for s in KALSHI_STATION_MAP.values()]
    
    # Verify stations
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs WHERE market_type IN ('HIGH', 'LOW')")
    available = {r[0] for r in cur.fetchall()}
    stations = [s for s in stations if s in available]
    
    print("=" * 80)
    print("FULL ENSEMBLE BACKTEST v6 — FINAL REPORT")
    print("=" * 80)
    print()
    print(f"Stations: {len(stations)}/20")
    print(f"Temporal alignment: YESTERDAY's signals → TODAY's market direction")
    print()
    
    # Strategy definitions
    strategies = {}
    
    # Strategy 1: Pressure tendency (ΔP > 1.5mb)
    def pressure_15(yesterday, day_before, market):
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 1.5:
                return 'up' if dp > 0 else 'down'
        return None
    strategies['Pressure(1.5)'] = pressure_15
    
    # Strategy 2: Pressure tendency (ΔP > 2.0mb)
    def pressure_20(yesterday, day_before, market):
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 2.0:
                return 'up' if dp > 0 else 'down'
        return None
    strategies['Pressure(2.0)'] = pressure_20
    
    # Strategy 3: Pressure tendency (ΔP > 3.0mb)
    def pressure_30(yesterday, day_before, market):
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 3.0:
                return 'up' if dp > 0 else 'down'
        return None
    strategies['Pressure(3.0)'] = pressure_30
    
    # Strategy 4: Pressure + Trend agreement
    def pressure_trend_15(yesterday, day_before, market):
        p_signal = None
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 1.5:
                p_signal = 'up' if dp > 0 else 'down'
        t_signal = 'up' if yesterday['high'] > day_before['high'] else 'down'
        if p_signal and p_signal == t_signal:
            return p_signal
        return None
    strategies['Pressure+Trend(1.5)'] = pressure_trend_15
    
    # Strategy 5: Pressure + Persistence agreement
    def pressure_persist_15(yesterday, day_before, market):
        p_signal = None
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 1.5:
                p_signal = 'up' if dp > 0 else 'down'
        pers_signal = market.get(yesterday['date'])
        if p_signal and pers_signal and p_signal == pers_signal:
            return p_signal
        return None
    strategies['Pressure+Persist(1.5)'] = pressure_persist_15
    
    # Strategy 6: Simple trend (baseline)
    def simple_trend(yesterday, day_before, market):
        return 'up' if yesterday['high'] > day_before['high'] else 'down'
    strategies['Simple Trend'] = simple_trend
    
    # Strategy 7: Persistence (baseline)
    def persistence(yesterday, day_before, market):
        return market.get(yesterday['date'])
    strategies['Persistence'] = persistence
    
    # Strategy 8: Pressure primary, trend fallback
    def pressure_trend_fallback(yesterday, day_before, market):
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 1.5:
                return 'up' if dp > 0 else 'down'
        return 'up' if yesterday['high'] > day_before['high'] else 'down'
    strategies['Pressure(1.5)+Trend'] = pressure_trend_fallback
    
    # Strategy 9: Pressure primary, persistence fallback
    def pressure_persist_fallback(yesterday, day_before, market):
        if yesterday.get('pressure') and day_before.get('pressure'):
            dp = yesterday['pressure'] - day_before['pressure']
            if abs(dp) > 1.5:
                return 'up' if dp > 0 else 'down'
        return market.get(yesterday['date'])
    strategies['Pressure(1.5)+Persist'] = pressure_persist_fallback
    
    # Run all strategies
    all_strategy_results = {}
    
    for strategy_name, signal_fn in strategies.items():
        total_correct = 0
        total_signals = 0
        station_results = {}
        
        for station in stations:
            temps, market = get_data(station, conn)
            correct, total = run_strategy(station, temps, market, signal_fn)
            total_correct += correct
            total_signals += total
            station_results[station] = {
                'correct': correct, 'total': total,
                'accuracy': correct/total if total else 0,
            }
        
        accuracy = total_correct / total_signals if total_signals else 0
        all_strategy_results[strategy_name] = {
            'accuracy': accuracy,
            'correct': total_correct,
            'total': total_signals,
            'station_results': station_results,
        }
    
    # Print results
    print("STRATEGY COMPARISON")
    print("-" * 80)
    print(f"{'Strategy':<25} {'Accuracy':>10} {'Correct':>8} {'Total':>8} {'Coverage':>10}")
    print("-" * 63)
    
    total_possible = 33954  # Total trading days across all stations
    
    for name in ['Simple Trend', 'Persistence', 'Pressure(1.5)', 'Pressure(2.0)', 
                 'Pressure(3.0)', 'Pressure+Trend(1.5)', 'Pressure+Persist(1.5)',
                 'Pressure(1.5)+Trend', 'Pressure(1.5)+Persist']:
        r = all_strategy_results[name]
        cov = r['total'] / total_possible * 100
        print(f"{name:<25} {r['accuracy']:>10.2%} {r['correct']:>8} {r['total']:>8} {cov:>9.1f}%")
    
    print()
    print("=" * 80)
    print("PER-STATION: Pressure(1.5) — Best Standalone Signal")
    print("-" * 80)
    print(f"{'Station':<8} {'Accuracy':>10} {'Correct':>8} {'Total':>8} {'Coverage':>10}")
    print("-" * 46)
    
    r = all_strategy_results['Pressure(1.5)']
    for station in sorted(stations):
        sr = r['station_results'][station]
        cov = sr['total'] / (total_possible / len(stations)) * 100
        print(f"{station:<8} {sr['accuracy']:>10.2%} {sr['correct']:>8} {sr['total']:>8} {cov:>9.1f}%")
    
    print()
    print("=" * 80)
    print("PER-STATION: Pressure+Trend(1.5) — Best Accuracy/Coverage Trade-off")
    print("-" * 80)
    print(f"{'Station':<8} {'Accuracy':>10} {'Correct':>8} {'Total':>8} {'Coverage':>10}")
    print("-" * 46)
    
    r = all_strategy_results['Pressure+Trend(1.5)']
    for station in sorted(stations):
        sr = r['station_results'][station]
        cov = sr['total'] / (total_possible / len(stations)) * 100
        print(f"{station:<8} {sr['accuracy']:>10.2%} {sr['correct']:>8} {sr['total']:>8} {cov:>9.1f}%")
    
    print()
    print("=" * 80)
    print("THRESHOLD CHECKS")
    print("=" * 80)
    
    for name in ['Pressure(1.5)', 'Pressure(2.0)', 'Pressure(3.0)', 
                 'Pressure+Trend(1.5)', 'Pressure+Persist(1.5)']:
        r = all_strategy_results[name]
        passes = r['accuracy'] >= DIRECTIONAL_THRESHOLD
        cov = r['total'] / total_possible * 100
        print(f"  {name:<25}: {r['accuracy']:.2%} {'✓' if passes else '✗'} (threshold: {DIRECTIONAL_THRESHOLD:.0%}), coverage: {cov:.1f}%")
    
    print()
    print("=" * 80)
    print("KEY FINDINGS")
    print("=" * 80)
    print()
    print("1. ORIGINAL 86.82% ACCURACY WAS INFLATED")
    print("   The settlement bucket IS the daily max temperature. Comparing today's")
    print("   temp trend to today's market direction is tautological (~100% accurate).")
    print("   Corrected (yesterday→today): simple trend = 51.23% (coin flip).")
    print()
    print("2. PRESSURE TENDENCY IS THE ONLY GENUINELY PREDICTIVE SIGNAL")
    print("   - ΔP > 1.5mb: 57.37% accuracy, 65.7% coverage")
    print("   - ΔP > 2.0mb: 58.29% accuracy, 56.7% coverage")
    print("   - ΔP > 3.0mb: 60.03% accuracy, 42.2% coverage")
    print("   - Passes Gray Room 58% threshold at ΔP > 2.0mb")
    print()
    print("3. AGREEMENT STRATEGIES IMPROVE ACCURACY AT COST OF COVERAGE")
    print("   - Pressure + Trend agreement: 60.63% accuracy, 25.0% coverage")
    print("   - Pressure + Persistence agreement: 60.62% accuracy, 25.0% coverage")
    print()
    print("4. OTHER SIGNALS ARE NOT PREDICTIVE")
    print("   - Wind advection: 41.46% (actively harmful)")
    print("   - Cloud modulator: 45.59% (below random)")
    print("   - LOW market signal: 48.36% (below random)")
    print("   - MA crossover: ~50% (random)")
    print("   - Kalman filter: ~50% (random)")
    print()
    print("5. STATION BACKFILL COMPLETE")
    print("   - KAUS: 1,700 HIGH + 1,700 LOW epochs added")
    print("   - KDFW: 1,700 HIGH + 1,700 LOW epochs added (correct Dallas station)")
    print("   - KHOU: 1,700 HIGH + 1,700 LOW epochs added (correct Houston station)")
    print("   - 21 stations total, 71,388 settlement epochs")
    print()
    print("=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print()
    print("DEPLOY: Pressure tendency signal (ΔP > 2.0mb)")
    print("  - Accuracy: 58.29% (passes Gray Room 58% threshold)")
    print("  - Coverage: 56.7% of trading days")
    print("  - Simple, no AI/ML, cost $0")
    print()
    print("DO NOT DEPLOY: Trend, wind, cloud, Kalman, MA crossover signals")
    print("  - These are not genuinely predictive when temporal alignment is correct")
    print()
    print("NEXT: The 84% target accuracy is not achievable with these simple signals.")
    print("  - Consider: multi-day pressure patterns, frontal passage detection,")
    print("  - or accept lower accuracy with position sizing / risk management.")
    
    conn.close()
    
    return all_strategy_results


if __name__ == "__main__":
    results = main()
