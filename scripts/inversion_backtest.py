#!/usr/bin/env python3
"""
INVERSION BACKTEST — directional, against CLEAN settlement_epochs labels.

Dan's inversion ask (2026-09-18): for the previously-killed signals, test whether
their "wrongness" against clean labels is STABLE (a genuine inversion edge — the
signal is *reliably* wrong in the same direction, so flipping it yields a real
signal) versus NOISE (47–52% = coin-flip; the wrongness is not persistent).

DELIVERABLE 1 — Directional inversion backtest for:
    - nwp_analog        (prior 32.63% kill figure)
    - spike_reversion   (prior 44.6% kill figure)
    - corrected_pressure_delta  (prior MCC −0.217; sweep_engine class-mapping
      bug FIXED in working tree: line 337 now maps → CorrectedPressureDeltaSignal,
      and .name now returns "corrected_pressure_delta" instead of the old
      "pressure_delta_phy_corrected").
    - pressure_delta    (reference — the signal corrected_pressure_delta inverts)

For EACH signal we compute BOTH original-direction accuracy/MCC AND
inverted-direction accuracy/MCC, then classify:
    - STABLE WRONG-WAY (inversion candidate): inverted accuracy well above 50%
      AND inverted MCC clearly positive; original well below 50% / negative MCC.
    - NOISE (coin-flip): original accuracy in ~47–52% band; inversion adds nothing.
    - (a signal can also be STABLE RIGHT-WAY if original is clearly >50%.)

ANTI-LOOP DESIGN (same as clean_label_signal_eval.py):
    - Per-station daily series loaded ONCE into memory; signals driven via
      evaluate(idx, days) — never evaluate_for_station() per call.
    - nwp_analog driven via evaluate_nwp_analog(station, date) with feature table
      cached inside the signal object.

READ-ONLY. No signal re-enabled/re-registered. No merge. No commit. No
paper/live trading. Recommendation + numbers only.

Output: JSON receipt under reports/ + console table.
"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal
from core.signals.pressure_delta_signal import PressureDeltaSignal
from core.signals.nwp_analog_signal import NwpAnalogSignal, NWP_ANALOG_ENABLED
from core.signals.spike_reversion_signal import SpikeReversionSignal

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")

LO, HI = -22, 119  # clean settlement_bucket / prior_settlement_bucket range


def load_clean_ground_truth(conn):
    """{station: {date: 'up'|'down'}} for clean closed HIGH epochs."""
    cur = conn.cursor()
    cur.execute("""
        SELECT station, local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE market_type = 'HIGH'
          AND epoch_status = 'closed'
          AND settlement_bucket BETWEEN ? AND ?
          AND prior_settlement_bucket BETWEEN ? AND ?
        ORDER BY station, local_trading_date
    """, (LO, HI, LO, HI))
    truth = defaultdict(dict)
    for station, date, b, p in cur.fetchall():
        if b > p:
            truth[station][date] = 'up'
        elif b < p:
            truth[station][date] = 'down'
    return truth


def load_daily_series(conn, station, field='pressure'):
    """Ordered list of {date, <field>} for one station (single GROUP BY)."""
    cur = conn.cursor()
    if field == 'pressure':
        cur.execute("""
            SELECT date_utc, AVG(pressure_mb)
            FROM metar_observations
            WHERE station = ? AND pressure_mb IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc ASC
        """, (station,))
    elif field == 'high':
        cur.execute("""
            SELECT date_utc, MAX(temp_c)
            FROM metar_observations
            WHERE station = ? AND temp_c IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc ASC
        """, (station,))
    else:
        raise ValueError(f"unknown field {field}")
    return [{'date': r[0], field: r[1]}
            for r in cur.fetchall() if r[1] is not None]


def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


def _accumulate(stats, direction, actual):
    if direction == 'up':
        if actual == 'up':
            stats['tp'] += 1
        else:
            stats['fp'] += 1
    else:  # 'down'
        if actual == 'down':
            stats['tn'] += 1
        else:
            stats['fn'] += 1


def _invert(direction):
    if direction == 'up':
        return 'down'
    if direction == 'down':
        return 'up'
    return None


def evaluate_daily(signal, truth, conn, field='pressure'):
    """Drive daily signal via evaluate(idx, days); returns original + inverted stats."""
    orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    for st in truth:
        days = load_daily_series(conn, st, field)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for date, actual in truth[st].items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            direction, _ = signal.evaluate(idx, days)
            if direction is None:
                continue
            _accumulate(orig, direction, actual)
            _accumulate(inv, _invert(direction), actual)
    return orig, inv


def evaluate_nwp(signal, truth):
    orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    signal._load_nwp_features()
    for st in truth:
        for date, actual in truth[st].items():
            direction, _ = signal.evaluate_nwp_analog(st, date)
            if direction is None:
                continue
            _accumulate(orig, direction, actual)
            _accumulate(inv, _invert(direction), actual)
    return orig, inv


def summarize_stats(stats):
    tp = stats['tp']; fp = stats['fp']; tn = stats['tn']; fn = stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    m = mcc(tp, fp, tn, fn)
    return {
        'accuracy': round(acc, 6),
        'mcc': round(m, 6),
        'total': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
    }


def classify(orig_acc, orig_mcc, inv_acc, inv_mcc):
    """Classify STABLE-WRONG (inversion candidate) vs NOISE vs RIGHT-WAY."""
    # Coin-flip band around 50%: 47–53% is noise for a directional signal.
    NOISE_LO, NOISE_HI = 0.47, 0.53
    if NOISE_LO <= orig_acc <= NOISE_HI and NOISE_LO <= inv_acc <= NOISE_HI:
        return "NOISE (coin-flip — wrongness is not persistent)"
    if inv_acc > 0.53 and inv_mcc > 0.0 and orig_acc < 0.50:
        return "STABLE WRONG-WAY (inversion candidate — flip yields a real edge)"
    if orig_acc > 0.53 and orig_mcc > 0.0:
        return "STABLE RIGHT-WAY (already correct; no inversion needed)"
    if orig_acc < 0.47 and inv_acc > 0.53:
        return "STABLE WRONG-WAY (inversion candidate)"
    # Catch-all: mixed / weak
    return "INCONCLUSIVE (weak or mixed — does not clear either threshold)"


def main():
    t0 = time.time()
    conn = sqlite3.connect(METAR_DB)
    truth = load_clean_ground_truth(conn)
    total_epochs = sum(len(v) for v in truth.values())
    n_stations = len(truth)

    print("=" * 80)
    print("INVERSION BACKTEST — directional, clean settlement_epochs labels")
    print("=" * 80)
    print(f"Clean HIGH epochs: {total_epochs} across {n_stations} stations")
    print(f"NWP_ANALOG_ENABLED (module flag): {NWP_ANALOG_ENABLED}")
    print()

    results = []

    # (1) corrected_pressure_delta — physics-corrected inversion of pressure_delta
    cps = CorrectedPressureDeltaSignal(METAR_DB)
    o, i = evaluate_daily(cps, truth, conn, field='pressure')
    results.append(('corrected_pressure_delta', o, i))

    # (2) spike_reversion — daily SpikeReversionSignal (formerly goldilocks)
    srs = SpikeReversionSignal(METAR_DB)
    o, i = evaluate_daily(srs, truth, conn, field='high')
    results.append(('spike_reversion', o, i))

    # (3) nwp_analog — deterministic k-NN analog
    nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
    o, i = evaluate_nwp(nwp, truth)
    results.append(('nwp_analog', o, i))

    # (4) pressure_delta — reference (corrected_pressure_delta is its inversion)
    ps = PressureDeltaSignal(METAR_DB)
    o, i = evaluate_daily(ps, truth, conn, field='pressure')
    results.append(('pressure_delta (reference)', o, i))

    conn.close()

    # ── Print ──
    print(f"{'signal':<28s} {'dir':<8s} {'accuracy':>10s} {'MCC':>9s} {'n':>7s}  verdict")
    print("-" * 100)
    out = {}
    for label, o, i in results:
        os_ = summarize_stats(o)
        is_ = summarize_stats(i)
        verdict = classify(os_['accuracy'], os_['mcc'], is_['accuracy'], is_['mcc'])
        print(f"{label:<28s} {'orig':<8s} {os_['accuracy']*100:>9.2f}% "
              f"{os_['mcc']:>+9.4f} {os_['total']:>7d}")
        print(f"{'':<28s} {'invert':<8s} {is_['accuracy']*100:>9.2f}% "
              f"{is_['mcc']:>+9.4f} {is_['total']:>7d}  → {verdict}")
        print()
        out[label] = {
            'original': os_,
            'inverted': is_,
            'verdict': verdict,
        }

    print(f"Elapsed: {time.time() - t0:.2f}s (single pass, no tuning)")

    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'clean_label_filter': {
            'market_type': 'HIGH', 'epoch_status': 'closed',
            'settlement_bucket_range': [LO, HI],
            'prior_settlement_bucket_range': [LO, HI],
            'note': 'range filter on both buckets excludes unresolved + linkage rows',
        },
        'total_clean_epochs': total_epochs,
        'stations': n_stations,
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': out,
        'thresholds': {
            'noise_band': [0.47, 0.53],
            'stable_wrong_way': 'inverted acc > 53% AND inv MCC > 0 AND orig acc < 50%',
            'stable_right_way': 'orig acc > 53% AND orig MCC > 0',
        },
        'read_only': True, 'no_mutation': True, 'no_commit': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, 'inversion_backtest_2026-09-18.json')
    with open(out_path, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out_path}")


if __name__ == "__main__":
    main()
