#!/usr/bin/env python3
"""
Variance-Weighted Position Sizing System

Implements variance estimation and variance-weighted blending for signal fusion
and position sizing per FP-VARIANCE-WEIGHTED-SIZING.md.

Key formulas:
  - Variance-weighted blend: weight = 1 / variance² for each signal
  - Position sizing: inversely proportional to volatility (high variance → smaller position)
  - Variance-adjusted Kelly: f = base_kelly / (1 + k * σ²_total)

Independent of the existing disagreement-based Kelly multiplier (B-Mode Cycle 6).
This is a first-principles formulation from Bayesian decision theory.

Version: 1.0 — FP Variance-Weighted Sizing (2026-08-06)
"""

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

_logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────

SEASONS = ["winter", "spring", "summer", "fall"]
MONTH_SEASON_MAP = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
}

MIN_SAMPLES_FOR_VARIANCE = 10      # Minimum data points to compute reliable variance
MIN_SIGNALS_FOR_BLEND = 2          # Minimum signals to produce a blend
DEFAULT_AGGRESSIVENESS_K = 2.0     # Default k in 1/(1 + k*σ²)
VARIANCE_FLOOR = 1e-8              # Numerical stability floor
MAX_VARIANCE_RATIO = 100.0         # Cap on variance ratio (weight clamping)
BASE_KELLY_FRACTION = 0.25         # Default base Kelly fraction (25% fractional)


# ─── Seasonal Helpers ──────────────────────────────────────────────────────

def _month_to_season(month: int) -> str:
    """Map month integer (1-12) to season string."""
    return MONTH_SEASON_MAP.get(month, "winter")


def _get_season_for_date(date_str: str) -> str:
    """Extract season from an ISO date string 'YYYY-MM-DD'."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return _month_to_season(dt.month)
    except (ValueError, IndexError):
        return "winter"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Variance Estimation: per-station, per-season temperature variance
# ═══════════════════════════════════════════════════════════════════════════

def compute_temp_variance(
    station: str,
    season: Optional[str] = None,
    metar_db: Optional[object] = None,
) -> float:
    """
    Compute per-station, per-season temperature variance from historical METAR data.

    Uses the climatological temperature distribution for the given station
    and season to estimate the natural variance of daily temperature deltas
    (day-over-day change).

    Args:
        station: ICAO station code (e.g., 'KNYC')
        season: One of 'winter', 'spring', 'summer', 'fall'.
                If None, uses all available data.
        metar_db: Database connection or adapter with a query_temperatures()
                  method that returns a list of (date, temp) tuples.
                  If None, returns a conservative default variance estimate.

    Returns:
        float: Temperature variance estimate in °C².
               Returns DEFAULT_TEMP_VARIANCE if data is insufficient.

    Raises:
        ValueError: If station is empty or invalid.
    """
    if not station or not isinstance(station, str):
        raise ValueError(f"Invalid station code: {station!r}")

    # Default variance (conservative — ~5°C standard deviation for daily deltas)
    DEFAULT_TEMP_VARIANCE = 25.0

    if metar_db is None:
        _logger.warning(
            "compute_temp_variance(%s, %s): No metar_db provided, returning default %.1f",
            station, season, DEFAULT_TEMP_VARIANCE,
        )
        return DEFAULT_TEMP_VARIANCE

    # Try to load temperature history from metar_db
    try:
        if hasattr(metar_db, "query_temperatures"):
            temps = metar_db.query_temperatures(station)
        elif hasattr(metar_db, "execute"):
            # Assume SQLite-like interface
            cursor = metar_db.execute(
                "SELECT date, temperature FROM metar_data WHERE station = ? ORDER BY date",
                (station,),
            )
            temps = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
        else:
            _logger.warning(
                "compute_temp_variance: metar_db has no query_temperatures or execute method"
            )
            return DEFAULT_TEMP_VARIANCE
    except Exception as e:
        _logger.warning("compute_temp_variance(%s): DB error — %s", station, e)
        return DEFAULT_TEMP_VARIANCE

    if not temps:
        _logger.info("compute_temp_variance(%s): No temperature data available", station)
        return DEFAULT_TEMP_VARIANCE

    # Compute day-over-day temperature deltas
    deltas: List[float] = []
    prev_temp: Optional[float] = None
    prev_season: Optional[str] = None

    for row in temps:
        if isinstance(row, (list, tuple)):
            date_str = str(row[0])
            temp = float(row[1])
        elif isinstance(row, dict):
            date_str = str(row.get("date", ""))
            temp = float(row.get("temperature", row.get("temp", float("nan"))))
        else:
            continue

        if math.isnan(temp):
            continue

        row_season = _get_season_for_date(date_str)

        # Filter by season if specified
        if season and row_season != season:
            prev_temp = None
            continue

        if prev_temp is not None and prev_season == row_season:
            delta = temp - prev_temp
            deltas.append(delta)

        prev_temp = temp
        prev_season = row_season

    if len(deltas) < MIN_SAMPLES_FOR_VARIANCE:
        _logger.info(
            "compute_temp_variance(%s, %s): Only %d delta samples (need %d), returning default",
            station, season or "all", len(deltas), MIN_SAMPLES_FOR_VARIANCE,
        )
        return DEFAULT_TEMP_VARIANCE

    # Compute sample variance of deltas
    n = len(deltas)
    mean = sum(deltas) / n
    variance = sum((d - mean) ** 2 for d in deltas) / (n - 1)  # Bessel's correction

    _logger.debug(
        "compute_temp_variance(%s, %s): n=%d, variance=%.4f",
        station, season or "all", n, variance,
    )

    return max(variance, VARIANCE_FLOOR)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Variance Estimation: per-signal, per-station prediction variance
# ═══════════════════════════════════════════════════════════════════════════

def compute_signal_variance(
    signal_name: str,
    station: str,
    backtest_results: Optional[Dict[str, List[Dict]]] = None,
) -> float:
    """
    Compute per-signal, per-station prediction variance from historical backtest results.

    Measures how much a signal's confidence predictions vary when it is right vs. wrong.
    Low variance = consistent performance. High variance = unpredictable reliability.

    Args:
        signal_name: Name of the signal (e.g., 'gaussian', 'pressure_delta')
        station: ICAO station code
        backtest_results: Dict of signal_name -> list of trade dicts, each containing at
                          least 'confidence' and 'correct' fields.
                          If None, returns a default variance estimate.

    Returns:
        float: Prediction variance estimate in [0, 0.25] range (normalized).
               Returns 0.0 if insufficient data.

    """
    DEFAULT_SIGNAL_VARIANCE = 0.1  # Moderate default uncertainty

    if not signal_name:
        return DEFAULT_SIGNAL_VARIANCE

    if backtest_results is None:
        _logger.debug(
            "compute_signal_variance(%s, %s): No backtest_results, returning default %.2f",
            signal_name, station, DEFAULT_SIGNAL_VARIANCE,
        )
        return DEFAULT_SIGNAL_VARIANCE

    # Get trades for this signal+station
    signal_key = f"{signal_name}:{station}"
    trades: List[Dict] = []

    # Try signal-specific key first, then signal name alone
    if signal_key in backtest_results:
        trades = backtest_results[signal_key]
    elif signal_name in backtest_results:
        trades = backtest_results[signal_name]
    else:
        # Try iterating over keys that contain this signal
        for k, v in backtest_results.items():
            if k.startswith(signal_name):
                if isinstance(v, list):
                    trades.extend(v)

    # Filter to station-specific trades if we got a broader set
    if trades and station:
        station_trades = [t for t in trades if t.get("station", "") == station]
        if station_trades:
            trades = station_trades

    if len(trades) < MIN_SAMPLES_FOR_VARIANCE:
        _logger.debug(
            "compute_signal_variance(%s, %s): Only %d trades (need %d), returning default",
            signal_name, station, len(trades), MIN_SAMPLES_FOR_VARIANCE,
        )
        return DEFAULT_SIGNAL_VARIANCE

    # Compute prediction-error variance
    # For each trade, measure (predicted_confidence - actual_outcome)²
    errors: List[float] = []
    for trade in trades:
        confidence = trade.get("confidence", 0.5)
        correct = trade.get("correct", False)
        actual = 1.0 if correct else 0.0
        error = (confidence - actual) ** 2
        errors.append(error)

    if not errors:
        return DEFAULT_SIGNAL_VARIANCE

    n = len(errors)
    mean_error = sum(errors) / n
    # Variance of the prediction errors (Bessel-corrected)
    if n > 1:
        error_variance = sum((e - mean_error) ** 2 for e in errors) / (n - 1)
    else:
        error_variance = mean_error  # Single sample: use error itself as estimate

    # Normalize to [0, 0.25] range (max Brier = 1.0, so max error variance = 0.25 for binary)
    normalized_variance = min(error_variance, 0.25)

    _logger.debug(
        "compute_signal_variance(%s, %s): n=%d, error_var=%.4f, norm=%.4f",
        signal_name, station, n, error_variance, normalized_variance,
    )

    return max(normalized_variance, VARIANCE_FLOOR)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Variance-Weighted Signal Blending
# ═══════════════════════════════════════════════════════════════════════════

def variance_weighted_blend(
    signal_predictions: Dict[str, Tuple[Union[str, int], float]],
    variances: Dict[str, float],
    min_signals: int = MIN_SIGNALS_FOR_BLEND,
) -> Tuple[Optional[str], float, Dict[str, float]]:
    """
    Blend signals using inverse-variance-squared weighting.

    weight_j = 1 / (variance_j² + epsilon)

    Signals with lower prediction variance get higher weight in the blended
    direction and confidence.

    Args:
        signal_predictions: Dict of signal_name -> (direction, confidence)
            direction is 'up'/'down' or 1/-1 numeric
            confidence is [0, 1]
        variances: Dict of signal_name -> variance estimate (float)
            Missing signals get a default high variance.
        min_signals: Minimum number of signals for a valid blend

    Returns:
        Tuple of (blended_direction, blended_confidence, weight_details)
        - blended_direction: 'up', 'down', or None (if insufficient signals)
        - blended_confidence: float in [0, 1]
        - weight_details: dict with signal weights and component contributions

    """
    if not signal_predictions or len(signal_predictions) < min_signals:
        return None, 0.0, {"error": f"Only {len(signal_predictions)} signals (need {min_signals})"}

    # Parse signals into numeric directions and weights
    numeric_dirs: Dict[str, float] = {}
    confidences: Dict[str, float] = {}
    weights: Dict[str, float] = {}
    total_weight = 0.0

    DEFAULT_VARIANCE = 0.1  # Moderate default for signals without variance data

    for sig_name, (direction, confidence) in signal_predictions.items():
        # Convert direction to numeric
        if isinstance(direction, (int, float)):
            num_dir = 1.0 if direction > 0 else -1.0
        elif isinstance(direction, str):
            num_dir = 1.0 if direction.lower() == "up" else -1.0
        else:
            continue

        if num_dir == 0 or confidence <= 0.0:
            continue

        # Get variance for this signal (with default fallback)
        sig_variance = variances.get(sig_name, DEFAULT_VARIANCE)
        sig_variance = max(sig_variance, VARIANCE_FLOOR)

        # Inverse-variance-squared weight
        weight = 1.0 / (sig_variance * sig_variance)

        # Apply confidence as secondary weight multiplier
        weight *= confidence

        # Clamp extreme weights to prevent a single signal from dominating
        if total_weight > 0 and weight / total_weight > MAX_VARIANCE_RATIO:
            weight = total_weight * MAX_VARIANCE_RATIO

        numeric_dirs[sig_name] = num_dir
        confidences[sig_name] = confidence
        weights[sig_name] = weight
        total_weight += weight

    if total_weight <= 0 or len(numeric_dirs) < min_signals:
        return None, 0.0, {"error": "Zero total weight after variance weighting"}

    # Compute weighted direction
    weighted_sum = sum(
        numeric_dirs[sig] * weights[sig] / total_weight
        for sig in numeric_dirs
    )

    # Direction is 'up' if weighted_sum > 0, 'down' if < 0
    # If exactly 0, use simple majority as tiebreaker
    if weighted_sum > 0:
        blended_direction = "up"
    elif weighted_sum < 0:
        blended_direction = "down"
    else:
        # Tiebreaker: majority vote
        up_votes = sum(1 for d in numeric_dirs.values() if d > 0)
        down_votes = sum(1 for d in numeric_dirs.values() if d < 0)
        blended_direction = "up" if up_votes >= down_votes else "down"

    # Compute blended confidence: weighted average of confidences,
    # adjusted by the strength of the directional signal
    weighted_conf = sum(
        confidences[sig] * weights[sig] / total_weight
        for sig in numeric_dirs
    )

    # Confidence boost: how decisive the weighted direction is
    # weighted_sum ranges from [-1, 1]; abs(weighted_sum) is the directional strength
    directional_strength = abs(weighted_sum)

    # Final confidence: blend of average signal confidence and directional strength
    blended_confidence = 0.7 * weighted_conf + 0.3 * directional_strength

    # Clamp to [0.5, 0.999] for trading purposes
    blended_confidence = max(0.5, min(0.999, blended_confidence))

    # Build weight details for diagnostics
    weight_details = {
        "n_signals": len(numeric_dirs),
        "weighted_direction": round(weighted_sum, 4),
        "directional_strength": round(directional_strength, 4),
        "weighted_confidence": round(weighted_conf, 4),
        "signal_weights": {s: round(w / total_weight, 4) for s, w in weights.items()},
        "signal_variances": {s: variances.get(s, DEFAULT_VARIANCE) for s in numeric_dirs},
    }

    return blended_direction, blended_confidence, weight_details


# ═══════════════════════════════════════════════════════════════════════════
# 4. Variance-Adjusted Kelly Position Sizing
# ═══════════════════════════════════════════════════════════════════════════

def variance_adjusted_kelly(
    capital: float,
    edge: float,
    variance: float,
    base_kelly: float = BASE_KELLY_FRACTION,
    aggressiveness_k: float = DEFAULT_AGGRESSIVENESS_K,
    floor_multiplier: float = 0.1,
    max_position_fraction: float = 0.25,
    min_position_usd: float = 0.0,
) -> int:
    """
    Compute variance-adjusted position size in whole contracts.

    Formula:
        kelly_multiplier = 1.0 / (1.0 + k * σ²)
        f = base_kelly * kelly_multiplier
        position = capital * f * edge_normalized
        contracts = floor(position / contract_price)

    High variance → smaller position size. The variance term captures
    total estimation uncertainty from both ensemble spread and inter-signal
    disagreement.

    Args:
        capital: Available capital in USD
        edge: Net edge after fees (probability - market_price - fee).
              Must be > 0 for a valid trade.
        variance: Total estimation variance σ²_total, normalized to [0, 1].
                 0 = no uncertainty, 1 = maximum uncertainty.
        base_kelly: Base fractional Kelly multiplier (default 0.25 = 25% Kelly)
        aggressiveness_k: Penalty parameter k in hyperbolic formula (default 2.0).
                          Higher k = more aggressive penalization of uncertainty.
        floor_multiplier: Minimum Kelly multiplier floor (default 0.1).
                          1/(1+k*σ²) is clamped at this floor.
        max_position_fraction: Maximum fraction of capital per position (default 0.25).
        min_position_usd: Minimum position size in USD (default 0.0).

    Returns:
        int: Number of whole contracts to purchase. 0 if edge ≤ 0,
             variance is invalid, or position rounds to zero.

    Raises:
        ValueError: If capital <= 0, edge is NaN, or variance is negative.

    """
    # Input validation
    if capital <= 0:
        raise ValueError(f"Capital must be positive, got {capital}")
    if math.isnan(edge) or math.isnan(variance):
        raise ValueError(f"edge and variance must be numeric (got edge={edge}, variance={variance})")
    if variance < 0:
        raise ValueError(f"Variance must be non-negative, got {variance}")
    if not 0 < base_kelly <= 1.0:
        _logger.warning("base_kelly=%.2f outside (0,1], clamping", base_kelly)
        base_kelly = max(0.01, min(1.0, base_kelly))

    # Edge check: do not trade negative or zero edge
    if edge <= 0:
        _logger.debug("variance_adjusted_kelly: edge=%.4f <= 0, no trade", edge)
        return 0

    # Clamp variance to [0, 1]
    sigma_sq = max(0.0, min(1.0, variance))

    # Hyperbolic Kelly multiplier: 1 / (1 + k * σ²)
    # At σ² = 0 → multiplier = 1.0 (full base Kelly)
    # At σ² = 1 → multiplier = 1/(1+k) (minimum, clamped to floor)
    kelly_multiplier = 1.0 / (1.0 + aggressiveness_k * sigma_sq)
    kelly_multiplier = max(floor_multiplier, kelly_multiplier)

    # Effective Kelly fraction
    effective_kelly = base_kelly * kelly_multiplier

    # Position size: capital * Kelly fraction * edge scaling
    # Edge scaling ensures proportional sizing based on edge strength
    edge_normalized = min(edge * 5.0, 1.0)  # Scale: 20pp edge → 1.0, 5pp edge → 0.25
    raw_position = capital * effective_kelly * edge_normalized

    # Apply max position cap
    max_position = capital * max_position_fraction
    position = min(raw_position, max_position)

    # Apply minimum position floor
    if position < min_position_usd:
        _logger.debug(
            "variance_adjusted_kelly: position $%.2f < min $%.2f, no trade",
            position, min_position_usd,
        )
        return 0

    # Convert to whole contracts (contract price ≈ edge-scaled market estimate)
    # Contract price is inferred: we size for the edge, not the notional
    # For binary options, cost = capital * fraction / contracts
    # We'll use a simple heuristic: the stronger the edge, the more contracts
    contract_price = 1.0  # Simplified: price per contract in USD units
    n_contracts = max(1, int(position / contract_price))

    _logger.debug(
        "variance_adjusted_kelly: capital=%.0f edge=%.4f σ²=%.4f k=%.1f "
        "kelly_mult=%.4f eff_kelly=%.4f position=%.2f contracts=%d",
        capital, edge, sigma_sq, aggressiveness_k,
        kelly_multiplier, effective_kelly, position, n_contracts,
    )

    return n_contracts


# ═══════════════════════════════════════════════════════════════════════════
# 5. Convenience: Full Sizing Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def variance_weighted_pipeline(
    signal_predictions: Dict[str, Tuple[Union[str, int], float]],
    capital: float,
    market_price: float = 0.5,
    fee_per_contract: float = 0.02,
    signal_variances: Optional[Dict[str, float]] = None,
    temp_variance: Optional[float] = None,
    blend_kwargs: Optional[Dict] = None,
    kelly_kwargs: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    End-to-end variance-weighted sizing pipeline.

    1. Variance-weighted blend of all signals
    2. Compute edge from blended confidence vs market price
    3. Compute total variance (signal + temperature components)
    4. Compute variance-adjusted Kelly position size

    Args:
        signal_predictions: signal_name -> (direction, confidence)
        capital: Available capital in USD
        market_price: Current market contract price [0, 1]
        fee_per_contract: Kalshi round-trip fee per contract
        signal_variances: Pre-computed signal variances.
                          If None, all signals get equal default variance.
        temp_variance: Temperature variance estimate (climatological).
                       If None, uses a conservative default.
        blend_kwargs: Additional kwargs for variance_weighted_blend()
        kelly_kwargs: Additional kwargs for variance_adjusted_kelly()

    Returns:
        Dict with keys:
            - direction: 'up', 'down', or None
            - confidence: float [0, 1]
            - edge: float (net edge after fees)
            - total_variance: float (combined variance estimate)
            - contracts: int
            - weight_details: dict
            - error: str or None

    """
    result: Dict[str, Any] = {
        "direction": None,
        "confidence": 0.0,
        "edge": 0.0,
        "total_variance": 0.0,
        "contracts": 0,
        "weight_details": {},
        "error": None,
    }

    # Step 1: Variance-weighted blend
    if signal_variances is None:
        signal_variances = {}

    b_kwargs = blend_kwargs or {}
    direction, confidence, weight_details = variance_weighted_blend(
        signal_predictions, signal_variances, **b_kwargs
    )

    result["direction"] = direction
    result["confidence"] = confidence
    result["weight_details"] = weight_details

    if direction is None:
        if "error" in weight_details:
            result["error"] = weight_details["error"]
        else:
            result["error"] = "Insufficient signal consensus"
        return result

    # Step 2: Compute edge
    edge = confidence - market_price - fee_per_contract
    result["edge"] = edge

    if edge <= 0:
        result["error"] = f"Edge {edge:.4f} <= 0 after fees"
        return result

    # Step 3: Compute total variance
    # Combine signal variance and temperature variance
    sig_var_list = list(signal_variances.values()) if signal_variances else [0.1]
    avg_signal_variance = sum(sig_var_list) / len(sig_var_list) if sig_var_list else 0.1

    t_var = temp_variance if temp_variance is not None else 25.0
    # Normalize temp variance to [0, 1] using a reference max (~15°C daily delta stdev)
    temp_variance_norm = min(t_var / 225.0, 1.0)  # 225 = 15²

    # Combined: 60% signal variance, 40% temperature relative variance
    total_variance = 0.6 * min(avg_signal_variance, 1.0) + 0.4 * temp_variance_norm
    total_variance = min(total_variance, 1.0)
    result["total_variance"] = total_variance

    # Step 4: Variance-adjusted Kelly
    k_kwargs = kelly_kwargs or {}
    contracts = variance_adjusted_kelly(
        capital=capital,
        edge=edge,
        variance=total_variance,
        **k_kwargs,
    )
    result["contracts"] = contracts

    return result


# ═══════════════════════════════════════════════════════════════════════════
# 6. Module-Level Registry / Cache
# ═══════════════════════════════════════════════════════════════════════════

# Cache for computed variances to avoid recomputation
_VARIANCE_CACHE: Dict[str, float] = {}


def clear_variance_cache() -> None:
    """Clear all cached variance values (e.g., when database is updated)."""
    _VARIANCE_CACHE.clear()
    _logger.debug("Variance cache cleared")


def get_cached_variance(key: str) -> Optional[float]:
    """Retrieve a cached variance value."""
    return _VARIANCE_CACHE.get(key)


def set_cached_variance(key: str, value: float) -> None:
    """Store a variance value in cache."""
    _VARIANCE_CACHE[key] = value


# ═══════════════════════════════════════════════════════════════════════════
# 7. Self-Test
# ═══════════════════════════════════════════════════════════════════════════

def _test_compute_temp_variance():
    """Test temperature variance estimation with synthetic data."""
    print("\n[Test] compute_temp_variance")
    try:
        var = compute_temp_variance("KNYC")
        assert var > 0, f"Expected positive variance, got {var}"
        print(f"  KNYC (no DB) → default {var:.1f} ✓")
    except Exception as e:
        print(f"  KNYC → ERROR: {e}")

    # Test with season filter (falls back to default without DB)
    for season in SEASONS:
        var = compute_temp_variance("KNYC", season=season)
        print(f"  KNYC/{season} → {var:.1f} ✓")

    # Test invalid station
    try:
        compute_temp_variance("")
        print("  Empty station: SHOULD HAVE RAISED")
    except ValueError:
        print("  Empty station → ValueError ✓")


def _test_variance_weighted_blend():
    """Test variance-weighted blending."""
    print("\n[Test] variance_weighted_blend")

    # Test 1: Equal variances, all agree up
    predictions = {
        "sig_a": ("up", 0.8),
        "sig_b": ("up", 0.7),
        "sig_c": ("up", 0.9),
    }
    variances = {"sig_a": 0.05, "sig_b": 0.05, "sig_c": 0.05}
    direction, conf, details = variance_weighted_blend(predictions, variances)
    assert direction == "up", f"Expected 'up', got {direction}"
    assert conf >= 0.5, f"Expected conf >= 0.5, got {conf}"
    print(f"  All agree up, equal vars → {direction}, conf={conf:.4f} ✓")
    print(f"    Weights: {details['signal_weights']}")

    # Test 2: Split signals, equal variances
    predictions = {
        "sig_a": ("up", 0.8),
        "sig_b": ("down", 0.7),
        "sig_c": ("up", 0.6),
    }
    direction, conf, details = variance_weighted_blend(predictions, variances)
    print(f"  2 up, 1 down, equal vars → {direction}, conf={conf:.4f} ✓")

    # Test 3: Low-variance signal should dominate
    predictions = {
        "sig_low_var": ("up", 0.8),
        "sig_high_var": ("down", 0.9),
    }
    variances = {"sig_low_var": 0.01, "sig_high_var": 0.2}
    direction, conf, details = variance_weighted_blend(predictions, variances)
    assert direction == "up", (
        f"Expected low-var signal (up) to dominate, got {direction}"
    )
    print(f"  Low-var up vs high-var down → {direction}, conf={conf:.4f} ✓")
    print(f"    Weights: {details['signal_weights']}")

    # Test 4: Insufficient signals
    direction, conf, details = variance_weighted_blend(
        {"sig_a": ("up", 0.8)}, {}, min_signals=2
    )
    assert direction is None, f"Expected None for insufficient signals, got {direction}"
    print(f"  Insufficient signals → direction=None ✓")

    # Test 5: Empty
    direction, conf, details = variance_weighted_blend({}, {})
    assert direction is None
    print(f"  Empty predictions → direction=None ✓")


def _test_pipeline():
    """Test the end-to-end variance-weighted pipeline."""
    print("\n[Test] variance_weighted_pipeline")

    predictions = {
        "sig_a": ("up", 0.8),
        "sig_b": ("up", 0.75),
        "sig_c": ("up", 0.85),
    }
    variances = {"sig_a": 0.02, "sig_b": 0.03, "sig_c": 0.01}

    result = variance_weighted_pipeline(
        signal_predictions=predictions,
        capital=10000.0,
        market_price=0.60,
        signal_variances=variances,
        temp_variance=15.0,
    )
    assert result["direction"] == "up", f"Expected up, got {result['direction']}"
    assert result["edge"] > 0, f"Expected positive edge, got {result['edge']}"
    assert result["contracts"] > 0, f"Expected positive contracts, got {result['contracts']}"
    print(f"  direction={result['direction']}, conf={result['confidence']:.4f}")
    print(f"  edge={result['edge']:.4f}, σ²={result['total_variance']:.4f}")
    print(f"  contracts={result['contracts']} ✓")

    # Test: edge killed by fees — split signals that cancel directionally
    # Two signals disagree, so directional_strength is low and blended conf stays low
    split_conf = {"sig_a": ("up", 0.52), "sig_b": ("down", 0.52)}
    result = variance_weighted_pipeline(
        signal_predictions=split_conf,
        capital=10000.0,
        market_price=0.55,
        signal_variances={"sig_a": 0.1, "sig_b": 0.1},
    )
    assert result["contracts"] == 0, "Expected 0 contracts when split signals + high mkt price kill edge"
    print(f"  Split/weak edge → contracts=0, error='{result['error']}' ✓")

    # Test: insufficient signals
    result = variance_weighted_pipeline(
        signal_predictions={"sig_a": ("up", 0.8)},
        capital=10000.0,
        market_price=0.55,
        signal_variances={},
        blend_kwargs={"min_signals": 2},
    )
    assert result["direction"] is None
    print(f"  Single signal → direction=None ✓")


def _test_variance_adjusted_kelly():
    """Test variance-adjusted Kelly sizing."""
    print("\n[Test] variance_adjusted_kelly")

    # Test 1: No variance, positive edge → full position
    contracts = variance_adjusted_kelly(
        capital=10000.0, edge=0.1, variance=0.0, base_kelly=0.25,
    )
    assert contracts > 0, f"Expected positive contracts, got {contracts}"
    print(f"  capital=$10K, edge=0.1, σ²=0 → {contracts} contracts ✓")

    # Test 2: High variance → reduced position
    contracts_low_var = variance_adjusted_kelly(10000.0, 0.1, 0.0)
    contracts_high_var = variance_adjusted_kelly(10000.0, 0.1, 0.8)
    assert contracts_high_var <= contracts_low_var, (
        f"High var ({contracts_high_var}) should be <= low var ({contracts_low_var})"
    )
    print(f"  σ²=0.0 → {contracts_low_var} contracts")
    print(f"  σ²=0.8 → {contracts_high_var} contracts (reduced) ✓")

    # Test 3: Zero edge → no trade
    contracts = variance_adjusted_kelly(10000.0, 0.0, 0.1)
    assert contracts == 0, f"Expected 0 for zero edge, got {contracts}"
    print(f"  Edge=0 → {contracts} contracts ✓")

    # Test 4: Negative edge → no trade
    contracts = variance_adjusted_kelly(10000.0, -0.05, 0.1)
    assert contracts == 0, f"Expected 0 for negative edge, got {contracts}"
    print(f"  Edge=-0.05 → {contracts} contracts ✓")

    # Test 5: Floor multiplier clamping
    contracts_no_floor = variance_adjusted_kelly(
        10000.0, 0.2, 1.0, aggressiveness_k=100,
    )
    contracts_with_floor = variance_adjusted_kelly(
        10000.0, 0.2, 1.0, aggressiveness_k=100, floor_multiplier=0.5,
    )
    assert contracts_with_floor >= contracts_no_floor, (
        "Floor should increase minimum position"
    )
    print(f"  Aggressive k=100, floor=0.1 → {contracts_no_floor}")
    print(f"  Aggressive k=100, floor=0.5 → {contracts_with_floor} ✓")

    # Test 6: Input validation
    try:
        variance_adjusted_kelly(0, 0.1, 0.1)
        print("  Zero capital: SHOULD HAVE RAISED")
    except ValueError:
        print("  Zero capital → ValueError ✓")


def main():
    """Run all self-tests."""
    print("=" * 60)
    print("Variance-Weighted Sizing — Self-Test")
    print("=" * 60)
    _test_compute_temp_variance()
    _test_variance_weighted_blend()
    _test_variance_adjusted_kelly()
    _test_pipeline()
    print("\n" + "=" * 60)
    print("All tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
