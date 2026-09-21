#!/usr/bin/env python3
"""
R/T-8F — Fusion wiring A/B (script-only, NO AI-in-loop, deterministic).

Builds BOTH prediction paths and A/B tests them on the CORRECTED 7-signal set
+ corrected ground-truth axis (strike_crossing_direction), then emits a
data-driven verdict on which produces better calibrated prediction accuracy.

  Path A (baseline): the agreement-gate stack in core/paper_trading_engine.py
      (adaptive_thresholds -> agreement_gate -> station_skill_gate ->
       spatial_coherence -> ensemble_diversity -> position_sizing).
      Decision = N-of-M agreement vote over the gated signal set.

  Path B: LIOP/UWC pool-of-pools fusion wired into the live path
      (core/signal_fusion.py SignalFusionEngine = flat 4-layer Log-Odds Linear
       Opinion Pool; UncertaintyWeightedCascade = UWC). Now importable after
       R/T-8a fixed import + name-mapping bugs.

GROUND-TRUTH AXIS: strike_crossing_direction (settlement_temp_f vs strike_price),
NOT day-over-day warming (that was the conflation bug this axis exists to fix).

7-SIGNAL CORRECTED SET:
  gaussian, gaussian_v2, pressure_delta, calendar_climatology,
  forecast_disagreement, corrected_pressure_delta, frontal_passage_intraday.

METRIC DISCIPLINE: every number carries axis + provenance. Lead with MCC
(imbalance-immune), then ECE, Brier, baseΔ, directional accuracy, Sharpe.

READ-ONLY. No data mutation, no DB writes. No merge (merge is Dan's).

Usage:
  python3 scripts/rt8f_fusion_ab.py [--rebuild]
"""

import argparse
import contextlib
import io
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import strike_crossing_direction

import scripts.ultimate_suite as suite

BASE = suite.BASE
METAR_DB = suite.METAR_DB
REPORTS_DIR = suite.REPORTS_DIR

# Fusion engines (Path B)
from core.signal_fusion import (
    SignalFusionEngine,
    UncertaintyWeightedCascade,
    FusionModeConfig,
)

# Path A gate (the agreement-gate stack)
from core.agreement_gate import SimpleAgreementChecker

# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

CORRECTED_7 = [
    "gaussian",
    "gaussian_v2",
    "pressure_delta",
    "calendar_climatology",
    "forecast_disagreement",
    "corrected_pressure_delta",
    "frontal_passage_intraday",
]

# Path A agreement-gate threshold (matches paper_trading_engine default env
# AGREEMENT_THRESHOLD=2).
AGREEMENT_N_REQUIRED = int(os.getenv("AGREEMENT_THRESHOLD", "2"))

CACHE_PATH = os.path.join(REPORTS_DIR, 'rt2b_prediction_matrix_cache.json')

AXIS = "strike-crossing (settlement_temp_f vs strike_price) -> strike_crossing_direction"


# ══════════════════════════════════════════════════════════════════════
# Metrics (every number carries axis + provenance)
# ══════════════════════════════════════════════════════════════════════

def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _sharpe(returns):
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    return 0.0 if sd == 0 else mean / sd * (252 ** 0.5)


def _brier(prob_actual_pairs):
    """Brier = mean((outcome - p)^2), outcome=1 if predicted class correct else 0,
    p = confidence in predicted class (matches core/calibrate_all_signals.compute_brier_score)."""
    if not prob_actual_pairs:
        return None
    total = 0.0
    for p, correct in prob_actual_pairs:
        outcome = 1.0 if correct else 0.0
        total += (outcome - p) ** 2
    return total / len(prob_actual_pairs)


def _ece(prob_actual_pairs, n_bins=10):
    """ECE on P(predicted class) vs observed correctness. Lower = better."""
    if not prob_actual_pairs:
        return None
    bins = [[] for _ in range(n_bins)]
    for p, correct in prob_actual_pairs:
        b = min(int(p * n_bins), n_bins - 1)
        bins[b].append((p, 1.0 if correct else 0.0))
    ece = 0.0
    for b in bins:
        if not b:
            continue
        mc = sum(p for p, _ in b) / len(b)
        ma = sum(y for _, y in b) / len(b)
        ece += (len(b) / len(prob_actual_pairs)) * abs(ma - mc)
    return ece


def metrics(preds):
    """preds = list of (predicted_dir, actual_dir, conf_p).
    conf_p = P(predicted class being correct). Returns dict with axis+provenance."""
    tp = fp = tn = fn = 0
    returns = []
    calib = []  # (p_predicted_class, correct_bool)
    for pred, actual, conf in preds:
        if pred == 'up':
            p = conf
            if actual == 'up':
                tp += 1
            else:
                fp += 1
        else:
            p = 1.0 - conf  # P(down) as the predicted class prob
            if actual == 'down':
                tn += 1
            else:
                fn += 1
        returns.append(1.0 if pred == actual else -1.0)
        calib.append((max(0.01, min(0.99, p)), pred == actual))

    total = tp + fp + tn + fn
    if total == 0:
        return None

    n_up_actual = tp + fn
    n_down_actual = tn + fp
    majority_rate = max(n_up_actual, n_down_actual) / total  # always-predict-majority acc
    directional_acc = (tp + tn) / total
    base_delta = directional_acc - majority_rate  # accuracy above majority baseline

    return {
        'n': total,
        'directional_accuracy': directional_acc,
        'base_delta': base_delta,
        'majority_baseline': majority_rate,
        'mcc': _mcc(tp, fp, tn, fn),
        'brier': _brier(calib),
        'ece': _ece(calib),
        'sharpe': _sharpe(returns),
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'up_called': tp + fp,
        'down_called': tn + fn,
        'up_precision': (tp / (tp + fp)) if (tp + fp) else 0.0,
        'down_precision': (tn / (tn + fn)) if (tn + fn) else 0.0,
    }


# ══════════════════════════════════════════════════════════════════════
# Data loading (reuses R/T-2b cached prediction matrix)
# ══════════════════════════════════════════════════════════════════════

def load_matrix(rebuild=False):
    """Load (or rebuild) the per-row prediction matrix. Each row:
    {station, mt, date, actual, signals: {name:(dir,conf)}}."""
    if os.path.exists(CACHE_PATH) and not rebuild:
        with open(CACHE_PATH) as f:
            matrix = json.load(f)
        return matrix

    # Rebuild via the same loader as R/T-2b.
    import scripts.rt2b_fusion_additive_sweep as rt2b
    labels, meta, excl = rt2b.load_labels_backfill()
    matrix, reg = rt2b.build_prediction_matrix(labels, meta, cache_path=CACHE_PATH)
    return matrix


# ══════════════════════════════════════════════════════════════════════
# Path A: agreement-gate stack (N-of-M vote over gated signals)
# ══════════════════════════════════════════════════════════════════════

def path_a_predict(signals_7):
    """
    Path A decision on one row's 7 signals.
    Returns (direction, confidence) or (None, None) if gate fails (no trade).

    Reproduces the paper_trading_engine agreement-gate semantics:
    - Only the 7 corrected signals participate.
    - SimpleAgreementChecker.check_agreement with n_required = AGREEMENT_N_REQUIRED.
    - If >= n signals agree on a direction, that direction is the prediction.
    - Confidence = share of the 7 signals voting that direction (deterministic).
    """
    if not signals_7:
        return None, None
    votes = []
    for name, (d, c) in signals_7.items():
        votes.append((name, d))  # direction is 'up'/'down'
    # Build tuples matching SimpleAgreementChecker signature (station, mt, dir, reason)
    # We only need direction; use placeholders for station/mt.
    group = [("S", "HIGH", d, name) for name, d in votes]
    agreed = SimpleAgreementChecker.check_agreement(group, n_required=AGREEMENT_N_REQUIRED)
    if not agreed:
        return None, None
    direction = agreed[0][2].upper()
    # Confidence = fraction of the 7 signals voting the agreed direction.
    n_votes = sum(1 for _, d in votes if d.upper() == direction)
    confidence = n_votes / len(votes)
    return ('up' if direction == 'UP' else 'down'), confidence


# ══════════════════════════════════════════════════════════════════════
# Path B: LIOP / UWC fusion
# ══════════════════════════════════════════════════════════════════════

def liop_fuse(signals_7):
    """Flat 4-layer Log-Odds Linear Opinion Pool (SignalFusionEngine)."""
    if not signals_7:
        return None, None
    sig_list = [(name, d, float(c)) for name, (d, c) in signals_7.items()]
    station = "KATL"  # placeholder city code — LIOP calibration is per-city but
                      # we use default accuracy (0.6) since no live city context.
    engine = SignalFusionEngine(
        signal_names=[name for name, _, _ in sig_list],
        city_codes=[station],
    )
    direction, prob, confidence = engine.fuse_signals(sig_list, station)
    if direction is None:
        return None, None
    return direction, confidence


def uwc_fuse(signals_7):
    """Uncertainty-Weighted Cascade (pool-of-pools)."""
    if not signals_7:
        return None, None
    sig_preds = {name: (d, float(c)) for name, (d, c) in signals_7.items()}
    uwc = UncertaintyWeightedCascade(FusionModeConfig(mode='uwc', n_agreement_gate=1))
    res = uwc.fuse(signal_predictions=sig_preds)
    direction = res.get('direction')
    if direction is None:
        bc = res.get('bayesian_conf', 0.5)
        direction = 'up' if bc >= 0.5 else 'down'
    confidence = res.get('confidence', 0.5)
    return direction, confidence


def liop_flat_fuse(signals_7):
    """Pure flat LIOP (no calibration/D-S) — the canonical log-odds pool used in
    R/T-2b, for a calibration-free apples-to-apples against Path A."""
    if not signals_7:
        return None, None
    log_odds = []
    for name, (d, c) in signals_7.items():
        cc = max(0.01, min(0.99, float(c)))
        lo = math.log(cc / (1.0 - cc))
        sign = 1.0 if d == 'up' else -1.0
        log_odds.append(sign * lo)
    n = len(log_odds)
    fused_lo = sum(log_odds) / math.sqrt(n)
    p = 1.0 / (1.0 + math.exp(-fused_lo))
    p = max(0.01, min(0.99, p))
    direction = 'up' if fused_lo >= 0 else 'down'
    confidence = p if direction == 'up' else 1.0 - p
    return direction, confidence


# ══════════════════════════════════════════════════════════════════════
# Run
# ══════════════════════════════════════════════════════════════════════

def run():
    t0 = time.time()
    matrix = load_matrix(rebuild=ARGS.rebuild)
    print(f"Prediction matrix: {len(matrix)} rows (axis: {AXIS})")

    # Filter each row to the corrected 7-signal set; keep only rows where >=1 of
    # the 7 signals fires (a prediction can only be made on rows with signal info).
    rows_7 = []
    for m in matrix:
        sigs = {name: v for name, v in m['signals'].items() if name in CORRECTED_7}
        if sigs:
            m['signals_7'] = sigs
            rows_7.append(m)
    print(f"Rows with >=1 of the 7 corrected signals: {len(rows_7)}")

    # Fire counts of the 7 signals on this axis
    fire7 = Counter()
    for m in rows_7:
        fire7.update(m['signals_7'].keys())
    print("7-signal fire counts (strike-crossing axis):")
    for name in CORRECTED_7:
        print(f"  {name:<28s} {fire7.get(name, 0)}")

    # ── Baselines ──
    n_up = sum(1 for m in rows_7 if m['actual'] == 'up')
    n_down = len(rows_7) - n_up
    print(f"\nGround truth: up={n_up} down={n_down} base-up={n_up/len(rows_7)*100:.2f}%")
    majority = 'up' if n_up >= n_down else 'down'
    majority_preds = [(majority, m['actual'], 0.5) for m in rows_7]
    majority_metrics = metrics(majority_preds)
    print(f"[NULL majority={majority}] {fmt(majority_metrics)}")

    # ── Path A ──
    path_a_preds = []
    for m in rows_7:
        d, c = path_a_predict(m['signals_7'])
        if d is not None:
            path_a_preds.append((d, m['actual'], c))
    path_a_metrics = metrics(path_a_preds)
    print(f"\n[PATH A agreement-gate N>={AGREEMENT_N_REQUIRED}] {fmt(path_a_metrics)}")
    print(f"  Path A rows traded: {len(path_a_preds)} / {len(rows_7)} "
          f"({100*len(path_a_preds)/len(rows_7):.1f}%)")

    # ── Path B variants ──
    path_b = {}
    b_variants = {
        'liop_flat': liop_flat_fuse,
        'liop_engine': liop_fuse,
        'uwc': uwc_fuse,
    }
    for vname, fn in b_variants.items():
        preds = []
        for m in rows_7:
            d, c = fn(m['signals_7'])
            if d is not None:
                preds.append((d, m['actual'], c))
        path_b[vname] = metrics(preds)
        print(f"\n[PATH B {vname}] {fmt(path_b[vname])}")

    # ── Verdict ──
    # Choose Path B primary = liop_engine (the actually-wired SignalFusionEngine)
    # with liop_flat as calibration-free reference and uwc as alt.
    verdict = build_verdict(path_a_metrics, path_b, majority_metrics, fire7)

    # ── Report ──
    def ser(m):
        if m is None:
            return None
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()}

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'suite': 'R/T-8F fusion wiring A/B',
        'axis': AXIS,
        'corrected_7_signals': CORRECTED_7,
        'agreement_n_required': AGREEMENT_N_REQUIRED,
        'n_rows': len(rows_7),
        'ground_truth_distribution': {'up': n_up, 'down': n_down},
        'majority_null': ser(majority_metrics),
        'path_a_agreement_gate': ser(path_a_metrics),
        'path_a_rows_traded': len(path_a_preds),
        'path_b': {k: ser(v) for k, v in path_b.items()},
        'verdict': verdict,
        'elapsed_seconds': round(time.time() - t0, 2),
    }

    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    json_path = os.path.join(REPORTS_DIR, f'rt8f_fusion_ab_{ts}.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\n=== Receipt JSON: {json_path} ===")
    print(f"\n=== VERDICT ===\n{verdict['summary']}")
    return report, json_path


def build_verdict(path_a, path_b, majority, fire7):
    """Data-driven verdict: which path is better on MCC (primary), then ECE/Brier."""
    def get_metric(m, key, default=None):
        if m is None:
            return default
        return m.get(key)

    # Primary ranking: MCC (imbalance-immune). Secondary: directional accuracy,
    # ECE (lower better), Brier (lower better).
    candidates = {'path_a_agreement_gate': path_a}
    candidates.update({f'path_b_{k}': v for k, v in path_b.items()})

    # MCC ranking (higher better), among non-None AND non-degenerate
    # (a path that predicts a single class — up_called==0 or down_called==0 —
    # has MCC=0 by construction, not by skill; exclude it from ranking).
    def is_degenerate(m):
        return m.get('up_called', 0) == 0 or m.get('down_called', 0) == 0

    mcc_rank = sorted(
        [(k, v) for k, v in candidates.items()
         if v is not None and v.get('mcc') is not None and not is_degenerate(v)],
        key=lambda kv: kv[1]['mcc'], reverse=True,
    )
    winner_mcc = mcc_rank[0][0] if mcc_rank else None

    # ECE ranking (lower better), non-degenerate
    ece_rank = sorted(
        [(k, v) for k, v in candidates.items()
         if v is not None and v.get('ece') is not None and not is_degenerate(v)],
        key=lambda kv: kv[1]['ece'],
    )
    winner_ece = ece_rank[0][0] if ece_rank else None

    # Brier ranking (lower better), non-degenerate
    brier_rank = sorted(
        [(k, v) for k, v in candidates.items()
         if v is not None and v.get('brier') is not None and not is_degenerate(v)],
        key=lambda kv: kv[1]['brier'],
    )
    winner_brier = brier_rank[0][0] if brier_rank else None

    # Determine default wiring: Path B (liop_engine) wins if its MCC >= Path A's
    # MCC AND its Brier <= Path A's Brier (strictly better or equal on both).
    a = path_a
    b_engine = path_b.get('liop_engine')
    b_flat = path_b.get('liop_flat')

    # Use liop_engine (actually wired) as the Path B default candidate.
    b_default = b_engine if b_engine is not None else b_flat

    lines = []
    if b_default is not None and a is not None:
        mcc_better = b_default['mcc'] >= a['mcc']
        brier_better = b_default['brier'] <= a['brier']
        if mcc_better and brier_better:
            default_path = 'Path B (LIOP)'
            lines.append(f"MCC: B={b_default['mcc']:+.4f} >= A={a['mcc']:+.4f}  -> B wins")
            lines.append(f"Brier: B={b_default['brier']:.4f} <= A={a['brier']:.4f}  -> B wins")
        elif not mcc_better and not brier_better:
            default_path = 'Path A (agreement-gate)'
            lines.append(f"MCC: A={a['mcc']:+.4f} > B={b_default['mcc']:+.4f}  -> A wins")
            lines.append(f"Brier: A={a['brier']:.4f} < B={b_default['brier']:.4f}  -> A wins")
        else:
            # Mixed: MCC and Brier disagree. Prefer MCC (imbalance-immune) as tiebreak,
            # but flag the mixed result honestly.
            default_path = 'Path B (LIOP)' if mcc_better else 'Path A (agreement-gate)'
            lines.append(f"MIXED: MCC {'B' if mcc_better else 'A'} wins, Brier {'B' if brier_better else 'A'} wins")
            lines.append(f"  MCC A={a['mcc']:+.4f} vs B={b_default['mcc']:+.4f}; "
                         f"Brier A={a['brier']:.4f} vs B={b_default['brier']:.4f}")
            lines.append(f"  Tiebreak on MCC (imbalance-immune) -> {default_path}")
    else:
        default_path = 'Path A (agreement-gate)'  # safety default if B missing
        lines.append("Path B produced no valid metrics; defaulting to Path A")

    summary = (
        f"Default (behind flag, reversible): {default_path}\n"
        + "\n".join(lines)
        + f"\nMCC winner: {winner_mcc} | ECE winner: {winner_ece} | Brier winner: {winner_brier}"
    )
    return {
        'default_path': default_path,
        'mcc_winner': winner_mcc,
        'ece_winner': winner_ece,
        'brier_winner': winner_brier,
        'summary': summary,
    }


def fmt(m):
    if m is None:
        return '(no predictions)'
    return (f"n={m['n']:5d} dir-acc={m['directional_accuracy']*100:6.2f}% "
            f"baseΔ={m['base_delta']*100:+6.2f}pt MCC={m['mcc']:+6.4f} "
            f"Brier={m['brier'] if m['brier'] is None else round(m['brier'],4)} "
            f"ECE={m['ece'] if m['ece'] is None else round(m['ece'],4)} "
            f"Sharpe={m['sharpe']:.2f}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true', help='rebuild prediction matrix cache')
    ARGS = ap.parse_args()
    run()
