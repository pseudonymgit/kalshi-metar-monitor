#!/usr/bin/env python3
"""
CLEAN-LABEL DIRECTIONAL SIGNAL EVAL — single deterministic pass (2026-09-18).

Evaluates the three revived-signal candidates + one reference against the
CLEAN settlement labels, in ONE pass.  This is NOT a tuning loop.

    (a) corrected_pressure_delta   CorrectedPressureDeltaSignal
    (b) nwp_analog                 NwpAnalogSignal (deterministic k-NN)
    (c) goldilocks microstructure  spike-reversion edge  (intraday; see §6)
    (d) pressure_delta             PressureDeltaSignal   (reference)

ANTI-LOOP DESIGN (this is what killed the prior 1h45m / 177k-call attempt):
  1. Per-station daily series are loaded ONCE into an in-memory `days` list.
     Each daily signal is driven via its in-memory `evaluate(idx, days)` path —
     NEVER `evaluate_for_station()` per call (that re-runs GROUP BY over the
     2.4 GB metar_backfill.db on every (station, date), which is the death loop).
  2. Clean ground truth = settlement_epochs rows where epoch_status='closed'
     AND settlement_bucket AND prior_settlement_bucket are both in [-22, 119].
     This single range filter excludes BOTH the 28 `unresolved` rows AND the
     18 "linkage" rows (the day after each unresolved row, whose clean bucket
     points at the unresolved day's still-corrupt prior bucket — see
     docs/plans/SETTLEMENT-EPOCHS-REWRITE-EXECUTED-2026-09-17.md §2/§4).
  3. nwp_analog has no meaningful evaluate(idx, days) (returns None by design —
     it needs the NWP feature surface).  Its NWP feature table is loaded once
     via `_load_nwp_features()`, per-station norm stats and METAR highs are
     cached inside the signal object, and each (station, date) is evaluated via
     `evaluate_nwp_analog(station, date)`.  Bounded, deterministic, no ML.

Output: directional accuracy + Matthews Correlation Coefficient (MCC) per
signal, written to a JSON receipt under reports/.

STDLIB-ONLY (except numpy already imported by the nwp signal).  Deterministic.
No LLM in the loop.  No tuning.  Read-only on the DBs.  No paper/live trading.
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")

LO, HI = -22, 119  # clean settlement_bucket / prior_settlement_bucket range


# ── Ground-truth loading (one query, one pass) ────────────────────────────────

def load_clean_ground_truth(conn):
    """Return {station: {date: 'up'|'down'}} for clean closed HIGH epochs.

    Range filter on BOTH buckets excludes the 28 unresolved rows (their own
    bucket is left corrupt / out-of-range) and the 18 linkage rows (their
    prior bucket points at an unresolved day's corrupt bucket).  Flat
    (bucket == prior) rows carry no direction and are skipped.
    """
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


# ── Daily-series loading (one query per station, called ONCE each) ────────────

def load_daily_pressure(conn, station):
    """Ordered list of {date, pressure} for one station (single GROUP BY)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, AVG(pressure_mb)
        FROM metar_observations
        WHERE station = ? AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    return [{'date': r[0], 'pressure': r[1]}
            for r in cur.fetchall() if r[1] is not None]


# ── Confusion / metrics ───────────────────────────────────────────────────────

def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


def _accumulate(stats, station, direction, actual):
    r = stats[station]
    if direction == 'up':
        if actual == 'up':
            r['tp'] += 1
        else:
            r['fp'] += 1
    else:  # 'down'
        if actual == 'down':
            r['tn'] += 1
        else:
            r['fn'] += 1


# ── Signal evaluators (in-memory; no per-call GROUP BY) ───────────────────────

def evaluate_daily_in_memory(signal, truth, conn):
    """Drive a daily signal via evaluate(idx, days) with per-station caching.

    For each station, `days` (pressure series) is loaded ONCE and an
    idx_by_date map is built ONCE; then the signal fires on in-memory data.
    """
    stats = defaultdict(lambda: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})
    for st in truth:
        days = load_daily_pressure(conn, st)
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
            _accumulate(stats, st, direction, actual)
    return stats


def evaluate_nwp_analog(signal, truth):
    """Evaluate nwp_analog via evaluate_nwp_analog(station, date).

    NWP feature table + per-station norm stats + METAR highs are all cached
    inside the signal object, so each (station, date) is a bounded in-memory
    k-NN lookup — no per-call GROUP BY re-scan of metar_backfill.db.
    """
    stats = defaultdict(lambda: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})
    # Pre-load NWP features once so the loop does not re-open the NWP DB.
    signal._load_nwp_features()
    for st in truth:
        for date, actual in truth[st].items():
            direction, _ = signal.evaluate_nwp_analog(st, date)
            if direction is None:
                continue
            _accumulate(stats, st, direction, actual)
    return stats


# ── Reporting ─────────────────────────────────────────────────────────────────

def summarize(label, stats):
    tp = sum(r['tp'] for r in stats.values())
    fp = sum(r['fp'] for r in stats.values())
    tn = sum(r['tn'] for r in stats.values())
    fn = sum(r['fn'] for r in stats.values())
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    m = mcc(tp, fp, tn, fn)
    fired = sum(1 for r in stats.values()
                if (r['tp'] + r['fp'] + r['tn'] + r['fn']) > 0)
    return {
        'label': label,
        'accuracy': round(acc, 6),
        'mcc': round(m, 6),
        'total': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'fired_stations': fired,
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(METAR_DB)

    truth = load_clean_ground_truth(conn)
    stations = sorted(truth.keys())
    total_epochs = sum(len(v) for v in truth.values())

    print("=" * 76)
    print("CLEAN-LABEL DIRECTIONAL EVAL — single deterministic pass")
    print("=" * 76)
    print(f"Clean HIGH epochs (closed, buckets in [{LO},{HI}]): {total_epochs}")
    print(f"Stations: {len(stations)}")
    print(f"NWP_ANALOG_ENABLED: {NWP_ANALOG_ENABLED}")
    print()

    results = []

    # (d) reference
    ps = PressureDeltaSignal(METAR_DB)
    results.append(summarize('pressure_delta (reference)',
                             evaluate_daily_in_memory(ps, truth, conn)))

    # (a) corrected_pressure_delta
    cps = CorrectedPressureDeltaSignal(METAR_DB)
    results.append(summarize('corrected_pressure_delta',
                             evaluate_daily_in_memory(cps, truth, conn)))

    # (b) nwp_analog
    nwp = NwpAnalogSignal(METAR_DB, NWP_DB)
    results.append(summarize('nwp_analog',
                             evaluate_nwp_analog(nwp, truth)))

    conn.close()

    # (c) goldilocks microstructure spike-reversion edge — intraday, see §6
    goldilocks_note = {
        'label': 'goldilocks_microstructure_spike_reversion',
        'evaluable_against_daily_settlement_labels': False,
        'accuracy': None,
        'mcc': None,
        'reason': (
            "The separated real-time microstructure spike-reversion edge "
            "(microstructure_spike_reversion, emitted via core/data_processor.py "
            "-> _compute_microstructure_spike_confidence) is an INTRAday alert on "
            "the transition_events label surface, not a daily directional signal "
            "with an evaluate(idx, days) interface. Its intraday label surface "
            "(transition_events in /var/data/alerts.db) is NOT present in this "
            "container, so it cannot be scored against the daily settlement_epochs "
            "HIGH buckets without fabricating a label. The daily-level "
            "SpikeReversionSignal (formerly goldilocks) remains KEEP-KILLED per "
            "KILLED-SIGNAL-REVIEW-2026-09-18.md §3 and is intentionally NOT scored "
            "here as a revival candidate."
        ),
    }

    # ── Print table ──
    print("-" * 76)
    print(f"{'signal':<34s} {'accuracy':>10s} {'MCC':>9s} {'n':>7s}")
    print("-" * 76)
    for r in results:
        print(f"{r['label']:<34s} {r['accuracy']*100:>9.2f}% "
              f"{r['mcc']:>+9.4f} {r['total']:>7d}")
    print("-" * 76)
    print(f"{goldilocks_note['label']:<34s} {'N/A (intraday)':>20s}")
    print()

    # ── Confusion detail ──
    print("Confusion (up = positive):")
    for r in results:
        print(f"  {r['label']:<30s} TP={r['tp']:>6d} FP={r['fp']:>6d} "
              f"TN={r['tn']:>6d} FN={r['fn']:>6d}  fired={r['fired_stations']}")
    print()

    print("§6 — goldilocks microstructure spike-reversion edge:")
    print("     " + goldilocks_note['reason'])
    print()
    print(f"Elapsed: {time.time() - t0:.2f}s (single pass, no tuning)")

    # ── Write JSON receipt ──
    receipt = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'clean_label_filter': {
            'market_type': 'HIGH',
            'epoch_status': 'closed',
            'settlement_bucket_range': [LO, HI],
            'prior_settlement_bucket_range': [LO, HI],
            'excluded_unresolved': 28,
            'excluded_linkage': 18,
            'note': 'range filter on both buckets excludes unresolved + linkage rows',
        },
        'total_clean_epochs': total_epochs,
        'stations': len(stations),
        'nwp_analog_enabled': NWP_ANALOG_ENABLED,
        'signals': results,
        'goldilocks_microstructure': goldilocks_note,
        'deterministic': True,
        'single_pass': True,
        'no_mutation': True,
    }
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, 'clean_label_signal_eval_2026-09-18.json')
    with open(out_path, 'w') as f:
        json.dump(receipt, f, indent=2)
    print(f"\nReceipt written: {out_path}")


if __name__ == "__main__":
    main()
