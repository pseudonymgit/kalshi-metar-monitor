#!/usr/bin/env python3
"""
PHASE 0.2: Selection Bias Correction

Applies two corrections to the combinatorial search results:

1. Deflated Sharpe Ratio (Bailey & López de Prado, 2014):
   Accounts for multiple testing across N=1,023 combinations.
   Computes the deflated Sharpe ratio threshold.

2. Bonferroni Correction for Accuracy:
   Adjusted threshold = 0.5 + z_critical * sqrt(0.5*0.5/N)
   where z_critical is Bonferroni-corrected.

No AI/ML in the loop. Self-contained. Deterministic.

Saves results to: data/selection_bias_results.json
"""

import json
import math
import os
import sys
from datetime import datetime

RESULTS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'combinatorial_search_10sig.json')
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'selection_bias_results.json')


def normal_cdf(x):
    """Standard normal CDF using math.erf."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def normal_ppf(p):
    """
    Normal quantile function using the Hastings approximation.
    Based on Abramowitz & Stegun 26.2.23.
    Accurate to ~4.5e-4 for all p in [0,1].
    Verified: ppf(0.975)=1.96, ppf(0.99997)=4.01
    """
    if p <= 0.0:
        return -float('inf')
    if p >= 1.0:
        return float('inf')

    if p < 0.5:
        return -normal_ppf(1.0 - p)

    t = math.sqrt(-2.0 * math.log(1.0 - p))

    # Hastings coefficients (Abramowitz & Stegun 26.2.23)
    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308

    z = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)
    return z


def compute_deflated_sharpe(N_trials, T_obs):
    """
    Compute the Deflated Sharpe Ratio threshold.

    Bailey & Lopez de Prado (2014):
    The deflated Sharpe ratio adjusts the observed Sharpe for
    the number of trials and finite sample length.

    Returns:
        deflated_sharpe_threshold: minimum Sharpe to be significant
        expected_max_sharpe_null:  expected max Sharpe under the null
    """
    gamma = 0.5772156649  # Euler-Mascheroni constant

    if N_trials <= 1:
        return 0.0, 0.0

    log_N = math.log(N_trials)
    if log_N <= 0:
        return 0.0, 0.0

    # Expected maximum of |Z| for N independent normal trials
    expected_max_Z = math.sqrt(2 * log_N) - (math.log(log_N) + gamma) / (2 * math.sqrt(2 * log_N))

    # Scale by 1/sqrt(T) for Sharpe ratio
    expected_max_sharpe = expected_max_Z / math.sqrt(T_obs)

    # Bonferroni-adjusted z-threshold
    alpha_eff = 1.0 / N_trials
    z_threshold = normal_ppf(1.0 - alpha_eff / 2)

    # Deflated Sharpe = z_threshold / sqrt(T)
    deflated_sharpe = z_threshold / math.sqrt(T_obs)

    return deflated_sharpe, expected_max_sharpe


def compute_bonferroni_accuracy_threshold(N_trials, N_min_trades, alpha=0.05):
    """
    Compute the Bonferroni-corrected accuracy floor.

    Under the null (coin flip):
    adjusted_alpha = alpha / N_trials
    z_critical = normal_ppf(1 - adjusted_alpha/2)
    accuracy_floor = 0.5 + z_critical * sqrt(0.5*0.5 / N_min_trades)
    """
    adjusted_alpha = alpha / N_trials
    z_critical = normal_ppf(1.0 - adjusted_alpha / 2)

    se = math.sqrt(0.5 * 0.5 / N_min_trades)
    accuracy_floor = 0.5 + z_critical * se

    return accuracy_floor, z_critical, adjusted_alpha


def main():
    print("=" * 90)
    print("PHASE 0.2: SELECTION BIAS CORRECTION")
    print("=" * 90)
    print(f"Results file: {RESULTS_PATH}")
    print()

    if not os.path.exists(RESULTS_PATH):
        print(f"ERROR: Results file not found at {RESULTS_PATH}")
        sys.exit(1)

    with open(RESULTS_PATH, 'r') as f:
        data = json.load(f)

    # --- Extract combinatorial search parameters ---
    summary = data.get('combinatorial_search_summary', {})
    total_signals = summary.get('total_signals_available', 10)

    all_combos = data.get('by_combination', {})
    total_combos_tested = len(all_combos)

    print(f"Total signals available: {total_signals}")
    print(f"Total combinations tested: {total_combos_tested}")
    print(f"Agreement thresholds tested: {summary.get('agreement_thresholds', [])}")
    print(f"Stations: {len(summary.get('tested_stations', []))}")
    print()

    # Extract all metric entries
    all_metric_entries = []
    for combo_name, result_list in all_combos.items():
        for entry in result_list:
            m = entry['metrics']
            all_metric_entries.append({
                'combo': combo_name,
                'agreement': entry.get('agreement_threshold', '?'),
                'accuracy': m.get('accuracy', 0),
                'sharpe': m.get('sharpe', 0),
                'total_trades': m.get('total_trades', 0),
                'correct': m.get('correct_predictions', 0),
                'stations_tested': m.get('stations_tested', 0)
            })

    print(f"Total metric entries across all combos/thresholds: {len(all_metric_entries)}")
    print()

    # Best overall metrics
    best_accuracy = max(e['accuracy'] for e in all_metric_entries)
    best_acc_entry = max(all_metric_entries, key=lambda e: e['accuracy'])
    best_sharpe = max(e['sharpe'] for e in all_metric_entries)
    best_sharpe_entry = max(all_metric_entries, key=lambda e: e['sharpe'])

    gg_entry = None
    for e in all_metric_entries:
        if e['combo'] == 'goldilocks+gaussian' and e['agreement'] == 2:
            gg_entry = e
            break

    print(f"Best overall accuracy: {best_accuracy:.2%} ({best_acc_entry['combo']}, agree={best_acc_entry['agreement']})")
    print(f"Best overall Sharpe:   {best_sharpe:.4f} ({best_sharpe_entry['combo']}, agree={best_sharpe_entry['agreement']})")
    if gg_entry:
        print(f"Goldilocks+Gaussian acc: {gg_entry['accuracy']:.2%} sharpe: {gg_entry['sharpe']:.4f} trades: {gg_entry['total_trades']}")
    print()

    # --- Part 1: Deflated Sharpe Ratio ---
    print("=" * 90)
    print("PART 1: DEFLATED SHARPE RATIO (Bailey & Lopez de Prado, 2014)")
    print("=" * 90)

    N_trials = total_combos_tested
    active_entries = [e for e in all_metric_entries if e['total_trades'] > 0]
    avg_trades = sum(e['total_trades'] for e in active_entries) / len(active_entries) if active_entries else 0
    med_trades = sorted(e['total_trades'] for e in active_entries)[len(active_entries) // 2] if active_entries else 0

    # Use median trades as representative observation count
    T_obs = max(med_trades, 30)

    deflated_sharpe, expected_max_sharpe = compute_deflated_sharpe(N_trials, T_obs)

    print(f"  Number of trials (N):       {N_trials}")
    print(f"  Representative T (trades):  {T_obs}")
    print(f"  Expected max Sharpe (null): {expected_max_sharpe:.4f}")
    print(f"  Deflated Sharpe threshold:  {deflated_sharpe:.6f}")
    print()

    entries_surviving_sharpe = [e for e in active_entries if e['sharpe'] > deflated_sharpe]
    pct_surviving_sharpe = len(entries_surviving_sharpe) / len(active_entries) * 100 if active_entries else 0
    print(f"  Combos exceeding deflated Sharpe: {len(entries_surviving_sharpe)}/{len(active_entries)} ({pct_surviving_sharpe:.1f}%)")

    if gg_entry:
        gg_survives = gg_entry['sharpe'] > deflated_sharpe
        print(f"  Goldilocks+Gaussian Sharpe: {gg_entry['sharpe']:.4f} - {'SURVIVES' if gg_survives else 'FAILS'} deflated Sharpe")

    survivors_sorted = sorted(entries_surviving_sharpe, key=lambda e: e['sharpe'], reverse=True)
    print(f"\n  Top 10 survivors by Sharpe:")
    print(f"  {'Combo':<55} {'Sharpe':>8} {'Acc':>8} {'Trades':>8}")
    print(f"  {'-'*55} {'-'*8} {'-'*8} {'-'*8}")
    for e in survivors_sorted[:10]:
        combo = e['combo']
        if len(combo) > 53:
            combo = combo[:50] + '...'
        print(f"  {combo:<55} {e['sharpe']:>8.4f} {e['accuracy']:>7.2%} {e['total_trades']:>8}")
    print()

    # --- Part 2: Bonferroni Correction for Accuracy ---
    print("=" * 90)
    print("PART 2: BONFERRONI CORRECTION FOR ACCURACY")
    print("=" * 90)

    N_min_trades = 30
    valid_entries = [e for e in all_metric_entries if e['total_trades'] >= N_min_trades]
    print(f"  Valid combos (>{N_min_trades} trades): {len(valid_entries)}/{len(all_metric_entries)}")

    accuracy_floor, z_critical, adjusted_alpha = compute_bonferroni_accuracy_threshold(
        N_trials, N_min_trades, alpha=0.05
    )

    print(f"  Significance level (alpha):      0.05")
    print(f"  Bonferroni-adjusted alpha:       {adjusted_alpha:.6e}")
    print(f"  Bonferroni-critical z:           {z_critical:.4f}")
    print(f"  Bonferroni-corrected acc floor:  {accuracy_floor:.2%}")
    print(f"  (Any combo below {accuracy_floor:.2%} could be explained by chance)")
    print()

    passed_65 = [e for e in valid_entries if e['accuracy'] >= 0.65]
    passed_bonferroni = [e for e in valid_entries if e['accuracy'] >= accuracy_floor]
    passed_65_unique = set(e['combo'] for e in passed_65)
    passed_bonferroni_unique = set(e['combo'] for e in passed_bonferroni)

    print(f"  Combos passing 65% threshold:     {len(passed_65):>6} ({len(passed_65_unique)} unique)")
    print(f"  Combos passing Bonferroni corr:   {len(passed_bonferroni):>6} ({len(passed_bonferroni_unique)} unique)")

    if gg_entry:
        gg_passes_bonf = gg_entry['accuracy'] >= accuracy_floor
        print(f"  Goldilocks+Gaussian: {gg_entry['accuracy']:.2%} - {'SURVIVES' if gg_passes_bonf else 'FAILS'} Bonferroni")
    print()

    # --- Part 3: Combined Assessment ---
    print("=" * 90)
    print("PART 3: COMBINED ASSESSMENT")
    print("=" * 90)

    passed_both = []
    for e in valid_entries:
        if e['accuracy'] >= accuracy_floor and e['sharpe'] > deflated_sharpe:
            passed_both.append(e)
    passed_both_unique = set(e['combo'] for e in passed_both)

    print(f"  Combos passing BOTH corrections: {len(passed_both)} ({len(passed_both_unique)} unique)")
    if passed_both:
        passed_both.sort(key=lambda e: e['accuracy'] * e['sharpe'], reverse=True)
        print(f"\n  Top 10 by combined score (acc x sharpe):")
        print(f"  {'Combo':<55} {'Acc':>8} {'Sharpe':>8} {'Trades':>8}")
        print(f"  {'-'*55} {'-'*8} {'-'*8} {'-'*8}")
        for e in passed_both[:10]:
            combo = e['combo']
            if len(combo) > 53:
                combo = combo[:50] + '...'
            print(f"  {combo:<55} {e['accuracy']:>7.2%} {e['sharpe']:>8.4f} {e['total_trades']:>8}")

    if gg_entry:
        passes_both = gg_entry['accuracy'] >= accuracy_floor and gg_entry['sharpe'] > deflated_sharpe
        print(f"\n  Goldilocks+Gaussian: acc={gg_entry['accuracy']:.2%}, sharpe={gg_entry['sharpe']:.4f}")
        print(f"  {'SURVIVES both corrections' if passes_both else 'FAILS one or both'}")

    # --- Save results ---
    output = {
        'metadata': {
            'timestamp': datetime.utcnow().isoformat(),
            'source_file': str(RESULTS_PATH),
            'total_combinations_tested': total_combos_tested,
            'total_signals': total_signals,
            'stations_tested': summary.get('tested_stations', [])
        },
        'best_metrics': {
            'best_accuracy': round(best_accuracy, 6),
            'best_accuracy_entry': {
                'combo': best_acc_entry['combo'],
                'agreement': best_acc_entry['agreement'],
                'accuracy': round(best_acc_entry['accuracy'], 6),
                'sharpe': round(best_acc_entry['sharpe'], 6),
                'trades': best_acc_entry['total_trades']
            },
            'best_sharpe': round(best_sharpe, 6),
            'best_sharpe_entry': {
                'combo': best_sharpe_entry['combo'],
                'agreement': best_sharpe_entry['agreement'],
                'accuracy': round(best_sharpe_entry['accuracy'], 6),
                'sharpe': round(best_sharpe_entry['sharpe'], 6),
                'trades': best_sharpe_entry['total_trades']
            }
        },
        'goldilocks_gaussian': {
            'accuracy': round(gg_entry['accuracy'], 6) if gg_entry else None,
            'sharpe': round(gg_entry['sharpe'], 6) if gg_entry else None,
            'trades': gg_entry['total_trades'] if gg_entry else None
        } if gg_entry else None,
        'deflated_sharpe': {
            'num_trials': N_trials,
            'representative_t_obs': T_obs,
            'expected_max_sharpe_null': round(expected_max_sharpe, 6),
            'deflated_sharpe_threshold': round(deflated_sharpe, 6),
            'combos_exceeding_threshold': len(entries_surviving_sharpe),
            'total_active_combos': len(active_entries),
            'goldilocks_gaussian_survives': gg_entry['sharpe'] > deflated_sharpe if gg_entry else None
        },
        'bonferroni_correction': {
            'significance_level': 0.05,
            'adjusted_alpha': adjusted_alpha,
            'critical_z': round(z_critical, 6),
            'min_trades_required': N_min_trades,
            'accuracy_floor': round(accuracy_floor, 6),
            'combos_passing_65pct': len(passed_65),
            'combos_passing_bonferroni': len(passed_bonferroni),
            'unique_combos_passing_65pct': len(passed_65_unique),
            'unique_combos_passing_bonferroni': len(passed_bonferroni_unique),
            'goldilocks_gaussian_survives': gg_entry['accuracy'] >= accuracy_floor if gg_entry else None
        },
        'combined_assessment': {
            'combos_passing_both': len(passed_both),
            'unique_combos_passing_both': len(passed_both_unique),
            'goldilocks_gaussian_survives_both': (
                gg_entry['accuracy'] >= accuracy_floor and gg_entry['sharpe'] > deflated_sharpe
            ) if gg_entry else None,
            'top_10_both': [
                {
                    'combo': e['combo'],
                    'agreement': e['agreement'],
                    'accuracy': round(e['accuracy'], 6),
                    'sharpe': round(e['sharpe'], 6),
                    'trades': e['total_trades']
                }
                for e in passed_both[:10]
            ]
        }
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {OUTPUT_PATH}")
    print("\nPHASE 0.2 COMPLETE")


if __name__ == '__main__':
    main()