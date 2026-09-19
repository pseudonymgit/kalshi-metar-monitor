#!/usr/bin/env python3
"""
KILLED-SIGNAL RETEST — TRUE DATA, EXPANDED per-contract strike-crossing axis.

Uses the FULL strike ladder from kalshi_market_snapshots.db (1008 contracts,
68 station-date-type keys, 2026-09-07/08, 17 stations) joined against
settlement_truth date-tiered settlement temps. Each contract is a distinct
binary outcome (temp vs its own strike), giving a ~52/48 balanced label set
(525 down / 483 up) — far better powered than the 56-row single-strike set.

Direction semantics (correct axis):
  A signal predicts 'up'  = "temp runs hot" -> HIGH contract wins (temp>strike),
                            LOW contract loses (temp>=strike).
  A signal predicts 'down'= "temp runs cold" -> HIGH loses, LOW wins.

So a single signal direction 'up'/'down' maps to per-contract win/loss depending
on market_type. We score each signal against each contract's actual win/loss,
translated to a 'up'/'down' outcome consistent with the signal's vocabulary.

For a HIGH contract: win -> 'up', loss -> 'down'.
For a LOW  contract: win -> 'down', loss -> 'up'.

A signal's 'up' prediction is "correct" if it fires on a HIGH-win or LOW-loss
(both are 'up' outcomes). 'down' correct on HIGH-loss or LOW-win.

READ-ONLY. No re-registration, no commit. Recommendation + numbers only.
"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import contract_is_itm

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
SNAP_DB = os.path.join(BASE, "data", "kalshi_market_snapshots.db")
REPORTS_DIR = os.path.join(BASE, "reports")


def build_contract_labels():
    """Return {(station, market_type, date): {strike: 'up'|'down'}} and temps.

    'up' = contract won in the HIGH sense (temp hot). Mapping:
      HIGH: temp>strike -> 'up' (win), else 'down' (loss)
      LOW : temp<strike -> 'down' (win), else 'up' (loss)
    So 'up' uniformly means "temp hot / above-strike direction", 'down' = cold.
    """
    import re
    c = sqlite3.connect(SNAP_DB); cur = c.cursor()
    cur.execute("SELECT station, market_ticker, strike_temp, market_type "
                "FROM market_snapshots WHERE strike_temp IS NOT NULL")
    rows = cur.fetchall()
    c.close()
    pat = re.compile(r'-(\d{2}[A-Z]{3}\d{2})-(?:T|B)(\d+(?:\.\d+)?)$')
    mon = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
           'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
    def ymd(d): return f'2026-{mon[d[2:5]]:02d}-{d[5:7]}'

    strikes = defaultdict(list)
    for st, mt_t, strike, mtype in rows:
        m = pat.search(mt_t)
        if m:
            strikes[(st, ymd(m.group(1)), mtype)].append(float(strike))

    c2 = sqlite3.connect(METAR_DB); cur2 = c2.cursor()
    cur2.execute("""SELECT station, market_type, local_trading_date, settlement_temp_f
                    FROM settlement_truth WHERE settlement_temp_f IS NOT NULL""")
    temps = defaultdict(dict)
    for st, mt, date, temp in cur2.fetchall():
        temps[(st, mt, date)] = temp
    c2.close()

    labels = defaultdict(dict)   # (station,mt,date) -> {strike: 'up'/'down'}
    for (st, date, mtype), slist in strikes.items():
        temp = temps.get((st, mtype, date))
        if temp is None:
            continue
        for strike in slist:
            itm = contract_is_itm(temp, strike, mtype)
            if itm is None:
                continue
            # map to 'up'/'down' uniform direction
            if mtype == 'HIGH':
                labels[(st, mtype, date)][strike] = 'up' if itm else 'down'
            else:  # LOW
                labels[(st, mtype, date)][strike] = 'down' if itm else 'up'
    return labels, temps


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
    if n < 50:
        return "UNDERPOWERED"
    if 0.40 <= oa <= 0.60 and 0.40 <= ia <= 0.60:
        return "NOISE (coin-flip)"
    if ia > 0.55 and im > 0.05 and oa < 0.50:
        return "STABLE WRONG-WAY (inversion edge)"
    if oa > 0.55 and om > 0.05:
        return "STABLE RIGHT-WAY"
    if oa < 0.45 and ia > 0.55:
        return "STABLE WRONG-WAY (inversion edge)"
    return "INCONCLUSIVE"


def main():
    t0 = time.time()
    labels, temps = build_contract_labels()
    n = sum(len(v) for v in labels.values())
    from collections import Counter
    lblc = Counter(d for v in labels.values() for d in v.values())
    print("=" * 90)
    print("KILLED-SIGNAL RETEST — TRUE DATA (expanded per-contract strike axis)")
    print("=" * 90)
    print(f"Per-contract labels: {n}  (down={lblc['down']}, up={lblc['up']})")
    print(f"Station-date-type keys: {len(labels)}")
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
        for (st, mt, date), strike_map in labels.items():
            days = load_daily_series(conn, st)
            if not days:
                continue
            idx_by_date = {d['date']: i for i, d in enumerate(days)}
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            try:
                direction, _ = signal.evaluate(idx, days)
            except Exception:
                continue
            if direction is None:
                continue
            for strike, actual in strike_map.items():
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
        for (st, mt, date), strike_map in labels.items():
            try:
                direction, _ = nwp.evaluate_nwp_analog(st, date)
            except Exception:
                continue
            if direction is None:
                continue
            for strike, actual in strike_map.items():
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
                                    'note': 'intraday; needs transition_events surface (absent)'}
    results["goldilocks_microstructure"] = {'unavailable': True,
                                            'note': 'intraday; needs transition_events surface (absent)'}
    print("late_day_momentum: UNAVAILABLE (intraday)")
    print("goldilocks_microstructure: UNAVAILABLE (intraday)")

    conn.close()
    print(f"\nElapsed: {time.time() - t0:.2f}s")

    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source', 'branch': 'temp-sprint-branch',
        'axis': 'strike-crossing per-contract (kalshi_market_snapshots strike ladder x settlement_truth date-tiered temp)',
        'n_contract_labels': n, 'label_distribution': dict(lblc),
        'station_date_type_keys': len(labels),
        'strike_source': 'kalshi_market_snapshots.db (1008 contracts, 2026-09-07/08, 17 stations)',
        'strike_backfill_status': 'P0-B NOT LANDED — only Sep 7/8 window available; full history pending',
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': results,
        'read_only': True, 'no_mutation': True, 'no_commit': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = os.path.join(REPORTS_DIR, 'killed_signal_retest_true_data_expanded_2026-09-18.json')
    with open(out, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out}")


if __name__ == "__main__":
    main()
