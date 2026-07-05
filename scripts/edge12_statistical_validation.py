#!/usr/bin/env python3
"""
EDGE 12: Statistical Validation Framework

- Benjamini-Hochberg FDR correction (α=0.05) across 16 hypotheses (8 cities × 2 directions)
- Walk-forward validation with bootstrap CI on accuracy
- Permutation test for significance (p<0.01)
- Cross-station permutation test

Standalone Python. No AI in the loop. Walk-forward only.
"""

import sqlite3
import math
import random
from collections import defaultdict
import os

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# 8 cities for FDR (top by data volume)
FDR_STATIONS = ['KNYC', 'KMIA', 'KDEN', 'KMDW', 'KLAX', 'KPHX', 'KATL', 'KBOS']

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
                'reversion': r[3]
            }
    return days, market


def compute_reversion_signal(idx, days):
    """Core reversion signal: z-score from 30-day window."""
    if idx < 31:
        return None, 0.0
    window = days[idx-31:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h - mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 0.5:
        return 'down', abs(z)
    elif z < -0.5:
        return 'up', abs(z)
    return None, 0.0


def compute_gaussian_signal(idx, days):
    """Gaussian signal: z-score from 48-day window."""
    if idx < 48:
        return None, 0.0
    window = days[idx-48:idx-1]
    highs = [d['high'] for d in window]
    mean = sum(highs) / len(highs)
    var = sum((h - mean)**2 for h in highs) / len(highs)
    std = math.sqrt(var) if var > 0 else 0.01
    z = (days[idx-1]['high'] - mean) / std if std > 0 else 0
    if z > 1.0:
        return 'down', abs(z)
    elif z < -1.0:
        return 'up', abs(z)
    return None, 0.0


def walk_forward_predictions(days, market, signal_fn, train_days=180, test_days=30):
    """Run walk-forward backtest for a single signal. Returns list of (predicted, actual)."""
    results = []
    start = train_days
    while start + test_days <= len(days):
        for idx in range(start, min(start + test_days, len(days))):
            date = days[idx]['date']
            actual = market.get(date)
            if actual is None: continue
            pred, conf = signal_fn(idx, days)
            if pred is not None:
                results.append((pred, actual['direction']))
        start += test_days
        if start > len(days): break
    return results


def benjamini_hochberg(p_values, alpha=0.05):
    """
    Benjamini-Hochberg FDR correction.
    Returns list of (index, p_value, rejected) tuples.
    """
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = [False] * n
    
    # Work from largest to smallest
    max_k = 0
    for rank, (orig_idx, p) in enumerate(indexed, 1):
        threshold = (rank * alpha) / n
        if p <= threshold:
            max_k = max(max_k, rank)
    
    # Reject all up to max_k
    for rank, (orig_idx, p) in enumerate(indexed, 1):
        if rank <= max_k:
            rejected[orig_idx] = True
    
    return [(indexed[i][0], indexed[i][1], rejected[indexed[i][0]]) for i in range(n)]


def binomial_test(correct, total, null_p=0.5):
    """Exact binomial test (two-sided). Returns p-value."""
    if total == 0: return 1.0
    p = correct / total
    # Normal approximation for large n
    if total > 100:
        z = (p - null_p) * math.sqrt(total) / math.sqrt(null_p * (1 - null_p))
        # Two-sided p-value from normal CDF
        p_val = 2 * (1 - normal_cdf(abs(z)))
        return p_val
    else:
        # Exact: sum of binomial probabilities
        from math import comb
        p_val = 0
        expected = null_p * total
        for k in range(total + 1):
            prob = comb(total, k) * (null_p ** k) * ((1 - null_p) ** (total - k))
            if abs(k - expected) >= abs(correct - expected):
                p_val += prob
        return min(p_val, 1.0)


def normal_cdf(x):
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bootstrap_ci(results, n_bootstrap=10000, confidence=0.95):
    """Bootstrap confidence interval on accuracy."""
    if not results: return (0, 0)
    n = len(results)
    accuracies = []
    for _ in range(n_bootstrap):
        sample = [random.choice(results) for _ in range(n)]
        correct = sum(1 for p, a in sample if p == a)
        accuracies.append(correct / n)
    accuracies.sort()
    lower = accuracies[int((1 - confidence) / 2 * n_bootstrap)]
    upper = accuracies[int((1 + confidence) / 2 * n_bootstrap)]
    return (lower, upper)


def permutation_test(results, n_perm=1000):
    """Permutation test: shuffle predictions and measure accuracy. Returns p-value."""
    if not results: return 1.0
    actual_acc = sum(1 for p, a in results if p == a) / len(results)
    count_better = 0
    for _ in range(n_perm):
        shuffled_preds = [r[0] for r in results]
        random.shuffle(shuffled_preds)
        perm_acc = sum(1 for p, a in zip(shuffled_preds, [r[1] for r in results]) if p == a) / len(results)
        if perm_acc >= actual_acc:
            count_better += 1
    return (count_better + 1) / (n_perm + 1)


def cross_station_permutation_test(station_results):
    """
    Cross-station permutation test.
    For each station, compare its accuracy against the distribution of accuracies
    obtained by randomly relabeling predictions across stations.
    """
    if len(station_results) < 2: return 1.0
    all_preds = []
    all_actuals = []
    for station, results in station_results.items():
        for p, a in results:
            all_preds.append(p)
            all_actuals.append(a)
    
    # Observed: each station's accuracy
    observed_accs = {}
    idx = 0
    for station, results in station_results.items():
        n = len(results)
        correct = sum(1 for p, a in results if p == a)
        observed_accs[station] = correct / n if n > 0 else 0
        idx += n
    
    # Permutation: shuffle all predictions, redistribute to stations
    n_perm = 1000
    count_better = 0
    combined_acc = sum(observed_accs.values()) / len(observed_accs)
    
    for _ in range(n_perm):
        random.shuffle(all_preds)
        perm_accs = {}
        idx = 0
        for station, results in station_results.items():
            n = len(results)
            correct = sum(1 for p, a in zip(all_preds[idx:idx+n], [r[1] for r in results]) if p == a)
            perm_accs[station] = correct / n if n > 0 else 0
            idx += n
        perm_combined = sum(perm_accs.values()) / len(perm_accs)
        if perm_combined >= combined_acc:
            count_better += 1
    
    return (count_better + 1) / (n_perm + 1)


def main():
    print("=" * 90)
    print("EDGE 12: STATISTICAL VALIDATION FRAMEWORK")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"FDR stations: {FDR_STATIONS}")
    print(f"Signals: Reversion (30-day), Gaussian (48-day)")
    print()
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    random.seed(42)  # Reproducible
    
    # ─── Part 1: Per-station accuracy + binomial test ─────────────────────
    print("=" * 90)
    print("PART 1: Per-Station Accuracy + Binomial Tests")
    print("=" * 90)
    
    station_results_rev = {}
    station_results_gauss = {}
    
    print(f"\n{'Station':<8} {'Signal':<15} {'Acc':>8} {'Trades':>8} {'Binom p':>10} {'95% CI':>16} {'Perm p':>10}")
    print("-" * 85)
    
    all_p_values = []
    
    for station in FDR_STATIONS:
        days, market = load_station_data(station, conn)
        
        # Reversion signal
        results_rev = walk_forward_predictions(days, market, compute_reversion_signal)
        station_results_rev[station] = results_rev
        
        if results_rev:
            correct_rev = sum(1 for p, a in results_rev if p == a)
            total_rev = len(results_rev)
            acc_rev = correct_rev / total_rev
            p_rev = binomial_test(correct_rev, total_rev)
            ci_rev = bootstrap_ci(results_rev)
            perm_p_rev = permutation_test(results_rev, n_perm=500)
            
            print(f"{station:<8} {'Reversion':<15} {acc_rev:>8.2%} {total_rev:>8} {p_rev:>10.4f} [{ci_rev[0]:.2%}, {ci_rev[1]:.2%}] {perm_p_rev:>10.4f}")
            all_p_values.append(p_rev)
        
        # Gaussian signal
        results_gauss = walk_forward_predictions(days, market, compute_gaussian_signal)
        station_results_gauss[station] = results_gauss
        
        if results_gauss:
            correct_g = sum(1 for p, a in results_gauss if p == a)
            total_g = len(results_gauss)
            acc_g = correct_g / total_g
            p_g = binomial_test(correct_g, total_g)
            ci_g = bootstrap_ci(results_gauss)
            perm_p_g = permutation_test(results_gauss, n_perm=500)
            
            print(f"{station:<8} {'Gaussian':<15} {acc_g:>8.2%} {total_g:>8} {p_g:>10.4f} [{ci_g[0]:.2%}, {ci_g[1]:.2%}] {perm_p_g:>10.4f}")
            all_p_values.append(p_g)
    
    # ─── Part 2: Benjamini-Hochberg FDR correction ────────────────────────
    print()
    print("=" * 90)
    print("PART 2: Benjamini-Hochberg FDR Correction (α=0.05)")
    print("=" * 90)
    
    bh_results = benjamini_hochberg(all_p_values, alpha=0.05)
    
    signal_labels = []
    for station in FDR_STATIONS:
        signal_labels.append(f"{station} Reversion")
        signal_labels.append(f"{station} Gaussian")
    
    print(f"\n{'Rank':<6} {'Hypothesis':<25} {'p-value':>10} {'BH Threshold':>14} {'Rejected?':>10}")
    print("-" * 70)
    
    n_significant = 0
    for rank, (orig_idx, p, rejected) in enumerate(bh_results, 1):
        threshold = (rank * 0.05) / len(all_p_values)
        label = signal_labels[orig_idx] if orig_idx < len(signal_labels) else f"Test {orig_idx}"
        status = "✓ REJECT" if rejected else "keep"
        print(f"{rank:<6} {label:<25} {p:>10.4f} {threshold:>14.4f} {status:>10}")
        if rejected:
            n_significant += 1
    
    print(f"\n  Total hypotheses: {len(all_p_values)}")
    print(f"  Significant after FDR: {n_significant}")
    print(f"  FDR rate: {n_significant}/{len(all_p_values)} = {n_significant/len(all_p_values):.1%}")
    
    # ─── Part 3: Cross-station permutation test ───────────────────────────
    print()
    print("=" * 90)
    print("PART 3: Cross-Station Permutation Test")
    print("=" * 90)
    
    cross_p_rev = cross_station_permutation_test(station_results_rev)
    cross_p_gauss = cross_station_permutation_test(station_results_gauss)
    
    print(f"\n  Reversion cross-station p-value: {cross_p_rev:.4f}")
    print(f"  Gaussian cross-station p-value:  {cross_p_gauss:.4f}")
    
    if cross_p_rev < 0.01:
        print(f"  ✓ Reversion signal is statistically significant (p<0.01)")
    else:
        print(f"  ✗ Reversion signal is NOT significant at p<0.01")
    
    if cross_p_gauss < 0.01:
        print(f"  ✓ Gaussian signal is statistically significant (p<0.01)")
    else:
        print(f"  ✗ Gaussian signal is NOT significant at p<0.01")
    
    # ─── Part 4: Summary ──────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("VALIDATION SUMMARY")
    print("=" * 90)
    
    # Overall accuracy
    all_rev = [(p, a) for results in station_results_rev.values() for p, a in results]
    all_gauss = [(p, a) for results in station_results_gauss.values() for p, a in results]
    
    rev_correct = sum(1 for p, a in all_rev if p == a)
    rev_total = len(all_rev)
    rev_acc = rev_correct / rev_total if rev_total > 0 else 0
    
    gauss_correct = sum(1 for p, a in all_gauss if p == a)
    gauss_total = len(all_gauss)
    gauss_acc = gauss_correct / gauss_total if gauss_total > 0 else 0
    
    print(f"\n  Reversion signal:")
    print(f"    Overall accuracy: {rev_acc:.2%} ({rev_correct}/{rev_total})")
    print(f"    FDR significant: {sum(1 for _, _, r in bh_results[:8] if r)}/8 stations")
    print(f"    Cross-station p: {cross_p_rev:.4f}")
    
    print(f"\n  Gaussian signal:")
    print(f"    Overall accuracy: {gauss_acc:.2%} ({gauss_correct}/{gauss_total})")
    print(f"    FDR significant: {sum(1 for _, _, r in bh_results[8:] if r)}/8 stations")
    print(f"    Cross-station p: {cross_p_gauss:.4f}")
    
    # Circularity check
    if rev_acc == 1.0 or gauss_acc == 1.0:
        print("\n  ⚠️  CIRCULARITY DETECTED — 100% accuracy — STOP AND FIX")
    
    # Signal noise check
    if n_significant == 0:
        print("\n  ⚠️  WARNING: No hypotheses survive FDR correction — signals may be noise")
        print("  ESCALATION NEEDED: Reassess whether METAR-only signals have real edge")
    else:
        print(f"\n  ✓ {n_significant} hypotheses survive FDR correction — signals are statistically real")
    
    conn.close()
    print(f"\n{'='*90}")
    print("EDGE 12 COMPLETE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
