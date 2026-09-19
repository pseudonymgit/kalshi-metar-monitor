#!/usr/bin/env python3
"""
KILLED-SIGNAL RETEST — TRUE DATA, FULL strike-crossing axis (T-contracts).

Builds the correct-axis ground truth from ALL Kalshi market tickers in
kalshi_market_snapshots.db (T = "greater"/"less" strict strike-crossing
contracts), joined against settlement_truth date-tiered settlement temps
(GHCN authoritative -> Kalshi finalized -> WeatherAPI recency bridge).

This is the CORRECT target variable: contract win/loss = strike-crossing,
NOT day-over-day warming/cooling (which the prior INVERSION-BACKTEST used).

Yields ~3,300 strict strike-crossing labels across 44 dates (Aug 6 -> Sep 18),
vs the 56-row single-strike set and the 51k day-over-day labels the prior
inversion backtest incorrectly scored against.

For each killed signal: direction accuracy + MCC, original AND inverted.
Direction vocabulary: 'up' = "temp runs hot" (HIGH wins / LOW loses),
'down' = "temp runs cold" (HIGH loses / LOW wins).

READ-ONLY. No re-registration, no commit. Recommendation + numbers only.
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
    """Return {station: {(market_type, date): {'up'|'down'}}} for T-contracts,
    plus per-label temp_source."""
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

    labels = defaultdict(dict)   # station -> {(mt,date): 'up'/'down'}
    src_map = defaultdict(dict)
    for st, mt_t in rows:
        p = parse_ticker(mt_t)
        if not p:
            continue
        mtype, date, stype, strike = p
        if mtype is None or stype != 'T':   # strict greater/less only
            continue
        d = ymd(date)
        temp = temps.get((st, mtype, d))
        if temp is None or trust[(st, mtype, d)] == 0:
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
        SELECT date_utc, MAX(temp_c) AS high, MIN(temp_c) AS low, AVG(temp_c) AS temp,
               AVG(pressure_mb) AS pressure, AVG(wind_direction_deg) AS wind_dir,
               AVG(wind_speed_kt) AS wind_speed
        FROM metar_observations WHERE station = ? GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    return [{'date': r[0], 'high': r[1], 'low': r[2], 'temp': r[3],
             'pressure': r[4], 'wind_dir': r[5], 'wind_speed': r[6]}
            for r in cur.fetchall()]


def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


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


def invert(d):
    return {'up': 'down', 'down': 'up'}.get(d)


def summarize(s):
    tp, fp, tn, fn = s['tp'], s['fp'], s['tn'], s['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    return {'accuracy': round(acc, 6), 'mcc': round(mcc(tp, fp, tn, fn), 6),
            'n': total, 'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn}


def classify(orig, inv):
    oa, om, n = orig['accuracy'], orig['mcc'], orig['n']
    ia, im = inv['accuracy'], inv['mcc']
    if n < 100:
        return "UNDERPOWERED (n<100)"
    if 0.46 <= oa <= 0.54 and 0.46 <= ia <= 0.54:
        return "NOISE (coin-flip)"
    if ia > 0.54 and im > 0.04 and oa < 0.50:
        return "STABLE WRONG-WAY (inversion edge)"
    if oa > 0.54 and om > 0.04:
        return "STABLE RIGHT-WAY"
    if oa < 0.46 and ia > 0.54:
        return "STABLE WRONG-WAY (inversion edge)"
    return "INCONCLUSIVE"


def main():
    t0 = time.time()
    labels, src_map = build_labels()
    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    srcc = Counter(s for v in src_map.values() for s in v.values())
    print("=" * 90)
    print("KILLED-SIGNAL RETEST — TRUE DATA (full strike-crossing, T-contracts)")
    print("=" * 90)
    print(f"Correct-axis labels: {n}  (down={lblc['down']}, up={lblc['up']})")
    print(f"temp_source: {dict(srcc)}")
    print(f"stations: {len(labels)}")
    print()

    conn = sqlite3.connect(METAR_DB)

    from core.signals.pressure_delta_signal import PressureDeltaSignal
    from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal
    from core.signals.spike_reversion_signal import SpikeReversionSignal
    from core.signals.persistence_signal import PersistenceSignal
    from core.signals.simple_trend_signal import SimpleTrendSignal
    from core.signals.wind_direction_shift import WindDirectionShiftSignal
    from core.signals.nwp_analog_signal import NwpAnalogSignal, NWP_ANALOG_ENABLED

    results = {}

    def run_daily(name, signal):
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
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
                except Exception:
                    continue
                if direction is None:
                    continue
                accumulate(orig, direction, actual)
                accumulate(inv, invert(direction), actual)
        o, i = summarize(orig), summarize(inv)
        results[name] = {'original': o, 'inverted': i, 'verdict': classify(o, i)}
        print(f"{name:<26s} orig acc={o['accuracy']*100:6.2f}% MCC={o['mcc']:+7.4f} "
              f"| inv acc={i['accuracy']*100:6.2f}% MCC={i['mcc']:+7.4f} "
              f"| n={o['n']:4d} -> {results[name]['verdict']}")

    run_daily("pressure_delta (ref)", PressureDeltaSignal(METAR_DB))
    run_daily("corrected_pressure_delta", CorrectedPressureDeltaSignal(METAR_DB))
    run_daily("spike_reversion", SpikeReversionSignal(METAR_DB))
    run_daily("persistence", PersistenceSignal(METAR_DB))
    run_daily("simple_trend", SimpleTrendSignal(METAR_DB))
    run_daily("wind_direction_shift", WindDirectionShiftSignal(METAR_DB))

    # nwp_analog
    try:
        nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
        nwp._load_nwp_features()
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        for st, lblmap in labels.items():
            for (mt, date), actual in lblmap.items():
                try:
                    direction, _ = nwp.evaluate_nwp_analog(st, date)
                except Exception:
                    continue
                if direction is None:
                    continue
                accumulate(orig, direction, actual)
                accumulate(inv, invert(direction), actual)
        o, i = summarize(orig), summarize(inv)
        results["nwp_analog"] = {'original': o, 'inverted': i, 'verdict': classify(o, i)}
        print(f"{'nwp_analog':<26s} orig acc={o['accuracy']*100:6.2f}% MCC={o['mcc']:+7.4f} "
              f"| inv acc={i['accuracy']*100:6.2f}% MCC={i['mcc']:+7.4f} "
              f"| n={o['n']:4d} -> {results['nwp_analog']['verdict']}")
    except Exception as e:
        results["nwp_analog"] = {'error': str(e)}
        print("nwp_analog ERROR", e)

    results["late_day_momentum"] = {'unavailable': True,
                                    'note': 'intraday 17-22 UTC; no daily evaluate surface; '
                                            'needs transition_events label surface (absent)'}
    results["goldilocks_microstructure"] = {'unavailable': True,
                                           'note': 'real-time microstructure; needs transition_events '
                                                   'label surface (absent)'}
    print("late_day_momentum: UNAVAILABLE (intraday)")
    print("goldilocks_microstructure: UNAVAILABLE (intraday)")

    conn.close()
    print(f"\nElapsed: {time.time() - t0:.2f}s")

    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source', 'branch': 'temp-sprint-branch',
        'axis': 'strike-crossing (T-contract greater/less) — settlement_temp vs strike, '
                'core/settlement_outcome.contract_is_itm',
        'label_universe': 'kalshi_market_snapshots.db market tickers (T-contracts) x '
                          'settlement_truth date-tiered temps',
        'n_labels': n, 'label_distribution': dict(lblc),
        'temp_source_distribution': dict(srcc),
        'stations': len(labels),
        'strike_backfill_status': 'P0-B NOT LANDED — full 2021->present strike history still pending; '
                                  'this uses the 2026-08-06..09-19 market-ticker strikes already captured',
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': results,
        'read_only': True, 'no_mutation': True, 'no_commit': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = os.path.join(REPORTS_DIR, 'killed_signal_retest_true_data_full_2026-09-18.json')
    with open(out, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out}")


if __name__ == "__main__":
    main()
