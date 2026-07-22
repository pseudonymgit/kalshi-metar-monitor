#!/usr/bin/env python3
"""
EDGE 15: Pre-Money Validation Protocol

Standardized go/no-go validation report:
- Directional accuracy ≥58%
- Sharpe ≥0.8 (with fees)
- Win rate ≥55%
- Max drawdown <10%
- Binomial test (null=50%)
- Sharpe CI (95%)
- Cross-station permutation test

Produces a standardized validation report that Dan can review.
Runs the full ensemble (6 approaches) through the validation gauntlet.

Standalone Python. No AI in the loop. Walk-forward only.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os
import sys

# Add core modules to path
CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core')
sys.path.insert(0, CORE_DIR)

from market_cost_model import MARKET_COST_MODEL

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
FEE_RATE = MARKET_COST_MODEL.round_trip_fraction()  # 5% round-trip fees

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
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station=? AND market_type='HIGH' AND epoch_status='closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            market[r[0]] = 'up' if r[1] > r[2] else 'down'
    return days, market


# ─── 6 approaches from v7 ───────────────────────────────────────────────────

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

def approach_persistence(idx, days):
    if idx < 2: return None, 0.0
    prev = days[idx-2]['high']
    curr = days[idx-1]['high']
    return ('up' if curr > prev else 'down'), 0.3

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

APPROACHES = [
    approach_reversion, approach_gaussian, approach_persistence,
    approach_regime, approach_gaussian_v2, approach_pressure
]


def walk_forward_ensemble(days, market, min_conf=0.0, train_days=180, test_days=30):
    """Run walk-forward ensemble with weighted vote."""
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
                        results.append((predicted, actual, conf))
            elif len(predictions) == 1:
                pred, conf = list(predictions.values())[0]
                if conf >= min_conf:
                    results.append((pred, actual, conf))
        start += test_days
        if start > len(days): break
    return results


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def binomial_test_p(correct, total, null_p=0.5):
    """Two-sided binomial test p-value."""
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


def bootstrap_ci(results, n_boot=5000, confidence=0.95):
    """Bootstrap CI on accuracy."""
    if not results: return (0, 0)
    n = len(results)
    accs = []
    for _ in range(n_boot):
        sample = [random.choice(results) for _ in range(n)]
        correct = sum(1 for p, a, c in sample if p == a)
        accs.append(correct / n)
    accs.sort()
    lo = accs[int((1-confidence)/2 * n_boot)]
    hi = accs[int((1+confidence)/2 * n_boot)]
    return (lo, hi)


def sharpe_with_fees(results, fee_rate=0.05):
    """Compute Sharpe with fees. Position = confidence, payout = ±2*confidence, minus fees."""
    if not results: return 0.0, (0, 0)
    vals = []
    for pred, actual, conf in results:
        ok = (pred == actual)
        gross = 2 * conf if ok else -2 * conf
        fee = fee_rate * conf
        net = gross - fee
        vals.append(net)
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean)**2 for v in vals) / n
    std = math.sqrt(var) if var > 0 else 0.01
    
    sharpe = mean / std if std > 0 else 0.0
    
    # Bootstrap Sharpe CI
    sharpe_samples = []
    for _ in range(2000):
        sample = [random.choice(vals) for _ in range(n)]
        m = sum(sample) / n
        v = sum((s - m)**2 for s in sample) / n
        s = math.sqrt(v) if v > 0 else 0.01
        sharpe_samples.append(m / s if s > 0 else 0)
    sharpe_samples.sort()
    ci = (sharpe_samples[int(0.025 * 2000)], sharpe_samples[int(0.975 * 2000)])
    
    return sharpe, ci


def max_drawdown(results, initial_bankroll=250.0, bet_size=10.0):
    """Compute maximum drawdown from sequential trading."""
    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    
    for pred, actual, conf in results:
        ok = (pred == actual)
        position = min(bet_size * conf, bankroll * 0.08)  # 8% cap
        if ok:
            bankroll += position * 0.95  # win, minus 5% fee
        else:
            bankroll -= position
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    return max_dd


def cross_station_permutation_test(station_results, n_perm=1000):
    """Cross-station permutation test."""
    if len(station_results) < 2: return 1.0
    all_preds = [p for results in station_results.values() for p, a, c in results]
    all_actuals = [a for results in station_results.values() for p, a, c in results]
    
    observed_acc = sum(1 for p, a in zip(all_preds, all_actuals) if p == a) / len(all_preds)
    
    count_better = 0
    for _ in range(n_perm):
        random.shuffle(all_preds)
        perm_acc = sum(1 for p, a in zip(all_preds, all_actuals) if p == a) / len(all_preds)
        if perm_acc >= observed_acc:
            count_better += 1
    
    return (count_better + 1) / (n_perm + 1)


def main():
    print("=" * 90)
    print("EDGE 15: PRE-MONEY VALIDATION PROTOCOL")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"Fee rate: {FEE_RATE:.0%}")
    print(f"Walk-forward: 6-month train / 1-month test")
    print(f"Aggregation: Weighted vote, confidence ≥0.7")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)
    
    MIN_CONF = 0.7
    station_results = {}
    
    for station in ALL_STATIONS:
        days, market = load_station_data(station, conn)
        if len(days) < 210: continue
        results = walk_forward_ensemble(days, market, min_conf=MIN_CONF)
        if results:
            station_results[station] = results
    
    # Aggregate
    all_results = [(p, a, c) for results in station_results.values() for p, a, c in results]
    
    total_trades = len(all_results)
    correct = sum(1 for p, a, c in all_results if p == a)
    accuracy = correct / total_trades if total_trades > 0 else 0
    win_rate = accuracy  # Same as accuracy for binary
    
    sharpe, sharpe_ci = sharpe_with_fees(all_results, fee_rate=FEE_RATE)
    max_dd = max_drawdown(all_results)
    binom_p = binomial_test_p(correct, total_trades)
    ci = bootstrap_ci(all_results)
    cross_p = cross_station_permutation_test(station_results, n_perm=500)
    
    # ─── VALIDATION REPORT ─────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("PRE-MONEY VALIDATION REPORT")
    print("=" * 90)
    
    print(f"""
┌─────────────────────────────────────────────────────────────────────────────┐
│                         GO / NO-GO DECISION REPORT                          │
│                         Generated: 2026-07-02 UTC                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  THRESHOLDS vs ACTUAL                                                       │
│                                                                             │
│  Metric              Threshold     Actual       Status                      │
│  ──────────────────── ──────────── ──────────── ──────                       │
│  Directional accuracy ≥58%         {accuracy:>7.2%}     {'✓ PASS' if accuracy >= 0.58 else '✗ FAIL':>7}                      │
│  Sharpe (with fees)   ≥0.8          {sharpe:>7.3f}     {'✓ PASS' if sharpe >= 0.8 else '✗ FAIL':>7}                      │
│  Win rate             ≥55%          {win_rate:>7.2%}     {'✓ PASS' if win_rate >= 0.55 else '✗ FAIL':>7}                      │
│  Max drawdown         <10%          {max_dd:>7.2%}     {'✓ PASS' if max_dd < 0.10 else '✗ FAIL':>7}                      │
│                                                                             │
│  STATISTICAL TESTS                                                          │
│                                                                             │
│  Binomial test (H₀: 50%)    p = {binom_p:.4f}   {'✓ SIG' if binom_p < 0.05 else '✗ NS':>7}                      │
│  Cross-station permutation  p = {cross_p:.4f}   {'✓ SIG' if cross_p < 0.01 else '✗ NS':>7}                      │
│  Accuracy 95% CI: [{ci[0]:.2%}, {ci[1]:.2%}]                                      │
│  Sharpe 95% CI:   [{sharpe_ci[0]:.3f}, {sharpe_ci[1]:.3f}]                                    │
│                                                                             │
│  TRADING DETAILS                                                            │
│                                                                             │
│  Total trades:    {total_trades:>6}                                                 │
│  Correct:         {correct:>6}                                                 │
│  Stations traded: {len(station_results):>6}                                                 │
│  Confidence gate: ≥{MIN_CONF}                                                │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DECISION: {'GO — PROCEED TO PAPER TRADING' if all([accuracy >= 0.58, sharpe >= 0.8, win_rate >= 0.55, max_dd < 0.10, binom_p < 0.05]) else 'NO-GO — THRESHOLDS NOT MET'}              │
│                                                                             │
│  FAILING: {'All pass' if all([accuracy >= 0.58, sharpe >= 0.8, win_rate >= 0.55, max_dd < 0.10]) else ', '.join([k for k, v in {'accuracy': accuracy >= 0.58, 'sharpe': sharpe >= 0.8, 'win_rate': win_rate >= 0.55, 'max_drawdown': max_dd < 0.10}.items() if not v])}                                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
""")
    
    # Per-station breakdown
    print("PER-STATION BREAKDOWN")
    print("-" * 70)
    print(f"{'Station':<8} {'Trades':>8} {'Accuracy':>10} {'Win Rate':>10} {'Sharpe':>8} {'MaxDD':>8}")
    print("-" * 60)
    
    for station in sorted(station_results.keys()):
        results = station_results[station]
        t = len(results)
        c = sum(1 for p, a, conf in results if p == a)
        acc = c / t if t > 0 else 0
        sh, _ = sharpe_with_fees(results, fee_rate=FEE_RATE)
        dd = max_drawdown(results)
        print(f"{station:<8} {t:>8} {acc:>10.2%} {acc:>10.2%} {sh:>8.3f} {dd:>8.2%}")
    
    # Circularity check
    if accuracy == 1.0:
        print(f"\n⚠️  CIRCULARITY DETECTED — 100% accuracy — STOP AND FIX")
    
    # Final assessment
    print()
    print("=" * 90)
    print("ASSESSMENT")
    print("=" * 90)
    
    # Count passing metrics
    metrics_pass = {
        'accuracy': accuracy >= 0.58,
        'sharpe': sharpe >= 0.8,
        'win_rate': win_rate >= 0.55,
        'max_drawdown': max_dd < 0.10,
    }
    failing_metrics = [k for k, v in metrics_pass.items() if not v]
    
    if accuracy >= 0.58 and binom_p < 0.05:
        print(f"\n  ✓ Directional accuracy is statistically significant: {accuracy:.2%} (p={binom_p:.4f})")
    else:
        print(f"\n  ✗ Directional accuracy is NOT significant: {accuracy:.2%} (p={binom_p:.4f})")
    
    if sharpe < 0.8:
        print(f"  ✗ Sharpe ratio {sharpe:.3f} below 0.8 threshold (signal-limited)")
        print(f"    To reach Sharpe ≥0.8, need accuracy ~75%+ or higher precision on fewer trades")
    
    if max_dd >= 0.10:
        print(f"  ✗ Max drawdown {max_dd:.2%} exceeds 10% threshold")
    
    overall_pass = (accuracy >= 0.58 and sharpe >= 0.8 and win_rate >= 0.55 and max_dd < 0.10 and binom_p < 0.05)
    
    if overall_pass:
        print(f"\n  ╔══════════════════════════════════════════════════╗")
        print(f"  ║  ✅ GO — ALL THRESHOLDS MET, PROCEED TO PAPER    ║")
        print(f"  ║     TRADING WITH $250 BANKROLL                   ║")
        print(f"  ╚══════════════════════════════════════════════════╝")
    else:
        print(f"\n  ╔══════════════════════════════════════════════════╗")
        print(f"  ║  ⛔ NO-GO — THRESHOLDS NOT MET                   ║")
        print(f"  ║     Do NOT trade with real money yet              ║")
        print(f"  ╚══════════════════════════════════════════════════╝")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 15 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
