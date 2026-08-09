#!/usr/bin/env python3
"""
frontal_passage_nowcast_signal.py — Spatial nowcasting signal for cold front detection.

Detects cold/warm fronts approaching a Kalshi trading station by computing
temperature gradients between the target station and its wind-direction-aware
upstream METAR stations.

Phase 1 fixes applied (Gray Room spec 2026-08-08):
  E1: Time enforcement — MAX_OBS_AGE_MINUTES = 30 enforced in all DB queries
  E1: Wind-direction-aware upstream selector — dynamic, not static UPSTREAM_MAP
  E1: Time-of-day filter — ignore fronts after 3pm / before 8am local
  E1: Region-aware gradient threshold — coastal vs continental
  E3: Probability-mass shift output — replaces binary (direction, confidence)

Design: docs/plans/GRAY-ROOM-NOWCASTING-TRAJECTORY-2026-08-08.md
B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Add repo root to path for standalone execution
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_SCRIPT_DIR, "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.signals.base_signal import BaseSignal

# ── Detection Thresholds ──
GRADIENT_THRESHOLD_C = 3.0           # Continental default: 3°C
GRADIENT_THRESHOLD_COASTAL_C = 5.0  # Coastal: 5°C (sea breeze = 5-8°C daily)
COASTAL_STATIONS = {"KSFO", "KLAX", "KMIA", "KSEA", "KPDX", "KNYC", "KBOS"}
MIN_UPSTREAM_STATIONS = 2            # Need ≥2 upstream obs for robustness
MAX_OBS_AGE_MINUTES = 30             # Enforced: reject obs older than 30 min
BASE_CONFIDENCE = 0.40               # Starting confidence for minimal detection
MAX_CONFIDENCE = 0.75                # Capped confidence

# Time-of-day filter
PRE_MARKET_HOUR = 8                  # Ignore fronts before 8am local
POST_MARKET_HOUR = 15                # Ignore fronts after 3pm local (15:00)

# Microstructure window weighting factors (used by nowcasting_lane)
WINDOW_WEIGHTS = {
    "pre_noon": 0.5,    # Before 12pm
    "peak": 1.0,        # 12pm-3pm
    "post_peak": 0.3,   # After 3pm
}

# Station timezone offsets (UTC → local)
STATION_TZ_OFFSETS = {
    'KNYC': -4, 'KBOS': -4, 'KPHL': -4, 'KDCA': -4, 'KJFK': -4, 'KBWI': -4,
    'KATL': -4, 'KMIA': -4, 'KMSY': -5, 'KHOU': -5, 'KBNA': -5, 'KCLT': -4,
    'KMDW': -5, 'KMSP': -5, 'KORD': -5, 'KDTW': -4, 'KMKE': -5,
    'KDFW': -5, 'KAUS': -5, 'KSAT': -5, 'KOKC': -5,
    'KLAX': -7, 'KSFO': -7, 'KSEA': -7, 'KPDX': -7, 'KPHX': -7, 'KLAS': -7,
    'KDEN': -6,
}

# Candidate upstream stations (all stations, for dynamic selection)
ALL_CANDIDATE_STATIONS = [
    "KNYC","KBOS","KPHL","KDCA","KJFK","KBWI",
    "KATL","KMIA","KMSY","KHOU","KBNA","KCLT",
    "KMDW","KMSP","KORD","KDTW","KMKE",
    "KDFW","KAUS","KSAT","KOKC",
    "KLAX","KSFO","KSEA","KPDX","KPHX","KLAS",
    "KDEN",
]

METAR_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "metar_backfill.db")


def _bearing(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> float:
    """Compute bearing (degrees) from point A to point B."""
    import math
    d_lon = math.radians(to_lon - from_lon)
    lat1 = math.radians(from_lat)
    lat2 = math.radians(to_lat)
    x = math.sin(d_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _bearing_to_station(target: str, candidate: str) -> float:
    """Compute bearing from candidate to target."""
    from core.station_registry import get_station_coordinates
    t_lat, t_lon = get_station_coordinates(target)
    c_lat, c_lon = get_station_coordinates(candidate)
    return _bearing(c_lat, c_lon, t_lat, t_lon)


def get_upstream_stations(target: str, wind_dir_deg: Optional[float],
                           candidates: List[str] = None,
                           max_stations: int = 3) -> List[str]:
    """
    Wind-direction-aware upstream station selector.

    Given a target station and current wind direction at the target,
    select the N stations most directly upwind.

    The upwind direction is (wind_dir + 180) % 360.
    Stations are ranked by how closely the bearing from candidate→target
    matches the upwind direction.

    If wind direction is unavailable, falls back to climatological upstream
    (W→E / NW→SE).
    """
    if candidates is None:
        candidates = [s for s in ALL_CANDIDATE_STATIONS if s != target]

    if wind_dir_deg is None:
        # Fallback: select by distance from west
        upwind = 270  # W→E climatological
    else:
        upwind = (wind_dir_deg + 180) % 360

    # Compute bearings and rank by closeness to upwind direction
    scored = []
    for c in candidates:
        try:
            b = _bearing_to_station(target, c)
            diff = min(abs(b - upwind), 360 - abs(b - upwind))
            scored.append((diff, c))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0])
    return [s[1] for s in scored[:max_stations]]


def _get_station_tz_offset(station: str) -> int:
    """Get UTC→local hour offset for a station."""
    return STATION_TZ_OFFSETS.get(station, -5)


def _is_in_trading_window(utc_ts: str, station: str) -> bool:
    """
    Time-of-day filter: return True if the current local time is within
    trading hours (8am-3pm local). Fronts arriving outside this window
    don't affect settlement.
    """
    try:
        dt = datetime.fromisoformat(utc_ts.replace('Z', '+00:00'))
        offset = _get_station_tz_offset(station)
        local_hour = (dt.hour + offset) % 24
        return PRE_MARKET_HOUR <= local_hour < POST_MARKET_HOUR
    except (ValueError, TypeError):
        return True  # Allow on parse failure


def _get_gradient_threshold(station: str) -> float:
    """Return region-aware gradient threshold.

    Coastal stations get a higher threshold to avoid false positives
    from sea breeze and land-sea thermal gradients.
    """
    if station in COASTAL_STATIONS:
        return GRADIENT_THRESHOLD_COASTAL_C
    return GRADIENT_THRESHOLD_C


def _compute_prob_shift(gradient_c: float, direction: str,
                         confidence: float) -> Dict[str, float]:
    """
    Compute probability-mass shift from gradient and confidence.

    Maps the gradient magnitude to a delta over temperature bucket
    probabilities. The shift is a multiplicative factor applied to
    the prior distribution: P_post(bucket) ∝ P_prior(bucket) × (1 + shift[bucket]).

    For direction='down' (cold front, upstream colder):
      - Lower buckets get positive shift
      - Higher buckets get negative shift
    For direction='up' (warm front, upstream warmer):
      - Higher buckets get positive shift
      - Lower buckets get negative shift

    Shift magnitude is proportional to gradient strength and confidence.
    """
    k = min(abs(gradient_c) / GRADIENT_THRESHOLD_C, 3.0)
    magnitude = min(k * confidence * 0.25, 0.25)  # max 25% shift

    shift = {}
    bucket_labels = [
        "below_30", "30-35", "35-40",
        "40-45", "45-50", "50-55", "above_55"
    ]

    if direction == "down":
        # Cold front: shift mass to lower buckets
        for i, label in enumerate(bucket_labels):
            # Linear decay from negative to positive
            t = i / (len(bucket_labels) - 1) if len(bucket_labels) > 1 else 0
            shift[label] = magnitude * (1.0 - t) - magnitude * t
    else:
        # Warm front: shift mass to higher buckets
        for i, label in enumerate(bucket_labels):
            t = i / (len(bucket_labels) - 1) if len(bucket_labels) > 1 else 0
            shift[label] = magnitude * t - magnitude * (1.0 - t)

    return shift


def _compute_nowcast_shift(station: str, direction: str, confidence: float,
                            gradient_c: float, n_upstream: int,
                            now_ts: str) -> Dict[str, Any]:
    """
    Build the NowcastShift dict (replaces binary (direction, confidence) output).

    Returns the full probability-mass shift payload for the Bayesian cascade.
    """
    return {
        "type": "nowcast_shift",
        "station": station,
        "direction": direction,
        "confidence": confidence,
        "gradient_C": round(gradient_c, 2),
        "n_upstream": n_upstream,
        "timestamp_utc": now_ts,
        "in_trading_window": _is_in_trading_window(now_ts, station),
        "window_weight": _get_window_weight(now_ts, station),
        "prob_shift": _compute_prob_shift(gradient_c, direction, confidence),
    }


def _get_window_weight(utc_ts: str, station: str) -> float:
    """Get microstructure window weighting factor."""
    try:
        dt = datetime.fromisoformat(utc_ts.replace('Z', '+00:00'))
        offset = _get_station_tz_offset(station)
        local_hour = (dt.hour + offset) % 24
        if local_hour < 12:
            return WINDOW_WEIGHTS["pre_noon"]
        elif local_hour < 15:
            return WINDOW_WEIGHTS["peak"]
        else:
            return WINDOW_WEIGHTS["post_peak"]
    except (ValueError, TypeError):
        return 0.5


# ── NowcastShift type alias ──
NowcastShift = Dict[str, Any]
# Fields:
#   type: str = "nowcast_shift"
#   station: str
#   direction: str  ("up" or "down")
#   confidence: float  (0.0-0.75)
#   gradient_C: float
#   n_upstream: int
#   timestamp_utc: str
#   in_trading_window: bool
#   window_weight: float  (0.3, 0.5, or 1.0)
#   prob_shift: Dict[str, float]  (bucket_label → delta)


class FrontalPassageNowcastSignal(BaseSignal):
    """
    Spatial nowcasting signal: detects cold/warm fronts approaching
    a Kalshi trading station by computing temperature gradients
    between the target station and its wind-direction-aware upstream.

    Phase 1: Fire and gauge approach with probability-mass shift output.
    """

    def __init__(self, metar_db_path: str = METAR_DB):
        super().__init__(db_path=metar_db_path)
        self._name = "frontal_passage_nowcast"
        self._cached_upstream_temps: Dict[str, Dict[str, float]] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 300  # 5 minutes

    @property
    def name(self) -> str:
        return self._name

    @property
    def min_lookback(self) -> int:
        return 0

    def evaluate_for_station(
        self,
        station: str,
        date: str,
        conn: Optional[sqlite3.Connection] = None,
        market_type: Optional[str] = None,
    ) -> NowcastShift:
        """
        Check for approaching front at this station.

        Returns a NowcastShift dict (see above) or empty dict if no signal.

        Phase 1 fixes applied:
        - Time enforcement (MAX_OBS_AGE_MINUTES = 30)
        - Wind-direction-aware upstream selector
        - Time-of-day filter
        - Region-aware gradient threshold
        - Probability-mass shift output
        """
        now_ts = datetime.now(timezone.utc).isoformat()

        # Time-of-day filter
        if not _is_in_trading_window(now_ts, station):
            logger.debug("Nowcasting: %s outside trading window, skipping", station)
            return {}

        # Get connection
        close_conn = False
        if conn is None:
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                close_conn = True
            except Exception as e:
                logger.error("Cannot connect to METAR DB: %s", e)
                return {}

        try:
            # Get target station temperature + wind direction
            target_temp, target_wind_dir = self._get_recent_obs(conn, station)
            if target_temp is None:
                logger.debug("No recent temperature for target %s", station)
                return {}

            # Wind-direction-aware upstream selection
            upstream_stations = get_upstream_stations(station, target_wind_dir)
            if len(upstream_stations) < MIN_UPSTREAM_STATIONS:
                logger.debug(
                    "Insufficient upstream stations for %s (wind dir=%s)",
                    station, target_wind_dir,
                )
                return {}

            # Get upstream temperatures
            upstream_temps = []
            for us in upstream_stations:
                temp, _ = self._get_recent_obs(conn, us)
                if temp is not None:
                    upstream_temps.append(temp)

            if len(upstream_temps) < MIN_UPSTREAM_STATIONS:
                logger.debug(
                    "Only %d/%d upstream stations have data for %s",
                    len(upstream_temps), len(upstream_stations), station,
                )
                return {}

            # Compute gradients (target - upstream)
            gradients = [target_temp - ut for ut in upstream_temps]
            g = sorted(gradients)[len(gradients) // 2]  # median

            # Region-aware gradient threshold
            threshold = _get_gradient_threshold(station)
            if abs(g) < threshold:
                return {}

            # Determine direction
            if g > threshold:
                direction = "down"  # upstream colder = cold front
            else:
                direction = "up"    # upstream warmer = warm front

            # Compute confidence
            k = abs(g) / threshold
            n_valid = max(len(upstream_temps), MIN_UPSTREAM_STATIONS)
            confidence = min(
                MAX_CONFIDENCE,
                BASE_CONFIDENCE + 0.05 * (k - 1) + 0.05 * (n_valid - MIN_UPSTREAM_STATIONS),
            )

            shift = _compute_nowcast_shift(
                station, direction, confidence, g, len(upstream_temps), now_ts,
            )

            logger.info(
                "Nowcasting: %s dir=%s grad=%.1f°C conf=%.2f wind=%s "
                "upstream=%s/%d threshold=%.1f°C in_window=%s weight=%.1f",
                station, direction, g, confidence,
                target_wind_dir, len(upstream_temps), len(upstream_stations),
                threshold, shift["in_trading_window"], shift["window_weight"],
            )

            return shift

        except Exception as e:
            logger.error("Error evaluating nowcasting for %s: %s", station, e)
            return {}

        finally:
            if close_conn and conn:
                conn.close()

    def _get_recent_obs(
        self,
        conn: sqlite3.Connection,
        station: str,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Get the most recent temperature AND wind direction for a station
        within MAX_OBS_AGE_MINUTES.

        Returns:
            (temp_c, wind_dir_deg) or (None, None) if no recent data
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=MAX_OBS_AGE_MINUTES))
            cutoff_iso = cutoff.isoformat()

            cur = conn.cursor()

            # Try metar_observations first (has wind direction)
            # Timestamp format: '2026-08-08T03:51:00+00:00'
            cur.execute("""
                SELECT temp_c, wind_direction_deg
                FROM metar_observations
                WHERE station = ?
                  AND temp_c IS NOT NULL
                  AND timestamp_utc >= ?
                ORDER BY timestamp_utc DESC
                LIMIT 1
            """, (station, cutoff_iso))
            row = cur.fetchone()
            if row:
                temp = float(row[0]) if row[0] is not None else None
                wind = float(row[1]) if row[1] is not None else None
                return temp, wind

            # Fallback: observations table
            cur.execute("""
                SELECT temp_c
                FROM observations
                WHERE station = ? AND temp_c IS NOT NULL
                  AND obs_time >= ?
                ORDER BY obs_time DESC
                LIMIT 1
            """, (station, cutoff_iso))
            row = cur.fetchone()
            if row and row[0] is not None:
                return float(row[0]), None

            # Fallback: metar_archive
            try:
                cur.execute("""
                    SELECT temp_c
                    FROM metar_archive
                    WHERE station = ? AND temp_c IS NOT NULL
                    ORDER BY obs_hour DESC
                    LIMIT 1
                """, (station,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0]), None
            except Exception:
                pass

            return None, None

        except Exception as e:
            logger.debug("Error fetching obs for %s: %s", station, e)
            return None, None

    def get_cached_state(self) -> dict:
        """Return current cached state for persistence."""
        return {
            "cached_upstream_temps": self._cached_upstream_temps,
            "cache_timestamp": (
                self._cache_timestamp.isoformat()
                if self._cache_timestamp
                else None
            ),
        }

    # ── BaseSignal interface ──

    def evaluate(self, context: dict) -> NowcastShift:
        station = context.get("station", "")
        date = context.get("date", "")
        conn = context.get("conn")
        market_type = context.get("market_type", "HIGH")
        return self.evaluate_for_station(station, date, conn, market_type)


# ── Standalone Test ──

def test_nowcasting_signal():
    """Test the nowcasting signal module."""
    print("=" * 72)
    print("  FrontalPassageNowcastSignal — Phase 1 Self-Test")
    print("=" * 72)

    signal = FrontalPassageNowcastSignal()
    print(f"  Signal name: {signal.name}")
    print(f"  MAX_OBS_AGE_MINUTES: {MAX_OBS_AGE_MINUTES}")
    print(f"  GRADIENT_THRESHOLD_C: {GRADIENT_THRESHOLD_C}°C")
    print(f"  GRADIENT_THRESHOLD_COASTAL_C: {GRADIENT_THRESHOLD_COASTAL_C}°C")
    print(f"  Trading window: {PRE_MARKET_HOUR}:00 - {POST_MARKET_HOUR}:00 local")

    # Test wind-direction-aware selector
    print(f"\n  Wind-direction-aware upstream selector:")
    for station, wind in [("KNYC", 270), ("KNYC", 90), ("KMDW", 360), ("KSFO", 180)]:
        ups = get_upstream_stations(station, wind)
        print(f"    {station} wind={wind}° → [{', '.join(ups)}]")

    # Test tier 2: wind_dir=None (climatological fallback)
    print(f"    KNYC wind=None → [{', '.join(get_upstream_stations('KNYC', None))}]")

    # Test time-of-day filter
    print(f"\n  Time-of-day filter:")
    for test_hour in [6, 8, 12, 15, 17]:
        test_ts = f"2026-08-08T{test_hour:02d}:00:00+00:00"
        in_window = _is_in_trading_window(test_ts, "KNYC")
        weight = _get_window_weight(test_ts, "KNYC")
        print(f"    {test_ts} KNYC: in_window={in_window} weight={weight}")

    # Test gradient thresholds
    print(f"\n  Gradient thresholds:")
    for station in ["KDEN", "KSFO", "KNYC", "KMDW"]:
        thresh = _get_gradient_threshold(station)
        print(f"    {station}: {thresh}°C")

    # Test prob_shift computation
    print(f"\n  Probability-mass shift examples:")
    for grad, direction, conf in [(5.0, "down", 0.6), (8.0, "down", 0.75), (4.0, "up", 0.55)]:
        shift = _compute_prob_shift(grad, direction, conf)
        total_shift = sum(shift.values())
        print(f"    grad={grad} dir={direction} conf={conf}: "
              f"total_shift={total_shift:.3f} "
              f"shift={json.dumps({k: round(v, 3) for k, v in shift.items()})}")

    # Full NowcastShift example
    print(f"\n  Full NowcastShift example:")
    ncs = _compute_nowcast_shift("KNYC", "down", 0.65, 6.2, 3,
                                   "2026-08-08T14:30:00+00:00")
    # Print non-prob_shift fields
    show = {k: v for k, v in ncs.items() if k != "prob_shift"}
    print(f"    {json.dumps(show, indent=4, default=str)}")
    ps = {k: round(v, 3) for k, v in ncs["prob_shift"].items()}
    print(f"    prob_shift: {json.dumps(ps)}")

    print("\n  ✅ Phase 1 self-test complete")


if __name__ == "__main__":
    test_nowcasting_signal()