#!/usr/bin/env python3
"""
ASOS Microstructure Signal — 1-Minute Resolution Temperature & Wind Analysis
==============================================================================
Extracts microstructure features from IEM ASOS 1-minute data for the
Goldilocks lane. Detects sub-hourly temperature swings, volatility regimes,
and extreme events that hourly METAR data would miss.

Key insight: ASOS 1-minute data at most stations does NOT include temperature
at every observation. Temperature is typically reported in METAR/SPECI reports
at 5-15 minute intervals. This signal gracefully handles sparse temperature
data by:
  1. Computing dT/dt from whatever temperature observations are available
  2. Using altimeter (alti) and wind (sknt, drct) fields which have near-100%
     coverage as supplementary microstructure indicators
  3. Gracefully degrading to (None, 0.0) when ASOS data is unavailable

Features extracted:
  - dT/dt: Rate of temperature change per 5-minute window (°F/5min)
  - Volatility: Rolling 30-minute standard deviation of temperature (°F)
  - Extreme events: Count of rapid temperature swings (≥2°F/5min, ≥3°F/15min)
  - Pressure micro-fluctuations: Short-term altimeter variability (supplementary)
  - Wind gust coherence: Ratio of gusts to sustained wind (supplementary)

B-Mode compliant. No AI/ML.

Data source: data/iem_asos.db (table: asos_observations)
"""

import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .base_signal import BaseSignal, validate_signal

logger = logging.getLogger(__name__)

# Path to the IEM ASOS 1-minute database
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ASOS_DB = os.path.join(SCRIPT_DIR, "data", "iem_asos.db")

# Temperature microstructure thresholds
DT_DT_THRESHOLD_F_PER_5MIN = 2.0   # °F/5min — rapid change threshold
SPIKE_THRESHOLD_F_15MIN = 3.0       # °F/15min — extreme event threshold
VOLATILITY_WINDOW_MIN = 30          # Rolling window for temperature std
DT_DT_WINDOW_MIN = 5                # Window for rate-of-change computation

# Pressure microstructure thresholds (supplementary)
ALTI_MICRO_THRESHOLD_INHG = 0.02    # inHg/5min — rapid pressure change
ALTI_VOLATILITY_WINDOW_MIN = 30     # Rolling window for pressure std

# Graceful degradation: max staleness for cached queries (seconds)
CACHE_TTL_SEC = 300


class AsosMicrostructureSignal(BaseSignal):
    """
    ASOS Microstructure Signal — 1-minute resolution feature extraction.

    Detects sub-hourly temperature and pressure swings that hourly METAR
    data would miss. Provides an early-warning indicator for rapid weather
    changes (frontal passages, thunderstorm outflow, sea breeze fronts).

    Only fires when:
      - ASOS 1-minute data is available for the target station/date
      - At least one microstructure feature exceeds threshold
    """

    def __init__(self, db_path: str = None, asos_db_path: str = None):
        super().__init__(db_path)
        self.asos_db_path = asos_db_path or DEFAULT_ASOS_DB
        self._asos_db_exists = os.path.exists(self.asos_db_path)
        # Cache for station observations to reduce DB reads
        self._obs_cache: Dict[str, Tuple[float, List[Dict]]] = {}

    @property
    def name(self) -> str:
        return "asos_microstructure"

    @property
    def min_lookback(self) -> int:
        return 1  # Only needs current day's 1-minute data

    # ═══════════════════════════════════════════════════════════════════
    # BaseSignal interface
    # ═══════════════════════════════════════════════════════════════════

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate ASOS microstructure signal at day index `idx`.

        This is the sweep-compatible interface. For real signal evaluation,
        use evaluate_for_station() which has access to ASOS DB data.

        The standard days-only interface doesn't carry ASOS 1-minute data,
        so this returns (None, 0.0) by default.
        """
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str,
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate ASOS microstructure signal for a specific station and date.

        Loads 1-minute ASOS data from data/iem_asos.db and computes
        microstructure features.

        Args:
            station: ICAO station code (e.g. 'KMDW')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection (if None, opens own connection)

        Returns:
            (direction, confidence) where:
              - direction: 'up' if microstructure indicates warming,
                           'down' if cooling,
                           None if no signal
              - confidence: 0.0 to 1.0
        """
        if not self._asos_db_exists:
            logger.debug(f"ASOS DB not found at {self.asos_db_path}")
            return None, 0.0

        # Load 1-minute data for the target station and date
        observations = self._load_asos_data(station, date, conn)

        if not observations or len(observations) < 5:
            # Not enough data to compute meaningful features
            return None, 0.0

        # Extract features
        features = self._compute_features(observations)

        # Determine signal direction and confidence from features
        return self._score_features(features, observations)

    # ═══════════════════════════════════════════════════════════════════
    # ASOS Data Loading
    # ═══════════════════════════════════════════════════════════════════

    def _load_asos_data(self, station: str, date: str,
                         conn: sqlite3.Connection = None) -> List[Dict]:
        """Load 1-minute ASOS observations for a station on a given date.

        Returns list of dicts with keys: valid, tmpf, dwpf, sknt, drct, alti.
        """
        # Check cache
        cache_key = f"{station}:{date}"
        now = time = datetime.now(timezone.utc).timestamp()
        if cache_key in self._obs_cache:
            cached_at, cached_data = self._obs_cache[cache_key]
            if now - cached_at < CACHE_TTL_SEC:
                return cached_data

        own_conn = conn is None
        if own_conn:
            if not os.path.exists(self.asos_db_path):
                return []
            conn = sqlite3.connect(self.asos_db_path)
            conn.row_factory = sqlite3.Row

        try:
            cur = conn.execute("""
                SELECT valid, tmpf, dwpf, sknt, drct, alti
                FROM asos_observations
                WHERE station = ? AND DATE(valid) = ?
                ORDER BY valid ASC
            """, (station, date))

            observations = []
            for row in cur.fetchall():
                obs = {
                    "valid": row["valid"],
                    "tmpf": row["tmpf"],
                    "dwpf": row["dwpf"],
                    "sknt": row["sknt"],
                    "drct": row["drct"],
                    "alti": row["alti"],
                }
                observations.append(obs)

            # Cache the result
            self._obs_cache[cache_key] = (now, observations)
            return observations

        except Exception as e:
            logger.debug(f"Error loading ASOS data for {station} {date}: {e}")
            return []
        finally:
            if own_conn and conn:
                conn.close()

    # ═══════════════════════════════════════════════════════════════════
    # Feature Computation
    # ═══════════════════════════════════════════════════════════════════

    def _compute_features(self, observations: List[Dict]) -> Dict:
        """Compute microstructure features from 1-minute ASOS data.

        Returns dict of computed features:
          - temp_obs_count: Number of temperature observations available
          - dt_dt_max: Maximum temperature rate of change (°F/5min)
          - dt_dt_abs_mean: Mean absolute temperature rate of change (°F/5min)
          - volatility_30min: Rolling 30-minute std of temperature
          - extreme_events_5min: Count of ≥2°F/5min swings
          - extreme_events_15min: Count of ≥3°F/15min swings
          - temp_net_change: Net temperature change over the period (°F)
          - alti_volatility: Rolling 30-minute std of altimeter
          - alti_micro_events: Count of rapid altimeter changes
          - has_temp_data: Whether any temperature data was available
        """
        features = {
            "temp_obs_count": 0,
            "dt_dt_max": 0.0,
            "dt_dt_abs_mean": 0.0,
            "volatility_30min": 0.0,
            "extreme_events_5min": 0,
            "extreme_events_15min": 0,
            "temp_net_change": 0.0,
            "alti_volatility": 0.0,
            "alti_micro_events": 0,
            "has_temp_data": False,
        }

        if not observations:
            return features

        # Extract time series
        times = []
        temps = []
        altis = []

        for obs in observations:
            try:
                valid_dt = datetime.strptime(obs["valid"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue

            times.append(valid_dt)

            if obs["tmpf"] is not None:
                temps.append((valid_dt, obs["tmpf"]))
            if obs["alti"] is not None:
                altis.append((valid_dt, obs["alti"]))

        # ── Temperature Features (sparse, but valuable when available) ──
        features["temp_obs_count"] = len(temps)
        features["has_temp_data"] = len(temps) >= 2

        if len(temps) >= 2:
            # Compute dT/dt for each consecutive temperature observation
            dt_dt_values = []
            for i in range(1, len(temps)):
                t1, temp1 = temps[i - 1]
                t2, temp2 = temps[i]
                delta_min = (t2 - t1).total_seconds() / 60.0
                if delta_min > 0:
                    # Normalize to °F/5min
                    dt_dt = (temp2 - temp1) * (DT_DT_WINDOW_MIN / delta_min)
                    dt_dt_values.append(dt_dt)

            if dt_dt_values:
                features["dt_dt_max"] = max(abs(v) for v in dt_dt_values)
                features["dt_dt_abs_mean"] = sum(abs(v) for v in dt_dt_values) / len(dt_dt_values)
                features["temp_net_change"] = temps[-1][1] - temps[0][1]

                # Count extreme events
                for v in dt_dt_values:
                    if abs(v) >= DT_DT_THRESHOLD_F_PER_5MIN:
                        features["extreme_events_5min"] += 1

                # 15-minute window extreme events
                for i in range(len(temps)):
                    for j in range(i + 1, min(i + 20, len(temps))):
                        delta_min = (temps[j][0] - temps[i][0]).total_seconds() / 60.0
                        if 10 <= delta_min <= 20:  # ~15 minute window
                            swing = abs(temps[j][1] - temps[i][1])
                            if swing >= SPIKE_THRESHOLD_F_15MIN:
                                features["extreme_events_15min"] += 1

            # Rolling volatility (30-minute windows)
            if len(temps) >= 3:
                windows = self._compute_rolling_volatility(temps, VOLATILITY_WINDOW_MIN)
                if windows:
                    features["volatility_30min"] = max(windows)

        # ── Pressure/Alti Features (high coverage — near 100%) ──
        if len(altis) >= 2:
            # Micro pressure changes (>0.02 inHg/5min)
            micro_pressure = 0
            for i in range(1, len(altis)):
                t1, alti1 = altis[i - 1]
                t2, alti2 = altis[i]
                delta_min = (t2 - t1).total_seconds() / 60.0
                if 0 < delta_min <= 10:
                    delta_alti = abs(alti2 - alti1)
                    if delta_alti >= ALTI_MICRO_THRESHOLD_INHG:
                        micro_pressure += 1
            features["alti_micro_events"] = micro_pressure

            # Rolling alti volatility
            if len(altis) >= 3:
                alti_windows = self._compute_rolling_volatility(altis, ALTI_VOLATILITY_WINDOW_MIN)
                if alti_windows:
                    features["alti_volatility"] = max(alti_windows)

        return features

    def _compute_rolling_volatility(self, data: List[Tuple[datetime, float]],
                                     window_minutes: int) -> List[float]:
        """Compute rolling standard deviation over a time window.

        Args:
            data: List of (datetime, value) tuples, sorted by time
            window_minutes: Window size in minutes

        Returns:
            List of std values for each window
        """
        windows = []
        for i in range(len(data)):
            window_vals = [data[i][1]]
            for j in range(i + 1, len(data)):
                delta = (data[j][0] - data[i][0]).total_seconds() / 60.0
                if delta <= window_minutes:
                    window_vals.append(data[j][1])
                else:
                    break
            if len(window_vals) >= 3:
                mean = sum(window_vals) / len(window_vals)
                variance = sum((v - mean) ** 2 for v in window_vals) / len(window_vals)
                windows.append(math.sqrt(variance))

        return windows

    # ═══════════════════════════════════════════════════════════════════
    # Scoring
    # ═══════════════════════════════════════════════════════════════════

    def _score_features(self, features: Dict,
                         observations: List[Dict]) -> Tuple[Optional[str], float]:
        """Convert computed features into a directional signal with confidence.

        Scoring logic:
          - dT/dt ≥ 2°F/5min → microstructure event active
          - Volatility ≥ 1.5°F over 30min → elevated microstructure activity
          - Extreme events ≥ 2 in a day → significant microstructure
          - Net temp change determines direction (up = warming, down = cooling)
          - Pressure micro events can confirm temperature microstructure

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        # Build evidence score
        evidence_score = 0.0
        evidence_count = 0

        # Temperature-based evidence
        if features["has_temp_data"]:
            if features["dt_dt_max"] >= DT_DT_THRESHOLD_F_PER_5MIN:
                evidence_score += 1.0
                evidence_count += 1
            elif features["dt_dt_max"] >= DT_DT_THRESHOLD_F_PER_5MIN * 0.5:
                evidence_score += 0.5
                evidence_count += 1

            if features["volatility_30min"] >= 1.5:
                evidence_score += 1.0
                evidence_count += 1
            elif features["volatility_30min"] >= 0.75:
                evidence_score += 0.5
                evidence_count += 1

            if features["extreme_events_5min"] >= 2:
                evidence_score += 1.0
                evidence_count += 1
            elif features["extreme_events_5min"] >= 1:
                evidence_score += 0.5
                evidence_count += 1

            if features["extreme_events_15min"] >= 1:
                evidence_score += 1.0
                evidence_count += 1

        # Pressure-based evidence (supplementary — available on more observations)
        if features["alti_micro_events"] >= 10:
            evidence_score += 0.5
            evidence_count += 1
        elif features["alti_micro_events"] >= 5:
            evidence_score += 0.25
            evidence_count += 1

        if features["alti_volatility"] >= 0.01:
            evidence_score += 0.25
            evidence_count += 1

        # Compute final confidence
        if evidence_count == 0:
            return None, 0.0

        raw_confidence = evidence_score / max(evidence_count, 1)
        # Scale: low evidence → low confidence, high evidence → capped at 0.7
        confidence = min(0.7, raw_confidence * 0.8)

        if confidence < 0.15:
            return None, 0.0

        # Determine direction from net temperature change, or pressure gradient
        if features["has_temp_data"] and features["temp_obs_count"] >= 3:
            direction = 'up' if features["temp_net_change"] >= 0 else 'down'
        else:
            # Use temperature-only observations to determine direction
            # If we don't have enough temp data, use the first/last temp
            direction = 'up' if features["temp_net_change"] >= 0 else 'down'

        if features["temp_obs_count"] < 2 and features["alti_micro_events"] == 0:
            # No temperature and no pressure signal — insufficient data
            return None, 0.0

        return direction, round(confidence, 3)

    # ═══════════════════════════════════════════════════════════════════
    # Query Helpers
    # ═══════════════════════════════════════════════════════════════════

    def has_asos_data(self, station: str, date: str) -> bool:
        """Quick check if ASOS data exists for a station/date without loading all rows."""
        if not self._asos_db_exists:
            return False
        try:
            conn = sqlite3.connect(self.asos_db_path)
            cur = conn.execute(
                "SELECT COUNT(*) FROM asos_observations WHERE station = ? AND DATE(valid) = ?",
                (station, date)
            )
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def get_microstructure_summary(self, station: str, date: str) -> Dict:
        """Get a human-readable summary of microstructure activity for a station/date.

        Useful for debugging and auditing.
        """
        result = {
            "station": station,
            "date": date,
            "has_asos_data": False,
            "signal_fired": False,
            "direction": None,
            "confidence": 0.0,
            "features": {},
        }

        observations = self._load_asos_data(station, date)
        if not observations:
            return result

        result["has_asos_data"] = True
        result["features"] = self._compute_features(observations)
        direction, confidence = self._score_features(result["features"], observations)

        if direction:
            result["signal_fired"] = True
            result["direction"] = direction
            result["confidence"] = confidence

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def create_asos_microstructure_signal(db_path: str = None,
                                      asos_db_path: str = None) -> AsosMicrostructureSignal:
    """Factory function to create an ASOS Microstructure signal instance."""
    return AsosMicrostructureSignal(db_path=db_path, asos_db_path=asos_db_path)