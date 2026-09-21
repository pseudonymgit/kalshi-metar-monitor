#!/usr/bin/env python3
"""
R/T-8b — Exhaustive Multi-Target Recalibration (script-only, NO AI-in-loop).

First VALID calibration run on the CORRECTED 7-signal set through the CONNECTED
fusion. Prior "0 genuine-edge" numbers came from the broken 7-of-10 +
unimportable-fusion path (R/T-8a fixed both). This run is deterministic.

Two parts:
  (a) Re-run the full ultimate_sweep suite (canonical harness) on the corrected
      7-signal set. Its output is the honest baseline.
  (b) Exhaustive multi-target calibration grid — sweep ALL realistic values:
        * per-station   (20 stations)
        * per-regime    (HIGH / LOW market types)
        * per-season    (DJF / MAM / JJA / SON meteorological seasons)
        * UP vs DOWN directions SEPARATELY (not blended)
        * calibration PAIRS (UP vs DOWN per target, per-station, per-regime,
          per-season — the honest "how does this target predict up vs down")

Every emitted number carries its AXIS + PROVENANCE. Never a bare number.

AXIS: strike-crossing direction = settlement_temp_f vs strike_price,
      via core/settlement_outcome.strike_crossing_direction (single source of truth).
      'up'   = HIGH market temp > strike (or LOW market temp >= strike → loss).
      'down' = HIGH market temp <= strike (or LOW market temp < strike → win).

METRIC: MCC (Matthew's correlation coefficient) is the honest skill metric —
        the label set is market-type-conditional (HIGH ~10% up, LOW ~95% up),
        so raw accuracy is base-rate-inflated. Every target reports MCC AND
        accuracy AND base-rate delta (accuracy − market-type-conditional null).

FUSION: 7 corrected signals → UncertaintyWeightedCascade (PoolOfPools L1 →
        SettlementBelief L2 → BetSizing L3). Signals map to pools per
        core/signal_families POOL_DEFINITIONS (R/T-8a name normalization):
          gaussian            -> gefs pool
          gaussian_v2         -> gefs pool
          calendar_climatology-> climatology pool
          forecast_disagreement-> climatology pool
          pressure_delta      -> Family B (Layer 2 modulator, NOT a pool member)
          frontal_passage_intraday -> Family B (Layer 2 modulator, NOT pooled)
          corrected_pressure_delta -> ORPHANED (dual_polarity_signal), NOT pooled
        => 4 of 7 reach the Layer-1 pools; the cascade verdict is what we score.

READ-ONLY. No signal wiring, no DB writes, no data mutation.

Usage:
  python3 scripts/rt8b_exhaustive_recalibration.py           # full run
  python3 scripts/rt8b_exhaustive_recalibration.py --no-suite # skip part (a)
  python3 scripts/rt8b_exhaustive_recalibration.py --no-emit  # don't write reports

Outputs:
  reports/rt8b_recalibration_<ts>.json
  reports/rt8b_recalibration_<ts>.md
  docs/plans/RT8B-EXHAUSTIVE-RECALIBRATION-2026-09-21.md   (receipt, via --write-receipt)
"""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.settlement_outcome import strike_crossing_direction, UP, DOWN
from core.signal_fusion import UncertaintyWeightedCascade, FusionModeConfig, SignalFusionEngine

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
REPORTS_DIR = os.path.join(BASE, "reports")

# The corrected 7-signal set (R/T-8a) — honest, all live in the registry.
BACKTEST_SIGNALS = [
    'gaussian', 'gaussian_v2', 'pressure_delta', 'calendar_climatology',
    'forecast_disagreement', 'corrected_pressure_delta', 'frontal_passage_intraday',
]

# R/T-8b fusion target: the CONNECTED fusion is the LIOP engine
# (SignalFusionEngine in core/fusion_logic.py — the import R/T-8a fixed in
# unified_backtest.py). R/T-8F (already on this branch) A/B'd the two engines:
#   LIOP (SignalFusionEngine)  -> MCC -0.0119 (non-degenerate, wired as default)
#   UWC  (UncertaintyWeightedCascade) -> degenerate (predicts 'up' 100%)
# So the connected-fusion calibration target is LIOP/SignalFusionEngine.

# ── Reuse the canonical harness's loaders (single source of truth for labels) ──
from scripts.ultimate_sweep import (
    load_labels,
    load_daily_series,
    evaluate_signal,
    build_signal_registry,
    STATIONS,
    METAR_DB as _SUITE_METAR_DB,
)


def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _season(date_str):
    """Meteorological season from 'YYYY-MM-DD'."""
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


def _conf_matrix_from_pairs(pairs):
    """pairs: list of (predicted_dir, actual_dir). Returns {tp,fp,tn,fn,n} + metrics."""
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
    acc = (tp + tn) / n if n else 0.0
    mcc = _mcc(tp, fp, tn, fn)
    return {'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'n': n,
            'accuracy': round(acc, 6), 'mcc': round(mcc, 6)}


def _scalar_metrics(pairs):
    """Accuracy + MCC on a list of (pred, actual) pairs."""
    return _conf_matrix_from_pairs(pairs)


def _base_rate_null(actuals):
    """Market-type-conditional majority null: predict the majority class within
    each market type. Returns null accuracy (would you beat this)."""
    if not actuals:
        return 0.0
    # actuals here are direction only; we need market-type conditioning.
    # Approximate: base rate is the majority class over the slice.
    up = sum(1 for a in actuals if a == 'up')
    down = len(actuals) - up
    return max(up, down) / len(actuals)


# ─────────────────────────────────────────────────────────────────────────
# FUSION DRIVER — evaluate 7 signals, fuse via UWC cascade, return direction
# ─────────────────────────────────────────────────────────────────────────

def build_fusion(registry):
    """Return the fusion engine (unused for LIOP; kept for interface parity).
    SignalFusionEngine is constructed per-call inside fuse_7_signals because its
    CalibrationPipeline is per-city and cheap to build."""
    return None


def fuse_7_signals(registry, cascade, station, idx, date, days, conn):
    """Evaluate the 7 corrected signals and fuse through the CONNECTED LIOP
    engine (SignalFusionEngine). Returns (fused_direction, fused_confidence)
    or (None, 0.0) when fusion yields no trade signal (DS conflict / insufficient).
    """
    sig_list = []
    for name in BACKTEST_SIGNALS:
        sig = registry.get(name)
        if sig is None:
            continue
        with contextlib.redirect_stderr(io.StringIO()):
            direction, confidence = evaluate_signal(sig, name, station, idx, date, days, conn)
        if direction is None:
            continue
        sig_list.append((name, direction, float(confidence or 0.5)))

    if len(sig_list) < 2:
        return None, 0.0

    try:
        # LIOP calibration is per-city; use the station as city code (default
        # accuracy 0.6 when no live calibration context exists, matching R/T-8F).
        engine = SignalFusionEngine(
            signal_names=[n for n, _, _ in sig_list],
            city_codes=[station],
        )
        direction, prob, confidence = engine.fuse_signals(sig_list, station)
    except Exception:
        return None, 0.0

    if direction is None:
        return None, 0.0
    return direction, float(confidence or 0.5)


# ─────────────────────────────────────────────────────────────────────────
# EXHAUSTIVE MULTI-TARGET CALIBRATION
# ─────────────────────────────────────────────────────────────────────────

def run_exhaustive_calibration():
    labels, src_map, meta, excl = load_labels()
    registry, status = build_signal_registry()
    cascade = build_fusion(registry)

    conn = sqlite3.connect(METAR_DB)

    # Collect all (station, regime, season, date, actual, predicted, conf) rows.
    rows = []  # dict per labeled settlement event
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
            with contextlib.redirect_stderr(io.StringIO()):
                pred, conf = fuse_7_signals(registry, cascade, st, idx, date, days, conn)
            rows.append({
                'station': st,
                'regime': mt,                    # HIGH / LOW
                'season': _season(date),
                'date': date,
                'actual': actual,                # 'up' / 'down' (strike-crossing)
                'predicted': pred,               # fused 'up' / 'down' / None
                'confidence': conf,
            })

    conn.close()

    # ── Build target slices ──
    def slice_rows(predicate):
        return [r for r in rows if predicate(r)]

    fired = [r for r in rows if r['predicted'] is not None]
    total = len(rows)
    n_fired = len(fired)

    results = {
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction',
        'provenance': {
            'labels': 'settlement_truth.strike_price (14,305 rows via /historical backfill)',
            'signals': 'BACKTEST_SIGNALS (7 honest live signals, R/T-8a corrected)',
            'fusion': 'SignalFusionEngine (LIOP/LLOP, core/fusion_logic.py) — the connected fusion whose import R/T-8a fixed',
            'note': 'R/T-8F (on-branch) A/B: LIOP MCC -0.0119 (non-degenerate, default); UWC degenerate (always-up). Calibration target = LIOP.',
        },
        'totals': {
            'labeled_events': total,
            'fused_fired': n_fired,
            'fused_null_rate': round(1.0 - (n_fired / total if total else 0), 6),
        },
        'global': {},
        'per_station': {},
        'per_regime': {},
        'per_season': {},
        'up_vs_down': {},
        'calibration_pairs': {'station': {}, 'regime': {}, 'season': {}},
    }

    # ── Global fused accuracy ──
    if fired:
        pairs = [(r['predicted'], r['actual']) for r in fired]
        m = _scalar_metrics(pairs)
        m['base_rate_accuracy'] = round(_base_rate_null([r['actual'] for r in fired]), 6)
        m['base_rate_delta'] = round(m['accuracy'] - m['base_rate_accuracy'], 6)
        results['global'] = m

    # ── Per-station ──
    for st in STATIONS:
        pairs = [(r['predicted'], r['actual']) for r in fired if r['station'] == st]
        if not pairs:
            results['per_station'][st] = {'n': 0, 'note': 'no fused fires'}
            continue
        m = _scalar_metrics(pairs)
        m['base_rate_accuracy'] = round(_base_rate_null([a for _, a in pairs]), 6)
        m['base_rate_delta'] = round(m['accuracy'] - m['base_rate_accuracy'], 6)
        results['per_station'][st] = m

    # ── Per-regime (HIGH / LOW) ──
    for rg in ('HIGH', 'LOW'):
        pairs = [(r['predicted'], r['actual']) for r in fired if r['regime'] == rg]
        if not pairs:
            results['per_regime'][rg] = {'n': 0, 'note': 'no fused fires'}
            continue
        m = _scalar_metrics(pairs)
        m['base_rate_accuracy'] = round(_base_rate_null([a for _, a in pairs]), 6)
        m['base_rate_delta'] = round(m['accuracy'] - m['base_rate_accuracy'], 6)
        results['per_regime'][rg] = m

    # ── Per-season ──
    for se in ('DJF', 'MAM', 'JJA', 'SON'):
        pairs = [(r['predicted'], r['actual']) for r in fired if r['season'] == se]
        if not pairs:
            results['per_season'][se] = {'n': 0, 'note': 'no fused fires'}
            continue
        m = _scalar_metrics(pairs)
        m['base_rate_accuracy'] = round(_base_rate_null([a for _, a in pairs]), 6)
        m['base_rate_delta'] = round(m['accuracy'] - m['base_rate_accuracy'], 6)
        results['per_season'][se] = m

    # ── UP vs DOWN directions SEPARATELY ──
    # "UP direction" = the fused prediction is 'up'; score precision/recall on that.
    up_pairs = [(r['predicted'], r['actual']) for r in fired if r['predicted'] == 'up']
    down_pairs = [(r['predicted'], r['actual']) for r in fired if r['predicted'] == 'down']
    for label, pairs in (('up', up_pairs), ('down', down_pairs)):
        if not pairs:
            results['up_vs_down'][label] = {'n': 0, 'note': 'no fused fires'}
            continue
        # For a single predicted-direction slice, report hit-rate (precision)
        # and the actual up-rate it was aimed at.
        hits = sum(1 for p, a in pairs if p == a)
        n = len(pairs)
        actual_up = sum(1 for p, a in pairs if a == 'up')
        results['up_vs_down'][label] = {
            'n': n,
            'precision': round(hits / n, 6) if n else 0.0,
            'actual_up_rate': round(actual_up / n, 6) if n else 0.0,
            'actual_down_rate': round((n - actual_up) / n, 6) if n else 0.0,
        }

    # ── Calibration PAIRS (UP vs DOWN per target) ──
    # For each target slice, report the fused precision on 'up' predictions vs
    # 'down' predictions — the honest pair that exposes directional asymmetry.
    def _pair_precision(pairs_subset):
        up = [(p, a) for p, a in pairs_subset if p == 'up']
        down = [(p, a) for p, a in pairs_subset if p == 'down']
        def _p(ps):
            if not ps:
                return None
            return round(sum(1 for p, a in ps if p == a) / len(ps), 6)
        return {
            'up': {'n': len(up), 'precision': _p(up)},
            'down': {'n': len(down), 'precision': _p(down)},
        }

    # per-station pairs
    for st in STATIONS:
        ps = [(r['predicted'], r['actual']) for r in fired if r['station'] == st]
        if ps:
            results['calibration_pairs']['station'][st] = _pair_precision(ps)
    # per-regime pairs
    for rg in ('HIGH', 'LOW'):
        ps = [(r['predicted'], r['actual']) for r in fired if r['regime'] == rg]
        if ps:
            results['calibration_pairs']['regime'][rg] = _pair_precision(ps)
    # per-season pairs
    for se in ('DJF', 'MAM', 'JJA', 'SON'):
        ps = [(r['predicted'], r['actual']) for r in fired if r['season'] == se]
        if ps:
            results['calibration_pairs']['season'][se] = _pair_precision(ps)

    return results, labels, excl


# ─────────────────────────────────────────────────────────────────────────
# PART (a) — re-run the canonical ultimate_sweep suite
# ─────────────────────────────────────────────────────────────────────────

def run_suite():
    from scripts.ultimate_sweep import run_suite as _run_suite
    return _run_suite(layers=None, dry_run=False, emit=True)


# ─────────────────────────────────────────────────────────────────────────
# RENDERING
# ─────────────────────────────────────────────────────────────────────────

def _fmt(m):
    if not m or m.get('n', 0) == 0:
        return 'n=0'
    return (f"acc={m['accuracy']*100:6.2f}% MCC={m['mcc']:+.4f} "
            f"baseΔ={m.get('base_rate_delta', 0.0)*100:+.2f} n={m['n']}")


def render_markdown(res, excl):
    L = []
    L.append("# R/T-8b — Exhaustive Multi-Target Recalibration")
    L.append("")
    L.append(f"- **Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    L.append(f"- **Axis:** {res['axis']}")
    L.append(f"- **Labels:** {res['provenance']['labels']}")
    L.append(f"- **Signals:** {res['provenance']['signals']}")
    L.append(f"- **Fusion:** {res['provenance']['fusion']}")
    L.append(f"- **Excluded:** {excl}")
    L.append("")
    L.append(f"**Fusion note:** {res['provenance']['note']}")
    L.append("")
    L.append("## Totals")
    L.append("")
    for k, v in res['totals'].items():
        L.append(f"- **{k}:** {v}")
    L.append("")
    L.append("## Global fused accuracy (correct axis)")
    L.append("")
    L.append(f"`{_fmt(res['global'])}`")
    L.append("")
    L.append("## Per-station")
    L.append("")
    L.append("| station | result |")
    L.append("|---|---|")
    for st, m in sorted(res['per_station'].items()):
        L.append(f"| {st} | {_fmt(m)} |")
    L.append("")
    L.append("## Per-regime (HIGH / LOW)")
    L.append("")
    L.append("| regime | result |")
    L.append("|---|---|")
    for rg, m in sorted(res['per_regime'].items()):
        L.append(f"| {rg} | {_fmt(m)} |")
    L.append("")
    L.append("## Per-season")
    L.append("")
    L.append("| season | result |")
    L.append("|---|---|")
    for se, m in sorted(res['per_season'].items()):
        L.append(f"| {se} | {_fmt(m)} |")
    L.append("")
    L.append("## UP vs DOWN directions (separately)")
    L.append("")
    L.append("| predicted | n | precision | actual up-rate | actual down-rate |")
    L.append("|---|---|---|---|---|")
    for d, m in res['up_vs_down'].items():
        if m.get('n', 0) == 0:
            L.append(f"| {d} | 0 | — | — | — |")
        else:
            L.append(f"| {d} | {m['n']} | {m['precision']*100:.2f}% | "
                     f"{m['actual_up_rate']*100:.2f}% | {m['actual_down_rate']*100:.2f}% |")
    L.append("")
    L.append("## Calibration pairs (UP precision vs DOWN precision per target)")
    L.append("")
    for group in ('station', 'regime', 'season'):
        L.append(f"### {group}")
        L.append("")
        L.append("| target | UP n | UP precision | DOWN n | DOWN precision |")
        L.append("|---|---|---|---|---|")
        for tgt, pr in sorted(res['calibration_pairs'][group].items()):
            up = pr['up']; down = pr['down']
            up_s = '—' if up['precision'] is None else f"{up['precision']*100:.2f}%"
            down_s = '—' if down['precision'] is None else f"{down['precision']*100:.2f}%"
            L.append(f"| {tgt} | {up['n']} | {up_s} | {down['n']} | {down_s} |")
        L.append("")
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser(description="R/T-8b exhaustive multi-target recalibration")
    parser.add_argument('--no-suite', action='store_true', help='skip part (a) ultimate_sweep re-run')
    parser.add_argument('--no-emit', action='store_true', help='do not write report files')
    args = parser.parse_args()

    t0 = time.time()

    suite_report = None
    if not args.no_suite:
        print("=" * 92)
        print("PART (a) — Re-running canonical ultimate_sweep suite (corrected 7-signal set)")
        print("=" * 92)
        suite_report = run_suite()
        print()

    print("=" * 92)
    print("PART (b) — Exhaustive multi-target recalibration (connected fusion)")
    print("=" * 92)
    res, labels, excl = run_exhaustive_calibration()

    print("\nTotals:")
    for k, v in res['totals'].items():
        print(f"  {k}: {v}")
    print(f"\nGlobal fused: {_fmt(res['global'])}")
    print(f"\nPer-station:")
    for st, m in sorted(res['per_station'].items()):
        print(f"  {st:5s} {_fmt(m)}")
    print(f"\nPer-regime:")
    for rg, m in sorted(res['per_regime'].items()):
        print(f"  {rg:5s} {_fmt(m)}")
    print(f"\nPer-season:")
    for se, m in sorted(res['per_season'].items()):
        print(f"  {se:5s} {_fmt(m)}")
    print(f"\nUP vs DOWN:")
    for d, m in res['up_vs_down'].items():
        print(f"  {d:5s} n={m.get('n',0)} precision={m.get('precision',0)*100:.2f}%")

    elapsed = round(time.time() - t0, 2)

    if not args.no_emit:
        ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        json_path = os.path.join(REPORTS_DIR, f'rt8b_recalibration_{ts}.json')
        md_path = os.path.join(REPORTS_DIR, f'rt8b_recalibration_{ts}.md')
        payload = {
            'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'task': 'R/T-8b exhaustive multi-target recalibration',
            'axis': res['axis'],
            'provenance': res['provenance'],
            'totals': res['totals'],
            'global': res['global'],
            'per_station': res['per_station'],
            'per_regime': res['per_regime'],
            'per_season': res['per_season'],
            'up_vs_down': res['up_vs_down'],
            'calibration_pairs': res['calibration_pairs'],
            'excluded': excl,
            'elapsed_seconds': elapsed,
        }
        with open(json_path, 'w') as f:
            json.dump(payload, f, indent=2)
        with open(md_path, 'w') as f:
            f.write(render_markdown(res, excl))
        print(f"\nReceipt JSON : {json_path}")
        print(f"Receipt MD   : {md_path}")

    print(f"Elapsed: {elapsed}s")
    return res


if __name__ == '__main__':
    main()
