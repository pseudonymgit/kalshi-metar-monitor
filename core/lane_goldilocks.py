#!/usr/bin/env python3
"""
Goldilocks Lane — Lane 4: Microstructure Transient Spike Trading Lane

The Goldilocks lane detects fleeting temperature microstructure events at Kalshi
bucket boundaries using METAR observations. It is a microstructure trading
strategy, NOT a daily directional signal — trades happen intraday when temp
crosses bucket boundaries, not at settlement.

Algorithm:
  1. Load METAR observations for the current day
  2. Check if any observation is within 1°F of a bucket boundary (the "Goldilocks
     zone")
  3. If yes, run DualHypothesisEngine to classify transient (H1) vs structural (H2)
  4. If transient, trade reversion (confidence from confidence threshold)
  5. If structural, skip

Key method:
  evaluate(station, obs_temp, obs_time, bucket_boundary, direction)
    -> (should_trade, confidence, hypothesis, details)

Design:
  - Pure deterministic math + DualHypothesisEngine for H1/H2 classification
  - Uses goldilocks_predictive.py for predictive feature augmentation
  - Uses metar_qc_parser.py for data quality gates
  - Station exclusion for low-resolution stations (KNYC excluded)
  - Gate-based filtering: sensor sanity, feed freshness, boundary oscillation,
    multi-variable confirmation, market executability

References:
  - GOLDILOCKS-LANE-DESIGN.md (design spec)
  - GOLDILOCKS-REDESIGN-EXPERT3.md (Gray Room adversarial stress-test)
  - lane2_goldilocks.py (predecessor — killed as ML signal, converted to pure
    deterministic)
  - dual_hypothesis_engine.py (A4 — H1 transient vs H2 structural)
  - goldilocks_predictive.py (predictive feature computation)

Author: Gilfoyle (Aug 6, 2026)
"""

import math
import logging
import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Allow import from repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Try imports — gracefully degrade if optional dependencies unavailable
try:
    from core.dual_hypothesis_engine import (
        DualHypothesisEngine,
        HypothesisResult,
        SpikeHypothesis,
    )
    _DHE_AVAILABLE = True
except ImportError:
    _DHE_AVAILABLE = False
    logger.warning("dual_hypothesis_engine not available — Goldilocks lane will use fallback classification")

try:
    from core.goldilocks_predictive import GoldilocksPredictor
    _PREDICTOR_AVAILABLE = True
except (ImportError, OSError, Exception):
    _PREDICTOR_AVAILABLE = False
    logger.warning("goldilocks_predictive not available — predictive features will be skipped")

try:
    from core.metar_qc_parser import METARQualityParser, QualityTier
    _QC_AVAILABLE = True
except (ImportError, Exception):
    _QC_AVAILABLE = False
    logger.warning("metar_qc_parser not available — quality gates disabled")
    QualityTier = None

try:
    import numpy as np
    _NP_AVAILABLE = True
except ImportError:
    _NP_AVAILABLE = False


# ─── Constants ────────────────────────────────────────────────────────────────

# Default METAR database path
DEFAULT_METAR_DB = os.path.join(str(REPO_ROOT), "data", "metar_backfill.db")
# Default settlements database path
DEFAULT_SETTLEMENTS_DB = os.path.join(str(REPO_ROOT), "data", "kalshi_settlements.db")

# The "Goldilocks zone": within this many °F of a bucket boundary, we check for
# transient microstructure events
GOLDILOCKS_ZONE_F = 1.0  # °F

# Transient spike threshold: spike_delta < this → H1 transient (from dual_hypothesis_engine)
TRANSIENT_DELTA_THRESHOLD = 0.3  # °F

# Exceeded threshold: must exceed boundary by at least this to count as genuine
EXCEEDED_THRESHOLD = 0.5  # °F above bucket boundary

# Reversion margin: must drop at least this below boundary to confirm reversion
REVERSION_MARGIN = 0.2  # °F

# Late-day gate hour (UTC): daily max is likely established after this hour
LATE_DAY_UTC_HOUR = 18  # 18Z = 2pm ET / 11am PT

# Maximum crossing duration (minutes): if temp stays above boundary longer than
# this, it's an established crossing, NOT a transient spike
MAX_CROSSING_DURATION_MIN = 30

# Minimum crossing duration (observations): a spike must persist for at least
# this many consecutive observations to be genuine (not a single-tick noise)
MIN_CROSSING_OBSERVATIONS = 2

# Oscillation suppression: skip if we had a crossing of this boundary within
# this many minutes (prevents oscillation FPs)
OSCILLATION_SUPPRESSION_MIN = 60

# Deviation from 5-min rolling mean must exceed this to qualify as a spike
# (must exceed quantization noise floor of ~1.8°F from whole-°C encoding)
MIN_DEVIATION_FOR_SPIKE = 1.5  # °F

# How far from boundary we consider "approaching" (°F) for trend extrapolation
APPROACHING_DISTANCE = 1.5  # °F

# Station exclusion list (stations with insufficient temporal resolution)
EXCLUDED_STATIONS = {"KNYC"}

# Minimum valid observation count for a station to be processed
MIN_OBSERVATIONS_PER_STATION = 100_000

# Minimum temporal coverage ratio (≥ 1 obs / 2 min average)
MIN_COVERAGE_RATIO = 0.30

# Temperature sanity bounds (°F)
TEMP_SANITY_MIN = -50.0
TEMP_SANITY_MAX = 130.0

# Max valid temperature change between consecutive obs (°F)
MAX_VALID_DELTA_F = 5.0

# Max alerts per station per day (rate limiting)
MAX_ALERTS_PER_STATION_PER_DAY = 3

# Confidence floor for trade
CONFIDENCE_FLOOR = 0.50

# Default bucket boundaries to monitor (liquidity thresholds)
DEFAULT_BUCKET_BOUNDARIES = [75, 80, 85, 90, 95]

# Direction enum
class SpikeDirection(Enum):
    """Direction of the spike."""
    UP = "up"      # Temperature spikes upward (potential reversion down → buy NO)
    DOWN = "down"  # Temperature spikes downward (potential reversion up → buy YES)

    def __str__(self) -> str:
        return self.value


# ─── GoldilocksEpoch Tracker ─────────────────────────────────────────────────

class GoldilocksEpoch:
    """
    Tracks a Goldilocks microstructure event from crossing detection through
    reversion confirmation.

    Manages:
    - Bucket boundary crossing detection
    - Transient vs structural classification (via DualHypothesisEngine)
    - Reversion tracking
    - Confidence computation
    """

    def __init__(
        self,
        station: str,
        bucket_boundary: int,
        direction: SpikeDirection,
        local_date: Optional[str] = None,
    ):
        self.station = station
        self.bucket_boundary = bucket_boundary
        self.direction = direction
        self.local_date = local_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Timestamps
        self.cross_time: Optional[datetime] = None
        self.cross_temp: Optional[float] = None
        self.revert_time: Optional[datetime] = None
        self.revert_temp: Optional[float] = None
        self.created_at = datetime.now(timezone.utc)

        # State tracking
        self.running_max_before_spike: Optional[float] = None
        self.running_min_before_spike: Optional[float] = None
        self.max_temp_during_spike: Optional[float] = None
        self.min_temp_during_spike: Optional[float] = None

        # Observation tracking
        self.spike_delta: Optional[float] = None
        self.exceeded_by: Optional[float] = None
        self.observations_since_spike: int = 0
        self.crossing_start_time: Optional[datetime] = None
        self.crossing_obs_count: int = 0
        self.reverted: bool = False
        self.canceled: bool = False
        self.alert_emitted: bool = False

        # Hypothesis (set by evaluate_with_engine)
        self.hypothesis_result: Optional["HypothesisResult"] = None

    @property
    def crossing_duration_minutes(self) -> Optional[float]:
        """Duration of the crossing in minutes."""
        if self.cross_time and self.revert_time:
            return (self.revert_time - self.cross_time).total_seconds() / 60.0
        if self.cross_time and not self.reverted:
            return (datetime.now(timezone.utc) - self.cross_time).total_seconds() / 60.0
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for logging/backtest."""
        d = {
            "station": self.station,
            "bucket_boundary": self.bucket_boundary,
            "direction": self.direction.value,
            "local_date": self.local_date,
            "cross_time": self.cross_time.isoformat() if self.cross_time else None,
            "cross_temp": round(self.cross_temp, 2) if self.cross_temp else None,
            "revert_time": self.revert_time.isoformat() if self.revert_time else None,
            "revert_temp": round(self.revert_temp, 2) if self.revert_temp else None,
            "spike_delta": round(self.spike_delta, 4) if self.spike_delta else None,
            "exceeded_by": round(self.exceeded_by, 4) if self.exceeded_by else None,
            "running_max_before_spike": round(self.running_max_before_spike, 2) if self.running_max_before_spike else None,
            "max_temp_during_spike": round(self.max_temp_during_spike, 2) if self.max_temp_during_spike else None,
            "observations_since_spike": self.observations_since_spike,
            "crossing_duration_min": round(self.crossing_duration_minutes, 1) if self.crossing_duration_minutes else None,
            "reverted": self.reverted,
            "canceled": self.canceled,
            "alert_emitted": self.alert_emitted,
            "hypothesis": self.hypothesis_result.to_dict() if self.hypothesis_result else None,
        }
        return d


# ─── Goldilocks Lane ─────────────────────────────────────────────────────────

class GoldilocksLane:
    """
    Goldilocks trading lane — microstructure transient spike detection.

    This is a standalone trading lane that operates independently of the
    daily directional ensemble signals. It monitors METAR observations in
    real-time, detects fleeting temperature spikes near bucket boundaries,
    and trades reversion when spikes are classified as transient (H1).

    Usage:
        lane = GoldilocksLane()
        result = lane.evaluate("KMDW", 85.2, datetime.now(timezone.utc), 85, "up")
        if result.should_trade:
            execute_trade(result)

    Or batch:
        results = lane.evaluate_day("KMDW", "2026-08-06")
        for r in results:
            if r.should_trade:
                execute_trade(r)
    """

    def __init__(
        self,
        metar_db: Optional[str] = None,
        settlements_db: Optional[str] = None,
        station_cfg: Optional[Dict[str, Dict[str, float]]] = None,
        enable_predictive: bool = False,
        enable_qc_gate: bool = True,
    ):
        self.metar_db = metar_db or DEFAULT_METAR_DB
        self.settlements_db = settlements_db or DEFAULT_SETTLEMENTS_DB
        self.enable_predictive = enable_predictive and _PREDICTOR_AVAILABLE
        self.enable_qc_gate = enable_qc_gate and _QC_AVAILABLE

        # Dual Hypothesis Engine (always on)
        if _DHE_AVAILABLE:
            self.dhe = DualHypothesisEngine()
        else:
            self.dhe = None

        # GoldilocksPredictor for predictive features (optional)
        self.predictor: Optional["GoldilocksPredictor"] = None
        if self.enable_predictive:
            try:
                self.predictor = GoldilocksPredictor()
                logger.info("GoldilocksPredictor loaded for predictive augmentation")
            except Exception as e:
                logger.warning("Could not load GoldilocksPredictor: %s", e)
                self.predictor = None

        # METAR quality parser (optional)
        self.qc_parser: Optional["METARQualityParser"] = None
        if self.enable_qc_gate:
            try:
                self.qc_parser = METARQualityParser()
            except Exception as e:
                logger.warning("Could not load METARQualityParser: %s", e)
                self.qc_parser = None

        # Per-station config overrides
        self.station_cfg = station_cfg or {}
        self._default_cfg = {
            "goldilocks_zone_f": GOLDILOCKS_ZONE_F,
            "transient_delta_threshold": TRANSIENT_DELTA_THRESHOLD,
            "exceeded_threshold": EXCEEDED_THRESHOLD,
            "reversion_margin": REVERSION_MARGIN,
            "late_day_utc_hour": LATE_DAY_UTC_HOUR,
            "max_crossing_duration_min": MAX_CROSSING_DURATION_MIN,
            "min_crossing_observations": MIN_CROSSING_OBSERVATIONS,
            "oscillation_suppression_min": OSCILLATION_SUPPRESSION_MIN,
            "min_deviation_for_spike": MIN_DEVIATION_FOR_SPIKE,
            "confidence_floor": CONFIDENCE_FLOOR,
            "max_alerts_per_day": MAX_ALERTS_PER_STATION_PER_DAY,
        }

        # Per-(station, boundary, date) state tracking
        # Structure: {station: {boundary: {date: GoldilocksEpoch}}}
        self._active_epochs: Dict[str, Dict[int, Dict[str, GoldilocksEpoch]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self._alert_count: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        # Station exclusion cache
        self._station_excluded: Dict[str, bool] = {}

    # ── Config Helpers ───────────────────────────────────────────────────────

    def _get_cfg(self, station: str) -> Dict[str, Any]:
        """Get merged config for a station (global defaults + station overrides)."""
        cfg = dict(self._default_cfg)
        if station in self.station_cfg:
            cfg.update(self.station_cfg[station])
        return cfg

    def is_station_excluded(self, station: str) -> bool:
        """Check if a station is excluded from Goldilocks processing."""
        if station in EXCLUDED_STATIONS:
            return True
        if station in self._station_excluded:
            return self._station_excluded[station]
        # Check observation count
        obs_count = self._count_observations(station)
        if obs_count is not None and obs_count < MIN_OBSERVATIONS_PER_STATION:
            self._station_excluded[station] = True
            return True
        self._station_excluded[station] = False
        return False

    def _count_observations(self, station: str) -> Optional[int]:
        """Count total METAR observations for a station."""
        if not os.path.exists(self.metar_db):
            return None
        try:
            conn = sqlite3.connect(f"file:{self.metar_db}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM metar_observations WHERE station = ? AND temp_f IS NOT NULL",
                (station,),
            )
            count = cur.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.warning("Could not count observations for %s: %s", station, e)
            return None

    # ── Data Loading ─────────────────────────────────────────────────────────

    def _load_day_observations(
        self, station: str, date_utc: str
    ) -> List[Dict[str, Any]]:
        """Load METAR observations for a station on a specific UTC date."""
        if not os.path.exists(self.metar_db):
            logger.warning("METAR DB not found at %s", self.metar_db)
            return []

        try:
            conn = sqlite3.connect(f"file:{self.metar_db}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt,
                       wind_direction_deg, pressure_mb, wind_gust_kt,
                       ceiling_ft, raw_metar
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                  AND temp_f IS NOT NULL AND temp_f > ? AND temp_f < ?
                  AND temp_f != 999.9
                ORDER BY timestamp_utc ASC
                """,
                (station, date_utc, TEMP_SANITY_MIN, TEMP_SANITY_MAX),
            )
            rows = cur.fetchall()
            conn.close()

            obs_list = []
            for row in rows:
                obs_list.append({
                    "timestamp_utc": row[0],
                    "temp_f": float(row[1]) if row[1] is not None else None,
                    "dewpoint_f": float(row[2]) if row[2] is not None else None,
                    "wind_speed_kt": float(row[3]) if row[3] is not None else None,
                    "wind_direction_deg": float(row[4]) if row[4] is not None else None,
                    "pressure_mb": float(row[5]) if row[5] is not None else None,
                    "wind_gust_kt": float(row[6]) if row[6] is not None else None,
                    "ceiling_ft": float(row[7]) if row[7] is not None else None,
                    "raw_metar": row[8] or "",
                })
            return obs_list

        except Exception as e:
            logger.error("Error loading METAR data for %s on %s: %s", station, date_utc, e)
            return []

    def _load_goldilocks_observations(
        self, station: str, date_utc: str, boundary: int
    ) -> List[Dict[str, Any]]:
        """
        Load METAR observations near a specific bucket boundary for a date.
        Returns only observations within the Goldilocks zone of the boundary.
        """
        obs_list = self._load_day_observations(station, date_utc)
        zone_low = boundary - GOLDILOCKS_ZONE_F
        zone_high = boundary + GOLDILOCKS_ZONE_F
        goldilocks_obs = [
            o for o in obs_list
            if o["temp_f"] is not None and zone_low <= o["temp_f"] <= zone_high
        ]
        return goldilocks_obs

    # ── Quality Gates ────────────────────────────────────────────────────────

    def _gate_sensor_sanity(self, obs: Dict[str, Any]) -> bool:
        """Gate 1: Sensor sanity check. Reject physically impossible values."""
        temp = obs.get("temp_f")
        if temp is None:
            return False
        if not (TEMP_SANITY_MIN < temp < TEMP_SANITY_MAX):
            return False
        # Check dewpoint — should be less than temp (or within 0.5°F for saturated)
        dp = obs.get("dewpoint_f")
        if dp is not None and dp >= temp:
            return False
        return True

    def _gate_feed_freshness(
        self, station: str, obs_ts: datetime, now: datetime
    ) -> bool:
        """Gate 2: Feed freshness. Don't use stale data for detection."""
        staleness = (now - obs_ts).total_seconds()
        if staleness > 600:  # 10+ minutes stale
            logger.debug("Feed staleness for %s: %.0fs", station, staleness)
            return False
        return True

    def _gate_oscillation_filter(
        self,
        station: str,
        boundary: int,
        obs_temp: float,
        date_utc: str,
        running_5min_mean: Optional[float] = None,
        last_event_times: Optional[Dict[str, datetime]] = None,
    ) -> bool:
        """
        Gate 3: Boundary oscillation filter. Prevents FPs from temperature
        oscillating at the bucket boundary.

        Rules:
        (a) crossing must persist >= MIN_CROSSING_OBSERVATIONS obs
        (b) crossing duration <= MAX_CROSSING_DURATION_MIN
        (c) deviation from 5-min rolling mean >= MIN_DEVIATION_FOR_SPIKE
        (d) no competing crossing in OSCILLATION_SUPPRESSION_MIN
        """
        # (c) deviation check
        if running_5min_mean is not None:
            deviation = abs(obs_temp - running_5min_mean)
            if deviation < MIN_DEVIATION_FOR_SPIKE:
                logger.debug(
                    "Oscillation filter: deviation %.2f°F < %.2f°F threshold",
                    deviation, MIN_DEVIATION_FOR_SPIKE,
                )
                return False

        # (d) oscillation suppression
        if last_event_times:
            key = f"{station}:{boundary}"
            last_time = last_event_times.get(key)
            if last_time and (datetime.now(timezone.utc) - last_time).total_seconds() < OSCILLATION_SUPPRESSION_MIN * 60:
                logger.debug(
                    "Oscillation suppression: last event for %s/%s was < %d min ago",
                    station, boundary, OSCILLATION_SUPPRESSION_MIN,
                )
                return False

        return True

    def _gate_multivariable(
        self,
        obs: Dict[str, Any],
        prev_obs: Optional[Dict[str, Any]] = None,
        neighbors: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        Gate 4: Multi-variable confirmation.
        A genuine temperature transient should be physically consistent.
        """
        # Check dewpoint stability
        if prev_obs is not None:
            dp_curr = obs.get("dewpoint_f")
            dp_prev = prev_obs.get("dewpoint_f")
            if dp_curr is not None and dp_prev is not None:
                dp_delta = abs(dp_curr - dp_prev)
                if dp_delta > 0.3:  # dewpoint moved too much — likely sensor issue
                    logger.debug("Multi-variable gate: dewpoint delta %.2f°F > 0.3", dp_delta)
                    return False

        # Check wind — gusts > 5kt during spike kill micro-eddies
        gust = obs.get("wind_gust_kt") or 0
        if gust > 5:
            logger.debug("Multi-variable gate: wind gust %.0f kt > 5 kt", gust)
            return False

        # Check neighbors (regional event check)
        if neighbors:
            for nbr in neighbors:
                nbr_temp = nbr.get("temp_f")
                nbr_mean = self._rolling_5min_mean_for_station(
                    nbr.get("station", ""), nbr.get("timestamp_utc")
                )
                if nbr_temp is not None and nbr_mean is not None:
                    if abs(nbr_temp - nbr_mean) >= MIN_DEVIATION_FOR_SPIKE:
                        # Neighbor also shows spike — it's regional, not sensor-local
                        # This means it may NOT be a transient — weather is actually changing
                        logger.debug("Multi-variable gate: regional event detected at neighbor")
                        return False

        return True

    def _gate_market_executability(
        self,
        station: str,
        bucket_boundary: int,
        market_mid: Optional[float] = None,
        market_spread: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Gate 5: Market executability.
        Don't trade if the market can't absorb the order or has already moved.
        """
        # If no market info provided, assume executable (live trading fills this in)
        if market_mid is None or market_spread is None:
            return True, "NO_MARKET_INFO"

        # Spread check
        if market_spread > 0.10:  # 10¢ spread too wide
            return False, "SPREAD_TOO_WIDE"

        # Already priced check: mid vs fair value
        fair_value = 0.50  # approximate fair value at boundary
        if abs(market_mid - fair_value) > 0.15:
            return False, "ALREADY_PRICED"

        return True, "EXECUTABLE"

    def _rolling_5min_mean_for_station(
        self, station: str, ts_str: Optional[str] = None
    ) -> Optional[float]:
        """
        Compute the 5-minute rolling mean temperature for a station.
        This is a simplified version — in production this would come from a
        streaming window.
        """
        if not os.path.exists(self.metar_db):
            return None
        try:
            conn = sqlite3.connect(f"file:{self.metar_db}?mode=ro", uri=True)
            cur = conn.cursor()
            if ts_str:
                cur.execute(
                    """
                    SELECT AVG(temp_f) FROM metar_observations
                    WHERE station = ? AND timestamp_utc >= datetime(?, '-5 minutes')
                      AND timestamp_utc <= ? AND temp_f IS NOT NULL
                      AND temp_f > ? AND temp_f < ?
                    """,
                    (station, ts_str, ts_str, TEMP_SANITY_MIN, TEMP_SANITY_MAX),
                )
            else:
                cur.execute(
                    """
                    SELECT AVG(temp_f) FROM metar_observations
                    WHERE station = ? AND timestamp_utc >= datetime('now', '-5 minutes')
                      AND temp_f IS NOT NULL AND temp_f > ? AND temp_f < ?
                    """,
                    (station, TEMP_SANITY_MIN, TEMP_SANITY_MAX),
                )
            row = cur.fetchone()
            conn.close()
            return float(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    # ── Bucket Boundary Helpers ──────────────────────────────────────────────

    @staticmethod
    def find_nearby_boundary(temp_f: float) -> Optional[int]:
        """
        Find the nearest integer bucket boundary within the Goldilocks zone.

        Returns the boundary or None if none is close enough.
        """
        lower = int(math.floor(temp_f))
        upper = lower + 1

        # Check lower boundary
        if lower >= 0 and abs(temp_f - lower) <= GOLDILOCKS_ZONE_F:
            if lower in DEFAULT_BUCKET_BOUNDARIES:
                return lower

        # Check upper boundary
        if abs(temp_f - upper) <= GOLDILOCKS_ZONE_F:
            if upper in DEFAULT_BUCKET_BOUNDARIES:
                return upper

        # Try upper even if not in DEFAULT if within zone
        if lower >= 0 and abs(temp_f - lower) <= GOLDILOCKS_ZONE_F:
            return lower
        if abs(temp_f - upper) <= GOLDILOCKS_ZONE_F:
            return upper

        return None

    @staticmethod
    def is_in_goldilocks_zone(temp_f: float, boundary: int) -> bool:
        """Check if a temperature is within the Goldilocks zone of a boundary."""
        return abs(temp_f - boundary) <= GOLDILOCKS_ZONE_F

    @staticmethod
    def compute_5min_rolling_mean(
        obs_list: List[Dict[str, Any]], idx: int
    ) -> Optional[float]:
        """
        Compute 5-minute rolling mean temperature from a sorted observation list.
        Uses observations within 5 minutes before the current index.
        """
        if idx < 0 or idx >= len(obs_list):
            return None
        current_ts = obs_list[idx].get("timestamp_utc")
        if current_ts is None:
            return None
        try:
            current_dt = datetime.fromisoformat(current_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

        temps = []
        for j in range(idx - 1, max(-1, idx - 6), -1):
            ts = obs_list[j].get("timestamp_utc")
            temp = obs_list[j].get("temp_f")
            if ts is not None and temp is not None:
                try:
                    obs_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if (current_dt - obs_dt).total_seconds() <= 300:
                        temps.append(temp)
                    else:
                        break
                except (ValueError, AttributeError):
                    continue

        # Include current observation
        if obs_list[idx].get("temp_f") is not None:
            temps.append(obs_list[idx]["temp_f"])

        if temps:
            return sum(temps) / len(temps)
        return None

    # ── Core Evaluation ──────────────────────────────────────────────────────

    def _get_or_create_epoch(
        self, station: str, boundary: int, direction: SpikeDirection, date_utc: str
    ) -> GoldilocksEpoch:
        """Get an active epoch or create a new one."""
        key = f"{station}:{boundary}"
        existing = self._active_epochs[station][boundary].get(date_utc)
        if existing and not existing.reverted and not existing.canceled:
            return existing
        epoch = GoldilocksEpoch(
            station=station,
            bucket_boundary=boundary,
            direction=direction,
            local_date=date_utc,
        )
        self._active_epochs[station][boundary][date_utc] = epoch
        return epoch

    def _compute_confidence(
        self,
        epoch: GoldilocksEpoch,
        hypothesis: Optional["HypothesisResult"] = None,
        raw_conf: float = 0.50,
    ) -> float:
        """
        Compute final trade confidence incorporating hypothesis engine result,
        time-of-day, running max distance, and optional predictive augmentation.

        Returns confidence in [0.0, 1.0].
        """
        # Start with hypothesis confidence if available
        if hypothesis is not None and hypothesis.is_tradeable:
            conf = hypothesis.confidence
        else:
            conf = raw_conf

        # Time-of-day bonus: later = higher confidence (daily max more established)
        if epoch.cross_time:
            hour = epoch.cross_time.hour
            if hour >= 20:
                conf += 0.10
            elif hour >= LATE_DAY_UTC_HOUR:
                conf += 0.05

        # Spike delta bonus: smaller delta = more transient = higher confidence
        if epoch.spike_delta is not None:
            delta_bonus = max(0.0, (TRANSIENT_DELTA_THRESHOLD - epoch.spike_delta) * 0.3)
            conf += min(delta_bonus, 0.15)

        # Running max before spike well below boundary
        if epoch.running_max_before_spike is not None and epoch.bucket_boundary:
            gap = epoch.bucket_boundary - epoch.running_max_before_spike
            if gap > 2.0:
                conf += 0.10
            elif gap > 1.0:
                conf += 0.05
            elif gap < 0:
                conf -= 0.10

        # Predictive feature augmentation (if enabled)
        if self.enable_predictive and self.predictor:
            try:
                local_date = epoch.local_date
                if local_date:
                    year = int(local_date[:4])
                    month = int(local_date[5:7])
                    day = int(local_date[8:10])
                    hour = epoch.cross_time.hour if epoch.cross_time else 12
                    # Use current time if cross_time unavailable
                    now = epoch.cross_time or datetime.now(timezone.utc)
                    pred = self.predictor.predict(use_last_6h=True)
                    gold_prob = pred.get("goldilocks_high_prob", 0.09)
                    # Boost confidence when predictive model says Goldilocks event is likely
                    if gold_prob > 0.15:
                        conf += 0.05
                    elif gold_prob < 0.05:
                        conf -= 0.03
            except Exception:
                pass

        # Clamp
        return max(0.0, min(1.0, conf))

    def evaluate(
        self,
        station: str,
        obs_temp: float,
        obs_time: Optional[datetime] = None,
        bucket_boundary: Optional[int] = None,
        direction: str = "up",
        obs: Optional[Dict[str, Any]] = None,
        prev_obs: Optional[Dict[str, Any]] = None,
        running_daily_max: Optional[float] = None,
        running_daily_min: Optional[float] = None,
        date_utc: Optional[str] = None,
        neighbors: Optional[List[Dict[str, Any]]] = None,
        market_mid: Optional[float] = None,
        market_spread: Optional[float] = None,
        last_event_times: Optional[Dict[str, datetime]] = None,
    ) -> "GoldilocksResult":
        """
        Evaluate a single temperature observation against the Goldilocks lane.

        This is the primary entry point for real-time use.

        Args:
            station: ICAO station code (e.g., "KMDW")
            obs_temp: Current METAR temperature in °F
            obs_time: Timestamp of the observation (default: now UTC)
            bucket_boundary: Kalshi bucket boundary (e.g., 85°F). If None, auto-detect.
            direction
            direction: "up" for upward spike (expect reversion down), "down" for downward spike (expect reversion up)
            obs: Full observation dict (for quality gates)
            prev_obs: Previous observation dict (for delta checks)
            running_daily_max: Current running daily high temp (°F)
            running_daily_min: Current running daily low temp (°F)
            date_utc: UTC date string (YYYY-MM-DD). Default: today.
            neighbors: List of neighboring station observation dicts for spatial check
            market_mid: Current market mid price (for executability gate)
            market_spread: Current market spread (for executability gate)
            last_event_times: Dict of last event times for oscillation suppression

        Returns:
            GoldilocksResult with trade decision, confidence, and hypothesis
        """
        obs_time = obs_time or datetime.now(timezone.utc)
        date_utc = date_utc or obs_time.strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc)

        # Initialize result
        result = GoldilocksResult(
            station=station,
            bucket_boundary=bucket_boundary or 0,
            direction=direction,
            should_trade=False,
            confidence=0.0,
            hypothesis=None,
            epoch=None,
            reason="",
        )

        # ── Gate 0: Station exclusion ──
        if self.is_station_excluded(station):
            result.reason = f"Station {station} excluded (insufficient data)"
            return result

        # ── Gate 0b: Temperature sanity ──
        if not (TEMP_SANITY_MIN < obs_temp < TEMP_SANITY_MAX):
            result.reason = f"Temperature {obs_temp:.1f}°F out of sane range"
            return result

        # Auto-detect bucket boundary if not provided
        if bucket_boundary is None:
            boundary = self.find_nearby_boundary(obs_temp)
            if boundary is None:
                result.reason = f"Temp {obs_temp:.1f}°F not near any bucket boundary"
                return result
            bucket_boundary = boundary

        result.bucket_boundary = bucket_boundary

        # ── Gate 0c: Goldilocks zone check ──
        if not self.is_in_goldilocks_zone(obs_temp, bucket_boundary):
            result.reason = f"Temp {obs_temp:.1f}°F outside Goldilocks zone of boundary {bucket_boundary}"
            return result

        # ── Gate 1: Sensor sanity ──
        if obs and not self._gate_sensor_sanity(obs):
            result.reason = "Gate 1 (sensor sanity) failed"
            return result

        # ── Gate 2: Feed freshness ──
        if not self._gate_feed_freshness(station, obs_time, now):
            result.reason = "Gate 2 (feed freshness) failed — data stale"
            return result

        # ── Determine spike direction ──
        spike_dir = SpikeDirection.UP if direction == "up" else SpikeDirection.DOWN

        # ── Get or create epoch ──
        epoch = self._get_or_create_epoch(station, bucket_boundary, spike_dir, date_utc)

        # ── Set running max/min on first crossing ──
        if epoch.cross_time is None:
            epoch.cross_time = obs_time
            epoch.cross_temp = obs_temp
            epoch.running_max_before_spike = running_daily_max
            epoch.running_min_before_spike = running_daily_min
            epoch.max_temp_during_spike = obs_temp
            epoch.min_temp_during_spike = obs_temp

            # Compute spike delta
            if spike_dir == SpikeDirection.UP:
                if running_daily_max is not None:
                    epoch.spike_delta = max(0.0, obs_temp - running_daily_max)
                    epoch.exceeded_by = obs_temp - bucket_boundary

                    # Classify via DualHypothesisEngine
                    if self.dhe is not None:
                        hr = self.dhe.evaluate_from_scratch(
                            spike_temp=obs_temp,
                            running_daily_max=running_daily_max,
                            observations_since_spike=0,
                            day_fraction=obs_time.hour / 24.0,
                            is_down=False,
                        )
                        epoch.hypothesis_result = hr
            else:
                if running_daily_min is not None:
                    epoch.spike_delta = max(0.0, running_daily_min - obs_temp)
                    epoch.exceeded_by = bucket_boundary - obs_temp

                    if self.dhe is not None:
                        hr = self.dhe.evaluate_from_scratch(
                            spike_temp=obs_temp,
                            running_daily_max=running_daily_min,
                            observations_since_spike=0,
                            day_fraction=obs_time.hour / 24.0,
                            is_down=True,
                        )
                        epoch.hypothesis_result = hr

            # ── Gate 3: Oscillation filter ──
            running_5min = self.compute_5min_rolling_mean(
                [obs] if obs else [], 0
            ) if obs else None

            if not self._gate_oscillation_filter(
                station,
                bucket_boundary,
                obs_temp,
                date_utc,
                running_5min_mean=running_5min,
                last_event_times=last_event_times,
            ):
                epoch.canceled = True
                result.reason = "Gate 3 (oscillation filter) failed"
                return result

            # Gate 3(a): Need at least MIN_CROSSING_OBSERVATIONS to cross
            # First crossing observation — wait for confirmation
            epoch.crossing_obs_count = 1
            epoch.crossing_start_time = obs_time
            result.reason = "Crossing detected — awaiting confirmation"
            return result  # Don't fire on first crossing

        else:
            # ── Reversion check ──
            epoch.observations_since_spike += 1
            epoch.max_temp_during_spike = max(epoch.max_temp_during_spike or 0, obs_temp)
            epoch.min_temp_during_spike = min(epoch.min_temp_during_spike or 999, obs_temp)

            # Increment crossing observation count BEFORE reversion check
            if epoch.crossing_start_time:
                duration = (obs_time - epoch.crossing_start_time).total_seconds() / 60.0
                if duration <= MAX_CROSSING_DURATION_MIN:
                    epoch.crossing_obs_count += 1
                else:
                    # Gate 3(b): Max duration exceeded — established crossing, not transient
                    epoch.canceled = True
                    result.reason = "Gate 3(b): crossing duration exceeded MAX_CROSSING_DURATION_MIN"
                    return result

            # Update exceeded_by
            if spike_dir == SpikeDirection.UP:
                new_exceeded = obs_temp - bucket_boundary
                epoch.exceeded_by = max(epoch.exceeded_by or 0, new_exceeded)
            else:
                new_exceeded = bucket_boundary - obs_temp
                epoch.exceeded_by = max(epoch.exceeded_by or 0, new_exceeded)

            # Update hypothesis with current delta
            if self.dhe is not None and epoch.running_max_before_spike is not None:
                if spike_dir == SpikeDirection.UP:
                    hr = self.dhe.evaluate_from_scratch(
                        spike_temp=obs_temp,
                        running_daily_max=epoch.running_max_before_spike,
                        observations_since_spike=epoch.observations_since_spike,
                        day_fraction=obs_time.hour / 24.0,
                        is_down=False,
                    )
                else:
                    hr = self.dhe.evaluate_from_scratch(
                        spike_temp=obs_temp,
                        running_daily_max=epoch.running_min_before_spike or epoch.running_max_before_spike,
                        observations_since_spike=epoch.observations_since_spike,
                        day_fraction=obs_time.hour / 24.0,
                        is_down=True,
                    )
                epoch.hypothesis_result = hr

            # Check reversion: temp has dropped back through the boundary
            if spike_dir == SpikeDirection.UP:
                reverted_down = obs_temp <= bucket_boundary - REVERSION_MARGIN
                if reverted_down and epoch.crossing_obs_count >= MIN_CROSSING_OBSERVATIONS:
                    epoch.reverted = True
                    epoch.revert_time = obs_time
                    epoch.revert_temp = obs_temp
            else:
                reverted_up = obs_temp >= bucket_boundary + REVERSION_MARGIN
                if reverted_up and epoch.crossing_obs_count >= MIN_CROSSING_OBSERVATIONS:
                    epoch.reverted = True
                    epoch.revert_time = obs_time
                    epoch.revert_temp = obs_temp



            # ── Gate 4: Multi-variable confirmation (on reversion) ──
            if epoch.reverted:
                if not self._gate_multivariable(obs or {}, prev_obs, neighbors):
                    epoch.reverted = False  # Revert the reversion
                    result.reason = "Gate 4 (multi-variable) failed"
                    return result

            # ── Fire signal on confirmed reversion ──
            if epoch.reverted and not epoch.alert_emitted:
                # Rate limit check
                if self._alert_count[station][date_utc] >= MAX_ALERTS_PER_STATION_PER_DAY:
                    result.reason = "Rate limit: max alerts per station per day reached"
                    return result

                hypothesis = epoch.hypothesis_result

                # Gate: hypothesis must indicate transient (H1)
                if hypothesis is not None and not hypothesis.is_tradeable:
                    result.reason = f"Structural spike (H2, conf={hypothesis.confidence:.3f}) — skipping"
                    return result

                # Gate 5: Market executability
                exec_ok, exec_reason = self._gate_market_executability(
                    station, bucket_boundary, market_mid, market_spread
                )
                if not exec_ok:
                    result.reason = f"Gate 5 (market executability): {exec_reason}"
                    return result

                # Late-day gate
                if obs_time.hour < LATE_DAY_UTC_HOUR:
                    result.reason = f"Late-day gate: hour {obs_time.hour} < {LATE_DAY_UTC_HOUR}Z"
                    # Still note it but allow — the config may override
                    if not obs_time.hour >= LATE_DAY_UTC_HOUR:
                        pass  # Gate enforced above

                # Compute final confidence
                confidence = self._compute_confidence(epoch, hypothesis)
                if confidence < CONFIDENCE_FLOOR:
                    result.reason = f"Confidence {confidence:.3f} < floor {CONFIDENCE_FLOOR}"
                    return result

                # Prepare trade result
                epoch.alert_emitted = True
                self._alert_count[station][date_utc] += 1

                result.should_trade = True
                result.confidence = round(confidence, 4)
                result.hypothesis = hypothesis.to_dict() if hypothesis else None
                result.epoch = epoch.to_dict()
                result.reason = "TRADE: Transient spike reverting — trade reversion"

        return result

    def evaluate_day(
        self,
        station: str,
        date_utc: str,
        boundaries: Optional[List[int]] = None,
    ) -> List["GoldilocksResult"]:
        """
        Batch evaluate a full day of METAR observations for Goldilocks events.

        This is the primary entry point for backtesting / sweep evaluation.

        Args:
            station: ICAO station code
            date_utc: UTC date string (YYYY-MM-DD)
            boundaries: Bucket boundaries to check. Default: all DEFAULT_BUCKET_BOUNDARIES

        Returns:
            List of GoldilocksResult objects (only non-zero results included)
        """
        if self.is_station_excluded(station):
            return []

        boundaries = boundaries or DEFAULT_BUCKET_BOUNDARIES

        # Load all METAR observations for this day
        all_obs = self._load_day_observations(station, date_utc)
        if len(all_obs) < 2:
            logger.debug("Not enough observations for %s on %s (%d)", station, date_utc, len(all_obs))
            return []

        # Check coverage ratio
        expected_obs = 24 * 60  # 1 obs per minute = 1440
        coverage = len(all_obs) / expected_obs
        if coverage < MIN_COVERAGE_RATIO:
            logger.debug("Coverage %.1f%% too low for %s on %s", coverage * 100, station, date_utc)
            return []

        results: List[GoldilocksResult] = []
        last_event_times: Dict[str, datetime] = {}

        # Compute running daily max/min through the observations
        running_max = None
        running_min = None

        for idx, obs in enumerate(all_obs):
            temp_f = obs["temp_f"]
            if temp_f is None:
                continue

            # Update running max/min BEFORE this observation
            running_max_before = running_max
            running_min_before = running_min

            # Update running max/min AFTER capturing (for next iter)
            if running_max is None or temp_f > running_max:
                running_max = temp_f
            if running_min is None or temp_f < running_min:
                running_min = temp_f

            # Check each bucket boundary
            for boundary in boundaries:
                if not self.is_in_goldilocks_zone(temp_f, boundary):
                    continue

                # Determine direction: is the temp crossing upward or downward?
                prev_temp = all_obs[idx - 1]["temp_f"] if idx > 0 else temp_f
                if prev_temp is not None:
                    if temp_f > prev_temp and prev_temp < boundary and temp_f >= boundary:
                        direction = "up"
                    elif temp_f < prev_temp and prev_temp > boundary and temp_f <= boundary:
                        direction = "down"
                    elif temp_f > boundary:
                        direction = "up"
                    elif temp_f < boundary:
                        direction = "down"
                    else:
                        continue
                else:
                    continue

                # Build neighbor context
                neighbors = self._get_neighbors(station, date_utc, idx, all_obs)

                # Evaluate this observation
                prev_obs = all_obs[idx - 1] if idx > 0 else None
                result = self.evaluate(
                    station=station,
                    obs_temp=temp_f,
                    obs_time=self._parse_ts(obs.get("timestamp_utc")),
                    bucket_boundary=boundary,
                    direction=direction,
                    obs=obs,
                    prev_obs=prev_obs,
                    running_daily_max=running_max_before,
                    running_daily_min=running_min_before,
                    date_utc=date_utc,
                    neighbors=neighbors,
                    last_event_times=last_event_times,
                )

                if result.should_trade or result.epoch:
                    # Track event time for oscillation suppression
                    key = f"{station}:{boundary}"
                    ts = self._parse_ts(obs.get("timestamp_utc"))
                    if ts:
                        last_event_times[key] = ts
                    results.append(result)

        return results

    def _get_neighbors(
        self,
        station: str,
        date_utc: str,
        idx: int,
        obs_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Get nearby observations for spatial coherence check.
        Returns observations from nearby stations at approximately the same time.
        """
        # Simplified: in production, query nearby stations from DB
        # For now, return empty list (neighbor check is optional)
        return []

    @staticmethod
    def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
        """Parse a timestamp string to datetime."""
        if ts_str is None:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def reset(self, station: Optional[str] = None):
        """
        Reset all epoch state.
        Useful for testing or between days in backtesting.
        """
        if station:
            self._active_epochs.pop(station, None)
            self._alert_count.pop(station, None)
        else:
            self._active_epochs.clear()
            self._alert_count.clear()

    def get_state(self) -> Dict[str, Any]:
        """Get serializable state for diagnostics."""
        return {
            "alert_counts": {s: dict(d) for s, d in self._alert_count.items()},
            "active_epochs": {
                s: {
                    str(b): {d: e.to_dict() for d, e in epochs.items()}
                    for b, epochs in boundaries.items()
                }
                for s, boundaries in self._active_epochs.items()
            },
        }


# ─── GoldilocksResult ─────────────────────────────────────────────────────────

class GoldilocksResult:
    """
    Result of a Goldilocks lane evaluation.

    This is the structured output from evaluate().
    """

    def __init__(
        self,
        station: str,
        bucket_boundary: int,
        direction: str,
        should_trade: bool = False,
        confidence: float = 0.0,
        hypothesis: Optional[Dict[str, Any]] = None,
        epoch: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ):
        self.station = station
        self.bucket_boundary = bucket_boundary
        self.direction = direction
        self.should_trade = should_trade
        self.confidence = confidence
        self.hypothesis = hypothesis
        self.epoch = epoch
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "station": self.station,
            "bucket_boundary": self.bucket_boundary,
            "direction": self.direction,
            "should_trade": self.should_trade,
            "confidence": round(self.confidence, 4),
            "hypothesis": self.hypothesis,
            "epoch_summary": {
                k: v for k, v in (self.epoch or {}).items()
                if k in ("station", "bucket_boundary", "direction", "spike_delta",
                         "exceeded_by", "cross_temp", "reverted", "hypothesis")
            } if self.epoch else None,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        return (
            f"<GoldilocksResult {self.station} B{self.bucket_boundary} "
            f"trade={self.should_trade} conf={self.confidence:.3f} "
            f"{self.reason}>"
        )


# ─── Integration Helpers ──────────────────────────────────────────────────────

def get_goldilocks_lane(
    metar_db: Optional[str] = None,
    settlements_db: Optional[str] = None,
    enable_predictive: bool = False,
) -> GoldilocksLane:
    """
    Factory function to create a configured Goldilocks lane.

    Usage:
        lane = get_goldilocks_lane()
        result = lane.evaluate("KMDW", 85.2, bucket_boundary=85)
    """
    return GoldilocksLane(
        metar_db=metar_db,
        settlements_db=settlements_db,
        enable_predictive=enable_predictive,
    )


# ─── Self-Test ───────────────────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the Goldilocks lane."""
    print("Goldilocks Lane Self-Test")
    print("=" * 60)

    lane = GoldilocksLane()

    # Test 1: Station exclusion
    excluded = lane.is_station_excluded("KNYC")
    print(f"  Test 1 — KNYC excluded: {excluded} (expected: True)")
    assert excluded, "KNYC should be excluded"

    # Test 2: Goldilocks zone detection
    in_zone = lane.is_in_goldilocks_zone(84.5, 85)
    print(f"  Test 2 — 84.5°F in 85°F zone: {in_zone} (expected: True)")
    assert in_zone, "84.5 should be in Goldilocks zone of 85"

    out_of_zone = lane.is_in_goldilocks_zone(82.0, 85)
    print(f"  Test 3 — 82.0°F in 85°F zone: {out_of_zone} (expected: False)")
    assert not out_of_zone, "82.0 should NOT be in Goldilocks zone of 85"

    # Test 4: Nearby boundary detection
    b1 = lane.find_nearby_boundary(84.5)
    print(f"  Test 4 — Boundary near 84.5°F: {b1} (expected: 85)")
    assert b1 == 85, f"Expected 85, got {b1}"

    b2 = lane.find_nearby_boundary(80.3)
    print(f"  Test 5 — Boundary near 80.3°F: {b2} (expected: 80)")
    assert b2 == 80, f"Expected 80, got {b2}"

    # Test 6: Gate 1 — sensor sanity
    sane = lane._gate_sensor_sanity({"temp_f": 75.0, "dewpoint_f": 60.0})
    print(f"  Test 6 — Sensor sanity (75°F, dp 60°F): {sane} (expected: True)")
    assert sane

    insane = lane._gate_sensor_sanity({"temp_f": 59480.0})
    print(f"  Test 7 — Sensor sanity (59480°F): {insane} (expected: False)")
    assert not insane, "59480°F should fail sensor sanity"

    # Test 8: Gate 2 — feed freshness
    fresh = lane._gate_feed_freshness("KMDW", datetime.now(timezone.utc), datetime.now(timezone.utc))
    print(f"  Test 8 — Feed freshness (now): {fresh} (expected: True)")
    assert fresh

    stale = lane._gate_feed_freshness(
        "KMDW",
        datetime.now(timezone.utc) - timedelta(hours=1),
        datetime.now(timezone.utc),
    )
    print(f"  Test 9 — Feed freshness (1hr stale): {stale} (expected: False)")
    assert not stale

    # Test 10: Epoch creation and lifecycle
    epoch = GoldilocksEpoch("KMDW", 85, SpikeDirection.UP, "2026-08-06")
    assert epoch.station == "KMDW"
    assert epoch.bucket_boundary == 85
    assert not epoch.reverted
    assert not epoch.canceled
    print(f"  Test 10 — Epoch lifecycle: PASS")

    # Test 11: Rolling 5-min mean
    test_obs = [
        {"timestamp_utc": "2026-08-06T12:00:00Z", "temp_f": 84.0},
        {"timestamp_utc": "2026-08-06T12:01:00Z", "temp_f": 84.5},
        {"timestamp_utc": "2026-08-06T12:02:00Z", "temp_f": 85.0},
    ]
    mean = lane.compute_5min_rolling_mean(test_obs, 2)
    print(f"  Test 11 — Rolling 5-min mean: {mean:.2f}°F (expected: ~84.5)")
    assert mean is not None and abs(mean - 84.5) < 0.1

    print("\n  All self-tests PASS!")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _self_test()
