#!/usr/bin/env python3
"""
Threshold Sweep for Hourly Late-Day Momentum Signal

Sweeps RATE_THRESHOLD (1.2 – 2.5 °F/hr) and MIN_CONFIDENCE gates (0.5, 0.6, 0.7)
to find settings that maximize Sharpe while keeping coverage ≥20% and accuracy ≥55%.

Outputs a table of all combos + recommended defaults.

Usage:
    python3 scripts/sweep_late_day_thresholds.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

import sqlite3
import math
import sys
import argparse
import numpy as np
from pathlib import Path
from collections import defaultdict
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")

# Import the module functions we need to override
sys.path.insert(0, str(REPO_ROOT / "core"))
import late_day_momentum_hourly as ldm
from market_cost_model import MARKET_COST_MODEL

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

# Sweep ranges
THRESHOLDS = [round(x, 1) for x in np.arange(1.2, 2.6, 0.1)]
CONF_GATES = [0.0, 0.5, 0.6, 0.7]

FEE_RATE = MARKET_COST_MODEL.round_trip_fraction()


def load_settlement_data(start_date=None, end_date=None):
    conn = sqlite3.connect(METAR_DB, timeout=10)
    c = conn.cursor()
    query = """
        SELECT ds.station, ds.date_utc, ds.max_temp_f, ds.min_temp_f,
               se.settlement_bucket, se.prior_settlement_bucket,
               se.local_trading_date
        FROM daily_stats ds
        JOIN settlement_epochs se
        ON ds.date_utc = se.local_trading_date AND ds.station = se.station
        WHERE se.market_type = 'HIGH'
        AND se.epoch_status = 'closed'
        AND se.settlement_bucket IS NOT NULL
        AND se.prior_settlement_bucket IS NOT NULL
        AND ds.max_temp_f IS NOT NULL
    """
    params = []
    if start_date:
        query += " AND ds.date_utc >= ?"
        params.append(start_date)
    if end_date:
        query += " AND ds.date_utc <= ?"
        params.append(end_date)
    query += " ORDER BY ds.date_utc, ds.station"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows


def get_actual_direction(settlement, prior):
    if settlement > prior:
        return 'up'
    elif settlement < prior:
        return 'down'
    else:
        return 'flat'


def compute_slope(hours, temps):
    n = len(hours)
    if n < 2:
        return 0.0
    X = np.array(hours, dtype=float)
    Y = np.array(temps, dtype=float)
    sum_x = X.sum()
    sum_y = Y.sum()
    sum_xy = (X * Y).sum()
    sum_x_sq = (X ** 2).sum()
    denom = n * sum_x_sq - sum_x ** 2
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def consistency_score(hours, temps, slope):
    if len(temps) < 3:
        return 0.5
    agreeing = 0
    total = 0
    for i in range(len(temps) - 1):
        dh = hours[i + 1] - hours[i]
        if dh == 0:
            continue
        seg_slope = (temps[i + 1] - temps[i]) / dh
        if abs(seg_slope) > 0.1:
            total += 1
            if (seg_slope > 0) == (slope > 0):
                agreeing += 1
    return agreeing / total if total > 0 else 0.5


def load_hourly_obs(station, date_str, conn):
    c = conn.cursor()
    c.execute("""
        SELECT CAST(strftime('%H', timestamp_utc) AS INTEGER) AS hour,
               temp_f
        FROM metar_observations
        WHERE station = ?
          AND date_utc = ?
          AND temp_f IS NOT NULL
          AND CAST(strftime('%H', timestamp_utc) AS INTEGER) >= 17
          AND CAST(strftime('%H', timestamp_utc) AS INTEGER) <= 22
        ORDER BY timestamp_utc
    """, (station, date_str))
    rows = c.fetchall()
    seen_hours = {}
    for hour, temp in rows:
        if hour not in seen_hours:
            seen_hours[hour] = temp
    return sorted(seen_hours.items())


def run_sweep(rows, metar_conn, threshold, conf_gate):
    """Run backtest for a specific threshold + confidence gate combination."""
    trades = []
    total_evaluated = 0
    non_trades = 0

    by_station = defaultdict(list)
    for row in rows:
        by_station[row[0]].append(row)

    for station in STATIONS:
        if station not in by_station:
            continue
        station_rows = by_station[station]
        for idx in range(len(station_rows) - 1):
            st, date, max_temp, min_temp, settlement, prior, trade_date = station_rows[idx]
            next_row = station_rows[idx + 1]
            _, _, _, _, next_settlement, next_prior, _ = next_row
            actual_dir = get_actual_direction(next_settlement, next_prior)
            if actual_dir == 'flat':
                continue

            total_evaluated += 1

            # Compute signal with overridden threshold
            obs = load_hourly_obs(st, date, metar_conn)
            if len(obs) < ldm.MIN_OBS:
                non_trades += 1
                continue

            hours = [h for h, _ in obs]
            temps = [t for _, t in obs]
            slope = compute_slope(hours, temps)

            if abs(slope) < threshold:
                non_trades += 1
                continue

            direction = "up" if slope > 0 else "down"
            excess = abs(slope) - threshold
            base_conf = min(1.0, excess / (threshold * 2))
            cons = consistency_score(hours, temps, slope)
            base_conf *= (0.5 + cons * 0.5)
            confidence = min(ldm.MAX_CONFIDENCE, base_conf)

            # Apply confidence gate
            if confidence < conf_gate:
                non_trades += 1
                continue

            raw_prob = 0.50 + min(0.40, abs(slope) / (threshold * 4))
            if direction == "down":
                raw_prob = 1.0 - raw_prob

            is_correct = (direction == actual_dir)
            pnl = (1.0 - 2 * FEE_RATE) if is_correct else (-1.0 - 2 * FEE_RATE)
            trades.append({
                'station': st,
                'date': date,
                'direction': direction,
                'actual': actual_dir,
                'correct': is_correct,
                'confidence': confidence,
                'raw_prob': raw_prob,
                'pnl': pnl
            })

    # Compute metrics
    n_trades = len(trades)
    if n_trades == 0:
        return {
            'threshold': threshold,
            'conf_gate': conf_gate,
            'trades': 0,
            'accuracy': 0.0,
            'sharpe': 0.0,
            'coverage': 0.0,
            'profit_factor': 0.0,
            'avg_pnl': 0.0,
            'passes': False
        }

    correct = sum(1 for t in trades if t['correct'])
    accuracy = correct / n_trades
    pnl_stream = [t['pnl'] for t in trades]

    avg_pnl = sum(pnl_stream) / len(pnl_stream)
    var_pnl = sum((p - avg_pnl) ** 2 for p in pnl_stream) / max(1, len(pnl_stream) - 1)
    std_pnl = math.sqrt(var_pnl)
    sharpe = (avg_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0

    coverage = n_trades / total_evaluated if total_evaluated > 0 else 0.0

    gains = sum(p for p in pnl_stream if p > 0)
    losses = abs(sum(p for p in pnl_stream if p < 0))
    profit_factor = gains / losses if losses > 0 else float('inf')

    passes = (coverage >= 0.20 and accuracy >= 0.55 and n_trades >= 20)

    return {
        'threshold': threshold,
        'conf_gate': conf_gate,
        'trades': n_trades,
        'accuracy': accuracy,
        'sharpe': sharpe,
        'coverage': coverage,
        'profit_factor': profit_factor,
        'avg_pnl': avg_pnl,
        'passes': passes
    }


def main():
    parser = argparse.ArgumentParser(description='Threshold sweep for late-day momentum signal')
    parser.add_argument('--start', type=str, default='2024-01-01')
    parser.add_argument('--end', type=str, default='2026-07-01')
    args = parser.parse_args()

    print("=" * 110)
    print("Threshold Sweep — Hourly Late-Day Momentum Signal")
    print(f"Date range: {args.start} to {args.end}")
    print(f"Thresholds: {THRESHOLDS}")
    print(f"Confidence gates: {CONF_GATES}")
    print("=" * 110)
    print()

    rows = load_settlement_data(args.start, args.end)
    print(f"Loaded {len(rows)} settlement records across {len(set(r[0] for r in rows))} stations")

    metar_conn = sqlite3.connect(METAR_DB, timeout=10)

    results = []
    for threshold in THRESHOLDS:
        for conf_gate in CONF_GATES:
            result = run_sweep(rows, metar_conn, threshold, conf_gate)
            results.append(result)

    metar_conn.close()

    # Print results table
    header = f"{'Threshold':>10} {'ConfGate':>8} {'Trades':>7} {'Accuracy':>9} {'Sharpe':>8} {'Coverage':>9} {'PF':>7} {'AvgPnL':>8} {'PASS':>5}"
    print(header)
    print("-" * len(header))

    for r in results:
        pf_str = "inf" if math.isinf(r['profit_factor']) else f"{r['profit_factor']:.2f}"
        pass_str = "✓" if r['passes'] else ""
        print(f"{r['threshold']:>10.1f} {r['conf_gate']:>8.1f} {r['trades']:>7} {r['accuracy']:>9.2%} "
              f"{r['sharpe']:>8.2f} {r['coverage']:>9.2%} {pf_str:>7} {r['avg_pnl']:>8.4f} {pass_str:>5}")

    print()

    # Filter passing results and find best by Sharpe
    passing = [r for r in results if r['passes']]
    if not passing:
        print("⚠ No parameter combinations pass all criteria (coverage ≥20%, accuracy ≥55%, trades ≥20)")
        # Find best by Sharpe anyway
        best_by_sharpe = max(results, key=lambda r: r['sharpe'])
        print(f"\nBest by Sharpe (does not pass all criteria):")
        print(f"  Threshold: {best_by_sharpe['threshold']:.1f} °F/hr")
        print(f"  Conf gate: {best_by_sharpe['conf_gate']:.1f}")
        print(f"  Sharpe: {best_by_sharpe['sharpe']:.2f}")
        print(f"  Accuracy: {best_by_sharpe['accuracy']:.2%}")
        print(f"  Coverage: {best_by_sharpe['coverage']:.2%}")
        print(f"  Trades: {best_by_sharpe['trades']}")
    else:
        best_by_sharpe = max(passing, key=lambda r: r['sharpe'])
        best_by_accuracy = max(passing, key=lambda r: r['accuracy'])
        best_by_pf = max(passing, key=lambda r: r['profit_factor'] if not math.isinf(r['profit_factor']) else 0)

        print(f"\n{'='*80}")
        print(f"PASSED combinations ({len(passing)} of {len(results)}):")
        print(f"{'='*80}")
        for r in passing:
            pf_str = "inf" if math.isinf(r['profit_factor']) else f"{r['profit_factor']:.2f}"
            print(f"  thresh={r['threshold']:.1f}  conf={r['conf_gate']:.1f}  "
                  f"acc={r['accuracy']:.2%}  sharpe={r['sharpe']:.2f}  "
                  f"cov={r['coverage']:.2%}  pf={pf_str}  trades={r['trades']}")

        print(f"\n{'='*80}")
        print("RECOMMENDED DEFAULTS (best Sharpe among passing):")
        print(f"{'='*80}")
        print(f"  RATE_THRESHOLD = {best_by_sharpe['threshold']:.1f}")
        print(f"  MIN_CONFIDENCE = {best_by_sharpe['conf_gate']:.1f}")
        print(f"  Expected Sharpe:    {best_by_sharpe['sharpe']:.2f}")
        print(f"  Expected Accuracy: {best_by_sharpe['accuracy']:.2%}")
        print(f"  Expected Coverage:  {best_by_sharpe['coverage']:.2%}")
        pf_str = "inf" if math.isinf(best_by_sharpe['profit_factor']) else f"{best_by_sharpe['profit_factor']:.2f}"
        print(f"  Profit Factor:      {pf_str}")
        print(f"  Trades:             {best_by_sharpe['trades']}")

    # Also print as JSON for machine consumption
    print(f"\n--- JSON OUTPUT ---")
    import json
    output = {
        'sweep_date': datetime.now().isoformat(),
        'date_range': {'start': args.start, 'end': args.end},
        'results': [{k: v for k, v in r.items()} for r in results],
        'recommended': {
            'threshold': best_by_sharpe['threshold'],
            'conf_gate': best_by_sharpe['conf_gate'],
            'sharpe': best_by_sharpe['sharpe'],
            'accuracy': best_by_sharpe['accuracy'],
            'coverage': best_by_sharpe['coverage'],
            'trades': best_by_sharpe['trades']
        } if passing else None
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
