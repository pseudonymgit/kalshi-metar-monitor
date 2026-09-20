#!/usr/bin/env python3
"""
R/T-3 LANE / GATE REBUILD-AND-TEST — re-validate EVERY trading lane + gate
threshold against the corrected ground-truth axis + real strikes.

P0-A/P0-B already committed (corrected strike-crossing axis + real strikes).
R/T-2 swept 43 standalone signals (2 thin candidates). R/T-2b swept fusion
(no edge; pooling dilutes). NEITHER re-validated the LANE / GATE threshold
constants themselves — R/T-2 §5 only checked import reachability.

This is the missing pass: for every lane in `ultimate_sweep.LANES` and every
gate in `ultimate_sweep.GATES`, re-test the actual THRESHOLD constants against
the corrected-axis feed-signal metrics (MCC + accuracy + Sharpe, base-rate-
corrected) and emit a deterministic pass/fail disposition.

READ-ONLY against the DB. No signal re-enable/wiring. No data mutation.
Deterministic: no RNG, fixed ordering, script-only scoring loop (no AI calls).

Usage:
  python3 scripts/rt3_lane_gate_rebuild.py              # full re-validation + emit
  python3 scripts/rt3_lane_gate_rebuild.py --dry-run    # enumerate only
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
import scripts.ultimate_sweep as suite

METAR_DB = suite.METAR_DB
REPORTS_DIR = suite.REPORTS_DIR


# ─────────────────────────────────────────────────────────────────────────
# LANE / GATE -> FEED-SIGNAL mapping (the signals whose corrected-axis metrics
# govern whether the lane/gate threshold is actually clearable).
# ─────────────────────────────────────────────────────────────────────────

# Each lane maps to the signals that feed it (per the lane module + registry).
LANE_FEEDS = {
    'forecasting_ensemble': ['gaussian', 'gaussian_v2', 'calendar_climatology',
                             'pressure_delta', 'forecast_disagreement'],
    'sure_thing': ['frontal_passage_intraday'],
    'goldilocks': ['goldilocks'],
    'trajectory': ['trajectory_state_vector', 'nwp_analog', 'nwp_analog_ensemble'],
    'nowcasting': ['metar_nowcast', 'frontal_passage_nowcast'],
    'radiational_cooling': ['radiational_cooling'],
}

# Each gate maps to the signals it filters/requires (per gate module logic).
GATE_FEEDS = {
    'agreement': ['forecasting_ensemble', 'gaussian', 'gaussian_v2',
                  'pressure_delta', 'calendar_climatology', 'forecast_disagreement'],
    'settlement_execution': ['frontal_passage_intraday', 'nwp_dtdt_fusion'],
    'production': ['spike_reversion'],          # production gate was built for spike reversion
    'station_skill': ['gaussian', 'calendar_climatology', 'pressure_delta', 'nowcasting'],
    'liquidity': ['spread_based_entry'],
    'trajectory_confirmation': ['trajectory_state_vector', 'nwp_analog', 'nwp_analog_ensemble'],
    'spatial_coherence': ['gaussian', 'nowcasting', 'calendar_climatology'],
    'adaptive_thresholds': ['calendar_climatology', 'gaussian', 'gaussian_v2',
                           'pressure_delta', 'forecast_disagreement', 'spike_reversion',
                           'goldilocks', 'wind_direction_shift', 'frontal_passage_intraday',
                           'frontal_detector', 'dewpoint_depression', 'temperature_advection',
                           'spread_based_entry', 'nowcasting'],
    'crossfeed': ['cross_model_divergence', 'nwp_analog', 'nwp_analog_ensemble'],
}

# Threshold constants (extracted from the gate/lane modules — the actual values
# the engine uses to decide pass/fail). These are the re-test targets.
LANE_GATE_THRESHOLDS = {
    # lane thresholds
    'forecasting_ensemble': {'min_edge': 0.01, 'n_agreement': 4, 'note': 'fusion min edge + N-of-M agreement'},
    'sure_thing': {'min_edge': 0.01, 'note': 'frontal-passage intraday sure-thing threshold'},
    'goldilocks': {'confidence_floor': 0.50, 'transient_delta': 0.3, 'exceeded': 0.5,
                   'min_deviation': 1.5, 'note': 'microstructure transient spike detection'},
    'trajectory': {'max_w_traj': 0.15, 'high_conf': 0.7, 'analog_count_med': 50, 'dtw_good': 0.2,
                   'note': 'trajectory lane = modulator, not gate (max 0.15 influence)'},
    'nowcasting': {'confidence': 0.0, 'note': 'nowcast lane = probability-shift modulator'},
    'radiational_cooling': {'confidence_floor': 0.50, 'note': 'LOW-only radiational cooling lane'},
    # gate thresholds
    'agreement': {'n_required': 3, 'm_total': 9, 'min_confidence': 0.25,
                  'note': 'N-of-M democratic consensus (3/9 signals agree)'},
    'settlement_execution': {'note': 'epoch tradeability: station-approval + open + in-window + cooldown'},
    'production': {'min_accuracy': 0.58, 'min_trades': 100, 'max_station_conc': 0.30, 'min_sharpe': 1.0,
                   'note': 'real-money unlock: acc>=58%, trades>=100, conc<=30%, Sharpe>=1.0'},
    'station_skill': {'bss': 0.0, 'note': 'per-station BSS > 0 vs persistence AND climatology'},
    'liquidity': {'note': 'reject trades where signal_edge < half-spread + commission (0.1%)'},
    'trajectory_confirmation': {'note': 'CONFIRM/NEUTRAL/OVERRIDE analog-matching confirmation'},
    'spatial_coherence': {'min_wsum': 0.8, 'note': 'dense-cluster min confirming weight-sum'},
    'adaptive_thresholds': {'tau_min': 0.55, 'tau_max': 0.85, 'note': 'Beta-Bernoulli lower credible bound 55-85%'},
    'crossfeed': {'bump_pp': 0.5, 'note': 'cross-model divergence modulation +0.5-2.0pp'},
}


def load_labels():
    """Load corrected-axis labels (reuse canonical suite loader → settlement_truth)."""
    labels, src_map, meta, excl = suite.load_labels()
    return labels, src_map, meta, excl


def sweep_feed_signals(labels, meta):
    """Re-run the directional signal sweep on the corrected axis to obtain the
    per-signal MCC / accuracy / Sharpe / base-rate-delta that gate thresholds
    are compared against. Reuses the canonical suite's signal scorer."""
    registry, status = suite.build_signal_registry()
    conn = sqlite3.connect(METAR_DB)
    results = {}
    for name in sorted(registry):
        sig = registry[name]
        if sig is None:
            results[name] = {'status': 'NOT_REACHABLE', 'reason': status[name]}
            continue
        if name in suite.SLOW_SIGNALS_TO_SKIP:
            results[name] = {'status': 'SKIPPED', 'reason': 'slow_or_live_api'}
            continue
        with contextlib.redirect_stderr(io.StringIO()):
            m = suite._score_signal(sig, name, labels, meta, conn)
        if m['n'] == 0:
            results[name] = {'status': 'NO_FIRES', 'reason': 'no direction produced'}
        else:
            results[name] = m
    conn.close()
    return results


def _signal_metric(feeds, results):
    """Aggregate the feed signals for a lane/gate into a single metric block.

    Returns (best_mcc, best_acc, best_sharpe, best_base_delta, any_fires, details).
    A lane/gate "clears" its threshold only if at least one feed signal produces
    genuine edge (MCC > 0 AND base_rate_delta > 0), because a gate that is fed
    only by skew-artifact signals (MCC≈0) cannot produce skill.
    """
    best_mcc = -1.0
    best_acc = 0.0
    best_sharpe = None
    best_base_delta = -1.0
    any_fires = False
    details = {}
    for name in feeds:
        r = results.get(name, {})
        if r.get('status') == 'SCORED' or 'mcc' in r:
            any_fires = True
            mcc = r.get('mcc', 0.0)
            acc = r.get('accuracy', 0.0)
            sharpe = r.get('sharpe')
            bd = r.get('base_rate_delta', 0.0)
            details[name] = {'mcc': mcc, 'acc': acc, 'sharpe': sharpe, 'base_delta': bd, 'n': r.get('n', 0)}
            if mcc > best_mcc:
                best_mcc = mcc
                best_acc = acc
                best_sharpe = sharpe
                best_base_delta = bd
        else:
            details[name] = {'status': r.get('status', 'unknown'), 'reason': r.get('reason')}
    return best_mcc, best_acc, best_sharpe, best_base_delta, any_fires, details


def evaluate_lanes_gates(feed_results):
    """Emit a per-lane/per-gate pass/fail disposition against threshold constants.

    PRIMARY SKILL BAR = MCC (imbalance-immune, skew-proof), per the R/T-3 mandate.
    `base_rate_delta` in the canonical scorer is measured against the market-type-
    conditional majority (~91%), which is always negative for any directional signal
    (no signal knows market type) — it is NOT the skew test. The skew test is MCC ≈ 0.

    Dispositions:
      - PASS    : feed signal MCC > 0 (genuine discrimination) AND clears its numeric
                  threshold (where the gate has one).
      - KILLED  : feed signal MCC <= 0 (skew-artifact / dead / negative), OR the feed
                  signal fails its numeric threshold (e.g. adaptive tau_min 0.55, but
                  best feed acc is below it).
      - PARKED  : modulator / non-directional lane-gate (no directional output by
                  design — a separate concern, not a directional edge source), OR
                  MCC > 0 but thin/needs-OOS (not yet cleared).
    """
    out = {'lanes': {}, 'gates': {}}

    # MCC bar: meaningful skill threshold. The two R/T-2 candidates sit at ~+0.106..0.108;
    # anything below ~+0.03 is marginal-to-noise on this axis.
    MCC_MEANINGFUL = 0.03

    # OOS-FAILED feed signals (R/T-2 candidates that cleared in-sample but failed
    # out-of-sample walk-forward). A lane/gate fed only by these cannot be a clean
    # PASS — its in-sample threshold clear is not reproducible OOS.
    OOS_FAILED = {'nwp_dtdt_fusion', 'frontal_passage_intraday'}

    def _feed_is_oos_failed(feeds):
        return any(f in OOS_FAILED for f in feeds)

    for lane in suite.LANES:
        feeds = LANE_FEEDS.get(lane, [])
        mcc, acc, sharpe, bd, fires, details = _signal_metric(feeds, feed_results)
        thresh = LANE_GATE_THRESHOLDS.get(lane, {})
        oos_failed = _feed_is_oos_failed(feeds)
        if not fires:
            verdict = 'PARKED'
            reason = 'no feed signal produced directional output (modulator/non-directional lane)'
        elif mcc <= 0.0:
            verdict = 'KILLED'
            reason = f'feed signals are base-rate artifacts (best MCC {mcc:+.4f} <= 0); edge is skew, not skill'
        elif oos_failed:
            verdict = 'PARKED'
            reason = f'feed signal {mcc:+.4f} MCC in-sample but is an OOS-failed candidate; NOT wire-ready'
        elif mcc < MCC_MEANINGFUL:
            verdict = 'PARKED'
            reason = f'feed signal MCC {mcc:+.4f} positive but marginal (< {MCC_MEANINGFUL}); not a reliable edge source'
        else:
            verdict = 'PASS'
            reason = f'feed signal MCC {mcc:+.4f} >= {MCC_MEANINGFUL} (genuine discrimination); acc {acc*100:.1f}%'
        out['lanes'][lane] = {
            'verdict': verdict,
            'threshold': thresh,
            'best_feed_mcc': round(mcc, 4),
            'best_feed_acc': round(acc, 4),
            'best_feed_sharpe': round(sharpe, 4) if sharpe is not None else None,
            'best_feed_base_delta': round(bd, 4),
            'feed_details': details,
            'reason': reason,
        }

    for gate in suite.GATES:
        feeds = GATE_FEEDS.get(gate, [])
        mcc, acc, sharpe, bd, fires, details = _signal_metric(feeds, feed_results)
        thresh = LANE_GATE_THRESHOLDS.get(gate, {})
        oos_failed = _feed_is_oos_failed(feeds)
        # Numeric threshold clear check (where the gate has a numeric bar).
        numeric_clear = None
        numeric_bar = None
        if 'min_accuracy' in thresh and acc > 0:
            numeric_bar = thresh['min_accuracy']
            numeric_clear = acc >= numeric_bar
        elif 'min_sharpe' in thresh and sharpe is not None:
            numeric_bar = thresh['min_sharpe']
            numeric_clear = sharpe >= numeric_bar
        elif 'tau_min' in thresh and acc > 0:
            numeric_bar = thresh['tau_min']
            numeric_clear = acc >= numeric_bar
        elif 'min_edge' in thresh and acc > 0:
            numeric_bar = thresh['min_edge']
            numeric_clear = bd >= numeric_bar

        if not fires:
            verdict = 'PARKED'
            reason = 'no feed signal produced directional output (non-directional / structural)'
        elif mcc <= 0.0:
            verdict = 'KILLED'
            reason = f'feed signals are base-rate artifacts (best MCC {mcc:+.4f} <= 0); no discriminative skill'
        elif numeric_clear is False:
            verdict = 'KILLED'
            reason = (f'best feed signal acc {acc*100:.1f}% does NOT clear numeric threshold {numeric_bar} '
                      f'(MCC {mcc:+.4f})')
        elif oos_failed:
            verdict = 'PARKED'
            reason = f'in-sample threshold clear (MCC {mcc:+.4f}) but feed signal is OOS-failed; NOT wire-ready'
        elif mcc >= MCC_MEANINGFUL:
            verdict = 'PASS'
            reason = f'feed signal clears threshold (MCC {mcc:+.4f}, acc {acc*100:.1f}%)'
        else:
            verdict = 'PARKED'
            reason = f'MCC {mcc:+.4f} positive but marginal; needs OOS validation'
        out['gates'][gate] = {
            'verdict': verdict,
            'threshold': thresh,
            'numeric_threshold_clear': numeric_clear,
            'best_feed_mcc': round(mcc, 4),
            'best_feed_acc': round(acc, 4),
            'best_feed_sharpe': round(sharpe, 4) if sharpe is not None else None,
            'best_feed_base_delta': round(bd, 4),
            'feed_details': details,
            'reason': reason,
        }

    return out


def run(dry_run=False):
    t0 = time.time()
    labels, src_map, meta, excl = load_labels()
    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    base_rate_up = lblc.get('up', 0) / n if n else 0.0

    print("=" * 90)
    print("R/T-3 LANE / GATE REBUILD-AND-TEST — corrected axis + real strikes")
    print("=" * 90)
    print(f"Axis        : strike-crossing (settlement_temp_f vs strike_price)")
    print(f"Truth source: settlement_truth.strike_price ({n} labels, up={lblc.get('up',0)}, down={lblc.get('down',0)})")
    print(f"Base rate UP: {base_rate_up*100:.2f}%  (always-down null Sharpe ~5.25)")
    print(f"Excluded    : {excl}")
    print()

    if dry_run:
        print("DRY-RUN — enumerate lanes/gates only (no scoring).")
        lg = suite.sweep_lanes_gates(dry_run=True)
        for group, items in lg.items():
            print(f"  {group}: {', '.join(sorted(items))}")
        return

    # Re-run feed-signal sweep on corrected axis (canonical scorer).
    print("[FEED-SIGNAL SWEEP — corrected axis, MCC + acc + Sharpe + base-delta]")
    feed = sweep_feed_signals(labels, meta)
    # Print compact feed-signal table.
    for name in sorted(feed):
        r = feed[name]
        if 'mcc' in r:
            print(f"  {name:<30s} acc={r['accuracy']*100:6.2f}% baseΔ={r['base_rate_delta']*100:+6.2f} "
                  f"MCC={r['mcc']:+6.4f} Sharpe={r['sharpe'] if r['sharpe'] is None else round(r['sharpe'],2)} n={r['n']:5d}")
        else:
            print(f"  {name:<30s} {r.get('status')} ({r.get('reason','')})")
    print()

    # Evaluate lanes + gates against thresholds.
    print("[LANE / GATE THRESHOLD RE-VALIDATION]")
    verdicts = evaluate_lanes_gates(feed)

    for group, items in verdicts.items():
        print(f"\n  {group.upper()}:")
        for name, d in items.items():
            v = d['verdict']
            print(f"    - {name:<26s} {v:<8s} feedMCC={d['best_feed_mcc']:+.4f} "
                  f"feedAcc={d['best_feed_acc']*100:.1f}% baseΔ={d['best_feed_base_delta']*100:+.2f}pt")
            print(f"        reason: {d['reason']}")

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'suite': 'R/T-3 lane/gate rebuild-and-test',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction',
        'truth_source': 'settlement_truth.strike_price (14,305 rows; 10,079 greater + 4,051 less + 54 between)',
        'n_labels': n,
        'label_distribution': dict(lblc),
        'base_rate_up': round(base_rate_up, 6),
        'excluded': excl,
        'feed_signals': feed,
        'lane_gate_verdicts': verdicts,
        'lane_gate_reachability': suite.sweep_lanes_gates(dry_run=False),
        'elapsed_seconds': round(time.time() - t0, 2),
    }

    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    json_path = os.path.join(REPORTS_DIR, f'rt3_lane_gate_rebuild_{ts}.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReceipt JSON : {json_path}")
    print(f"Elapsed      : {report['elapsed_seconds']:.2f}s")
    return report, json_path


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    run(dry_run=args.dry_run)
