#!/usr/bin/env python3
"""
KILLED-SIGNAL RETEST against TRUE data (corrected strike-crossing axis).

Dan's directive: "old failed signals we might be able to test against true data or invert."

This re-runs the killed-signal direction-accuracy + MCC + INVERTED evaluation
against the CORRECT target variable — strike-crossing (settlement_temp vs Kalshi
strike) — NOT the day-over-day (settlement_bucket vs prior_settlement_bucket) axis
that the prior INVERSION-BACKTEST-2026-09-18.md used.

KEY DIFFERENCE from the prior inversion backtest:
  - Prior: direction = 'up' if settlement_bucket > prior_settlement_bucket (day-over-day)
  - Now:    direction = 'up' if settlement_temp > strike (HIGH), '<' strike (LOW)
    per core/settlement_outcome.py strike_crossing_direction().

The killed signals (per task):
  corrected_pressure_delta, spike_reversion (goldilocks daily), nwp_analog,
  persistence, simple_trend, wind_direction_shift, late_day_momentum,
  goldilocks (microstructure).

For each: direction accuracy + MCC, both original AND inverted, against TRUE data.

READ-ONLY. No signal re-registered, no commit, no live trading. Recommendation + numbers only.

LIMITATION (documented, not a blocker): the full historical strike backfill (P0-B) is
NOT yet landed. The correct-axis strike-crossing labels are limited to the 56 rows in
settlement_truth that carry a real strike (2026-09-07/08, GHCN authoritative temp).
Results below are computed on best-available correct data and MUST be re-run after P0-B
lands (full 2021->present strike history) before any revival decision.
"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import strike_crossing_direction

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")


# ---------------------------------------------------------------------------
# 1. Correct-axis ground truth: strike-crossing from settlement_truth
# ---------------------------------------------------------------------------

def load_strike_truth(conn):
    """{station: {date: 'up'|'down'}} using strike_crossing_direction on
    settlement_truth (settlement_temp_f vs strike_price), market_type-aware.
    Skips untrustworthy dates. Excludes ties (direction=None only when strike
    missing; ties resolve to 'down' for HIGH / 'up' for LOW per strict rule)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f,
               strike_price, strike_type, trustworthy
        FROM settlement_truth
        WHERE strike_price IS NOT NULL
          AND settlement_temp_f IS NOT NULL
        ORDER BY station, market_type, local_trading_date
    """)
    truth = defaultdict(dict)
    meta = defaultdict(dict)
    for station, mt, date, temp, strike, stype, trust in cur.fetchall():
        if trust == 0:
            continue  # skip untrustworthy (2026-05-28, 2026-08-10)
        d = strike_crossing_direction(temp, strike, mt)
        if d is None:
            continue
        # key by station+date+market_type to avoid HIGH/LOW collision
        key = (station, mt, date)
        truth[station][key] = d
        meta[station][key] = {'temp': temp, 'strike': strike, 'type': stype, 'market_type': mt}
    return truth, meta


# ---------------------------------------------------------------------------
# 2. Daily series loaders (for signal.evaluate(idx, days))
# ---------------------------------------------------------------------------

def load_daily_series(conn, station):
    """Ordered list of daily dicts with all fields the signals need:
    date, high, low, temp, pressure, wind_dir, wind_speed."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc,
               MAX(temp_c) AS high,
               MIN(temp_c) AS low,
               AVG(temp_c) AS temp,
               AVG(pressure_mb) AS pressure,
               AVG(wind_direction_deg) AS wind_dir,
               AVG(wind_speed_kt) AS wind_speed
        FROM metar_observations
        WHERE station = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    out = []
    for r in cur.fetchall():
        d = {
            'date': r[0],
            'high': r[1],
            'low': r[2],
            'temp': r[3],
            'pressure': r[4],
            'wind_dir': r[5],
            'wind_speed': r[6],
        }
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 3. MCC + stats
# ---------------------------------------------------------------------------

def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


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


def invert(direction):
    if direction == 'up':
        return 'down'
    if direction == 'down':
        return 'up'
    return None


def summarize(stats):
    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    m = mcc(tp, fp, tn, fn)
    return {'accuracy': round(acc, 6), 'mcc': round(m, 6), 'n': total,
            'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn}


def classify(orig, inv):
    oa, om = orig['accuracy'], orig['mcc']
    ia, im = inv['accuracy'], inv['mcc']
    NOISE_LO, NOISE_HI = 0.40, 0.60  # widened band given tiny n
    if orig['n'] < 20:
        return "UNDERPOWERED (n<20 — cannot resolve inversion vs noise; re-run after P0-B)"
    if NOISE_LO <= oa <= NOISE_HI and NOISE_LO <= ia <= NOISE_HI:
        return "NOISE (coin-flip; wrongness not persistent)"
    if ia > 0.55 and im > 0.0 and oa < 0.50:
        return "STABLE WRONG-WAY (inversion edge — flip yields real signal)"
    if oa > 0.55 and om > 0.0:
        return "STABLE RIGHT-WAY (already correct on strike axis)"
    if oa < 0.45 and ia > 0.55:
        return "STABLE WRONG-WAY (inversion edge)"
    return "INCONCLUSIVE (weak/mixed; does not clear thresholds)"


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    conn = sqlite3.connect(METAR_DB)
    truth, meta = load_strike_truth(conn)

    # flatten to per-station list of (key, actual)
    n_labels = sum(len(v) for v in truth.values())
    print("=" * 90)
    print("KILLED-SIGNAL RETEST — TRUE DATA (strike-crossing axis)")
    print("=" * 90)
    print(f"Correct-axis labels (strike-crossing, settlement_truth): {n_labels}")
    print(f"Stations: {len(truth)}")
    print("NOTE: strike backfill (P0-B) NOT landed -> only Sep 7/8 window (~56 rows).")
    print()

    # Import signals
    from core.signals.pressure_delta_signal import PressureDeltaSignal
    from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal
    from core.signals.spike_reversion_signal import SpikeReversionSignal
    from core.signals.persistence_signal import PersistenceSignal
    from core.signals.simple_trend_signal import SimpleTrendSignal
    from core.signals.wind_direction_shift import WindDirectionShiftSignal
    from core.signals.nwp_analog_signal import NwpAnalogSignal, NWP_ANALOG_ENABLED

    # NOTE: late_day_momentum and goldilocks-microstructure are intraday/real-time
    # signals; they don't have a daily evaluate(idx, days) surface in this tree.
    # We mark them UNAVAILABLE (needs transition_events label surface, absent).

    results = {}

    def run_daily(name, signal, field_hint=None):
        """Drive a daily signal over strike-truth dates."""
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        n_fired = 0
        for st, truth_map in truth.items():
            days = load_daily_series(conn, st)
            if not days:
                continue
            idx_by_date = {d['date']: i for i, d in enumerate(days)}
            for key, actual in truth_map.items():
                station, mt, date = key
                idx = idx_by_date.get(date)
                if idx is None:
                    continue
                try:
                    direction, _ = signal.evaluate(idx, days)
                except Exception as e:
                    # some signals need wind/pressure; if missing, skip
                    continue
                if direction is None:
                    continue
                n_fired += 1
                accumulate(orig, direction, actual)
                accumulate(inv, invert(direction), actual)
        os_ = summarize(orig)
        is_ = summarize(inv)
        results[name] = {'original': os_, 'inverted': is_,
                         'verdict': classify(os_, is_), 'n_fired': n_fired}
        print(f"{name:<26s} orig acc={os_['accuracy']*100:6.2f}% MCC={os_['mcc']:+7.4f} "
              f"| inv acc={is_['accuracy']*100:6.2f}% MCC={is_['mcc']:+7.4f} "
              f"| n={os_['n']:3d} fired={n_fired:3d} -> {results[name]['verdict']}")

    # (1) pressure_delta reference
    run_daily("pressure_delta (ref)", PressureDeltaSignal(METAR_DB))
    # (2) corrected_pressure_delta
    run_daily("corrected_pressure_delta", CorrectedPressureDeltaSignal(METAR_DB))
    # (3) spike_reversion (goldilocks daily)
    run_daily("spike_reversion", SpikeReversionSignal(METAR_DB))
    # (4) persistence
    run_daily("persistence", PersistenceSignal(METAR_DB))
    # (5) simple_trend
    run_daily("simple_trend", SimpleTrendSignal(METAR_DB))
    # (6) wind_direction_shift
    run_daily("wind_direction_shift", WindDirectionShiftSignal(METAR_DB))

    # (7) nwp_analog — has a special evaluate path
    try:
        nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
        nwp._load_nwp_features()
        orig = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        inv = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        n_fired = 0
        for st, truth_map in truth.items():
            for key, actual in truth_map.items():
                station, mt, date = key
                try:
                    direction, _ = nwp.evaluate_nwp_analog(st, date)
                except Exception:
                    continue
                if direction is None:
                    continue
                n_fired += 1
                accumulate(orig, direction, actual)
                accumulate(inv, invert(direction), actual)
        os_ = summarize(orig); is_ = summarize(inv)
        results["nwp_analog"] = {'original': os_, 'inverted': is_,
                                 'verdict': classify(os_, is_), 'n_fired': n_fired}
        print(f"{'nwp_analog':<26s} orig acc={os_['accuracy']*100:6.2f}% MCC={os_['mcc']:+7.4f} "
              f"| inv acc={is_['accuracy']*100:6.2f}% MCC={is_['mcc']:+7.4f} "
              f"| n={os_['n']:3d} fired={n_fired:3d} -> {results['nwp_analog']['verdict']}")
    except Exception as e:
        results["nwp_analog"] = {'error': str(e)}
        print(f"nwp_analog: ERROR {e}")

    # (8) late_day_momentum + goldilocks microstructure — intraday, unavailable
    results["late_day_momentum"] = {
        'unavailable': True,
        'note': 'intraday 17-22 UTC signal; no daily evaluate(idx,days) surface in tree; '
                'needs transition_events label surface (absent).'
    }
    results["goldilocks_microstructure"] = {
        'unavailable': True,
        'note': 'real-time microstructure spike-reversion; needs transition_events label '
                'surface (absent in this container).'
    }
    print("late_day_momentum: UNAVAILABLE (intraday; needs transition_events surface)")
    print("goldilocks_microstructure: UNAVAILABLE (intraday; needs transition_events surface)")

    conn.close()

    print(f"\nElapsed: {time.time() - t0:.2f}s")

    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price), '
                'core/settlement_outcome.strike_crossing_direction',
        'label_source': 'settlement_truth (date-tiered: GHCN authoritative through 2026-09-12; '
                        'kalshi_finalized 09-13..16; weatherapi recency bridge)',
        'n_correct_axis_labels': n_labels,
        'stations': len(truth),
        'strike_backfill_status': 'P0-B NOT LANDED — 56 strike rows (Sep 7/8) only; '
                                  'full history pending',
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': results,
        'read_only': True, 'no_mutation': True, 'no_commit': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, 'killed_signal_retest_true_data_2026-09-18.json')
    with open(out_path, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out_path}")


if __name__ == "__main__":
    main()
