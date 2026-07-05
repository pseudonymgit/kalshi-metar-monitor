#!/usr/bin/env python3
"""
ENSEMBLE V9 — BACKTEST WITH NEW SIGNALS (Edge 5 Only, Edge 2 Failed)

Combines all available approaches:
- Previously validated: reversion, gaussian, regime-switching, gaussian v2, pressure
- New addition: forecast disagreement (Edge 5) 
- Excluded: persistence was removed in v8
- Failed to integrate: time-of-day decay (Edge 2) showed no signal during backtest

Walk-forward only. No circularity. No AI in the loop.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
from datetime import datetime

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = 0.05

ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']


def load_station_data(station, conn):
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
        AND prior_settlement_bucket IS NOT NULL
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        market[r[0]] = {
            'direction': 'up' if r[1] > r[2] else 'down',
            'reversion': r[3] if r[3] is not None else 0,
            'bucket': r[1],
            'prior': r[2]
        }
    return days, market


# ─── PREVIOUS APPROACHES (FROM V8) ─────────────────────────────────────────

def approach_reversion(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_gaussian(idx, days):
    if idx < 48: return None, 0.0
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0: return 'down', abs(z)
    elif z < -1.0: return 'up', abs(z)
    return None, 0.0

def approach_regime(idx, days):
    if idx < 15: return None, 0.0
    window = days[idx-15:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    vol = math.sqrt(var)
    slope = (highs[-1] - highs[0]) / len(highs) if len(highs) >= 2 else 0
    if vol < 1.0 and abs(slope) < 0.5:
        if idx >= 31:
            w30 = days[idx-31:idx-1]
            h30 = [d['high'] for d in w30]
            m30 = sum(h30) / len(h30)
            dist = days[idx-1]['high'] - m30
            if dist > 1.0: return 'down', min(dist/3.0, 0.8)
            elif dist < -1.0: return 'up', min(abs(dist)/3.0, 0.8)
    return None, 0.0

def approach_gaussian_v2(idx, days):
    if idx < 31: return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h-mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5: return 'down', abs(z)
    elif z < -0.5: return 'up', abs(z)
    return None, 0.0

def approach_pressure(idx, days):
    if idx < 3: return None, 0.0
    dp = days[idx-1]['pressure'] - days[idx-2]['pressure']
    if abs(dp) > 2.0:
        return ('up' if dp > 0 else 'down'), min(abs(dp)/5.0, 0.8)
    return None, 0.0


# ─── NEW APPROACH: Edge 5 (Forecast Disagreement) ─────────────────────────

def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))

def approach_forecast_disagreement(idx, days):
    """
    Forecast disagreement proxy: When yesterday_actual differs from 7-day_mean,
    bet in direction of the mean (longer-term expectation).
    
    Original spec: Compare GFS vs NWS forecasts. Proxy: Yesterday's observed 
    (like NWS persistence forecast) vs 7-day rolling mean (like GFS climatology).
    """
    if idx < 8:
        return None, 0.0
    
    disagreement_threshold = 5.0  # deg F
    
    yesterday_high = days[idx - 1]['high']
    week_days = days[idx - 8:idx - 1]  # 7 days
    weekly_mean = sum(d['high'] for d in week_days) / len(week_days)
    
    disagreement = yesterday_high - weekly_mean
    abs_disagreement = abs(disagreement)
    
    if abs_disagreement < disagreement_threshold:
        return None, 0.0
    
    # Bet in direction of weekly mean (GFS-like forecast)
    # If yesterday was warmer than mean -> expect reversion down
    # If yesterday was cooler than mean -> expect recovery up
    direction = 'down' if disagreement > 0 else 'up'
    confidence = sigmoid((abs_disagreement - disagreement_threshold) / 3.0)
    
    return direction, confidence


# ─── ENSEMBLE LOGIC ───────────────────────────────────────────────────────

APPROACHES = [
    approach_reversion,
    approach_gaussian, 
    approach_regime,
    approach_gaussian_v2,
    approach_pressure,
    approach_forecast_disagreement  # ADDITION: Edge 5
]

APPROACH_NAMES = [
    "Reversion", 
    "Gaussian(48d)", 
    "Regime", 
    "Gaussian v2(30d)", 
    "Pressure", 
    "Forecast Disagreement"  # NEW: Edge 5
]


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
            prob = comb(total, k) * (null_p**k) * ((1-null_p)**(total-k))
            if abs(k - expected) >= abs(correct - expected):
                p_val += prob
        return min(p_val, 1.0)

def bootstrap_ci(results, n_boot=2000, confidence=0.95):
    if not results: return (0, 0)
    n = len(results)
    accs = []
    for _ in range(n_boot):
        sample = [random.choice(results) for _ in range(n)]
        correct = sum(1 for p, a in sample if p == a)
        accs.append(correct / n)
    accs.sort()
    return (accs[int((1-confidence)/2 * n_boot)], accs[int((1+confidence)/2 * n_boot)])

def compute_sharpe(returns, fee_rate=0.05):
    if not returns: return 0.0
    vals = []
    for conf, ok in returns:
        gross = 2 * conf if ok else -2 * conf
        fee = fee_rate * conf
        vals.append(gross - fee)
    n = len(vals)
    if n == 0: return 0.0
    mean = sum(vals) / n
    var = sum((v - mean)**2 for v in vals) / n
    std = math.sqrt(var) if var > 0 else 0.01
    return mean / std if std > 0 else 0.0

def max_drawdown(results, initial=250.0, bet=10.0):
    bankroll = initial
    peak = bankroll
    max_dd = 0.0
    for pred, actual, conf in results:
        ok = (pred == actual)
        position = min(bet * conf, bankroll * 0.08)
        if ok: bankroll += position * 0.95
        else: bankroll -= position
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


def walk_forward_ensemble(days, market, min_conf=0.0, train_days=180, test_days=30):
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            
            predictions = {}
            for i, fn in enumerate(APPROACHES):
                pred, conf = fn(idx, days)
                if pred is not None and conf >= min_conf:
                    predictions[f'a{i+1}'] = (pred, conf)
            
            if len(predictions) >= 2:
                wsum = sum((1 if p=='up' else -1) * c for _, (p, c) in predictions.items())
                aw = sum(c for _, (p, c) in predictions.items())
                if aw > 0:
                    conf = abs(wsum) / aw
                    if conf >= min_conf:
                        predicted = 'up' if wsum > 0 else 'down'
                        results.append((predicted, actual['direction'], conf))
            elif len(predictions) == 1:
                pred, conf = list(predictions.values())[0]
                if conf >= min_conf:
                    results.append((pred, actual['direction'], conf))
        start += test_days
        if start > len(days): break
    return results


def main():
    print("=" * 90)
    print("ENSEMBLE V9 — BACKTEST WITH EDGE 5 INTEGRATED")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Approaches: {len(APPROACHES)} (including Forecast Disagreement)")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Fee rate: {FEE_RATE:.0%}")
    print(f"Edge 7 (Persistence): EXCLUDED")
    print(f"Edge 2 (Time-of-Day Decay): FAILED TO SIGNAL DURING TESTING")
    print()
    print("  Approaches in ensemble:")
    for i, name in enumerate(APPROACH_NAMES):
        print(f"    {i+1}. {name}")
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)
    
    # ─── PART 1: Per-station ensemble accuracy ─────────────────────────────
    print()
    print("=" * 90)
    print("PART 1: ENSEMBLE ACCURACY (conf ≥0.7)")
    print("=" * 90)
    
    print(f"\n{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Sharpe':>8} {'MaxDD':>8}")
    print("-" * 65)
    
    all_results = []
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue
        results = walk_forward_ensemble(days, market, min_conf=0.7)
        if not results: continue
        
        total = len(results)
        correct = sum(1 for p, a, c in results if p == a)
        acc = correct / total
        sharpe = compute_sharpe([(c, p==a) for p, a, c in results])
        dd = max_drawdown(results)
        
        all_results.extend(results)
        
        print(f"{station:<8} {total:>8} {correct:>8} {acc:>10.2%} {sharpe:>8.3f} {dd:>8.2%}")
    
    # Aggregate
    total = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total if total > 0 else 0
    if total > 0:
        sharpe = compute_sharpe([(c, p==a) for p, a, c in all_results])
        dd = max_drawdown(all_results)
        ci = bootstrap_ci([(p, a) for p, a, c in all_results])
        
        print(f"\n{'AGGREGATE':<8} {total:>8} {correct:>8} {accuracy:>10.2%} {sharpe:>8.3f} {dd:>8.2%}")
        print(f"  95% CI: [{ci[0]:.2%}, {ci[1]:.2%}]")
        print(f"  Circularity check: {accuracy == 1.0}")
    
    # ─── PART 2: Individual approach evaluation ──────────────────────────
    print()
    print("=" * 90)
    print("PART 2: INDIVIDUAL APPROACH PERFORMANCE (separate walk-forward)")
    print("=" * 90)
    
    print(f"\n{'Approach':<20} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10}")
    print("-" * 65)
    
    for i, approach in enumerate(APPROACHES):
        name = APPROACH_NAMES[i]
        approach_total = 0
        approach_correct = 0
        coverage_days = 0
        total_days = 0
        
        for station in ALL_STATIONS:
            days, market = load_station_data(station, conn)
            if len(days) < 210: continue
            
            # Run walk-forward test with single approach
            start = 180
            test_days = 30
            while start + test_days <= len(days):
                for idx in range(start, min(start + test_days, len(days))):
                    date = days[idx]['date']
                    actual = market.get(date)
                    if actual is None: continue
                    
                    total_days += 1
                    pred, conf = approach(idx, days)
                    
                    if pred is not None and conf >= 0.7:
                        approach_total += 1
                        if pred == actual['direction']:
                            approach_correct += 1
                        coverage_days += 1
                
                start += test_days
                if start > len(days): break
        
        approc_acc = approach_correct / approach_total if approach_total > 0 else 0
        approc_cov = coverage_days / total_days if total_days > 0 else 0
        
        print(f"{name:<20} {approach_total:>8} {approach_correct:>8} {approc_acc:>10.2%} {approc_cov:>10.1%}")
    
    # ─── PART 3: Confidence-gated results ─────────────────────────────────
    print()
    print("=" * 90)
    print("PART 3: CONFIDENCE-GATED AGGREGATE RESULTS")
    print("=" * 90)
    
    for threshold in [0.0, 0.5, 0.6, 0.7, 0.8, 0.9]:
        if total == 0:
            print(f"  conf ≥ {threshold:.1f}: 0 trades")
            continue
            
        gated = [(p, a, c) for p, a, c in all_results if c >= threshold]
        if not gated:
            print(f"  conf ≥ {threshold:.1f}: 0 trades")
            continue
        gt = len(gated)
        gc = sum(1 for p, a, c in gated if p == a)
        ga = gc / gt if gt > 0 else 0
        sharpe_gt = compute_sharpe([(c, p==a) for p, a, c in gated]) if gated else 0
        print(f"  conf ≥ {threshold:.1f}: {gt:>6} trades, {gc:>6} correct, {ga:>7.2%} (SR: {sharpe_gt:.3f})")
    
    # ─── VERDICT ──────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("V9 VERDICT")
    print("=" * 90)
    
    v8_accuracy = 0.6479  # From Ensemble v8 run
    accuracy_improvement = accuracy - v8_accuracy if total > 0 else 0
    
    print(f"\n  Ensemble v8 accuracy: {v8_accuracy:.2%}")
    print(f"  Ensemble v9 accuracy: {accuracy:.2%} ({'+' if accuracy_improvement >= 0 else ''}{accuracy_improvement:.2%})")
    print(f"  Sharpe: {sharpe:.3f}")
    print(f"  Max drawdown: {dd:.2%}")
    print(f"  Total trades: {total}")
    print(f"  Binomial p-value: {binomial_test_p(correct, total):.4f}")
    
    if total > 0:
        if accuracy >= 0.58:
            print(f"\n  ✓ ENSEMBLE PASSES 58% threshold")
        else:
            print(f"\n  ✗ ENSEMBLE FAILS 58% threshold")
    
        print(f"  Forecast Disagreement signal: ADDED to ensemble")
        print(f"  Edge 2 (Time-of-Day Decay): SKIPPED - no signal during testing")
        
        if abs(accuracy_improvement) < 0.03 and accuracy_improvement != 0:
            print(f"\n  ⚠️  IMPROVEMENT <3% — evaluate if complexity gain worth benefit")
        elif accuracy_improvement >= 0.03:
            print(f"\n  ✓ SIGNIFICANT IMPROVEMENT ≥3% — value added by Edge 5")

    else:
        print(f"\n  ⚠️  NO TRADES GENERATED - unable to validate ensemble performance")
    
    conn.close()
    print(f"\n{'='*90}")
    print("ENSEMBLE V9 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
