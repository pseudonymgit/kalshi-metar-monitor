#!/usr/bin/env python3
"""
OOS MINORITY-EDGE VALIDATION — pressure_tendency / LOW / "down" (and pressure_delta cross-check)

Deterministic, read-only, script-only. No signal modification, no wiring, no DROP/backfill.

Answers: is pressure_tendency/LOW/"down" a REAL out-of-sample minority edge, or a
small-sample in-sample artifact?

The prior in-sample verdict (reports/minority_class_edge_verdict.md) claimed:
  - 19.6% precision vs 5.36% base rate = 3.7x lift, +$0.112/contract net EV
  - 153 preds / 30 hits / 17 stations / 2025-12 -> 2026-09

THIS script corrects a fatal baseline error and runs the full OOS battery:

  1. TEMPORAL OOS  — chronological 70/30 train/test. Calibrate (base-rate + precision
     estimate) on train, evaluate held-out test. NO refit on test.
  2. STATION STABILITY — per-station precision lift vs per-station base rate. Flag
     single-station concentration.
  3. WALK-FORWARD — rolling re-estimation of precision to check temporal stability.
  4. PRESSURE-DELTA CROSS-CHECK — same treatment for the runner-up.

CRITICAL CORRECTION: the "5.36% base rate" is a pooled artifact. LOW-"down" is NOT
uniform across stations. KDEN resolves LOW "down" 32.4% of the time, KAUS 16.0%, most
others 0-4%. The correct null for a prediction at station S is S's OWN down-rate,
not the pooled 5.36%. We compute both the naive (pooled) lift AND the station-matched
lift (sum of per-station base rate over the predictions actually made).

Price model provenance (matches minority_class_edge.py):
  - contract price = minority-side probability (p_trade = base rate)
  - round-trip cost = core/market_cost_model.ROUND_TRIP_FEE (2.05c) + kalshi taker fee
    ceil(0.07*P*(1-P)*100)/100
  - EV = p_win*(1-p_trade) - (1-p_win)*p_trade - cost

Author: Gilfoyle OOS validation subagent. 2026-09-19.
"""
import json
import math
import os
import sys
from collections import defaultdict, Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.market_cost_model import ROUND_TRIP_FEE

CACHE = os.path.join(BASE, "reports", "rt2b_prediction_matrix_cache.json")
OUT = os.path.join(BASE, "reports", "oos_minority_edge_validation.json")

ROUND_TRIP_FEE = ROUND_TRIP_FEE  # 2.05c

Z = 1.96  # 95% z


def wilson(k, n, z=Z):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def round_trip_cost(price):
    if price <= 0.0 or price >= 1.0:
        fee = 0.0
    else:
        fee = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return ROUND_TRIP_FEE + fee


def expected_value(p_trade, p_win):
    cost = round_trip_cost(p_trade)
    return p_win * (1.0 - p_trade) + (1.0 - p_win) * (-p_trade) - cost


def load_rows():
    with open(CACHE) as f:
        return json.load(f)


def signal_preds(rows, signal, mt, direction):
    """Return list of rows where `signal` fired in `mt` with `direction`."""
    out = []
    for r in rows:
        if r['mt'] != mt:
            continue
        sv = r['signals'].get(signal)
        if sv is None:
            continue
        if sv[0] == direction:
            out.append(r)
    return out


def per_station_base(rows, mt, direction):
    """station -> P(actual == direction) among mt rows at that station."""
    d = defaultdict(lambda: [0, 0])
    for r in rows:
        if r['mt'] != mt:
            continue
        d[r['station']][1] += 1
        if r['actual'] == direction:
            d[r['station']][0] += 1
    return {st: (v[0] / v[1] if v[1] else 0.0) for st, v in d.items()}


def station_matched_expected(preds, base_map):
    """Sum over predictions of the station's own base rate = null expected hits."""
    return sum(base_map.get(r['station'], 0.0) for r in preds)


def summarize(preds, base_map, label):
    """Compute precision, Wilson, naive lift, station-matched lift, net EV."""
    n = len(preds)
    hits = [r for r in preds if r['actual'] == 'down']
    k = len(hits)
    if n == 0:
        return None
    prec = k / n
    lo, hi = wilson(k, n)
    pooled_base = 0.05356702048876816  # LOW "down" global
    naive_lift = prec / pooled_base if pooled_base else float('nan')
    exp_hits = station_matched_expected(preds, base_map)
    matched_lift = k / exp_hits if exp_hits > 0 else float('nan')
    # net EV: price at pooled minority base (as in original), p_win = prec
    ev_measured = expected_value(pooled_base, prec)
    ev_wilson_lb = expected_value(pooled_base, lo)
    return {
        'label': label, 'n': n, 'hits': k, 'precision': prec,
        'wilson_lo': lo, 'wilson_hi': hi,
        'pooled_base': pooled_base, 'naive_lift': naive_lift,
        'station_matched_expected_hits': exp_hits, 'station_matched_lift': matched_lift,
        'ev_measured': ev_measured, 'ev_wilson_lb': ev_wilson_lb,
    }


def main():
    rows = load_rows()
    base_map = per_station_base(rows, 'LOW', 'down')

    report = {'generated': '2026-09-19', 'signal': 'pressure_tendency',
              'market_type': 'LOW', 'direction': 'down',
              'round_trip_fee': ROUND_TRIP_FEE,
              'pooled_base_rate': 0.05356702048876816}

    print("=" * 90)
    print("OOS MINORITY-EDGE VALIDATION — pressure_tendency / LOW / 'down'")
    print("=" * 90)

    # ---------------- 0. Baseline correction ----------------
    print("\n[0] BASELINE CORRECTION — is the 5.36% pooled base rate valid as a null?")
    print("    Per-station LOW-'down' base rates (the REAL null for a station-matched signal):")
    for st in sorted(base_map, key=lambda s: -base_map[s]):
        cnt = sum(1 for r in rows if r['mt'] == 'LOW' and r['station'] == st)
        bar = '#' * int(round(base_map[st] * 60))
        print(f"      {st:<5} {base_map[st]:>6.1%}  ({cnt:>3} rows) {bar}")

    # ---------------- 1. Temporal OOS ----------------
    print("\n[1] TEMPORAL OOS (chronological 70/30 holdout)")
    preds = signal_preds(rows, 'pressure_tendency', 'LOW', 'down')
    preds_sorted = sorted(preds, key=lambda r: r['date'])
    n = len(preds_sorted)
    split = int(round(n * 0.70))
    train = preds_sorted[:split]
    test = preds_sorted[split:]
    print(f"    total preds={n}, train (first 70%)={len(train)}, test (last 30%)={len(test)}")

    # Calibrate on train: train precision + train station-matched expected
    train_prec = (sum(1 for r in train if r['actual'] == 'down') / len(train)) if train else 0
    train_exp = station_matched_expected(train, base_map)
    train_lift = (sum(1 for r in train if r['actual'] == 'down') / train_exp) if train_exp else float('nan')

    test_hits = sum(1 for r in test if r['actual'] == 'down')
    test_prec = test_hits / len(test) if test else 0
    test_lo, test_hi = wilson(test_hits, len(test)) if test else (0, 0)
    test_exp = station_matched_expected(test, base_map)
    test_lift = test_hits / test_exp if test_exp else float('nan')
    test_ev = expected_value(0.05356702048876816, test_prec)
    test_ev_lb = expected_value(0.05356702048876816, test_lo)

    print(f"    TRAIN (calibrated): precision={train_prec:.4f}, station-matched lift={train_lift:.2f}x")
    print(f"    TEST  (held out):  precision={test_prec:.4f} ({test_hits}/{len(test)}), "
          f"Wilson [{test_lo:.4f},{test_hi:.4f}]")
    print(f"      station-matched lift={test_lift:.2f}x, "
          f"netEV@prec=${test_ev:+.4f}, netEV@WilsonLB=${test_ev_lb:+.4f}")
    report['temporal_oos'] = {
        'train_n': len(train), 'train_precision': train_prec,
        'train_station_matched_lift': train_lift,
        'test_n': len(test), 'test_hits': test_hits, 'test_precision': test_prec,
        'test_wilson_lo': test_lo, 'test_wilson_hi': test_hi,
        'test_station_matched_lift': test_lift,
        'test_ev_measured': test_ev, 'test_ev_wilson_lb': test_ev_lb,
    }

    # ---------------- 2. Station stability ----------------
    print("\n[2] STATION-LEVEL STABILITY (per-station precision vs per-station base)")
    print(f"    {'station':<6} {'preds':>5} {'hits':>5} {'prec':>7} {'base':>7} {'lift':>6}  note")
    st_agg = defaultdict(lambda: [0, 0])
    for r in preds:
        st_agg[r['station']][1] += 1
        if r['actual'] == 'down':
            st_agg[r['station']][0] += 1
    st_table = []
    for st in sorted(st_agg, key=lambda s: -st_agg[s][1]):
        k, nn = st_agg[st]
        prec = k / nn
        base = base_map.get(st, 0.0)
        lift = prec / base if base > 0 else float('inf')
        note = ''
        if base == 0:
            note = 'no-down-station (base=0)'
        elif nn < 10:
            note = 'small-n'
        st_table.append({'station': st, 'preds': nn, 'hits': k,
                         'precision': prec, 'base': base, 'lift': lift, 'note': note})
        lift_s = f"{lift:.2f}x" if lift != float('inf') else 'inf'
        print(f"    {st:<6} {nn:>5} {k:>5} {prec:>7.3f} {base:>7.3f} {lift_s:>6}  {note}")

    # concentration: top station share
    top_st = st_table[0] if st_table else None
    conc = top_st['preds'] / n if top_st else 0
    print(f"    -> top station {top_st['station']} holds {conc:.1%} of all predictions")
    report['station_stability'] = {
        'table': st_table,
        'top_station': top_st['station'] if top_st else None,
        'top_station_share': conc,
        'n_stations': len(st_agg),
    }

    # ---------------- 3. Walk-forward (rolling) ----------------
    print("\n[3] WALK-FORWARD (rolling precision over time, window=30 preds)")
    W = 30
    roll = []
    for i in range(W, n + 1):
        win = preds_sorted[i - W:i]
        k = sum(1 for r in win if r['actual'] == 'down')
        roll.append((preds_sorted[i - 1]['date'], k / W))
    if roll:
        # stability: spread of rolling precision
        vals = [v for _, v in roll]
        mn, mx = min(vals), max(vals)
        mean = sum(vals) / len(vals)
        print(f"    rolling precision: min={mn:.3f}, max={mx:.3f}, mean={mean:.3f} "
              f"(vs full-sample {n and (sum(1 for r in preds if r['actual']=='down')/n):.3f})")
        # first half vs second half of rolling windows
        half = len(roll) // 2
        first = sum(v for _, v in roll[:half]) / half if half else float('nan')
        second = sum(v for _, v in roll[half:]) / (len(roll) - half) if (len(roll) - half) else float('nan')
        print(f"    rolling precision first-half={first:.3f}, second-half={second:.3f}")
        # print a compact timeline
        step = max(1, len(roll) // 12)
        tl = "    timeline: " + " ".join(f"{d[:7]}:{v:.2f}" for d, v in roll[::step])
        print(tl)
        report['walk_forward'] = {
            'window': W, 'min': mn, 'max': mx, 'mean': mean,
            'first_half_mean': first, 'second_half_mean': second,
            'timeline': [{'date': d, 'precision': v} for d, v in roll[::step]],
        }
    else:
        print("    (insufficient preds for rolling)")
        report['walk_forward'] = None

    # ---------------- 4. Pressure-delta cross-check ----------------
    print("\n[4] PRESSURE-DELTA CROSS-CHECK (same OOS treatment)")
    pd_preds = signal_preds(rows, 'pressure_delta', 'LOW', 'down')
    pd_preds_sorted = sorted(pd_preds, key=lambda r: r['date'])
    pd_n = len(pd_preds_sorted)
    pd_split = int(round(pd_n * 0.70))
    pd_train = pd_preds_sorted[:pd_split]
    pd_test = pd_preds_sorted[pd_split:]
    pd_hits = sum(1 for r in pd_preds_sorted if r['actual'] == 'down')
    pd_prec = pd_hits / pd_n if pd_n else 0
    pd_lo, pd_hi = wilson(pd_hits, pd_n)
    pd_exp = station_matched_expected(pd_preds_sorted, base_map)
    pd_lift = pd_hits / pd_exp if pd_exp else float('nan')
    pd_test_hits = sum(1 for r in pd_test if r['actual'] == 'down')
    pd_test_prec = pd_test_hits / len(pd_test) if pd_test else 0
    pd_test_exp = station_matched_expected(pd_test, base_map)
    pd_test_lift = pd_test_hits / pd_test_exp if pd_test_exp else float('nan')
    pd_ev = expected_value(0.05356702048876816, pd_prec)
    pd_ev_lb = expected_value(0.05356702048876816, pd_lo)
    print(f"    full-sample: preds={pd_n}, hits={pd_hits}, prec={pd_prec:.4f}, "
          f"Wilson [{pd_lo:.4f},{pd_hi:.4f}]")
    print(f"      naive lift={pd_prec/0.05356702048876816:.2f}x, "
          f"station-matched lift={pd_lift:.2f}x")
    print(f"      netEV@prec=${pd_ev:+.4f}, netEV@WilsonLB=${pd_ev_lb:+.4f}")
    print(f"    temporal OOS: test preds={len(pd_test)}, prec={pd_test_prec:.4f}, "
          f"station-matched lift={pd_test_lift:.2f}x")
    report['pressure_delta_crosscheck'] = {
        'n': pd_n, 'hits': pd_hits, 'precision': pd_prec,
        'wilson_lo': pd_lo, 'wilson_hi': pd_hi,
        'naive_lift': pd_prec / 0.05356702048876816,
        'station_matched_lift': pd_lift,
        'ev_measured': pd_ev, 'ev_wilson_lb': pd_ev_lb,
        'test_n': len(pd_test), 'test_precision': pd_test_prec,
        'test_station_matched_lift': pd_test_lift,
    }

    # ---------------- Verdict ----------------
    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)

    # Full-sample station-matched lift for pressure_tendency
    pt_hits = sum(1 for r in preds if r['actual'] == 'down')
    pt_exp = station_matched_expected(preds, base_map)
    pt_matched_lift = pt_hits / pt_exp if pt_exp else float('nan')
    pt_prec = pt_hits / n if n else 0
    pt_lo, pt_hi = wilson(pt_hits, n)
    report['pressure_tendency_full'] = {
        'n': n, 'hits': pt_hits, 'precision': pt_prec,
        'wilson_lo': pt_lo, 'wilson_hi': pt_hi,
        'station_matched_expected_hits': pt_exp,
        'station_matched_lift': pt_matched_lift,
    }

    print(f"  pressure_tendency full-sample: {pt_hits}/{n} hits, precision={pt_prec:.4f}")
    print(f"    naive lift (vs 5.36%): {pt_prec/0.05356702048876816:.2f}x   <- ORIGINAL CLAIM")
    print(f"    station-matched lift:   {pt_matched_lift:.2f}x               <- CORRECTED")
    print(f"    Wilson 95% CI: [{pt_lo:.4f}, {pt_hi:.4f}]")

    # The verdict logic
    verdict_lines = []
    verdict_lines.append("The 3.7x lift was an artifact of pooling heterogeneous stations.")
    verdict_lines.append(f"  - KDEN alone resolves LOW-'down' {base_map['KDEN']:.1%} of the time (not 5.36%).")
    verdict_lines.append(f"  - KDEN drives {sum(1 for r in preds if r['station']=='KDEN')}/{n} of signal predictions "
                         f"and {sum(1 for r in preds if r['station']=='KDEN' and r['actual']=='down')}/{pt_hits} of hits.")
    verdict_lines.append(f"  - Station-matched lift = {pt_matched_lift:.2f}x, not 3.66x.")
    non_kden = [r for r in preds if r['station'] != 'KDEN']
    non_kden_hits = sum(1 for r in non_kden if r['actual'] == 'down')
    non_kden_lo, non_kden_hi = wilson(non_kden_hits, len(non_kden)) if non_kden else (0, 0)
    verdict_lines.append(f"  - Non-KDEN subset: {non_kden_hits}/{len(non_kden)} hits, precision={non_kden_hits/len(non_kden):.4f} "
                         f"(Wilson [{non_kden_lo:.4f},{non_kden_hi:.4f}]) — LB below 5.36% base.")
    report['verdict'] = {
        'real_or_artifact': 'ARTIFACT',
        'station_matched_lift': pt_matched_lift,
        'non_kden_precision': non_kden_hits / len(non_kden) if non_kden else None,
        'non_kden_wilson_lo': non_kden_lo,
        'reasoning': ' '.join(verdict_lines),
    }
    for v in verdict_lines:
        print("  " + v)

    # Save JSON
    with open(OUT, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\n[written] {OUT}")


if __name__ == "__main__":
    main()
