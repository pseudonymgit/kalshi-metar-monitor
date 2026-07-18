#!/usr/bin/env python3
"""
Walk-Forward Calibrated Backtest: Deterministic k-NN NWP Analog Signal

Integrates with CalibrationPipeline for walk-forward isotonic regression
calibration. Tests the calibrated signal accuracy.

Usage:
    NWP_ANALOG_ENABLED=1 PYTHONPATH=. python3 scripts/backtest_nwp_analog_knn.py

Reference: Expert 4 spec @ docs/plans/GRAY-ROOM-ROUND3-EXPERT4-METEOROLOGY.md
"""

import logging
import sys
import os
from collections import defaultdict
from datetime import datetime, timedelta
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals.nwp_analog_signal import NwpAnalogSignal
from core.calibration_pipeline import CalibrationPipeline

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_calibrated_backtest(stations=None):
    """
    Walk-forward backtest with isotonic calibration.

    For each (station, date) with NWP data, the signal is evaluated using
    only data strictly before that date. The raw confidence is calibrated
    via expanding-window isotonic regression.  Results are compared against
    the METAR-observed HIGH temperature direction.
    """
    sig = NwpAnalogSignal()
    features, all_stations, all_dates = sig._load_nwp_features()

    if stations is None:
        stations = all_stations

    # Pre-filter dates to those with valid feature vectors
    print("Pre-loading data...")
    station_calendar = {}  # {station: [(date, target_vec, prev_date), ...]}
    for st in stations:
        entries = []
        for d in all_dates:
            if d < all_dates[60]:  # Need enough history
                continue
            vec = sig._build_feature_vector(st, d)
            if vec is not None:
                prev_d = (datetime.strptime(d, '%Y-%m-%d')
                          - timedelta(days=1)).strftime('%Y-%m-%d')
                entries.append((d, vec, prev_d))
        if entries:
            station_calendar[st] = entries

    # Initialise calibration pipeline for nwp_analog signal
    calib = CalibrationPipeline(['nwp_analog'], stations)

    results = defaultdict(lambda: {
        'total': 0, 'correct': 0, 'raw_correct': 0,
        'up_pred': 0, 'down_pred': 0,
        'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0,
        'sum_raw_conf': 0.0, 'sum_cal_conf': 0.0,
    })
    overall = {'total': 0, 'correct': 0, 'raw_correct': 0,
               'sum_raw_conf': 0.0, 'sum_cal_conf': 0.0}

    # ── Walk-forward ──────────────────────────────────────────────
    for st in stations:
        entries = station_calendar.get(st, [])
        if not entries:
            continue

        highs = sig._load_metar_highs(st)
        print(f"Backtesting {st}: {len(entries)} dates")

        for idx, (td, _, prev_d) in enumerate(entries):
            # Ground truth
            if prev_d not in highs or td not in highs:
                continue
            if highs[prev_d] is None or highs[td] is None:
                continue
            actual_dir = 'up' if highs[td] > highs[prev_d] else 'down'

            # NWP analog prediction
            direction, raw_conf = sig.evaluate_nwp_analog(st, td)
            if direction is None:
                continue

            # Walk-forward calibration: fit on data up to idx-1, calibrate idx
            calib.set_window_start(0)  # Expanding window
            calib.refit()
            cal_conf = calib.calibrate('nwp_analog', st, raw_conf)

            # If calibrated confidence gives same direction, score it
            # Calibration doesn't change direction — it adjusts confidence
            cal_direction = direction  # Direction unchanged by calibration

            res = results[st]
            res['total'] += 1
            overall['total'] += 1
            res['sum_raw_conf'] += raw_conf
            res['sum_cal_conf'] += cal_conf
            overall['sum_raw_conf'] += raw_conf
            overall['sum_cal_conf'] += cal_conf

            if actual_dir == 'up':
                res['up_actual'] = res.get('up_actual', 0) + 1
            else:
                res['down_actual'] = res.get('down_actual', 0) + 1

            if direction == 'up':
                res['up_pred'] += 1
                if actual_dir == 'up':
                    res['correct'] += 1
                    overall['correct'] += 1
                    res['tp'] += 1
                else:
                    res['fp'] += 1
            else:
                res['down_pred'] += 1
                if actual_dir == 'down':
                    res['correct'] += 1
                    overall['correct'] += 1
                    res['tn'] += 1
                else:
                    res['fn'] += 1

            # Update calibration history
            calib.update('nwp_analog', st, raw_conf, direction == actual_dir)

    # ── Report ───────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("NWP Analog k-NN — Calibrated Walk-Forward Backtest")
    print(f"{'='*72}")
    print(f"K={sig.K}, ±{sig.SEASONAL_WINDOW}d window, fields={list(sig.FIELD_WEIGHTS.keys())}")
    print(f"Calibration: isotonic regression via CalibrationPipeline (min_samples={calib.MIN_SAMPLES})")
    print(f"{'='*72}")

    print(f"\n{'Station':<8s} {'Total':<7s} {'Correct':<8s} {'Acc':<7s} "
          f"{'Up':<5s} {'Down':<5s} {'RawConf':<8s} {'CalConf':<8s}")
    print("-" * 64)

    accs = []
    for st in stations:
        r = results.get(st)
        if r and r['total'] > 0:
            acc = r['correct'] / r['total'] * 100
            avg_raw = r['sum_raw_conf'] / r['total'] * 100
            avg_cal = r['sum_cal_conf'] / r['total'] * 100
            accs.append(acc)
            print(f"{st:<8s} {r['total']:<7d} {r['correct']:<8d} "
                  f"{acc:<6.1f}% "
                  f"{r['up_pred']:<5d} {r['down_pred']:<5d} "
                  f"{avg_raw:<7.1f}% {avg_cal:<7.1f}%")
        else:
            print(f"{st:<8s} {'---':<7s}")

    print("-" * 64)
    if overall['total'] > 0:
        oa = overall['correct'] / overall['total'] * 100
        oraw = overall['sum_raw_conf'] / overall['total'] * 100
        ocal = overall['sum_cal_conf'] / overall['total'] * 100
        print(f"{'ALL':<8s} {overall['total']:<7d} {overall['correct']:<8d} "
              f"{oa:<6.1f}% {'':<12s} {oraw:<7.1f}% {ocal:<7.1f}%")
        print(f"\n  Per-city: min={min(accs):.1f}%  mean={sum(accs)/len(accs):.1f}%  max={max(accs):.1f}%")

    # ECE evaluation
    print("\n--- Calibration Quality ---")
    for st in stations:
        r = results.get(st)
        if r and r['total'] >= 50:
            raw_confs = []
            corrects = []
            for entry in station_calendar.get(st, []):
                td, _, prev_d = entry
                highs = sig._load_metar_highs(st)
                if prev_d not in highs or td not in highs:
                    continue
                actual = 'up' if highs[td] > highs[prev_d] else 'down'
                dir, conf = sig.evaluate_nwp_analog(st, td)
                if dir is not None:
                    raw_confs.append(conf)
                    corrects.append(dir == actual)
            ece = calib.evaluate_calibration('nwp_analog', st, raw_confs, corrects) if len(raw_confs) >= 50 else 0
            print(f"  {st}: ECE={ece:.4f}  (n={len(raw_confs)})" if len(raw_confs) >= 50 else f"  {st}: insufficient data")

    print()


if __name__ == "__main__":
    run_calibrated_backtest()
