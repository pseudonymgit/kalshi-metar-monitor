#!/usr/bin/env python3
"""
R/T-2 FULL-HISTORY SIGNAL RE-SWEEP — honest edge vs base-rate-artifact audit.

Runs the full signal sweep (all 43 active + killed + recovered) against the
COMPLETE strike backfill (settlement_epochs.strike_price/strike_type joined to
settlement_truth.settlement_temp_f date-tiered temps), NOT the 1,537-label subset.

The whole point of R/T-2 is to distinguish REAL EDGE from BASE-RATE ARTIFACT.
The correct-axis label set is NOT uniformly up-skewed: HIGH ('greater') markets
resolve 'up' only ~10% of the time (strike set above typical temp), while LOW
('less') markets resolve 'up' ~95% of the time. So the honest null is the
MARKET-TYPE-CONDITIONAL base rate, and any signal must beat that null on MCC +
Sharpe, not raw accuracy.

READ-ONLY. No signal re-enable/wiring. No commit of signal changes. Dan holds wire-in.

Usage:
  python3 scripts/rt2_full_sweep.py                 # full sweep + QC, writes reports/docs
  python3 scripts/rt2_full_sweep.py --dry-run       # enumerate only
"""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import strike_crossing_direction, UP, DOWN

# Reuse the canonical suite for signal enumeration + evaluation routing.
import scripts.ultimate_suite as suite

BASE = suite.BASE
METAR_DB = suite.METAR_DB
REPORTS_DIR = suite.REPORTS_DIR


# ══════════════════════════════════════════════════════════════════════
# 1. TRUTH LOADER — full strike backfill (settlement_epochs + settlement_truth)
# ══════════════════════════════════════════════════════════════════════

def load_labels_backfill():
    """Load correct-axis labels from the FULL 14,265-row strike backfill.

    Join settlement_epochs (strike_price/strike_type, authoritative from Kalshi
    /historical/markets) against settlement_truth (settlement_temp_f, date-tiered
    by (station, market_type, local_trading_date)). Excludes 'between' bins (54)
    which have no single-strike greater/less semantics.
    """
    c = sqlite3.connect(METAR_DB)
    cur = c.cursor()
    cur.execute("""
        SELECT e.station, e.market_type, e.local_trading_date, e.strike_price, e.strike_type,
               t.settlement_temp_f, t.temp_source, t.trustworthy
        FROM settlement_epochs e
        JOIN settlement_truth t
          ON e.station = t.station
         AND e.market_type = t.market_type
         AND e.local_trading_date = t.local_trading_date
        WHERE e.strike_price IS NOT NULL
          AND t.settlement_temp_f IS NOT NULL
    """)
    rows = cur.fetchall()
    c.close()

    labels = defaultdict(dict)   # station -> {(mt,date): 'up'/'down'}
    src_map = defaultdict(dict)
    meta = defaultdict(dict)     # station -> {(mt,date): {'strike_type','strike','temp'}}
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
        src_map[st][(mt, date)] = src
        meta[st][(mt, date)] = {'strike_type': stype, 'strike': strike, 'temp': temp, 'market_type': mt}

    return labels, src_map, meta, {'between_excluded': between, 'untrusted_excluded': untrusted}


# ══════════════════════════════════════════════════════════════════════
# 2. METRICS — with base-rate correction (the honest skill bar)
# ══════════════════════════════════════════════════════════════════════

def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _sharpe(returns, rf=0.0):
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    return (mean - rf) / sd * (252 ** 0.5)


def _ece_by_market_type(results, n_bins=10):
    """ECE computed WITHIN each market type then averaged (correct for the
    market-type-conditional base rate; a global ECE is misleading when the label
    base rate differs wildly by market type)."""
    by_mt = defaultdict(list)
    for pred, actual, conf, mt in results:
        by_mt[mt].append((pred, actual, conf))
    eces = []
    for mt, rlist in by_mt.items():
        bins = [[] for _ in range(n_bins)]
        for pred, actual, conf in rlist:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            b = min(int(p * n_bins), n_bins - 1)
            bins[b].append((p, y))
        ece = 0.0
        for b in bins:
            if not b:
                continue
            mean_conf = sum(p for p, _ in b) / len(b)
            mean_acc = sum(y for _, y in b) / len(b)
            ece += (len(b) / len(rlist)) * abs(mean_acc - mean_conf)
        eces.append((mt, ece, len(rlist)))
    if not eces:
        return 1.0
    return sum(e for _, e, _ in eces) / len(eces)


def evaluate_signal_metrics(sig, name, labels, meta, conn):
    """Score one signal on the correct axis, producing base-rate-corrected metrics."""
    stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    returns = []
    calib = []  # (pred, actual, conf, market_type)
    per_mt = defaultdict(lambda: {'tp':0,'fp':0,'tn':0,'fn':0})
    for st, lblmap in labels.items():
        days = suite.load_daily_series(conn, st)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for (mt, date), actual in lblmap.items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            direction, confidence = suite.evaluate_signal(sig, name, st, idx, date, days, conn)
            if direction is None:
                continue
            if direction == 'up':
                if actual == 'up':
                    stats['tp'] += 1; per_mt[mt]['tp'] += 1
                else:
                    stats['fp'] += 1; per_mt[mt]['fp'] += 1
            else:
                if actual == 'down':
                    stats['tn'] += 1; per_mt[mt]['tn'] += 1
                else:
                    stats['fn'] += 1; per_mt[mt]['fn'] += 1
            returns.append(1.0 if direction == actual else -1.0)
            calib.append((direction, actual, float(confidence or 0.5), mt))

    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0

    # Base rate: majority-class accuracy per market type (the honest null).
    # For a signal that makes a uniform prediction per market type, the null
    # accuracy is the per-market-type majority fraction, blended by the actual
    # prediction counts (NOT the label counts) so it's an apples-to-apples null.
    null_correct = 0
    for mt in per_mt:
        m = per_mt[mt]
        n_mt = m['tp'] + m['fp'] + m['tn'] + m['fn']
        up_n = m['tp'] + m['fn']   # actual 'up' count in this market type
        down_n = m['fp'] + m['tn'] # actual 'down' count
        # null = predict the majority class of THIS market type every time
        null_correct += max(up_n, down_n)
    null_acc = null_correct / total if total else 0.0

    out = {
        'accuracy': round(acc, 6),
        'mcc': round(_mcc(tp, fp, tn, fn), 6),
        'n': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'base_rate_accuracy': round(null_acc, 6),          # market-type-conditional null
        'base_rate_delta': round(acc - null_acc, 6),       # acc above null
        'ece': round(_ece_by_market_type(calib), 6) if calib else None,
        'sharpe': round(_sharpe(returns), 6) if returns else None,
        'n_returns': len(returns),
        'up_ratio_predicted': round((tp + fp) / total, 4) if total else None,
        'per_market_type': {
            mt: {
                'n': m['tp']+m['fp']+m['tn']+m['fn'],
                'acc': round((m['tp']+m['tn'])/(m['tp']+m['fp']+m['tn']+m['fn']), 4) if (m['tp']+m['fp']+m['tn']+m['fn']) else 0.0,
                'actual_up_rate': round((m['tp']+m['fn'])/(m['tp']+m['fp']+m['tn']+m['fn']), 4) if (m['tp']+m['fp']+m['tn']+m['fn']) else 0.0,
            }
            for mt, m in sorted(per_mt.items())
        },
    }
    return out


# ══════════════════════════════════════════════════════════════════════
# 3. SWEEP DRIVERS
# ══════════════════════════════════════════════════════════════════════

def sweep_signals_rt2(labels, meta, dry_run=False):
    registry = suite.build_signal_registry()
    results = {}
    if dry_run:
        return {
            'enumerated': len(registry),
            'present': sorted(n for n, s in registry.items() if s is not None),
            'missing_import': sorted(n for n, s in registry.items() if s is None),
        }
    conn = sqlite3.connect(METAR_DB)
    for name, sig in registry.items():
        if sig is None:
            results[name] = {'error': 'import_failed'}
            continue
        if name in suite.SLOW_SIGNALS_TO_SKIP:
            results[name] = {'skipped': 'slow_or_live_api'}
            continue
        # Suppress per-signal stderr noise (deprecation warnings, schema-mismatch
        # errors from signals that hit the wrong column). These yield empty results
        # naturally, which is itself a finding.
        with contextlib.redirect_stderr(io.StringIO()):
            results[name] = evaluate_signal_metrics(sig, name, labels, meta, conn)
    conn.close()
    return results


def run(full=True):
    t0 = time.time()
    labels, src_map, meta, excl = load_labels_backfill()

    # label distribution + base rate
    lblc = Counter(d for v in labels.values() for d in v.values())
    n = sum(lblc.values())
    up_rate = lblc.get('up', 0) / n if n else 0.0
    # per market type base rate
    mt_up = Counter(); mt_tot = Counter()
    for st, lblmap in labels.items():
        for (mt, date), d in lblmap.items():
            mt_tot[mt] += 1
            if d == 'up':
                mt_up[mt] += 1

    print("=" * 90)
    print("R/T-2 FULL-HISTORY SIGNAL RE-SWEEP — honest edge vs base-rate artifact")
    print("=" * 90)
    print(f"Truth source : settlement_epochs.strike_price (full backfill)")
    print(f"Labels       : {n}  (up={lblc.get('up',0)}, down={lblc.get('down',0)})")
    print(f"Base rate UP : {up_rate*100:.2f}%  (null 'always-down' wins {100-up_rate*100:.2f}%)")
    for mt in sorted(mt_tot):
        r = mt_up[mt] / mt_tot[mt] if mt_tot[mt] else 0.0
        print(f"  {mt:<5s} base-rate up = {r*100:.1f}%  (n={mt_tot[mt]})")
    print(f"Excluded     : between={excl['between_excluded']} untrusted={excl['untrusted_excluded']}")
    print(f"Stations     : {len(labels)}")
    print()

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'suite': 'R/T-2 full-history re-sweep',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — settlement_epochs joined settlement_truth',
        'truth_source': 'settlement_epochs.strike_price (full backfill)',
        'n_labels': n,
        'label_distribution': dict(lblc),
        'base_rate_up': round(up_rate, 6),
        'base_rate_by_market_type': {mt: round(mt_up[mt]/mt_tot[mt], 6) for mt in mt_tot},
        'stations': len(labels),
        'excluded': excl,
        'temp_source_distribution': dict(Counter(s for v in src_map.values() for s in v.values())),
        'results': {},
    }

    print("[SIGNAL SWEEP — all 43 signals on full-history correct axis]")
    report['results']['signal'] = sweep_signals_rt2(labels, meta)
    for name, m in sorted(report['results']['signal'].items()):
        if isinstance(m, dict) and 'mcc' in m:
            print(f"  {name:<30s} acc={m['accuracy']*100:6.2f}% baseΔ={m['base_rate_delta']*100:+6.2f} "
                  f"MCC={m['mcc']:+6.4f} ECE={m['ece'] if m['ece'] is None else round(m['ece'],3)} "
                  f"Sharpe={m['sharpe'] if m['sharpe'] is None else round(m['sharpe'],2)} n={m['n']:5d}")
        else:
            print(f"  {name:<30s} {m}")

    # lane/gate/fusion/calibration-mode availability sweep
    print("\n[LANE / GATE / FUSION / CALIBRATION-MODE SWEEP]")
    report['results']['lane_gate'] = suite.sweep_lanes_gates(dry_run=False)
    for group, items in report['results']['lane_gate'].items():
        print(f"  {group}:")
        for name, d in items.items():
            print(f"    - {name:<26s} {d['status']}")

    # calibration sweep (Brier/ECE per signal)
    print("\n[CALIBRATION SWEEP — per-signal Brier + ECE]")
    # reuse suite sweep_calibration but with backfill labels
    report['results']['calibration'] = _sweep_calibration_rt2(labels)
    for name, m in sorted(report['results']['calibration'].items()):
        if isinstance(m, dict):
            print(f"  {name:<30s} Brier={m['brier']:.4f} ECE={m['ece']:.4f} n={m['n']}")

    report['elapsed_seconds'] = round(time.time() - t0, 2)

    # emit artifacts
    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    json_path = os.path.join(REPORTS_DIR, f'rt2_full_sweep_{ts}.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReceipt JSON : {json_path}")
    print(f"Elapsed      : {report['elapsed_seconds']:.2f}s")
    return report, json_path


def _sweep_calibration_rt2(labels):
    """Per-signal Brier + ECE on the backfill axis (reuses suite's inline impl)."""
    registry = suite.build_signal_registry()
    conn = sqlite3.connect(METAR_DB)
    out = {}
    for name, sig in registry.items():
        if sig is None or name in suite.SLOW_SIGNALS_TO_SKIP:
            continue
        results = []
        with contextlib.redirect_stderr(io.StringIO()):
            for st, lblmap in labels.items():
                days = suite.load_daily_series(conn, st)
                if not days:
                    continue
                idx_by_date = {d['date']: i for i, d in enumerate(days)}
                for (mt, date), actual in lblmap.items():
                    idx = idx_by_date.get(date)
                    if idx is None:
                        continue
                    direction, confidence = suite.evaluate_signal(sig, name, st, idx, date, days, conn)
                    if direction is None:
                        continue
                    results.append((direction, actual, float(confidence or 0.5)))
        if not results:
            continue
        # Brier
        brier_vals = []
        for pred, actual, conf in results:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            brier_vals.append((p - y) ** 2)
        brier = sum(brier_vals) / len(brier_vals) if brier_vals else 1.0
        # ECE
        n_bins = 10
        bins = [[] for _ in range(n_bins)]
        for pred, actual, conf in results:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            b = min(int(p * n_bins), n_bins - 1)
            bins[b].append((p, y))
        ece = 0.0
        for b in bins:
            if not b:
                continue
            mean_conf = sum(p for p, _ in b) / len(b)
            mean_acc = sum(y for _, y in b) / len(b)
            ece += (len(b) / len(results)) * abs(mean_acc - mean_conf)
        out[name] = {'brier': round(brier, 4), 'ece': round(ece, 4), 'n': len(results)}
    conn.close()
    return out


if __name__ == '__main__':
    run()
