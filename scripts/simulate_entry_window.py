#!/usr/bin/env python3
"""
Deterministic entry-WINDOW simulator (Dan 2026-09-19 re-run).

Replaces the prior `simulate_entry_prices.py` offset-based sim. That prior sim
drew entry = "fair price − 5¢ offset ± 2¢ jitter", which baked in a free ~5¢
pricing edge — the reported +4¢ EV was an artifact of that offset, NOT a real
signal edge. This re-run drops the offset and instead models Dan's ACTUAL
entry discipline:

  * ENTRY WINDOW: buy only when the entry price lands in [lower, upper]
    (default 0.45–0.80). Hard ceiling: never buy above `upper`.
  * NO-BET DAYS: days where the price falls OUTSIDE the window are no-bet —
    no position, no P&L, capital idle. They do NOT count as wins/losses,
    they reduce realized return vs always-in-market, and their fraction is
    reported explicitly.
  * CRITICAL COMPARISON: net EV reported BOTH ways —
      (a) WITH no-bet days baked in (honest), and
      (b) assuming always-bet (biased baseline),
    so the no-bet bias is quantified exactly.

PRICING ASSUMPTION (explicit, configurable, NO free edge):
  * True win probability P per contract is calibrated empirically from
    settlement truth via a logistic fit on z-scored strike distance. This is
    a NON-DEGENERATE probability (not 0/1), and it is the model's best
    estimate of the true probability — there is NO systematic discount.
  * Entry price is drawn ~ Uniform[P - delta, P + delta] around P (symmetric,
    zero-mean, no offset), then clipped to [0.01, 0.99]. `delta` models
    execution uncertainty (spread/slippage/jitter) but is symmetric so it
    carries no directional edge.
  * If you buy YES at entry e with true P, gross EV = P - e. Net EV subtracts
    the canonical Kalshi taker fee (one side, hold-to-settlement).

FEE MODEL (canonical): `core.fees.kalshi_fee` — fee = ceil(0.07·e·(1-e)·C)¢.
  Hold-to-settlement pays ONE side. We also report a round-trip-spread-cost
  variant (spread/2 + slippage = 2.05¢ per contract, from market_cost_model)
  for the "always-bet" baseline so both are honest.

Reproducibility: fixed seed + per-contract PRNG key = sha256(contract identity)
^ seed. Same contract always draws the same value regardless of order/subsets.

Writes ONE compact JSON artifact (overwrite, no append).
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

SNAP = 'data/kalshi_market_snapshots.db'
TRUTH = 'data/z8-frozen/z8_settlement_truth.db'
OUT = 'reports/simulated_entry_window_ev.json'

TICK = re.compile(r'^KX(HIGH|LOW)(\w+)-(\d{2}[A-Z]{3}\d{2})-(T|B)(\d+(?:\.\d+)?)$')
MONTHS = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
          'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}


def datecode_to_iso(dc):
    yy = int(dc[0:2]); mon = MONTHS[dc[2:5]]; dd = int(dc[5:7])
    return f"20{yy:02d}-{mon:02d}-{dd:02d}"


def load_truth():
    c = sqlite3.connect(TRUTH)
    rows = c.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f
        FROM settlement_truth WHERE settlement_temp_f IS NOT NULL
    """).fetchall()
    c.close()
    m = {}
    for st, mt, d, t in rows:
        m[(st, mt, d)] = t
    return m


def load_contracts():
    c = sqlite3.connect(SNAP)
    rows = c.execute("""
        SELECT snapshot_time, station, market_ticker, mid_price
        FROM market_snapshots WHERE market_type='binary'
    """).fetchall()
    c.close()
    contracts = defaultdict(list)
    parse_fail = 0
    for stime, station, ticker, mid in rows:
        m = TICK.match(ticker)
        if not m:
            parse_fail += 1
            continue
        hilo, tstn, dc, tb, strike_s = m.groups()
        strike = float(strike_s)
        date_iso = datecode_to_iso(dc)
        contracts[(station, hilo, date_iso, tb, strike)].append((stime, mid))
    return contracts, parse_fail


def settle_outcome(station, hilo, date_iso, tb, strike, truth):
    key = (station, hilo, date_iso)
    temp = truth.get(key)
    if temp is None:
        return None
    if hilo == 'HIGH':
        if tb == 'T':
            return 1.0 if temp > strike else 0.0
        else:
            return 1.0 if temp <= strike else 0.0
    else:  # LOW
        if tb == 'B':
            return 1.0 if temp < strike else 0.0
        else:
            return 1.0 if temp >= strike else 0.0


def prng_key(contract_key):
    h = hashlib.sha256(repr(contract_key).encode()).hexdigest()
    return int(h[:8], 16)


def rng(seed):
    x = seed & 0xffffffff
    def next_float():
        nonlocal x
        x ^= (x << 13) & 0xffffffff
        x ^= x >> 17
        x ^= (x << 5) & 0xffffffff
        x &= 0xffffffff
        return x / 0xffffffff
    return next_float


def logistic(z):
    # stable logistic
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def calibrate_probability_model(contracts, truth):
    """
    Calibrate P(win) per contract as a NON-DEGENERATE empirical probability.

    We fit a per-side logistic: P(win) = sigmoid(a + b * z), where z is the
    z-scored strike distance from the station's typical temp. This is a real,
    defensible probability curve (not 0/1, not a flat 50%), and it carries NO
    offset — it is the model's best estimate of the true probability.

    We fit a and b by grid search maximizing log-likelihood over settled
    outcomes, per (hilo, tb) side, deterministically.
    """
    # station temp stats
    st_temps = defaultdict(list)
    for (st, mt, d), t in truth.items():
        st_temps[st].append(t)
    st_mean = {st: sum(v) / len(v) for st, v in st_temps.items()}
    st_var = {st: (sum((x - st_mean[st]) ** 2 for x in v) / len(v)) for st, v in st_temps.items()}
    st_std = {st: math.sqrt(st_var[st]) if st_var[st] > 0 else 5.0 for st in st_mean}

    # collect (side, z, outcome)
    side_data = defaultdict(list)
    for key in contracts:
        station, hilo, date_iso, tb, strike = key
        o = settle_outcome(station, hilo, date_iso, tb, strike, truth)
        if o is None:
            continue
        z = (strike - st_mean.get(station, 50.0)) / st_std.get(station, 5.0)
        side_data[(hilo, tb)].append((z, o))

    # fit logistic per side via fine deterministic grid (wide slope range so
    # the steep monotone z->winrate curve is captured, not saturated).
    A_GRID = [a / 10.0 for a in range(-40, 41, 2)]          # -4.0 .. +4.0 step 0.2
    B_GRID = [b / 10.0 for b in range(-80, 81, 2)]          # -8.0 .. +8.0 step 0.2
    models = {}
    for side, data in side_data.items():
        best = None
        best_ll = -1e18
        for a in A_GRID:
            for b in B_GRID:
                ll = 0.0
                for z, o in data:
                    p = logistic(a + b * z)
                    p = min(max(p, 1e-6), 1 - 1e-6)
                    ll += o * math.log(p) + (1 - o) * math.log(1 - p)
                if ll > best_ll:
                    best_ll = ll
                    best = (a, b)
        models[side] = best

    def p_of(station, hilo, date_iso, tb, strike):
        side = (hilo, tb)
        a, b = models.get(side, (0.0, 0.0))
        z = (strike - st_mean.get(station, 50.0)) / st_std.get(station, 5.0)
        p = logistic(a + b * z)
        return min(max(p, 0.01), 0.99)

    return p_of, models, st_mean, st_std


def kalshi_fee(contracts, price):
    """Canonical Kalshi taker fee (one side), dollars. Mirrors core/fees.py."""
    if contracts <= 0:
        return 0.0
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * contracts) / 100.0


def run_window_sim(contracts, truth, p_of, lower, upper, delta, seed, fee_mode,
                  n_draws, spread_cost):
    """
    Simulate entry prices under Dan's window discipline and compute net EV
    both WITH and WITHOUT no-bet days.

    fee_mode: 'taker' (canonical kalshi_fee one side) or 'roundtrip'
              (spread/2+slippage flat per contract).
    Returns a rich summary dict.
    """
    # Per-side accumulators
    # (hilo,tb) -> aggregates
    side = defaultdict(lambda: {
        'n_bet_draws': 0, 'n_nobet_draws': 0, 'n_bet_contracts': 0,
        'ev_with': [], 'ev_without': [], 'pnl_with': [], 'pnl_without': [],
        'ev_out_of_window': [], 'entry_list': [], 'P_list': [],
    })

    for key, snaps in contracts.items():
        station, hilo, date_iso, tb, strike = key
        outcome = settle_outcome(station, hilo, date_iso, tb, strike, truth)
        if outcome is None:
            continue
        P = p_of(station, hilo, date_iso, tb, strike)
        rngf = rng(prng_key(key) ^ seed)
        s = side[(hilo, tb)]
        for _ in range(n_draws):
            # symmetric draw around P, NO offset
            e = P - delta + 2 * delta * rngf()
            e = min(max(e, 0.01), 0.99)

            # fee for this entry
            if fee_mode == 'taker':
                fee = kalshi_fee(1, e)
            else:  # roundtrip spread cost
                fee = spread_cost

            # EV of buying YES at e with true P
            ev = P - e - fee
            pnl = (1.0 - e - fee) if outcome > 0.5 else -(e + fee)

            s['entry_list'].append(e)
            s['P_list'].append(P)

            if lower <= e <= upper:
                # BET day
                s['n_bet_draws'] += 1
                s['ev_with'].append(ev)
                s['pnl_with'].append(pnl)
            else:
                # NO-BET day: no position, no P&L
                s['n_nobet_draws'] += 1
                # "always-bet" (biased) baseline: pretend we still bet
                s['ev_without'].append(ev)
                s['pnl_without'].append(pnl)
                # record the EV we would have gotten OUTSIDE the window
                s['ev_out_of_window'].append(ev)

            # for the "without" baseline we count EVERY draw as a bet
            # but "with" only counts in-window draws
            s['n_bet_contracts'] = s['n_bet_draws']  # placeholder, recompute below

    summary = {}
    grand = {
        'n_bet_draws': 0, 'n_nobet_draws': 0,
        'ev_with_sum': 0.0, 'ev_without_sum': 0.0,
        'pnl_with_sum': 0.0, 'pnl_without_sum': 0.0,
        'ev_without_count': 0,
    }
    for (hilo, tb), s in side.items():
        n_with = s['n_bet_draws']
        n_total = n_with + s['n_nobet_draws']
        ev_with = (sum(s['ev_with']) / n_with) if n_with else 0.0
        # always-bet baseline uses ALL draws (bet + nobet)
        ev_without = (sum(s['ev_with'] + s['ev_without']) / n_total) if n_total else 0.0
        pnl_with = (sum(s['pnl_with']) / n_with) if n_with else 0.0
        pnl_without = (sum(s['pnl_with'] + s['pnl_without']) / n_total) if n_total else 0.0
        avg_entry = sum(s['entry_list']) / len(s['entry_list']) if s['entry_list'] else 0.0
        avg_P = sum(s['P_list']) / len(s['P_list']) if s['P_list'] else 0.0
        nobet_frac = (s['n_nobet_draws'] / n_total) if n_total else 0.0
        # EV the window EXCLUDED (out-of-window draws, would-have-bet baseline)
        ev_out = (sum(s['ev_out_of_window']) / len(s['ev_out_of_window'])) if s['ev_out_of_window'] else 0.0
        # Capital-utilization haircut: realized return on TOTAL deployed capital
        # = per-bet EV x bet-fraction. This is the honest 'realized return'.
        bet_frac = 1.0 - nobet_frac
        realized_ev = ev_with * bet_frac

        summary[f"{hilo}-{tb}"] = {
            'n_bet_draws': n_with,
            'n_nobet_draws': s['n_nobet_draws'],
            'n_total_draws': n_total,
            'nobet_fraction': round(nobet_frac, 4),
            'avg_P': round(avg_P, 4),
            'avg_entry': round(avg_entry, 4),
            'ev_with_nobet': round(ev_with, 6),
            'ev_alwaysbet': round(ev_without, 6),
            'ev_bias_delta': round(ev_without - ev_with, 6),
            'ev_out_of_window': round(ev_out, 6),
            'window_selects_better': round(ev_with - ev_out, 6),
            'realized_ev_on_capital': round(realized_ev, 6),
            'pnl_with_nobet': round(pnl_with, 6),
            'pnl_alwaysbet': round(pnl_without, 6),
        }
        grand['n_bet_draws'] += n_with
        grand['n_nobet_draws'] += s['n_nobet_draws']
        grand['ev_with_sum'] += sum(s['ev_with'])
        grand['ev_without_sum'] += sum(s['ev_with'] + s['ev_without'])
        grand['pnl_with_sum'] += sum(s['pnl_with'])
        grand['pnl_without_sum'] += sum(s['pnl_with'] + s['pnl_without'])
        grand['ev_without_count'] += n_total

    # grand totals
    n_with_tot = grand['n_bet_draws']
    n_total_tot = n_with_tot + grand['n_nobet_draws']
    ev_with_tot = grand['ev_with_sum'] / n_with_tot if n_with_tot else 0.0
    ev_without_tot = grand['ev_without_sum'] / n_total_tot if n_total_tot else 0.0
    pnl_with_tot = grand['pnl_with_sum'] / n_with_tot if n_with_tot else 0.0
    pnl_without_tot = grand['pnl_without_sum'] / n_total_tot if n_total_tot else 0.0
    nobet_frac_tot = grand['n_nobet_draws'] / n_total_tot if n_total_tot else 0.0
    bet_frac_tot = 1.0 - nobet_frac_tot
    realized_ev_tot = ev_with_tot * bet_frac_tot

    summary['__OVERALL__'] = {
        'n_bet_draws': n_with_tot,
        'n_nobet_draws': grand['n_nobet_draws'],
        'n_total_draws': n_total_tot,
        'nobet_fraction': round(nobet_frac_tot, 4),
        'ev_with_nobet': round(ev_with_tot, 6),
        'ev_alwaysbet': round(ev_without_tot, 6),
        'ev_bias_delta': round(ev_without_tot - ev_with_tot, 6),
        'realized_ev_on_capital': round(realized_ev_tot, 6),
        'pnl_with_nobet': round(pnl_with_tot, 6),
        'pnl_alwaysbet': round(pnl_without_tot, 6),
    }

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lower', type=float, default=0.45, help='window lower bound')
    ap.add_argument('--upper', type=float, default=0.80, help='window upper bound (hard ceiling)')
    ap.add_argument('--delta', type=float, default=0.02, help='symmetric entry jitter half-width around P (NO offset)')
    ap.add_argument('--seed', type=int, default=20260919)
    ap.add_argument('--fee-mode', choices=['taker', 'roundtrip'], default='taker',
                    help="taker = canonical kalshi_fee one side; roundtrip = spread/2+slippage flat")
    ap.add_argument('--spread-cost', type=float, default=0.0205,
                    help='flat per-contract round-trip spread cost (for fee-mode=roundtrip)')
    ap.add_argument('--n-draws', type=int, default=1, help='draws per contract')
    ap.add_argument('--out', default=OUT)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    truth = load_truth()
    contracts, parse_fail = load_contracts()
    p_of, models, st_mean, st_std = calibrate_probability_model(contracts, truth)
    n_settled = sum(1 for k in contracts
                    if settle_outcome(*k, truth) is not None)

    summary = run_window_sim(contracts, truth, p_of,
                             args.lower, args.upper, args.delta, args.seed,
                             args.fee_mode, args.n_draws, args.spread_cost)

    # verdict: does any side clear positive net EV within window WITH nobet days?
    positive_sides = []
    for side, s in summary.items():
        if side == '__OVERALL__':
            continue
        if s['ev_with_nobet'] > 0:
            positive_sides.append((side, s['ev_with_nobet']))

    # A side has a REAL tradable edge only if its realized EV on total capital
    # (with no-bet days baked in) is positive after fees.
    real_edge_sides = []
    for side, s in summary.items():
        if side == '__OVERALL__':
            continue
        if s['realized_ev_on_capital'] > 0:
            real_edge_sides.append((side, s['realized_ev_on_capital']))

    overall = summary['__OVERALL__']

    artifact = {
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'directive': 'Dan 2026-09-19: entry-WINDOW discipline re-run (drop offset, add no-bet days)',
        'method': {
            'entry_window': [args.lower, args.upper],
            'hard_ceiling': args.upper,
            'no_offset': True,
            'entry_model': f'Uniform[P-delta, P+delta], delta={args.delta}, symmetric (no discount)',
            'P_model': 'logistic on z-scored strike distance (empirical, non-degenerate, no offset)',
            'fee_model': args.fee_mode,
            'spread_cost': args.spread_cost if args.fee_mode == 'roundtrip' else None,
            'n_draws': args.n_draws,
        },
        'reproducibility': {
            'seed': args.seed,
            'prng': 'xorshift32, per-contract key = sha256(contract identity) ^ seed',
        },
        'counts': {
            'settlement_truth_rows': len(truth),
            'distinct_contracts': len(contracts),
            'settled_contracts': n_settled,
            'parse_fail': parse_fail,
        },
        'calibration': {f"{h}-{t}": {'a': a, 'b': b} for (h, t), (a, b) in models.items()},
        'results_by_side': summary,
        'verdict': {
            'positive_net_ev_sides_with_nobet': positive_sides,
            'real_edge_sides_realized_capital': real_edge_sides,
            'overall_ev_with_nobet': overall['ev_with_nobet'],
            'overall_ev_alwaysbet': overall['ev_alwaysbet'],
            'overall_ev_bias_delta': overall['ev_bias_delta'],
            'overall_realized_ev_on_capital': overall['realized_ev_on_capital'],
            'overall_nobet_fraction': overall['nobet_fraction'],
        },
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w') as f:
        json.dump(artifact, f, indent=2)

    # console report
    print(f"Settled contracts: {n_settled}, parse_fail: {parse_fail}")
    print(f"Window: [{args.lower:.2f}, {args.upper:.2f}]  delta={args.delta}  "
          f"fee={args.fee_mode}  seed={args.seed}  n_draws={args.n_draws}")
    print()
    print(f"{'side':<10} {'n_bet':>7} {'n_nobet':>8} {'nobet%':>8} {'avgP':>7} {'ev_w/nobet':>11} {'ev_always':>11} {'out_winEV':>11} {'realized':>10}")
    for side, s in sorted(summary.items()):
        if side == '__OVERALL__':
            continue
        print(f"{side:<10} {s['n_bet_draws']:>7} {s['n_nobet_draws']:>8} "
              f"{s['nobet_fraction']*100:>7.1f}% {s['avg_P']:>7.3f} "
              f"{s['ev_with_nobet']:>+11.4f} {s['ev_alwaysbet']:>+11.4f} "
              f"{s['ev_out_of_window']:>+11.4f} {s['realized_ev_on_capital']:>+10.4f}")
    o = summary['__OVERALL__']
    print(f"{'OVERALL':<10} {o['n_bet_draws']:>7} {o['n_nobet_draws']:>8} "
          f"{o['nobet_fraction']*100:>7.1f}% {'':>7} "
          f"{o['ev_with_nobet']:>+11.4f} {o['ev_alwaysbet']:>+11.4f} "
          f"{'':>11} {o['realized_ev_on_capital']:>+10.4f}")
    print()
    print(f"Positive per-bet net-EV sides (with no-bet baked in): {positive_sides if positive_sides else 'NONE'}")
    print(f"Real-edge sides (realized EV on total capital > 0): {real_edge_sides if real_edge_sides else 'NONE'}")
    print(f"Overall realized EV on capital: {overall['realized_ev_on_capital']:+.4f}")
    print(f"Artifact written: {args.out}")


if __name__ == '__main__':
    main()
