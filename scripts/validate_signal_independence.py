#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 23.2: Signal Independence Validation — pairwise Spearman, diversity scoring]
#

"""
PHASE 23.2 — Signal Independence Validation

Measures pairwise signal independence for all active signals using Spearman
rank correlation. Identifies reversion cluster dominance and computes
Ensemble Diversity Score (EDS) for the full signal set.

Key metrics:
- Spearman ρ: pairwise signal prediction correlation (-1 to 1)
- Redundancy threshold: |ρ| > 0.70 indicates significant redundancy
- Reversion Cluster Dominance: % of signals that are reversion-based
- EDS: Ensemble Diversity Score (0-1, higher = more diverse)

Usage:
    python3 scripts/validate_signal_independence.py
"""

import sqlite3
import math
import json
import os
import sys
from datetime import datetime
from collections import defaultdict
from typing import Optional, Tuple, List, Dict

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.signals import create_signal_registry
from core.signals.base_signal import _safe_get

# ─── Configuration ─────────────────────────────────────────────────────────────

REDUNDANCY_THRESHOLD = 0.70  # Spearman |ρ| above this = significant redundancy
MIN_FIRING_RATE = 0.01       # Skip signals that fire less than 1% of days

# Reversion-based signals (those that predict mean-reversion)
REVERSION_SIGNAL_NAMES = {
    'fogr_reversion',
    'nwp_dtdt_fusion',
    'metar_dtdt',
    'intraday_metar_confirmation',
    'spread_based_entry',
    'volume_momentum',
    'settlement_arbitrage',
}

# Trend-following signals
TREND_SIGNAL_NAMES = {
    'wind_direction_shift',
    'nwp_direct',
    'pressure_delta',
    'pressure_tendency',
    'forecast_disagreement',
    'temperature_advection',
    'frontal_detector',
    'calendar_climatology',
    'hrrr_bias_corrected',
    'esdr',
}

# All active signals
ALL_SIGNAL_NAMES = REVERSION_SIGNAL_NAMES | TREND_SIGNAL_NAMES


# ─── Station Data Loading ──────────────────────────────────────────────────────

def load_station_days(station: str, conn: sqlite3.Connection) -> List[Dict]:
    """Load daily METAR data for one station."""
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
        if any(v is None for v in r[1:]):
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
            'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7],
        })
    return days


def get_signal_predictions(station: str, conn: sqlite3.Connection,
                          registry) -> Dict[str, List[Tuple[int, str, float]]]:
    """
    Get all signal predictions for a station.
    Returns dict: signal_name -> [(day_idx, direction, confidence)]
    """
    days = load_station_days(station, conn)
    signals = registry.get_all_signals()

    preds = defaultdict(list)
    for sig_name, sig_obj in signals.items():
        if sig_name == 'goldilocks':
            continue  # Intraday arb signal, not for daily

        for idx in range(1, len(days)):
            try:
                direction, confidence = sig_obj.evaluate(idx, days)
                if direction is not None and confidence > 0.1:
                    preds[sig_name].append((idx, direction, confidence))
            except Exception as e:
                # Signals that throw errors are treated as not firing
                pass

    return dict(preds)


# ─── Spearman Rank Correlation ─────────────────────────────────────────────────

def _rank(seq):
    """Assign ranks to a sequence (1-indexed, ties averaged)."""
    n = len(seq)
    indexed = sorted(enumerate(seq), key=lambda x: x[1])
    ranks = [0] * n
    i = 0
    while i < n:
        j = i
        # Find all tied values
        while j < n and abs(indexed[j][1] - indexed[i][1]) < 1e-10:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def spearman_rho(x: List[float], y: List[float]) -> float:
    """
    Compute Spearman rank correlation coefficient between two lists.

    Returns ρ in [-1, 1]. NaN if insufficient data.
    """
    if len(x) < 3 or len(y) < 3:
        return float('nan')
    if len(x) != len(y):
        return float('nan')

    rx = _rank(x)
    ry = _rank(y)

    n = len(x)
    d_sq = sum((rx[i] - ry[i]) ** 2 for i in range(n))

    # Correction for ties
    tx = n - len(set(rx))
    ty = n - len(set(ry))
    denom = n * (n * n - 1) / 6.0
    if denom == 0:
        return float('nan')

    return 1.0 - (d_sq + tx + ty) / denom


def compute_pairwise_correlation(preds_a: List[Tuple[int, str, float]],
                                 preds_b: List[Tuple[int, str, float]]) -> float:
    """
    Compute Spearman correlation between two signal's prediction confidences.
    Only considers days where both signals fired.
    """
    conf_a_by_idx = {idx: conf for idx, _, conf in preds_a}
    conf_b_by_idx = {idx: conf for idx, _, conf in preds_b}

    common_idxs = sorted(set(conf_a_by_idx.keys()) & set(conf_b_by_idx.keys()))
    if len(common_idxs) < 10:
        return float('nan')

    x = [conf_a_by_idx[i] for i in common_idxs]
    y = [conf_b_by_idx[i] for i in common_idxs]

    # Also check direction agreement rate
    dir_a_by_idx = {idx: d for idx, d, _ in preds_a}
    dir_b_by_idx = {idx: d for idx, d, _ in preds_b}
    agreements = sum(1 for i in common_idxs if dir_a_by_idx[i] == dir_b_by_idx[i])
    dir_agreement = agreements / len(common_idxs) if common_idxs else 0.0

    rho = spearman_rho(x, y)
    return rho if not math.isnan(rho) else 0.0


# ─── Ensemble Diversity Score ──────────────────────────────────────────────────

def compute_ensemble_diversity_score(signal_predictions: Dict[str, List[Tuple[int, str, float]]],
                                     station: str) -> Dict:
    """
    Compute Ensemble Diversity Score (EDS) for a station.

    EDS measures how effectively signals provide independent information.
    Based on:
    1. Average pairwise correlation (lower = better)
    2. Direction agreement rate (lower = more diverse)
    3. Firing rate spread (more varied = better)
    4. Reversion vs trend balance
    """
    signal_names = [s for s in ALL_SIGNAL_NAMES if s in signal_predictions]
    if len(signal_names) < 3:
        return {'eds': float('nan'), 'n_signals': len(signal_names), 'station': station}

    # 1. Average pairwise Spearman correlation
    rho_values = []
    for i, s1 in enumerate(signal_names):
        for s2 in signal_names[i + 1:]:
            rho = compute_pairwise_correlation(
                signal_predictions[s1], signal_predictions[s2]
            )
            if not math.isnan(rho):
                rho_values.append(rho)

    avg_rho = sum(rho_values) / len(rho_values) if rho_values else 0.0

    # 2. Direction agreement rate
    dir_agreements = []
    for i, s1 in enumerate(signal_names):
        dirs1 = {idx: d for idx, d, _ in signal_predictions[s1]}
        for s2 in signal_names[i + 1:]:
            dirs2 = {idx: d for idx, d, _ in signal_predictions[s2]}
            common = set(dirs1.keys()) & set(dirs2.keys())
            if len(common) >= 10:
                agree = sum(1 for i in common if dirs1[i] == dirs2[i]) / len(common)
                dir_agreements.append(agree)

    avg_dir_agreement = sum(dir_agreements) / len(dir_agreements) if dir_agreements else 0.5

    # 3. Firing rate spread
    total_days = max(
        (max(idx for idx, _, _ in preds) for preds in signal_predictions.values()),
        default=0
    ) + 1 if signal_predictions else 0

    firing_rates = []
    for sig_name in signal_names:
        preds = signal_predictions[sig_name]
        n_fired = len(preds)
        rate = n_fired / total_days if total_days > 0 else 0
        firing_rates.append(rate)

    firing_rate_std = (sum((r - (sum(firing_rates) / len(firing_rates))) ** 2
                          for r in firing_rates) / len(firing_rates)) ** 0.5 if firing_rates else 0

    # 4. Reversion vs trend balance
    reversion_count = sum(1 for s in signal_names if s in REVERSION_SIGNAL_NAMES)
    trend_count = sum(1 for s in signal_names if s in TREND_SIGNAL_NAMES)
    total_active = reversion_count + trend_count
    reversion_ratio = reversion_count / total_active if total_active > 0 else 0.5

    # Balance score: 1.0 when perfectly balanced (50/50), 0.0 when all one type
    balance_score = 1.0 - 2.0 * abs(reversion_ratio - 0.5)

    # 5. Combined EDS
    # Lower avg_rho = better diversity
    rho_score = 1.0 - abs(avg_rho)

    # Lower direction agreement = better diversity
    agreement_penalty = 1.0 - avg_dir_agreement

    # Higher firing rate spread = better diversity (different signals fire at different times)
    firing_score = min(1.0, firing_rate_std * 5.0)

    # Weighted combination
    eds = (
        0.35 * rho_score +
        0.25 * agreement_penalty +
        0.15 * firing_score +
        0.25 * balance_score
    )

    return {
        'eds': round(eds, 4),
        'avg_pairwise_rho': round(avg_rho, 4),
        'avg_dir_agreement': round(avg_dir_agreement, 4),
        'firing_rate_std': round(firing_rate_std, 4),
        'reversion_ratio': round(reversion_ratio, 4),
        'balance_score': round(balance_score, 4),
        'n_signals_fired': len(signal_names),
        'n_reversion': reversion_count,
        'n_trend': trend_count,
        'station': station,
    }


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_path = 'data/metar_backfill.db'
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return

    # Use DB stations
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM metar_observations ORDER BY station")
    stations = [r[0] for r in cur.fetchall()]

    registry = create_signal_registry(db_path)
    all_signals = registry.get_all_signals()
    print(f"Signal Registry: {len(all_signals)} signals loaded")
    print(f"  Active signals: {sorted(ALL_SIGNAL_NAMES)}")
    print(f"  Reversion-based: {len(REVERSION_SIGNAL_NAMES)}")
    print(f"  Trend-following: {len(TREND_SIGNAL_NAMES)}")

    # Collect per-station results
    station_eds = []
    all_pairwise_rhos = defaultdict(list)
    signal_firing_rates = defaultdict(list)

    for station in stations:
        print(f"\nProcessing {station}...")
        signal_preds = get_signal_predictions(station, conn, registry)

        # Track firing rates
        total_days = 0
        for sig_name, preds in signal_preds.items():
            signal_firing_rates[sig_name].append(len(preds))
            if total_days == 0 and preds:
                total_days = max(idx for idx, _, _ in preds) + 1

        # Compute pairwise correlations
        signal_names = [s for s in ALL_SIGNAL_NAMES if s in signal_preds]
        for i, s1 in enumerate(signal_names):
            for s2 in signal_names[i + 1:]:
                rho = compute_pairwise_correlation(signal_preds[s1], signal_preds[s2])
                if not math.isnan(rho):
                    all_pairwise_rhos[(s1, s2)].append(rho)

        # Compute EDS
        eds_result = compute_ensemble_diversity_score(signal_preds, station)
        station_eds.append(eds_result)
        print(f"  EDS: {eds_result['eds']:.4f}  (ρ={eds_result['avg_pairwise_rho']:.3f}, "
              f"agree={eds_result['avg_dir_agreement']:.3f}, "
              f"rev_ratio={eds_result['reversion_ratio']:.2f})")

    conn.close()

    # ─── Aggregate Results ─────────────────────────────────────────────────────

    # Pairwise correlation summary
    print("\n\n=== PAIRWISE SPEARMAN CORRELATION ===")
    redundant_pairs = []
    for (s1, s2), rho_vals in sorted(all_pairwise_rhos.items()):
        avg_rho = sum(rho_vals) / len(rho_vals)
        status = "⚠️ REDUNDANT" if abs(avg_rho) > REDUNDANCY_THRESHOLD else "OK"
        if abs(avg_rho) > REDUNDANCY_THRESHOLD:
            redundant_pairs.append((s1, s2, avg_rho))
        print(f"  {s1:30s} vs {s2:30s}: ρ = {avg_rho:+.4f}  [{status}]")

    if redundant_pairs:
        print(f"\n⚠️ REDUNDANT SIGNAL PAIRS (|ρ| > {REDUNDANCY_THRESHOLD}):")
        for s1, s2, rho in redundant_pairs:
            print(f"  {s1} ↔ {s2}: ρ = {rho:.4f}")
    else:
        print(f"\n✅ No redundant signal pairs found (|ρ| > {REDUNDANCY_THRESHOLD})")

    # Reversion cluster dominance
    print("\n\n=== REVERSION CLUSTER ANALYSIS ===")
    rev_signal_names = [s for s in REVERSION_SIGNAL_NAMES if s in all_signals or s in signal_firing_rates]
    trend_signal_names = [s for s in TREND_SIGNAL_NAMES if s in all_signals or s in signal_firing_rates]
    print(f"  Reversion signals count: {len(rev_signal_names)}")
    print(f"  Trend signals count: {len(trend_signal_names)}")
    print(f"  Reversion dominance: {len(rev_signal_names) / max(1, len(rev_signal_names) + len(trend_signal_names)):.1%}")

    # Ensemble Diversity Score
    print("\n\n=== ENSEMBLE DIVERSITY SCORE ===")
    valid_eds = [e for e in station_eds if not math.isnan(e['eds'])]
    if valid_eds:
        avg_eds = sum(e['eds'] for e in valid_eds) / len(valid_eds)
        min_eds = min(e['eds'] for e in valid_eds)
        max_eds = max(e['eds'] for e in valid_eds)

        print(f"  Overall EDS: {avg_eds:.4f} (range: {min_eds:.4f} - {max_eds:.4f})")
        print(f"  Stations with data: {len(valid_eds)}/{len(stations)}")

        best = max(valid_eds, key=lambda e: e['eds'])
        worst = min(valid_eds, key=lambda e: e['eds'])
        print(f"  Best station: {best['station']} ({best['eds']:.4f})")
        print(f"  Worst station: {worst['station']} ({worst['eds']:.4f})")
    else:
        print("  No valid EDS data")

    # ─── Save Results ──────────────────────────────────────────────────────────

    report = {
        'timestamp': datetime.now().isoformat(),
        'database': db_path,
        'stations_analyzed': stations,
        'signals_analyzed': sorted(ALL_SIGNAL_NAMES),
        'redundancy_threshold': REDUNDANCY_THRESHOLD,
        'pairwise_spearman': {
            f'{s1} vs {s2}': round(sum(v) / len(v), 4)
            for (s1, s2), v in sorted(all_pairwise_rhos.items())
        },
        'redundant_pairs': [
            {'signal_a': s1, 'signal_b': s2, 'spearman_rho': round(rho, 4)}
            for s1, s2, rho in redundant_pairs
        ],
        'reversion_dominance': {
            'reversion_count': len(rev_signal_names),
            'trend_count': len(trend_signal_names),
            'dominance_ratio': len(rev_signal_names) / max(1, len(rev_signal_names) + len(trend_signal_names)),
        },
        'ensemble_diversity_score': {
            'overall_eds': round(avg_eds, 4) if valid_eds else None,
            'per_station': {
                e['station']: {
                    'eds': e['eds'],
                    'avg_pairwise_rho': e['avg_pairwise_rho'],
                    'avg_dir_agreement': e['avg_dir_agreement'],
                    'balance_score': e['balance_score'],
                    'n_signals_fired': e['n_signals_fired'],
                } for e in station_eds if not math.isnan(e['eds'])
            },
        },
        'recommendations': [],
    }

    # Generate recommendations
    if redundant_pairs:
        for s1, s2, rho in redundant_pairs:
            report['recommendations'].append(
                f"Consider merging or removing one of {s1}/{s2} (ρ={rho:.3f})"
            )

    if reversion_count := len(rev_signal_names):
        total = reversion_count + len(trend_signal_names)
        if total > 0 and reversion_count / total > 0.6:
            report['recommendations'].append(
                f"Reversion cluster dominance: {reversion_count}/{total} signals are reversion-based. "
                "Consider adding more trend-following signals."
            )

    output_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, 'signal_independence_report.json')
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\n✅ Report saved to {output_file}")
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    if valid_eds:
        print(f"  Ensemble Diversity Score: {avg_eds:.4f}")
    else:
        print(f"  Ensemble Diversity Score: N/A")
    print(f"  Redundant pairs: {len(redundant_pairs)}")
    print(f"  Reversion dominance: {len(rev_signal_names)}/{len(rev_signal_names) + len(trend_signal_names)}")


if __name__ == '__main__':
    main()