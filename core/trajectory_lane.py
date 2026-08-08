#!/usr/bin/env python3
"""
Trajectory Lane — Heavy Informant, Not Gate

Produces a secondary probability estimate P(trajectory_direction) based on
historical analog matching, weighted into the fusion layer as a precision
modulator. Does NOT block trades — it is a heavy informant.

Per design:
  - w_traj = 0.15 * traj_quality   (max 0.15 influence on fused probability)
  - traj_quality in [0, 1], composite of analog_count, DTW similarity,
    station match, and recency
  - Output: (direction, probability, weight) where weight = w_traj

Reuses from trajectory_confirmation_gate.py:
  - EpochBuilder
  - AnalogMatcher
  - Epoch, STATION_TO_ZONE, CLIMATE_ZONES, FEATURE_WEIGHTS, BUCKET_THRESHOLDS_HIGH
  - _classify_bucket, bucket_is_warm, bucket_is_cool

Data sources (local, no external API):
  - data/metar_backfill.db   -> METAR observations
  - data/kalshi_settlements.db -> settlement temperatures
"""

import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.trajectory_confirmation_gate import (
    Epoch,
    EpochBuilder,
    AnalogMatcher,
    STATION_TO_ZONE,
    CLIMATE_ZONES,
    FEATURE_WEIGHTS,
    BUCKET_THRESHOLDS_HIGH,
    _classify_bucket,
    bucket_is_warm,
    bucket_is_cool,
    EpochBuildError,
    SAME_STATION_MULTIPLIER,
    CROSS_ZONE_MULTIPLIER,
    DEFAULT_EPOCH_DB,
    METAR_DB,
    SETTLEMENTS_DB,
)

# -- Paths --------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent

# -- Quality Scoring Weights (from TRAJECTORY-LANE-DESIGN.md) -----------------
QUALITY_WEIGHTS = {
    "analog_count": 0.40,
    "dtw_similarity": 0.30,
    "station_match": 0.20,
    "recency": 0.10,
}

# Max trajectory weight cap
MAX_W_TRAJ = 0.15

# Analog count score thresholds
ANALOG_COUNT_HIGH = 100    # Score = 1.0
ANALOG_COUNT_MED = 50      # Score = 0.6
ANALOG_COUNT_LOW = 20      # Score = 0.3

# DTW distance thresholds (lower is better — mean of top-10)
DTW_GOOD = 0.20    # Score = 1.0
DTW_FAIR = 0.35    # Score = 0.6
DTW_WEAK = 0.50    # Score = 0.2

# Recency threshold — corpus freshness in days
RECENCY_DAYS = 90

# -- Trajectory Confidence Thresholds -----------------------------------------
HIGH_CONFIDENCE_THRESHOLD = 0.7
MEDIUM_CONFIDENCE_THRESHOLD = 0.3
LOW_CONFIDENCE_THRESHOLD = 0.0  # anything below 0.3


# =============================================================================
# TRAJECTORY LANE — Heavy Informant
# =============================================================================

class TrajectoryLane:
    """
    The trajectory lane produces a secondary probability estimate based on
    historical analog matching. It is a HEAVY INFORMANT, not a gate — it never
    blocks trades; it only modulates confidence.

    Output tuple: (lane_direction, lane_probability, weight)
      - lane_direction: 'up', 'down', or None (insufficient data)
      - lane_probability: P(direction) in [0.5, 1.0]
      - weight: w_traj = 0.15 * traj_quality, applied to fused probability

    Integration into the fusion layer:
      fused_prob = (1 - w_traj) * ensemble_prob + w_traj * trajectory_prob
    """

    def __init__(self, epoch_db: str = DEFAULT_EPOCH_DB,
                 metar_db: str = METAR_DB,
                 settlements_db: str = SETTLEMENTS_DB):
        self.matcher = AnalogMatcher(epoch_db, metar_db, settlements_db)
        self._builder = EpochBuilder(metar_db, settlements_db)

    # ── Public API Methods ──────────────────────────────────────────────────

    def evaluate(self, station: str, date: str,
                 gefs_probability: float,
                 signal_direction: str) -> Tuple[Optional[str], float, float]:
        """
        Produce the trajectory lane estimate for a given station and date.

        Args:
            station: ICAO station code (e.g. 'KNYC')
            date: UTC date string 'YYYY-MM-DD'
            gefs_probability: GEFS ensemble probability for direction (0-1)
            signal_direction: 'up' or 'down'

        Returns:
            (lane_direction, lane_probability, weight)
            - lane_direction: 'up', 'down', or None (insufficient data)
            - lane_probability: P(direction) in [0, 1]
            - weight: w_traj (0 to MAX_W_TRAJ), applied as precision modulator

        If insufficient data, returns (None, 0.5, 0.0) so the lane has zero
        influence on the fusion layer.
        """
        # 1. Build the query epoch
        try:
            epoch = self._builder.build_epoch(station, date)
        except EpochBuildError:
            return (None, 0.5, 0.0)

        # 2. Find analogs
        analogs = self.matcher.find_analogs(epoch, top_k=200)
        if len(analogs) < 10:
            return (None, 0.5, 0.0)

        # 3. Compute trajectory quality score
        traj_quality = self.get_trajectory_quality(station, date)
        weight = MAX_W_TRAJ * traj_quality  # w_traj

        # 4. Compute trajectory bucket distribution
        traj_dist = self.matcher.compute_trajectory_distribution(analogs)
        if traj_dist is None:
            return (None, 0.5, weight)

        # 5. Determine trajectory direction and probability
        lane_direction, lane_probability = self._compute_trajectory_estimate(
            traj_dist, gefs_probability, signal_direction
        )

        return (lane_direction, lane_probability, weight)

    def get_trajectory_quality(self, station: str,
                                date: str) -> float:
        """
        Compute the trajectory quality score [0, 1] for a station and date.

        Composite score based on:
          - Analog count (40%): <20=0, 20-50=0.3, 50-100=0.6, >100=1.0
          - DTW similarity (30%): Mean distance of top-10 matches [0, 1]
          - Station match (20%): 1.0 if same-station, 0.5 if cross-station only
          - Recency (10%): 1.0 if corpus includes last 90 days, 0.5 otherwise

        Returns:
            traj_quality in [0, 1]
        """
        # We need at least the analogs to compute quality
        try:
            epoch = self._builder.build_epoch(station, date)
        except EpochBuildError:
            return 0.0

        analogs = self.matcher.find_analogs(epoch, top_k=200)
        if len(analogs) < 10:
            return 0.0

        return self._compute_quality_from_analogs(analogs, station, date)

    def get_trajectory_quality_from_analogs(
        self, analogs: List[Dict], station: str, date: str
    ) -> float:
        """Compute trajectory quality from pre-fetched analogs. Same scoring as
        get_trajectory_quality() but accepts an analogs list directly."""
        if len(analogs) < 10:
            return 0.0
        return self._compute_quality_from_analogs(analogs, station, date)

    # ── Internal Methods ────────────────────────────────────────────────────

    def _compute_quality_from_analogs(
        self, analogs: List[Dict], station: str, date: str
    ) -> float:
        """Compute traj_quality [0,1] from a list of analog results."""

        # 1. Analog count score (40%)
        n_analogs = len(analogs)
        if n_analogs >= ANALOG_COUNT_HIGH:
            analog_score = 1.0
        elif n_analogs >= ANALOG_COUNT_MED:
            analog_score = 0.6 + 0.4 * (n_analogs - ANALOG_COUNT_MED) / (ANALOG_COUNT_HIGH - ANALOG_COUNT_MED)
        elif n_analogs >= ANALOG_COUNT_LOW:
            analog_score = 0.3 + 0.3 * (n_analogs - ANALOG_COUNT_LOW) / (ANALOG_COUNT_MED - ANALOG_COUNT_LOW)
        else:
            analog_score = 0.3 * n_analogs / ANALOG_COUNT_LOW

        # 2. DTW similarity score (30%) — mean distance of top-10 matches
        top_k = min(10, len(analogs))
        if top_k > 0:
            mean_dtw = float(np.mean([a["distance"] for a in analogs[:top_k]]))
            if mean_dtw <= DTW_GOOD:
                dtw_score = 1.0
            elif mean_dtw <= DTW_FAIR:
                dtw_score = 0.6 + 0.4 * (DTW_FAIR - mean_dtw) / (DTW_FAIR - DTW_GOOD)
            elif mean_dtw <= DTW_WEAK:
                dtw_score = 0.2 + 0.4 * (DTW_WEAK - mean_dtw) / (DTW_WEAK - DTW_FAIR)
            else:
                dtw_score = max(0.0, 0.2 * (DTW_WEAK * 2 - mean_dtw) / DTW_WEAK)
        else:
            dtw_score = 0.0

        # 3. Station match score (20%)
        same_station_count = sum(1 for a in analogs if a.get("is_same_station", False))
        total_analogs = len(analogs)
        if total_analogs > 0:
            same_station_frac = same_station_count / total_analogs
            # Score: 1.0 if all same-station, 0.5 if none same-station, interpolated
            station_score = 0.5 + 0.5 * same_station_frac
        else:
            station_score = 0.5  # Default cross-station only

        # 4. Recency score (10%) — does the corpus include recent data?
        recency_score = self._compute_recency_score(station, date)

        # Composite
        traj_quality = (
            QUALITY_WEIGHTS["analog_count"] * analog_score
            + QUALITY_WEIGHTS["dtw_similarity"] * dtw_score
            + QUALITY_WEIGHTS["station_match"] * station_score
            + QUALITY_WEIGHTS["recency"] * recency_score
        )

        return max(0.0, min(1.0, traj_quality))

    def _compute_recency_score(self, station: str, query_date: str) -> float:
        """Check if the corpus includes data within the last RECENCY_DAYS days."""
        try:
            q_dt = datetime.strptime(query_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return 0.5

        # Check the epoch DB for recent entries from this station
        corpus = self.matcher._load_epochs_from_db()
        if not corpus:
            return 0.5

        recent_dates = [
            r["date"] for r in corpus
            if r["station"] == station
        ]
        if not recent_dates:
            return 0.5

        # Find the most recent date in corpus for this station
        try:
            latest_str = max(recent_dates)
            latest_dt = datetime.strptime(latest_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            return 0.5

        days_diff = abs((q_dt - latest_dt).days)
        if days_diff <= RECENCY_DAYS:
            return 1.0
        else:
            return 0.5

    def _compute_trajectory_estimate(
        self,
        traj_dist: Dict[str, float],
        gefs_probability: float,
        signal_direction: str,
    ) -> Tuple[Optional[str], float]:
        """
        Compute the trajectory lane's direction and probability estimate.

        Uses bucket distribution from analog outcomes:
          - P(warm) = sum of probabilities for warm buckets
          - P(cool) = sum of probabilities for cool buckets

        The trajectory lane compares its own estimate against the GEFS signal
        to determine influence, not to override it.

        Returns:
            (lane_direction, lane_probability)
            - lane_direction: 'up', 'down', or None (ambiguous)
            - lane_probability: P(direction) in [0, 1]; 0.5 if ambiguous
        """
        p_warm = sum(p for b, p in traj_dist.items() if bucket_is_warm(b))
        p_cool = sum(p for b, p in traj_dist.items() if bucket_is_cool(b))

        # Threshold for directional conviction
        if p_warm > 0.55:
            return ("up", p_warm)
        elif p_cool > 0.55:
            return ("down", p_cool)
        else:
            # Ambiguous — the lane defers to the GEFS signal but provides
            # a weak probability based on the slight directional lean
            if p_warm > p_cool:
                return ("up", min(0.55, p_warm))
            elif p_cool > p_warm:
                return ("down", min(0.55, p_cool))
            else:
                return (None, 0.5)

    def compute_diagnostic_packet(
        self, station: str, date: str,
        gefs_probability: float, signal_direction: str
    ) -> Dict:
        """
        Produce a full diagnostic packet for analysis (per spec JSON example).

        Returns a dict with:
          - station, date, trajectory_days, analog_count
          - analog_same_station, analog_same_climate
          - mean_dtw_distance, bucket_distribution
          - recommended_buckets, traj_quality, w_traj, regime_divergence
        """
        try:
            epoch = self._builder.build_epoch(station, date)
        except EpochBuildError:
            return {"station": station, "date": date, "error": "epoch_build_error"}

        analogs = self.matcher.find_analogs(epoch, top_k=200)
        n_analogs = len(analogs)

        if n_analogs < 10:
            return {
                "station": station, "date": date,
                "trajectory_days": 5, "analog_count": n_analogs,
                "error": "insufficient_analogs"
            }

        traj_dist = self.matcher.compute_trajectory_distribution(analogs)
        if traj_dist is None:
            return {
                "station": station, "date": date,
                "trajectory_days": 5, "analog_count": n_analogs,
                "error": "no_trajectory_distribution"
            }

        traj_quality = self.get_trajectory_quality_from_analogs(analogs, station, date)
        w_traj = MAX_W_TRAJ * traj_quality

        # Count same-station and same-climate analogs
        same_station = sum(1 for a in analogs if a.get("is_same_station", False))
        same_climate = sum(1 for a in analogs if a.get("is_same_zone", False) and not a.get("is_same_station", False))

        # Mean DTW of top-10
        top_k = min(10, n_analogs)
        mean_dtw = float(np.mean([a["distance"] for a in analogs[:top_k]])) if top_k > 0 else 0.0

        # Bucket distribution with standard error
        total_analogs = sum(traj_dist.values())
        bucket_dist = {}
        for bucket, count_pct in sorted(traj_dist.items()):
            count = int(count_pct * n_analogs)
            se = math.sqrt(count_pct * (1 - count_pct) / n_analogs) if n_analogs > 0 else 0.0
            bucket_dist[bucket] = {
                "count": count,
                "pct": count_pct,
                "se": se,
            }

        # Determine recommended buckets
        recommended = []
        sorted_buckets = sorted(traj_dist.items(), key=lambda x: x[1], reverse=True)
        for bucket, pct in sorted_buckets:
            count = int(pct * n_analogs)
            if count >= 20 and bucket_dist[bucket]["se"] <= 0.10:
                action = "CONSIDER_POSITION"
                recommended.append({"bucket": bucket, "traj_prob": pct, "action": action})
            elif count >= 10:
                recommended.append({"bucket": bucket, "traj_prob": pct, "action": "MARGINAL"})

        # Regime divergence check
        lane_dir, lane_prob = self._compute_trajectory_estimate(traj_dist, gefs_probability, signal_direction)
        regime_divergence = False
        if lane_dir is not None and lane_dir != signal_direction:
            if traj_quality >= MEDIUM_CONFIDENCE_THRESHOLD:
                regime_divergence = True

        lane_direction, lane_probability, _ = self.evaluate(station, date, gefs_probability, signal_direction)

        return {
            "station": station,
            "date": date,
            "trajectory_days": 5,
            "analog_count": n_analogs,
            "analog_same_station": same_station,
            "analog_same_climate": same_climate,
            "mean_dtw_distance": round(mean_dtw, 4),
            "bucket_distribution": bucket_dist,
            "recommended_buckets": recommended,
            "traj_quality": round(traj_quality, 4),
            "w_traj": round(w_traj, 4),
            "regime_divergence": regime_divergence,
            "lane_direction": lane_direction,
            "lane_probability": round(lane_probability, 4),
        }


# =============================================================================
# Convenience / Main Entry Point
# =============================================================================

def evaluate_lane_for_station_date(
    station: str, date: str,
    gefs_probability: float,
    signal_direction: str,
    lane: Optional[TrajectoryLane] = None,
) -> Tuple[Optional[str], float, float, float, bool]:
    """
    One-shot trajectory lane evaluation.

    Args:
        station: ICAO station code
        date: 'YYYY-MM-DD'
        gefs_probability: GEFS ensemble probability for the signal direction
        signal_direction: 'up' or 'down'
        lane: optional TrajectoryLane instance (lazy-created if None)

    Returns:
        (lane_direction, lane_probability, weight, traj_quality, regime_divergence)
    """
    if lane is None:
        lane = TrajectoryLane()

    lane_dir, lane_prob, weight = lane.evaluate(station, date, gefs_probability, signal_direction)
    traj_quality = lane.get_trajectory_quality(station, date)
    regime_divergence = False
    if lane_dir is not None and lane_dir != signal_direction and traj_quality >= MEDIUM_CONFIDENCE_THRESHOLD:
        regime_divergence = True

    return (lane_dir, lane_prob, weight, traj_quality, regime_divergence)


def apply_trajectory_lane_to_probability(
    ensemble_prob: float,
    lane_direction: Optional[str],
    lane_probability: float,
    lane_weight: float,
) -> float:
    """
    Apply the trajectory lane as a precision modulator to a fused probability.

    fused_prob = (1 - w_traj) * ensemble_prob + w_traj * trajectory_prob

    If lane_direction is None (insufficient data), returns ensemble_prob unmodified.
    """
    if lane_direction is None or lane_weight <= 0:
        return ensemble_prob

    return (1.0 - lane_weight) * ensemble_prob + lane_weight * lane_probability


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    """CLI entry point for testing the trajectory lane."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Trajectory Lane — Heavy Informant Diagnostic"
    )
    parser.add_argument("--station", default="KNYC", help="ICAO station code")
    parser.add_argument("--date", default=None, help="Date in YYYY-MM-DD format")
    parser.add_argument("--direction", default="up", choices=["up", "down"],
                        help="Signal direction to test")
    parser.add_argument("--probability", type=float, default=0.65,
                        help="GEFS ensemble probability (0-1)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="Print full diagnostic packet")
    args = parser.parse_args()

    if args.date is None:
        from datetime import timezone
        args.date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lane = TrajectoryLane()

    if args.diagnostic:
        packet = lane.compute_diagnostic_packet(
            args.station, args.date,
            args.probability, args.direction
        )
        import json
        print(json.dumps(packet, indent=2, default=str))
        return

    lane_dir, lane_prob, weight = lane.evaluate(
        args.station, args.date,
        args.probability, args.direction
    )
    traj_quality = lane.get_trajectory_quality(args.station, args.date)

    print(f"  Station: {args.station}")
    print(f"  Date: {args.date}")
    print(f"  GEFS signal: {args.direction} @ {args.probability:.4f}")
    print(f"  Trajectory quality: {traj_quality:.4f}")
    print(f"  Lane direction: {lane_dir}")
    print(f"  Lane probability: {lane_prob:.4f}")
    print(f"  Weight (w_traj): {weight:.4f}")
    print(f"  Fused probability: {apply_trajectory_lane_to_probability(args.probability, lane_dir, lane_prob, weight):.4f}")


if __name__ == "__main__":
    main()