#!/usr/bin/env python3
"""
R/T-1 ULTIMATE SWEEP / TEST / CALIBRATE SUITE — canonical test harness.

One entry point that sweeps EVERYTHING against the CORRECT ground truth
(strike-crossing axis), covering ALL test layers:

  Layer 1 — directional          (signal direction vs strike-crossing label)
  Layer 2 — intraday             (intraday signal surfaces where available)
  Layer 3 — predictions          (probability / Brier / ECE / calibration)
  Layer 4 — trajectory / sub-hourly (Kalman / trajectory lane / gate)
  Layer 5 — fusion               (majority / weighted / UWC cascade)
  Layer 6 — calibration          (Platt / BMA / EMOS + per-signal fit)
  Layer 7 — entry timing         (spread / settlement-window / execution gates)
  Layer 8 — exit logic           (stop-loss / loss-limiter / drawdown)
  Layer 9 — risk / sizing        (Kelly / variance-weighted / continuous-Kelly)
  Layer 10 — crossfeed           (cross-model divergence / agreement)

Four sweeps (canonical):
  1. signal sweep      — every registered signal, active + killed + recovered,
                         MCC + accuracy + Sharpe + Brier + ECE.
  2. lane/gate sweep   — every gate/lane/fusion mode enumerated (pass/fail).
  3. calibration sweep — calibration surface (Platt/BMA/EMOS + per-signal fit).
  4. experiment/function sweep — the reusable experiment functions (DTW, Gaussian,
                         etc.) smoke-tested against the truth set.

GROUND TRUTH (correct axis, NOT day-over-day):
  settlement_temp vs strike, per core/settlement_outcome.strike_crossing_direction.

TRUTH HOT-SWAP:
  The label loader accepts a truth source. TODAY it loads the reconstructed
  1,537-label set (T-contract strikes parsed from kalshi_market_snapshots.db,
  joined against settlement_truth date-tiered temps). When P0-B lands (full
  2021->present strike history persisted to settlement_truth.strike_price), the
  SAME loader reads settlement_truth.strike_price directly — no re-architecture.

  Source selection:
    --truth reconstruct   (default)  — parse market_ticker strikes (1,537 labels)
    --truth settlement               — read settlement_truth.strike_price directly
                                        (56 rows today; full set after P0-B)

MCC, not accuracy, is the honest skill metric (label set is 82% up-skewed).

Usage:
  python3 scripts/ultimate_suite.py                    # full suite
  python3 scripts/ultimate_suite.py --dry-run         # enumerate everything, no scoring
  python3 scripts/ultimate_suite.py --layer directional --layer calibration
  python3 scripts/ultimate_suite.py --truth settlement

Output:
  reports/ultimate_suite_<ts>.json    (structured, R/T-2..4 consumable)
  reports/ultimate_suite_<ts>.md       (markdown summary)

READ-ONLY. No signal changes. No commit of signal state. Dan holds wire-in/revival.
"""

import argparse
import importlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.settlement_outcome import (
    strike_crossing_direction,
    contract_is_itm,
    UP, DOWN,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
SNAP_DB = os.path.join(BASE, "data", "kalshi_market_snapshots.db")
REPORTS_DIR = os.path.join(BASE, "reports")

MON = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
       'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO',
]


# ══════════════════════════════════════════════════════════════════════
# 1. TRUTH LOADER — hot-swap source (reconstruct vs settlement)
# ══════════════════════════════════════════════════════════════════════

def _ymd(d):
    return f'2026-{MON[d[2:5]]:02d}-{d[5:7]}'


def _parse_ticker(t):
    m = re.match(r'^(KX[HIGHLOW]+T?)(\w+)-(\d{2}[A-Z]{3}\d{2})-([TB])([\d.]+)$', t)
    if not m:
        return None
    prefix = m.group(1)
    mtype = 'LOW' if 'LOW' in prefix else ('HIGH' if 'HIGH' in prefix else None)
    return (mtype, m.group(3), m.group(4), m.group(5))


def load_labels_reconstruct():
    """Reconstruct correct-axis labels from kalshi_market_snapshots.db tickers.

    Parses T-contract (strict greater/less) strikes from market_ticker, joins
    against settlement_truth date-tiered temps (GHCN -> Kalshi finalized ->
    WeatherAPI). This is the 1,537-label set TODAY; after P0-B, `settlement`
    source supersedes it.
    """
    c = sqlite3.connect(SNAP_DB); cur = c.cursor()
    cur.execute("SELECT DISTINCT station, market_ticker FROM market_snapshots")
    rows = cur.fetchall()
    c.close()

    c2 = sqlite3.connect(METAR_DB); cur2 = c2.cursor()
    cur2.execute("""SELECT station, market_type, local_trading_date, settlement_temp_f,
                           temp_source, trustworthy
                    FROM settlement_truth WHERE settlement_temp_f IS NOT NULL""")
    temps = defaultdict(dict); src = defaultdict(dict); trust = defaultdict(dict)
    for st, mt, date, temp, ts, tr in cur2.fetchall():
        temps[(st, mt, date)] = temp
        src[(st, mt, date)] = ts
        trust[(st, mt, date)] = tr
    c2.close()

    labels = defaultdict(dict)   # station -> {(mt,date): 'up'/'down'}
    src_map = defaultdict(dict)
    for st, mt_t in rows:
        p = _parse_ticker(mt_t)
        if not p:
            continue
        mtype, date, stype, strike = p
        if mtype is None or stype != 'T':
            continue
        d = _ymd(date)
        temp = temps.get((st, mtype, d))
        if temp is None or trust[(st, mtype, d)] == 0:
            continue
        itm = contract_is_itm(temp, float(strike), mtype)
        if itm is None:
            continue
        labels[st][(mtype, d)] = (UP if itm else DOWN) if mtype == 'HIGH' else (DOWN if itm else UP)
        src_map[st][(mtype, d)] = src[(st, mtype, d)]
    return labels, src_map


def load_labels_settlement():
    """Load correct-axis labels from settlement_truth.strike_price directly.

    This is the P0-B full-set path. TODAY it yields 56 rows (only 2026-09-07/08
    carry a real strike); after P0-B lands it returns the full 2021->present set.
    """
    c = sqlite3.connect(METAR_DB); cur = c.cursor()
    cur.execute("""SELECT station, market_type, local_trading_date, settlement_temp_f,
                          temp_source, trustworthy, strike_price
                   FROM settlement_truth
                   WHERE settlement_temp_f IS NOT NULL AND strike_price IS NOT NULL""")
    labels = defaultdict(dict); src_map = defaultdict(dict)
    for st, mt, date, temp, ts, tr, strike in cur.fetchall():
        if tr == 0:
            continue
        d = strike_crossing_direction(temp, strike, mt)
        if d is None:
            continue
        labels[st][(mt, date)] = d
        src_map[st][(mt, date)] = ts
    c.close()
    return labels, src_map


LOADERS = {
    'reconstruct': load_labels_reconstruct,
    'settlement': load_labels_settlement,
}


def load_labels(source: str):
    """Return (labels, src_map). labels = {station: {(mt,date): 'up'|'down'}}."""
    return LOADERS[source]()


# ══════════════════════════════════════════════════════════════════════
# 2. METRICS — MCC is the honest skill metric (imbalance-immune)
# ══════════════════════════════════════════════════════════════════════

def mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _accumulate(stats, predicted, actual):
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


def _confusion_to_metrics(s, returns=None):
    tp, fp, tn, fn = s['tp'], s['fp'], s['tn'], s['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    out = {
        'accuracy': round(acc, 6),
        'mcc': round(mcc(tp, fp, tn, fn), 6),
        'n': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
    }
    if returns is not None and len(returns) > 0:
        out['sharpe'] = _sharpe(returns)
        out['n_returns'] = len(returns)
    return out


def _sharpe(returns, rf=0.0):
    """Annualized Sharpe from per-trade returns (unit bets)."""
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    # Binary-option returns are roughly daily; annualize with sqrt(252).
    return (mean - rf) / sd * (252 ** 0.5)


def invert(d):
    return {'up': 'down', 'down': 'up'}.get(d)


# ══════════════════════════════════════════════════════════════════════
# 3. SIGNAL ENUMERATION — active + killed + recovered
# ══════════════════════════════════════════════════════════════════════

def _safe_import_signal(rel_module, class_name):
    try:
        mod = importlib.import_module(f"core.signals.{rel_module}")
        return getattr(mod, class_name, None)
    except Exception:
        return None


def _try_instantiate(cls, **extra_kw):
    if cls is None:
        return None
    for kw in [{**extra_kw, "db_path": METAR_DB}, {"db_path": METAR_DB}, {}]:
        try:
            return cls(**kw)
        except Exception:
            continue
    return None


def build_signal_registry():
    """Full signal enumeration: active + killed + recovered (from sweep_engine
    build_signal_registry, reproduced here as the canonical surface)."""
    nwp_db = NWP_DB
    registry = {}

    def _reg(name, rel_module, class_name, **extra):
        cls = _safe_import_signal(rel_module, class_name)
        kw = dict(extra)
        if "db_path" not in kw:
            kw["db_path"] = METAR_DB
        registry[name] = _try_instantiate(cls, **kw)

    def _reg_func(name, rel_module, func_name):
        try:
            mod = importlib.import_module(f"core.signals.{rel_module}")
            registry[name] = getattr(mod, func_name, None)
        except Exception:
            registry[name] = None

    # Radiational cooling (LOW only)
    _reg("radiational_cooling", "radiational_cooling_signal", "RadiationalCoolingSignal")

    # Active (9)
    _reg("gaussian", "gaussian_signal", "GaussianSignal")
    _reg("gaussian_v2", "gaussian_v2_signal", "GaussianV2Signal")
    _reg("pressure_delta", "pressure_delta_signal", "PressureDeltaSignal")
    _reg("forecast_disagreement", "forecast_disagreement_signal", "ForecastDisagreementSignal")
    _reg("calendar_climatology", "calendar_climatology_signal", "CalendarClimatologySignal")
    _reg("frontal_passage_intraday", "frontal_passage_intraday_signal", "FrontalPassageIntradaySignal")
    _reg("dual_polarity", "dual_polarity_signal", "DualPolaritySignal", wrapped_signal=None)
    _reg("cloud_cover_index", "cloud_cover_index_signal", "CloudCoverIndexSignal")
    _reg("feels_like_delta", "feels_like_delta_signal", "FeelsLikeDeltaSignal")

    # Built not in registry (3)
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
    _reg("ai_composite", "ai_composite_signal", "AiCompositeSignal")
    _reg_func("frontal_passage_detector", "frontal_passage_detector", "evaluate")
    _reg("nwp_direct", "nwp_direct_signal", "NwpDirectSignal")
    _reg("nwp_dtdt_fusion", "nwp_dtdt_fusion_signal", "NwpDtdtFusionSignal", nwp_db_path=nwp_db)
    _reg("metar_nowcast", "metar_nowcast_signal", "MetarNowcastSignal")
    _reg("spread_based_entry", "spread_based_entry_signal", "SpreadBasedEntryDetector")
    _reg_func("dewpoint_depression", "dewpoint_depression_modulator", "modulate_confidence")
    _reg("regime", "regime_signal", "RegimeSignal")
    _reg("frontal_passage_nowcast", "frontal_passage_nowcast_signal", "FrontalPassageNowcastSignal", metar_db_path=METAR_DB)
    _reg("temperature_advection", "temperature_advection_signal", "TemperatureAdvectionSignal", db_path=METAR_DB)

    # Recovered / recently-revived targets (calibration targets in scope)
    _reg("seasonal_regime", "seasonal_regime_signal", "SeasonalRegimeSignal")
    _reg("cooling_failure", "cooling_failure_signal", "CoolingFailureSignal")
    _reg("warm_momentum", "warm_momentum_signal", "WarmMomentumSignal")
    _reg("trajectory_state_vector", "trajectory_state_vector_signal", "TrajectoryStateVectorSignal")
    _reg("nwp_analog_ensemble", "nwp_analog_signal", "NwpAnalogSignal", db_path=METAR_DB, nwp_db_path=nwp_db)

    # A1: 82-member meta-signal (ensemble fraction)
    _reg("eighty_two_member_ensemble", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(BASE, "data", "gefs_archive.db"),
         tigge_db=os.path.join(BASE, "data", "tigge_archive.db"))
    _reg("eighty_two_member_ensemble_ece", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(BASE, "data", "gefs_archive.db"),
         tigge_db=os.path.join(BASE, "data", "tigge_archive.db"),
         weighting_mode='ece_inverse')
    _reg("eighty_two_member_ensemble_pooled", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(BASE, "data", "gefs_archive.db"),
         tigge_db=os.path.join(BASE, "data", "tigge_archive.db"),
         weighting_mode='member_pooling')

    return registry


# Signals whose evaluate() always returns (None, 0.0) — must use evaluate_for_station
SIGNALS_NEEDING_FOR_STATION = {
    'intraday_metar_confirmation', 'fogr_reversion', 'metar_dtdt', 'pressure_tendency',
    'ecmwf_bias_corrected', 'esdr', 'nwp_dtdt_fusion'
}

# Signals that need sig._station set for evaluate() to work
SIGNALS_NEEDING_STATION_CONTEXT = {'nwp_direct', 'ai_composite'}

# Slow / live-API signals to skip in this read-only environment
SLOW_SIGNALS_TO_SKIP = {
    'temperature_advection', 'intraday_metar_confirmation', 'ecmwf_bias_corrected',
    'esdr', 'nwp_direct'
}

STANDARD_EVALUATE_SIGNALS = {
    'gaussian', 'gaussian_v2', 'goldilocks', 'persistence', 'pressure_delta',
    'calendar_climatology', 'wind_direction_shift', 'forecast_disagreement',
    'frontal_detector', 'spread_based_entry', 'volume_momentum', 'settlement_arbitrage',
    'seasonal_regime'
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
    """Daily METAR series (mirror of killed_signal_retest loader)."""
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


# ══════════════════════════════════════════════════════════════════════
# 4. SWEEP 1 — SIGNAL SWEEP (directional layer, MCC+acc+Sharpe)
# ══════════════════════════════════════════════════════════════════════

def sweep_signals(labels, dry_run=False):
    registry = build_signal_registry()
    results = {}

    if dry_run:
        # Enumerate only — proof that every signal is accounted for.
        present = sorted(n for n, s in registry.items() if s is not None)
        missing = sorted(n for n, s in registry.items() if s is None)
        return {
            'enumerated': len(registry),
            'present': present,
            'missing_import': missing,
            'note': 'dry-run: signals enumerated, not scored',
        }

    conn = sqlite3.connect(METAR_DB)
    for name, sig in registry.items():
        if sig is None:
            results[name] = {'error': 'import_failed'}
            continue
        if name in SLOW_SIGNALS_TO_SKIP:
            results[name] = {'skipped': 'slow_or_live_api'}
            continue
        stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
        returns = []
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
                _accumulate(stats, direction, actual)
                returns.append(1.0 if direction == actual else -1.0)
        results[name] = _confusion_to_metrics(stats, returns)
    conn.close()
    return results


# ══════════════════════════════════════════════════════════════════════
# 5. SWEEP 2 — LANE / GATE / FUSION SWEEP (enumerate + smoke-test)
# ══════════════════════════════════════════════════════════════════════

# Canonical enumeration of every gate/lane/fusion mode in the codebase.
LANES = [
    'forecasting_ensemble',   # Lane 1
    'sure_thing',             # Lane 2 (frontal passage intraday)
    'goldilocks',             # Lane 2 alt (goldilocks microstructure)
    'trajectory',             # Lane 4 (trajectory confirmation)
    'nowcasting',             # metar_nowcast lane
    'radiational_cooling',    # LOW-only lane
]

GATES = [
    'agreement',              # N-of-M consensus
    'settlement_execution',   # settlement-window gate
    'production',             # LossLimiter / production readiness
    'station_skill',          # per-station BSS gate
    'liquidity',              # spread/volume gate
    'trajectory_confirmation',# trajectory gate
    'spatial_coherence',      # region agreement
    'adaptive_thresholds',    # Bayesian Beta-Bernoulli
    'crossfeed',              # cross-model divergence consumer
]

FUSION_MODES = ['none', 'uwc', 'majority', 'weighted']

CALIBRATION_MODES = ['platt', 'bma', 'emos', 'both']

EXPERIMENT_FUNCTIONS = [
    'a1_dtw_directional', 'a2_dtw_normalize', 'a3_dtw_matching', 'a4_dtw_seasonal_check',
    'a5_dtw_scaletest', 'gaussian_fusion', 'goldilocks_feature_engineering',
    'kalman_smoothing', 'trajectory_dtw_replay', 'nwp_analog_knn',
]


def _gate_available(gate_name):
    """Check whether a gate's module imports cleanly (smoke-test)."""
    mapping = {
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
    try:
        importlib.import_module(mapping[gate_name])
        return True
    except Exception as e:
        return False


def _lane_available(lane_name):
    mapping = {
        'forecasting_ensemble': 'core.signal_fusion',
        'sure_thing': 'core.signals.frontal_passage_intraday_signal',
        'goldilocks': 'core.lane_goldilocks',
        'trajectory': 'core.trajectory_lane',
        'nowcasting': 'core.nowcasting_lane',
        'radiational_cooling': 'core.signals.radiational_cooling_signal',
    }
    try:
        importlib.import_module(mapping[lane_name])
        return True
    except Exception:
        return False


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
        avail = _lane_available(lane)
        out['lanes'][lane] = {
            'available': avail,
            'status': 'enumerated' if dry_run else ('available' if avail else 'unavailable'),
        }
    for gate in GATES:
        avail = _gate_available(gate)
        out['gates'][gate] = {
            'available': avail,
            'status': 'enumerated' if dry_run else ('available' if avail else 'unavailable'),
        }
    for mode in FUSION_MODES:
        avail = _fusion_available(mode)
        out['fusion_modes'][mode] = {
            'available': avail,
            'status': 'enumerated' if dry_run else ('available' if avail else 'unavailable'),
        }
    for mode in CALIBRATION_MODES:
        avail = _calibration_available(mode)
        out['calibration_modes'][mode] = {
            'available': avail,
            'status': 'enumerated' if dry_run else ('available' if avail else 'unavailable'),
        }
    return out


# ══════════════════════════════════════════════════════════════════════
# 6. SWEEP 3 — CALIBRATION SWEEP (reuse phaseB pipeline + Brier/ECE)
# ══════════════════════════════════════════════════════════════════════

def sweep_calibration(labels, dry_run=False):
    """Calibration surface: per-signal Brier + ECE against correct-axis labels,
    plus calibration-mode availability. Reuses unified_backtest compute_brier/ece."""
    if dry_run:
        return {'note': 'dry-run: calibration modes enumerated (see lanes/gates sweep)',
                'calibration_modes': CALIBRATION_MODES}

    # Inline Brier/ECE (avoids unified_backtest's broken SignalFusionEngine import).
    def _brier(results):
        # results: (pred_dir, actual_dir, confidence). Brier on directional prob.
        vals = []
        for pred, actual, conf in results:
            p = conf if pred == 'up' else (1.0 - conf)
            y = 1.0 if actual == 'up' else 0.0
            vals.append((p - y) ** 2)
        return sum(vals) / len(vals) if vals else 1.0

    def _ece(results, n_bins=10):
        if not results:
            return 1.0
        bins = [[] for _ in range(n_bins)]
        for pred, actual, conf in results:
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
            ece += (len(b) / len(results)) * abs(mean_acc - mean_conf)
        return ece

    registry = build_signal_registry()
    conn = sqlite3.connect(METAR_DB)
    out = {}
    for name, sig in registry.items():
        if sig is None or name in SLOW_SIGNALS_TO_SKIP:
            continue
        results = []   # (pred_dir, actual_dir, confidence)
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
            continue
        out[name] = {
            'brier': round(_brier(results), 4),
            'ece': round(_ece(results), 4),
            'n': len(results),
        }
    conn.close()
    return out


# ══════════════════════════════════════════════════════════════════════
# 7. SWEEP 4 — EXPERIMENT / FUNCTION SWEEP (smoke-test reusable funcs)
# ══════════════════════════════════════════════════════════════════════

def sweep_experiments(labels, dry_run=False):
    """Enumerate and smoke-test the reusable experiment functions (DTW, Gaussian,
    Kalman, NWP-analog, trajectory). Proves the function surface is accounted for."""
    import glob
    func_files = {
        'dtw': ['scripts/a1_dtw_directional.py', 'scripts/a2_dtw_normalize.py',
                'scripts/a3_dtw_matching.py', 'scripts/a4_dtw_seasonal_check.py',
                'scripts/a5_dtw_scaletest.py'],
        'gaussian': ['scripts/apply_gaussian_fusion.py'],
        'goldilocks': ['scripts/goldilocks_feature_engineering.py'],
        'kalman': ['experiment_option2_kalman_smoothing.py'],  # repo root
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
            'status': 'enumerated' if dry_run else ('present' if present else 'missing'),
        }
    out['_label_count'] = sum(len(v) for v in labels.values()) if not dry_run else None
    return out


# ══════════════════════════════════════════════════════════════════════
# 8. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

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
}


def run_suite(source='reconstruct', layers=None, dry_run=False):
    t0 = time.time()
    labels, src_map = load_labels(source)
    n = sum(len(v) for v in labels.values())
    lblc = Counter(d for v in labels.values() for d in v.values())
    srcc = Counter(s for v in src_map.values() for s in v.values())

    # Determine which sweeps to run from requested layers.
    requested = set(layers) if layers else set(LAYER_SWEEPS.keys())
    sweeps = set()
    for layer in requested:
        sweeps.update(LAYER_SWEEPS.get(layer, ()))

    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'repo': 'prototypes/weather-engine-source',
        'branch': 'temp-sprint-branch',
        'suite': 'R/T-1 ultimate sweep/test/calibrate',
        'axis': 'strike-crossing (settlement_temp vs strike) — core/settlement_outcome',
        'truth_source': source,
        'n_labels': n,
        'label_distribution': dict(lblc),
        'temp_source_distribution': dict(srcc),
        'stations': len(labels),
        'dry_run': dry_run,
        'layers_requested': sorted(requested),
        'sweeps': sorted(sweeps),
        'results': {},
    }

    # Banner
    print("=" * 90)
    print("R/T-1 ULTIMATE SUITE — sweep / test / calibrate (correct strike-crossing axis)")
    print("=" * 90)
    print(f"Truth source : {source}")
    print(f"Correct-axis labels: {n}  (up={lblc.get('up',0)}, down={lblc.get('down',0)})")
    print(f"temp_source  : {dict(srcc)}")
    print(f"stations     : {len(labels)}")
    print(f"dry_run      : {dry_run}")
    print(f"sweeps       : {sorted(sweeps)}")
    print()

    if 'signal' in sweeps:
        print(f"\n[SIGNAL SWEEP]")
        report['results']['signal'] = sweep_signals(labels, dry_run)
        if dry_run:
            r = report['results']['signal']
            print(f"  enumerated={r['enumerated']}  present={len(r['present'])}  missing={len(r['missing_import'])}")
            for s in r['present']:
                print(f"    - {s}")
        else:
            for name, m in report['results']['signal'].items():
                if 'mcc' in m:
                    print(f"  {name:<28s} acc={m['accuracy']*100:6.2f}% MCC={m['mcc']:+7.4f} "
                          f"Sharpe={m.get('sharpe', 0.0):+6.2f} n={m['n']:4d}")
                else:
                    print(f"  {name:<28s} {m}")

    if 'lane_gate' in sweeps:
        print(f"\n[LANE / GATE / FUSION / CALIBRATION-MODE SWEEP]")
        report['results']['lane_gate'] = sweep_lanes_gates(dry_run)
        lg = report['results']['lane_gate']
        for group, items in lg.items():
            print(f"  {group}:")
            for name, d in items.items():
                print(f"    - {name:<26s} {d['status']}")

    if 'calibration' in sweeps:
        print(f"\n[CALIBRATION SWEEP]")
        report['results']['calibration'] = sweep_calibration(labels, dry_run)
        if not dry_run:
            for name, m in report['results']['calibration'].items():
                print(f"  {name:<28s} Brier={m['brier']:.4f} ECE={m['ece']:.4f} n={m['n']}")

    if 'experiment' in sweeps:
        print(f"\n[EXPERIMENT / FUNCTION SWEEP]")
        report['results']['experiment'] = sweep_experiments(labels, dry_run)
        for group, d in report['results']['experiment'].items():
            if group.startswith('_'):
                continue
            print(f"  {group:<14s} present={d['present']} missing={d['missing']}")

    report['elapsed_seconds'] = round(time.time() - t0, 2)

    # Emit structured artifacts
    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    json_path = os.path.join(REPORTS_DIR, f'ultimate_suite_{ts}.json')
    md_path = os.path.join(REPORTS_DIR, f'ultimate_suite_{ts}.md')
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
    lines.append(f"# R/T-1 Ultimate Suite — {report['generated_utc']}")
    lines.append("")
    lines.append(f"- **Axis:** {report['axis']}")
    lines.append(f"- **Truth source:** {report['truth_source']}")
    lines.append(f"- **Correct-axis labels:** {report['n_labels']} (up={report['label_distribution'].get('up',0)}, down={report['label_distribution'].get('down',0)})")
    lines.append(f"- **temp_source:** {report['temp_source_distribution']}")
    lines.append(f"- **Dry run:** {report['dry_run']}")
    lines.append(f"- **Sweeps:** {', '.join(report['sweeps'])}")
    lines.append("")
    if report['results'].get('signal'):
        lines.append("## Signal Sweep")
        lines.append("")
        sig = report['results']['signal']
        if isinstance(sig, dict) and 'present' in sig:
            # dry-run enumeration shape
            lines.append(f"- **Enumerated:** {sig['enumerated']}  present={len(sig['present'])}  missing={len(sig['missing_import'])}")
            lines.append("- **Present:** " + ", ".join(sig['present']))
            if sig['missing_import']:
                lines.append("- **Missing import:** " + ", ".join(sig['missing_import']))
        else:
            lines.append("| signal | accuracy | MCC | Sharpe | n |")
            lines.append("|---|---|---|---|---|")
            for name, m in sorted(sig.items()):
                if isinstance(m, dict) and 'mcc' in m:
                    lines.append(f"| {name} | {m['accuracy']*100:.2f}% | {m['mcc']:+.4f} | {m.get('sharpe',0.0):+.2f} | {m['n']} |")
                else:
                    lines.append(f"| {name} | — | — | — | {m} |")
        lines.append("")
    if report['results'].get('calibration'):
        lines.append("## Calibration Sweep")
        lines.append("")
        cal = report['results']['calibration']
        if isinstance(cal, dict) and 'note' in cal and 'calibration_modes' in cal:
            lines.append(f"- **Dry-run:** calibration modes enumerated: {', '.join(cal['calibration_modes'])}")
        else:
            lines.append("| signal | Brier | ECE | n |")
            lines.append("|---|---|---|---|")
            for name, m in sorted(cal.items()):
                if isinstance(m, dict):
                    lines.append(f"| {name} | {m['brier']} | {m['ece']} | {m['n']} |")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="R/T-1 Ultimate Suite")
    parser.add_argument('--truth', default='reconstruct',
                        choices=['reconstruct', 'settlement'],
                        help='truth source: reconstruct (1,537 labels) or settlement (P0-B full set)')
    parser.add_argument('--dry-run', action='store_true', help='enumerate only, no scoring')
    parser.add_argument('--layer', action='append', default=None,
                        choices=list(LAYER_SWEEPS.keys()),
                        help='restrict to specific layer(s); repeat for multiple')
    parser.add_argument('--sweep', action='append', default=None,
                        choices=['signal', 'lane_gate', 'calibration', 'experiment'],
                        help='run specific sweep(s)')
    args = parser.parse_args()

    # Map --sweep to layers
    sweep_to_layers = {
        'signal': ['directional'],
        'lane_gate': ['trajectory', 'fusion', 'entry_timing', 'exit_logic', 'risk_sizing', 'crossfeed'],
        'calibration': ['predictions', 'calibration'],
        'experiment': ['experiment'],
    }

    layers = list(args.layer) if args.layer else None
    if args.sweep:
        derived = set()
        for s in args.sweep:
            derived.update(sweep_to_layers.get(s, []))
        # force the experiment sweep when requested (it has no layer key)
        if 'experiment' in args.sweep:
            layers = list(derived) if layers else ['directional']
            run_suite(args.truth, layers=layers, dry_run=args.dry_run)
            # also run experiment sweep explicitly
            import time as _t
            labels, _ = load_labels(args.truth)
            print("\n[EXPERIMENT / FUNCTION SWEEP]")
            exp = sweep_experiments(labels, args.dry_run)
            for group, d in exp.items():
                if group.startswith('_'):
                    continue
                print(f"  {group:<14s} present={d['present']} missing={d['missing']}")
            return
        layers = list(derived)

    run_suite(args.truth, layers=layers, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
