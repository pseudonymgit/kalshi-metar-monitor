#!/usr/bin/env python3
"""
Spatial Coherence Gate — Regional Consensus Confidence Modifier

Applied AFTER signal generation, BEFORE ensemble voting.

For each station region, computes regional consensus on direction (up/down/neutral)
and adjusts individual station confidence based on agreement with the region.

Regions:
  - Northeast: KNYC, KBOS, KPHL, KDCA
  - Southeast: KATL, KMIA, KMSY, KHOU
  - Midwest: KMDW, KMSP, KDFW, KOKC
  - West Coast: KLAX, KSFO, KSEA
  - Southwest: KPHX, KLAS, KDEN, KSAT, KAUS

Modulation:
  - City disagrees with region, both low confidence  (<0.70): reduce 30-50%
  - City high confidence (>0.80) but region disagrees: cap at 0.70
  - Region strong consensus (>0.80) and city agrees: boost 10-20%

Usage:
    from core.spatial_coherence import apply_spatial_coherence_gate
    adjusted_signals = apply_spatial_coherence_gate(per_station_signals)
"""

import logging
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

# ─── Station Regions ────────────────────────────────────────────────────────

STATION_REGIONS = {
    "northeast": ["KNYC", "KBOS", "KPHL", "KDCA"],
    "southeast": ["KATL", "KMIA", "KMSY", "KHOU"],
    "midwest":   ["KMDW", "KMSP", "KDFW", "KOKC"],
    "west_coast": ["KLAX", "KSFO", "KSEA"],
    "southwest": ["KPHX", "KLAS", "KDEN", "KSAT", "KAUS"],
}

# Reverse mapping: station -> region name
_STATION_TO_REGION = {
    station: region
    for region, stations in STATION_REGIONS.items()
    for station in stations
}

# Default confidence boost/reduction factors
CONFIDENCE_REDUCTION_LOW = 0.6     # 40% reduction: city disagrees region, both low
CONFIDENCE_REDUCTION_HIGH = 0.5    # 50% reduction: if both very low
CONFIDENCE_CAP = 0.70              # Cap: city high conf but region disagrees
CONFIDENCE_BOOST_MIN = 1.10        # 10% boost: region strong consensus, city agrees
CONFIDENCE_BOOST_MAX = 1.20        # 20% boost: very strong agreement


def get_region_for_station(station: str) -> Optional[str]:
    """Return the region name for a station, or None if unassigned."""
    return _STATION_TO_REGION.get(station.upper())


def get_stations_in_region(region: str) -> List[str]:
    """Return the list of stations in a region."""
    return STATION_REGIONS.get(region, [])


def compute_regional_consensus(
    station_signals: Dict[str, Tuple[Optional[str], float]],
) -> Dict[str, Dict]:
    """
    For each region, compute the majority direction and consensus strength.

    Args:
        station_signals: dict mapping station code -> (direction, confidence)

    Returns:
        dict mapping region name -> {
            'majority_direction': 'up'|'down'|None,
            'consensus_strength': float (0.0-1.0),
            'total_signals': int,
            'agreeing': int,
        }
    """
    # Group stations by region
    region_votes: Dict[str, Dict[str, list]] = {}
    for station, (direction, confidence) in station_signals.items():
        region = get_region_for_station(station)
        if region is None or direction is None:
            continue
        if region not in region_votes:
            region_votes[region] = {"up": [], "down": []}
        region_votes[region][direction].append((station, confidence))

    result = {}
    for region, votes in region_votes.items():
        up_count = len(votes["up"])
        down_count = len(votes["down"])
        total = up_count + down_count

        if total == 0:
            result[region] = {
                "majority_direction": None,
                "consensus_strength": 0.0,
                "total_signals": 0,
                "agreeing": 0,
            }
            continue

        if up_count > down_count:
            majority = "up"
            agreeing = up_count
        elif down_count > up_count:
            majority = "down"
            agreeing = down_count
        else:
            majority = None  # tie
            agreeing = total  # counts as perfect agreement on "no consensus"

        consensus_strength = agreeing / total

        result[region] = {
            "majority_direction": majority,
            "consensus_strength": consensus_strength,
            "total_signals": total,
            "agreeing": agreeing,
        }

    return result


def apply_spatial_coherence_gate(
    per_station_signals: Dict[str, Tuple[Optional[str], float]],
) -> Dict[str, Tuple[Optional[str], float]]:
    """
    Apply spatial coherence gate to adjust confidence values.

    The gate is applied AFTER signal generation, BEFORE ensemble voting.

    Args:
        per_station_signals: dict mapping station code -> (direction, confidence)

    Returns:
        dict mapping station code -> (direction, adjusted_confidence)
        Only stations in known regions are modified; others pass through unchanged.
    """
    if not per_station_signals:
        return per_station_signals

    adjusted = dict(per_station_signals)  # Copy to avoid mutating input

    # Compute regional consensus
    region_consensus = compute_regional_consensus(per_station_signals)

    for station, (direction, confidence) in adjusted.items():
        region = get_region_for_station(station)
        if region is None or direction is None:
            continue

        consensus = region_consensus.get(region)
        if consensus is None or consensus["majority_direction"] is None:
            continue

        station_agrees = (direction == consensus["majority_direction"])
        consensus_strength = consensus["consensus_strength"]

        if station_agrees:
            # City agrees with regional consensus
            if consensus_strength > 0.80:
                boost = CONFIDENCE_BOOST_MIN + (CONFIDENCE_BOOST_MAX - CONFIDENCE_BOOST_MIN) * (
                    (consensus_strength - 0.80) / 0.20
                )
                new_conf = min(1.0, confidence * boost)
                _logger.debug(
                    f"SpatialGate: {station} agrees with {region} consensus "
                    f"(strength={consensus_strength:.2f}), boosting confidence "
                    f"{confidence:.3f} -> {new_conf:.3f}"
                )
                adjusted[station] = (direction, new_conf)

        else:
            # City disagrees with regional consensus
            if confidence < 0.70 and consensus_strength < 0.70:
                # Both low confidence: reduce by 40-50%
                reduction = CONFIDENCE_REDUCTION_HIGH if (confidence < 0.50 and consensus_strength < 0.50) else CONFIDENCE_REDUCTION_LOW
                new_conf = confidence * reduction
                _logger.debug(
                    f"SpatialGate: {station} disagrees with {region} consensus "
                    f"(both low conf), reducing {confidence:.3f} -> {new_conf:.3f}"
                )
                adjusted[station] = (direction, new_conf)

            elif confidence > 0.80:
                # City is confident but region disagrees: cap at 0.70
                new_conf = min(CONFIDENCE_CAP, confidence)
                _logger.debug(
                    f"SpatialGate: {station} disagrees with {region} consensus "
                    f"(high city conf), capping {confidence:.3f} -> {new_conf:.3f}"
                )
                adjusted[station] = (direction, new_conf)

    return adjusted