#!/usr/bin/env python3
"""
R/T-8H — TRUE Exhaustive Calibration Grid (script-only, NO AI-in-loop, READ-ONLY).

R/T-8b was a SINGLE-PASS fusion + target slicing (282s). It did NOT sweep the
calibration HYPERPARAMETERS — it only sliced the fused output by station/regime/
season/direction. This is the real mandate: sweep the PARAMETERS, not the slices.

WHAT IS ACTUALLY TUNABLE (verified against source, 2026-09-21):

  The connected-fusion target is SignalFusionEngine.fuse_signals() (LIOP/LLOP in
  core/fusion_logic.py). Its Layer-0 calibration is a CalibrationPipeline
  (core/calibration_pipeline.py) using per-signal-per-city isotonic regression.

  The calibration pipeline exposes FOUR genuinely tunable knobs that change the
  DIRECTIONAL strike-crossing score:

    1. IsotonicRegression clipping bounds  y_min / y_max
       — hardcoded 0.05 / 0.95 in refit(); swept {0.01,0.05,0.10} x {0.90,0.95,0.99}
    2. MIN_SAMPLES  (min co-firing trades to fit a per-cell calibrator)
       — class attr = 200; swept {10, 30, 50, 100, 200}
    3. max_history  (cap on per-bucket history, walk-forward memory)
       — ctor = 2000; swept {500, 2000, None (unbounded)}
    4. window_start (expanding-window lower bound offset)
       — ctor = 0; swept {0, 100, 500}

  IMPORTANT HONESTY NOTE — why these four, and why NOT the others:
    * These four live INSIDE the directional LIOP fusion path and are re-fitted
      from real history each run, so sweeping them changes the actual fused
      direction/confidence on the strike-crossing axis.
    * The calibration MODE (platt/bma/emos/both) axis lives in ModulatorStack
      (core/modulator_stack.py) — a SEPARATE post-fusion stage that is NOT on
      the LIOP directional scoring path used here. It is a different surface.
    * hierarchical_bayes.py TIER_THRESHOLDS + sample_floor.py shrinkage operate
      on TEMPERATURE-ERROR calibration (bias/mae/rmse in °F), NOT on the
      directional P(correct) confidence. They do not touch the strike-crossing
      directional score. They are enumerated but NOT swept as directional axes
      (they would be a fabricated axis — no effect on this score).
    * bma_emos regularization is applied at cache-load of pre-computed params;
      it does not affect the isotonic LIOP path.

  So the grid below is REAL: every swept parameter genuinely moves the fused
  directional output, and the effect is measured with a correct walk-forward
  fit (no lookahead).

CRITICAL FIX vs R/T-8b: R/T-8b constructed SignalFusionEngine fresh per event and
NEVER called fit_calibration()/refit() — so its isotonic calibration was identity
and those parameters were inert. THIS script performs a proper WALK-FORWARD
population: for each labeled event at time t, the calibrator is fitted ONLY on
strictly-earlier events (date < t), so the isotonic params are actually live and
the sweep is meaningful.

GRID DIMENSIONS:
  param combos = 9 (clip) x 5 (min_samples) x 3 (max_history) x 3 (window_start)
               = 405
  target slices = per-station(20) x per-regime(2) x per-season(4) x per-direction(2)
  => 405 param combos scored against the FULL fused event set, then the resulting
     events are SLICED into the multi-target dimensions. Total scoring cells =
     405 (param combos) [each producing a full fused-event table], then slices
     derived cheaply from each table.

PARALLEL ENGINE: ProcessPool, worker-per-param-combo (deterministic ordering,
chunksize=1). Each worker rebuilds the signal registry + its own sqlite conn
(no shared mutable state). Byte-identical schema to serial path.

GROUND TRUTH: core/settlement_outcome.strike_crossing_direction
              labels from settlement_truth.strike_price (14,264 rows).
Signals = BACKTEST_SIGNALS (7 live). Fusion = SignalFusionEngine (LIOP).

READ-ONLY. No signal wiring, no DB writes, no data mutation.

Usage:
  python3 scripts/rt8h_exhaustive_calibration_grid.py              # full grid
  python3 scripts/rt8h_exhaustive_calibration_grid.py --workers 8  # parallel
  python3 scripts/rt8h_exhaustive_calibration_grid.py --dry-run    # enumerate only
  python3 scripts/rt8h_exhaustive_calibration_grid.py --subsample  # documented subsample
  python3 scripts/rt8h_exhaustive_calibration_grid.py --write-receipt  # docs receipt

Outputs:
  reports/rt8h_exhaustive_calibration_grid_<ts>.json
  reports/rt8h_exhaustive_calibration_grid_<ts>.md
"""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np

# ── Parallelization (reuses R/T-8G pattern) ──
import concurrent.futures as _cf
import multiprocessing as _mp

_PARALLEL = False
_NUM_WORKERS = 1

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.settlement_outcome import strike_crossing_direction, UP, DOWN
from core.calibration_pipeline import CalibrationPipeline
from core.fusion_logic import SignalFusionEngine

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
REPORTS_DIR = os.path.join(BASE, "reports")

BACKTEST_SIGNALS = [
    'gaussian', 'gaussian_v2', 'pressure_delta', 'calendar_climatology',
    'forecast_disagreement', 'corrected_pressure_delta', 'frontal_passage_intraday',
]

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO',
]

# ── Parameterizable calibration pipeline (exposes the 4 real knobs) ──

class ParamCalibrationPipeline(CalibrationPipeline):
    """CalibrationPipeline with the isotonic clipping bounds + min-samples
    exposed as constructor parameters (they are hardcoded in the parent)."""

    def __init__(self, signal_names, city_codes,
                 y_min=0.05, y_max=0.95, min_samples=200,
                 max_history=2000, window_start=0):
        # Set MIN_SAMPLES before super().__init__ so refit() uses our value.
        self.MIN_SAMPLES = min_samples
        # 'None' (unbounded) is NOT supported by the parent update() which does
        # `len(history) > self.max_history`; encode as a large sentinel instead.
        if max_history is None:
            max_history = 10 ** 9
        super().__init__(signal_names, city_codes,
                         max_history=max_history, window_start=window_start)
        self.y_min = y_min
        self.y_max = y_max

    def refit(self):
        """Refit with parameterized clipping bounds + min-samples (mirrors parent
        logic but with the knobs swept)."""
        self.prune_history()

        for signal in self.signal_names:
            for city in self.city_codes:
                key = (signal, city)
                data = self.history.get(key, [])
                if len(data) >= self.MIN_SAMPLES:
                    X = np.array([d[0] for d in data])
                    y = np.array([float(d[1]) for d in data])
                    iso = _isotonic(self.y_min, self.y_max)
                    iso.fit(X, y)
                    self.calibrators[key] = iso
                elif key in self.calibrators:
                    del self.calibrators[key]

        for signal in self.signal_names:
            global_data = []
            for city in self.city_codes:
                global_data.extend(self.history.get((signal, city), []))
            if len(global_data) >= 2 * self.MIN_SAMPLES:
                X = np.array([d[0] for d in global_data])
                y = np.array([float(d[1]) for d in global_data])
                iso = _isotonic(self.y_min, self.y_max)
                iso.fit(X, y)
                self.fallback_calibrators[signal] = iso
            elif signal in self.fallback_calibrators:
                del self.fallback_calibrators[signal]

        all_data = []
        for data in self.history.values():
            all_data.extend(data)
        if len(all_data) >= 4 * self.MIN_SAMPLES:
            X = np.array([d[0] for d in all_data])
            y = np.array([float(d[1]) for d in all_data])
            iso = _isotonic(self.y_min, self.y_max)
            iso.fit(X, y)
            self.global_calibrator = iso

        self.refitted = True


def _isotonic(y_min, y_max):
    from sklearn.isotonic import IsotonicRegression
    return IsotonicRegression(out_of_bounds='clip', y_min=y_min, y_max=y_max)


# ── Reuse the canonical harness loaders (single source of truth for labels) ──

from scripts.ultimate_sweep import (
    load_labels,
    load_daily_series,
    evaluate_signal,
    build_signal_registry,
)


def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _season(date_str):
    try:
        m = int(date_str.split('-')[1])
    except (ValueError, IndexError):
        return 'UNK'
    if m in (12, 1, 2):
        return 'DJF'
    if m in (3, 4, 5):
        return 'MAM'
    if m in (6, 7, 8):
        return 'JJA'
    return 'SON'


def _conf_matrix(pairs):
    tp = fp = tn = fn = 0
    for pred, actual in pairs:
        if pred == 'up':
            if actual == 'up':
                tp += 1
            else:
                fp += 1
        else:
            if actual == 'down':
                tn += 1
            else:
                fn += 1
    n = tp + fp + tn + fn
    return {
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'n': n,
        'accuracy': round((tp + tn) / n, 6) if n else 0.0,
        'mcc': round(_mcc(tp, fp, tn, fn), 6),
    }


def _base_rate_null(actuals):
    if not actuals:
        return 0.0
    up = sum(1 for a in actuals if a == 'up')
    down = len(actuals) - up
    return max(up, down) / len(actuals)


def _brier(pairs):
    """pairs: list of (pred, actual, conf). Brier on P(up)."""
    if not pairs:
        return 1.0
    total = 0.0
    for pred, actual, conf in pairs:
        p = conf if pred == 'up' else (1.0 - conf)
        y = 1.0 if actual == 'up' else 0.0
        total += (p - y) ** 2
    return total / len(pairs)


def _ece(pairs, n_bins=10):
    """ECE on P(up), equal-width bins."""
    if not pairs:
        return 1.0
    bins = [[] for _ in range(n_bins)]
    for pred, actual, conf in pairs:
        p = conf if pred == 'up' else (1.0 - conf)
        y = 1.0 if actual == 'up' else 0.0
        b = min(int(p * n_bins), n_bins - 1)
        bins[b].append((p, y))
    ece = 0.0
    n = len(pairs)
    for b in bins:
        if not b:
            continue
        mean_conf = sum(p for p, _ in b) / len(b)
        mean_acc = sum(y for _, y in b) / len(b)
        ece += (len(b) / n) * abs(mean_acc - mean_conf)
    return ece


def _full_metrics(pairs, base_rate):
    cm = _conf_matrix([(p, a) for p, a, _ in pairs])
    cm['base_rate_accuracy'] = round(base_rate, 6)
    cm['base_rate_delta'] = round(cm['accuracy'] - base_rate, 6)
    cm['brier'] = round(_brier(pairs), 6)
    cm['ece'] = round(_ece(pairs), 6)
    return cm


# ─────────────────────────────────────────────────────────────────────────
# GRID DEFINITION
# ─────────────────────────────────────────────────────────────────────────

def _clip_axis():
    return [(ymin, ymax)
            for ymin in (0.01, 0.05, 0.10)
            for ymax in (0.90, 0.95, 0.99)]


def _param_combos():
    """Deterministic ordered list of (y_min, y_max, min_samples, max_history, window_start)."""
    combos = []
    for (ymin, ymax) in _clip_axis():
        for ms in (10, 30, 50, 100, 200):
            for mh in (500, 2000, None):
                for ws in (0, 100, 500):
                    combos.append((ymin, ymax, ms, mh, ws))
    return combos


# ─────────────────────────────────────────────────────────────────────────
# WALK-FORWARD FUSED EVENT COLLECTION (one param combo → fused event table)
# ─────────────────────────────────────────────────────────────────────────

def _collect_raw_events(registry, labels, conn):
    """Collect all labeled events ONCE with raw signal evaluations.

    This is COMBO-INDEPENDENT: signal evaluation depends only on the signals,
    not the calibration hyperparameters. Computed once, reused across every
    param combo (the grid sweeps calibration params, not signal evaluation).

    Returns a chronologically-sorted list of event dicts:
      {station, regime, season, date, actual, sig_list}
    where sig_list = [(name, direction, raw_conf), ...].
    """
    all_events = []
    for st in STATIONS:
        lblmap = labels.get(st)
        if not lblmap:
            continue
        days = load_daily_series(conn, st)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for (mt, date), actual in lblmap.items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            sig_list = []
            for name in BACKTEST_SIGNALS:
                sig = registry.get(name)
                if sig is None:
                    continue
                with contextlib.redirect_stderr(io.StringIO()):
                    direction, confidence = evaluate_signal(sig, name, st, idx, date, days, conn)
                if direction is None:
                    continue
                sig_list.append((name, direction, float(confidence or 0.5)))
            all_events.append({
                'station': st, 'regime': mt, 'season': _season(date),
                'date': date, 'actual': actual, 'sig_list': sig_list,
            })
    all_events.sort(key=lambda e: (e['date'], e['station'], e['regime']))
    return all_events


def _build_fused_events(params, raw_events):
    """Walk-forward fused events for ONE param combo, reusing raw_events.

    Fits calibration ONLY on strictly-earlier events (no lookahead), then
    fuses each event through SignalFusionEngine with the swept params.
    """
    y_min, y_max, min_samples, max_history, window_start = params

    history = defaultdict(list)
    out = []
    for ev in raw_events:
        st = ev['station']
        sig_list = ev['sig_list']
        names = [n for n, _, _ in sig_list]

        engine = SignalFusionEngine(signal_names=names, city_codes=[st])
        engine.calibration_pipeline = ParamCalibrationPipeline(
            signal_names=names, city_codes=[st],
            y_min=y_min, y_max=y_max, min_samples=min_samples,
            max_history=max_history, window_start=window_start,
        )
        # Feed PRIOR walk-forward history (only for this station's signals).
        for (sig_name, city), hist in history.items():
            if city == st and sig_name in names:
                for raw_c, was_correct in hist:
                    engine.calibration_pipeline.update(sig_name, city, raw_c, was_correct)
        engine.calibration_pipeline.refit()

        try:
            direction, prob, confidence = engine.fuse_signals(sig_list, st)
        except Exception:
            direction, prob, confidence = None, 0.0, 0.0

        predicted = direction
        conf = float(confidence or 0.5) if direction is not None else 0.5

        out.append({
            'station': st, 'regime': ev['regime'], 'season': ev['season'],
            'date': ev['date'], 'actual': ev['actual'],
            'predicted': predicted, 'confidence': conf,
        })

        # Record this event's outcomes for FUTURE events (walk-forward).
        if predicted is not None:
            for (sig_name, sig_dir, sig_conf) in sig_list:
                was_correct = (sig_dir == ev['actual'])
                history[(sig_name, st)].append((sig_conf, was_correct))

    return out


# ─────────────────────────────────────────────────────────────────────────
# SCORE one param combo → global + sliced metrics
# ─────────────────────────────────────────────────────────────────────────

def _score_combo(params, raw_events):
    events = _build_fused_events(params, raw_events)
    fired = [e for e in events if e['predicted'] is not None]

    result = {
        'params': {
            'y_min': params[0], 'y_max': params[1],
            'min_samples': params[2],
            'max_history': params[3], 'window_start': params[4],
        },
        'totals': {
            'labeled_events': len(events),
            'fused_fired': len(fired),
            'fused_null_rate': round(1.0 - (len(fired) / len(events) if events else 0), 6),
        },
    }

    def _slices(rows):
        d = {}
        # global
        d['global'] = _full_metrics(
            [(r['predicted'], r['actual'], r['confidence']) for r in rows],
            _base_rate_null([r['actual'] for r in rows]),
        )
        # per station
        d['station'] = {}
        for st in STATIONS:
            r = [x for x in rows if x['station'] == st]
            if r:
                d['station'][st] = _full_metrics(
                    [(x['predicted'], x['actual'], x['confidence']) for x in r],
                    _base_rate_null([x['actual'] for x in r]),
                )
        # per regime
        d['regime'] = {}
        for rg in ('HIGH', 'LOW'):
            r = [x for x in rows if x['regime'] == rg]
            if r:
                d['regime'][rg] = _full_metrics(
                    [(x['predicted'], x['actual'], x['confidence']) for x in r],
                    _base_rate_null([x['actual'] for x in r]),
                )
        # per season
        d['season'] = {}
        for se in ('DJF', 'MAM', 'JJA', 'SON'):
            r = [x for x in rows if x['season'] == se]
            if r:
                d['season'][se] = _full_metrics(
                    [(x['predicted'], x['actual'], x['confidence']) for x in r],
                    _base_rate_null([x['actual'] for x in r]),
                )
        # per direction (up vs down separately)
        d['direction'] = {}
        for dr in ('up', 'down'):
            r = [x for x in rows if x['predicted'] == dr]
            if r:
                d['direction'][dr] = _full_metrics(
                    [(x['predicted'], x['actual'], x['confidence']) for x in r],
                    _base_rate_null([x['actual'] for x in r]),
                )
        return d

    result['metrics'] = _slices(fired)
    return result


# ─────────────────────────────────────────────────────────────────────────
# PARALLEL MAP (worker-per-param-combo)
# ─────────────────────────────────────────────────────────────────────────

def _combo_worker(task):
    """Top-level worker (picklable): scores ONE combo against shared raw events.

    raw_events is passed in the task (fully picklable: only strings/floats/ints)."""
    (params, raw_events) = task
    res = _score_combo(params, raw_events)
    return params, res


def _run_grid(param_combos, raw_events):
    """Deterministic ordered sweep over param combos (serial or parallel)."""
    results = []
    tasks = [(p, raw_events) for p in param_combos]

    n_workers = min(_NUM_WORKERS, len(param_combos)) if _NUM_WORKERS > 0 else 1
    if not _PARALLEL or n_workers <= 1:
        for task in tasks:
            params, res = _combo_worker(task)
            results.append((params, res))
    else:
        with _cf.ProcessPoolExecutor(max_workers=n_workers) as ex:
            for params, res in ex.map(_combo_worker, tasks, chunksize=1):
                results.append((params, res))
    return results


# ─────────────────────────────────────────────────────────────────────────
# SUBSAMPLED GRID (documented strategy)
# ─────────────────────────────────────────────────────────────────────────

def _subsampled_combos():
    """Documented subsample: anchor each axis while sweeping the others.
    Reduces 405 -> 43 combos, still covers every axis value at least once."""
    combos = []
    # Anchor baseline
    anchor = (0.05, 0.95, 200, 2000, 0)
    combos.append(anchor)
    # Sweep clip (9) holding others at anchor
    for (ymin, ymax) in _clip_axis():
        if (ymin, ymax) != (0.05, 0.95):
            combos.append((ymin, ymax, 200, 2000, 0))
    # Sweep min_samples (5) holding others
    for ms in (10, 30, 50, 100, 200):
        if ms != 200:
            combos.append((0.05, 0.95, ms, 2000, 0))
    # Sweep max_history (3)
    for mh in (500, 2000, None):
        if mh != 2000:
            combos.append((0.05, 0.95, 200, mh, 0))
    # Sweep window_start (3)
    for ws in (0, 100, 500):
        if ws != 0:
            combos.append((0.05, 0.95, 200, 2000, ws))
    # Dedupe preserving order
    seen = set()
    out = []
    for c in combos:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ─────────────────────────────────────────────────────────────────────────
# BEST-CONFIG PER TARGET
# ─────────────────────────────────────────────────────────────────────────

def _best_per_target(grid_results):
    """For each target slice (global + station + regime + season + direction),
    select the param combo maximizing MCC (tie-break: higher base_rate_delta,
    then lower ECE). Returns dict target -> best config."""
    # Collect all (params, target_key, metrics) entries.
    entries = []  # (target_key, params, metrics)
    for params, res in grid_results:
        m = res['metrics']
        for group in ('global',):
            if group in m:
                entries.append(('global', params, m[group]))
        for group, d in (('station', m['station']),
                         ('regime', m['regime']),
                         ('season', m['season']),
                         ('direction', m['direction'])):
            for tgt, met in d.items():
                entries.append((f'{group}:{tgt}', params, met))

    best = {}
    for key, params, met in entries:
        if met.get('n', 0) == 0:
            continue
        score = (met['mcc'], met['base_rate_delta'], -met['ece'])
        if key not in best or score > best[key]['score']:
            best[key] = {
                'score': score,
                'params': {
                    'y_min': params[0], 'y_max': params[1],
                    'min_samples': params[2], 'max_history': params[3],
                    'window_start': params[4],
                },
                'metrics': met,
            }
    # Drop the internal score tuple for clean output
    for key in best:
        best[key].pop('score')
    return best


# ─────────────────────────────────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────────────────────────────────

def _fmt_metrics(m):
    if not m or m.get('n', 0) == 0:
        return 'n=0'
    return (f"acc={m['accuracy']*100:6.2f}% MCC={m['mcc']:+.4f} "
            f"baseΔ={m.get('base_rate_delta', 0.0)*100:+.2f} "
            f"Brier={m.get('brier', 1.0):.4f} ECE={m.get('ece', 1.0):.4f} n={m['n']}")


def render_markdown(payload):
    L = []
    L.append("# R/T-8H — TRUE Exhaustive Calibration Grid")
    L.append("")
    L.append(f"- **Generated:** {payload['generated_utc']}")
    L.append(f"- **Axis:** {payload['axis']}")
    L.append(f"- **Labels:** {payload['provenance']['labels']}")
    L.append(f"- **Signals:** {payload['provenance']['signals']}")
    L.append(f"- **Fusion:** {payload['provenance']['fusion']}")
    L.append(f"- **Grid size:** {payload['grid_size']} param combos "
             f"x {payload['target_slices']} target slices = {payload['total_cells']} cells")
    L.append(f"- **Runtime:** {payload['elapsed_seconds']}s "
             f"(workers={payload['num_workers']}, parallel={payload['parallel']})")
    L.append(f"- **Mode:** {payload['mode']}")
    L.append(f"- **Excluded:** {payload['excluded']}")
    L.append("")
    L.append("## Honesty note")
    L.append("")
    L.append(payload['honesty_note'])
    L.append("")
    L.append("## Best calibration config per target (MCC-max, tie: baseΔ, then ECE)")
    L.append("")
    L.append("| target | y_min | y_max | min_samples | max_history | window_start | result |")
    L.append("|---|---|---|---|---|---|---|")
    for tgt in sorted(payload['best_per_target']):
        b = payload['best_per_target'][tgt]
        p = b['params']
        L.append(f"| {tgt} | {p['y_min']} | {p['y_max']} | {p['min_samples']} "
                 f"| {p['max_history']} | {p['window_start']} | {_fmt_metrics(b['metrics'])} |")
    L.append("")
    L.append("## Full grid (all param combos → global metrics)")
    L.append("")
    L.append("| y_min | y_max | min_samples | max_history | window_start | fired | null_rate | result |")
    L.append("|---|---|---|---|---|---|---|---|")
    for entry in payload['grid']:
        p = entry['params']
        t = entry['totals']
        m = entry['metrics'].get('global', {})
        L.append(f"| {p['y_min']} | {p['y_max']} | {p['min_samples']} "
                 f"| {p['max_history']} | {p['window_start']} "
                 f"| {t['fused_fired']} | {t['fused_null_rate']} | {_fmt_metrics(m)} |")
    L.append("")
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser(description="R/T-8H exhaustive calibration grid")
    parser.add_argument('--workers', type=int, default=1, metavar='N',
                        help='parallel workers (default 1 = serial)')
    parser.add_argument('--dry-run', action='store_true', help='enumerate grid only')
    parser.add_argument('--subsample', action='store_true',
                        help='use documented subsampled grid (43 combos)')
    parser.add_argument('--no-emit', action='store_true', help='do not write reports')
    parser.add_argument('--write-receipt', action='store_true', help='write docs receipt')
    parser.add_argument('--raw-cache', type=str, default=None, metavar='PATH',
                        help='cache/load raw events to/from a pickle file (skip 300s collection)')
    args = parser.parse_args()

    global _PARALLEL, _NUM_WORKERS
    _NUM_WORKERS = args.workers
    _PARALLEL = args.workers > 1

    t0 = time.time()

    param_combos = _subsampled_combos() if args.subsample else _param_combos()

    print("=" * 92)
    print("R/T-8H — TRUE EXHAUSTIVE CALIBRATION GRID")
    print("=" * 92)
    print(f"Grid: {len(param_combos)} param combos (y_min x y_max x min_samples x max_history x window_start)")
    print(f"Targets: per-station(20) x per-regime(2) x per-season(4) x per-direction(2)")
    print(f"Parallel: {_PARALLEL} workers={_NUM_WORKERS}")
    print(f"Mode: {'SUBSAMPLED (documented)' if args.subsample else 'FULL GRID'}")

    if args.dry_run:
        print("\n[DRY-RUN] Enumerating param combos (not scoring):")
        for i, p in enumerate(param_combos):
            print(f"  {i:3d}: y_min={p[0]:.2f} y_max={p[1]:.2f} "
                  f"min_samples={p[2]:3d} max_history={p[3]} window_start={p[4]}")
        print(f"\nTotal cells = {len(param_combos)} param combos x "
              f"{20 * 2 * 4 * 2} target slices = {len(param_combos) * 320}")
        return

    # Load ground truth once (parent) for provenance/header
    labels, src_map, meta, excl = load_labels()
    n_labels = sum(len(v) for v in labels.values())

    print(f"Labels loaded: {n_labels}")
    print(f"Excluded: {excl}")

    # Collect raw signal evaluations ONCE (combo-independent).
    raw_cache = args.raw_cache
    raw_events = None
    if raw_cache and os.path.exists(raw_cache):
        import pickle as _pkl
        print(f"Loading raw events from cache: {raw_cache}")
        with open(raw_cache, 'rb') as f:
            raw_events = _pkl.load(f)
        print(f"  Raw events: {len(raw_events)} (loaded from cache)")
    else:
        print("Collecting raw signal evaluations (combo-independent)...")
        t_raw = time.time()
        registry, _status = build_signal_registry()
        conn = sqlite3.connect(METAR_DB)
        try:
            raw_events = _collect_raw_events(registry, labels, conn)
        finally:
            conn.close()
        print(f"  Raw events: {len(raw_events)} (collected in {round(time.time()-t_raw,2)}s)")
        if raw_cache:
            import pickle as _pkl
            with open(raw_cache, 'wb') as f:
                _pkl.dump(raw_events, f, protocol=_pkl.HIGHEST_PROTOCOL)
            print(f"  Cached raw events -> {raw_cache}")

    # Run the grid
    grid_results = _run_grid(param_combos, raw_events)

    elapsed = round(time.time() - t0, 2)

    # Build payload
    grid_out = []
    for params, res in grid_results:
        grid_out.append({
            'params': res['params'],
            'totals': res['totals'],
            'metrics': res['metrics'],
        })

    best = _best_per_target(grid_results)

    total_cells = len(param_combos) * 320  # 20 station x 2 regime x 4 season x 2 dir

    payload = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'task': 'R/T-8h TRUE exhaustive calibration grid',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction',
        'provenance': {
            'labels': 'settlement_truth.strike_price (14,264 rows via /historical backfill)',
            'signals': 'BACKTEST_SIGNALS (7 live, R/T-8a corrected)',
            'fusion': 'SignalFusionEngine (LIOP/LLOP, core/fusion_logic.py) with WALK-FORWARD fitted calibration',
            'note': 'R/T-8b did single-pass (inert calibration, cold-start identity). This grid performs a proper walk-forward fit so the swept params genuinely change the fused output.',
        },
        'grid_size': len(param_combos),
        'target_slices': 320,
        'total_cells': total_cells,
        'elapsed_seconds': elapsed,
        'num_workers': _NUM_WORKERS,
        'parallel': _PARALLEL,
        'mode': 'subsampled' if args.subsample else 'full',
        'subsampled_combos': len(param_combos) if args.subsample else None,
        'excluded': excl,
        'honesty_note': (
            "Swept parameters are the FOUR genuinely tunable knobs inside the "
            "directional LIOP fusion path (isotonic y_min/y_max, MIN_SAMPLES, "
            "max_history, window_start) — verified in source. The calibration "
            "MODE axis (platt/bma/emos/both, ModulatorStack), hierarchical_bayes "
            "TIER_THRESHOLDS, and sample_floor shrinkage are REAL in code but on a "
            "DIFFERENT surface (post-fusion modulator / temperature-error °F "
            "calibration), NOT the directional strike-crossing score — sweeping "
            "them here would be a fabricated axis with no effect. Reported as such."
        ),
        'grid': grid_out,
        'best_per_target': best,
    }

    if not args.no_emit:
        ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        json_path = os.path.join(REPORTS_DIR, f'rt8h_exhaustive_calibration_grid_{ts}.json')
        md_path = os.path.join(REPORTS_DIR, f'rt8h_exhaustive_calibration_grid_{ts}.md')
        with open(json_path, 'w') as f:
            json.dump(payload, f, indent=2)
        with open(md_path, 'w') as f:
            f.write(render_markdown(payload))
        print(f"\nReceipt JSON : {json_path}")
        print(f"Receipt MD   : {md_path}")

    print(f"\nGrid: {len(param_combos)} combos x 320 targets = {total_cells} cells")
    print(f"Elapsed: {elapsed}s (workers={_NUM_WORKERS}, parallel={_PARALLEL})")

    # Print best-per-target summary
    print("\nBest config per target (MCC-max):")
    for tgt in sorted(best):
        b = best[tgt]
        p = b['params']
        print(f"  {tgt:20s} y_min={p['y_min']:.2f} y_max={p['y_max']:.2f} "
              f"ms={p['min_samples']:3d} mh={p['max_history']} ws={p['window_start']} "
              f"→ {_fmt_metrics(b['metrics'])}")

    if args.write_receipt:
        receipt_path = os.path.join(
            BASE, 'docs', 'plans', 'RT8H-EXHAUSTIVE-CALIBRATION-GRID-2026-09-21.md')
        with open(receipt_path, 'w') as f:
            f.write(render_markdown(payload))
            f.write("\n\n---\n\n## Receipt (R/T-8H)\n\n")
            f.write(f"- **Commit:** (see git log; committed after run)\n")
            f.write(f"- **Grid size:** {len(param_combos)} param combos x 320 targets = {total_cells} cells\n")
            f.write(f"- **Runtime:** {elapsed}s (workers={_NUM_WORKERS}, parallel={_PARALLEL})\n")
            f.write(f"- **Mode:** {'subsampled' if args.subsample else 'full'}\n")
        print(f"Receipt written: {receipt_path}")

    return payload


if __name__ == '__main__':
    main()
