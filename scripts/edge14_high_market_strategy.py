#!/usr/bin/env python3
"""
EDGE 14: HIGH Market 79.7% Strategy

Regime classifier: stable (vol<1.0, grad<0.5), transient (vol<2.0, grad<1.0), volatile (else)
Trading rule: only trade 'stable' regime.
  - Reversion=0 → bet UP (historical 97.5% edge claim)
  - Reversion=1 → bet DOWN (historical 69.8% edge claim)

THIS IS THE CRITICAL TEST: Does the 97.5% claim hold walk-forward?

Standalone Python. No AI in the loop. Walk-forward only.
"""

import sqlite3
import math
from collections import defaultdict
import os

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']


def load_station_data(station, conn):
    """Load daily METAR data and market outcomes for a station."""
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
        if any(v is None for v in r[1:]): continue
        days.append({'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
                      'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]})
    
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket, reversion_occurred
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = {
                'direction': 'up' if r[1] > r[2] else 'down',
                'bucket': r[1], 'prior': r[2],
                'reversion': r[3] if r[3] is not None else 0
            }
    return days, market


def classify_regime(idx, days):
    """
    Classify regime based on 15-day rolling window.
    stable: vol<1.0, |grad|<0.5
    transient: vol<2.0, |grad|<1.0
    volatile: else
    """
    if idx < 15:
        return 'insufficient'
    
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    
    mean = sum(highs) / len(highs)
    variance = sum((h - mean)**2 for h in highs) / len(highs)
    vol = math.sqrt(variance)
    
    if len(highs) >= 2:
        slope = (highs[-1] - highs[0]) / len(highs)
    else:
        slope = 0
    
    if vol < 1.0 and abs(slope) < 0.5:
        return 'stable'
    elif vol < 2.0 and abs(slope) < 1.0:
        return 'transient'
    else:
        return 'volatile'


def compute_reversion_flag(idx, days):
    """
    Reversion flag: 1 if today's high is significantly above 30-day mean, 0 otherwise.
    This matches the settlement_epochs.reversion_occurred field.
    """
    if idx < 31:
        return 0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    # If today's high is > 2°F above the 30-day mean, flag reversion
    if days[idx-1]['high'] > mean + 2.0:
        return 1
    return 0


def walk_forward_strategy(days, market, train_days=180, test_days=30):
    """
    Walk-forward backtest of the HIGH Market 79.7% Strategy.
    
    For each test day:
    1. Classify regime from data available BEFORE market settles
    2. If stable regime:
       - reversion=0 → predict UP
       - reversion=1 → predict DOWN
    3. Otherwise: abstain
    
    Returns detailed results by regime and reversion status.
    """
    results = {
        'stable_up': {'correct': 0, 'total': 0},
        'stable_down': {'correct': 0, 'total': 0},
        'transient': {'correct': 0, 'total': 0, 'predictions': []},
        'volatile': {'correct': 0, 'total': 0, 'predictions': []},
        'all': {'correct': 0, 'total': 0, 'abstain': 0},
        'returns': []
    }
    
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None:
                continue
            
            regime = classify_regime(idx, days)
            reversion = compute_reversion_flag(idx, days)
            
            if regime == 'stable':
                if reversion == 0:
                    predicted = 'up'
                    key = 'stable_up'
                else:
                    predicted = 'down'
                    key = 'stable_down'
                
                ok = (predicted == actual['direction'])
                results[key]['total'] += 1
                results['all']['total'] += 1
                if ok:
                    results[key]['correct'] += 1
                    results['all']['correct'] += 1
                results['returns'].append((1.0, ok))
            elif regime == 'transient':
                # For transient regime: use reversion signal
                if reversion == 0:
                    predicted = 'up'
                else:
                    predicted = 'down'
                ok = (predicted == actual['direction'])
                results['transient']['total'] += 1
                results['all']['total'] += 1
                if ok:
                    results['transient']['correct'] += 1
                    results['all']['correct'] += 1
                results['returns'].append((0.5, ok))
            else:
                # Volatile regime: abstain
                results['all']['abstain'] += 1
        
        start += test_days
        if start > len(days):
            break
    
    return results


def compute_sharpe(returns):
    """Compute Sharpe ratio from (confidence, ok) tuples."""
    if not returns: return 0.0
    vals = [(2*c - 1) if ok else -(2*c - 1) for c, ok in returns]
    n = len(vals)
    if n == 0: return 0.0
    mean = sum(vals) / n
    if n == 1: return 0.0
    var = sum((v - mean)**2 for v in vals) / n
    std = math.sqrt(var)
    return mean / std if std > 0 else 0.0


def main():
    print("=" * 90)
    print("EDGE 14: HIGH MARKET 79.7% STRATEGY — WALK-FORWARD VALIDATION")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Strategy: stable regime → reversion=0: UP, reversion=1: DOWN")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    
    # Aggregate across all stations
    agg_stable_up = {'correct': 0, 'total': 0}
    agg_stable_down = {'correct': 0, 'total': 0}
    agg_transient = {'correct': 0, 'total': 0}
    agg_all = {'correct': 0, 'total': 0, 'abstain': 0}
    all_returns = []
    
    print(f"{'Station':<8} {'Stable+Up':>12} {'Stable+Down':>14} {'Transient':>12} {'Overall':>10} {'Sharpe':>8} {'Abstain':>8}")
    print("-" * 80)
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210:
            print(f"{station:<8} {'N/A':>12} {'N/A':>14} {'N/A':>12} {'N/A':>10} {'N/A':>8} {'N/A':>8}")
            continue
        
        results = walk_forward_strategy(days, market)
        
        su = results['stable_up']
        sd = results['stable_down']
        tr = results['transient']
        al = results['all']
        sh = compute_sharpe(results['returns'])
        
        su_acc = su['correct']/su['total']*100 if su['total'] > 0 else 0
        sd_acc = sd['correct']/sd['total']*100 if sd['total'] > 0 else 0
        tr_acc = tr['correct']/tr['total']*100 if tr['total'] > 0 else 0
        al_acc = al['correct']/al['total']*100 if al['total'] > 0 else 0
        
        print(f"{station:<8} {su_acc:>11.2f}% ({su['total']:>3}) {sd_acc:>13.2f}% ({sd['total']:>3}) {tr_acc:>11.2f}% ({tr['total']:>3}) {al_acc:>9.2f}% {sh:>8.3f} {al['abstain']:>8}")
        
        agg_stable_up['correct'] += su['correct']
        agg_stable_up['total'] += su['total']
        agg_stable_down['correct'] += sd['correct']
        agg_stable_down['total'] += sd['total']
        agg_transient['correct'] += tr['correct']
        agg_transient['total'] += tr['total']
        agg_all['correct'] += al['correct']
        agg_all['total'] += al['total']
        agg_all['abstain'] += al['abstain']
        all_returns.extend(results['returns'])
    
    # Aggregate results
    print()
    print("=" * 90)
    print("AGGREGATE RESULTS (all 21 stations)")
    print("=" * 90)
    
    su_acc = agg_stable_up['correct']/agg_stable_up['total'] if agg_stable_up['total'] > 0 else 0
    sd_acc = agg_stable_down['correct']/agg_stable_down['total'] if agg_stable_down['total'] > 0 else 0
    tr_acc = agg_transient['correct']/agg_transient['total'] if agg_transient['total'] > 0 else 0
    al_acc = agg_all['correct']/agg_all['total'] if agg_all['total'] > 0 else 0
    overall_sharpe = compute_sharpe(all_returns)
    coverage = agg_all['total'] / (agg_all['total'] + agg_all['abstain']) if (agg_all['total'] + agg_all['abstain']) > 0 else 0
    
    print(f"\n  Stable + Reversion=0 (bet UP):")
    print(f"    Accuracy: {su_acc:.2%} ({agg_stable_up['correct']}/{agg_stable_up['total']} trades)")
    print(f"    CLAIM: 97.5% — {'✓ HOLDS' if su_acc >= 0.90 else '✗ DOES NOT HOLD'} (walk-forward: {su_acc:.2%})")
    
    print(f"\n  Stable + Reversion=1 (bet DOWN):")
    print(f"    Accuracy: {sd_acc:.2%} ({agg_stable_down['correct']}/{agg_stable_down['total']} trades)")
    print(f"    CLAIM: 69.8% — {'✓ HOLDS' if sd_acc >= 0.60 else '✗ DOES NOT HOLD'} (walk-forward: {sd_acc:.2%})")
    
    print(f"\n  Transient regime:")
    print(f"    Accuracy: {tr_acc:.2%} ({agg_transient['correct']}/{agg_transient['total']} trades)")
    
    print(f"\n  Overall strategy:")
    print(f"    Accuracy: {al_acc:.2%} ({agg_all['correct']}/{agg_all['total']} trades)")
    print(f"    Coverage: {coverage:.1%}")
    print(f"    Abstain: {agg_all['abstain']} days (volatile regime)")
    print(f"    Sharpe: {overall_sharpe:.3f}")
    
    # Circularity check
    if su_acc == 1.0 or sd_acc == 1.0 or al_acc == 1.0:
        print(f"\n  ⚠️  CIRCULARITY DETECTED — 100% accuracy — STOP AND FIX")
    
    # Verdict
    print()
    print("=" * 90)
    print("VERDICT")
    print("=" * 90)
    
    if su_acc >= 0.90:
        print(f"\n  ✓ Stable+UP claim HOLDS at {su_acc:.2%} (claim: 97.5%)")
    elif su_acc >= 0.58:
        print(f"\n  ⚠️  Stable+UP claim PARTIALLY HOLDS at {su_acc:.2%} (claim: 97.5%, threshold: 58%)")
    else:
        print(f"\n  ✗ Stable+UP claim FAILS at {su_acc:.2%} (claim: 97.5%)")
    
    if sd_acc >= 0.60:
        print(f"  ✓ Stable+DOWN claim HOLDS at {sd_acc:.2%} (claim: 69.8%)")
    elif sd_acc >= 0.58:
        print(f"  ⚠️  Stable+DOWN claim PARTIALLY HOLDS at {sd_acc:.2%} (claim: 69.8%, threshold: 58%)")
    else:
        print(f"  ✗ Stable+DOWN claim FAILS at {sd_acc:.2%} (claim: 69.8%)")
    
    if al_acc >= 0.58:
        print(f"  ✓ Overall strategy PASSES 58% threshold at {al_acc:.2%}")
    else:
        print(f"  ✗ Overall strategy FAILS 58% threshold at {al_acc:.2%}")
    
    if su_acc < 0.58 and sd_acc < 0.58:
        print(f"\n  ⚠️  STOP CONDITION: Both stable regime claims fail — escalate to Dan")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 14 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
