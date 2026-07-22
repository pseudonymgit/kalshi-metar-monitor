# CHANGELOG (last 10 broad changes):
# 1. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Trajectory Tracer

Performs forward trajectory analysis from matched analog epochs.

Key features:
- Forward lookup: 3 epochs following matched epoch
- Cluster analysis: group trajectories by outcome similarity
- Consensus resolution: rank clusters by (member_count × mean_match_score)
- Consecutive match bonus: 3^(N-1), capped at 5
- Gap tolerance: 1-epoch gap gets 0.5 penalty multiplier
- Minimum trajectory length: 3 epochs
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.p3_match_engine import Match


# Trajectory parameters (fixed, never learned)
TRAJECTORY_LENGTH = 3  # Forward epochs to follow
MIN_TRAJECTORY_LENGTH = 3  # Minimum for predictive information
CONSECUTIVE_MATCH_WEIGHT_BASE = 3  # Base for exponential growth
CONSECUTIVE_MATCH_WEIGHT_MAX = 5  # Capped at 5

# Gap tolerance (1-epoch gap penalty)
GAP_PENALTY_MULTIPLIER = 0.5


@dataclass(frozen=True)
class Trajectory:
    """A trajectory following from an analog epoch."""
    analog_epoch_id: int
    analog_epoch_data: Dict[str, Any]
    forward_epochs: List[Dict[str, Any]]
    match_score: float
    consecutive_count: int  # Number of consecutive matches in trajectory
    has_gap: bool  # True if trajectory has a 1-epoch gap
    outcome_vector: Tuple[float, ...]  # Mean of forward epochs' key metrics


@dataclass(frozen=True)
class TrajectoryCluster:
    """Cluster of similar trajectories."""
    trajectories: List[Trajectory]
    cluster_id: int
    weight: float  # member_count × mean_match_score
    mean_match_score: float
    mean_outcome: Tuple[float, ...]


@dataclass(frozen=True)
class TrajectoryResult:
    """Result of trajectory tracing."""
    query_epoch_id: int
    matched_analogs: int
    successful_trajectories: List[Trajectory]
    trajectory_clusters: List[TrajectoryCluster]
    primary_projection: Optional[TrajectoryCluster]
    secondary_projection: Optional[TrajectoryCluster]
    has_divided_consensus: bool


def get_forward_trajectory(
    analog_match: Match,
    corpus_epochs: List[Dict[str, Any]],
    lookback: int = 3,
) -> Optional[Trajectory]:
    """
    Get forward trajectory from an analog epoch.
    
    Looks for TRAJECTORY_LENGTH epochs that follow the matched epoch in chronological order.
    
    Returns None if:
    - No epochs exist after the matched epoch
    - Found epochs < MIN_TRAJECTORY_LENGTH
    """
    # Find index of matched epoch in corpus (sorted by id)
    matched_id = analog_match.matched_epoch_id
    matched_idx = None
    
    sorted_epochs = sorted(corpus_epochs, key=lambda e: e.get("id", 0))
    
    for i, epoch in enumerate(sorted_epochs):
        if epoch.get("id") == matched_id:
            matched_idx = i
            break
    
    if matched_idx is None or matched_idx >= len(sorted_epochs) - 1:
        return None  # No forward epochs
    
    # Get forward epochs (next TRAJECTORY_LENGTH epochs)
    forward_epochs = []
    consecutive_count = 1
    gap_detected = False
    
    for i in range(matched_idx + 1, min(matched_idx + TRAJECTORY_LENGTH + 2, len(sorted_epochs))):
        epoch = sorted_epochs[i]
        
        # Check if this is consecutive (no gap)
        if forward_epochs:
            last_id = forward_epochs[-1].get("id", 0)
            if epoch.get("id", 0) == last_id + 1:
                consecutive_count += 1
            else:
                gap_detected = True
        
        forward_epochs.append(epoch)
        
        if len(forward_epochs) >= TRAJECTORY_LENGTH:
            break
    
    # Validate minimum trajectory length
    if len(forward_epochs) < MIN_TRAJECTORY_LENGTH:
        return None
    
    # Calculate consecutive match weight
    consecutive_weight = min(
        CONSECUTIVE_MATCH_WEIGHT_BASE ** (consecutive_count - 1),
        CONSECUTIVE_MATCH_WEIGHT_MAX,
    )
    
    # Apply gap penalty if detected
    if gap_detected:
        consecutive_weight *= GAP_PENALTY_MULTIPLIER
    
    # Calculate outcome vector (mean of forward epochs' key metrics)
    settlement_jumps = [e.get("settlement_jump_magnitude", 0) for e in forward_epochs if e.get("settlement_jump_magnitude") is not None]
    reversions = [1 if e.get("reversion_occurred") else 0 for e in forward_epochs]
    terminals = [1 if e.get("terminal_state_reached") else 0 for e in forward_epochs]
    
    outcome_vector = (
        sum(settlement_jumps) / len(settlement_jumps) if settlement_jumps else 0.0,
        sum(reversions) / len(reversions) if reversions else 0.0,
        sum(terminals) / len(terminals) if terminals else 0.0,
        consecutive_weight,
    )
    
    return Trajectory(
        analog_epoch_id=matched_id,
        analog_epoch_data=analog_match.epoch_data,
        forward_epochs=forward_epochs,
        match_score=analog_match.match_score,
        consecutive_count=consecutive_count,
        has_gap=gap_detected,
        outcome_vector=outcome_vector,
    )


def trace_all_trajectories(
    matches: List[Match],
    corpus_epochs: List[Dict[str, Any]],
) -> TrajectoryResult:
    """
    Trace forward trajectories from all matched analogs.
    
    Args:
        matches: List of matched epochs (strong and weak)
        corpus_epochs: Full corpus of epoch data
        
    Returns:
        TrajectoryResult with all successful trajectories and clusters
    """
    successful_trajectories: List[Trajectory] = []
    
    for match in matches:
        trajectory = get_forward_trajectory(match, corpus_epochs)
        if trajectory:
            successful_trajectories.append(trajectory)
    
    # Cluster trajectories by outcome similarity
    clusters = cluster_trajectories_by_outcome(successful_trajectories)
    
    # Calculate weights and find primary/secondary
    if clusters:
        clusters.sort(key=lambda c: c.weight, reverse=True)
        
        primary = clusters[0]
        secondary = clusters[1] if len(clusters) > 1 else None
        
        # Check for divided consensus (top two within 20% of each other)
        has_divided = False
        if secondary and primary.weight > 0:
            diff = abs(primary.weight - secondary.weight) / primary.weight
            has_divided = diff <= 0.20
        
        return TrajectoryResult(
            query_epoch_id=matches[0].matched_epoch_id if matches else 0,
            matched_analogs=len(matches),
            successful_trajectories=successful_trajectories,
            trajectory_clusters=clusters,
            primary_projection=primary,
            secondary_projection=secondary,
            has_divided_consensus=has_divided,
        )
    
    return TrajectoryResult(
        query_epoch_id=matches[0].matched_epoch_id if matches else 0,
        matched_analogs=len(matches),
        successful_trajectories=[],
        trajectory_clusters=[],
        primary_projection=None,
        secondary_projection=None,
        has_divided_consensus=False,
    )


def cluster_trajectories_by_outcome(
    trajectories: List[Trajectory],
) -> List[TrajectoryCluster]:
    """
    Cluster trajectories by outcome similarity.
    
    Clusters by settlement_bucket of first forward epoch.
    """
    if not trajectories:
        return []
    
    # Group by outcome similarity (settlement bucket of first forward epoch)
    clusters: Dict[int, List[Trajectory]] = {}
    for traj in trajectories:
        if traj.forward_epochs:
            first_bucket = traj.forward_epochs[0].get("settlement_bucket", 0)
        else:
            first_bucket = 0
        
        if first_bucket not in clusters:
            clusters[first_bucket] = []
        clusters[first_bucket].append(traj)
    
    # Create cluster objects with weights
    result = []
    for cluster_id, (bucket, traj_list) in enumerate(clusters.items()):
        mean_score = sum(t.match_score for t in traj_list) / len(traj_list)
        
        # Calculate mean outcome vector
        n = len(traj_list)
        mean_outcome = tuple(
            sum(t.outcome_vector[i] for t in traj_list) / n
            for i in range(len(traj_list[0].outcome_vector))
        ) if traj_list else (0.0, 0.0, 0.0, 0.0)
        
        weight = len(traj_list) * mean_score
        
        result.append(TrajectoryCluster(
            trajectories=traj_list,
            cluster_id=cluster_id,
            weight=weight,
            mean_match_score=mean_score,
            mean_outcome=mean_outcome,
        ))
    
    result.sort(key=lambda c: c.weight, reverse=True)
    return result


def get_trajectory_weight(traj: Trajectory) -> float:
    """
    Calculate trajectory weight: match_score × consecutive_bonus.
    
    Consecutive bonus: 3^(N-1), capped at 5
    Gap penalty: 0.5 multiplier if any gap
    """
    # Base consecutive bonus: 3^(N-1), capped at 5
    consecutive_bonus = min(
        CONSECUTIVE_MATCH_WEIGHT_BASE ** (traj.consecutive_count - 1),
        CONSECUTIVE_MATCH_WEIGHT_MAX,
    )
    
    # Apply gap penalty if detected
    if traj.has_gap:
        consecutive_bonus *= GAP_PENALTY_MULTIPLIER
    
    return traj.match_score * consecutive_bonus


def calculate_trajectory_outcome_similarity(
    outcome_a: Tuple[float, ...],
    outcome_b: Tuple[float, ...],
) -> float:
    """
    Calculate similarity between two outcome vectors.
    
    Uses cosine similarity for the trajectory direction components.
    """
    if len(outcome_a) != len(outcome_b):
        return 0.0
    
    # Dot product
    dot = sum(a * b for a, b in zip(outcome_a, outcome_b))
    
    # Magnitudes
    mag_a = (sum(a ** 2 for a in outcome_a)) ** 0.5
    mag_b = (sum(b ** 2 for b in outcome_b)) ** 0.5
    
    if mag_a == 0 or mag_b == 0:
        return 0.0
    
    return dot / (mag_a * mag_b)


def rank_clusters_by_weight(
    clusters: List[TrajectoryCluster],
) -> List[TrajectoryCluster]:
    """
    Rank clusters by (member_count × mean_match_score) weight.
    
    Higher weight = more confidence in that cluster's consensus.
    """
    return sorted(clusters, key=lambda c: c.weight, reverse=True)


def get_consensus_projection(
    result: TrajectoryResult,
) -> Tuple[Optional[TrajectoryCluster], Optional[TrajectoryCluster]]:
    """
    Get primary and secondary consensus projections.
    
    If has_divided_consensus is True, both projections are reported.
    Otherwise, only the primary projection is authoritative.
    """
    if result.primary_projection:
        if result.has_divided_consensus and result.secondary_projection:
            return result.primary_projection, result.secondary_projection
        return result.primary_projection, None
    
    return None, None
