"""
logodds_consensus_boost.py — Log-Odds Consensus Probability Boost

Replaces raw probability addition (+0.30 to +0.40) with a log-odds
transformation that scales appropriately near probability edges.

The key insight is that a constant boost in log-odds space is equivalent to
multiplying by a Bayes factor. This naturally gives:
  - Larger probability boost near the middle (uncertainty is highest)
  - Smaller probability boost near the edges (already confident)

Mathematically:
    logit(P_final) = logit(P_base) + boost_log_odds

where boost_log_odds = boost_strength * (n_agree / n_total) * boost_scale

This module is a separate concern from consensus_scoring.py (which handles
threshold-based scoring and convergence detection). The boost module handles
the probability enhancement for trade decisions.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Default log-odds boost scale per unit of fractional agreement.
# 1.0 log-odds corresponds to ~0.30 probability boost at p=0.50 (near the spec midpoint).
DEFAULT_BOOST_SCALE: float = 1.0


# ── Core Transformations ─────────────────────────────────────────────────────


def probability_to_logodds(p: float) -> float:
    """
    Convert a probability in [0, 1] to log-odds (logit) space.

    Handles edge cases:
      - p == 0.0  → returns -float('inf')
      - p == 1.0  → returns  float('inf')
      - p in (0,1) → returns ln(p / (1-p))

    Parameters
    ----------
    p : float
        Probability in [0, 1].

    Returns
    -------
    float
        Log-odds (logit). Can be ±inf at exact boundaries.

    Raises
    ------
    ValueError
        If p is outside [0, 1] (with a small tolerance for floating point).

    Examples
    --------
    >>> probability_to_logodds(0.5)
    0.0
    >>> probability_to_logodds(0.0)
    -inf
    >>> probability_to_logodds(1.0)
    inf
    >>> probability_to_logodds(0.7310585786300049)
    1.0
    """
    if p < 0.0 or p > 1.0:
        raise ValueError(f"Probability must be in [0, 1], got {p}")

    if p == 0.0:
        return float("-inf")
    if p == 1.0:
        return float("inf")

    return math.log(p / (1.0 - p))


def logodds_to_probability(lo: float) -> float:
    """
    Convert a log-odds (logit) value back to a probability in [0, 1].

    Uses the logistic sigmoid: P = 1 / (1 + exp(-lo))

    Handles ±inf edge cases:
      - lo == +inf → returns 1.0
      - lo == -inf → returns 0.0

    Parameters
    ----------
    lo : float
        Log-odds (logit) value. Can be ±inf.

    Returns
    -------
    float
        Probability in [0, 1].

    Examples
    --------
    >>> logodds_to_probability(0.0)
    0.5
    >>> logodds_to_probability(float('inf'))
    1.0
    >>> logodds_to_probability(float('-inf'))
    0.0
    >>> logodds_to_probability(1.0)  # doctest: +ELLIPSIS
    0.7310585786300049
    """
    if lo == float("inf"):
        return 1.0
    if lo == float("-inf"):
        return 0.0

    # Numerically stable sigmoid: for lo >= 0 use 1/(1+exp(-lo)),
    # for lo < 0 use exp(lo)/(1+exp(lo)) to avoid overflow in exp(-lo).
    if lo >= 0:
        return 1.0 / (1.0 + math.exp(-lo))
    else:
        exp_lo = math.exp(lo)
        return exp_lo / (1.0 + exp_lo)


# ── Consensus Boost ──────────────────────────────────────────────────────────


def consensus_boost_logodds(
    base_probability: float,
    n_signals_agree: int,
    n_total_signals: int,
    boost_strength: float = 1.0,
    boost_scale: float = DEFAULT_BOOST_SCALE,
    min_probability: float = 0.01,
    max_probability: float = 0.99,
) -> float:
    """
    Boost a base probability using log-odds transformation.

    Algorithm
    ---------
    1. Convert base_probability to log-odds
    2. Compute boost amount:
         boost_log_odds = boost_strength * (n_agree / n_total) * boost_scale
    3. Add boost to the log-odds value
    4. Convert back to probability
    5. Clamp to [min_probability, max_probability]

    The key insight is that a CONSTANT boost in log-odds space has
    *different* effects in probability space depending on the base rate:

      boost_strength=1.0, boost_scale=1.0, full agreement (4/4):
        - At p=0.50: 0.50 → ~0.73  (large effect — high uncertainty)
        - At p=0.80: 0.80 → ~0.92  (moderate effect; already somewhat confident)
        - At p=0.95: 0.95 → ~0.98  (small effect — already highly confident)

    This matches the desired behavior: agreement matters most when the
    system is uncertain, and matters less when already highly confident.

    Parameters
    ----------
    base_probability : float
        The base (pre-consensus) probability in [0, 1].
    n_signals_agree : int
        Number of signals in agreement (must be <= n_total_signals).
    n_total_signals : int
        Total number of signals considered.
    boost_strength : float, optional
        Scales the overall boost. Default 1.0. Sweepable via existing
        parameter sweep framework (see get_sweep_params).
    boost_scale : float, optional
        Base log-odds boost per unit of fractional agreement.
        Default 1.0 corresponds to ~0.30 probability boost at p=0.50
        with full agreement, matching the original spec midpoint.
    min_probability : float, optional
        Lower clamp for output probability. Default 0.01.
    max_probability : float, optional
        Upper clamp for output probability. Default 0.99.

    Returns
    -------
    float
        Boosted probability, clamped to [min_probability, max_probability].

    Raises
    ------
    ValueError
        If n_signals_agree > n_total_signals, or n_total_signals <= 0,
        or base_probability outside [0, 1].

    Examples
    --------
    >>> # p=0.50, 4/4 agreement at strength=1.0
    >>> b = consensus_boost_logodds(0.50, 4, 4)
    >>> round(b, 4)
    0.7311

    >>> # p=0.80, 4/4 agreement
    >>> b = consensus_boost_logodds(0.80, 4, 4)
    >>> round(b, 4)
    0.9158

    >>> # p=0.95, 4/4 agreement (already confident — minimal boost)
    >>> b = consensus_boost_logodds(0.95, 4, 4)
    >>> round(b, 4)
    0.9810

    >>> # p=0.50, 0/4 agreement (no agreement — no boost)
    >>> b = consensus_boost_logodds(0.50, 0, 4)
    >>> round(b, 4)
    0.5

    >>> # p=0.50, 2/4 agreement (partial agreement — partial boost)
    >>> b = consensus_boost_logodds(0.50, 2, 4)
    >>> round(b, 4)
    0.6225

    >>> # p=0.50, 3/4 agreement
    >>> b = consensus_boost_logodds(0.50, 3, 4)
    >>> round(b, 4)
    0.6792
    """
    # Input validation
    if n_signals_agree < 0:
        raise ValueError(f"n_signals_agree must be >= 0, got {n_signals_agree}")
    if n_total_signals <= 0:
        raise ValueError(f"n_total_signals must be > 0, got {n_total_signals}")
    if n_signals_agree > n_total_signals:
        raise ValueError(
            f"n_signals_agree ({n_signals_agree}) cannot exceed "
            f"n_total_signals ({n_total_signals})"
        )
    if base_probability < 0.0 or base_probability > 1.0:
        raise ValueError(
            f"base_probability must be in [0, 1], got {base_probability}"
        )

    # Special cases: edge probabilities or zero agreement → no meaningful boost
    if base_probability in (0.0, 1.0) or n_signals_agree == 0:
        return max(min_probability, min(max_probability, base_probability))

    # Step 1: Convert base probability to log-odds
    logit_base = probability_to_logodds(base_probability)

    # Step 2: Compute boost amount
    agreement_fraction = n_signals_agree / n_total_signals
    boost_log_odds = boost_strength * agreement_fraction * boost_scale

    # Step 3: Add boost in log-odds space
    logit_boosted = logit_base + boost_log_odds

    # Step 4: Convert back to probability
    boosted = logodds_to_probability(logit_boosted)

    # Step 5: Clamp
    return max(min_probability, min(max_probability, boosted))


# ── Configuration Helper for Parameter Sweep ─────────────────────────────────


def get_sweep_params() -> Dict[str, list]:
    """
    Return the sweepable parameters for this module in the format expected
    by the per_parameter_sweep engine.

    These parameters should be added to the sweep configuration alongside
    other sweep-eligible parameters (signal thresholds, fusion weights, etc.).

    Returns
    -------
    dict
        Keys are parameter names; values are lists of candidate values.

    Example
    -------
    >>> params = get_sweep_params()
    >>> 'consensus_boost_strength' in params
    True
    >>> isinstance(params['consensus_boost_strength'], list)
    True
    """
    return {
        # Boost strength: scaling factor applied to the log-odds boost.
        # 0.0 = no boost, 1.0 = default, 2.0 = aggressive, 3.0 = very aggressive.
        "consensus_boost_strength": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
        # Boost scale: base log-odds per unit fractional agreement.
        # Higher values mean each signal contributes more log-odds.
        "consensus_boost_scale": [0.5, 1.0, 1.5, 2.0, 2.5],
        # Minimum number of agreeing signals required to trigger the boost.
        "consensus_min_signals": [2, 3, 4],
        # Minimum base confidence required before boost is applied.
        # Signals below this threshold are not boosted (avoids noise amplification).
        "consensus_min_confidence": [0.50, 0.55, 0.60, 0.65],
    }


def build_boost_config_from_params(sweep_params: Dict[str, float]) -> dict:
    """
    Build a boost configuration dict from a sweep parameter set.

    This bridges the per_parameter_sweep engine (which returns a flat param
    dict) to the boost module's configuration structure.

    Parameters
    ----------
    sweep_params : dict
        Flat dict with keys matching those from get_sweep_params().
        Expected keys: consensus_boost_strength, consensus_boost_scale,
        consensus_min_signals, consensus_min_confidence.

    Returns
    -------
    dict
        Boost configuration with keys:
          - boost_strength: float
          - boost_scale: float
          - min_signals: int
          - min_confidence: float
    """
    return {
        "boost_strength": float(sweep_params.get("consensus_boost_strength", 1.0)),
        "boost_scale": float(sweep_params.get("consensus_boost_scale", DEFAULT_BOOST_SCALE)),
        "min_signals": int(sweep_params.get("consensus_min_signals", 2)),
        "min_confidence": float(sweep_params.get("consensus_min_confidence", 0.50)),
    }


def apply_consensus_boost_from_config(
    base_confidence: float,
    num_agreeing_signals: int,
    total_signals: int,
    config: dict,
) -> float:
    """
    Apply log-odds consensus boost using a configuration dict (from sweep).

    This is the integration function called by signal fusion / trading logic.
    It respects the configuration's min_signals and min_confidence thresholds.

    Parameters
    ----------
    base_confidence : float
        Base probability/confidence before consensus.
    num_agreeing_signals : int
        Number of signals in agreement.
    total_signals : int
        Total number of signals evaluated.
    config : dict
        Configuration dict with keys:
          - boost_strength (float, default 1.0)
          - boost_scale (float, default 1.0)
          - min_signals (int, default 2)
          - min_confidence (float, default 0.50)

    Returns
    -------
    float
        Boosted probability, or base_confidence if thresholds not met.

    Examples
    --------
    >>> cfg = {'boost_strength': 1.0, 'boost_scale': 1.0,
    ...        'min_signals': 2, 'min_confidence': 0.50}
    >>> round(apply_consensus_boost_from_config(0.50, 4, 4, cfg), 4)
    0.7311

    >>> # Below min_signals threshold
    >>> apply_consensus_boost_from_config(0.50, 1, 4, cfg)
    0.5

    >>> # Below min_confidence threshold
    >>> apply_consensus_boost_from_config(0.45, 4, 4, cfg)
    0.45
    """
    boost_strength = config.get("boost_strength", 1.0)
    boost_scale = config.get("boost_scale", DEFAULT_BOOST_SCALE)
    min_signals = config.get("min_signals", 2)
    min_confidence = config.get("min_confidence", 0.50)

    # Threshold checks
    if num_agreeing_signals < min_signals:
        return base_confidence
    if base_confidence < min_confidence:
        return base_confidence

    return consensus_boost_logodds(
        base_probability=base_confidence,
        n_signals_agree=num_agreeing_signals,
        n_total_signals=total_signals,
        boost_strength=boost_strength,
        boost_scale=boost_scale,
    )


# ── Self-Test ────────────────────────────────────────────────────────────────


def _approx_equal(a: float, b: float, tol: float = 1e-4) -> bool:
    """Check if two floats are approximately equal within tolerance."""
    return abs(a - b) < tol


def run_self_test() -> Tuple[bool, int, int]:
    """
    Run self-tests on all core functions.

    Returns
    -------
    (passed, total_tests, passed_count)
        passed : True if all tests pass.
        total_tests : Total number of assertions run.
        passed_count : Number of assertions that passed.

    Each failure is logged at error level with details.
    """
    total = 0
    passed = 0
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        nonlocal total, passed
        total += 1
        if condition:
            passed += 1
        else:
            failures.append(label)

    # ── probability_to_logodds tests ───────────────────────────────────
    check(
        probability_to_logodds(0.5) == 0.0,
        "logodds(0.5) == 0.0",
    )
    check(
        probability_to_logodds(0.0) == float("-inf"),
        "logodds(0.0) == -inf",
    )
    check(
        probability_to_logodds(1.0) == float("inf"),
        "logodds(1.0) == inf",
    )
    check(
        _approx_equal(probability_to_logodds(0.7310585786300049), 1.0),
        "logodds(0.731...) ≈ 1.0",
    )
    check(
        _approx_equal(probability_to_logodds(0.2689414213699951), -1.0),
        "logodds(0.269...) ≈ -1.0",
    )
    try:
        probability_to_logodds(-0.1)
        check(False, "logodds(-0.1) should raise ValueError")
    except ValueError:
        check(True, "logodds(-0.1) raises ValueError")
    try:
        probability_to_logodds(1.1)
        check(False, "logodds(1.1) should raise ValueError")
    except ValueError:
        check(True, "logodds(1.1) raises ValueError")

    # ── logodds_to_probability tests ───────────────────────────────────
    check(
        logodds_to_probability(0.0) == 0.5,
        "inv_logodds(0.0) == 0.5",
    )
    check(
        logodds_to_probability(float("inf")) == 1.0,
        "inv_logodds(inf) == 1.0",
    )
    check(
        logodds_to_probability(float("-inf")) == 0.0,
        "inv_logodds(-inf) == 0.0",
    )
    check(
        _approx_equal(logodds_to_probability(1.0), 0.7310585786300049),
        "inv_logodds(1.0) ≈ 0.7311",
    )
    check(
        _approx_equal(logodds_to_probability(-1.0), 0.2689414213699951),
        "inv_logodds(-1.0) ≈ 0.269",
    )

    # ── Round-trip identity ────────────────────────────────────────────
    for p in [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99]:
        lo = probability_to_logodds(p)
        p_back = logodds_to_probability(lo)
        check(
            _approx_equal(p, p_back),
            f"round-trip p={p}: {p} → lo={lo:.6f} → {p_back:.6f}",
        )

    # ── consensus_boost_logodds tests ──────────────────────────────────
    # p=0.50, 4/4, boost_strength=1.0 → ~0.7311
    b = consensus_boost_logodds(0.50, 4, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.7311),
        "p=0.50, 4/4, s=1.0 → ~0.7311",
    )

    # p=0.80, 4/4, boost_strength=1.0 → ~0.9158
    b = consensus_boost_logodds(0.80, 4, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.9158),
        "p=0.80, 4/4, s=1.0 → ~0.9158",
    )

    # p=0.95, 4/4, boost_strength=1.0 → ~0.9810 (minimal boost)
    b = consensus_boost_logodds(0.95, 4, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.9810),
        "p=0.95, 4/4, s=1.0 → ~0.9810",
    )

    # p=0.50, 0/4, boost_strength=1.0 → 0.50 (no boost)
    b = consensus_boost_logodds(0.50, 0, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.50),
        "p=0.50, 0/4, s=1.0 → 0.50",
    )

    # p=0.50, 2/4, boost_strength=1.0 → ~0.6225 (partial agreement)
    b = consensus_boost_logodds(0.50, 2, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.6225, tol=1e-3),
        "p=0.50, 2/4, s=1.0 → ~0.6225",
    )

    # p=0.50, 3/4, boost_strength=1.0 → ~0.6792
    b = consensus_boost_logodds(0.50, 3, 4, boost_strength=1.0)
    check(
        _approx_equal(b, 0.6792, tol=1e-3),
        "p=0.50, 3/4, s=1.0 → ~0.6792",
    )

    # Edge: p=0.0 → clamped back (no meaningful boost possible)
    b = consensus_boost_logodds(0.0, 4, 4, boost_strength=1.0)
    check(
        b == 0.01,  # min_probability clamp
        "p=0.0, 4/4 → min clamp (0.01)",
    )

    # Edge: p=1.0 → clamped back
    b = consensus_boost_logodds(1.0, 4, 4, boost_strength=1.0)
    check(
        b == 0.99,  # max_probability clamp
        "p=1.0, 4/4 → max clamp (0.99)",
    )

    # Edge: boost_strength=0.0 → no effect regardless of agreement
    b = consensus_boost_logodds(0.50, 4, 4, boost_strength=0.0)
    check(
        _approx_equal(b, 0.50),
        "boost_strength=0.0 → no change",
    )

    # Input validation
    try:
        consensus_boost_logodds(0.50, 5, 4)
        check(False, "n_agree > n_total should raise ValueError")
    except ValueError:
        check(True, "n_agree > n_total raises ValueError")

    try:
        consensus_boost_logodds(0.50, -1, 4)
        check(False, "n_agree < 0 should raise ValueError")
    except ValueError:
        check(True, "n_agree < 0 raises ValueError")

    try:
        consensus_boost_logodds(0.50, 0, 0)
        check(False, "n_total=0 should raise ValueError")
    except ValueError:
        check(True, "n_total=0 raises ValueError")

    # ── apply_consensus_boost_from_config tests ────────────────────────
    cfg = {
        "boost_strength": 1.0,
        "boost_scale": 1.0,
        "min_signals": 2,
        "min_confidence": 0.50,
    }
    b = apply_consensus_boost_from_config(0.50, 4, 4, cfg)
    check(
        _approx_equal(b, 0.7311),
        "apply_consensus_boost_from_config: p=0.50, 4/4 → ~0.7311",
    )

    # Below min_signals → no boost
    b = apply_consensus_boost_from_config(0.50, 1, 4, cfg)
    check(
        _approx_equal(b, 0.50),
        "apply: below min_signals → no boost",
    )

    # Below min_confidence → no boost
    b = apply_consensus_boost_from_config(0.45, 4, 4, cfg)
    check(
        _approx_equal(b, 0.45),
        "apply: below min_confidence → no boost",
    )

    # ── get_sweep_params integrity ─────────────────────────────────────
    params = get_sweep_params()
    expected_keys = [
        "consensus_boost_strength",
        "consensus_boost_scale",
        "consensus_min_signals",
        "consensus_min_confidence",
    ]
    for key in expected_keys:
        check(key in params, f"sweep params contains '{key}'")
        check(
            isinstance(params[key], list) and len(params[key]) > 0,
            f"sweep param '{key}' is non-empty list",
        )

    # ── build_boost_config_from_params round-trip ──────────────────────
    sample_params = {
        "consensus_boost_strength": 2.0,
        "consensus_boost_scale": 1.5,
        "consensus_min_signals": 3,
        "consensus_min_confidence": 0.55,
    }
    built_cfg = build_boost_config_from_params(sample_params)
    check(
        built_cfg["boost_strength"] == 2.0,
        "build_cfg: boost_strength round-trip",
    )
    check(
        built_cfg["boost_scale"] == 1.5,
        "build_cfg: boost_scale round-trip",
    )
    check(
        built_cfg["min_signals"] == 3,
        "build_cfg: min_signals round-trip",
    )
    check(
        built_cfg["min_confidence"] == 0.55,
        "build_cfg: min_confidence round-trip",
    )

    # ── Summary ────────────────────────────────────────────────────────
    all_passed = len(failures) == 0
    if all_passed:
        logger.info("✅ All %d/%d self-tests passed.", passed, total)
    else:
        logger.error("❌ %d/%d tests failed:", len(failures), total)
        for f in failures:
            logger.error("    FAIL: %s", f)

    return all_passed, total, passed


# ── Improvement Suggestion ──────────────────────────────────────────────────
#
# Suggestion: Add signal-quality-weighted log-odds boost
#
# Instead of giving every agreeing signal the same weight (1/n_total),
# weight each signal by its historical accuracy or Brier score:
#
#   weighted_fraction = sum(w_i for i in agreeing_signals) / sum(w_i for all signals)
#
# where w_i = accuracy_i (or 1 - brier_i, or some normalized skill score).
#
# Why it helps: Not all signals are equally predictive. A consensus of 4 high-skill
# signals (ecmwf_bias_corrected, pressure_delta, seasonal_regime, metar_trend)
# deserves a larger boost than 4 low-skill signals. Weighted agreement better
# reflects the true information content of the consensus event, improving
# calibration (lower Brier score) without introducing additional parameters.
#
# Integration would add a signal_weights dict (computed offline or from rolling
# calibration) and replace the agreement_fraction calculation with a weighted
# version in consensus_boost_logodds(). The boost_strength sweep would still
# calibrate the overall magnitude.


# ── CLI Entry Point ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok, total, passed = run_self_test()
    if ok:
        print(f"\n✅ All {total} tests passed.")
    else:
        print(f"\n❌ {total - passed}/{total} tests failed.")
        raise SystemExit(1)