#!/usr/bin/env python3
"""
R/T-1 ULTIMATE SWEEP / TEST / CALIBRATE SUITE — canonical test harness.

THE canonical entry point that everything downstream depends on:
  R/T-3 (wire-in candidate shortlist), R/T-4 (calibration freeze),
  R/R review, Dan's wire-in.

One deterministic script sweeps EVERYTHING, no exceptions:

  * Every registered signal — active + killed + recovered.
  * Every trading lane + gate threshold.
  * Every experiment + recovered function.
  * Every test layer (Dan's explicit list):
      1. directional           — signal direction vs strike-crossing label
      2. intraday              — intraday signal surfaces (where reachable)
      3. predictions           — probability / Brier / ECE / calibration
      4. trajectory/sub-hourly — Kalman / trajectory lane / L4 gate
      5. fusion                — majority / weighted / UWC cascade
      6. calibration           — Platt / BMA / EMOS + per-signal fit
      7. entry timing          — spread / settlement-window / execution gates
      8. exit logic            — stop-loss / loss-limiter / drawdown
      9. risk / sizing         — Kelly / variance-weighted / continuous-Kelly
     10. crossfeed             — cross-model divergence / agreement

GROUND TRUTH (P0 closed — clean, correct axis):
  * CORRECT axis: `settlement_temp_f > strike_price` (strike-crossing), NOT the
    old `settlement_bucket > prior_settlement_bucket` (day-over-day).
  * Real strikes: 14,305 rows (10,139 greater + 4,112 less + 54 between) via
    /historical backfill, persisted in `settlement_truth.strike_price`.
  * Date-tiered temp source ladder: GHCN -> Kalshi finalized -> WeatherAPI.
  * Contemporaneous-only data: NO lookahead (NWP gated via core/nwp_lookahead_gate,
    GHCN `date <= target_date`, ERA5 removed from training).

EVERY emitted number carries its axis + ground-truth source (provenance).
Never a bare number. MCC (not accuracy) is the honest skill metric — the label
set is market-type-conditional (HIGH resolves 'up' ~10%, LOW ~95%), so base-rate
comparison is mandatory.

READ-ONLY. No signal re-enable/wiring. No commit of signal state (R/T-5 / Dan).
Deterministic: fixed ordering, no RNG in the scoring loop.

Reuses (does not rebuild) the existing core:
  core/settlement_outcome.py   — strike_crossing_direction (single source of truth)
  core/unified_backtest.py     — Brier/ECE/Sharpe/drawdown helpers
  core/calibrate_all_signals.py — per-signal calibration driver
  scripts/sweep_engine.py      — signal registry + lane/gate enumeration

Usage:
  python3 scripts/ultimate_sweep.py                     # full suite
  python3 scripts/ultimate_sweep.py --dry-run           # enumerate only, no scoring
  python3 scripts/ultimate_sweep.py --layer directional # restrict to a layer
  python3 scripts/ultimate_sweep.py --no-emit           # don't write report files

Output:
  reports/ultimate_sweep_<ts>.json   (structured manifest, R/T-3..4 consumable)
  reports/ultimate_sweep_<ts>.md     (markdown manifest)
"""

import argparse
import contextlib
import importlib
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

# ── Parallelization (R/T-8G) ──
# ProcessPool worker-per-signal.  Each worker rebuilds its own signal instance
# (by name) and its own sqlite connection so there is NO shared mutable state
# and NO pickling of live signal objects across processes.  The parent merges
# results in sorted-key order, making parallel output deterministic and
# byte-identical to the serial path (modulo dict insertion order, which is
# preserved because both paths iterate `sorted(registry)`).
import concurrent.futures as _cf
import multiprocessing as _mp

_PARALLEL = False
_NUM_WORKERS = 1

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.settlement_outcome import strike_crossing_direction, UP, DOWN

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
SNAP_DB = os.path.join(BASE, "data", "kalshi_market_snapshots.db")
WEATHERAPI_DB = os.path.join(BASE, "data", "weatherapi_archive.db")
REPORTS_DIR = os.path.join(BASE, "reports")

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO',
]

# Synthetic fixture stations excluded from all scoring paths.
EXCLUDED_STATIONS = {"TEST"}

# ─────────────────────────────────────────────────────────────────────────
# GROUND-TRUTH LOADER — correct axis, real strikes, date-tiered temp ladder
# ─────────────────────────────────────────────────────────────────────────

def load_labels():
    """Load correct-axis labels DIRECTLY from settlement_truth.strike_price.

    This is the P0-closed full set: 14,305 strike rows (10,139 greater + 4,112
    less + 54 between). `between` bins have no single-strike greater/less
    semantics and are excluded (they are a bin, not a single-strike event).

    Returns:
      labels   : {station: {(market_type, date): 'up'|'down'}}
      src_map  : {station: {(market_type, date): temp_source}}
      meta     : {station: {(market_type, date): {strike, strike_type, temp, market_type}}}
      excl     : {'between_excluded', 'untrusted_excluded', 'no_temp', 'unknown_mt'}
    """
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


# ─────────────────────────────────────────────────────────────────────────
# METRICS — MCC is the honest skill metric (imbalance-immune)
# ─────────────────────────────────────────────────────────────────────────

def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _sharpe(returns, rf=0.0):
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    return (mean - rf) / sd * (252 ** 0.5)


def _ece_by_market_type(results, n_bins=10):
    """ECE computed WITHIN each market type then averaged (correct for the
    market-type-conditional base rate)."""
    by_mt = defaultdict(list)
    for pred, actual, conf, mt in results:
        by_mt[mt].append((pred, actual, conf))
    eces = []
    for mt, rlist in by_mt.items():
        bins = [[] for _ in range(n_bins)]
        for pred, actual, conf in rlist:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            b = min(int(p * n_bins), n_bins - 1)
            bins[b].append((p, y))
        ece = 0.0
        for b in bins:
            if not b:
                continue
            mean_conf = sum(p for p, _ in b) / len(b)
            mean_acc = sum(y for _, y in b) / len(b)
            ece += (len(b) / len(rlist)) * abs(mean_acc - mean_conf)
        eces.append(ece)
    if not eces:
        return None
    return sum(eces) / len(eces)


# ─────────────────────────────────────────────────────────────────────────
# SIGNAL ENUMERATION — active + killed + recovered (canonical surface)
# ─────────────────────────────────────────────────────────────────────────

def _safe_import_signal(rel_module, class_name):
    try:
        mod = importlib.import_module(f"core.signals.{rel_module}")
        return getattr(mod, class_name, None)
    except Exception:
        return None


def _try_instantiate(cls, **extra_kw):
    if cls is None:
        return None
    # Try in order: (1) caller's explicit kwargs as-is (respects an explicit
    # db_path such as NWP/WeatherAPI DB), (2) with a METAR_DB db_path fallback
    # (only when the caller didn't already supply one), (3) db_path=METAR_DB
    # alone, (4) no kwargs.  Never clobber an explicit db_path.
    attempts = [dict(extra_kw)]
    if "db_path" not in extra_kw:
        attempts.append({**extra_kw, "db_path": METAR_DB})
    attempts += [{"db_path": METAR_DB}, {}]
    for kw in attempts:
        try:
            return cls(**kw)
        except Exception:
            continue
    return None


def build_signal_registry():
    """Full signal enumeration: active + killed + recovered.

    Returns (registry, import_status) where import_status maps name -> 'ok' | 'missing_cls' |
    'import_failed:<exc>'. This lets the suite distinguish a signal that is
    REACHABLE (instantiated) from one that is NOT-REACHABLE (import/class missing),
    so nothing silently drops out.
    """
    nwp_db = NWP_DB
    registry = {}
    status = {}

    from core.signals.dual_polarity_signal import CorrectedPressureDeltaSignal as _CorrectedPressureDelta

    def _reg(name, rel_module, class_name, **extra):
        cls = _safe_import_signal(rel_module, class_name)
        if cls is None:
            # Distinguish module import failure vs missing class.
            try:
                importlib.import_module(f"core.signals.{rel_module}")
                status[name] = 'missing_cls'
            except Exception as e:
                status[name] = f'import_failed:{type(e).__name__}'
            registry[name] = None
            return
        kw = dict(extra)
        if "db_path" not in kw:
            kw["db_path"] = METAR_DB
        registry[name] = _try_instantiate(cls, **kw)
        status[name] = 'ok' if registry[name] is not None else 'instantiate_failed'

    def _reg_func(name, rel_module, func_name):
        try:
            mod = importlib.import_module(f"core.signals.{rel_module}")
            registry[name] = getattr(mod, func_name, None)
            status[name] = 'ok' if registry[name] is not None else 'missing_func'
        except Exception as e:
            registry[name] = None
            status[name] = f'import_failed:{type(e).__name__}'

    # Radiational cooling (LOW only)
    _reg("radiational_cooling", "radiational_cooling_signal", "RadiationalCoolingSignal")

    # Active (registry / Lane 1 forecasting ensemble)
    _reg("gaussian", "gaussian_signal", "GaussianSignal")
    _reg("gaussian_v2", "gaussian_v2_signal", "GaussianV2Signal")
    _reg("pressure_delta", "pressure_delta_signal", "PressureDeltaSignal")
    _reg("forecast_disagreement", "forecast_disagreement_signal", "ForecastDisagreementSignal")
    _reg("calendar_climatology", "calendar_climatology_signal", "CalendarClimatologySignal")
    _reg("frontal_passage_intraday", "frontal_passage_intraday_signal", "FrontalPassageIntradaySignal")
    _reg("dual_polarity", "dual_polarity_signal", "DualPolaritySignal",
         wrapped_signal=_CorrectedPressureDelta(db_path=METAR_DB))
    _reg("cloud_cover_index", "cloud_cover_index_signal", "CloudCoverIndexSignal", db_path=WEATHERAPI_DB)
    _reg("feels_like_delta", "feels_like_delta_signal", "FeelsLikeDeltaSignal", db_path=WEATHERAPI_DB)

    # Built not in registry
    _reg("metar_trend", "metar_trend_signal", "MetarTrendSignal", db_path=METAR_DB)
    _reg("cross_model_divergence", "cross_model_divergence_signal", "CrossModelDivergenceSignal",
         nwp_db_path=nwp_db, metar_db_path=METAR_DB)
    _reg("nwp_analog", "nwp_analog_signal", "NwpAnalogSignal", db_path=METAR_DB, nwp_db_path=nwp_db)

    # Dead / Killed / Orphaned (recovered + killed — calibrated targets)
    _reg("wind_direction_shift", "wind_direction_shift", "WindDirectionShiftSignal")
    _reg("corrected_pressure_delta", "dual_polarity_signal", "CorrectedPressureDeltaSignal")
    _reg("spike_reversion", "spike_reversion_signal", "SpikeReversionSignal")
    _reg("goldilocks", "goldilocks_signal", "GoldilocksSignal")
    _reg("pressure_tendency", "pressure_tendency_signal", "PressureTendencySignal")
    _reg("metar_dtdt", "metar_dtdt_signal", "MetarDtdtSignal")
    _reg("volume_momentum", "volume_momentum_signal", "VolumeMomentumSignal")
    _reg("settlement_arbitrage", "settlement_arbitrage_signal", "SettlementTimeArbitrageSignal")
    _reg("intraday_metar_confirmation", "intraday_metar_confirmation_signal", "IntradayMetarConfirmationSignal")
    _reg("fogr_reversion", "fogr_reversion_signal", "FogrReversionSignal")
    _reg("esdr", "esdr_signal", "EsdrSignal", nwp_db_path=nwp_db)
    _reg("frontal_detector", "frontal_detector_signal", "FrontalDetectorSignal")
    _reg("ai_composite", "ai_composite_signal", "AiCompositeSignal", db_path=nwp_db)
    _reg_func("frontal_passage_detector", "frontal_passage_detector", "evaluate")
    _reg("nwp_direct", "nwp_direct_signal", "NwpDirectSignal")
    _reg("nwp_dtdt_fusion", "nwp_dtdt_fusion_signal", "NwpDtdtFusionSignal", nwp_db_path=nwp_db)
    _reg("metar_nowcast", "metar_nowcast_signal", "MetarNowcastSignal")
    _reg("spread_based_entry", "spread_based_entry_signal", "SpreadBasedEntryDetector")
    _reg_func("dewpoint_depression", "dewpoint_depression_modulator", "modulate_confidence")
    _reg("regime", "regime_signal", "RegimeSignal")
    _reg("frontal_passage_nowcast", "frontal_passage_nowcast_signal", "FrontalPassageNowcastSignal", metar_db_path=METAR_DB)
    _reg("temperature_advection", "temperature_advection_signal", "TemperatureAdvectionSignal", db_path=METAR_DB)

    # Recovered / recently-revived targets
    _reg("seasonal_regime", "seasonal_regime_signal", "SeasonalRegimeClassifier")
    _reg("cooling_failure", "cooling_failure_signal", "CoolingFailureSignal")
    _reg("warm_momentum", "warm_momentum_signal", "WarmMomentumSignal")
    _reg("trajectory_state_vector", "trajectory_state_vector_signal", "TrajectoryStateVectorSignal")
    _reg("nwp_analog_ensemble", "nwp_analog_signal", "NwpAnalogSignal", db_path=METAR_DB, nwp_db_path=nwp_db)

    # A1: 82-member meta-signal (ensemble fraction)
    gefs_db = os.path.join(BASE, "data", "gefs_archive.db")
    tigge_db = os.path.join(BASE, "data", "tigge_archive.db")
    _reg("eighty_two_member_ensemble", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=gefs_db, tigge_db=tigge_db)
    _reg("eighty_two_member_ensemble_ece", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=gefs_db, tigge_db=tigge_db, weighting_mode='ece_inverse')
    _reg("eighty_two_member_ensemble_pooled", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=gefs_db, tigge_db=tigge_db, weighting_mode='member_pooling')

    return registry, status


# Signals whose evaluate() always returns (None, 0.0) — must use evaluate_for_station
SIGNALS_NEEDING_FOR_STATION = {
    'intraday_metar_confirmation', 'fogr_reversion', 'metar_dtdt', 'pressure_tendency',
    'ecmwf_bias_corrected', 'esdr', 'nwp_dtdt_fusion',
}

# Signals that need sig._station set for evaluate() to work
SIGNALS_NEEDING_STATION_CONTEXT = {'nwp_direct', 'ai_composite'}

# Signals that are slow / hit live APIs — enumerated but not scored in read-only mode
SLOW_SIGNALS_TO_SKIP = {
    'temperature_advection', 'intraday_metar_confirmation', 'ecmwf_bias_corrected',
    'esdr', 'nwp_direct',
}

STANDARD_EVALUATE_SIGNALS = {
    'gaussian', 'gaussian_v2', 'goldilocks', 'persistence', 'pressure_delta',
    'calendar_climatology', 'wind_direction_shift', 'forecast_disagreement',
    'frontal_detector', 'spread_based_entry', 'volume_momentum', 'settlement_arbitrage',
    'seasonal_regime',
}

# Signals that read from their OWN database (not the METAR conn passed by the
# sweep).  The sweep's `conn` is the METAR DB; these signals must self-connect
# to their own archive (e.g. weatherapi_archive.db) rather than reuse it.
SIGNALS_WITH_OWN_DB = {
    'cloud_cover_index', 'feels_like_delta',
}


def evaluate_signal(sig, sig_name, station, idx, date, days, conn):
    """Route signal evaluation correctly (from phaseB_calibration_pipeline)."""
    if sig_name in SIGNALS_NEEDING_FOR_STATION:
        if hasattr(sig, 'evaluate_for_station'):
            try:
                return sig.evaluate_for_station(station, date, conn=conn)
            except Exception:
                pass
        return None, 0.0
    if sig_name in SIGNALS_WITH_OWN_DB:
        # These signals connect to their own archive (not the METAR conn).
        if hasattr(sig, 'evaluate_for_station'):
            try:
                return sig.evaluate_for_station(station, date, conn=None)
            except Exception:
                pass
        return None, 0.0
    if sig_name in SIGNALS_NEEDING_STATION_CONTEXT:
        sig._station = station
    try:
        direction, confidence = sig.evaluate(idx, days)
    except Exception:
        direction, confidence = None, 0.0
    if direction is None and sig_name not in STANDARD_EVALUATE_SIGNALS:
        if hasattr(sig, 'evaluate_for_station'):
            try:
                direction, confidence = sig.evaluate_for_station(station, date, conn=conn)
            except Exception:
                pass
    return direction, confidence


def load_daily_series(conn, station):
    """Daily METAR series (mirror of killed_signal_retest loader).

    Emits BOTH °C and °F temperature fields.  `settlement_temp_f` (the strike-
    crossing axis) is in °F; °F-native signals (e.g. eighty_two_member_ensemble,
    seasonal_regime) must read the °F fields, so we also expose `high_f`,
    `low_f`, and `temp_f` computed from the stored °C values.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_c) AS high, MIN(temp_c) AS low, AVG(temp_c) AS temp,
               AVG(pressure_mb) AS pressure, AVG(wind_direction_deg) AS wind_dir,
               AVG(wind_speed_kt) AS wind_speed
        FROM metar_observations WHERE station = ? GROUP BY date_utc ORDER BY date_utc ASC
    """, (station,))
    out = []
    for r in cur.fetchall():
        high_c, low_c, temp_c = r[1], r[2], r[3]
        out.append({
            'date': r[0], 'station': station,
            'high': high_c, 'low': low_c, 'temp': temp_c,
            'high_f': (high_c * 9.0 / 5.0 + 32.0) if high_c is not None else None,
            'low_f': (low_c * 9.0 / 5.0 + 32.0) if low_c is not None else None,
            'temp_f': (temp_c * 9.0 / 5.0 + 32.0) if temp_c is not None else None,
            'pressure': r[4], 'wind_dir': r[5], 'wind_speed': r[6],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# SWEEP 1 — SIGNAL SWEEP (directional layer, MCC + acc + Sharpe + base-rate)
# ─────────────────────────────────────────────────────────────────────────

def _score_signal(sig, name, labels, meta, conn):
    """Score one signal on the correct axis, with base-rate comparison."""
    stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    returns = []
    calib = []
    per_mt = defaultdict(lambda: {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})
    for st, lblmap in labels.items():
        days = load_daily_series(conn, st)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for (mt, date), actual in lblmap.items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            direction, confidence = evaluate_signal(sig, name, st, idx, date, days, conn)
            if direction is None:
                continue
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
            returns.append(1.0 if direction == actual else -1.0)
            calib.append((direction, actual, float(confidence or 0.5), mt))

    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0

    # Base rate: per-market-type majority null, blended by prediction counts.
    null_correct = 0
    for mt in per_mt:
        m = per_mt[mt]
        up_n = m['tp'] + m['fn']
        down_n = m['fp'] + m['tn']
        null_correct += max(up_n, down_n)
    null_acc = null_correct / total if total else 0.0

    return {
        'accuracy': round(acc, 6),
        'mcc': round(_mcc(tp, fp, tn, fn), 6),
        'n': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'base_rate_accuracy': round(null_acc, 6),
        'base_rate_delta': round(acc - null_acc, 6),
        'ece': round(_ece_by_market_type(calib), 6) if calib else None,
        'sharpe': round(_sharpe(returns), 6) if returns else None,
        'n_returns': len(returns),
        'up_ratio_predicted': round((tp + fp) / total, 4) if total else None,
    }


def sweep_signals(labels, meta, dry_run=False, registry=None, status=None):
    if registry is None or status is None:
        registry, status = build_signal_registry()
    results = {}
    if dry_run:
        return {
            'enumerated': len(registry),
            'reachable': sorted(n for n in registry if registry[n] is not None),
            'not_reachable': {
                n: status[n] for n in sorted(registry) if registry[n] is None
            },
        }
    conn = sqlite3.connect(METAR_DB)
    for name in sorted(registry):
        sig = registry[name]
        if sig is None:
            results[name] = {'status': 'NOT_REACHABLE', 'reason': status[name]}
            continue
        if name in SLOW_SIGNALS_TO_SKIP:
            results[name] = {'status': 'SKIPPED', 'reason': 'slow_or_live_api'}
            continue
        with contextlib.redirect_stderr(io.StringIO()):
            m = _score_signal(sig, name, labels, meta, conn)
        if m['n'] == 0:
            results[name] = {'status': 'NO_FIRES', 'reason': 'signal never produced a direction on any label'}
        else:
            m['status'] = 'PASS' if (m['mcc'] > 0 and m['base_rate_delta'] > 0) else 'NO_EDGE'
            results[name] = m
    conn.close()
    return results


# ─────────────────────────────────────────────────────────────────────────
# SWEEP 2 — LANE / GATE / FUSION / CALIBRATION-MODE SWEEP
# ─────────────────────────────────────────────────────────────────────────

LANES = [
    'forecasting_ensemble',
    'sure_thing',
    'goldilocks',
    'trajectory',
    'nowcasting',
    'radiational_cooling',
]

GATES = [
    'agreement',
    'settlement_execution',
    'production',
    'station_skill',
    'liquidity',
    'trajectory_confirmation',
    'spatial_coherence',
    'adaptive_thresholds',
    'crossfeed',
]

FUSION_MODES = ['none', 'uwc', 'majority', 'weighted']
CALIBRATION_MODES = ['platt', 'bma', 'emos', 'both']

EXPERIMENT_FUNCTIONS = [
    'a1_dtw_directional', 'a2_dtw_normalize', 'a3_dtw_matching', 'a4_dtw_seasonal_check',
    'a5_dtw_scaletest', 'gaussian_fusion', 'goldilocks_feature_engineering',
    'kalman_smoothing', 'trajectory_dtw_replay', 'nwp_analog_knn',
]

_LANE_MODULES = {
    'forecasting_ensemble': 'core.signal_fusion',
    'sure_thing': 'core.signals.frontal_passage_intraday_signal',
    'goldilocks': 'core.lane_goldilocks',
    'trajectory': 'core.trajectory_lane',
    'nowcasting': 'core.nowcasting_lane',
    'radiational_cooling': 'core.signals.radiational_cooling_signal',
}

_GATE_MODULES = {
    'agreement': 'core.agreement_gate',
    'settlement_execution': 'core.settlement_execution_gate',
    'production': 'core.production_gate',
    'station_skill': 'core.station_skill_gate',
    'liquidity': 'core.liquidity_gate',
    'trajectory_confirmation': 'core.trajectory_confirmation_gate',
    'spatial_coherence': 'core.spatial_coherence',
    'adaptive_thresholds': 'core.adaptive_thresholds',
    'crossfeed': 'core.crossfeed_consumer',
}


def _module_reachable(module_name):
    try:
        importlib.import_module(module_name)
        return True, None
    except Exception as e:
        return False, f'{type(e).__name__}: {str(e)[:120]}'


def _fusion_available(mode):
    try:
        from core.signal_fusion import (
            UncertaintyWeightedCascade, fuse_majority_vote, fuse_weighted_vote,
        )
        if mode == 'none':
            return True
        if mode == 'uwc':
            return UncertaintyWeightedCascade is not None
        if mode == 'majority':
            return fuse_majority_vote is not None
        if mode == 'weighted':
            return fuse_weighted_vote is not None
        return False
    except Exception:
        return False


def _calibration_available(mode):
    try:
        if mode == 'platt':
            from core.platt_calibration import PlattCalibrationPipeline
            return PlattCalibrationPipeline is not None
        if mode in ('bma', 'emos', 'both'):
            from core.bma_emos import bma_calibrate, emos_calibrate
            return bma_calibrate is not None and emos_calibrate is not None
        return False
    except Exception:
        return False


def sweep_lanes_gates(dry_run=False):
    out = {'lanes': {}, 'gates': {}, 'fusion_modes': {}, 'calibration_modes': {}}
    for lane in LANES:
        ok, err = _module_reachable(_LANE_MODULES[lane])
        out['lanes'][lane] = {
            'reachable': ok,
            'status': 'enumerated' if dry_run else ('reachable' if ok else 'NOT_REACHABLE'),
            'reason': None if ok else err,
        }
    for gate in GATES:
        ok, err = _module_reachable(_GATE_MODULES[gate])
        out['gates'][gate] = {
            'reachable': ok,
            'status': 'enumerated' if dry_run else ('reachable' if ok else 'NOT_REACHABLE'),
            'reason': None if ok else err,
        }
    for mode in FUSION_MODES:
        ok = _fusion_available(mode)
        out['fusion_modes'][mode] = {
            'reachable': ok,
            'status': 'enumerated' if dry_run else ('reachable' if ok else 'NOT_REACHABLE'),
        }
    for mode in CALIBRATION_MODES:
        ok = _calibration_available(mode)
        out['calibration_modes'][mode] = {
            'reachable': ok,
            'status': 'enumerated' if dry_run else ('reachable' if ok else 'NOT_REACHABLE'),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────
# SWEEP 3 — CALIBRATION SWEEP (per-signal Brier + ECE on correct axis)
# ─────────────────────────────────────────────────────────────────────────

def sweep_calibration(labels, dry_run=False, registry=None, status=None):
    if dry_run:
        return {'note': 'dry-run: calibration modes enumerated (see lanes/gates sweep)',
                'calibration_modes': CALIBRATION_MODES}
    if registry is None or status is None:
        registry, status = build_signal_registry()

    def _brier(results):
        vals = []
        for pred, actual, conf in results:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            vals.append((p - y) ** 2)
        return sum(vals) / len(vals) if vals else 1.0

    conn = sqlite3.connect(METAR_DB)
    out = {}
    for name in sorted(registry):
        sig = registry[name]
        if sig is None:
            out[name] = {'status': 'NOT_REACHABLE', 'reason': status[name]}
            continue
        if name in SLOW_SIGNALS_TO_SKIP:
            out[name] = {'status': 'SKIPPED', 'reason': 'slow_or_live_api'}
            continue
        results = []
        with contextlib.redirect_stderr(io.StringIO()):
            for st, lblmap in labels.items():
                days = load_daily_series(conn, st)
                if not days:
                    continue
                idx_by_date = {d['date']: i for i, d in enumerate(days)}
                for (mt, date), actual in lblmap.items():
                    idx = idx_by_date.get(date)
                    if idx is None:
                        continue
                    direction, confidence = evaluate_signal(sig, name, st, idx, date, days, conn)
                    if direction is None:
                        continue
                    results.append((direction, actual, float(confidence or 0.5)))
        if not results:
            out[name] = {'status': 'NO_FIRES', 'reason': 'no predictions produced'}
            continue
        out[name] = {
            'status': 'SCORED',
            'brier': round(_brier(results), 4),
            'ece': round(_ece_by_market_type(
                [(p, a, c, 'HIGH') for p, a, c in results]) or 1.0, 4),
            'n': len(results),
        }
    conn.close()
    return out


# ─────────────────────────────────────────────────────────────────────────
# PARALLEL EXECUTION (R/T-8G) — worker-per-signal, deterministic merge
# ─────────────────────────────────────────────────────────────────────────
#
# Architecture:
#   * Each worker rebuilds its OWN signal instance from a name (via
#     build_signal_registry) and opens its OWN sqlite connection.  Nothing is
#     pickled except the (name, labels, meta) tuple — labels/meta are plain
#     JSON-serializable dicts.
#   * The parent submits one task per signal name in `sorted(registry)` order,
#     collects results, then re-sorts by name.  This makes the merged dict
#     insertion-ordered identically to the serial path.
#   * No shared mutable state: no module-level LossLimiter/gate singletons are
#     touched (those live in sweep_engine.py, not here).
#
# Worker functions MUST be module-level (picklable) for ProcessPool on the
# 'spawn' start method.

def _score_signal_worker(args):
    """ProcessPool worker: rebuild signal by name, score it, return (name, result)."""
    name, labels, meta = args
    registry, status = build_signal_registry()
    sig = registry.get(name)
    if sig is None:
        return name, {'status': 'NOT_REACHABLE', 'reason': status.get(name)}
    if name in SLOW_SIGNALS_TO_SKIP:
        return name, {'status': 'SKIPPED', 'reason': 'slow_or_live_api'}
    conn = sqlite3.connect(METAR_DB)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            m = _score_signal(sig, name, labels, meta, conn)
    finally:
        conn.close()
    if m['n'] == 0:
        return name, {'status': 'NO_FIRES', 'reason': 'signal never produced a direction on any label'}
    m['status'] = 'PASS' if (m['mcc'] > 0 and m['base_rate_delta'] > 0) else 'NO_EDGE'
    return name, m


def _calibrate_signal_worker(args):
    """ProcessPool worker: rebuild signal by name, calibrate it, return (name, result)."""
    name, labels, meta = args
    registry, status = build_signal_registry()
    sig = registry.get(name)
    if sig is None:
        return name, {'status': 'NOT_REACHABLE', 'reason': status.get(name)}
    if name in SLOW_SIGNALS_TO_SKIP:
        return name, {'status': 'SKIPPED', 'reason': 'slow_or_live_api'}

    def _brier(results):
        vals = []
        for pred, actual, conf in results:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            vals.append((p - y) ** 2)
        return sum(vals) / len(vals) if vals else 1.0

    conn = sqlite3.connect(METAR_DB)
    try:
        results = []
        with contextlib.redirect_stderr(io.StringIO()):
            for st, lblmap in labels.items():
                days = load_daily_series(conn, st)
                if not days:
                    continue
                idx_by_date = {d['date']: i for i, d in enumerate(days)}
                for (mt, date), actual in lblmap.items():
                    idx = idx_by_date.get(date)
                    if idx is None:
                        continue
                    direction, confidence = evaluate_signal(sig, name, st, idx, date, days, conn)
                    if direction is None:
                        continue
                    results.append((direction, actual, float(confidence or 0.5)))
    finally:
        conn.close()
    if not results:
        return name, {'status': 'NO_FIRES', 'reason': 'no predictions produced'}
    return name, {
        'status': 'SCORED',
        'brier': round(_brier(results), 4),
        'ece': round(_ece_by_market_type(
            [(p, a, c, 'HIGH') for p, a, c in results]) or 1.0, 4),
        'n': len(results),
    }


def _parallel_map(fn, names, labels, meta):
    """Deterministic parallel map over signal names.

    Submits tasks in `names` (already sorted), collects in submission order,
    and returns results keyed by name.  `chunksize=1` avoids reordering
    surprises from batching.  Falls back to serial on any failure.
    """
    n_workers = min(_NUM_WORKERS, len(names)) if _NUM_WORKERS > 0 else 1
    if not _PARALLEL or n_workers <= 1:
        out = {}
        for name in names:
            _, res = fn((name, labels, meta))
            out[name] = res
        return out
    tasks = [(name, labels, meta) for name in names]
    out = {}
    with _cf.ProcessPoolExecutor(max_workers=n_workers) as ex:
        for name, res in ex.map(fn, tasks, chunksize=1):
            out[name] = res
    return out


def sweep_signals_parallel(labels, meta, registry, status):
    """Parallel equivalent of sweep_signals (same output schema)."""
    names = sorted(registry)
    return _parallel_map(_score_signal_worker, names, labels, meta)


def sweep_calibration_parallel(labels, meta, registry, status):
    """Parallel equivalent of sweep_calibration (same output schema)."""
    names = sorted(registry)
    return _parallel_map(_calibrate_signal_worker, names, labels, meta)


# ─────────────────────────────────────────────────────────────────────────
# SWEEP 4 — EXPERIMENT / FUNCTION SWEEP (recoverable functions)
# ─────────────────────────────────────────────────────────────────────────

def sweep_experiments(dry_run=False):
    """Enumerate + smoke-test reusable experiment functions."""
    import glob
    func_files = {
        'dtw': ['scripts/a1_dtw_directional.py', 'scripts/a2_dtw_normalize.py',
                'scripts/a3_dtw_matching.py', 'scripts/a4_dtw_seasonal_check.py',
                'scripts/a5_dtw_scaletest.py'],
        'gaussian': ['scripts/apply_gaussian_fusion.py'],
        'goldilocks': ['scripts/goldilocks_feature_engineering.py'],
        'kalman': ['experiment_option2_kalman_smoothing.py'],
        'trajectory': ['scripts/trajectory_dtw_replay.py'],
        'nwp_analog': ['scripts/backtest_nwp_analog_knn.py'],
    }
    out = {}
    for group, paths in func_files.items():
        present = [p for p in paths if os.path.exists(os.path.join(BASE, p))]
        missing = [p for p in paths if not os.path.exists(os.path.join(BASE, p))]
        out[group] = {
            'present': [os.path.basename(p) for p in present],
            'missing': [os.path.basename(p) for p in missing],
            'status': 'enumerated' if dry_run else ('present' if present else 'NOT_REACHABLE'),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────
# LAYER -> SWEEP MAPPING + ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────

LAYER_SWEEPS = {
    'directional': ('signal',),
    'intraday': ('signal',),
    'predictions': ('calibration',),
    'trajectory': ('lane_gate',),
    'fusion': ('lane_gate',),
    'calibration': ('calibration',),
    'entry_timing': ('lane_gate',),
    'exit_logic': ('lane_gate',),
    'risk_sizing': ('lane_gate',),
    'crossfeed': ('lane_gate',),
    'experiment': ('experiment',),
}

ALL_LAYERS = list(LAYER_SWEEPS.keys())


def run_suite(layers=None, dry_run=False, emit=True):
    t0 = time.time()
    labels, src_map, meta, excl = load_labels()

    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    srcc = Counter(s for v in src_map.values() for s in v.values())
    mt_up = Counter(); mt_tot = Counter()
    for st, lblmap in labels.items():
        for (mt, date), d in lblmap.items():
            mt_tot[mt] += 1
            if d == 'up':
                mt_up[mt] += 1

    registry, status = build_signal_registry()

    requested = set(layers) if layers else set(ALL_LAYERS)
    sweeps = set()
    for layer in requested:
        sweeps.update(LAYER_SWEEPS.get(layer, ()))

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'suite': 'R/T-1 ultimate sweep/test/calibrate',
        'axis': 'strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction',
        'truth_source': 'settlement_truth.strike_price (14,305 rows via /historical backfill)',
        'n_labels': n,
        'label_distribution': dict(lblc),
        'base_rate_up': round(lblc.get('up', 0) / n, 6) if n else 0.0,
        'base_rate_by_market_type': {
            mt: round(mt_up[mt] / mt_tot[mt], 6) for mt in mt_tot
        },
        'stations': len(labels),
        'temp_source_distribution': dict(srcc),
        'excluded': excl,
        'dry_run': dry_run,
        'layers_requested': sorted(requested),
        'sweeps': sorted(sweeps),
        'parallel': bool(_PARALLEL),
        'num_workers': int(_NUM_WORKERS),
        'results': {},
    }

    print("=" * 92)
    print("R/T-1 ULTIMATE SWEEP / TEST / CALIBRATE — canonical harness")
    print("=" * 92)
    print(f"Axis         : {report['axis']}")
    print(f"Truth source : {report['truth_source']}")
    print(f"Labels       : {n}  (up={lblc.get('up',0)}, down={lblc.get('down',0)})")
    print(f"Base rate UP : {report['base_rate_up']*100:.2f}%")
    for mt in sorted(mt_tot):
        r = mt_up[mt] / mt_tot[mt] if mt_tot[mt] else 0.0
        print(f"  {mt:<5s} base-rate up = {r*100:.1f}%  (n={mt_tot[mt]})")
    print(f"Excluded     : {excl}")
    print(f"temp_source  : {dict(srcc)}")
    print(f"stations     : {len(labels)}")
    print(f"dry_run      : {dry_run}")
    print(f"sweeps       : {sorted(sweeps)}")
    print()

    if 'signal' in sweeps:
        print("[SWEEP 1 — SIGNAL SWEEP (directional layer, correct axis)]")
        if _PARALLEL and not dry_run:
            report['results']['signal'] = sweep_signals_parallel(labels, meta, registry, status)
        else:
            report['results']['signal'] = sweep_signals(labels, meta, dry_run, registry, status)
        r = report['results']['signal']
        if dry_run:
            print(f"  enumerated={r['enumerated']}  reachable={len(r['reachable'])}  not_reachable={len(r['not_reachable'])}")
            for s in r['reachable']:
                print(f"    - {s}")
            for s, why in r['not_reachable'].items():
                print(f"    - {s}  NOT_REACHABLE ({why})")
        else:
            for name, m in sorted(r.items()):
                if m.get('status') == 'NOT_REACHABLE':
                    print(f"  {name:<30s} NOT_REACHABLE ({m.get('reason')})")
                elif m.get('status') == 'SKIPPED':
                    print(f"  {name:<30s} SKIPPED ({m.get('reason')})")
                elif m.get('status') == 'NO_FIRES':
                    print(f"  {name:<30s} NO_FIRES")
                else:
                    tag = m['status']
                    print(f"  {name:<30s} acc={m['accuracy']*100:6.2f}% baseΔ={m['base_rate_delta']*100:+6.2f} "
                          f"MCC={m['mcc']:+6.4f} Sharpe={m['sharpe'] if m['sharpe'] is None else round(m['sharpe'],2)} "
                          f"n={m['n']:5d} [{tag}]")

    if 'lane_gate' in sweeps:
        print("\n[SWEEP 2 — LANE / GATE / FUSION / CALIBRATION-MODE]")
        report['results']['lane_gate'] = sweep_lanes_gates(dry_run)
        lg = report['results']['lane_gate']
        for group, items in lg.items():
            print(f"  {group}:")
            for name, d in items.items():
                reason = f" ({d['reason']})" if d.get('reason') else ""
                print(f"    - {name:<26s} {d['status']}{reason}")

    if 'calibration' in sweeps:
        print("\n[SWEEP 3 — CALIBRATION SWEEP (Brier/ECE)]")
        if _PARALLEL and not dry_run:
            report['results']['calibration'] = sweep_calibration_parallel(labels, meta, registry, status)
        else:
            report['results']['calibration'] = sweep_calibration(labels, dry_run, registry, status)
        if not dry_run:
            for name, m in sorted(report['results']['calibration'].items()):
                if m.get('status') == 'SCORED':
                    print(f"  {name:<30s} Brier={m['brier']:.4f} ECE={m['ece']:.4f} n={m['n']}")
                else:
                    print(f"  {name:<30s} {m.get('status')} ({m.get('reason')})")

    if 'experiment' in sweeps:
        print("\n[SWEEP 4 — EXPERIMENT / FUNCTION SWEEP]")
        report['results']['experiment'] = sweep_experiments(dry_run)
        for group, d in report['results']['experiment'].items():
            print(f"  {group:<14s} present={d['present']} missing={d['missing']} status={d['status']}")

    report['elapsed_seconds'] = round(time.time() - t0, 2)

    if emit:
        ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        json_path = os.path.join(REPORTS_DIR, f'ultimate_sweep_{ts}.json')
        md_path = os.path.join(REPORTS_DIR, f'ultimate_sweep_{ts}.md')
        with open(json_path, 'w') as f:
            json.dump(report, f, indent=2)
        with open(md_path, 'w') as f:
            f.write(render_markdown(report))
        print(f"\nReceipt JSON : {json_path}")
        print(f"Receipt MD   : {md_path}")
    print(f"Elapsed      : {report['elapsed_seconds']:.2f}s")
    return report


def render_markdown(report):
    lines = []
    lines.append(f"# R/T-1 Ultimate Sweep — {report['generated_utc']}")
    lines.append("")
    lines.append(f"- **Axis:** {report['axis']}")
    lines.append(f"- **Truth source:** {report['truth_source']}")
    lines.append(f"- **Labels:** {report['n_labels']} (up={report['label_distribution'].get('up',0)}, down={report['label_distribution'].get('down',0)})")
    lines.append(f"- **Base rate UP:** {report['base_rate_up']*100:.2f}%")
    lines.append(f"- **Base rate by market type:** {report['base_rate_by_market_type']}")
    lines.append(f"- **temp_source:** {report['temp_source_distribution']}")
    lines.append(f"- **Excluded:** {report['excluded']}")
    lines.append(f"- **Dry run:** {report['dry_run']}")
    lines.append(f"- **Sweeps:** {', '.join(report['sweeps'])}")
    lines.append("")

    if report['results'].get('signal'):
        lines.append("## Signal Sweep")
        lines.append("")
        sig = report['results']['signal']
        if isinstance(sig, dict) and 'reachable' in sig:
            lines.append(f"- **Enumerated:** {sig['enumerated']}  reachable={len(sig['reachable'])}  not_reachable={len(sig['not_reachable'])}")
            lines.append("- **Reachable:** " + ", ".join(sig['reachable']))
            if sig['not_reachable']:
                lines.append("- **Not reachable:** " + ", ".join(f"{k} ({v})" for k, v in sig['not_reachable'].items()))
        else:
            lines.append("| signal | status | accuracy | MCC | baseΔ | Sharpe | n |")
            lines.append("|---|---|---|---|---|---|---|")
            for name, m in sorted(sig.items()):
                if m.get('status') in ('NOT_REACHABLE', 'SKIPPED', 'NO_FIRES'):
                    lines.append(f"| {name} | {m.get('status')} | — | — | — | — | {m.get('reason','')} |")
                else:
                    lines.append(f"| {name} | {m['status']} | {m['accuracy']*100:.2f}% | {m['mcc']:+.4f} | {m['base_rate_delta']*100:+.2f} | {m['sharpe'] if m['sharpe'] is None else round(m['sharpe'],2)} | {m['n']} |")
        lines.append("")

    if report['results'].get('lane_gate'):
        lines.append("## Lane / Gate / Fusion / Calibration-Mode Sweep")
        lines.append("")
        lg = report['results']['lane_gate']
        for group, items in lg.items():
            lines.append(f"### {group}")
            lines.append("")
            lines.append("| name | status | reason |")
            lines.append("|---|---|---|")
            for name, d in items.items():
                lines.append(f"| {name} | {d['status']} | {d.get('reason','') or ''} |")
            lines.append("")

    if report['results'].get('calibration'):
        lines.append("## Calibration Sweep")
        lines.append("")
        cal = report['results']['calibration']
        if isinstance(cal, dict) and 'note' in cal:
            lines.append(f"- **Dry-run:** {cal['note']}")
        else:
            lines.append("| signal | status | Brier | ECE | n |")
            lines.append("|---|---|---|---|---|")
            for name, m in sorted(cal.items()):
                if m.get('status') == 'SCORED':
                    lines.append(f"| {name} | SCORED | {m['brier']} | {m['ece']} | {m['n']} |")
                else:
                    lines.append(f"| {name} | {m.get('status')} | — | — | {m.get('reason','')} |")
        lines.append("")

    if report['results'].get('experiment'):
        lines.append("## Experiment / Function Sweep")
        lines.append("")
        lines.append("| group | present | missing | status |")
        lines.append("|---|---|---|---|")
        for group, d in report['results']['experiment'].items():
            lines.append(f"| {group} | {', '.join(d['present'])} | {', '.join(d['missing'])} | {d['status']} |")
        lines.append("")

    return "\n".join(lines)


def main():
    global _PARALLEL, _NUM_WORKERS
    parser = argparse.ArgumentParser(description="R/T-1 Ultimate Sweep / Test / Calibrate Suite")
    parser.add_argument('--dry-run', action='store_true', help='enumerate only, no scoring')
    parser.add_argument('--layer', action='append', default=None,
                        choices=ALL_LAYERS,
                        help='restrict to specific layer(s); repeat for multiple')
    parser.add_argument('--no-emit', action='store_true', help='do not write report files')
    parser.add_argument('--workers', type=int, default=1, metavar='N',
                        help='parallelize signal/calibration scoring across N worker processes '
                             '(deterministic; output identical to serial). 1 = serial (default).')
    args = parser.parse_args()
    _NUM_WORKERS = args.workers
    _PARALLEL = args.workers > 1
    # dry-run enumerates only and never emits a (partial) manifest.
    emit = (not args.dry_run) and (not args.no_emit)
    run_suite(layers=args.layer, dry_run=args.dry_run, emit=emit)


if __name__ == '__main__':
    main()
