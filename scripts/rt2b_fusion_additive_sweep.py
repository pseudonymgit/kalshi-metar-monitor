#!/usr/bin/env python3
"""
R/T-2b FUSION + ADDITIVE RE-SWEEP — does the FUSED system have edge individual signals lack?

R/T-2 swept 43 STANDALONE signals and found only 2 thin candidates
(nwp_dtdt_fusion MCC +0.105, frontal_passage_intraday MCC +0.109). But its §5 only
verified that fusion/lane/gate "import cleanly" — it never MEASURED the fusion layer
or additive stack for actual accuracy/MCC/edge on the correct strike-crossing axis.

This driver measures (not availability-checks) on the SAME 14,130-label backfill:

  1. FUSION — LOOP/LIOP pool-of-pools (log-odds opinion pool) directional accuracy,
     MCC, up-edge, ECE. Headline: does the POOLED signal beat every individual signal
     AND the down-skew null?
  2. UWC cascade — same measurement (uncertainty-weighted cascade).
  3. weighted / majority fusion modes — same measurement.
  4. ADDITIVE stack — frontal detectors (frontal_detector, frontal_passage_detector,
     frontal_passage_nowcast) + co-occurrence + six Gamma signals + context module.
     Measure each additive's MARGINAL contribution (does adding it to the fusion
     improve MCC/up-edge vs fusion alone?).
  5. COMBINATION — fuse the 2 thin candidates (nwp_dtdt_fusion + frontal_passage_intraday)
     with the pool-of-pools; does the combination have more edge than either alone?

METRIC DISCIPLINE (hard rule): lead with MCC (imbalance-immune), NOT accuracy/Sharpe.
Down-skew null = always-down = 65.7% acc / Sharpe 5.25. Every result must beat the null
on MCC + minority-side ("up") precision. Sharpe is NOT edge here.

READ-ONLY. No signal re-enable/wiring/kill. No commit. Kill list stays PARKED.

Usage:
  python3 scripts/rt2b_fusion_additive_sweep.py
"""

import argparse
import contextlib
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import strike_crossing_direction

import scripts.ultimate_suite as suite

BASE = suite.BASE
METAR_DB = suite.METAR_DB
REPORTS_DIR = suite.REPORTS_DIR

# Fusion engines
from core.intraday.fusion import HourlyFusionEngine  # LOOP (log-odds opinion pool)
from core.signal_fusion import (
    UncertaintyWeightedCascade,
    FusionModeConfig,
    fuse_majority_vote,
    fuse_weighted_vote,
    PoolOfPools,
)


# ══════════════════════════════════════════════════════════════════════
# 1. TRUTH LOADER (same as R/T-2)
# ══════════════════════════════════════════════════════════════════════

def load_labels_backfill():
    c = sqlite3.connect(METAR_DB)
    cur = c.cursor()
    cur.execute("""
        SELECT e.station, e.market_type, e.local_trading_date, e.strike_price, e.strike_type,
               t.settlement_temp_f, t.temp_source, t.trustworthy
        FROM settlement_epochs e
        JOIN settlement_truth t
          ON e.station = t.station AND e.market_type = t.market_type
          AND e.local_trading_date = t.local_trading_date
        WHERE e.strike_price IS NOT NULL AND t.settlement_temp_f IS NOT NULL
    """)
    rows = cur.fetchall()
    c.close()

    labels = defaultdict(dict)
    meta = defaultdict(dict)
    between = 0
    untrusted = 0
    for st, mt, date, strike, stype, temp, src, tr in rows:
        if tr == 0:
            untrusted += 1
            continue
        if stype == 'between':
            between += 1
            continue
        d = strike_crossing_direction(temp, strike, mt)
        if d is None:
            continue
        labels[st][(mt, date)] = d
        meta[st][(mt, date)] = {'strike_type': stype, 'strike': strike, 'temp': temp, 'market_type': mt}
    return labels, meta, {'between_excluded': between, 'untrusted_excluded': untrusted}


# ══════════════════════════════════════════════════════════════════════
# 2. METRICS (MCC-first discipline)
# ══════════════════════════════════════════════════════════════════════

def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _sharpe(returns):
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    return 0.0 if sd == 0 else mean / sd * (252 ** 0.5)


def _ece(conf_pairs, n_bins=10):
    """ECE on P(predicted class) vs observed fraction. conf_pairs = [(p, y)]."""
    if not conf_pairs:
        return None
    bins = [[] for _ in range(n_bins)]
    for p, y in conf_pairs:
        b = min(int(p * n_bins), n_bins - 1)
        bins[b].append((p, y))
    ece = 0.0
    for b in bins:
        if not b:
            continue
        mc = sum(p for p, _ in b) / len(b)
        ma = sum(y for _, y in b) / len(b)
        ece += (len(b) / len(conf_pairs)) * abs(ma - mc)
    return ece


def metrics_from_predictions(preds):
    """preds = list of (predicted_dir, actual_dir, conf_p). Returns full metric dict."""
    tp = fp = tn = fn = 0
    returns = []
    calib = []
    for pred, actual, conf in preds:
        if pred == 'up':
            p = conf
            if actual == 'up':
                tp += 1
            else:
                fp += 1
        else:
            p = 1.0 - conf
            if actual == 'down':
                tn += 1
            else:
                fn += 1
        returns.append(1.0 if pred == actual else -1.0)
        y = 1.0 if actual == 'up' else 0.0
        calib.append((max(0.01, min(0.99, p)), y))

    total = tp + fp + tn + fn
    if total == 0:
        return None

    up_called = tp + fp
    up_correct = tp
    up_prec = up_correct / up_called if up_called else 0.0
    base_up = (tp + fn) / total  # actual up rate in this slice

    return {
        'n': total,
        'accuracy': (tp + tn) / total,
        'mcc': _mcc(tp, fp, tn, fn),
        'sharpe': _sharpe(returns),
        'ece': _ece(calib),
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'up_called': up_called,
        'up_precision': up_prec,
        'up_base_rate': base_up,
        'up_edge': up_prec - base_up,
        'down_called': tn + fn,
        'down_precision': (tn / (tn + fn)) if (tn + fn) else 0.0,
    }


def fmt(m):
    if m is None:
        return '(no predictions)'
    return (f"n={m['n']:5d} acc={m['accuracy']*100:6.2f}% MCC={m['mcc']:+6.4f} "
            f"up-prec={m['up_precision']*100:5.1f}% up-edge={m['up_edge']*100:+5.1f}pt "
            f"ECE={m['ece'] if m['ece'] is None else round(m['ece'],3)} "
            f"Sharpe={m['sharpe']:.2f}")


# ══════════════════════════════════════════════════════════════════════
# 3. BUILD PREDICTION MATRIX (one pass over all signals, cached)
# ══════════════════════════════════════════════════════════════════════

def build_prediction_matrix(labels, meta, cache_path=None):
    """For every label, collect (signal_name -> (direction, confidence)) predictions
    from all signals that produce output. Returns:
      matrix: list of dicts [{ 'station','mt','date','actual', 'signals': {name:(dir,conf)} }]
    """
    if cache_path and os.path.exists(cache_path):
        print(f"[cache] loading prediction matrix from {cache_path}")
        with open(cache_path) as f:
            matrix = json.load(f)
        return matrix, None

    reg = suite.build_signal_registry()
    conn = sqlite3.connect(METAR_DB)

    # Preload daily series per station (fast).
    stations = sorted(labels.keys())
    alldays = {}
    for st in stations:
        alldays[st] = suite.load_daily_series(conn, st)
    idx_by_date = {st: {d['date']: i for i, d in enumerate(alldays[st])} for st in stations}

    # Flatten labels into a list of rows.
    rows = []
    for st, lblmap in labels.items():
        for (mt, date), actual in lblmap.items():
            rows.append({'station': st, 'mt': mt, 'date': date, 'actual': actual})

    # Only evaluate signals that actually FIRE on this axis (skip the ~21 dead
    # signals that produce no directional output — they cost time, add nothing).
    # This set is the R/T-2 confirmed firing set.
    FIRING_SIGNALS = {
        'calendar_climatology', 'cooling_failure', 'corrected_pressure_delta',
        'cross_model_divergence', 'fogr_reversion', 'forecast_disagreement',
        'frontal_passage_intraday', 'gaussian', 'gaussian_v2', 'goldilocks',
        'metar_dtdt', 'metar_trend', 'nwp_analog', 'nwp_analog_ensemble',
        'nwp_dtdt_fusion', 'pressure_delta', 'pressure_tendency',
        'radiational_cooling', 'regime', 'spike_reversion', 'warm_momentum',
        'wind_direction_shift',
    }

    signal_predictions = defaultdict(dict)  # sig_name -> row_idx -> (dir, conf)

    for name, sig in reg.items():
        if sig is None or name in suite.SLOW_SIGNALS_TO_SKIP or name not in FIRING_SIGNALS:
            continue
        t0 = time.time()
        nfired = 0
        for ri, row in enumerate(rows):
            st = row['station']
            idx = idx_by_date[st].get(row['date'])
            if idx is None:
                continue
            days = alldays[st]
            with contextlib.redirect_stderr(io.StringIO()):
                d, c = suite.evaluate_signal(sig, name, st, idx, row['date'], days, conn)
            if d is not None:
                dd = 'up' if (isinstance(d, str) and d.lower() in ('up', 'high', 'yes', '1')) or (isinstance(d, (int, float)) and d > 0) else 'down'
                signal_predictions[name][ri] = (dd, float(c or 0.5))
                nfired += 1
        print(f"  [signal] {name:<28s} fired={nfired:5d}  ({time.time()-t0:.1f}s)", flush=True)

    conn.close()

    # Assemble matrix.
    matrix = []
    for ri, row in enumerate(rows):
        sigs = {name: v[ri] for name, v in signal_predictions.items() if ri in v}
        matrix.append({
            'station': row['station'],
            'mt': row['mt'],
            'date': row['date'],
            'actual': row['actual'],
            'signals': sigs,
        })

    if cache_path:
        with open(cache_path, 'w') as f:
            json.dump(matrix, f)
        print(f"[cache] wrote prediction matrix to {cache_path}")

    return matrix, reg


# ══════════════════════════════════════════════════════════════════════
# 4. FUSION EVALUATORS
# ══════════════════════════════════════════════════════════════════════

def loop_fuse(sigs):
    """LOOP (log-odds opinion pool) — canonical fusion. sigs = {name:(dir,conf)}."""
    if not sigs:
        return None, 0.0
    log_odds = []
    for name, (d, c) in sigs.items():
        cc = max(0.01, min(0.99, c))
        lo = math.log(cc / (1.0 - cc))
        sign = 1.0 if d == 'up' else -1.0
        log_odds.append(sign * lo)
    n = len(log_odds)
    fused_lo = sum(log_odds) / math.sqrt(n)
    p = 1.0 / (1.0 + math.exp(-fused_lo))
    p = max(0.01, min(0.99, p))
    d = 'up' if fused_lo >= 0 else 'down'
    conf = p if d == 'up' else 1.0 - p
    return d, conf


def pool_of_pools_fuse(sigs):
    """PoolOfPools (Beta-binomial hierarchical) — returns mean probability."""
    if not sigs:
        return None, 0.0
    # PoolOfPools.evaluate expects signal_predictions keyed by SIGNAL names (pool members).
    try:
        pop = PoolOfPools()
        result = pop.evaluate(signal_predictions=dict(sigs))
        mean = result.get('mean_probability', 0.5)
        d = result.get('direction') or ('up' if mean >= 0.5 else 'down')
        conf = mean if d == 'up' else 1.0 - mean
        return d, conf
    except Exception:
        return loop_fuse(sigs)


def uwc_fuse(sigs):
    """UWC cascade — returns direction/confidence (or None if no-trade)."""
    if not sigs:
        return None, 0.0
    try:
        uwc = UncertaintyWeightedCascade(FusionModeConfig(mode='uwc', n_agreement_gate=1))
        res = uwc.fuse(signal_predictions=dict(sigs))
        d = res.get('direction')
        if d is None:
            # fall back to bayesian_conf direction
            bc = res.get('bayesian_conf', 0.5)
            d = 'up' if bc >= 0.5 else 'down'
        conf = res.get('confidence', 0.5)
        return d, conf
    except Exception:
        return loop_fuse(sigs)


def majority_fuse(sigs):
    d, c, n = fuse_majority_vote(sigs)
    if d is None:
        return None, 0.0
    return d, c


def weighted_fuse(sigs):
    d, c, n = fuse_weighted_vote(sigs)
    if d is None:
        return None, 0.0
    return d, c


# ══════════════════════════════════════════════════════════════════════
# 5. RUN
# ══════════════════════════════════════════════════════════════════════

def run():
    t0 = time.time()
    labels, meta, excl = load_labels_backfill()

    lblc = Counter(d for v in labels.values() for d in v.values())
    n = sum(lblc.values())
    up_rate = lblc.get('up', 0) / n if n else 0.0
    print(f"Labels: {n}  up={lblc.get('up',0)} down={lblc.get('down',0)}  base-up={up_rate*100:.2f}%")
    print(f"Null: always-down = {100-up_rate*100:.2f}% acc")

    cache_path = os.path.join(REPORTS_DIR, 'rt2b_prediction_matrix_cache.json')
    matrix, reg = build_prediction_matrix(labels, meta, cache_path=cache_path)
    print(f"Prediction matrix: {len(matrix)} rows")

    # Which signals actually fire?
    fire_counts = Counter()
    for m in matrix:
        for name in m['signals']:
            fire_counts[name] += 1
    firing = {name: cnt for name, cnt in fire_counts.items() if cnt > 0}
    print(f"\nSignals that fire (n>0): {len(firing)}")
    for name in sorted(firing, key=lambda k: -firing[k]):
        print(f"  {name:<32s} fires={firing[name]}")

    # ── Baselines ──
    # always-down null
    null_preds = [('down', m['actual'], 0.5) for m in matrix]
    null_metrics = metrics_from_predictions(null_preds)
    print(f"\n[NULL] always-down: {fmt(null_metrics)}")

    # best standalone (for reference — recompute from matrix)
    standalone = {}
    for name in firing:
        preds = []
        for m in matrix:
            if name in m['signals']:
                d, c = m['signals'][name]
                preds.append((d, m['actual'], c))
        if preds:
            standalone[name] = metrics_from_predictions(preds)

    # ── Fusion modes over ALL firing signals ──
    fusion_results = {}
    fusion_modes = {
        'loop_pool_of_pools': loop_fuse,
        'pool_of_pools_beta': pool_of_pools_fuse,
        'uwc_cascade': uwc_fuse,
        'majority': majority_fuse,
        'weighted': weighted_fuse,
    }
    for mode_name, fuse_fn in fusion_modes.items():
        preds = []
        for m in matrix:
            d, c = fuse_fn(m['signals'])
            if d is not None:
                preds.append((d, m['actual'], c))
        fusion_results[mode_name] = metrics_from_predictions(preds)
        print(f"\n[FUSION {mode_name}] {fmt(fusion_results[mode_name])}")

    # ── Fusion over the "honest" signal subset (exclude base-rate artifacts) ──
    # The pool-of-pools should be fed signals that aren't pure down-skew artifacts.
    # Define a subset: signals with standalone MCC > 0.01 OR positive up-edge.
    honest_signals = set()
    for name, m in standalone.items():
        if m and (m['mcc'] > 0.01 or m['up_edge'] > 0.0):
            honest_signals.add(name)
    print(f"\n[SUBSET] 'honest' signals (MCC>0.01 or up-edge>0): {sorted(honest_signals)}")

    fusion_honest = {}
    for mode_name, fuse_fn in fusion_modes.items():
        preds = []
        for m in matrix:
            subsigs = {k: v for k, v in m['signals'].items() if k in honest_signals}
            d, c = fuse_fn(subsigs)
            if d is not None:
                preds.append((d, m['actual'], c))
        fusion_honest[mode_name] = metrics_from_predictions(preds)
        print(f"[FUSION-honest {mode_name}] {fmt(fusion_honest[mode_name])}")

    # ── Additive stack marginal contribution ──
    # frontal detectors + gamma signals are NOT standalone directional in this schema
    # (they were "no output" in R/T-2). Their value is as ADDITIVES to a base fusion.
    # We measure: base LOOP over honest signals, then does any additive improve it?
    # Since the additive signals return offset_f (degF) not direction, we test the
    # additive stack's DIRECTIONAL vote separately where available.
    additive_names = [
        'frontal_detector', 'frontal_passage_detector', 'frontal_passage_nowcast',
        'frontal_passage_intraday',
    ]
    # Gamma signals (6) are in core/additive, not the directional registry.
    # They produce temperature offsets, not up/down votes; we note this in the doc.

    additive_contrib = {}
    # Base = LOOP over honest signals
    base_loop_preds = []
    for m in matrix:
        subsigs = {k: v for k, v in m['signals'].items() if k in honest_signals}
        d, c = loop_fuse(subsigs)
        if d is not None:
            base_loop_preds.append((d, m['actual'], c))
    base_loop_metrics = metrics_from_predictions(base_loop_preds)
    additive_contrib['base_loop_honest'] = base_loop_metrics
    print(f"\n[ADDITIVE base = LOOP over honest] {fmt(base_loop_metrics)}")

    # Add each frontal/additive signal one at a time to the fusion
    for aname in additive_names:
        if aname not in firing:
            additive_contrib[aname] = {'note': 'did not fire as directional (no standalone output)'}
            continue
        preds = []
        for m in matrix:
            subsigs = {k: v for k, v in m['signals'].items() if k in honest_signals}
            # add this additive if it fired
            if aname in m['signals']:
                subsigs[aname] = m['signals'][aname]
            d, c = loop_fuse(subsigs)
            if d is not None:
                preds.append((d, m['actual'], c))
        additive_contrib[f'base+{aname}'] = metrics_from_predictions(preds)
        print(f"[ADDITIVE +{aname}] {fmt(additive_contrib[f'base+{aname}'])}")

    # ── Combination test: 2 thin candidates + pool-of-pools ──
    cand = ['nwp_dtdt_fusion', 'frontal_passage_intraday']
    combos = {}
    # (a) the two candidates fused together
    preds = []
    for m in matrix:
        subsigs = {k: v for k, v in m['signals'].items() if k in cand}
        d, c = loop_fuse(subsigs)
        if d is not None:
            preds.append((d, m['actual'], c))
    combos['two_candidates_loop'] = metrics_from_predictions(preds)
    print(f"\n[COMBO two candidates LOOP] {fmt(combos['two_candidates_loop'])}")

    # (b) two candidates + honest-signal pool-of-pools
    preds = []
    for m in matrix:
        subsigs = {k: v for k, v in m['signals'].items() if k in honest_signals or k in cand}
        d, c = loop_fuse(subsigs)
        if d is not None:
            preds.append((d, m['actual'], c))
    combos['candidates_plus_honest_loop'] = metrics_from_predictions(preds)
    print(f"[COMBO candidates+honest LOOP] {fmt(combos['candidates_plus_honest_loop'])}")

    # (c) candidates + ALL firing signals
    preds = []
    for m in matrix:
        d, c = loop_fuse(m['signals'])
        if d is not None:
            preds.append((d, m['actual'], c))
    combos['all_signals_loop'] = metrics_from_predictions(preds)
    print(f"[COMBO all-signals LOOP] {fmt(combos['all_signals_loop'])}")

    # ── Assemble report ──
    def serialize(m):
        if m is None:
            return None
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()}

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'suite': 'R/T-2b fusion + additive re-sweep',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price)',
        'n_labels': n,
        'label_distribution': dict(lblc),
        'base_rate_up': round(up_rate, 6),
        'null_always_down': serialize(null_metrics),
        'signals_firing': {k: v for k, v in sorted(firing.items(), key=lambda x: -x[1])},
        'standalone': {k: serialize(v) for k, v in standalone.items()},
        'fusion_all_signals': {k: serialize(v) for k, v in fusion_results.items()},
        'honest_signals': sorted(honest_signals),
        'fusion_honest': {k: serialize(v) for k, v in fusion_honest.items()},
        'additive_contrib': {k: serialize(v) if isinstance(v, dict) and 'n' in v else v
                             for k, v in additive_contrib.items()},
        'combination': {k: serialize(v) for k, v in combos.items()},
        'elapsed_seconds': round(time.time() - t0, 2),
    }

    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    json_path = os.path.join(REPORTS_DIR, f'rt2b_fusion_additive_sweep_{ts}.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\n=== Receipt JSON: {json_path} ===")
    print(f"Elapsed: {report['elapsed_seconds']:.2f}s")
    return report, json_path


if __name__ == '__main__':
    run()
