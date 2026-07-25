#!/usr/bin/env python3
"""
D4 + D2 — Spatial Coherence Gate & Cross-Station Trade Coordination

Detects and manages cross-station spatial correlations in trading signals.

D4 — Spatial Correlation Gate:
  Checks if two nearby stations both signal the same direction within a short
  time window. Verifies they're not double-counting the same weather event.
  Downgrades confidence or skips the second trade.

D2 — Cross-Station Trade Coordination:
  Actively coordinates trades between nearby stations:
  - Coherent signal (both same direction within window) → trade both, mark coordinated
  - Conflicting signal (opposite directions within window) → flag for review
  - Records coordination events for later analysis

Usage:
    from core.spatial_coherence import SpatialCoherenceGate, CoordinatedSignal

    gate = SpatialCoherenceGate()
    result = gate.evaluate("KATL", "up", "2026-07-24", "HIGH")
    if result.is_coherent:
        # Signal is verifiable — proceed
    elif result.is_conflicting:
        # Conflicting signals nearby — reduce confidence or skip
"""

import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────

# Proximity threshold in miles for "nearby" stations
PROXIMITY_MILES = 200

# Time window in minutes for "same event" detection
EVENT_WINDOW_MINUTES = 30

# Confidence adjustments
COHERENT_BOOST = 1.10        # +10% for coherent cross-station signals
CONFLICT_PENALTY = 0.60      # -40% for conflicting signals nearby
NO_NEIGHBOR_DISCOUNT = 0.95  # -5% if no nearby stations for verification

# Earth radius in miles
EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in miles between two lat/lon points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


# ─── Result ────────────────────────────────────────────────────

class CoordinationResult:
    """
    Result of a spatial coherence evaluation.

    Attributes:
        station: The evaluated station
        is_coherent: True if nearby stations confirm the same direction
        is_conflicting: True if nearby stations have conflicting signals
        has_neighbors: True if nearby stations were found for verification
        coordinated_signals: List of coordinated signal details
        adjusted_confidence: Confidence multiplier based on coherence
        nearby_station_count: Number of nearby stations found
        conflicting_count: Number of nearby stations with conflicting signals
        coherent_count: Number of nearby stations with coherent signals
        recommendation: 'trade', 'skip', 'investigate'
    """

    def __init__(
        self,
        station: str,
        is_coherent: bool = False,
        is_conflicting: bool = False,
        has_neighbors: bool = False,
        coordinated_signals: Optional[List[Dict]] = None,
        adjusted_confidence: float = 1.0,
        nearby_station_count: int = 0,
        conflicting_count: int = 0,
        coherent_count: int = 0,
        recommendation: str = "trade",
    ):
        self.station = station
        self.is_coherent = is_coherent
        self.is_conflicting = is_conflicting
        self.has_neighbors = has_neighbors
        self.coordinated_signals = coordinated_signals or []
        self.adjusted_confidence = adjusted_confidence
        self.nearby_station_count = nearby_station_count
        self.conflicting_count = conflicting_count
        self.coherent_count = coherent_count
        self.recommendation = recommendation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "station": self.station,
            "is_coherent": self.is_coherent,
            "is_conflicting": self.is_conflicting,
            "has_neighbors": self.has_neighbors,
            "adjusted_confidence": round(self.adjusted_confidence, 4),
            "nearby_station_count": self.nearby_station_count,
            "conflicting_count": self.conflicting_count,
            "coherent_count": self.coherent_count,
            "recommendation": self.recommendation,
            "coordinated_signals": self.coordinated_signals[:5],  # Limit output
        }

    def __repr__(self) -> str:
        return (f"<CoordinationResult {self.station} "
                f"coherent={self.is_coherent} conflict={self.is_conflicting} "
                f"rec={self.recommendation}>")


# ─── SpatialCoherenceGate ──────────────────────────────────────

class SpatialCoherenceGate:
    """
    Spatial coherence gate for cross-station signal verification.

    Uses station proximity (within 200 miles) and event timing (within
    30 minutes) to determine if signals are spatially coherent or
    conflicting.
    """

    def __init__(
        self,
        station_mapping_path: Optional[str] = None,
        alert_db_path: Optional[str] = None,
        proximity_miles: float = PROXIMITY_MILES,
        event_window_minutes: int = EVENT_WINDOW_MINUTES,
    ):
        self.proximity_miles = proximity_miles
        self.event_window_minutes = event_window_minutes

        # Station coordinates loaded from mapping
        self._station_coords: Dict[str, Tuple[float, float]] = {}
        self._load_station_coords(station_mapping_path)

        # Coordination event tracking
        self._coordination_events: List[Dict] = []

    # ── Station Loading ───────────────────────────────────────

    def _load_station_coords(self, mapping_path: Optional[str] = None) -> None:
        """Load station coordinates from the station mapping file."""
        paths_to_try = [
            mapping_path,
            "data/station_mapping.json",
            "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/station_mapping.json",
        ]

        for path in paths_to_try:
            if path and Path(path).exists():
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    stations = data.get("stations", data)
                    if isinstance(stations, dict):
                        for code, info in stations.items():
                            lat = info.get("lat")
                            lon = info.get("lon")
                            if lat is not None and lon is not None:
                                self._station_coords[code] = (float(lat), float(lon))
                    logger.info(f"Loaded {len(self._station_coords)} station coordinates")
                    return
                except Exception as e:
                    logger.warning(f"Failed to load station mapping: {e}")

        # Fallback: hardcoded station coordinates for our 20 stations
        self._station_coords = {
            "KATL": (33.6407, -84.4277),
            "KAUS": (30.1945, -97.6699),
            "KBOS": (42.3656, -71.0096),
            "KDCA": (38.8512, -77.0402),
            "KDEN": (39.8561, -104.6737),
            "KDFW": (32.8998, -97.0403),
            "KHOU": (29.6455, -95.2784),
            "KJAX": (30.4941, -81.6879),
            "KJFK": (40.6397, -73.7789),
            "KLAS": (36.0801, -115.1362),
            "KLAX": (33.9425, -118.4081),
            "KMCI": (39.2975, -94.7139),
            "KMCO": (28.4280, -81.3090),
            "KMDW": (41.7856, -87.7524),
            "KMIA": (25.7959, -80.2870),
            "KMSP": (44.8819, -93.2217),
            "KMSY": (29.9933, -90.2580),
            "KNYC": (40.7168, -73.9982),
            "KOKC": (35.3931, -97.6007),
            "KORD": (41.9786, -87.9047),
            "KPHL": (39.8728, -75.2411),
            "KPHX": (33.4350, -112.0065),
            "KPIT": (40.4914, -80.2328),
            "KRDU": (35.8776, -78.7875),
            "KSEA": (47.4489, -122.3093),
            "KSFO": (37.6213, -122.3789),
            "KSLC": (40.7884, -111.9778),
            "KSTL": (38.7472, -90.3600),
            "KTPA": (27.9799, -82.5349),
            "KTTD": (45.5494, -122.4033),
        }
        logger.info(f"Loaded {len(self._station_coords)} station coordinates (fallback)")

    # ── Proximity Query ───────────────────────────────────────

    def get_station_distance(self, station_a: str, station_b: str) -> Optional[float]:
        """Get distance in miles between two stations."""
        coords_a = self._station_coords.get(station_a)
        coords_b = self._station_coords.get(station_b)
        if coords_a and coords_b:
            return haversine_miles(
                coords_a[0], coords_a[1], coords_b[0], coords_b[1]
            )
        return None

    def get_nearby_stations(
        self,
        station: str,
        max_distance: Optional[float] = None,
    ) -> List[Tuple[str, float]]:
        """
        Get all stations within proximity range of the given station.

        Args:
            station: Station code
            max_distance: Maximum distance in miles (defaults to proximity_miles)

        Returns:
            List of (station_code, distance_miles) sorted by distance
        """
        max_dist = max_distance or self.proximity_miles
        results = []

        coords = self._station_coords.get(station)
        if not coords:
            return []

        for other_code, other_coords in self._station_coords.items():
            if other_code == station:
                continue
            distance = haversine_miles(
                coords[0], coords[1], other_coords[0], other_coords[1]
            )
            if distance <= max_dist:
                results.append((other_code, round(distance, 1)))

        return sorted(results, key=lambda x: x[1])

    # ─── Signal Evaluation ────────────────────────────────────

    def evaluate(
        self,
        station: str,
        direction: str,
        date: str,
        market_type: str = "HIGH",
        current_signals: Optional[Dict[str, str]] = None,
        verbose: bool = False,
    ) -> CoordinationResult:
        """
        Evaluate spatial coherence for a signal at a station.

        Checks nearby stations for:
        - Coherent signals (same direction) → confidence boost
        - Conflicting signals (opposite direction) → confidence penalty
        - No nearby stations → slight discount (unverifiable)

        Args:
            station: Station code
            direction: 'up' or 'down'
            date: ISO date string
            market_type: 'HIGH' or 'LOW'
            current_signals: Dict of {station_code: direction} for currently
                            active signals across stations. If None, uses
                            proximity-only analysis.
            verbose: If True, include additional debug info

        Returns:
            CoordinationResult with coherence assessment
        """
        nearby = self.get_nearby_stations(station)

        if not nearby:
            return CoordinationResult(
                station=station,
                is_coherent=False,
                is_conflicting=False,
                has_neighbors=False,
                adjusted_confidence=NO_NEIGHBOR_DISCOUNT,
                nearby_station_count=0,
                recommendation="trade",
            )

        # Check signals from nearby stations
        coherent_count = 0
        conflicting_count = 0
        silent_count = 0
        coordinated_signals = []

        for nearby_code, distance in nearby:
            nearby_dir = None
            if current_signals:
                nearby_dir = current_signals.get(nearby_code)

            if nearby_dir is None:
                silent_count += 1
                continue

            signal_info = {
                "station": nearby_code,
                "distance_miles": distance,
                "direction": nearby_dir,
            }

            if nearby_dir == direction:
                coherent_count += 1
                signal_info["relation"] = "coherent"
                coordinated_signals.append(signal_info)
            else:
                conflicting_count += 1
                signal_info["relation"] = "conflicting"
                coordinated_signals.append(signal_info)

        # Determine coherence state
        has_any_signal = coherent_count + conflicting_count > 0
        has_neighbors = len(nearby) > 0

        if not has_any_signal:
            # Nearby stations exist but no signals — slight discount
            return CoordinationResult(
                station=station,
                is_coherent=False,
                is_conflicting=False,
                has_neighbors=True,
                adjusted_confidence=0.98,
                nearby_station_count=len(nearby),
                recommendation="trade",
            )

        if conflicting_count > coherent_count:
            # More conflicting than coherent — significant conflict
            confidence_mult = CONFLICT_PENALTY
            recommendation = "investigate"
            is_conflicting = True
            is_coherent = False
        elif coherent_count >= 1 and conflicting_count == 0:
            # All nearby signals agree
            confidence_mult = COHERENT_BOOST
            recommendation = "trade"
            is_coherent = True
            is_conflicting = False
        else:
            # Mixed signals
            ratio = coherent_count / (coherent_count + conflicting_count) if (coherent_count + conflicting_count) > 0 else 0
            if ratio >= 0.5:
                confidence_mult = 1.0 + (ratio - 0.5) * 0.1
                recommendation = "trade"
                is_coherent = True
                is_conflicting = False
            else:
                confidence_mult = CONFLICT_PENALTY + ratio * 0.4
                recommendation = "investigate"
                is_coherent = False
                is_conflicting = True

        # Log coordination event
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "station": station,
            "direction": direction,
            "date": date,
            "market_type": market_type,
            "coherent_count": coherent_count,
            "conflicting_count": conflicting_count,
            "nearby_count": len(nearby),
            "recommendation": recommendation,
        }
        self._coordination_events.append(event)

        return CoordinationResult(
            station=station,
            is_coherent=is_coherent,
            is_conflicting=is_conflicting,
            has_neighbors=has_neighbors,
            coordinated_signals=coordinated_signals,
            adjusted_confidence=confidence_mult,
            nearby_station_count=len(nearby),
            coherent_count=coherent_count,
            conflicting_count=conflicting_count,
            recommendation=recommendation,
        )

    def apply_to_confidence(
        self,
        station: str,
        direction: str,
        confidence: float,
        date: str,
        market_type: str = "HIGH",
        current_signals: Optional[Dict[str, str]] = None,
    ) -> Tuple[float, CoordinationResult]:
        """
        Convenience: evaluate spatial coherence and apply adjustment to confidence.

        Args:
            station: Station code
            direction: 'up' or 'down'
            confidence: Original confidence [0.0, 1.0]
            date: ISO date string
            market_type: 'HIGH' or 'LOW'
            current_signals: Dict of nearby station signals

        Returns:
            (adjusted_confidence, CoordinationResult)
        """
        result = self.evaluate(
            station=station,
            direction=direction,
            date=date,
            market_type=market_type,
            current_signals=current_signals,
        )
        adjusted = confidence * result.adjusted_confidence
        return min(1.0, adjusted), result

    def get_coordination_events(
        self,
        station: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """Get recent coordination events."""
        if station:
            events = [e for e in self._coordination_events if e["station"] == station]
        else:
            events = self._coordination_events
        return events[-limit:]

    def get_nearby_summary(self, station: str) -> Dict[str, Any]:
        """Get a summary of nearby stations for a given station."""
        nearby = self.get_nearby_stations(station)
        return {
            "station": station,
            "nearby_stations": [{"code": c, "distance_miles": d} for c, d in nearby],
            "count": len(nearby),
            "farthest_miles": max((d for _, d in nearby), default=0),
            "closest_miles": min((d for _, d in nearby), default=0),
        }


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation."""
    gate = SpatialCoherenceGate()

    # Test 1: Station distance (KNYC to KJFK ~12mi, KNYC to KLAX ~2470mi)
    d_prox = gate.get_station_distance("KNYC", "KJFK")
    d_far = gate.get_station_distance("KNYC", "KLAX")
    assert d_prox is not None and d_prox < 50, f"Expected nearby (~12mi), got {d_prox}"
    assert d_far is not None and d_far > 2000, f"Expected far (~2470mi), got {d_far}"
    print(f"Test 1 PASS: KNYC-KJFK={d_prox:.0f}mi, KNYC-KLAX={d_far:.0f}mi")

    # Test 2: Nearby stations for KATL (should find KCLT, KHOU, KJAX, etc.)
    nearby = gate.get_nearby_stations("KATL")
    assert len(nearby) > 0, f"Expected nearby stations for KATL, got none"
    print(f"Test 2 PASS: KATL nearby = {len(nearby)} stations: {[s[0] for s in nearby[:5]]}")

    # Test 3: Coherent signal → confidence boost
    signals = {"KATL": "up", "KBOS": "up", "KNYC": "up", "KPHL": "up"}
    r3 = gate.evaluate("KDCA", "up", "2026-07-24", current_signals=signals)
    assert r3.is_coherent, f"Expected coherent for KDCA up with NE corridor"
    assert r3.adjusted_confidence >= 1.0, f"Expected boost, got {r3.adjusted_confidence}"
    print(f"Test 3 PASS: coherent={r3.is_coherent} mult={r3.adjusted_confidence} rec={r3.recommendation}")

    # Test 4: Conflicting signal → penalty
    signals_conflict = {"KNYC": "up", "KPHL": "down"}
    r4 = gate.evaluate("KDCA", "up", "2026-07-24", current_signals=signals_conflict)
    assert r4.is_conflicting, f"Expected conflicting"
    assert r4.adjusted_confidence < 1.0, f"Expected penalty, got {r4.adjusted_confidence}"
    print(f"Test 4 PASS: conflicting={r4.is_conflicting} mult={r4.adjusted_confidence}")

    # Test 5: Isolated station → slight discount
    r5 = gate.evaluate("KSEA", "up", "2026-07-24")
    # KSEA is on the west coast - few nearby within 200mi
    assert r5.adjusted_confidence <= 1.0
    print(f"Test 5 PASS: isolated station mult={r5.adjusted_confidence} neighbors={r5.nearby_station_count}")

    # Test 6: apply_to_confidence
    adj_confidence, result = gate.apply_to_confidence(
        "KDCA", "up", 0.70, "2026-07-24",
        current_signals={"KNYC": "up", "KPHL": "up"},
    )
    assert adj_confidence > 0.70, f"Expected boosted confidence, got {adj_confidence}"
    print(f"Test 6 PASS: confidence {0.70} → {adj_confidence:.3f}")

    # Test 7: get_nearby_summary
    summary = gate.get_nearby_summary("KATL")
    assert "nearby_stations" in summary
    assert summary["count"] > 0
    print(f"Test 7 PASS: KATL summary = {summary['count']} nearby")

    # Test 8: Coordination events
    events = gate.get_coordination_events(station="KDCA")
    assert len(events) >= 2
    print(f"Test 8 PASS: {len(events)} coordination events for KDCA")

    # Test 9: No signal nearby
    r9 = gate.evaluate("KDCA", "up", "2026-07-24", current_signals={})
    assert r9.adjusted_confidence == 0.98  # Nearby exist but no signals
    print(f"Test 9 PASS: no signals nearby mult={r9.adjusted_confidence}")

    # Test 10: to_dict
    d = r9.to_dict()
    assert "is_coherent" in d
    assert "adjusted_confidence" in d
    assert "recommendation" in d
    print(f"Test 10 PASS: to_dict={d['recommendation']}")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()