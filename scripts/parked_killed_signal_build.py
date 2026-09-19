#!/usr/bin/env python3
"""
Standalone build + test harness for PARKED/REVIVABLE-KILLED signals (2026-09-19).

Dan's directive: stop parking signals — build and test them standalone.

Correct-axis labels (strike-crossing, NOT day-over-day): built from
kalshi_market_snapshots.db T-contract tickers (strict greater/less strikes)
joined against settlement_truth date-tiered settlement temps, scored via
core/settlement_outcome.contract_is_itm.  This is the SAME axis as
scripts/killed_signal_retest_true_data_full.py (P0 direction-axis correction).

Each signal is evaluated standalone (NOT wired into the live path), emits:
  accuracy, MCC, minority-edge, fire count, axis + provenance.

READ-ONLY on data DBs.  No writes, no re-registration, no live-path wiring.
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import contract_is_itm

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
SNAP_DB = os.path.join(BASE, "data", "kalshi_market_snapshots.db")
GHCN_DB = os.path.join(BASE, "data", "ghcn_settlements.db")
REPORTS_DIR = os.path.join(BASE, "reports")

MON = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
       'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}


def ymd(d):
    return f'2026-{MON[d[2:5]]:02d}-{d[5:7]}'


def parse_ticker(t):
    m = re.match(r'^(KX[HIGHLOW]+T?)(\w+)-(\d{2}[A-Z]{3}\d{2})-([TB])([\d.]+)$', t)
    if not m:
        return None
    prefix = m.group(1)
    mtype = 'LOW' if 'LOW' in prefix else ('HIGH' if 'HIGH' in prefix else None)
    return (mtype, m.group(3), m.group(4), m.group(5))


def build_labels():
    c = sqlite3.connect(SNAP_DB); cur = c.cursor()
    cur.execute("SELECT DISTINCT station, market_ticker FROM market_snapshots")
    rows = cur.fetchall()
    c.close()

    c2 = sqlite3.connect(METAR_DB); cur2 = c2.cursor()
    cur2.execute("""SELECT station, market_type, local_trading_date, settlement_temp_f,
                           temp_source, trustworthy
                    FROM settlement_truth WHERE settlement_temp_f IS NOT NULL""")
    temps = defaultdict(dict); src = defaultdict(dict); trust = defaultdict(dict)
    for st, mt, date, temp, ts, tr in cur2.fetchall():
        temps[(st, mt, date)] = temp
        src[(st, mt, date)] = ts
        trust[(st, mt, date)] = tr
    c2.close()

    labels = defaultdict(dict)
    src_map = defaultdict(dict)
    for st, mt_t in rows:
        p = parse_ticker(mt_t)
        if not p:
            continue
        mtype, date, stype, strike = p
        if mtype is None or stype != 'T':
            continue
        d = ymd(date)
        temp = temps.get((st, mtype, d))
        if temp is None or trust.get((st, mtype, d)) == 0:
            continue
        itm = contract_is_itm(temp, float(strike), mtype)
        if itm is None:
            continue
        if mtype == 'HIGH':
            labels[st][(mtype, d)] = 'up' if itm else 'down'
        else:
            labels[st][(mtype, d)] = 'down' if itm else 'up'
        src_map[st][(mtype, d)] = src[(st, mtype, d)]
    return labels, src_map


def load_daily_series(conn, station):
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) AS high, MIN(temp_f) AS low, AVG(temp_f) AS temp,
               AVG(pressure_mb) AS pressure, AVG(wind_direction_deg) AS wind_dir,
               AVG(wind_speed_kt) AS wind_speed, AVG(dewpoint_f) AS dewpoint
        FROM metar_observations WHERE station = ? GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    return [{'date': r[0], 'high': r[1], 'low': r[2], 'temp': r[3],
             'pressure': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'dewpoint': r[7]}
            for r in cur.fetchall()]


def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def minority_edge(tp, fp, tn, fn):
    """Edge on the minority class (the rarer of up/down), imbalance-aware.

    Returns (minority_class, minority_precision, minority_accuracy_share).
    """
    up = tp + fn     # actual 'up' total
    down = tn + fp   # actual 'down' total
    if up == down == 0:
        return ('none', 0.0, 0.0)
    if up <= down:
        # minority = 'up'; precision on 'up' predictions
        pred_up = tp + fp
        prec = tp / pred_up if pred_up else 0.0
        return ('up', prec, up / (up + down))
    else:
        pred_down = tn + fn
        prec = tn / pred_down if pred_down else 0.0
        return ('down', prec, down / (up + down))


def accumulate(stats, predicted, actual):
    if predicted == 'up':
        if actual == 'up':
            stats['tp'] += 1
        else:
            stats['fp'] += 1
    elif predicted == 'down':
        if actual == 'down':
            stats['tn'] += 1
        else:
            stats['fn'] += 1


def summarize(s):
    tp, fp, tn, fn = s['tp'], s['fp'], s['tn'], s['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    m, prec, share = minority_edge(tp, fp, tn, fn)
    return {'accuracy': round(acc, 6), 'mcc': round(mcc(tp, fp, tn, fn), 6),
            'n': total, 'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
            'minority_class': m, 'minority_precision': round(prec, 4),
            'minority_share': round(share, 4)}


def main():
    t0 = time.time()
    labels, src_map = build_labels()
    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    srcc = Counter(s for v in src_map.values() for s in v.values())
    print("=" * 88)
    print("PARKED/KILLED SIGNAL — STANDALONE BUILD + TEST (correct strike-crossing axis)")
    print("=" * 88)
    print(f"Correct-axis labels: {n}  (up={lblc['up']}, down={lblc['down']})")
    print(f"temp_source: {dict(srcc)}")
    print(f"stations: {len(labels)}")
    print()

    conn = sqlite3.connect(METAR_DB)

    # ── Import target signals ──
    from core.signals.trajectory_state_vector_signal import TrajectoryStateVectorSignal
    from core.signals.rdae_mos_signal import RDAEMOSSignal
    from core.signals.ghcn_calibration_signal import GHCNCalibrationSignal
    from core.signals.sensor_bias_correction_signal import SensorBiasCorrectionSignal
    from core.signals.nwp_analog_signal import NwpAnalogSignal, NWP_ANALOG_ENABLED
    from core.signals.goldilocks_signal import GoldilocksSignal
    from core.signals.persistence_signal import PersistenceSignal

    results = {}

    def run_daily(name, signal):
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        fired = 0
        for st, lblmap in labels.items():
            days = load_daily_series(conn, st)
            if not days:
                continue
            idx_by_date = {d['date']: i for i, d in enumerate(days)}
            for (mt, date), actual in lblmap.items():
                idx = idx_by_date.get(date)
                if idx is None:
                    continue
                try:
                    direction, _ = signal.evaluate(idx, days)
                except Exception as e:
                    continue
                if direction is None:
                    continue
                fired += 1
                accumulate(orig, direction, actual)
        o = summarize(orig)
        o['fired'] = fired
        results[name] = o
        print(f"{name:<28s} acc={o['accuracy']*100:6.2f}% MCC={o['mcc']:+7.4f} "
              f"n={o['n']:5d} fired={fired:5d} "
              f"minority={o['minority_class']}(prec={o['minority_precision']:.2f},share={o['minority_share']:.2f})")

    def run_station(name, signal):
        """Evaluate a signal via evaluate_for_station(station, date) DB path."""
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        fired = 0
        for st, lblmap in labels.items():
            for (mt, date), actual in lblmap.items():
                try:
                    direction, _ = signal.evaluate_for_station(st, date)
                except Exception:
                    continue
                if direction is None:
                    continue
                fired += 1
                accumulate(orig, direction, actual)
        o = summarize(orig)
        o['fired'] = fired
        results[name] = o
        print(f"{name:<28s} acc={o['accuracy']*100:6.2f}% MCC={o['mcc']:+7.4f} "
              f"n={o['n']:5d} fired={fired:5d} "
              f"minority={o['minority_class']}(prec={o['minority_precision']:.2f},share={o['minority_share']:.2f})")

    # ── Daily in-memory signals ──
    run_daily("trajectory_state_vector", TrajectoryStateVectorSignal(METAR_DB))
    run_daily("rdae_mos", RDAEMOSSignal(METAR_DB))
    run_daily("sensor_bias_correction", SensorBiasCorrectionSignal(METAR_DB))
    run_daily("goldilocks", GoldilocksSignal(METAR_DB))
    run_daily("persistence", PersistenceSignal(METAR_DB))

    # ghcn_calibration: in-memory evaluate() is a structurally-broken placeholder
    # (baseline + recent drawn from the same window -> deviation ~0, never fires).
    # Its DOCUMENTED purpose is the DB-backed evaluate_for_station (METAR vs GHCN
    # long-term climatology).  Test that path.
    run_station("ghcn_calibration", GHCNCalibrationSignal(METAR_DB))

    # ── nwp_analog (DB-backed k-NN, contemporaneous-gated) ──
    try:
        nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
        nwp._load_nwp_features()
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        fired = 0
        for st, lblmap in labels.items():
            for (mt, date), actual in lblmap.items():
                try:
                    direction, _ = nwp.evaluate_nwp_analog(st, date)
                except Exception:
                    continue
                if direction is None:
                    continue
                fired += 1
                accumulate(orig, direction, actual)
        o = summarize(orig)
        o['fired'] = fired
        results["nwp_analog"] = o
        print(f"{'nwp_analog':<28s} acc={o['accuracy']*100:6.2f}% MCC={o['mcc']:+7.4f} "
              f"n={o['n']:5d} fired={fired:5d} "
              f"minority={o['minority_class']}(prec={o['minority_precision']:.2f},share={o['minority_share']:.2f})")
    except Exception as e:
        results["nwp_analog"] = {'error': str(e)}
        print("nwp_analog ERROR", e)

    conn.close()
    print(f"\nElapsed: {time.time() - t0:.2f}s")

    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source', 'branch': 'temp-sprint-branch',
        'axis': 'strike-crossing (T-contract greater/less) — settlement_temp vs strike, '
                'core/settlement_outcome.contract_is_itm',
        'label_universe': 'kalshi_market_snapshots.db T-contract tickers x settlement_truth temps',
        'n_labels': n, 'label_distribution': dict(lblc),
        'temp_source_distribution': dict(srcc),
        'stations': len(labels),
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': results,
        'read_only': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = os.path.join(REPORTS_DIR, 'parked_killed_signal_build_2026-09-19.json')
    with open(out, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out}")


if __name__ == "__main__":
    main()
