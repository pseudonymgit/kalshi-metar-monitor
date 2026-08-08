#!/usr/bin/env python3
"""
Radiational Cooling Signal — BaseSignal implementation

Detects clear-night radiational cooling events from METAR observations
and produces directional LOW-market predictions.

Four-pillar trigger:
  1. Clear skies (CLR/FEW cloud codes)
  2. Calm winds (<5 kt sustained)
  3. Low humidity (dewpoint depression >15°F, adjusted for arctic airmasses)
  4. Dry boundary layer

Distinguishes radiational from advective cooling:
  - If frontal passage detected → no trade (advective cooling)
  - If calm, clear, dry → strong confidence in radiational cooling

LOW market only — radiational cooling depresses overnight minima.

Architecture:
  - evaluate(idx, days): sweep-compatible, uses daily METAR aggregates
  - evaluate_for_station(station, date, conn): detailed per-station analysis
    with intraday METAR observations, raw_metar parsing, and RCP engine
  - evaluate_station_rcp(): single-point RCP evaluation (compatible with
    existing RadiationalCoolingDetector from core/radiational_cooling.py)

B-Mode compliant. No AI/ML in the prediction loop.

Usage:
    from core.signals.radiational_cooling_signal import RadiationalCoolingSignal
    signal = RadiationalCoolingSignal('data/metar_backfill.db')
    direction, confidence = signal.evaluate_for_station('KMSP', '2026-01-15')
"""

import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from .base_signal import BaseSignal, validate_signal

# ─── Threshold Constants ────────────────────────────────────────

# Wind speed threshold for radiational cooling (knots)
CALM_WIND_THRESHOLD_KT = 5.0
GUST_DISRUPTION_THRESHOLD_KT = 8.0

# Dewpoint depression thresholds for dryness (Fahrenheit)
DPD_THRESHOLD_STANDARD = 15.0  # Standard dryness threshold
DPD_THRESHOLD_MODERATE = 12.0  # Moderate dryness (dewpoint 15-25°F)
DPD_THRESHOLD_ARCTIC = 5.0     # Arctic airmass (dewpoint < 15°F) - Reduced from 8.0 to 5.0

# Cloud cover thresholds
CLEAR_CLOUD_FRACTION = 0.25        # Cloud fraction below this → clear (increased from 0.15)
OVERCASY_CLOUD_FRACTION = 0.60     # Cloud fraction above this → blocked

# Visibility for fog detection (statute miles)
FOG_VISIBILITY_THRESHOLD_MI = 1.0
FOG_DPD_THRESHOLD_F = 5.0

# Confidence mapping from RCP
RCP_STRONG_THRESHOLD = 0.80
RCP_MODERATE_THRESHOLD = 0.60
RCP_WEAK_THRESHOLD = 0.40
RCP_AMBIENT_THRESHOLD = 0.20

# Cooling type classification scores
ADVECTIVE_FRONTAL_OVERRIDE_SCORE = 3
RADIATIONAL_WIND_SCORE = 2
RADIATIONAL_CLOUD_SCORE = 2
RADIATIONAL_PRESSURE_SCORE = 1
RADIATIONAL_DEWPOINT_SCORE = 1

# METAR raw text sky condition parsing
SKY_COVER_TO_FRACTION = {
    'CLR': 0.00,
    'SKC': 0.00,   # Sky Clear (manual)
    'FEW': 0.20,   # Few (1-2 oktas)
    'SCT': 0.40,   # Scattered (3-4 oktas)
    'BKN': 0.70,   # Broken (5-7 oktas)
    'OVC': 1.00,   # Overcast
}

# Station latitudes for night length computation
STATION_LATITUDES: Dict[str, float] = {
    'KMSP': 44.88, 'KDEN': 39.85, 'KMDW': 41.79, 'KBOS': 42.36,
    'KNYC': 40.78, 'KPHL': 39.87, 'KDCA': 38.85, 'KORD': 41.98,
    'KATL': 33.64, 'KDFW': 32.90, 'KSEA': 47.45, 'KSFO': 37.62,
    'KLAX': 33.94, 'KPHX': 33.43, 'KMIA': 25.79, 'KHOU': 29.65,
    'KMSY': 29.99, 'KOKC': 35.39, 'KSAT': 29.53, 'KAUS': 30.19,
    'KLAS': 36.08,
}

# ─── Helper Functions ──────────────────────────────────────────

def parse_cloud_fraction(raw_metar: str) -> float:
    """
    Parse cloud fraction from raw METAR sky condition groups.

    Extracts CLR/FEW/SCT/BKN/OVC codes from the raw METAR string
    and returns the maximum cloud fraction value.

    Uses heuristics:
      - Highest coverage layer determines cloud fraction
      - CLR/SKC → 0.00
      - FEW (1-2 oktas) → 0.20
      - SCT (3-4 oktas) → 0.40
      - BKN (5-7 oktas) → 0.70
      - OVC → 1.00

    Args:
        raw_metar: Raw METAR string (e.g. 'KATL 150156Z 00000KT 10SM CLR 15/08 A3025')

    Returns:
        Cloud fraction in [0.0, 1.0], or None if parsing fails
    """
    if not raw_metar:
        return None

    # Match sky condition groups: CLR, SKC, FEWnnn, SCTnnn, BKNnnn, OVCnnn
    # Some METARs also have VV (vertical visibility) for indefinite ceiling
    # and CAVOK (ceiling and visibility OK, means clear)
    sky_matches = re.findall(
        r'\b(CLR|SKC|FEW|SCT|BKN|OVC)(\d{3})?\b',
        raw_metar.upper()
    )

    if not sky_matches:
        # Check for CAVOK (clear skies)
        if 'CAVOK' in raw_metar.upper():
            return 0.0
        return None

    # Use the highest coverage layer
    highest = max(code for code, _ in sky_matches)
    return SKY_COVER_TO_FRACTION.get(highest, 0.50)


def compute_dpd(temp_f: float, dewpoint_f: float) -> float:
    """Compute dewpoint depression (Fahrenheit)."""
    if temp_f is None or dewpoint_f is None:
        return 0.0
    return temp_f - dewpoint_f


def is_dry_enough(temp_f: float, dewpoint_f: float) -> Tuple[bool, float]:
    """
    Check if humidity is low enough for radiational cooling.

    Args:
        temp_f: Temperature in Fahrenheit
        dewpoint_f: Dewpoint in Fahrenheit

    Returns:
        (is_dry, dryness_score) where dryness_score ∈ [0, 1]
    """
    dpd = compute_dpd(temp_f, dewpoint_f)

    # Adjust threshold for arctic airmasses
    if dewpoint_f is not None and dewpoint_f < 15.0:
        is_dry = dpd >= DPD_THRESHOLD_ARCTIC  # Changed > to >= for edge cases
        score = min(1.0, dpd / 12.0)
    elif dewpoint_f is not None and dewpoint_f < 25.0:
        is_dry = dpd >= DPD_THRESHOLD_MODERATE  # Changed > to >= for edge cases
        score = min(1.0, dpd / 20.0)
    else:
        is_dry = dpd >= DPD_THRESHOLD_STANDARD  # Changed > to >= for edge cases
        score = min(1.0, dpd / 25.0)

    return is_dry, score


def is_calm_wind(wind_speed_kt: float, wind_gust_kt: Optional[float] = None) -> Tuple[bool, float]:
    """
    Check if wind is calm enough for radiational cooling.

    Args:
        wind_speed_kt: Sustained wind speed in knots
        wind_gust_kt: Optional gust speed in knots

    Returns:
        (is_calm, wind_score) where wind_score ∈ [0, 1]
    """
    if wind_speed_kt is None:
        return False, 0.0

    # Sustained wind check
    if wind_speed_kt >= CALM_WIND_THRESHOLD_KT:
        return False, 0.0

    # Gust check — even intermittent gusts disrupt the stable layer
    if wind_gust_kt is not None and wind_gust_kt > 0:
        if wind_gust_kt >= GUST_DISRUPTION_THRESHOLD_KT:
            return False, 0.0
        # Gust ratio > 1.5× sustained with moderate sustained wind → disruptive
        if wind_speed_kt > 3.0 and wind_gust_kt > 1.5 * wind_speed_kt:
            return False, 0.0

    # Compute wind score (1.0 at 0 kt, 0.0 at threshold)
    score = max(0.0, 1.0 - wind_speed_kt / CALM_WIND_THRESHOLD_KT)
    return True, score


def is_clear_skies(cloud_fraction: Optional[float]) -> Tuple[bool, float]:
    """
    Check if skies are clear enough for radiational cooling.

    Args:
        cloud_fraction: Cloud fraction in [0.0, 1.0] or None

    Returns:
        (is_clear, cloud_score) where cloud_score ∈ [0, 1]
    """
    if cloud_fraction is None:
        # Unknown — conservative: assume partly cloudy
        return False, 0.5

    if cloud_fraction <= CLEAR_CLOUD_FRACTION:
        return True, 1.0 - cloud_fraction
    elif cloud_fraction >= OVERCASY_CLOUD_FRACTION:
        return False, max(0.0, 1.0 - cloud_fraction)
    else:
        # Partial cloud cover — weak signal
        return False, max(0.0, 1.0 - cloud_fraction)


def fog_present(visibility_mi: Optional[float], temp_f: float,
                dewpoint_f: float) -> bool:
    """
    Check if fog/dew formation is present, which releases latent heat
    and suppresses radiational cooling.

    Args:
        visibility_mi: Visibility in statute miles
        temp_f: Temperature in Fahrenheit
        dewpoint_f: Dewpoint in Fahrenheit

    Returns:
        True if fog conditions detected
    """
    if visibility_mi is None:
        return False

    dpd = compute_dpd(temp_f, dewpoint_f)

    # Fog: low visibility + near-saturation
    return visibility_mi < FOG_VISIBILITY_THRESHOLD_MI and dpd < FOG_DPD_THRESHOLD_F


def compute_night_length_factor(latitude: float, day_of_year: int) -> float:
    """
    Compute night length factor from latitude and day of year.

    Simplified model: night length proportional to cos(latitude) and day of year.

    Args:
        latitude: Station latitude in degrees
        day_of_year: Day of year (1-366)

    Returns:
        NightLengthFactor [0.87, 1.10]
    """
    # Solar declination approximation
    declination = 23.44 * math.cos(math.radians(360.0 * (day_of_year - 172) / 365.0))

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    cos_hour_angle = -math.tan(lat_rad) * math.tan(dec_rad)
    cos_hour_angle = max(-1.0, min(1.0, cos_hour_angle))

    day_length_hours = math.degrees(math.acos(cos_hour_angle)) / 15.0
    night_length_hours = 24.0 - day_length_hours

    factor = min(1.10, math.sqrt(night_length_hours / 12.0))
    return factor


def compute_snow_factor_simple(temp_f: float) -> float:
    """
    Simplified snow factor — check if cold enough for snow cover.

    Args:
        temp_f: Current temperature

    Returns:
        SnowFactor [1.0, 1.30]
    """
    if temp_f is not None and temp_f < 32.0:
        return 1.30
    return 1.00


# ─── Cooling Type Classification ──────────────────────────────

def classify_cooling_type(
    wind_speed_kt: Optional[float],
    wind_gust_kt: Optional[float],
    cloud_fraction: Optional[float],
    pressure_trend_24h: Optional[float],
    dewpoint_trend_6h: Optional[float],
    frontal_detected: bool = False,
) -> str:
    """
    Classify the dominant cooling mechanism for a given night.

    Distinguishes radiational cooling from cold air advection (frontal passage).

    Args:
        wind_speed_kt: Evening wind speed
        wind_gust_kt: Evening gust (or None)
        cloud_fraction: Evening cloud fraction
        pressure_trend_24h: 24-hour pressure trend (mb, positive = rising)
        dewpoint_trend_6h: 6-hour dewpoint trend (°F, negative = dropping)
        frontal_detected: Whether a frontal passage has been detected

    Returns:
        'radiational', 'advective', 'mixed', or 'unknown'
    """
    rad_score = 0
    adv_score = 0

    # Frontal passage overrides everything
    if frontal_detected:
        adv_score += ADVECTIVE_FRONTAL_OVERRIDE_SCORE

    # Wind
    if wind_speed_kt is not None:
        if wind_speed_kt < 5.0:
            rad_score += RADIATIONAL_WIND_SCORE
        elif wind_speed_kt > 8.0:
            adv_score += RADIATIONAL_WIND_SCORE

    # Cloud cover
    if cloud_fraction is not None:
        if cloud_fraction < 0.2:
            rad_score += RADIATIONAL_CLOUD_SCORE
        elif cloud_fraction > 0.6:
            adv_score += RADIATIONAL_CLOUD_SCORE

    # Pressure trend (rising = high pressure building = radiational)
    if pressure_trend_24h is not None:
        if pressure_trend_24h > 2.0:
            rad_score += RADIATIONAL_PRESSURE_SCORE
        elif pressure_trend_24h < -2.0:
            adv_score += RADIATIONAL_PRESSURE_SCORE

    # Dewpoint trend (stable/slight decrease = radiational; sharp drop = frontal)
    if dewpoint_trend_6h is not None:
        if dewpoint_trend_6h > -2.0:
            rad_score += RADIATIONAL_DEWPOINT_SCORE
        elif dewpoint_trend_6h < -5.0:
            adv_score += RADIATIONAL_DEWPOINT_SCORE

    # Decision
    if rad_score >= 4 and adv_score <= 1:
        return 'radiational'
    elif adv_score >= 4 and rad_score <= 1:
        return 'advective'
    elif rad_score >= 3 and adv_score >= 3:
        return 'mixed'
    else:
        return 'unknown'


# ─── Signal Class ──────────────────────────────────────────────

class RadiationalCoolingSignal(BaseSignal):
    """
    Radiational Cooling Signal — detects clear-night radiational cooling
    and produces LOW-direction predictions with confidence proportional
    to the expected temperature drop potential.

    Key formula:
        RCP = CloudScore × WindScore × DrynessScore × NightLengthFactor × SnowFactor
        confidence = f(RCP) mapped to [0, 1]
        direction = 'down' when RCP ≥ threshold and no frontal passage detected

    Only fires on LOW markets — radiational cooling depresses nighttime minima.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self._frontal_detector = None  # Lazy import

    @property
    def name(self) -> str:
        return "radiational_cooling"

    @property
    def min_lookback(self) -> int:
        return 2  # Need at least 2 days for trend analysis

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Sweep-compatible evaluate() using daily METAR aggregates.

        Since days are daily aggregates (not nighttime-specific), this is a
        simplified check:
          - Checks average wind speed for calm conditions
          - Checks dewpoint depression for dryness
          - Checks if nighttime conditions likely supported radiational cooling

        For precise evaluation, use evaluate_for_station() which accesses
        raw intraday METAR observations.

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure

        Returns:
            ('down', confidence) if radiational cooling detected,
            (None, 0.0) otherwise.
        """
        if idx < self.min_lookback or idx >= len(days):
            return None, 0.0

        day = days[idx]

        # Extract daily fields (aggregate, not nighttime-specific)
        temp = day.get('temp')           # Avg temp (F)
        low = day.get('low')             # Low temp (F)
        dewpoint = day.get('dewpoint')   # Avg dewpoint (F)
        wind_speed = day.get('wind_speed')  # Avg wind speed (kt)
        pressure = day.get('pressure')   # Avg pressure (mb)
        high = day.get('high')           # High temp (F)

        # Need at least temperature and dewpoint
        if temp is None or dewpoint is None:
            return None, 0.0

        # Check dryness via dewpoint depression
        is_dry, dryness_score = is_dry_enough(temp, dewpoint)
        if not is_dry:
            return None, 0.0

        # Check winds (daily average wind speed — loose filter)
        if wind_speed is not None:
            calm, wind_score = is_calm_wind(wind_speed)
            # With daily aggregates, wind_score may be 0 if wind_speed >= 5.0 kt
            # even though the daily avg includes daytime winds. Use relaxed threshold.
            if wind_score == 0.0 and wind_speed <= CALM_WIND_THRESHOLD_KT * 1.5:
                wind_score = max(0.0, 1.0 - wind_speed / (CALM_WIND_THRESHOLD_KT * 1.5))
            # With daily aggregates, be more lenient on wind
            # (daily avg includes daytime, so calm nighttime avg > 5 kt is unusual)
            if wind_speed > CALM_WIND_THRESHOLD_KT * 1.5:
                return None, 0.0
        else:
            wind_score = 0.5

        # Cloud cover not available in daily aggregates — skip
        cloud_score = 0.7  # Assume somewhat clear (conservative)

        # Compute RCP-based confidence
        latitude = None
        # Try to determine station from days metadata (not directly available)
        # Use a default mid-latitude
        latitude = 40.0
        day_ref = datetime.strptime(day.get('date', '2021-01-01'), '%Y-%m-%d')
        day_of_year = day_ref.timetuple().tm_yday

        night_factor = compute_night_length_factor(latitude, day_of_year)
        snow_factor = compute_snow_factor_simple(low if low else temp)

        rcp = cloud_score * wind_score * dryness_score * night_factor * snow_factor

        # Confidence proportional to RCP
        if rcp < RCP_AMBIENT_THRESHOLD:
            return None, 0.0

        confidence = min(1.0, rcp / RCP_STRONG_THRESHOLD)

        # Direction
        direction = 'down'

        logger.debug(
            "evaluate() -> RCP=%.3f dir=%s conf=%.3f "
            "cloud=%.2f wind=%.2f dry=%.2f night=%.2f snow=%.2f",
            rcp, direction, confidence,
            cloud_score, wind_score, dryness_score, night_factor, snow_factor
        )

        return direction, confidence

    def evaluate_for_station(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection = None,
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate radiational cooling conditions for a specific station
        using raw intraday METAR observations.

        This is the primary evaluation method. It:
          1. Fetches nighttime METAR observations (sunset to sunrise window)
          2. Parses raw_metar for cloud cover codes
          3. Checks wind, dewpoint depression, visibility
          4. Computes RCP score
          5. Classifies cooling type (radiational vs advective)
          6. Returns direction='down' with proportional confidence

        LOW market only — does not fire on HIGH markets.

        Args:
            station: ICAO station code (e.g. 'KMSP')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar_backfill.db

        Returns:
            ('down', confidence) if radiational cooling detected,
            (None, 0.0) otherwise.
        """
        own_conn = conn is None
        if own_conn:
            if not self.db_path or not os.path.exists(self.db_path):
                logger.warning("METAR DB not found at %s", self.db_path)
                return None, 0.0
            conn = sqlite3.connect(self.db_path)

        try:
            station = station.upper()
            latitude = STATION_LATITUDES.get(station, 40.0)
            day_ref = datetime.strptime(date, '%Y-%m-%d')
            day_of_year = day_ref.timetuple().tm_yday
            month = day_ref.month

            # ── Step 1: Fetch nighttime METAR observations ──────
            # Nighttime window: 00:00-12:00 UTC approximates overnight for US stations
            # This covers the period when radiational cooling is most active.
            # For more precision, compute sunset/sunrise from lat/lon.
            night_start = f"{date}T00:00:00"
            night_end = f"{date}T13:00:00"

            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt,
                       wind_gust_kt, visibility_mi, raw_metar, pressure_mb
                FROM metar_observations
                WHERE station = ?
                  AND timestamp_utc >= ?
                  AND timestamp_utc < ?
                  AND temp_f IS NOT NULL
                  AND temp_f > -100
                  AND temp_f < 200
                ORDER BY timestamp_utc ASC
            """, (station, night_start, night_end))

            observations = cur.fetchall()

            if len(observations) < 2:
                logger.debug("evaluate_for_station(%s, %s): insufficient nighttime obs (%d)",
                             station, date, len(observations))
                return None, 0.0

            # ── Step 2: Compute nighttime cloud fraction ───────
            cloud_fractions = []
            wind_speeds = []
            wind_gusts = []
            temps = []
            dewpoints = []
            visibilities = []
            pressures = []

            for obs in observations:
                ts, tf, dw, ws, wg, vis, raw, press = obs
                temps.append(tf)
                dewpoints.append(dw)
                if ws is not None:
                    wind_speeds.append(ws)
                if wg is not None and wg > 0:
                    wind_gusts.append(wg)
                if vis is not None:
                    visibilities.append(vis)
                if press is not None:
                    pressures.append(press)

                # Parse cloud fraction from raw_metar
                cf = parse_cloud_fraction(raw)
                if cf is not None:
                    cloud_fractions.append(cf)

            if not temps or not dewpoints:
                return None, 0.0

            # ── Step 3: Average nighttime conditions ───────────
            avg_temp = sum(temps) / len(temps)
            avg_dewpoint = sum(dewpoints) / len(dewpoints)
            avg_wind = sum(wind_speeds) / len(wind_speeds) if wind_speeds else None
            # Use max gust as the strongest disruptor
            max_gust = max(wind_gusts) if wind_gusts else None
            # Cloud fraction: use the maximum coverage (conservative — cloudiest moment matters)
            # Fall back to ceiling height or visibility as cloud cover proxy
            if cloud_fractions:
                max_cloud = max(cloud_fractions)
            else:
                # Use ceiling_ft as cloud cover proxy (most METARs have this from ISD-lite)
                cur.execute("""
                    SELECT AVG(CAST(ceiling_ft AS REAL))
                    FROM metar_observations
                    WHERE station = ?
                      AND timestamp_utc >= ?
                      AND timestamp_utc < ?
                      AND temp_f IS NOT NULL AND temp_f > -100 AND temp_f < 200
                """, (station, night_start, night_end))
                avg_ceiling = cur.fetchone()[0]
                if avg_ceiling is not None and avg_ceiling > 0:
                    # Ceiling > 25000 ft -> clear; 10000-25000 -> few; < 10000 -> broken/ovc
                    if avg_ceiling > 25000:
                        max_cloud = 0.00
                    elif avg_ceiling > 10000:
                        max_cloud = 0.20
                    elif avg_ceiling > 5000:
                        max_cloud = 0.40
                    elif avg_ceiling > 2000:
                        max_cloud = 0.70
                    else:
                        max_cloud = 1.00
                else:
                    # Fall back to visibility as rough proxy (high vis -> clear skies)
                    cur.execute("""
                        SELECT AVG(visibility_mi)
                        FROM metar_observations
                        WHERE station = ?
                          AND timestamp_utc >= ?
                          AND timestamp_utc < ?
                          AND visibility_mi IS NOT NULL
                    """, (station, night_start, night_end))
                    avg_vis = cur.fetchone()[0]
                    if avg_vis is not None and avg_vis > 8.0:
                        max_cloud = 0.10
                    elif avg_vis is not None and avg_vis > 5.0:
                        max_cloud = 0.30
                    else:
                        max_cloud = 0.50  # Unknown — conservative
            # Pressure: use average
            avg_pressure = sum(pressures) / len(pressures) if pressures else None

            # ── Step 4: Fog check ──────────────────────────────
            min_vis = min(visibilities) if visibilities else None
            if fog_present(min_vis, avg_temp, avg_dewpoint):
                logger.debug("evaluate_for_station(%s, %s): fog detected, suppressing signal",
                             station, date)
                return None, 0.0

            # ── Step 5: Four-pillar checks ─────────────────────
            # 5a: Cloud cover
            clear, cloud_score = is_clear_skies(max_cloud)
            if not clear and cloud_score < 0.3:
                return None, 0.0

            # 5b: Wind
            if avg_wind is not None:
                calm, wind_score = is_calm_wind(avg_wind, max_gust)
                if not calm:
                    return None, 0.0
            else:
                wind_score = 0.5

            # 5c: Dryness
            is_dry, dryness_score = is_dry_enough(avg_temp, avg_dewpoint)
            if not is_dry:
                return None, 0.0

            # 5d: Night length
            night_factor = compute_night_length_factor(latitude, day_of_year)

            # 5e: Snow
            snow_factor = compute_snow_factor_simple(avg_temp)

            # ── Step 6: Compute RCP score ──────────────────────
            rcp = cloud_score * wind_score * dryness_score * night_factor * snow_factor

            if rcp < RCP_AMBIENT_THRESHOLD:
                return None, 0.0

            # ── Step 7: Classify cooling type ──────────────────
            # Check for frontal passage
            frontal_detected = self._detect_frontal_passage(station, date, conn)

            # Compute pressure trend (compare to previous day)
            pressure_trend = self._compute_pressure_trend(station, date, conn)

            # Compute dewpoint trend
            dewpoint_trend = self._compute_dewpoint_trend(station, date, conn)

            cooling_type = classify_cooling_type(
                wind_speed_kt=avg_wind,
                wind_gust_kt=max_gust,
                cloud_fraction=max_cloud,
                pressure_trend_24h=pressure_trend,
                dewpoint_trend_6h=dewpoint_trend,
                frontal_detected=frontal_detected,
            )

            if cooling_type == 'advective':
                # Frontal passage detected — do NOT apply radiational correction
                logger.debug("evaluate_for_station(%s, %s): advective cooling, no trade",
                             station, date)
                return None, 0.0

            # Mixed cooling: reduce confidence by 50%
            mixed_penalty = 0.5 if cooling_type == 'mixed' else 1.0

            # ── Step 8: Compute confidence ─────────────────────
            confidence = min(1.0, rcp / RCP_STRONG_THRESHOLD)
            confidence *= mixed_penalty

            logger.debug(
                "evaluate_for_station(%s, %s): RCP=%.4f type=%s dir=down conf=%.3f "
                "cloud=%.2f wind=%.2f dry=%.2f night=%.2f snow=%.2f frnt=%s",
                station, date, rcp, cooling_type, confidence,
                cloud_score, wind_score, dryness_score, night_factor, snow_factor,
                frontal_detected
            )

            return 'down', confidence

        except Exception as e:
            logger.error("evaluate_for_station(%s, %s): %s", station, date, str(e))
            return None, 0.0
        finally:
            if own_conn and conn:
                conn.close()

    # ─── Internal Helpers ──────────────────────────────────────

    def _detect_frontal_passage(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection,
    ) -> bool:
        """
        Check if a frontal passage has been detected for the station.

        Uses the existing FrontalPassageIntradaySignal for detection.
        If that signal isn't available, uses a simplified heuristic.

        Args:
            station: ICAO station code
            date: ISO date string
            conn: SQLite connection

        Returns:
            True if frontal passage detected
        """
        # Try to use the existing frontal passage detector
        try:
            from .frontal_passage_intraday_signal import FrontalPassageIntradaySignal
            if self._frontal_detector is None:
                self._frontal_detector = FrontalPassageIntradaySignal(self.db_path)
            direction, confidence = self._frontal_detector.evaluate_for_station(station, date, conn)
            if direction is not None and confidence > 0.65:
                # Only override at 3/3 conditions (confidence 0.80) or
                # 2/3 with very strong signal (confidence 0.65+)
                return True
        except (ImportError, Exception) as e:
            logger.debug("Frontal detector unavailable: %s", e)

        # Fallback: simplified frontal detection from METAR data
        try:
            cur = conn.cursor()
            # Get 24h of observations around the target date
            target_date = datetime.strptime(date, '%Y-%m-%d')
            start_dt = (target_date - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S')
            end_dt = (target_date + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S')

            cur.execute("""
                SELECT timestamp_utc, wind_direction_deg, pressure_mb, temp_f
                FROM metar_observations
                WHERE station = ?
                  AND timestamp_utc >= ?
                  AND timestamp_utc < ?
                  AND wind_direction_deg IS NOT NULL
                  AND pressure_mb IS NOT NULL
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, start_dt, end_dt))

            rows = cur.fetchall()
            if len(rows) < 6:
                return False

            # Check for wind shift > 60° within 3 hours
            # and pressure change > 1.5 mb/3h
            wind_shift_detected = False
            pressure_jump_detected = False

            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    t1 = datetime.fromisoformat(rows[i][0].replace('Z', '+00:00'))
                    t2 = datetime.fromisoformat(rows[j][0].replace('Z', '+00:00'))
                    hours_diff = (t2 - t1).total_seconds() / 3600.0

                    if 1.5 <= hours_diff <= 4.0:
                        wd1, wd2 = rows[i][1], rows[j][1]
                        p1, p2 = rows[i][2], rows[j][2]

                        # Wind shift
                        wd_diff = abs(wd1 - wd2)
                        wd_diff = min(wd_diff, 360 - wd_diff)
                        if wd_diff > 60:
                            wind_shift_detected = True

                        # Pressure change per 3h
                        p_change = abs(p2 - p1) * (3.0 / hours_diff)
                        if p_change > 1.5:
                            pressure_jump_detected = True

            return wind_shift_detected and pressure_jump_detected

        except Exception as e:
            logger.debug("Frontal fallback check failed: %s", e)
            return False

    def _compute_pressure_trend(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection,
    ) -> Optional[float]:
        """
        Compute 24-hour pressure trend (current day pressure minus
        previous day pressure).

        Positive = pressure rising (high pressure building = radiational)
        Negative = pressure falling (trough approaching = advective)

        Args:
            station: ICAO station code
            date: ISO date string
            conn: SQLite connection

        Returns:
            Pressure trend in mb (today avg - yesterday avg), or None
        """
        try:
            cur = conn.cursor()
            target = datetime.strptime(date, '%Y-%m-%d')
            today_start = target.strftime('%Y-%m-%dT00:00:00')
            today_end = target.strftime('%Y-%m-%dT23:59:59')
            prev_day = (target - timedelta(days=1))
            prev_start = prev_day.strftime('%Y-%m-%dT00:00:00')
            prev_end = prev_day.strftime('%Y-%m-%dT23:59:59')

            # Today's avg pressure
            cur.execute("""
                SELECT AVG(pressure_mb)
                FROM metar_observations
                WHERE station = ? AND timestamp_utc >= ? AND timestamp_utc < ?
                  AND pressure_mb IS NOT NULL
            """, (station, today_start, today_end))
            today_p = cur.fetchone()[0]

            # Yesterday's avg pressure
            cur.execute("""
                SELECT AVG(pressure_mb)
                FROM metar_observations
                WHERE station = ? AND timestamp_utc >= ? AND timestamp_utc < ?
                  AND pressure_mb IS NOT NULL
            """, (station, prev_start, prev_end))
            prev_p = cur.fetchone()[0]

            if today_p is not None and prev_p is not None:
                return round(today_p - prev_p, 2)
            return None
        except Exception as e:
            logger.debug("Pressure trend failed: %s", e)
            return None

    def _compute_dewpoint_trend(
        self,
        station: str,
        date: str,
        conn: sqlite3.Connection,
    ) -> Optional[float]:
        """
        Compute 6-hour dewpoint trend going into the evening.

        Args:
            station: ICAO station code
            date: ISO date string
            conn: SQLite connection

        Returns:
            Dewpoint trend in °F (latest - earliest), negative = dropping
        """
        try:
            cur = conn.cursor()
            target = datetime.strptime(date, '%Y-%m-%d')
            evening_window_start = target.strftime('%Y-%m-%dT18:00:00')
            evening_window_end = (target + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00')

            cur.execute("""
                SELECT timestamp_utc, dewpoint_f
                FROM metar_observations
                WHERE station = ?
                  AND timestamp_utc >= ?
                  AND timestamp_utc < ?
                  AND dewpoint_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, evening_window_start, evening_window_end))

            rows = cur.fetchall()
            if len(rows) < 2:
                # Fall back to 24-hour window
                prev_day = (target - timedelta(hours=18))
                start = prev_day.strftime('%Y-%m-%dT00:00:00')
                end = target.strftime('%Y-%m-%dT12:00:00')
                cur.execute("""
                    SELECT timestamp_utc, dewpoint_f
                    FROM metar_observations
                    WHERE station = ?
                      AND timestamp_utc >= ?
                      AND timestamp_utc < ?
                      AND dewpoint_f IS NOT NULL
                    ORDER BY timestamp_utc ASC
                """, (station, start, end))
                rows = cur.fetchall()

            if len(rows) >= 2:
                # Earliest to latest dewpoint change
                first_dp = rows[0][1]
                last_dp = rows[-1][1]
                return round(last_dp - first_dp, 2)
            return None
        except Exception as e:
            logger.debug("Dewpoint trend failed: %s", e)
            return None

    def evaluate_station_rcp(
        self,
        station: str,
        date: str,
        temp_f: float,
        dewpoint_f: float,
        wind_speed_kt: float,
        wind_gust_kt: Optional[float] = None,
        cloud_fraction: float = 0.0,
        conn: sqlite3.Connection = None,
    ) -> Tuple[Optional[str], float]:
        """
        Evaluate radiational cooling from pre-computed inputs.

        This is a convenience method compatible with the existing
        RadiationalCoolingDetector interface from core/radiational_cooling.py.
        Unlike evaluate_for_station(), this takes pre-parsed inputs rather
        than querying the database for raw observations.

        Args:
            station: ICAO station code
            date: ISO date string (YYYY-MM-DD)
            temp_f: Temperature in Fahrenheit
            dewpoint_f: Dewpoint in Fahrenheit
            wind_speed_kt: Sustained wind speed in knots
            wind_gust_kt: Optional gust speed
            cloud_fraction: Cloud fraction [0, 1]
            conn: Optional DB connection (used for pressure/dewpoint trends)

        Returns:
            ('down', confidence) or (None, 0.0)
        """
        station = station.upper()
        latitude = STATION_LATITUDES.get(station, 40.0)
        day_ref = datetime.strptime(date, '%Y-%m-%d')
        day_of_year = day_ref.timetuple().tm_yday
        month = day_ref.month

        # Fog check
        if fog_present(None, temp_f, dewpoint_f):
            return None, 0.0

        # Cloud cover
        clear, cloud_score = is_clear_skies(cloud_fraction)

        # Wind
        calm, wind_score = is_calm_wind(wind_speed_kt, wind_gust_kt)
        if not calm:
            return None, 0.0

        # Dryness
        is_dry, dryness_score = is_dry_enough(temp_f, dewpoint_f)
        if not is_dry:
            return None, 0.0

        # Night length
        night_factor = compute_night_length_factor(latitude, day_of_year)

        # Snow
        snow_factor = compute_snow_factor_simple(temp_f)

        # RCP
        rcp = cloud_score * wind_score * dryness_score * night_factor * snow_factor

        if rcp < RCP_AMBIENT_THRESHOLD:
            return None, 0.0

        # Cooling type classification
        if conn:
            pressure_trend = self._compute_pressure_trend(station, date, conn)
            dewpoint_trend = self._compute_dewpoint_trend(station, date, conn)
            frontal = self._detect_frontal_passage(station, date, conn)
        else:
            pressure_trend = None
            dewpoint_trend = None
            frontal = False

        cooling_type = classify_cooling_type(
            wind_speed_kt=wind_speed_kt,
            wind_gust_kt=wind_gust_kt,
            cloud_fraction=cloud_fraction,
            pressure_trend_24h=pressure_trend,
            dewpoint_trend_6h=dewpoint_trend,
            frontal_detected=frontal,
        )

        if cooling_type == 'advective':
            return None, 0.0

        mixed_penalty = 0.5 if cooling_type == 'mixed' else 1.0
        confidence = min(1.0, rcp / RCP_STRONG_THRESHOLD) * mixed_penalty

        return 'down', confidence

    def evaluate_station_bias_correction(
        self,
        station: str,
        date: str,
        ensemble_low_f: float,
        conn: sqlite3.Connection = None,
    ) -> Dict:
        """
        Full bias-correction evaluation for pipeline integration.

        Provides a complete analysis packet for use in the P3 scheduler
        or LLOP fusion layer:
          - RCP score and components
          - Cooling type classification
          - Bias correction magnitude (ensemble_low - expected drop)
          - Confidence and metadata for alerting

        Args:
            station: ICAO station code
            date: ISO date string
            ensemble_low_f: GEFS ensemble mean LOW forecast (°F)
            conn: Optional DB connection

        Returns:
            Dict with full evaluation result
        """
        dir_result, confidence = self.evaluate_for_station(station, date, conn)

        result = {
            'signal': 'radiational_cooling',
            'station': station,
            'date': date,
            'direction': dir_result,
            'confidence': confidence,
            'fired': dir_result is not None and confidence > 0,
            'bias_correction_f': 0.0,
            'adjusted_low_f': ensemble_low_f,
        }

        if not result['fired']:
            return result

        # Compute expected temperature drop using the existing engine
        try:
            from core.radiational_cooling import RadiationalCoolingDetector
            detector = RadiationalCoolingDetector()
            month = int(date.split('-')[1])

            # Get average nighttime conditions from DB
            if conn or self.db_path:
                local_conn = conn if conn else sqlite3.connect(self.db_path)
                cur = local_conn.cursor()
                night_start = f"{date}T00:00:00"
                night_end = f"{date}T13:00:00"
                cur.execute("""
                    SELECT AVG(temp_f), AVG(dewpoint_f), AVG(wind_speed_kt)
                    FROM metar_observations
                    WHERE station = ? AND timestamp_utc >= ? AND timestamp_utc < ?
                      AND temp_f IS NOT NULL AND temp_f > -100 AND temp_f < 200
                """, (station, night_start, night_end))
                row = cur.fetchone()
                if row and row[0] is not None:
                    t, dp, ws = row
                    rcp_result = detector.evaluate(
                        station=station,
                        date_str=date,
                        temp_f=t,
                        dewpoint_f=dp,
                        wind_speed_kt=ws or 2.0,
                        cloud_fraction=0.0,
                        ensemble_low_temp=ensemble_low_f,
                    )
                    delta_t = rcp_result.get('delta_t_f', 0.0)
                    result['bias_correction_f'] = -delta_t
                    result['adjusted_low_f'] = ensemble_low_f - delta_t
                    result['rcp_score'] = rcp_result.get('rcp_score')
                    result['components'] = rcp_result.get('components')
                if not conn:
                    local_conn.close()
        except Exception as e:
            logger.error("Bias correction failed: %s", e)

        return result