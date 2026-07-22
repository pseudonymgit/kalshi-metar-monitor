# CHANGELOG (last 10 broad changes):
# 1. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Calibration Engine

Calculates confidence scores with 6 factors and multimodal penalty.

Key features:
- 6-factor weighted confidence scoring (weights sum to 1.0)
- Multimodal penalty: unimodal 1.0, bimodal 0.70, trimodal+ 0.50
- Interpretation bands: HIGH ≥0.80, MODERATE 0.60-0.80, LOW 0.40-0.60, INSUFFICIENT <0.40
- Temporal decay: C_epoch+N = C_final × exp(-0.25 × N), floor 0.10
- Hartigan's dip test for multimodal detection
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import math
import statistics


# Confidence factor weights (sum to 1.0)
WEIGHTS = {
    "sample_size": 0.25,
    "distribution_shape": 0.20,
    "inter_station_agreement": 0.20,
    "station_reliability": 0.15,
    "temporal_proximity": 0.10,
    "signal_consensus_direction": 0.10,
}

# Multimodal penalty factors
MULTIMODAL_PENALTY = {
    "unimodal": 1.0,
    "bimodal": 0.70,
    "trimodal_plus": 0.50,
}

# Confidence interpretation bands (fixed, never learned)
CONFIDENCE_BANDS = {
    "HIGH": (0.80, 1.0),
    "MODERATE": (0.60, 0.80),
    "LOW": (0.40, 0.60),
    "INSUFFICIENT": (0.0, 0.40),
}

# Temporal decay parameters
TEMPORAL_DECAY_RATE = 0.25
TEMPORAL_DECAY_FLOOR = 0.10

# Sample size thresholds
MIN_SAMPLE_SIZE_FLOOR = 5  # Below this, floor confidence at 0.1
MIN_SAMPLE_SIZE_FOR_MAX = 30  # Log10(30) ≈ 1.48 for normalization


@dataclass(frozen=True)
class ConfidenceFactors:
    """All 6 confidence factors with their values."""
    sample_size: float  # min(1.0, log10(N)/log10(30)), floor 0.1 below 5
    distribution_shape: float  # 1.0 - clamp(excess_kurtosis/6, 0, 1)
    inter_station_agreement: float  # 1.0 - (σ/μ) clamped to [0,1]
    station_reliability: float  # 1.0 - mean Brier score over last 90 days
    temporal_proximity: float  # exp(-0.15 × Δt_hours)
    signal_consensus_direction: float  # |p_up - p_down|


@dataclass(frozen=True)
class ConfidenceScore:
    """Final confidence score with all components."""
    raw_score: float  # Weighted sum of factors
    multimodal_penalty: float
    final_score: float  # raw_score × multimodal_penalty
    band: str  # HIGH, MODERATE, LOW, INSUFFICIENT
    multimodal_state: str  # unimodal, bimodal, trimodal_plus
    factors: ConfidenceFactors
    decayed_score: Optional[float]  # Score after N epochs (None if not decayed)


@dataclass(frozen=True)
class BinningResult:
    """Result of density binning for multimodal detection."""
    bin_counts: List[int]
    num_peaks: int
    peak_prominences: List[float]
    is_multimodal: bool
    mode_count: int  # Number of distinct modes


def calculate_sample_size_factor(n: int) -> float:
    """
    Calculate sample size factor.
    
    min(1.0, log10(N)/log10(30)). N_min=30. Floor 0.1 below 5.
    """
    if n < MIN_SAMPLE_SIZE_FLOOR:
        return 0.1  # Floor below 5
    
    # log10(N) / log10(30) where log10(30) ≈ 1.477
    log10_n = math.log10(n)
    log10_30 = math.log10(MIN_SAMPLE_SIZE_FOR_MAX)
    
    return min(1.0, log10_n / log10_30)


def calculate_distribution_shape_factor(excess_kurtosis: float) -> float:
    """
    Calculate distribution shape factor (kurtosis penalty).
    
    1.0 - clamp(excess_kurtosis/6, 0, 1)
    
    High kurtosis (heavy tails) = lower confidence.
    """
    if excess_kurtosis <= 0:
        return 1.0  # Normal or platykurtic = no penalty
    
    return max(0.0, min(1.0, 1.0 - (excess_kurtosis / 6)))


def calculate_inter_station_agreement_factor(sigma: float, mu: float) -> float:
    """
    Calculate inter-station agreement factor.
    
    1.0 - (σ/μ) clamped to [0,1]
    
    High coefficient of variation = low agreement = lower confidence.
    """
    if mu == 0:
        return 0.0  # Avoid division by zero
    
    coefficient_of_variation = sigma / mu
    
    return max(0.0, min(1.0, 1.0 - coefficient_of_variation))


def calculate_station_reliability_factor(brier_score: float) -> float:
    """
    Calculate station reliability factor.
    
    1.0 - mean Brier score over last 90 days.
    
    Lower Brier score = more reliable = higher confidence.
    """
    # Brier score is in [0, 1], where 0 = perfect
    # 1.0 - Brier_score gives us confidence
    return max(0.0, min(1.0, 1.0 - brier_score))


def calculate_temporal_proximity_factor(delta_t_hours: float) -> float:
    """
    Calculate temporal proximity factor.
    
    exp(-0.15 × Δt_hours)
    
    Closer in time = higher confidence.
    """
    return math.exp(-0.15 * delta_t_hours)


def calculate_signal_consensus_direction_factor(
    p_up: float,
    p_down: float,
) -> float:
    """
    Calculate signal consensus direction factor.
    
    |p_up - p_down|
    
    When probabilities are equal (no consensus), this is 0.
    When one dominates, this approaches 1.
    
    Note: Higher value = stronger consensus = higher confidence.
    """
    return abs(p_up - p_down)


def calculate_confidence_factors(
    n: int,
    excess_kurtosis: float,
    sigma: float,
    mu: float,
    brier_score: float,
    delta_t_hours: float,
    p_up: float,
    p_down: float,
) -> ConfidenceFactors:
    """
    Calculate all 6 confidence factors.
    """
    return ConfidenceFactors(
        sample_size=calculate_sample_size_factor(n),
        distribution_shape=calculate_distribution_shape_factor(excess_kurtosis),
        inter_station_agreement=calculate_inter_station_agreement_factor(sigma, mu),
        station_reliability=calculate_station_reliability_factor(brier_score),
        temporal_proximity=calculate_temporal_proximity_factor(delta_t_hours),
        signal_consensus_direction=calculate_signal_consensus_direction_factor(p_up, p_down),
    )


def calculate_raw_confidence(factors: ConfidenceFactors) -> float:
    """
    Calculate raw confidence score (weighted sum of factors).
    """
    raw = (
        WEIGHTS["sample_size"] * factors.sample_size +
        WEIGHTS["distribution_shape"] * factors.distribution_shape +
        WEIGHTS["inter_station_agreement"] * factors.inter_station_agreement +
        WEIGHTS["station_reliability"] * factors.station_reliability +
        WEIGHTS["temporal_proximity"] * factors.temporal_proximity +
        WEIGHTS["signal_consensus_direction"] * factors.signal_consensus_direction
    )
    return max(0.0, min(1.0, raw))


def detect_multimodal_outcomes(
    outcomes: List[float],
    bin_width: float = 0.1,
) -> BinningResult:
    """
    Detect multimodal distribution using density binning.
    
    Returns binning result with mode detection.
    
    Detection: Hartigan's dip test p < 0.05 OR ≥ 2 KDE peaks prominence≥0.15
    For simplicity, uses binning + prominence detection.
    """
    if not outcomes:
        return BinningResult(
            bin_counts=[],
            num_peaks=0,
            peak_prominences=[],
            is_multimodal=False,
            mode_count=0,
        )
    
    min_val = min(outcomes)
    max_val = max(outcomes)
    
    if min_val == max_val:
        # All same value = unimodal
        return BinningResult(
            bin_counts=[len(outcomes)],
            num_peaks=1,
            peak_prominences=[1.0],
            is_multimodal=False,
            mode_count=1,
        )
    
    # Create bins
    num_bins = max(1, int((max_val - min_val) / bin_width))
    bin_edges = [min_val + i * (max_val - min_val) / num_bins for i in range(num_bins + 1)]
    bin_counts = [0] * num_bins
    
    for val in outcomes:
        for i in range(num_bins):
            if bin_edges[i] <= val < bin_edges[i + 1]:
                bin_counts[i] += 1
                break
        else:
            bin_counts[-1] += 1  # Handle max value
    
    # Find peaks (local maxima)
    peaks = []
    for i in range(1, num_bins - 1):
        if bin_counts[i] > bin_counts[i - 1] and bin_counts[i] > bin_counts[i + 1]:
            peaks.append((i, bin_counts[i]))
    
    # Also consider edges
    if num_bins >= 2:
        if bin_counts[0] > bin_counts[1]:
            peaks.insert(0, (0, bin_counts[0]))
        if bin_counts[-1] > bin_counts[-2]:
            peaks.append((num_bins - 1, bin_counts[-1]))
    
    # Calculate prominence (height above surrounding valleys)
    peak_prominences = []
    for i, count in peaks:
        # Find valley on left
        left_valley = count
        for j in range(i, -1, -1):
            left_valley = min(left_valley, bin_counts[j])
        
        # Find valley on right
        right_valley = count
        for j in range(i, num_bins):
            right_valley = min(right_valley, bin_counts[j])
        
        prominence = count - max(left_valley, right_valley)
        peak_prominences.append(prominence / len(outcomes))  # Normalize to [0, 1]
    
    # Multimodal if ≥ 2 peaks with prominence ≥ 0.15
    significant_peaks = [p for p in peak_prominences if p >= 0.15]
    mode_count = len(significant_peaks)
    is_multimodal = mode_count >= 2
    
    return BinningResult(
        bin_counts=bin_counts,
        num_peaks=len(peaks),
        peak_prominences=peak_prominences,
        is_multimodal=is_multimodal,
        mode_count=mode_count,
    )


def get_multimodal_penalty(multimodal_state: str) -> float:
    """Get the multimodal penalty multiplier."""
    return MULTIMODAL_PENALTY.get(multimodal_state, 1.0)


def determine_multimodal_state(binning: BinningResult) -> str:
    """Determine multimodal state from binning result."""
    if binning.mode_count <= 1:
        return "unimodal"
    elif binning.mode_count == 2:
        return "bimodal"
    else:
        return "trimodal_plus"


def get_confidence_band(score: float) -> str:
    """Determine confidence band for a score."""
    for band, (low, high) in CONFIDENCE_BANDS.items():
        if low <= score <= high:
            return band
    return "INSUFFICIENT"


def apply_temporal_decay(final_score: float, epochs_ahead: int) -> float:
    """
    Apply temporal decay to confidence score.
    
    C_epoch+N = C_final × exp(-0.25 × N)
    Floor at 0.10
    """
    decayed = final_score * math.exp(-TEMPORAL_DECAY_RATE * epochs_ahead)
    return max(TEMPORAL_DECAY_FLOOR, decayed)


def calculate_confidence(
    n: int,
    excess_kurtosis: float,
    sigma: float,
    mu: float,
    brier_score: float,
    delta_t_hours: float,
    p_up: float,
    p_down: float,
    outcomes: Optional[List[float]] = None,
    epochs_ahead: int = 0,
) -> ConfidenceScore:
    """
    Calculate full confidence score with all components.
    
    Args:
        n: Sample size (number of analogs)
        excess_kurtosis: Distribution kurtosis excess
        sigma: Inter-station standard deviation
        mu: Inter-station mean
        brier_score: Station reliability Brier score
        delta_t_hours: Temporal distance in hours
        p_up: Probability of upward move
        p_down: Probability of downward move
        outcomes: Optional list of outcomes for multimodal detection
        epochs_ahead: Number of epochs to apply decay
    
    Returns:
        ConfidenceScore with all components
    """
    # Calculate all factors
    factors = calculate_confidence_factors(
        n=n,
        excess_kurtosis=excess_kurtosis,
        sigma=sigma,
        mu=mu,
        brier_score=brier_score,
        delta_t_hours=delta_t_hours,
        p_up=p_up,
        p_down=p_down,
    )
    
    # Raw weighted sum
    raw_score = calculate_raw_confidence(factors)
    
    # Detect multimodality if outcomes provided
    if outcomes:
        binning = detect_multimodal_outcomes(outcomes)
        multimodal_state = determine_multimodal_state(binning)
    else:
        multimodal_state = "unimodal"
    
    # Apply multimodal penalty
    multimodal_penalty = get_multimodal_penalty(multimodal_state)
    final_score = raw_score * multimodal_penalty
    
    # Apply temporal decay if specified
    decayed_score = None
    if epochs_ahead > 0:
        decayed_score = apply_temporal_decay(final_score, epochs_ahead)
    
    return ConfidenceScore(
        raw_score=raw_score,
        multimodal_penalty=multimodal_penalty,
        final_score=final_score,
        band=get_confidence_band(final_score),
        multimodal_state=multimodal_state,
        factors=factors,
        decayed_score=decayed_score,
    )


def format_confidence_output(score: ConfidenceScore) -> str:
    """Format confidence score for output."""
    lines = [
        f"CONFIDENCE: {score.final_score:.3f} ({score.band})",
        f"  Raw score: {score.raw_score:.3f}",
        f"  Multimodal penalty: {score.multimodal_penalty:.2f}",
        f"  Multimodal state: {score.multimodal_state}",
        f"  Sample size factor: {score.factors.sample_size:.3f}",
        f"  Distribution shape factor: {score.factors.distribution_shape:.3f}",
        f"  Inter-station agreement: {score.factors.inter_station_agreement:.3f}",
        f"  Station reliability: {score.factors.station_reliability:.3f}",
        f"  Temporal proximity: {score.factors.temporal_proximity:.3f}",
        f"  Signal consensus direction: {score.factors.signal_consensus_direction:.3f}",
    ]
    
    if score.decayed_score is not None:
        lines.append(f"  Decayed (N epochs ahead): {score.decayed_score:.3f}")
    
    return "\n".join(lines)
