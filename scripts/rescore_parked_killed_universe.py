#!/usr/bin/env python3
"""
RE-SCORE parked/killed signals on the CORRECT label universe (2026-09-19).

Deterministic, read-only, script-only. No signal modification, no wiring, no
DROP/backfill.

PROBLEM: reports/parked_killed_signal_build_2026-09-19.json scored the parked/
killed signals against the WRONG target — "T-contract tickers" from
kalshi_market_snapshots.db (up=82%, n=1537). The R/T-1 ultimate sweep used
settlement_truth.strike_price strike-crossing (HIGH up=10.13% / LOW down=5.36%,
n=14130). These are NOT comparable.

THIS SCRIPT re-scores the SAME 7 rebuilt signals (trajectory_state_vector,
rdae_mos, sensor_bias_correction, goldilocks, persistence, late_day_momentum,
nwp_analog) against the SAME universe as R/T-1:
  * settlement_truth.strike_price
  * strike-crossing axis (settlement_temp_f vs strike_price)
  * HIGH and LOW market types
  * contemporaneous-only (signal evaluated at/just-before the label date)
  * date-tiered temp source (settlement_temp_f / temp_source ladder)

For each signal it emits: accuracy, MCC, minority-class precision vs base rate
(separate HIGH-up and LOW-down), and net EV at the real price model
(core/market_cost_model round-trip + Kalshi taker fee) — identical methodology to
reports/minority_class_edge_measurement.json and minority_class_edge.py.

The 7 signals use evaluate(idx, days) with a °F daily series (their native unit).
NOTE: the R/T-1 ultimate sweep's load_daily_series() fed temp_c into these °F-native
in-memory signals — a unit mismatch that explains the NO_FIRES for
trajectory_state_vector/nwp_analog/goldilocks. This re-score uses °F (correct).

READ-ONLY. Outputs a JSON + markdown receipt under reports/.
"""

import contextlib
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.settlement_outcome import strike_crossing_direction
from core.market_cost_model import MARKET_COST_MODEL, ROUND_TRIP_FEE

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")

# Base rates measured from the R/T-1 sweep (reports/minority_class_edge_measurement.json)
HIGH_UP_BASE = 0.10129973211628138   # minority in HIGH = 'up'
LOW_UP_BASE = 0.9464329795112318     # minority in LOW = 'down' => P(down)=1-LOW_UP_BASE


def minority_dir(mt):
    return 'up' if mt == 'HIGH' else 'down'


def minority_base(mt):
    return HIGH_UP_BASE if mt == 'HIGH' else (1.0 - LOW_UP_BASE)


def round_trip_cost(price):
    if price <= 0.0 or price >= 1.0:
        fee = 0.0
    else:
        fee = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return ROUND_TRIP_FEE + fee


def expected_value(p_trade, p_win):
    cost = round_trip_cost(p_trade)
    return p_win * (1.0 - p_trade) + (1.0 - p_win) * (-p_trade) - cost


def load_labels():
    """Mirror of ultimate_sweep.load_labels() — the R/T-1 correct universe."""
    c = sqlite3.connect(METAR_DB)
    cur = c.cursor()
    cur.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f,
               temp_source, trustworthy, strike_price, strike_type
        FROM settlement_truth
        WHERE settlement_temp_f IS NOT NULL
          AND strike_price IS NOT NULL
        ORDER BY station, market_type, local_trading_date
    """)
    rows = cur.fetchall()
    c.close()

    labels = defaultdict(dict)
    src_map = defaultdict(dict)
    meta = defaultdict(dict)
    between = untrusted = no_temp = unknown_mt = 0

    for st, mt, date, temp, src, trust, strike, stype in rows:
        if trust == 0:
            untrusted += 1
            continue
        if stype == 'between':
            between += 1
            continue
        d = strike_crossing_direction(temp, strike, mt)
        if d is None:
            if temp is None:
                no_temp += 1
            else:
                unknown_mt += 1
            continue
        labels[st][(mt, date)] = d
        src_map[st][(mt, date)] = src
        meta[st][(mt, date)] = {
            'strike': strike, 'strike_type': stype, 'temp': temp, 'market_type': mt,
        }

    excl = {
        'between_excluded': between,
        'untrusted_excluded': untrusted,
        'no_temp': no_temp,
        'unknown_market_type': unknown_mt,
    }
    return labels, src_map, meta, excl


def load_daily_series_f(conn, station):
    """Daily METAR series in °F (native unit for these signals). Mirrors the
    parked_killed_signal_build.py loader (which used temp_f), NOT ultimate_sweep's
    temp_c loader."""
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


def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def score_signal(signal, name, labels, conn, mode='daily'):
    """Evaluate signal on correct universe, collecting per-(signal,mt) minority stats.

    Returns per-signal dict with aggregate confusion + per-market-type minority stats.
    """
    stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    per_mt = defaultdict(lambda: {'pred_min': 0, 'pred_min_hit': 0, 'actual_min': 0,
                                  'caught_min': 0, 'total': 0, 'tp': 0, 'fp': 0,
                                  'tn': 0, 'fn': 0})
    fired = 0

    for st, lblmap in labels.items():
        if mode == 'daily':
            days = load_daily_series_f(conn, st)
            if not days:
                continue
            idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for (mt, date), actual in lblmap.items():
            if mode == 'daily':
                idx = idx_by_date.get(date)
                if idx is None:
                    continue
                try:
                    direction, _conf = signal.evaluate(idx, days)
                except Exception:
                    direction = None
            else:
                # station path
                try:
                    direction, _conf = signal.evaluate_for_station(st, date)
                except Exception:
                    direction = None
            if direction is None:
                continue
            fired += 1
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
            # minority accounting
            per_mt[mt]['total'] += 1
            mdir = minority_dir(mt)
            is_min = (actual == mdir)
            pred_min = (direction == mdir)
            if is_min:
                per_mt[mt]['actual_min'] += 1
            if pred_min:
                per_mt[mt]['pred_min'] += 1
                if is_min:
                    per_mt[mt]['pred_min_hit'] += 1
                    per_mt[mt]['caught_min'] += 1

    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0

    result = {
        'accuracy': round(acc, 6),
        'mcc': round(_mcc(tp, fp, tn, fn), 6),
        'n': total, 'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'fired': fired,
        'per_market_type': {},
    }

    for mt in sorted(per_mt.keys()):
        a = per_mt[mt]
        base = minority_base(mt)
        mdir = minority_dir(mt)
        if a['pred_min'] > 0:
            precision = a['pred_min_hit'] / a['pred_min']
        else:
            precision = None
        if a['actual_min'] > 0:
            recall = a['caught_min'] / a['actual_min']
        else:
            recall = None
        delta = (precision - base) if precision is not None else None
        if precision is not None and a['pred_min'] >= 1:
            ev = expected_value(base, precision)
        else:
            ev = None
        result['per_market_type'][mt] = {
            'minority': mdir, 'base': base,
            'n_total': a['total'], 'pred_min': a['pred_min'],
            'actual_min': a['actual_min'], 'caught_min': a['caught_min'],
            'precision': precision, 'recall': recall, 'delta': delta, 'ev': ev,
        }

    return result


def score_late_day_momentum(ldm_fn, labels, conn):
    """Score late_day_momentum using hourly metar_observations (17-22 UTC, temp_f)."""
    # Preload hourly obs per station once
    hourly_cache = {}
    cur = conn.cursor()
    cur.execute("""
        SELECT station, date_utc, CAST(strftime('%H', timestamp_utc) AS INTEGER) AS hr, temp_f
        FROM metar_observations
        WHERE temp_f IS NOT NULL AND temp_f < 200
    """)
    by_station_date = defaultdict(lambda: defaultdict(list))
    for st, date, hr, tf in cur.fetchall():
        by_station_date[st][date].append((hr, tf, date))
    for st in by_station_date:
        hourly_cache[st] = dict(by_station_date[st])

    stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    per_mt = defaultdict(lambda: {'pred_min': 0, 'pred_min_hit': 0, 'actual_min': 0,
                                  'caught_min': 0, 'total': 0, 'tp': 0, 'fp': 0,
                                  'tn': 0, 'fn': 0})
    fired = 0
    for st, lblmap in labels.items():
        hc = hourly_cache.get(st, {})
        for (mt, date), actual in lblmap.items():
            obs = hc.get(date)
            if not obs:
                continue
            try:
                direction, _conf = ldm_fn(obs, date)
            except Exception:
                direction = None
            if direction is None:
                continue
            fired += 1
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
            per_mt[mt]['total'] += 1
            mdir = minority_dir(mt)
            is_min = (actual == mdir)
            pred_min = (direction == mdir)
            if is_min:
                per_mt[mt]['actual_min'] += 1
            if pred_min:
                per_mt[mt]['pred_min'] += 1
                if is_min:
                    per_mt[mt]['pred_min_hit'] += 1
                    per_mt[mt]['caught_min'] += 1

    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    result = {'accuracy': round(acc, 6), 'mcc': round(_mcc(tp, fp, tn, fn), 6),
              'n': total, 'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'fired': fired,
              'per_market_type': {}}
    for mt in sorted(per_mt.keys()):
        a = per_mt[mt]
        base = minority_base(mt)
        mdir = minority_dir(mt)
        precision = a['pred_min_hit'] / a['pred_min'] if a['pred_min'] > 0 else None
        recall = a['caught_min'] / a['actual_min'] if a['actual_min'] > 0 else None
        delta = (precision - base) if precision is not None else None
        ev = expected_value(base, precision) if precision is not None and a['pred_min'] >= 1 else None
        result['per_market_type'][mt] = {
            'minority': mdir, 'base': base, 'n_total': a['total'],
            'pred_min': a['pred_min'], 'actual_min': a['actual_min'],
            'caught_min': a['caught_min'], 'precision': precision,
            'recall': recall, 'delta': delta, 'ev': ev,
        }
    return result


def main():
    t0 = time.time()
    labels, src_map, meta, excl = load_labels()
    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    srcc = Counter(s for v in src_map.values() for s in v.values())
    mt_count = Counter()
    for v in labels.values():
        for (mt, _date) in v.keys():
            mt_count[mt] += 1

    print("=" * 90)
    print("RE-SCORE PARKED/KILLED SIGNALS — CORRECT R/T-1 UNIVERSE")
    print("=" * 90)
    print(f"Correct-axis labels: {n}  (up={lblc['up']}, down={lblc['down']})")
    print(f"market_type: {dict(mt_count)}")
    print(f"temp_source: {dict(srcc)}")
    print(f"excluded: {excl}")
    print(f"base HIGH up = {HIGH_UP_BASE:.4f} (minority 'up'), LOW down = {1-LOW_UP_BASE:.4f} (minority 'down')")
    print()

    conn = sqlite3.connect(METAR_DB)

    # Import the 7 signals
    from core.signals.trajectory_state_vector_signal import TrajectoryStateVectorSignal
    from core.signals.rdae_mos_signal import RDAEMOSSignal
    from core.signals.sensor_bias_correction_signal import SensorBiasCorrectionSignal
    from core.signals.goldilocks_signal import GoldilocksSignal
    from core.signals.persistence_signal import PersistenceSignal
    from core.signals.nwp_analog_signal import NwpAnalogSignal

    signals = [
        ("trajectory_state_vector", TrajectoryStateVectorSignal(METAR_DB), 'daily'),
        ("rdae_mos", RDAEMOSSignal(METAR_DB), 'daily'),
        ("sensor_bias_correction", SensorBiasCorrectionSignal(METAR_DB), 'daily'),
        ("goldilocks", GoldilocksSignal(METAR_DB), 'daily'),
        ("persistence", PersistenceSignal(METAR_DB), 'daily'),
        ("nwp_analog", NwpAnalogSignal(METAR_DB, NWP_DB), 'station'),
    ]

    results = {}

    for name, sig, mode in signals:
        with contextlib.redirect_stderr(io.StringIO()):
            if name == "nwp_analog":
                try:
                    sig._load_nwp_features()
                except Exception:
                    pass
            r = score_signal(sig, name, labels, conn, mode=mode)
        results[name] = r
        pmt = r['per_market_type']
        parts = []
        for mt in ('HIGH', 'LOW'):
            if mt in pmt:
                a = pmt[mt]
                prec = f"{a['precision']:.4f}" if a['precision'] is not None else " — "
                ev = f"{a['ev']:+.4f}" if a['ev'] is not None else " — "
                parts.append(f"{mt}/{a['minority']}:prec={prec}(base{a['base']:.4f}) EV={ev}")
        print(f"{name:<24s} acc={r['accuracy']*100:6.2f}% MCC={r['mcc']:+7.4f} "
              f"n={r['n']:5d} fired={r['fired']:5d}")
        for p in parts:
            print(f"    {p}")

    # late_day_momentum — hourly signal via late_day_momentum_signal(hourly_obs, date)
    from core.late_day_momentum import late_day_momentum_signal as _ldm
    results["late_day_momentum"] = score_late_day_momentum(_ldm, labels, conn)
    r = results["late_day_momentum"]
    print(f"{'late_day_momentum':<24s} acc={r['accuracy']*100:6.2f}% MCC={r['mcc']:+7.4f} "
          f"n={r['n']:5d} fired={r['fired']:5d}")
    for mt in ('HIGH', 'LOW'):
        if mt in r['per_market_type']:
            a = r['per_market_type'][mt]
            prec = f"{a['precision']:.4f}" if a['precision'] is not None else " — "
            ev = f"{a['ev']:+.4f}" if a['ev'] is not None else " — "
            print(f"    {mt}/{a['minority']}:prec={prec}(base{a['base']:.4f}) EV={ev}")

    conn.close()

    # Build receipt
    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'purpose': 're-score parked/killed signals on the R/T-1 correct universe',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction',
        'truth_source': 'settlement_truth.strike_price',
        'n_labels': n,
        'label_distribution': dict(lblc),
        'market_type_distribution': dict(mt_count),
        'base_rate_by_market_type': {'HIGH_up': HIGH_UP_BASE, 'LOW_up': LOW_UP_BASE},
        'temp_source_distribution': dict(srcc),
        'excluded': excl,
        'round_trip_fee': ROUND_TRIP_FEE,
        'daily_series_unit': 'fahrenheit (temp_f) — native unit for these signals',
        'note': ('R/T-1 ultimate_sweep.load_daily_series() fed temp_c into these '
                 'in-memory signals (unit mismatch -> NO_FIRES). This re-score uses '
                 'temp_f, matching the parked/killed build and signal semantics.'),
        'signals': results,
        'read_only': True,
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_json = os.path.join(REPORTS_DIR, 'parked_killed_rescore_correct_universe_2026-09-19.json')
    with open(out_json, 'w') as f:
        json.dump(receipt, f, indent=2)

    # Markdown report
    lines = []
    lines.append("# Parked/Killed Signal Re-Score — Correct R/T-1 Universe")
    lines.append("")
    lines.append(f"**Generated:** {receipt['generated_utc']}")
    lines.append(f"**Axis:** {receipt['axis']}")
    lines.append(f"**Truth:** {receipt['truth_source']} — {n} labels (HIGH up={HIGH_UP_BASE:.4f}, LOW down={1-LOW_UP_BASE:.4f})")
    lines.append("")
    lines.append("**Unit note:** R/T-1 `ultimate_sweep.load_daily_series()` fed `temp_c` into")
    lines.append("these °F-native in-memory signals — a unit mismatch that produced NO_FIRES")
    lines.append("for `trajectory_state_vector`/`nwp_analog`/`goldilocks`. This re-score uses")
    lines.append("`temp_f` (correct native unit).")
    lines.append("")
    lines.append("## Re-Scored Table (comparable universe)")
    lines.append("")
    lines.append("| signal | accuracy | MCC | n | HIGH-up prec/EV | LOW-down prec/EV |")
    lines.append("|---|---|---|---|---|---|")
    for name in sorted(results):
        r = results[name]
        if r.get('accuracy') is None or r.get('n') is None:
            lines.append(f"| {name} | — | — | — | — | — |")
            continue
        pmt = r['per_market_type']
        h = pmt.get('HIGH')
        l = pmt.get('LOW')
        hs = f"{h['precision']:.4f} / {h['ev']:+.4f}" if h and h['precision'] is not None else "—"
        ls = f"{l['precision']:.4f} / {l['ev']:+.4f}" if l and l['precision'] is not None else "—"
        lines.append(f"| {name} | {r['accuracy']*100:.2f}% | {r['mcc']:+.4f} | {r['n']} | {hs} | {ls} |")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    # Determine if any signal has positive net EV on minority
    any_pos = []
    for name in sorted(results):
        r = results[name]
        for mt in ('HIGH', 'LOW'):
            a = r.get('per_market_type', {}).get(mt)
            if a and a.get('ev') is not None and a['ev'] > 0 and a['pred_min'] >= 20:
                any_pos.append((name, mt, a['ev'], a['precision'], a['pred_min']))
    if any_pos:
        lines.append("Minority-class cells with positive net EV (min 20 preds):")
        for name, mt, ev, prec, n in sorted(any_pos, key=lambda x: -x[2]):
            lines.append(f"  - `{name}` / {mt}: net EV {ev:+.4f}, precision {prec:.4f}, n={n}")
    else:
        lines.append("No parked/killed signal shows minority-class lift that survives fees + OOS.")
    lines.append("")
    lines.append("_Read-only. No signal modification, no wiring, no DROP/backfill._")

    out_md = os.path.join(REPORTS_DIR, 'parked_killed_rescore_correct_universe_2026-09-19.md')
    with open(out_md, 'w') as f:
        f.write("\n".join(lines))

    print(f"\nElapsed: {time.time() - t0:.2f}s")
    print(f"[written] {out_json}")
    print(f"[written] {out_md}")


if __name__ == "__main__":
    main()
