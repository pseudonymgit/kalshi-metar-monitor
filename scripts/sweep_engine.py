#!/usr/bin/env python3
"""
sweep_engine
Extracted from scripts/big_sweep.py.
"""

import math
import json
import csv
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from core.signal_families import get_active_signals, POOL_NAMES
from core.signal_fusion import UncertaintyWeightedCascade
from core.adaptive_thresholds import AdaptiveThresholdRegistry
from core.luck_elimination import print_luck_report, wire_dependencies, run_luck_elimination
from core.production_gate import LossLimiter
from core.ensemble_fraction import load_bias_corrections
from core.platt_calibration import PlattCalibrationPipeline
from core.db_utils import query_db, with_db
from scripts.sweep.config import STATIONS as _DEF_STATIONS
from scripts.sweep.config import SLIPPAGE_BUDGET
from scripts.sweep.tiers import get_tier, get_tier_info

#!/usr/bin/env python3
"""
big_sweep.py — Big Sweep: Phase 1 (Signal-Only) + Phase 2 (Meta-Sweep)

Phase 1: All 36 Signals × Full Parameter Sweep (extended)
Phase 2: Meta-Sweep over gate/lever/lane/modulator combinations
         using top signal configs from Phase 1.

Usage:
    python3 scripts/big_sweep.py                     # Phase 1 (default)
    python3 scripts/big_sweep.py --phase 1           # Explicit Phase 1
    python3 scripts/big_sweep.py --phase 2           # Phase 2 meta-sweep
    python3 scripts/big_sweep.py --phase 1 --fast    # Phase 1 fast mode

Phase 1 outputs:
  data/sweep_results_v1.json          — Raw per-signal config results
  data/sweep_signal_summary.csv       — Per-signal aggregate metrics
  data/sweep_correlation_matrix.csv   — Cross-signal correlation
  data/sweep/differential_results.json— All-pairs differential analysis
  data/sweep_phase1_top_configs.json  — Top 5 configs per signal (NEW)

Phase 2 outputs:
  data/sweep_phase2_results.json      — Per-meta-config results
  data/sweep_phase2_summary.csv       — Best meta-config summary
  data/sweep_phase2_gate_stats.json   — Per-gate pass/fail counts
  data/sweep_phase2_luck_stats.json   — Luck-adjusted metrics
  data/sweep_phase2_portfolio.csv     — Portfolio-level metrics

B-Mode compliant. No AI/ML inside the sweep loop.
"""

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.sweep import config as sweep_config
from scripts.sweep.config import STATIONS as _DEF_STATIONS
from scripts.sweep.config import SLIPPAGE_BUDGET
from scripts.sweep.tiers import get_tier, get_tier_info
from core.platt_calibration import PlattCalibrationPipeline
from core.bma_emos import bma_calibrate, emos_calibrate
from core.trajectory_confirmation_gate import (
    TrajectoryConfirmationGate,
    evaluate_gate_for_station_date,
)
from core.trajectory_lane import (
    TrajectoryLane,
    evaluate_lane_for_station_date,
    apply_trajectory_lane_to_probability,
)
from core.settlement_execution_gate import (
    SettlementExecutionGate,
    GateVerdict,
)
from core.db_utils import query_db, with_db
from core.continuous_kelly import fee_aware_kelly, KellyState, kalshi_fee as ck_fee
from core.liquidity_gate import LiquidityGate
from core.ensemble_fraction import load_bias_corrections
from core.production_gate import ProductionGate, LossLimiter
from core.station_skill_gate import StationSkillGate
from core.agreement_gate import AgreementGate
from core.signal_fusion import (
    UncertaintyWeightedCascade,
    FusionModeConfig,
    fuse_majority_vote,
    fuse_weighted_vote,
    DEFAULT_FUSION_CONFIG,
)
from core.signal_families import get_active_signals, POOL_NAMES
from core.adaptive_thresholds import AdaptiveThresholdRegistry
from core.variance_weighted_sizing import (
    variance_weighted_blend,
    variance_adjusted_kelly,
    variance_weighted_pipeline,
    compute_signal_variance,
)
from core.luck_elimination import (
    wire_dependencies as luck_wire_dependencies,
    run_luck_elimination,
    print_luck_report,
)

METAR_DB = os.path.join(str(REPO_ROOT), "data", "metar_backfill.db")
SETTLEMENTS_DB = sweep_config.DB_PATH
SWEEP_DIR = sweep_config.SWEEP_DIR
RESULTS_DIR = Path(str(REPO_ROOT)) / "data"
os.makedirs(SWEEP_DIR, exist_ok=True)

KALSHI_REAL_FEE_RATE = 0.07
MIN_TRADES_REPORT = 10
MIN_TRADES_CALIBRATE = 20
MIN_TRADES_REPORT = 10
MIN_TRADES_CALIBRATE = 20

# Trajectory Confirmation Gate (lazy init)
_TRAJECTORY_GATE_ENABLED: bool = False
_TRAJECTORY_GATE: Optional[TrajectoryConfirmationGate] = None

# Settlement Execution Gate (lazy init)
_SETTLEMENT_GATE_ENABLED: bool = False
_SETTLEMENT_GATE: Optional[SettlementExecutionGate] = None

def set_trajectory_gate(enabled: bool):
    global _TRAJECTORY_GATE, _TRAJECTORY_GATE_ENABLED
    _TRAJECTORY_GATE_ENABLED = enabled
    if enabled:
        _TRAJECTORY_GATE = TrajectoryConfirmationGate()

def get_trajectory_gate() -> Optional[TrajectoryConfirmationGate]:
    return _TRAJECTORY_GATE

# Trajectory Lane (heavy informant — lazy init)
_TRAJECTORY_LANE_ENABLED: bool = False
_TRAJECTORY_LANE: Optional[TrajectoryLane] = None

def set_trajectory_lane(enabled: bool):
    global _TRAJECTORY_LANE, _TRAJECTORY_LANE_ENABLED
    _TRAJECTORY_LANE_ENABLED = enabled
    if enabled:
        _TRAJECTORY_LANE = TrajectoryLane()

def get_trajectory_lane() -> Optional[TrajectoryLane]:
    return _TRAJECTORY_LANE

def set_settlement_gate(enabled: bool):
    global _SETTLEMENT_GATE, _SETTLEMENT_GATE_ENABLED
    _SETTLEMENT_GATE_ENABLED = enabled
    if enabled:
        _SETTLEMENT_GATE = SettlementExecutionGate()

def get_settlement_gate() -> Optional[SettlementExecutionGate]:
    return _SETTLEMENT_GATE

# LossLimiter registry (keyed by (signal_name, station))
_LOSS_LIMITERS: Dict[Tuple[str, str], LossLimiter] = {}

def _get_loss_limiter(signal_name: str, station: str) -> LossLimiter:
    """Get or create a LossLimiter for a (signal, station) pair."""
    key = (signal_name, station)
    if key not in _LOSS_LIMITERS:
        _LOSS_LIMITERS[key] = LossLimiter()
    return _LOSS_LIMITERS[key]

def _record_trade_outcome(signal_name: str, station: str, trade: dict) -> None:
    """Record a trade outcome in the appropriate LossLimiter."""
    limiter = _get_loss_limiter(signal_name, station)
    was_loss = not trade.get("correct", False)
    magnitude = trade.get("net_pnl", 0.0)
    limiter.record_outcome(was_loss=was_loss, magnitude=magnitude)

# Agreement Gate (N-of-M consensus filter)
_AGREEMENT_GATE_ENABLED: bool = False
_AGREEMENT_GATE: Optional[AgreementGate] = None
_AGREEMENT_N: int = 3
_AGREEMENT_M: int = 9

def set_agreement_gate(enabled: bool, n_required: int = 3, m_total: int = 9):
    global _AGREEMENT_GATE, _AGREEMENT_GATE_ENABLED, _AGREEMENT_N, _AGREEMENT_M
    _AGREEMENT_GATE_ENABLED = enabled
    _AGREEMENT_N = n_required
    _AGREEMENT_M = m_total
    if enabled:
        _AGREEMENT_GATE = AgreementGate(n_required=n_required, m_total=m_total)

def get_agreement_gate() -> Optional[AgreementGate]:
    return _AGREEMENT_GATE

# Spatual coherence toggle (set from --spatial-coherence flag)
_SPATIAL_COHERENCE_ENABLED: bool = False
_SPATIAL_COHERENCE_GATE = None  # lazy init
_SPATIAL_COHERENCE_TRACKER: dict = {}

# Adaptive Thresholds (Bayesian Beta-Bernoulli)
_ADAPTIVE_THRESHOLDS_ENABLED: bool = False
_ADAPTIVE_REGISTRY: Optional[AdaptiveThresholdRegistry] = None

# Station Skill Gate (lazy init)
_STATION_SKILL_GATE_ENABLED: bool = False
_STATION_SKILL_GATE: Optional[StationSkillGate] = None
_STATION_SKILL_GATE_BSS_THRESHOLD: float = 0.0
def set_adaptive_thresholds(enabled: bool):
    global _ADAPTIVE_REGISTRY, _ADAPTIVE_THRESHOLDS_ENABLED
    _ADAPTIVE_THRESHOLDS_ENABLED = enabled
    if enabled:
        _ADAPTIVE_REGISTRY = AdaptiveThresholdRegistry()
    else:
        _ADAPTIVE_REGISTRY = None
def get_adaptive_registry() -> Optional[AdaptiveThresholdRegistry]:
    return _ADAPTIVE_REGISTRY
def set_spatial_coherence(enabled: bool):
    global _SPATIAL_COHERENCE_ENABLED, _SPATIAL_COHERENCE_GATE
    _SPATIAL_COHERENCE_ENABLED = enabled
    if enabled and _SPATIAL_COHERENCE_GATE is None:
        from core.spatial_coherence import SpatialCoherenceGate
        _SPATIAL_COHERENCE_GATE = SpatialCoherenceGate()
def get_spatial_coherence() -> bool:
    return _SPATIAL_COHERENCE_ENABLED

def set_station_skill_gate(enabled: bool, bss_threshold: float = 0.0):
    global _STATION_SKILL_GATE, _STATION_SKILL_GATE_ENABLED, _STATION_SKILL_GATE_BSS_THRESHOLD
    _STATION_SKILL_GATE_ENABLED = enabled
    _STATION_SKILL_GATE_BSS_THRESHOLD = bss_threshold
    if enabled and _STATION_SKILL_GATE is None:
        _STATION_SKILL_GATE = StationSkillGate(METAR_DB)

def get_station_skill_gate_enabled() -> bool:
    return _STATION_SKILL_GATE_ENABLED

def set_calibration_mode(mode: str):
    global _CALIBRATION_MODE
    _CALIBRATION_MODE = mode

def get_calibration_mode() -> str:
    return _CALIBRATION_MODE

FT_NONE, FT_TAKER, FT_TAKER_SLIPPAGE, FT_MAKER = range(4)
STATIONS = list(_DEF_STATIONS)
_get_thread_ident = threading.get_ident
def _safe_import_signal(rel_module, class_name):
    """Import a signal using the proper package path."""
    try:
        full = f"core.signals.{rel_module}"
        import importlib
        mod = importlib.import_module(full)
        return getattr(mod, class_name, None)
    except Exception:
        return None
def _try_instantiate(cls, db_path=METAR_DB, **extra_kw):
    if cls is None:
        return None
    # Try with all kwargs first, then fall back
    for kw in [{**extra_kw, "db_path": db_path}, {"db_path": db_path}, {}]:
        try:
            return cls(**kw)
        except (TypeError, Exception):
            continue
    return None
def build_signal_registry():
    sig_dir = os.path.join(str(REPO_ROOT), "core", "signals")
    nwp_db = os.path.join(str(REPO_ROOT), "data", "nwp_forecasts.db")
    registry = {}

    def _reg(name, rel_module, class_name, **extra):
        cls = _safe_import_signal(rel_module, class_name)
        if cls is None:
            registry[name] = None
            return
        kw = dict(extra)
        if "db_path" not in kw:
            kw["db_path"] = METAR_DB
        inst = _try_instantiate(cls, **kw)
        registry[name] = inst

    def _reg_func(name, rel_module, func_name):
        try:
            full = f"core.signals.{rel_module}"
            import importlib
            mod = importlib.import_module(full)
            registry[name] = getattr(mod, func_name, None)
        except Exception:
            registry[name] = None

    # Radiational Cooling (LOW only signal)
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
    _reg("metar_trend", "metar_trend_signal", "MetarTrendSignal",
         db_path=METAR_DB)
    _reg("cross_model_divergence", "cross_model_divergence_signal", "CrossModelDivergenceSignal",
         nwp_db_path=nwp_db, metar_db_path=METAR_DB)
    _reg("nwp_analog", "nwp_analog_signal", "NwpAnalogSignal", db_path=METAR_DB, nwp_db_path=nwp_db)

    # Dead/Retired (24)
    # KILLED (Big Sweep 2026-08-07): persistence & simple_trend are ρ=1.0 identical
    # _reg("persistence", "persistence_signal", "PersistenceSignal")
    # _reg("simple_trend", "simple_trend_signal", "SimpleTrendSignal")
    _reg("wind_direction_shift", "wind_direction_shift", "WindDirectionShiftSignal")
    _reg("corrected_pressure_delta", "dual_polarity_signal", "DualPolaritySignal", wrapped_signal=None)
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

    # ── A1: Combined 82-member meta-signal ──
    # Equal weighting (default)
    _reg("eighty_two_member_ensemble", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(str(REPO_ROOT), "data", "gefs_archive.db"),
         tigge_db=os.path.join(str(REPO_ROOT), "data", "tigge_archive.db"))
    
    # ECE‑inverse weighting
    _reg("eighty_two_member_ensemble_ece", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(str(REPO_ROOT), "data", "gefs_archive.db"),
         tigge_db=os.path.join(str(REPO_ROOT), "data", "tigge_archive.db"),
         weighting_mode='ece_inverse')
    
    # Member‑pooling weighting
    _reg("eighty_two_member_ensemble_pooled", "eighty_two_member_ensemble_signal", "EightyTwoMemberEnsembleSignal",
         gefs_db=os.path.join(str(REPO_ROOT), "data", "gefs_archive.db"),
         tigge_db=os.path.join(str(REPO_ROOT), "data", "tigge_archive.db"),
         weighting_mode='member_pooling')

    return registry
# ═══════════════════════════════════════════════════════════════
# 2. DATA CACHE
# ═══════════════════════════════════════════════════════════════
class DataCache:
    _settlements = None
    _market_types = {}
    _metar_data = {}
    _date_range = ("", "")

    @classmethod
    def get_settlements(cls):
        if cls._settlements is not None:
            return cls._settlements

        rows = query_db(SETTLEMENTS_DB,
                        "SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
        result = defaultdict(dict)
        for r in rows:
            if r['kalshi_temp'] is not None:
                result[r['station']][r['target_date']] = float(r['kalshi_temp'])
        cls._settlements = dict(result)

        try:
            rows2 = query_db(SETTLEMENTS_DB,
                             "SELECT station, target_date, market_type FROM kalshi_settlements")
            cls._market_types = {f"{r['station']}|{r['target_date']}": r['market_type'] for r in rows2}
        except Exception:
            cls._market_types = {}

        all_dates = set()
        for sd in cls._settlements.values():
            all_dates.update(sd.keys())
        if all_dates:
            sd = sorted(all_dates)
            cls._date_range = (sd[0], sd[-1])
        return cls._settlements

    @classmethod
    def get_market_type(cls, station, date_str):
        cls.get_settlements()
        return cls._market_types.get(f"{station}|{date_str}", "HIGH")

    @classmethod
    def get_date_range(cls):
        cls.get_settlements()
        return cls._date_range

    @classmethod
    def get_metar_data(cls, station):
        if station in cls._metar_data:
            return cls._metar_data[station]
        rows = query_db(METAR_DB, """
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                   AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                   AVG(pressure_mb) as pressure,
                   AVG(wind_speed_kt) as wind_speed,
                   AVG(wind_direction_deg) as wind_dir
            FROM metar_observations
            WHERE station = ? AND temp_f IS NOT NULL AND temp_f < 200
            GROUP BY date_utc ORDER BY date_utc ASC
        """, (station,))
        days = []
        for r in rows:
            days.append({'date': r['date_utc'], 'high': r['high'], 'low': r['low'], 'dewpoint': r['dewpoint'],
                         'temp': r['temp'], 'pressure': r['pressure'], 'wind_speed': r['wind_speed'], 'wind_dir': r['wind_dir']})
        cls._metar_data[station] = days
        return days
# ═══════════════════════════════════════════════════════════════
# 3. LHS WITH 18 PARAMS
# ═══════════════════════════════════════════════════════════════
def _latin_hypercube(n_samples, n_dims, seed=42):
    rng = np.random.default_rng(seed)
    try:
        from scipy.stats import qmc
        sampler = qmc.LatinHypercube(d=n_dims, seed=seed)
        return sampler.random(n=n_samples)
    except ImportError:
        rng = np.random.RandomState(seed)
        s = np.zeros((n_samples, n_dims))
        for j in range(n_dims):
            perm = rng.permutation(n_samples)
            s[perm, j] = (np.arange(n_samples) + rng.uniform(size=n_samples)) / n_samples
        return s
def generate_sweep_configs(n_configs):
    cont_params = {
        "kl_dro_lambda": (0.0, 1.0),
        "edge_threshold": (0.001, 0.2),
        "kelly_fraction": (0.1, 1.0),   # MAX 1.0 — never over-bet beyond optimal Kelly
        "entry_price_min": (0.01, 0.5),
        "slippage_budget": (0.0, 0.01),
        "fee_deduction": (0.0, 1.0),
        "confidence_floor": (0.5, 0.95),
        "entry_price_max": (0.5, 0.99),
    }
    cont_names = list(cont_params.keys())
    n_cont = len(cont_names)
    cat_params = {
        "fee_type": [0,1,2,3],
        "member_weighting": [0,1,2,3],
        "validation_type": [0,1,2,3],
        "market_type_split": [0,1,2],
        "station_pool_size": [10,15,20],
        "position_sizing_model": [0,1,2],
        "stop_loss_kind": [0,1,2],
        "agreement_n": [2,3,4,5],
        "agreement_m": [5,7,9,11],
        "calibration_mode": [0,1,2,3],  # NEW: 0=platt, 1=bma, 2=emos, 3=both
    }
    cat_items = list(cat_params.items())
    n_cat = sum(len(v) for v in cat_params.values())
    n_dims = n_cont + n_cat
    random.seed(42)
    np.random.seed(42)  # Fixed seed for reproducible LHS config generation
    samples = _latin_hypercube(n_configs, n_dims, seed=42)
    configs = []
    for i in range(n_configs):
        s = samples[i]
        cfg = {}
        idx = 0
        for pn in cont_names:
            lo, hi = cont_params[pn]
            cfg[pn] = round(lo + s[idx] * (hi - lo), 6)
            idx += 1
        log_lo, log_hi = math.log10(10), math.log10(1000)   # max 1000 contracts — sane binary options cap
        cfg["max_contracts"] = int(10 ** (log_lo + s[idx] * (log_hi - log_lo)))
        idx += 1
        for cat_name, cat_vals in cat_items:
            cat_idx = min(int(s[idx] * len(cat_vals)), len(cat_vals) - 1)
            cfg[cat_name] = cat_vals[cat_idx]
            idx += 1
        ft_label = ["none", "taker_only", "taker_and_slippage", "maker"]
        cfg["fee_type_label"] = ft_label[cfg["fee_type"]]
        cal_mode_map = {0: "platt", 1: "bma", 2: "emos", 3: "both"}
        cfg["calibration_mode_label"] = cal_mode_map[cfg["calibration_mode"]]
        cfg["config_id"] = i
        configs.append(cfg)
    return configs
# ═══════════════════════════════════════════════════════════════
# 3b. LHS — Phase 2 (30 meta-parameters)
# ═══════════════════════════════════════════════════════════════════════

def generate_meta_configs(n_configs: int) -> List[Dict[str, Any]]:
    """Generate LHS configs over ~30 meta-parameters for Phase 2.

    Categories:
      - Gates (12 params)
      - Levers (5)
      - Lanes (3)
      - Modulators (4)
    """
    cont_params = {
        "agreement_n": (1.0, 9.0),
        "agreement_m": (3.0, 15.0),
        "settlement_cooldown_hours": (0.0, 72.0),
        "bss_threshold": (-0.5, 0.5),
        "adaptive_prior_alpha": (1.0, 10.0),
        "adaptive_prior_beta": (1.0, 10.0),
        "spread_threshold": (0.01, 0.10),
        "trajectory_min_analogs": (10.0, 100.0),
        "kelly_fraction": (0.1, 1.0),
        "capital_base": (10000.0, 200000.0),
        "variance_penalty": (0.5, 2.0),
        "drawdown_halve_at": (0.05, 0.20),
        "drawdown_stop_at": (0.15, 0.40),
        "max_per_station": (0.10, 0.50),
        "max_per_signal": (0.05, 0.30),
        "trajectory_lane_weight": (0.05, 0.30),
        "production_min_accuracy": (0.50, 0.75),
        "production_min_trades": (50.0, 500.0),
        "production_min_sharpe": (0.5, 2.0),
    }
    cont_names = list(cont_params.keys())
    n_cont = len(cont_names)
    cat_params = {
        "agreement_gate_enabled": [0, 1],
        "settlement_gate_enabled": [0, 1],
        "production_gate_enabled": [0, 1],
        "station_skill_gate_enabled": [0, 1],
        "adaptive_thresholds_enabled": [0, 1],
        "liquidity_gate_enabled": [0, 1],
        "trajectory_gate_enabled": [0, 1],
        "variance_sizing_enabled": [0, 1],
        "goldilocks_lane_enabled": [0, 1],
        "trajectory_lane_enabled": [0, 1],
        "spatial_coherence_enabled": [0, 1],
        "fusion_mode": [0, 1, 2, 3],
        "calibration_mode": [0, 1, 2, 3],
    }
    cat_items = list(cat_params.items())
    n_cat = sum(len(v) for v in cat_params.values())
    n_dims = n_cont + n_cat
    samples = _latin_hypercube(n_configs, n_dims)
    cal_mode_labels = {0: "platt", 1: "bma", 2: "emos", 3: "both"}
    fusion_mode_labels = {0: "none", 1: "uwc", 2: "majority", 3: "weighted"}
    configs = []
    for i in range(n_configs):
        s = samples[i]
        cfg: Dict[str, Any] = {}
        idx = 0
        for pn in cont_names:
            lo, hi = cont_params[pn]
            val = lo + s[idx] * (hi - lo)
            if pn in ("agreement_n", "agreement_m", "trajectory_min_analogs",
                      "production_min_trades"):
                cfg[pn] = int(round(val))
            elif pn in ("adaptive_prior_alpha", "adaptive_prior_beta"):
                cfg[pn] = int(round(val))
            else:
                cfg[pn] = round(val, 6)
            idx += 1
        for cat_name, cat_vals in cat_items:
            cat_idx = min(int(s[idx] * len(cat_vals)), len(cat_vals) - 1)
            cfg[cat_name] = cat_vals[cat_idx]
            idx += 1
        cfg["calibration_mode_label"] = cal_mode_labels.get(cfg["calibration_mode"], "platt")
        cfg["fusion_mode_label"] = fusion_mode_labels.get(cfg["fusion_mode"], "none")
        cfg["config_id"] = i
        configs.append(cfg)
    return configs
# ═══════════════════════════════════════════════════════════════
# 4. TRADE SIMULATION
# ═══════════════════════════════════════════════════════════════

# Platt calibration pipeline singleton
_platt_pipeline: Optional[PlattCalibrationPipeline] = None

def get_platt_pipeline() -> PlattCalibrationPipeline:
    global _platt_pipeline
    if _platt_pipeline is None:
        path = os.path.join(str(REPO_ROOT), "data", "platt_calibration.json")
        if not os.path.exists(path):
            print(f"  ⚠️  No calibration file at {path}. Using identity calibrator.")
            _platt_pipeline = PlattCalibrationPipeline()
            _platt_pipeline.refitted = True
            return _platt_pipeline
        try:
            _platt_pipeline = PlattCalibrationPipeline.load(path)
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠️  Failed to load calibration ({e}). Using identity calibrator.")
            _platt_pipeline = PlattCalibrationPipeline()
            _platt_pipeline.refitted = True
    return _platt_pipeline

# Bias correction singleton
_bias_data: Optional[dict] = None

def get_bias_corrections() -> dict:
    """Load bias corrections once and cache globally."""
    global _bias_data
    if _bias_data is None:
        bias_path = os.path.join(str(REPO_ROOT), "data", "ensemble_fraction_bias_corrections.json")
        _bias_data = load_bias_corrections(bias_path)
        if _bias_data:
            n_stations = len(_bias_data.get("bias_table", {}))
            n_pairs = _bias_data.get("matched_pairs", 0)
            print(f"  Bias corrections loaded: {n_stations} stations, {n_pairs} matched pairs")
        else:
            print("  ⚠️  Bias corrections file not found — all corrections will be identity")
    return _bias_data


def _latin_hypercube(n_samples, n_dims, seed=42):
    rng = np.random.default_rng(seed)
    try:
        from scipy.stats import qmc
        sampler = qmc.LatinHypercube(d=n_dims, seed=seed)
        return sampler.random(n=n_samples)
    except ImportError:
        rng = np.random.RandomState(seed)
        s = np.zeros((n_samples, n_dims))
        for j in range(n_dims):
            perm = rng.permutation(n_samples)
            s[perm, j] = (np.arange(n_samples) + rng.uniform(size=n_samples)) / n_samples
        return s
def generate_sweep_configs(n_configs):
    cont_params = {
        "kl_dro_lambda": (0.0, 1.0),
        "edge_threshold": (0.001, 0.2),
        "kelly_fraction": (0.1, 1.0),   # MAX 1.0 — never over-bet beyond optimal Kelly
        "entry_price_min": (0.01, 0.5),
        "slippage_budget": (0.0, 0.01),
        "fee_deduction": (0.0, 1.0),
        "confidence_floor": (0.5, 0.95),
        "entry_price_max": (0.5, 0.99),
    }
    cont_names = list(cont_params.keys())
    n_cont = len(cont_names)
    cat_params = {
        "fee_type": [0,1,2,3],
        "member_weighting": [0,1,2,3],
        "validation_type": [0,1,2,3],
        "market_type_split": [0,1,2],
        "station_pool_size": [10,15,20],
        "position_sizing_model": [0,1,2],
        "stop_loss_kind": [0,1,2],
        "agreement_n": [2,3,4,5],
        "agreement_m": [5,7,9,11],
        "calibration_mode": [0,1,2,3],  # NEW: 0=platt, 1=bma, 2=emos, 3=both
    }
    cat_items = list(cat_params.items())
    n_cat = sum(len(v) for v in cat_params.values())
    n_dims = n_cont + n_cat
    random.seed(42)
    np.random.seed(42)  # Fixed seed for reproducible LHS config generation
    samples = _latin_hypercube(n_configs, n_dims, seed=42)
    configs = []
    for i in range(n_configs):
        s = samples[i]
        cfg = {}
        idx = 0
        for pn in cont_names:
            lo, hi = cont_params[pn]
            cfg[pn] = round(lo + s[idx] * (hi - lo), 6)
            idx += 1
        log_lo, log_hi = math.log10(10), math.log10(1000)   # max 1000 contracts — sane binary options cap
        cfg["max_contracts"] = int(10 ** (log_lo + s[idx] * (log_hi - log_lo)))
        idx += 1
        for cat_name, cat_vals in cat_items:
            cat_idx = min(int(s[idx] * len(cat_vals)), len(cat_vals) - 1)
            cfg[cat_name] = cat_vals[cat_idx]
            idx += 1
        ft_label = ["none", "taker_only", "taker_and_slippage", "maker"]
        cfg["fee_type_label"] = ft_label[cfg["fee_type"]]
        cal_mode_map = {0: "platt", 1: "bma", 2: "emos", 3: "both"}
        cfg["calibration_mode_label"] = cal_mode_map[cfg["calibration_mode"]]
        cfg["config_id"] = i
        configs.append(cfg)
    return configs
# ═══════════════════════════════════════════════════════════════
# 3b. LHS — Phase 2 (30 meta-parameters)
# ═══════════════════════════════════════════════════════════════════════

def generate_meta_configs(n_configs: int) -> List[Dict[str, Any]]:
    """Generate LHS configs over ~30 meta-parameters for Phase 2.

    Categories:
      - Gates (12 params)
      - Levers (5)
      - Lanes (3)
      - Modulators (4)
    """
    cont_params = {
        "agreement_n": (1.0, 9.0),
        "agreement_m": (3.0, 15.0),
        "settlement_cooldown_hours": (0.0, 72.0),
        "bss_threshold": (-0.5, 0.5),
        "adaptive_prior_alpha": (1.0, 10.0),
        "adaptive_prior_beta": (1.0, 10.0),
        "spread_threshold": (0.01, 0.10),
        "trajectory_min_analogs": (10.0, 100.0),
        "kelly_fraction": (0.1, 1.0),
        "capital_base": (10000.0, 200000.0),
        "variance_penalty": (0.5, 2.0),
        "drawdown_halve_at": (0.05, 0.20),
        "drawdown_stop_at": (0.15, 0.40),
        "max_per_station": (0.10, 0.50),
        "max_per_signal": (0.05, 0.30),
        "trajectory_lane_weight": (0.05, 0.30),
        "production_min_accuracy": (0.50, 0.75),
        "production_min_trades": (50.0, 500.0),
        "production_min_sharpe": (0.5, 2.0),
    }
    cont_names = list(cont_params.keys())
    n_cont = len(cont_names)
    cat_params = {
        "agreement_gate_enabled": [0, 1],
        "settlement_gate_enabled": [0, 1],
        "production_gate_enabled": [0, 1],
        "station_skill_gate_enabled": [0, 1],
        "adaptive_thresholds_enabled": [0, 1],
        "liquidity_gate_enabled": [0, 1],
        "trajectory_gate_enabled": [0, 1],
        "variance_sizing_enabled": [0, 1],
        "goldilocks_lane_enabled": [0, 1],
        "trajectory_lane_enabled": [0, 1],
        "spatial_coherence_enabled": [0, 1],
        "fusion_mode": [0, 1, 2, 3],
        "calibration_mode": [0, 1, 2, 3],
    }
    cat_items = list(cat_params.items())
    n_cat = sum(len(v) for v in cat_params.values())
    n_dims = n_cont + n_cat
    samples = _latin_hypercube(n_configs, n_dims)
    cal_mode_labels = {0: "platt", 1: "bma", 2: "emos", 3: "both"}
    fusion_mode_labels = {0: "none", 1: "uwc", 2: "majority", 3: "weighted"}
    configs = []
    for i in range(n_configs):
        s = samples[i]
        cfg: Dict[str, Any] = {}
        idx = 0
        for pn in cont_names:
            lo, hi = cont_params[pn]
            val = lo + s[idx] * (hi - lo)
            if pn in ("agreement_n", "agreement_m", "trajectory_min_analogs",
                      "production_min_trades"):
                cfg[pn] = int(round(val))
            elif pn in ("adaptive_prior_alpha", "adaptive_prior_beta"):
                cfg[pn] = int(round(val))
            else:
                cfg[pn] = round(val, 6)
            idx += 1
        for cat_name, cat_vals in cat_items:
            cat_idx = min(int(s[idx] * len(cat_vals)), len(cat_vals) - 1)
            cfg[cat_name] = cat_vals[cat_idx]
            idx += 1
        cfg["calibration_mode_label"] = cal_mode_labels.get(cfg["calibration_mode"], "platt")
        cfg["fusion_mode_label"] = fusion_mode_labels.get(cfg["fusion_mode"], "none")
        cfg["config_id"] = i
        configs.append(cfg)
    return configs
# ═══════════════════════════════════════════════════════════════
# 4. TRADE SIMULATION
# ═══════════════════════════════════════════════════════════════

# Platt calibration pipeline singleton
_platt_pipeline: Optional[PlattCalibrationPipeline] = None

def get_platt_pipeline() -> PlattCalibrationPipeline:
    global _platt_pipeline
    if _platt_pipeline is None:
        path = os.path.join(str(REPO_ROOT), "data", "platt_calibration.json")
        if not os.path.exists(path):
            print(f"  ⚠️  No calibration file at {path}. Using identity calibrator.")
            _platt_pipeline = PlattCalibrationPipeline()
            _platt_pipeline.refitted = True
            return _platt_pipeline
        try:
            _platt_pipeline = PlattCalibrationPipeline.load(path)
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠️  Failed to load calibration ({e}). Using identity calibrator.")
            _platt_pipeline = PlattCalibrationPipeline()
            _platt_pipeline.refitted = True
    return _platt_pipeline

# Bias correction singleton
_bias_data: Optional[dict] = None

def get_bias_corrections() -> dict:
    """Load bias corrections once and cache globally."""
    global _bias_data
    if _bias_data is None:
        bias_path = os.path.join(str(REPO_ROOT), "data", "ensemble_fraction_bias_corrections.json")
        _bias_data = load_bias_corrections(bias_path)
        if _bias_data:
            n_stations = len(_bias_data.get("bias_table", {}))
            n_pairs = _bias_data.get("matched_pairs", 0)
            print(f"  Bias corrections loaded: {n_stations} stations, {n_pairs} matched pairs")
        else:
            print("  ⚠️  Bias corrections file not found — all corrections will be identity")
    return _bias_data