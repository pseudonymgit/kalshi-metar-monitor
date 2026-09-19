#!/usr/bin/env python3
"""
SINGLE clean-label directional eval of revived signals (2026-09-18).

One pass — NOT a tuning loop. Evaluates the revived signals against the CLEAN
settlement labels, head-to-head vs pressure_delta as reference.

Clean labels: settlement_epochs rows where epoch_status='closed' AND both
settlement_bucket and prior_settlement_bucket are within the valid range
[-22, 119]. This excludes the 28 `unresolved` rows and the 18 "linkage" rows
(the day immediately after each unresolved row). Direction = 'up' if today's
bucket > prior, 'down' if less; flat rows skipped.

Signals scored (daily directional, HIGH market):
  - corrected_pressure_delta   (CorrectedPressureDeltaSignal)
  - nwp_analog                 (NwpAnalogSignal, k-NN)
  - pressure_delta             (PressureDeltaSignal — reference)

The goldilocks *daily* signal stays killed (not evaluated). The separated
real-time microstructure_spike_reversion edge is an intraday alert (no daily
surface); reported separately, NOT scored against daily settlement labels.

Read-only on data/metar_backfill.db and data/nwp_forecasts.db. No writes.

Efficiency note: per-station daily pressure/temperature series are loaded ONCE
and the signals are driven via their in-memory evaluate(idx, days) path, so the
2.4GB metar_backfill.db is scanned a bounded number of times (once per station),
not once per (station, date).
"""

import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal
from core.signals.pressure_delta_signal import PressureDeltaSignal
from core.signals.nwp_analog_signal import NwpAnalogSignal, NWP_ANALOG_ENABLED

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")

LO, HI = -22, 119


def load_clean_ground_truth(conn):
    """Return {station: {date: 'up'|'down'}} for clean closed HIGH epochs."""
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


def load_daily_pressure(conn, station):
    """Return ordered list of {date, pressure} dicts for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, AVG(pressure_mb)
        FROM metar_observations
        WHERE station=? AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    return [{'date': r[0], 'pressure': r[1]} for r in cur.fetchall() if r[1] is not None]


def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


def evaluate_daily_signal(signal, truth, stations, conn):
    """Evaluate a daily signal via its in-memory evaluate(idx, days) path.

    Returns per-signal confusion aggregates.
    """
    stats = defaultdict(lambda: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})
    for st in stations:
        days = load_daily_pressure(conn, st)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for date, actual in truth[st].items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            direction, conf = signal.evaluate(idx, days)
            if direction is None:
                continue
            r = stats[st]
            if direction == 'up':
                if actual == 'up':
                    r['tp'] += 1
                else:
                    r['fp'] += 1
            else:
                if actual == 'down':
                    r['tn'] += 1
                else:
                    r['fn'] += 1
    return stats


def evaluate_nwp_analog(signal, truth, stations):
    """Evaluate nwp_analog via evaluate_nwp_analog(station, date) with caching."""
    stats = defaultdict(lambda: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})
    # Force NWP feature load once up front.
    signal._load_nwp_features()
    for st in stations:
        for date, actual in truth[st].items():
            direction, conf = signal.evaluate_nwp_analog(st, date)
            if direction is None:
                continue
            r = stats[st]
            if direction == 'up':
                if actual == 'up':
                    r['tp'] += 1
                else:
                    r['fp'] += 1
            else:
                if actual == 'down':
                    r['tn'] += 1
                else:
                    r['fn'] += 1
    return stats


def report(label, stats):
    tp = sum(r['tp'] for r in stats.values())
    fp = sum(r['fp'] for r in stats.values())
    tn = sum(r['tn'] for r in stats.values())
    fn = sum(r['fn'] for r in stats.values())
    total = tp + fp + tn + fn
    correct = tp + tn
    acc = correct / total if total else 0.0
    m = mcc(tp, fp, tn, fn)
    fired = sum(1 for r in stats.values() if (r['tp'] + r['fp'] + r['tn'] + r['fn']) > 0)
    print(f"{label:<28s}  acc={acc*100:6.2f}%  MCC={m:+.4f}  "
          f"n={total}  fired_stations={fired}")
    return {'label': label, 'accuracy': acc, 'mcc': m, 'total': total,
            'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'fired': fired}


def main():
    conn = sqlite3.connect(METAR_DB)
    try:
        truth = load_clean_ground_truth(conn)
    finally:
        pass  # keep conn open for eval

    stations = sorted(truth.keys())
    total_epochs = sum(len(v) for v in truth.values())

    print("=" * 72)
    print("CLEAN-LABEL DIRECTIONAL EVAL — revived signals (single pass)")
    print("=" * 72)
    print(f"Clean HIGH epochs (closed, buckets in [{LO},{HI}]): {total_epochs}")
    print(f"Stations: {len(stations)}")
    print(f"NWP_ANALOG_ENABLED: {NWP_ANALOG_ENABLED}")
    print()

    results = []

    # Reference + corrected pressure delta (in-memory daily path)
    ps = PressureDeltaSignal(METAR_DB)
    stats = evaluate_daily_signal(ps, truth, stations, conn)
    results.append(report('pressure_delta (reference)', stats))

    cps = CorrectedPressureDeltaSignal(METAR_DB)
    stats = evaluate_daily_signal(cps, truth, stations, conn)
    results.append(report('corrected_pressure_delta', stats))

    # nwp_analog (k-NN)
    nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
    stats = evaluate_nwp_analog(nwp, truth, stations)
    results.append(report('nwp_analog', stats))

    conn.close()

    print()
    print("-" * 72)
    print("Confusion (up=positive):")
    for r in results:
        print(f"  {r['label']:<28s}  TP={r['tp']:6d} FP={r['fp']:6d} "
              f"TN={r['tn']:6d} FN={r['fn']:6d}")
    print()

    print("Goldilocks microstructure edge (microstructure_spike_reversion):")
    print("  NOT scored against daily settlement labels in this pass.")
    print("  Intraday real-time alert (settlement_up -> fast reversion), wired")
    print("  via core/alert_schema.py (REVERSAL) + core/position_sizing.py")
    print("  (0.80 weight) + core/data_processor.py emission pipeline. Its")
    print("  directional accuracy must be measured on its own intraday label")
    print("  surface (transition_history), not settlement_epochs HIGH buckets.")
    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
