# CHANGELOG (last 10 broad changes):
# 1. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Match Engine

Performs epoch similarity matching for analog forecasting.

Key features:
- Fixed weights and thresholds (no learned parameters)
- Handles sparse (<5 epochs), normal (5-29), and abundant (≥30) regimes
- Raw distance = Σ(w_i × d_i), match_score = 1.0 - (raw_distance / max_possible_distance)
- Thresholds: strong ≥0.85, weak 0.70-0.85, no match <0.70
- Gate matching: station and market_type must match exactly
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.p3_feature_extractor import (
    FeatureVector,
    calculate_match_score,
    extract_features_from_epoch,
    are_gates_compatible,
    is_station_gate_match,
    is_market_type_gate_match,
)


# Match quality thresholds (fixed, never learned)
STRONG_MATCH_THRESHOLD = 0.85
WEAK_MATCH_THRESHOLD = 0.70

# Regime thresholds
SPARSE_REGIME_THRESHOLD = 5  # < 5 epochs
ABUNDANT_REGIME_THRESHOLD = 30  # ≥ 30 epochs


@dataclass(frozen=True)
class Match:
    """A single matched analog epoch."""
    matched_epoch_id: int
    epoch_data: Dict[str, Any]
    match_score: float
    is_strong: bool
    is_weak: bool
    features: FeatureVector


@dataclass(frozen=True)
class MatchResult:
    """Result of matching query epoch against corpus."""
    query_epoch_id: int
    station: str
    market_type: Optional[str]
    local_trading_date: str
    strong_matches: List[Match]  # score >= 0.85
    weak_matches: List[Match]  # 0.70 <= score < 0.85
    total_analogs: int
    regime: str  # "sparse", "normal", or "abundant"
    min_score: float
    max_score: float
    avg_score: float


def is_epoch_compatible(
    query_features: FeatureVector,
    candidate_features: FeatureVector,
) -> Tuple[bool, float]:
    """
    Check if candidate epoch is compatible with query (gates match).
    
    Returns:
        Tuple of (is_compatible, distance_to_gate_match)
        distance_to_gate_match = 0 if gates match, 1.0 if not
    """
    station_match = is_station_gate_match(query_features, candidate_features)
    market_match = is_market_type_gate_match(query_features, candidate_features)
    
    if station_match and market_match:
        return True, 0.0
    else:
        # Gate mismatch = distance 1.0
        return False, 1.0


def find_similar_epochs(
    query_features: FeatureVector,
    corpus_epochs: List[Dict[str, Any]],
    min_analogs: int = 3,
) -> MatchResult:
    """
    Find similar epochs in the corpus to the query epoch.
    
    Args:
        query_features: Feature vector of the target epoch
        corpus_epochs: List of epoch dictionaries to search
        min_analogs: Minimum number of analogs required (default 3)
        
    Returns:
        MatchResult with strong/weak matches, regime info, and statistics
    """
    # Extract features from corpus epochs and calculate match scores
    matches: List[Match] = []
    
    for epoch_data in corpus_epochs:
        # Gate check first
        corpus_features = extract_features_from_epoch(epoch_data)
        gate_compatible, _ = is_epoch_compatible(query_features, corpus_features)
        
        if not gate_compatible:
            continue
        
        # Calculate match score
        score = calculate_match_score(query_features, corpus_features)
        
        # Determine match quality
        is_strong = score >= STRONG_MATCH_THRESHOLD
        is_weak = not is_strong and score >= WEAK_MATCH_THRESHOLD
        
        match = Match(
            matched_epoch_id=int(epoch_data.get("id") or 0),
            epoch_data=epoch_data,
            match_score=score,
            is_strong=is_strong,
            is_weak=is_weak,
            features=corpus_features,
        )
        matches.append(match)
    
    # Sort by score descending
    matches.sort(key=lambda m: m.match_score, reverse=True)
    
    # Separate strong and weak matches
    strong_matches = [m for m in matches if m.is_strong]
    weak_matches = [m for m in matches if m.is_weak]
    
    # Calculate statistics
    all_scores = [m.match_score for m in matches]
    min_score = min(all_scores) if all_scores else 0.0
    max_score = max(all_scores) if all_scores else 0.0
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    
    # Determine regime based on strong match count
    strong_count = len(strong_matches)
    if strong_count < SPARSE_REGIME_THRESHOLD:
        regime = "sparse"
    elif strong_count >= ABUNDANT_REGIME_THRESHOLD:
        regime = "abundant"
    else:
        regime = "normal"
    
    return MatchResult(
        query_epoch_id=int(query_features.settlement_bucket or 0),
        station=query_features.station or "UNKNOWN",
        market_type=query_features.market_type,
        local_trading_date=f"epoch_{query_features.settlement_bucket}",
        strong_matches=strong_matches,
        weak_matches=weak_matches,
        total_analogs=len(matches),
        regime=regime,
        min_score=min_score,
        max_score=max_score,
        avg_score=avg_score,
    )


def get_top_analogs(
    match_result: MatchResult,
    k: int = 10,
) -> List[Match]:
    """
    Get top K analogs from match result.
    
    Strategy:
    - Sparse regime: use weak matches if strong < 3, relaxed threshold τ=0.65
    - Normal regime: use all strong matches, fall back to weak if < 3
    - Abundant regime: use top K ranked by match_score × recency_factor
    """
    if match_result.regime == "abundant":
        # Use top K by score
        return match_result.strong_matches[:k]
    elif match_result.regime == "sparse":
        # Sparse regime - relax threshold and use weak matches
        threshold = 0.65
        return [m for m in match_result.strong_matches + match_result.weak_matches 
                if m.match_score >= threshold][:k]
    else:
        # Normal regime - use all strong matches
        return match_result.strong_matches[:k]


def calculate_recency_factor(epoch_data: Dict[str, Any]) -> float:
    """
    Calculate recency factor for an epoch (closer to current = higher weight).
    
    Uses epoch_id as proxy for recency (higher id = more recent).
    """
    epoch_id = int(epoch_data.get("id") or 0)
    # Recency factor: 1.0 for most recent, decaying for older
    # Using simple linear decay from 1.0
    return 1.0 - (epoch_id * 0.0001)  # Small decay per epoch


def rank_by_score_and_recency(
    matches: List[Match],
) -> List[Match]:
    """
    Rank matches by (match_score × recency_factor).
    
    This prioritizes both quality (score) and recency (recent epochs matter more).
    """
    ranked = []
    for match in matches:
        recency = calculate_recency_factor(match.epoch_data)
        weighted_score = match.match_score * recency
        ranked.append((match, weighted_score))
    
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [m for m, _ in ranked]


def get_best_analogs(
    match_result: MatchResult,
    min_strong: int = 3,
) -> Tuple[List[Match], bool]:
    """
    Get best analogs for prediction, handling sparse regimes gracefully.
    
    Returns:
        Tuple of (best_matches, has_divided_consensus)
        
    A "divided consensus" occurs when:
    - Top cluster and secondary cluster have weights within 20% of each other
    - This indicates conflicting trails that need special handling
    """
    # Start with strong matches
    best_matches = list(match_result.strong_matches)
    
    # If we have enough strong matches, use them
    if len(best_matches) >= min_strong:
        return best_matches, False
    
    # Sparse regime - fall back to weak matches
    weak_fallback = list(match_result.weak_matches)
    while len(best_matches) < min_strong and weak_fallback:
        best_matches.append(weak_fallback.pop(0))
    
    # Check for divided consensus (top vs second cluster within 20%)
    if len(best_matches) >= 2:
        top_score = best_matches[0].match_score
        second_score = best_matches[1].match_score
        if top_score > 0 and (top_score - second_score) / top_score <= 0.20:
            return best_matches, True
    
    return best_matches, False


def cluster_matches_by_outcome(
    matches: List[Match],
) -> List[List[Match]]:
    """
    Cluster matches by outcome similarity.
    
    Simple clustering based on settlement_bucket similarity.
    """
    if not matches:
        return []
    
    # Group by settlement_bucket (similar outcomes)
    clusters: Dict[int, List[Match]] = {}
    for match in matches:
        bucket = match.epoch_data.get("settlement_bucket", 0)
        if bucket not in clusters:
            clusters[bucket] = []
        clusters[bucket].append(match)
    
    # Return clusters sorted by size (largest first)
    cluster_list = list(clusters.values())
    cluster_list.sort(key=len, reverse=True)
    return cluster_list


def calculate_cluster_weight(
    cluster: List[Match],
) -> float:
    """
    Calculate cluster weight: (member_count × mean_match_score).
    
    This ranks clusters by influence - larger clusters with higher average scores
    have more weight in consensus resolution.
    """
    if not cluster:
        return 0.0
    
    member_count = len(cluster)
    mean_score = sum(m.match_score for m in cluster) / member_count
    
    return member_count * mean_score


def identify_conflicting_trails(
    match_result: MatchResult,
) -> Tuple[List[List[Match]], Optional[int]]:
    """
    Identify conflicting trail clusters.
    
    Returns:
        Tuple of (clusters, divided_cluster_index or None if consensus)
        
    A "divided" state occurs when top two clusters have weights within 20%.
    """
    clusters = cluster_matches_by_outcome(match_result.strong_matches)
    
    if len(clusters) < 2:
        return clusters, None
    
    # Calculate weights for top two clusters
    weights = [calculate_cluster_weight(c) for c in clusters[:2]]
    
    if weights[0] > 0 and abs(weights[0] - weights[1]) / weights[0] <= 0.20:
        return clusters, 1  # Divided between top two clusters
    
    return clusters, None
