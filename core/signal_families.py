"""
signal_families.py — Signal Taxonomy Registry for Multi-Signal Fusion

Maps all 39 registered signals into families, pools, and metadata per the
Multi-Signal Fusion Spec (docs/plans/MULTI-SIGNAL-FUSION-SPEC.md).

Family A: Temperature Prediction (10 signals) — fused in Layer 1
Family B: Temperature Modulators (8 signals)  — modulate Layer 1 output in Layer 2
Family C: Microstructure / Market (5 signals) — enter Layer 3 (bet sizing)
Family D: Regime / State (6 signals)          — precision modulators (Layer 1/Layer 4)
Family E: Dead / Killed / Orphaned (10 signals) — excluded from fusion
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Signal pool definitions — hierarchical clustering for Layer 1
# ══════════════════════════════════════════════════════════════════════

@dataclass
class SignalEntry:
    """A single signal in the taxonomy registry."""
    module_name: str                    # e.g. 'gaussian_signal'
    family: str                         # A, B, C, D, E
    pool: Optional[str]                 # 'gefs', 'ecmwf', 'nwp_direct', 'climatology',
                                        # 'hrrr', 'metar_nowcast', 'market' (for Family A),
                                        # or None for non-temperature families
    description: str = ""
    status: str = "ACTIVE"              # ACTIVE, KILLED, ORPHANED, GATED
    timescale: str = "daily"            # daily, sub-daily, hourly, intraday
    baseline_correlation_to_gefs: float = 0.0
    freshness_hours: float = 6.0        # precision decay half-life
    module_path: str = "core.signals"   # import path


# ══════════════════════════════════════════════════════════════════════
# The signal taxonomy (from MULTI-SIGNAL-FUSION-SPEC.md §1)
# ══════════════════════════════════════════════════════════════════════

SIGNAL_REGISTRY: Dict[str, SignalEntry] = {
    # ── Family A: Temperature Prediction (10 signals) ──────────────
    "eighty_two_member_ensemble_signal": SignalEntry(
        module_name="eighty_two_member_ensemble_signal",
        family="A", pool="gefs",
        description="Ensemble fraction (T > B) — 82-member GEFS",
        baseline_correlation_to_gefs=1.0,
        freshness_hours=6.0,
    ),
    "gaussian_signal": SignalEntry(
        module_name="gaussian_signal",
        family="A", pool="gefs",
        description="P(T > B) via Gaussian Z-score",
        baseline_correlation_to_gefs=0.85,
        freshness_hours=6.0,
    ),
    "gaussian_v2_signal": SignalEntry(
        module_name="gaussian_v2_signal",
        family="A", pool="gefs",
        description="P(T > B) via refined Gaussian",
        baseline_correlation_to_gefs=0.90,
        freshness_hours=6.0,
    ),
    "calendar_climatology_signal": SignalEntry(
        module_name="calendar_climatology_signal",
        family="A", pool="climatology",
        description="Historical frequency P(T > B) from climatology",
        baseline_correlation_to_gefs=0.40,
        freshness_hours=6.0,
    ),
    "ecmwf_bias_corrected_signal": SignalEntry(
        module_name="ecmwf_bias_corrected_signal",
        family="A", pool="ecmwf",
        description="ECMWF fraction (T > B), bias-corrected",
        baseline_correlation_to_gefs=0.70,
        freshness_hours=6.0,
    ),
    "nwp_direct_signal": SignalEntry(
        module_name="nwp_direct_signal",
        family="A", pool="nwp_direct",
        description="GFS/IFS/ICON/GEM multi-model mean",
        baseline_correlation_to_gefs=0.65,
        freshness_hours=6.0,
    ),
    "nwp_analog_signal": SignalEntry(
        module_name="nwp_analog_signal",
        family="A", pool="nwp_direct",
        description="k-NN historical analog (KILLED — 32.63% accuracy)",
        status="KILLED",
        baseline_correlation_to_gefs=0.32,
        freshness_hours=6.0,
    ),
    "hrrr_bias_corrected_signal": SignalEntry(
        module_name="hrrr_bias_corrected_signal",
        family="A", pool="hrrr",
        description="HRRR 3km convection-resolving extremes",
        baseline_correlation_to_gefs=0.60,
        freshness_hours=3.0,
    ),
    "spread_based_entry_signal": SignalEntry(
        module_name="spread_based_entry_signal",
        family="A", pool="market",
        description="Market spread → settlement convergence (intraday)",
        timescale="intraday",
        baseline_correlation_to_gefs=0.20,
        freshness_hours=1.0,
    ),
    "metar_nowcast_signal": SignalEntry(
        module_name="metar_nowcast_signal",
        family="A", pool="metar_nowcast",
        description="Current METAR obs → daily extreme nowcast (hourly)",
        timescale="hourly",
        baseline_correlation_to_gefs=0.50,
        freshness_hours=1.5,
    ),

    # ── Family B: Temperature Modulators (8 signals) ──────────────
    "cross_model_divergence_signal": SignalEntry(
        module_name="cross_model_divergence_signal",
        family="B", pool=None,
        description="Multi-model divergence → confidence modifier",
        freshness_hours=6.0,
    ),
    "frontal_detector_signal": SignalEntry(
        module_name="frontal_detector_signal",
        family="B", pool=None,
        description="Frontal passage detection → rapid change signal",
        freshness_hours=3.0,
    ),
    "frontal_passage_detector": SignalEntry(
        module_name="frontal_passage_detector",
        family="B", pool=None,
        description="Frontal passage detector (alternative algo)",
        freshness_hours=3.0,
    ),
    "frontal_passage_intraday_signal": SignalEntry(
        module_name="frontal_passage_intraday_signal",
        family="B", pool=None,
        description="Intraday front timing",
        timescale="intraday",
        freshness_hours=1.0,
    ),
    "frontal_passage_nowcast_signal": SignalEntry(
        module_name="frontal_passage_nowcast_signal",
        family="B", pool=None,
        description="Nowcast front position",
        freshness_hours=1.5,
    ),
    "temperature_advection_signal": SignalEntry(
        module_name="temperature_advection_signal",
        family="B", pool=None,
        description="Warm/cold temperature advection",
        freshness_hours=3.0,
    ),
    "pressure_delta_signal": SignalEntry(
        module_name="pressure_delta_signal",
        family="B", pool=None,
        description="Pressure delta → temperature trend",
        freshness_hours=3.0,
    ),
    "pressure_tendency_signal": SignalEntry(
        module_name="pressure_tendency_signal",
        family="B", pool=None,
        description="Pressure tendency (different timescale)",
        freshness_hours=3.0,
    ),

    # ── Family C: Microstructure / Market Signals (5 signals) ─────
    "volume_momentum_signal": SignalEntry(
        module_name="volume_momentum_signal",
        family="C", pool=None,
        description="Order flow anomaly detection",
        timescale="intraday",
        freshness_hours=1.0,
    ),
    "settlement_arbitrage_signal": SignalEntry(
        module_name="settlement_arbitrage_signal",
        family="C", pool=None,
        description="Price vs fair value arbitrage",
        timescale="intraday",
        freshness_hours=1.0,
    ),
    "spike_reversion_signal": SignalEntry(
        module_name="spike_reversion_signal",
        family="C", pool=None,
        description="Price overshoot reversion",
        timescale="intraday",
        freshness_hours=1.0,
    ),
    "fogr_reversion_signal": SignalEntry(
        module_name="fogr_reversion_signal",
        family="C", pool=None,
        description="FOGR reversion pattern",
        timescale="intraday",
        freshness_hours=1.0,
    ),
    "simple_trend_signal": SignalEntry(
        module_name="simple_trend_signal",
        family="C", pool=None,
        description="Price momentum trend",
        timescale="intraday",
        freshness_hours=1.0,
    ),

    # ── Family D: Regime / State Signals (6 signals) ──────────────
    "regime_signal": SignalEntry(
        module_name="regime_signal",
        family="D", pool=None,
        description="Weather regime (frontal/stagnant/transitional)",
        freshness_hours=6.0,
    ),
    "dewpoint_depression_modulator": SignalEntry(
        module_name="dewpoint_depression_modulator",
        family="D", pool=None,
        description="Humidity state → cloud cover confidence modifier",
        freshness_hours=3.0,
    ),
    "cloud_cover_index_signal": SignalEntry(
        module_name="cloud_cover_index_signal",
        family="D", pool=None,
        description="Cloud cover proxy from METAR",
        freshness_hours=1.5,
    ),
    "feels_like_delta_signal": SignalEntry(
        module_name="feels_like_delta_signal",
        family="D", pool=None,
        description="Heat index divergence from forecast",
        freshness_hours=3.0,
    ),
    "wind_direction_shift": SignalEntry(
        module_name="wind_direction_shift",
        family="D", pool=None,
        description="Wind shift detection → direction qualifier",
        freshness_hours=1.5,
    ),
    "ai_composite_signal": SignalEntry(
        module_name="ai_composite_signal",
        family="D", pool=None,
        description="ML composite signal (GATED — off by default)",
        status="GATED",
        freshness_hours=6.0,
    ),

    # ── Family E: Dead / Killed / Orphaned (10 signals) ───────────
    "persistence_signal": SignalEntry(
        module_name="persistence_signal",
        family="E", pool=None,
        description="KILLED — 48.31% accuracy",
        status="KILLED",
    ),
    "goldilocks_signal": SignalEntry(
        module_name="goldilocks_signal",
        family="E", pool=None,
        description="KILLED — 49.85% negative EV",
        status="KILLED",
    ),
    "late_day_momentum": SignalEntry(
        module_name="late_day_momentum",
        family="E", pool=None,
        description="KILLED — 48.31%",
        status="KILLED",
        module_path="core",
    ),
    "nwp_analog_signal": SignalEntry(
        module_name="nwp_analog_signal",
        family="E", pool=None,
        description="KILLED — 32.63% (also in Family A)",
        status="KILLED",
    ),
    "intraday_metar_confirmation": SignalEntry(
        module_name="intraday_metar_confirmation",
        family="E", pool=None,
        description="ORPHANED — replaced by METAR nowcast",
        status="ORPHANED",
        module_path="core",
    ),
    "intraday_metar_confirmation_signal": SignalEntry(
        module_name="intraday_metar_confirmation_signal",
        family="E", pool=None,
        description="ORPHANED — replaced by METAR nowcast",
        status="ORPHANED",
    ),
    "nwp_dtdt_fusion_signal": SignalEntry(
        module_name="nwp_dtdt_fusion_signal",
        family="E", pool=None,
        description="ORPHANED — different model architecture",
        status="ORPHANED",
    ),
    "metar_dtdt_signal": SignalEntry(
        module_name="metar_dtdt_signal",
        family="E", pool=None,
        description="ORPHANED — dead-end pipeline",
        status="ORPHANED",
    ),
    "dual_polarity_signal": SignalEntry(
        module_name="dual_polarity_signal",
        family="E", pool=None,
        description="ORPHANED — never wired",
        status="ORPHANED",
    ),
    "esdr_signal": SignalEntry(
        module_name="esdr_signal",
        family="E", pool=None,
        description="ORPHANED — never wired",
        status="ORPHANED",
    ),
}


# ══════════════════════════════════════════════════════════════════════
# Pool definitions for Layer 1 pool-of-pools fusion
# ══════════════════════════════════════════════════════════════════════

POOL_DEFINITIONS = {
    "gefs": {
        "signals": [
            "eighty_two_member_ensemble_signal",
            "gaussian_signal",
            "gaussian_v2_signal",
        ],
        "rho_within": 0.85,           # high intra-pool correlation
        "freshness_hours": 6.0,
        "description": "GEFS-derived ensemble fraction signals",
    },
    "ecmwf": {
        "signals": [
            "ecmwf_bias_corrected_signal",
        ],
        "rho_within": 1.0,            # single signal in pool
        "freshness_hours": 6.0,
        "description": "ECMWF bias-corrected signal",
    },
    "nwp_direct": {
        "signals": [
            "nwp_direct_signal",
        ],
        "rho_within": 1.0,
        "freshness_hours": 6.0,
        "description": "GFS/IFS/ICON/GEM direct model output",
    },
    "climatology": {
        "signals": [
            "calendar_climatology_signal",
        ],
        "rho_within": 1.0,
        "freshness_hours": 6.0,
        "description": "Calendar-based climatology baseline",
    },
    "hrrr": {
        "signals": [
            "hrrr_bias_corrected_signal",
        ],
        "rho_within": 1.0,
        "freshness_hours": 3.0,
        "description": "HRRR high-resolution model",
    },
    "metar_nowcast": {
        "signals": [
            "metar_nowcast_signal",
        ],
        "rho_within": 1.0,
        "freshness_hours": 1.5,
        "description": "METAR observation nowcast (final 6h window)",
    },
    "market": {
        "signals": [
            "spread_based_entry_signal",
        ],
        "rho_within": 1.0,
        "freshness_hours": 1.0,
        "description": "Market-derived signals (spread, price)",
    },
}


# ══════════════════════════════════════════════════════════════════════
# Cross-pool correlation matrix (rho_cross)
# ══════════════════════════════════════════════════════════════════════
# From the spec §3.1. These are initial estimates; should be updated
# empirically from a 90-day rolling window once live.
# ══════════════════════════════════════════════════════════════════════

CROSS_POOL_RHO: Dict[str, Dict[str, float]] = {
    "gefs":        {"gefs": 1.00, "ecmwf": 0.70, "nwp_direct": 0.70,
                    "climatology": 0.40, "hrrr": 0.60, "metar_nowcast": 0.50,
                    "market": 0.20},
    "ecmwf":       {"gefs": 0.70, "ecmwf": 1.00, "nwp_direct": 0.65,
                    "climatology": 0.38, "hrrr": 0.55, "metar_nowcast": 0.48,
                    "market": 0.18},
    "nwp_direct":  {"gefs": 0.70, "ecmwf": 0.65, "nwp_direct": 1.00,
                    "climatology": 0.35, "hrrr": 0.58, "metar_nowcast": 0.45,
                    "market": 0.15},
    "climatology": {"gefs": 0.40, "ecmwf": 0.38, "nwp_direct": 0.35,
                    "climatology": 1.00, "hrrr": 0.30, "metar_nowcast": 0.55,
                    "market": 0.10},
    "hrrr":        {"gefs": 0.60, "ecmwf": 0.55, "nwp_direct": 0.58,
                    "climatology": 0.30, "hrrr": 1.00, "metar_nowcast": 0.55,
                    "market": 0.15},
    "metar_nowcast":{"gefs": 0.50, "ecmwf": 0.48, "nwp_direct": 0.45,
                    "climatology": 0.55, "hrrr": 0.55, "metar_nowcast": 1.00,
                    "market": 0.12},
    "market":      {"gefs": 0.20, "ecmwf": 0.18, "nwp_direct": 0.15,
                    "climatology": 0.10, "hrrr": 0.15, "metar_nowcast": 0.12,
                    "market": 1.00},
}

# Pool ordering (for consistent iteration)
POOL_NAMES = ["gefs", "ecmwf", "nwp_direct", "climatology", "hrrr", "metar_nowcast", "market"]

# ══════════════════════════════════════════════════════════════════════
# Freshness half-lives (hours) per pool
# ══════════════════════════════════════════════════════════════════════
# From the spec §2.2 freshness precision decay
# ══════════════════════════════════════════════════════════════════════

FRESHNESS_HALF_LIFE_HOURS: Dict[str, float] = {
    "gefs":         6.0,
    "ecmwf":        6.0,
    "nwp_direct":   6.0,
    "climatology":  6.0,
    "hrrr":         3.0,
    "metar_nowcast": 1.5,
    "market":       1.0,
}


# ══════════════════════════════════════════════════════════════════════
# Modulation parameters (from spec §4)
# ══════════════════════════════════════════════════════════════════════

# Cross-model divergence agreement thresholds
CROSS_MODEL_DIVERGENCE = {
    "high_agreement_threshold": 0.75,   # fraction of models agreeing
    "high_agreement_boost": 1.15,       # n_eff multiplier
    "low_agreement_threshold": 0.50,
    "low_agreement_penalty": 0.80,
}

# Spatial coherence parameters
SPATIAL_COHERENCE = {
    "high_agreement_threshold": 0.80,   # region agreement fraction
    "high_agreement_boost": 1.10,
    "low_agreement_threshold": 0.40,
    "low_agreement_penalty": 0.85,
}

# Dewpoint depression (DPD) modulation
DEWPOINT_MODULATION = {
    "humid_threshold_c": 5.0,           # DPD < 5°F → humid, cloudy
    "humid_penalty": 0.85,
    "dry_threshold_c": 15.0,            # DPD > 15°F → clear, dry
    "dry_boost": 1.05,
}

# Regime modulation
REGIME_MODULATION = {
    "seasonal_transition_penalty": 0.90,
    "frontal_instability_penalty": 0.75,
    "blocking_pattern_boost": 1.05,
}

# Agreement gate
AGREEMENT_GATE = {
    "n_threshold": 4,                   # require N of 7 pools agreeing
    "total_pools": 7,
}

# Goldilocks gate
GOLDILOCKS = {
    "min_p_g": 0.15,                    # only apply when P(Goldilocks) > 0.15
    "max_hours_to_settlement": 6.0,     # only when < 6h to settlement
    "epsilon_weights": [0.55, 0.30, 0.15],  # 3-bin discretization
    "epsilon_bins_f": [1.0, 3.5, 6.0],  # bin centers: 0-2, 2-5, 5+ °F
}


# ══════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════

def get_active_signals(family: Optional[str] = None) -> Dict[str, SignalEntry]:
    """
    Get all ACTIVE signals, optionally filtered by family.

    Args:
        family: 'A', 'B', 'C', 'D', or None for all active signals

    Returns:
        Dict of {signal_name: SignalEntry} for ACTIVE/GATED status signals
    """
    result = {}
    for name, entry in SIGNAL_REGISTRY.items():
        if entry.status in ("ACTIVE", "GATED"):
            if family is None or entry.family == family:
                result[name] = entry
    return result


def get_pool_members() -> Dict[str, List[str]]:
    """
    Get the signal names in each pool (Family A only, excluding killed).

    Returns:
        Dict {pool_name: [signal_name, ...]} for each of the 7 pools
    """
    members = {}
    for pool_name, pool_def in POOL_DEFINITIONS.items():
        active = []
        for sig_name in pool_def["signals"]:
            entry = SIGNAL_REGISTRY.get(sig_name)
            if entry is not None and entry.status == "ACTIVE":
                active.append(sig_name)
        if active:
            members[pool_name] = active
    return members


def get_cross_pool_rho(pool_i: str, pool_j: str) -> float:
    """
    Get cross-pool correlation coefficient.

    Args:
        pool_i: first pool name
        pool_j: second pool name

    Returns:
        Correlation coefficient rho (0.0 to 1.0)
    """
    return CROSS_POOL_RHO.get(pool_i, {}).get(pool_j, 0.5)


def get_freshness_weight(hours_since_update: float, pool: str,
                         multiplier: float = 1.0) -> float:
    """
    Compute freshness weight using exponential decay.

    weight = 2^(-t / tau) where tau = freshness_hours * multiplier

    Args:
        hours_since_update: hours since the signal/pool was last fresh
        pool: pool name
        multiplier: freshness half-life multiplier (sweep param)

    Returns:
        Decay weight in [0, 1]
    """
    half_life = FRESHNESS_HALF_LIFE_HOURS.get(pool, 6.0) * multiplier
    if half_life <= 0:
        return 1.0
    if hours_since_update <= 0:
        return 1.0
    return 2.0 ** (-hours_since_update / half_life)


def compute_n_eff(n_signals: int, rho_within: float) -> float:
    """
    Compute effective sample size for within-pool correlation.

    n_eff = n / (1 + (n - 1) * rho_within)

    Args:
        n_signals: number of signals in the pool
        rho_within: average pairwise correlation within the pool

    Returns:
        Effective sample size (float)
    """
    if n_signals <= 1:
        return float(n_signals)
    denom = 1.0 + (n_signals - 1) * rho_within
    if denom <= 0:
        return float(n_signals)
    return n_signals / denom


def compute_cross_pool_n_eff(pool_n_effs: Dict[str, float]) -> Tuple[float, float]:
    """
    Compute combined n_eff and combined k (successes) across all pools.

    Combined n_eff = max(n_eff_p) + Σ_{q≠p} n_eff_q × (1 - ρ_cross_pq)
    Combined k = max(k_p) + Σ_{q≠p} k_q × (1 - ρ_cross_pq)

    Args:
        pool_n_effs: Dict {pool_name: {"n_eff": float, "k": float}}

    Returns:
        (combined_n_eff, combined_k)
    """
    if not pool_n_effs:
        return 0.0, 0.0

    pool_names = list(pool_n_effs.keys())

    # Use the pool with the largest n_eff as the base
    base_pool = max(pool_names,
                    key=lambda p: pool_n_effs[p].get("n_eff", 0.0))

    combined_n_eff = pool_n_effs[base_pool].get("n_eff", 0.0)
    combined_k = pool_n_effs[base_pool].get("k", 0.0)

    for pool_name in pool_names:
        if pool_name == base_pool:
            continue
        rho = get_cross_pool_rho(base_pool, pool_name)
        n_eff_q = pool_n_effs[pool_name].get("n_eff", 0.0)
        k_q = pool_n_effs[pool_name].get("k", 0.0)
        decorrelation = 1.0 - rho
        combined_n_eff += n_eff_q * decorrelation
        combined_k += k_q * decorrelation

    return combined_n_eff, combined_k


def compute_pool_beta(pool_name: str, signals_predictions: List[Tuple[str, float]],
                      rho_within: Optional[float] = None,
                      prior_alpha: float = 1.0, prior_beta: float = 1.0) -> Tuple[float, float, float]:
    """
    Compute Beta posterior for a pool from its member signals.

    For each signal: (direction_sign, confidence)
    - direction_sign: +1 (UP) or -1 (DOWN) or 'up'/'down'
    - confidence: P(direction | signal) in [0, 1]

    Uses uniform Beta(1,1) prior and n_eff correlation correction.

    Args:
        pool_name: pool name
        signals_predictions: list of (direction, confidence) tuples
        rho_within: within-pool correlation (uses default from POOL_DEFINITIONS if None)
        prior_alpha: Beta prior alpha (default 1.0 for uniform)
        prior_beta: Beta prior beta (default 1.0 for uniform)

    Returns:
        (alpha, beta, n_eff) — Beta posterior parameters and effective sample size
    """
    if not signals_predictions:
        return prior_alpha, prior_beta, 0.0

    # Determine if majority is UP or DOWN
    up_count = 0
    down_count = 0
    total_conf = 0.0
    for direction, confidence in signals_predictions:
        if isinstance(direction, str):
            if direction.lower() == 'up':
                up_count += 1
            elif direction.lower() == 'down':
                down_count += 1
        else:
            if direction > 0:
                up_count += 1
            elif direction < 0:
                down_count += 1
        total_conf += confidence

    n_signals = len(signals_predictions)

    # Compute mean confidence in the majority direction
    majority = 1 if up_count >= down_count else -1
    mean_conf = total_conf / n_signals if n_signals > 0 else 0.5

    # Count exceedances (signals agreeing with majority direction)
    k_count = 0
    for direction, confidence in signals_predictions:
        if isinstance(direction, str):
            sig_dir = 1 if direction.lower() == 'up' else -1
        else:
            sig_dir = 1 if direction > 0 else -1
        if sig_dir == majority:
            k_count += 1

    if rho_within is None:
        rho_within = POOL_DEFINITIONS.get(pool_name, {}).get("rho_within", 0.5)

    pool_n_eff = compute_n_eff(n_signals, rho_within)

    # Beta posterior with correlation-corrected sample size
    alpha = prior_alpha + k_count * (pool_n_eff / n_signals) if n_signals > 0 else prior_alpha
    beta = prior_beta + (pool_n_eff - k_count * (pool_n_eff / n_signals)) if n_signals > 0 else prior_beta

    return alpha, beta, pool_n_eff