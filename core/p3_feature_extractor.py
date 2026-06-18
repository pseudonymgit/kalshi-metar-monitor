"""
Phase 3 Feature Extractor

Extracts a 14-dimensional feature vector from settlement epochs for similarity matching.

Features (14 dimensions):
1. settlement_jump_magnitude (int) - weight 3
2. reversion_occurred (binary) - weight 2
3. max_excursion_above_settlement (float) - weight 1
4. duration_at_or_above_seconds (float) - weight 1
5. duration_strictly_above_seconds (float) - weight 1
6. terminal_state_reached (binary) - weight 2
7. transition_count (int) - weight 1
8. settlement_bucket (int) - weight 1 (exact gate for market_type, not for station)
9. reversion_latency_seconds (float) - weight 2
10. goldilocks_emitted (binary) - weight 2
11. prior_settlement_bucket (int) - weight 1
12. station (exact gate - weight ∞) - NOT included in numeric vector
13. market_type (exact gate - weight ∞) - NOT included in numeric vector
14. local_trading_date (encoded as float for seasonal phase) - weight 1

Distance Functions:
- Integer: d = |a - b|
- Binary: d = 0 if equal else 1
- Float: d = |a - b| / MAX_RANGE (fixed per-dimension constants)

No data normalization - uses fixed MAX_RANGE constants per dimension.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import math


# Fixed MAX_RANGE constants for float normalization (never data-normalized)
# These represent the expected maximum value range for each dimension
MAX_RANGES = {
    "settlement_jump_magnitude": 20,  # Typical temperature jump is small
    "max_excursion_above_settlement": 30,  # Max expected excursion in degrees F
    "duration_at_or_above_seconds": 86400,  # Max 24 hours in seconds
    "duration_strictly_above_seconds": 86400,  # Max 24 hours in seconds
    "reversion_latency_seconds": 3600,  # Typical reversion latency in seconds
    "transition_count": 50,  # Max expected transitions per epoch
    "prior_settlement_bucket": 100,  # Temperature range
    "local_trading_date_normalized": 1.0,  # Already normalized
}

# Fixed weights per Phase 3 spec
WEIGHTS = {
    "settlement_jump_magnitude": 3,  # Primary structural signature
    "reversion_occurred": 2,  # Strong discriminator
    "max_excursion_above_settlement": 1,
    "duration_at_or_above_seconds": 1,
    "duration_strictly_above_seconds": 1,
    "terminal_state_reached": 2,  # Structurally distinct
    "transition_count": 1,
    "settlement_bucket": 1,
    "reversion_latency_seconds": 2,  # Key behavioral signature
    "goldilocks_emitted": 2,  # High-signal discriminator
    "prior_settlement_bucket": 1,
    "local_trading_date_normalized": 1,
}


@dataclass(frozen=True)
class FeatureVector:
    """14-dimensional feature vector for an epoch."""
    settlement_jump_magnitude: int
    reversion_occurred: int  # 0 or 1
    max_excursion_above_settlement: float
    duration_at_or_above_seconds: float
    duration_strictly_above_seconds: float
    terminal_state_reached: int  # 0 or 1
    transition_count: int
    settlement_bucket: int
    reversion_latency_seconds: float
    goldilocks_emitted: int  # 0 or 1
    prior_settlement_bucket: Optional[int]
    local_trading_date_normalized: float  # 0.0 to 1.0, seasonal phase
    station: Optional[str] = None  # Gate - not part of numeric vector
    market_type: Optional[str] = None  # Gate - not part of numeric vector
    
    def to_list(self) -> List[float]:
        """Convert to numeric list (14 dimensions, excluding gates)."""
        return [
            float(self.settlement_jump_magnitude),
            float(self.reversion_occurred),
            self.max_excursion_above_settlement,
            self.duration_at_or_above_seconds,
            self.duration_strictly_above_seconds,
            float(self.terminal_state_reached),
            float(self.transition_count),
            float(self.settlement_bucket),
            self.reversion_latency_seconds,
            float(self.goldilocks_emitted),
            float(self.prior_settlement_bucket or 0),
            self.local_trading_date_normalized,
        ]


def normalize_local_trading_date(date_str: str) -> float:
    """
    Normalize a trading date to [0.0, 1.0] for seasonal phase encoding.
    
    This encodes the day-of-year as a fraction of the year, preserving
    seasonal relationships (e.g., Dec 25 ≈ Jan 1 in circular distance).
    """
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    
    # Get day of year (1-366)
    day_of_year = dt.timetuple().tm_yday
    
    # Normalize to [0.0, 1.0]
    days_in_year = 365.25  # Account for leap years
    return (day_of_year - 1) / days_in_year


def normalize_integer(value: int, max_range: int) -> float:
    """Normalize an integer value using fixed MAX_RANGE."""
    return abs(float(value)) / max_range


def normalize_float(value: float, max_range: float) -> float:
    """Normalize a float value using fixed MAX_RANGE."""
    return abs(value) / max_range


def binary_distance(a: int, b: int) -> int:
    """Binary distance: 0 if equal, 1 otherwise."""
    return 0 if a == b else 1


def integer_distance(a: int, b: int) -> int:
    """Integer distance: absolute difference."""
    return abs(a - b)


def float_distance(a: float, b: float, max_range: float) -> float:
    """Float distance: normalized absolute difference."""
    return abs(a - b) / max_range


def extract_features_from_epoch(epoch_data: Dict[str, Any]) -> FeatureVector:
    """
    Extract a 14-dimensional feature vector from an epoch dictionary.
    
    Args:
        epoch_data: Dictionary containing epoch fields from settlement_epochs table
        
    Returns:
        FeatureVector with all 14 dimensions populated
    """
    # Extract basic fields
    settlement_jump = epoch_data.get("settlement_jump_magnitude")
    reversion = 1 if epoch_data.get("reversion_occurred") else 0
    max_excursion = float(epoch_data.get("max_excursion_above_settlement") or 0.0)
    duration_at_or_above = float(epoch_data.get("duration_at_or_above_settlement_seconds") or 0.0)
    duration_strictly_above = float(epoch_data.get("duration_strictly_above_settlement_seconds") or 0.0)
    terminal = 1 if epoch_data.get("terminal_state_reached") else 0
    transition_count = int(epoch_data.get("last_transition_event_id") or 0)
    settlement_bucket = int(epoch_data.get("settlement_bucket") or 0)
    prior_bucket = epoch_data.get("prior_settlement_bucket")
    
    # Calculate reversion latency (seconds since reversion)
    first_reversion_ts = epoch_data.get("first_reversion_timestamp_utc")
    reversion_latency = 0.0
    if first_reversion_ts:
        try:
            reversion_dt = datetime.fromisoformat(first_reversion_ts.replace("Z", "+00:00"))
            now_dt = datetime.now(reversion_dt.tzinfo)
            reversion_latency = max(0.0, (now_dt - reversion_dt).total_seconds())
        except (ValueError, TypeError):
            reversion_latency = 0.0
    
    # Goldilocks emitted - check metadata or assume not tracked
    goldilocks = 0  # Default: not tracked in this version
    
    # Normalize date to seasonal phase
    local_date = epoch_data.get("local_trading_date", "")
    date_normalized = normalize_local_trading_date(local_date)
    
    return FeatureVector(
        settlement_jump_magnitude=settlement_jump or 0,
        reversion_occurred=reversion,
        max_excursion_above_settlement=max_excursion,
        duration_at_or_above_seconds=duration_at_or_above,
        duration_strictly_above_seconds=duration_strictly_above,
        terminal_state_reached=terminal,
        transition_count=transition_count,
        settlement_bucket=settlement_bucket,
        reversion_latency_seconds=reversion_latency,
        goldilocks_emitted=goldilocks,
        prior_settlement_bucket=prior_bucket if isinstance(prior_bucket, int) else None,
        local_trading_date_normalized=date_normalized,
        station=epoch_data.get("station"),
        market_type=epoch_data.get("market_type"),
    )


def calculate_distance_vector(
    features_a: FeatureVector,
    features_b: FeatureVector,
) -> Dict[str, float]:
    """
    Calculate all 14 dimension distances between two feature vectors.
    
    Returns dictionary of dimension name -> distance (normalized to [0, 1])
    """
    distances = {}
    
    # settlement_jump_magnitude (int)
    distances["settlement_jump_magnitude"] = integer_distance(
        features_a.settlement_jump_magnitude,
        features_b.settlement_jump_magnitude,
    ) / MAX_RANGES["settlement_jump_magnitude"]
    
    # reversion_occurred (binary)
    distances["reversion_occurred"] = binary_distance(
        features_a.reversion_occurred,
        features_b.reversion_occurred,
    )
    
    # max_excursion_above_settlement (float)
    distances["max_excursion_above_settlement"] = float_distance(
        features_a.max_excursion_above_settlement,
        features_b.max_excursion_above_settlement,
        MAX_RANGES["max_excursion_above_settlement"],
    )
    
    # duration_at_or_above_seconds (float)
    distances["duration_at_or_above_seconds"] = float_distance(
        features_a.duration_at_or_above_seconds,
        features_b.duration_at_or_above_seconds,
        MAX_RANGES["duration_at_or_above_seconds"],
    )
    
    # duration_strictly_above_seconds (float)
    distances["duration_strictly_above_seconds"] = float_distance(
        features_a.duration_strictly_above_seconds,
        features_b.duration_strictly_above_seconds,
        MAX_RANGES["duration_strictly_above_seconds"],
    )
    
    # terminal_state_reached (binary)
    distances["terminal_state_reached"] = binary_distance(
        features_a.terminal_state_reached,
        features_b.terminal_state_reached,
    )
    
    # transition_count (int)
    distances["transition_count"] = integer_distance(
        features_a.transition_count,
        features_b.transition_count,
    ) / MAX_RANGES["transition_count"]
    
    # settlement_bucket (int)
    distances["settlement_bucket"] = integer_distance(
        features_a.settlement_bucket,
        features_b.settlement_bucket,
    ) / MAX_RANGES["prior_settlement_bucket"]  # Use prior_settlement_bucket range
    
    # reversion_latency_seconds (float)
    distances["reversion_latency_seconds"] = float_distance(
        features_a.reversion_latency_seconds,
        features_b.reversion_latency_seconds,
        MAX_RANGES["reversion_latency_seconds"],
    )
    
    # goldilocks_emitted (binary)
    distances["goldilocks_emitted"] = binary_distance(
        features_a.goldilocks_emitted,
        features_b.goldilocks_emitted,
    )
    
    # prior_settlement_bucket (int)
    distances["prior_settlement_bucket"] = integer_distance(
        features_a.prior_settlement_bucket or 0,
        features_b.prior_settlement_bucket or 0,
    ) / MAX_RANGES["prior_settlement_bucket"]
    
    # local_trading_date_normalized (float)
    distances["local_trading_date_normalized"] = float_distance(
        features_a.local_trading_date_normalized,
        features_b.local_trading_date_normalized,
        MAX_RANGES["local_trading_date_normalized"],
    )
    
    return distances


def calculate_weighted_distance(
    distances: Dict[str, float],
) -> Tuple[float, float]:
    """
    Calculate weighted distance and max possible distance.
    
    Returns:
        Tuple of (weighted_distance, max_possible_distance)
    """
    weighted_sum = 0.0
    max_possible_sum = 0.0
    
    for dim_name, distance in distances.items():
        weight = WEIGHTS.get(dim_name, 1)
        weighted_sum += weight * distance
        max_possible_sum += weight  # Max distance per dimension is 1.0
    
    return weighted_sum, max_possible_sum


def calculate_match_score(
    features_a: FeatureVector,
    features_b: FeatureVector,
) -> float:
    """
    Calculate match score between two feature vectors.
    
    match_score = 1.0 - (raw_distance / max_possible_distance)
    
    Returns score in [0, 1], where higher is more similar.
    """
    distances = calculate_distance_vector(features_a, features_b)
    raw_distance, max_distance = calculate_weighted_distance(distances)
    
    if max_distance == 0:
        return 1.0  # Both vectors are empty/identical
    
    score = 1.0 - (raw_distance / max_distance)
    return max(0.0, min(1.0, score))


def is_station_gate_match(features_a: FeatureVector, features_b: FeatureVector) -> bool:
    """Check if station gates match (exact match required)."""
    return features_a.station == features_b.station


def is_market_type_gate_match(features_a: FeatureVector, features_b: FeatureVector) -> bool:
    """Check if market_type gates match (exact match required)."""
    return features_a.market_type == features_b.market_type


def are_gates_compatible(
    features_a: FeatureVector,
    features_b: FeatureVector,
) -> bool:
    """
    Check if both station and market_type gates are compatible.
    Both must match exactly for a valid comparison.
    """
    return (is_station_gate_match(features_a, features_b) and 
            is_market_type_gate_match(features_a, features_b))


def extract_features_for_query(
    station: str,
    market_type: Optional[str],
    local_trading_date: str,
    settlement_bucket: int,
) -> FeatureVector:
    """
    Create a FeatureVector for query purposes (only numeric dimensions, gates set).
    
    This is used when querying for analogs - the query vector has the target's
    gates (station, market_type) set, and numeric dimensions derived from the
    target epoch's values.
    """
    return FeatureVector(
        settlement_jump_magnitude=0,  # Will be set from actual data
        reversion_occurred=0,
        max_excursion_above_settlement=0.0,
        duration_at_or_above_seconds=0.0,
        duration_strictly_above_seconds=0.0,
        terminal_state_reached=0,
        transition_count=0,
        settlement_bucket=settlement_bucket,
        reversion_latency_seconds=0.0,
        goldilocks_emitted=0,
        prior_settlement_bucket=None,
        local_trading_date_normalized=normalize_local_trading_date(local_trading_date),
        station=station,
        market_type=market_type,
    )


def extract_features_from_row(row: Tuple, column_names: List[str]) -> FeatureVector:
    """
    Extract FeatureVector from a raw SQLite row tuple.
    
    Args:
        row: Tuple of column values from SQLite query
        column_names: List of column names in order
        
    Returns:
        FeatureVector populated from row
    """
    row_dict = dict(zip(column_names, row))
    return extract_features_from_epoch(row_dict)
