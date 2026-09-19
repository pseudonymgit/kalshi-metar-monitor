#!/usr/bin/env python3
"""
MINORITY-CLASS EDGE MEASUREMENT — read-only, deterministic, script-only.

Answers Dan's challenge (2026-09-19): does aggregate accuracy/MCC on a ~10:1
market-type-conditional skew HIDE real minority-class edge?

Axis: strike-crossing (settlement_temp_f vs strike_price). Contemporaneous-only.
Ground truth base rates (measured from rt2b_prediction_matrix_cache.json):
  HIGH resolves 'up'  10.13%  (minority = 'up')
  LOW  resolves 'up'  94.64%  (minority = 'down')

For EVERY signal that fired in the ultimate sweep, compute:
  1. MINORITY PRECISION: when signal predicts minority direction, hit-rate vs base rate.
  2. MINORITY RECALL: fraction of rare events the signal actually catches.
  3. NET-OF-PRICE EV: using real contract price = minority-side probability, net of
     round-trip fees (core/market_cost_model MARKET_COST_MODEL + kalshi_fee).
  4. Compare vs null ("always predict majority" = 0 skill, 0 EV).

READ-ONLY. No signal modification, no wiring, no DROP/backfill. No AI.
"""
import json
import math
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.market_cost_model import MARKET_COST_MODEL, ROUND_TRIP_FEE

CACHE = os.path.join(BASE, "reports", "rt2b_prediction_matrix_cache.json")

# Base rates (measured, matching task context)
HIGH_UP_BASE = 0.10129973211628138   # minority in HIGH = 'up'
LOW_UP_BASE = 0.9464329795112318     # minority in LOW = 'down'


def minority_dir(mt):
    """The minority (rare) direction for a given market type."""
    return 'up' if mt == 'HIGH' else 'down'


def minority_base(mt):
    """Base rate (probability) of the minority direction resolving true."""
    if mt == 'HIGH':
        return HIGH_UP_BASE           # P(up) in HIGH
    return 1.0 - LOW_UP_BASE          # P(down) in LOW = 1 - P(up)


def majority_dir(mt):
    return 'down' if mt == 'HIGH' else 'up'


def round_trip_cost(price):
    """Total round-trip cost per contract in dollars, net of the Kalshi taker fee
    (which is price-dependent: ceil(0.07*P*(1-P)*100)/100) plus the fixed
    spread/2 + slippage (ROUND_TRIP_FEE = 2.05c)."""
    if price <= 0.0 or price >= 1.0:
        fee = 0.0
    else:
        fee = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return ROUND_TRIP_FEE + fee


def expected_value(p_trade, p_win):
    """EV of buying a contract at price p_trade that wins with prob p_win, net of
    round-trip cost. Payout is $1 on win, $0 on loss. (We hold to settlement;
    cost is one-way entry spread + taker fee both ways = round_trip_cost.)"""
    cost = round_trip_cost(p_trade)
    # Win: +($1 - p_trade) ; Loss: -$p_trade ; net of cost
    return p_win * (1.0 - p_trade) + (1.0 - p_win) * (-p_trade) - cost


def main():
    with open(CACHE) as f:
        rows = json.load(f)

    # Build per-signal aggregation structures.
    # For each signal: for each market type, collect predictions of minority dir.
    # minority_precision: P(actual == minority | signal predicted minority)
    # minority_recall:    P(signal predicted minority | actual == minority)
    # We also track the signal's predicted direction + confidence, and actual.

    # Aggregate per (signal, market_type)
    sig_mt = defaultdict(lambda: defaultdict(lambda: {
        'pred_min': 0,        # times signal predicted minority direction
        'pred_min_hit': 0,    # of those, times actual was minority (correct)
        'total': 0,           # total labels for this (signal, mt)
        'actual_min': 0,      # total minority outcomes
        'caught_min': 0,      # minority outcomes the signal predicted minority
    }))

    # For net-EV: we need price model. Minority side trades near its probability.
    # In HIGH, "up" (minority) ~$0.10 ; majority "down" ~$0.90.
    # In LOW, "down" (minority) ~$0.05 ; majority "up" ~$0.95.
    # We price the trade at the minority-side probability = base rate (real model).
    # For each minority-direction prediction, EV = p_win * (1-p_trade) - (1-p_win)*p_trade - cost,
    # where p_trade = minority base rate (approx contract price), p_win = realized
    # minority precision for that signal/mt (sample estimate).

    for row in rows:
        mt = row['mt']
        actual = row['actual']
        for sig, sv in row['signals'].items():
            pred_dir = sv[0]
            agg = sig_mt[sig][mt]
            agg['total'] += 1
            is_min = (actual == minority_dir(mt))
            if is_min:
                agg['actual_min'] += 1
            pred_min = (pred_dir == minority_dir(mt))
            if pred_min:
                agg['pred_min'] += 1
                if is_min:
                    agg['pred_min_hit'] += 1
                    agg['caught_min'] += 1

    # Build report
    print("=" * 100)
    print("MINORITY-CLASS EDGE MEASUREMENT — strike-crossing axis, contemporaneous-only")
    print("=" * 100)
    print(f"Base rates:  HIGH up = {HIGH_UP_BASE:.4f} (minority 'up'), "
          f"LOW up = {LOW_UP_BASE:.4f} (minority 'down' = {1-LOW_UP_BASE:.4f})")
    print(f"Round-trip fixed cost (spread/2+slippage) = {ROUND_TRIP_FEE:.4f} "
          f"plus Kalshi taker fee ceil(0.07*P*(1-P)*100)/100")
    print()

    results = []
    for sig in sorted(sig_mt.keys()):
        for mt in sorted(sig_mt[sig].keys()):
            a = sig_mt[sig][mt]
            if a['total'] == 0:
                continue
            base = minority_base(mt)
            mdir = minority_dir(mt)
            # precision: among minority-direction predictions, hit rate
            if a['pred_min'] > 0:
                precision = a['pred_min_hit'] / a['pred_min']
            else:
                precision = None
            # recall: among actual minority outcomes, fraction caught
            if a['actual_min'] > 0:
                recall = a['caught_min'] / a['actual_min']
            else:
                recall = None

            delta = (precision - base) if precision is not None else None

            # Net-of-price EV. Price the minority-side contract at its base rate.
            # p_trade = base (approximate market price of minority side).
            # p_win = precision (sample). Use Wilson lower-bound for sanity later;
            # here report raw precision as p_win estimate.
            if precision is not None and a['pred_min'] >= 1:
                p_trade = base
                p_win = precision
                ev = expected_value(p_trade, p_win)
            else:
                ev = None

            results.append({
                'signal': sig, 'mt': mt, 'minority': mdir,
                'base': base, 'n_total': a['total'],
                'pred_min': a['pred_min'], 'actual_min': a['actual_min'],
                'caught_min': a['caught_min'],
                'precision': precision, 'recall': recall,
                'delta': delta, 'ev': ev,
            })

    # Print table grouped by signal
    print(f"{'signal':<26} {'mt':<5} {'minor':<5} {'base':>7} {'predMin':>7} "
          f"{'prec':>7} {'delta':>7} {'recall':>7} {'netEV$':>8}")
    print("-" * 100)
    prev_sig = None
    for r in results:
        if r['signal'] != prev_sig and prev_sig is not None:
            print("-" * 100)
        prev_sig = r['signal']
        prec_s = f"{r['precision']:.4f}" if r['precision'] is not None else "  —  "
        delta_s = f"{r['delta']:+.4f}" if r['delta'] is not None else "  —  "
        rec_s = f"{r['recall']:.4f}" if r['recall'] is not None else "  —  "
        ev_s = f"{r['ev']:+.4f}" if r['ev'] is not None else "  —  "
        print(f"{r['signal']:<26} {r['mt']:<5} {r['minority']:<5} {r['base']:>7.4f} "
              f"{r['pred_min']:>7} {prec_s:>7} {delta_s:>7} {rec_s:>7} {ev_s:>8}")

    # Summary: any signal with positive net EV and delta > 0 (with min sample)
    print()
    print("=" * 100)
    print("POSITIVE MINORITY EDGE CANDIDATES (delta > 0 AND net EV > 0, min 20 minority preds)")
    print("=" * 100)
    candidates = [r for r in results
                  if r['delta'] is not None and r['ev'] is not None
                  and r['delta'] > 0 and r['ev'] > 0 and r['pred_min'] >= 20]
    if not candidates:
        print("  (none) — no signal clears positive net EV on minority-class prediction")
    else:
        for r in sorted(candidates, key=lambda x: -x['ev']):
            print(f"  {r['signal']:<26} {r['mt']:<5} delta={r['delta']:+.4f} "
                  f"recall={r['recall']:.4f} netEV=${r['ev']:+.4f} n={r['pred_min']}")

    # Also show strongest delta signals regardless of EV (to diagnose)
    print()
    print("STRONGEST PRECISION DELTAS (all, sorted by |delta|, min 20 minority preds):")
    diag = [r for r in results if r['delta'] is not None and r['pred_min'] >= 20]
    diag.sort(key=lambda x: -abs(x['delta']))
    for r in diag[:15]:
        print(f"  {r['signal']:<26} {r['mt']:<5} delta={r['delta']:+.4f} "
              f"prec={r['precision']:.4f} (base {r['base']:.4f}) n={r['pred_min']} "
              f"recall={r['recall']:.4f} netEV=${r['ev']:+.4f}")

    # Dump structured JSON for downstream
    out = os.path.join(BASE, "reports", "minority_class_edge_measurement.json")
    with open(out, "w") as f:
        json.dump({
            'base_rates': {'HIGH_up': HIGH_UP_BASE, 'LOW_up': LOW_UP_BASE},
            'round_trip_fee': ROUND_TRIP_FEE,
            'per_signal_mt': results,
            'positive_edge_candidates': candidates,
        }, f, indent=2)
    print(f"\n[written] {out}")

if __name__ == "__main__":
    main()
