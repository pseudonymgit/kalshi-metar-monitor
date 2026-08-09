#!/usr/bin/env python3
"""
ADVANCE Signal 2: Intraday METAR Nowcasting (<1d)

Real-time temperature tracking from hourly METAR feeds.
Compares current METAR temperature against the forecasted daily max/min
to generate a confidence-weighted nowcast.

Logic:
  - Fetch latest METAR for the station (from DB or live API)
  - Compare current temp against the forecasted daily max/min for that station
  - If current temp is within N°F of the forecasted extreme → confidence rises
  - If current temp has already exceeded forecasted extreme → re-evaluate

B-Mode compliant. No AI/ML.

Usage:
    from core.signals.metar_nowcast_signal import MetarNowcastSignal

    signal = MetarNowcastSignal()
    result = signal.evaluate(station="KNYC", bucket_temp_f=84,
                             gefs_max_f=88, gefs_min_f=72)
    print(result["confidence"], result["direction"])
"""

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
<<<<<<< HEAD
from typing import Dict, List, Optional, Tuple
=======
from typing import Dict, Optional, Tuple
>>>>>>> origin/main

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO_ROOT, "data")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# Confidence thresholds
CONFIDENCE_CEILING = 0.95
CONFIDENCE_MIN = 0.1

# How close to forecasted extreme before confidence increases
DEGREES_TO_EXTREME_HIGH = 3.0    # °F: within this of forecasted max
DEGREES_TO_EXTREME_LOW = 3.0     # °F: within this of forecasted min
FORECAST_EXCEEDED_BUFFER = 2.0   # °F: exceeded forecast by this much → reconsider

# Time windows
METAR_FRESHNESS_HOURS = 2     # METAR older than this is stale
METAR_DB_PATH = os.path.join(DATA_DIR, "metar_archive.db")


<<<<<<< HEAD
from .base_signal import BaseSignal


class MetarNowcastSignal(BaseSignal):
    """Confidence-weighted nowcast based on live METAR vs forecast."""

    def __init__(self, config: Optional[dict] = None, db_path: str = None):
        super().__init__(db_path)
        self.config = config or {}
        self._metar_cache: Dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "metar_nowcast"

    @property
    def min_lookback(self) -> int:
        return 1

    # ---- BaseSignal sweep interface ----

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Sweep-compatible evaluate placeholder.

        Real METAR-based evaluate() requires per-station DB access unavailable
        in the standard sweep (days-only) interface. Use evaluate_for_station()
        for real signal evaluation.
        """
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate METAR nowcast for a specific station using live DB data.

        Returns (None, 0.5) if METAR observation is fresh (< 2h old),
        (None, 0.0) if stale or unavailable.
        """
        metar = self._get_latest_metar(station)
        if metar is None or not metar.get("fresh", False):
            return None, 0.0
        return None, 0.5

    # ---- Legacy station-level interface (preserved) ----

=======
class MetarNowcastSignal:
    """Confidence-weighted nowcast based on live METAR vs forecast."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._metar_cache: Dict[str, dict] = {}

>>>>>>> origin/main
    def _get_latest_metar(self, station: str) -> Optional[dict]:
        """Fetch latest METAR observation for a station from DB."""
        if not os.path.exists(METAR_DB_PATH):
            logger.warning(f"METAR DB not found at {METAR_DB_PATH}")
            return None

        try:
            conn = sqlite3.connect(METAR_DB_PATH)
            cursor = conn.execute(
                """SELECT station, obs_time_utc, temp_c, dewpoint_c, wind_speed_kt,
                          wind_dir, visibility_m, cloud_cover, pressure_hpa
                   FROM metar_observations
                   WHERE station = ?
                   ORDER BY obs_time_utc DESC
                   LIMIT 1""",
                (station,)
            )
            row = cursor.fetchone()
            conn.close()

            if row is None:
                return None

            return {
                "station": row[0],
                "obs_time": row[1],
                "temp_c": row[2],
                "temp_f": round(row[2] * 9.0 / 5.0 + 32, 1),
                "dewpoint_c": row[3],
                "wind_speed_kt": row[4],
                "fresh": (datetime.now(timezone.utc) - datetime.fromisoformat(
                    row[1].replace("Z", "+00:00"))).total_seconds() / 3600 < METAR_FRESHNESS_HOURS
            }
        except Exception as e:
            logger.error(f"Failed to fetch METAR for {station}: {e}")
            return None

<<<<<<< HEAD
    def evaluate_forecast(self, station: str, bucket_temp_f: int,
                          gefs_max_f: float, gefs_min_f: float) -> Dict:
=======
    def evaluate(self, station: str, bucket_temp_f: int,
                 gefs_max_f: float, gefs_min_f: float) -> Dict:
>>>>>>> origin/main
        """
        Evaluate nowcast confidence based on current METAR vs forecast.

        Args:
            station: Kalshi station code
            bucket_temp_f: temperature bucket being evaluated
            gefs_max_f: forecasted daily max temperature (°F)
            gefs_min_f: forecasted daily min temperature (°F)

        Returns:
            Dict with confidence, direction, and reasoning
        """
        result = {
            "signal": False,
            "confidence": 0.0,
            "direction": None,  # "HIGH" or "LOW" or None
            "bucket_temp_f": bucket_temp_f,
            "reason": None,
            "metar_temp_f": None,
            "metar_fresh": False,
        }

        metar = self._get_latest_metar(station)
        if metar is None:
            result["reason"] = f"No METAR data for {station}"
            return result

        result["metar_temp_f"] = metar["temp_f"]
        result["metar_fresh"] = metar["fresh"]

        if not metar["fresh"]:
            result["reason"] = f"METAR stale (last: {metar['obs_time']})"
            return result

        current_temp_f = metar["temp_f"]

        # Determine direction: are we approaching HIGH or LOW bucket?
        # If current temp is rising toward forecasted max, signal HIGH bucket
        # If current temp is dropping toward forecasted min, signal LOW bucket

        # Compute how close to each extreme
        dist_to_max = abs(gefs_max_f - current_temp_f)
        dist_to_min = abs(current_temp_f - gefs_min_f)

        if dist_to_max < dist_to_min:
            # Approaching high
            if current_temp_f >= gefs_max_f - DEGREES_TO_EXTREME_HIGH:
                if current_temp_f <= gefs_max_f + FORECAST_EXCEEDED_BUFFER:
                    # Within range of forecasted max
                    confidence = max(CONFIDENCE_MIN, min(
                        CONFIDENCE_CEILING,
                        1.0 - (dist_to_max / (DEGREES_TO_EXTREME_HIGH * 3))
                    ))
                    result["signal"] = True
                    result["direction"] = "HIGH"
                    result["confidence"] = round(confidence, 3)
                    result["reason"] = (f"METAR {current_temp_f}°F approaching forecasted max "
                                       f"{gefs_max_f}°F (dist={dist_to_max:.1f}°F)")
                else:
                    result["reason"] = (f"METAR {current_temp_f}°F already exceeded forecasted "
                                       f"max {gefs_max_f}°F")
            else:
                result["reason"] = (f"METAR {current_temp_f}°F too far from forecasted "
                                   f"max {gefs_max_f}°F (dist={dist_to_max:.1f}°F)")
        else:
            # Approaching low
            if current_temp_f <= gefs_min_f + DEGREES_TO_EXTREME_LOW:
                if current_temp_f >= gefs_min_f - FORECAST_EXCEEDED_BUFFER:
                    confidence = max(CONFIDENCE_MIN, min(
                        CONFIDENCE_CEILING,
                        1.0 - (dist_to_min / (DEGREES_TO_EXTREME_LOW * 3))
                    ))
                    result["signal"] = True
                    result["direction"] = "LOW"
                    result["confidence"] = round(confidence, 3)
                    result["reason"] = (f"METAR {current_temp_f}°F approaching forecasted min "
                                       f"{gefs_min_f}°F (dist={dist_to_min:.1f}°F)")
                else:
                    result["reason"] = (f"METAR {current_temp_f}°F already below forecasted "
                                       f"min {gefs_min_f}°F")
            else:
                result["reason"] = (f"METAR {current_temp_f}°F too far from forecasted "
                                   f"min {gefs_min_f}°F (dist={dist_to_min:.1f}°F)")

        return result

    def evaluate_bucket(self, station: str, bucket_temp_f: int,
                        gefs_max_f: float, gefs_min_f: float) -> Dict:
        """
        Evaluate whether current METAR temp supports a specific bucket.
        Returns confidence that this bucket will be settled.
        """
<<<<<<< HEAD
        nowcast = self.evaluate_forecast(station, bucket_temp_f, gefs_max_f, gefs_min_f)
=======
        nowcast = self.evaluate(station, bucket_temp_f, gefs_max_f, gefs_min_f)
>>>>>>> origin/main
        if not nowcast["signal"]:
            return {"confidence": 0.0, "reason": nowcast["reason"]}

        # Refine: how likely is the specific bucket given METAR?
        if nowcast["direction"] == "HIGH":
            score = 1.0 - abs(gefs_max_f - bucket_temp_f) / abs(gefs_max_f - gefs_min_f + 1)
        else:
            score = 1.0 - abs(gefs_min_f - bucket_temp_f) / abs(gefs_max_f - gefs_min_f + 1)

        score = max(0.0, min(1.0, score))
        confidence = nowcast["confidence"] * score

        return {
            "confidence": round(confidence, 3),
            "direction": nowcast["direction"],
            "metar_temp_f": nowcast["metar_temp_f"],
            "reason": f"Bucket {bucket_temp_f}°F: {nowcast['reason']}",
        }