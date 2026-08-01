"""
Radiational Cooling Detection Signal (FP 6.6)

Ensemble bias correction for LOW temperature markets during radiational cooling nights.

Detects nights with strong radiational cooling conditions from METAR observations
and produces a bias-corrected LOW estimate that undercuts the ensemble consensus.

Key formula:
    ΔT_rad = RCP × BasePotential(station, month) × SeasonalMultiplier × SnowFactor
    LOW_adj = LOW_ensemble - ΔT_rad

RCP = CloudScore × WindScore × DrynessScore × NightLengthFactor × SnowFactor

Usage:
    from core.radiational_cooling import RadiationalCoolingDetector
    detector = RadiationalCoolingDetector()
    result = detector.evaluate(station="KMSP", date="2026-01-15",
                                temp_f=25.0, dewpoint_f=5.0,
                                wind_speed_kt=2.0, cloud_fraction=0.0)

B-Mode R8 Cycle 4.5: Implementation of FP 6.6 spec.
"""

import math
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, date, timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Station Base Potential (FP 6.6 Section 4.2)
# ─────────────────────────────────────────────────────────────────────

STATION_POTENTIAL: Dict[str, Dict[str, Any]] = {
    'KMSP': {'base_potential': 7.0, 'snow_bonus': 3.0, 'year_round': False, 'notes': 'Minneapolis - strongest effect'},
    'KDEN': {'base_potential': 6.0, 'snow_bonus': 2.0, 'year_round': False, 'notes': 'Denver - high elevation, dry air'},
    'KMDW': {'base_potential': 5.0, 'snow_bonus': 2.0, 'year_round': False, 'notes': 'Chicago - winter only'},
    'KBOS': {'base_potential': 5.0, 'snow_bonus': 2.0, 'year_round': False, 'notes': 'Boston - winter/coastal'},
    'KNYC': {'base_potential': 4.0, 'snow_bonus': 1.0, 'year_round': False, 'notes': 'New York - urban heat island'},
    'KPHL': {'base_potential': 4.0, 'snow_bonus': 1.0, 'year_round': False, 'notes': 'Philadelphia'},
    'KDCA': {'base_potential': 4.0, 'snow_bonus': 1.0, 'year_round': False, 'notes': 'Washington DC'},
    'KORD': {'base_potential': 5.0, 'snow_bonus': 2.0, 'year_round': False, 'notes': 'Chicago O\'Hare'},
    'KATL': {'base_potential': 3.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Atlanta - marginal'},
    'KDFW': {'base_potential': 3.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Dallas - marginal'},
    'KSEA': {'base_potential': 2.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Seattle - maritime climate'},
    'KSFO': {'base_potential': 2.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'San Francisco - maritime'},
    'KLAX': {'base_potential': 1.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Los Angeles - weak'},
    'KPHX': {'base_potential': 2.0, 'snow_bonus': 0.0, 'year_round': True, 'notes': 'Phoenix - dry but warm'},
    'KMIA': {'base_potential': 0.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Miami - tropical, no effect'},
    'KHOU': {'base_potential': 1.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Houston - humid'},
    'KMSY': {'base_potential': 0.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'New Orleans - humid'},
    'KOKC': {'base_potential': 4.0, 'snow_bonus': 1.0, 'year_round': False, 'notes': 'Oklahoma City'},
    'KSAT': {'base_potential': 2.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'San Antonio'},
    'KAUS': {'base_potential': 2.0, 'snow_bonus': 0.0, 'year_round': False, 'notes': 'Austin'},
    'KLAS': {'base_potential': 3.0, 'snow_bonus': 0.0, 'year_round': True, 'notes': 'Las Vegas - dry desert'},
}

# Stations where snow factor is never applied
NO_SNOW_STATIONS = {'KPHX', 'KMIA', 'KLAX', 'KSFO', 'KHOU', 'KMSY',
                    'KATL', 'KAUS', 'KSAT', 'KDFW', 'KOKC', 'KPHL', 'KDCA'}

# Cloud cover sky code -> cloud fraction (FP 6.6 Section 3.2)
SKY_CODE_TO_FRACTION = {
    0: 0.00,   # CLR
    2: 0.125,  # FEW
    4: 0.375,  # SCT
    6: 0.625,  # BKN
    7: 0.75,   # BKN (some formats)
    8: 1.00,   # OVC
    9: 0.50,   # Missing/unknown — assume scattered
}

# Seasonal multipliers by month (FP 6.6 Section 4.3)
SEASONAL_MULTIPLIERS = {
    1: 1.0, 2: 1.0, 3: 0.9, 4: 0.75,
    5: 0.6, 6: 0.5, 7: 0.5, 8: 0.6,
    9: 0.75, 10: 0.9, 11: 1.0, 12: 1.0,
}


def compute_cloud_score(cloud_fraction: float) -> float:
    """
    Compute CloudScore from weighted cloud fraction (FP 6.6 Section 3.2).

    Args:
        cloud_fraction: Evening weighted cloud fraction [0, 1]

    Returns:
        CloudScore [0, 1] where 1 = clear, 0 = overcast
    """
    return max(0.0, 1.0 - cloud_fraction)


def compute_wind_score(wind_speed_kt: float, wind_gust_kt: Optional[float] = None) -> float:
    """
    Compute WindScore from sustained wind (FP 6.6 Section 3.3).

    Args:
        wind_speed_kt: Sustained wind speed in knots
        wind_gust_kt: Optional gust speed in knots

    Returns:
        WindScore [0, 1] where 1 = calm, 0 = disrupted
    """
    calm_threshold = 3.0
    moderate_threshold = 8.0

    if wind_speed_kt <= calm_threshold:
        score = 1.0
    elif wind_speed_kt >= moderate_threshold:
        score = 0.0
    else:
        score = 1.0 - (wind_speed_kt - calm_threshold) / (moderate_threshold - calm_threshold)

    # Gust penalty
    if wind_gust_kt is not None and wind_speed_kt > 0:
        gust_ratio = wind_gust_kt / wind_speed_kt
        if gust_ratio > 2.0 and wind_gust_kt >= 10.0:
            score *= 0.5

    return max(0.0, score)


def compute_dryness_score(temp_f: float, dewpoint_f: float) -> float:
    """
    Compute DrynessScore from dewpoint depression (FP 6.6 Section 3.4).

    Args:
        temp_f: Temperature in Fahrenheit
        dewpoint_f: Dewpoint in Fahrenheit

    Returns:
        DrynessScore [0, 1] where 1 = desert dry
    """
    dpd = temp_f - dewpoint_f

    # Adjust effective DPD for very dry airmasses
    if dewpoint_f < 15.0:
        dpd_effective = max(dpd, 20.0)
    elif dewpoint_f < 25.0:
        dpd_effective = max(dpd, 15.0)
    else:
        dpd_effective = dpd

    return min(1.0, dpd_effective / 25.0)


def compute_night_length_factor(latitude: float, day_of_year: int) -> float:
    """
    Compute NightLengthFactor from latitude and day of year (FP 6.6 Section 3.5).

    Simplified model: night length proportional to cos(latitude) and day of year.

    Args:
        latitude: Station latitude in degrees
        day_of_year: Day of year (1-366)

    Returns:
        NightLengthFactor [0.87, 1.10]
    """
    # Solar declination approximation
    declination = 23.44 * math.cos(math.radians(360.0 * (day_of_year - 172) / 365.0))

    # Day length at given latitude (hours)
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    cos_hour_angle = -math.tan(lat_rad) * math.tan(dec_rad)
    cos_hour_angle = max(-1.0, min(1.0, cos_hour_angle))

    day_length_hours = math.degrees(math.acos(cos_hour_angle)) / 15.0
    night_length_hours = 24.0 - day_length_hours

    # Normalized: winter solstice ~15h -> 1.0, summer solstice ~9h -> 0.87
    factor = min(1.10, math.sqrt(night_length_hours / 12.0))
    return factor


def compute_snow_factor(station: str, temp_f: float) -> float:
    """
    Compute SnowFactor for radiational cooling amplification (FP 6.6 Section 3.6).

    Simplified: check if station is cold enough for snow cover potential.

    Args:
        station: ICAO station code
        temp_f: Current temperature

    Returns:
        SnowFactor [1.0, 1.3]
    """
    if station in NO_SNOW_STATIONS:
        return 1.0
    # Simple heuristic: snow possible if temp < 32°F
    if temp_f < 32.0:
        return 1.30
    return 1.00


def compute_rcp_score(
    station: str,
    temp_f: float,
    dewpoint_f: float,
    wind_speed_kt: float,
    wind_gust_kt: Optional[float] = None,
    cloud_fraction: float = 0.0,
    latitude: float = 40.0,
    day_of_year: Optional[int] = None,
) -> float:
    """
    Compute Radiational Cooling Potential (RCP) score (FP 6.6 Section 3.7).

    RCP = CloudScore × WindScore × DrynessScore × NightLengthFactor × SnowFactor

    Args:
        station: ICAO station code
        temp_f: Temperature in Fahrenheit
        dewpoint_f: Dewpoint in Fahrenheit
        wind_speed_kt: Sustained wind speed in knots
        wind_gust_kt: Optional gust speed
        cloud_fraction: Evening cloud fraction [0, 1]
        latitude: Station latitude in degrees
        day_of_year: Day of year (default: today)

    Returns:
        RCP score [0, 1]
    """
    if day_of_year is None:
        day_of_year = datetime.now(timezone.utc).timetuple().tm_yday

    cloud_score = compute_cloud_score(cloud_fraction)
    wind_score = compute_wind_score(wind_speed_kt, wind_gust_kt)
    dryness_score = compute_dryness_score(temp_f, dewpoint_f)
    night_length = compute_night_length_factor(latitude, day_of_year)
    snow_factor = compute_snow_factor(station, temp_f)

    rcp = cloud_score * wind_score * dryness_score * night_length * snow_factor

    logger.debug(
        "RCP=%.4f station=%s cloud=%.2f wind=%.2f dry=%.2f night=%.2f snow=%.2f",
        rcp, station, cloud_score, wind_score, dryness_score, night_length, snow_factor
    )

    return min(1.0, rcp)


def get_seasonal_multiplier(month: int) -> float:
    """Get seasonal multiplier for a given month (FP 6.6 Section 4.3)."""
    return SEASONAL_MULTIPLIERS.get(month, 0.75)


def compute_delta_temperature(
    station: str,
    rcp: float,
    month: int,
    temp_f: float,
) -> float:
    """
    Compute expected temperature drop ΔT_rad (FP 6.6 Section 5.2).

    ΔT_rad = RCP × BasePotential × SeasonalMultiplier × SnowFactor

    Args:
        station: ICAO station code
        rcp: Radiational Cooling Potential score [0, 1]
        month: Month (1-12)
        temp_f: Current temperature for snow check

    Returns:
        Expected temperature drop in °F (always positive or zero)
    """
    station_data = STATION_POTENTIAL.get(station, {'base_potential': 1.0, 'snow_bonus': 0.0})
    base_potential = station_data['base_potential']

    seasonal_mult = get_seasonal_multiplier(month)
    snow_factor = compute_snow_factor(station, temp_f)

    # Base potential includes snow bonus when applicable
    snow_bonus = station_data.get('snow_bonus', 0.0)
    if snow_factor > 1.0 and station not in NO_SNOW_STATIONS:
        effective_potential = base_potential + snow_bonus
    else:
        effective_potential = base_potential

    delta_t = rcp * effective_potential * seasonal_mult * (1.0 if snow_factor == 1.0 else snow_factor)

    return max(0.0, delta_t)


def compute_correction_confidence(rcp: float) -> float:
    """
    Compute confidence in the radiational cooling correction (FP 6.6 Section 5.3).

    Args:
        rcp: Radiational Cooling Potential score [0, 1]

    Returns:
        Correction confidence [0, 1]
    """
    if rcp < 0.20:
        return 0.0
    return min(1.0, rcp / 0.60)


def classify_rcp(rcp: float) -> str:
    """Classify RCP score into an action category (FP 6.6 Section 3.7)."""
    if rcp >= 0.80:
        return "strong"
    elif rcp >= 0.60:
        return "moderate"
    elif rcp >= 0.40:
        return "weak"
    elif rcp >= 0.20:
        return "ambient"
    else:
        return "suppressed"


class RadiationalCoolingDetector:
    """
    Detects radiational cooling conditions and produces bias-corrected LOW estimates.

    Usage:
        detector = RadiationalCoolingDetector()
        result = detector.evaluate(
            station="KMSP", date="2026-01-15",
            temp_f=25.0, dewpoint_f=5.0,
            wind_speed_kt=2.0, cloud_fraction=0.0
        )
    """

    def __init__(self, station_latitudes: Optional[Dict[str, float]] = None):
        """
        Args:
            station_latitudes: Dict of station -> latitude. Internal defaults if None.
        """
        self.latitudes = station_latitudes or {
            'KMSP': 44.88, 'KDEN': 39.85, 'KMDW': 41.79, 'KBOS': 42.36,
            'KNYC': 40.78, 'KPHL': 39.87, 'KDCA': 38.85, 'KORD': 41.98,
            'KATL': 33.64, 'KDFW': 32.90, 'KSEA': 47.45, 'KSFO': 37.62,
            'KLAX': 33.94, 'KPHX': 33.43, 'KMIA': 25.79, 'KHOU': 29.65,
            'KMSY': 29.99, 'KOKC': 35.39, 'KSAT': 29.53, 'KAUS': 30.19,
            'KLAS': 36.08,
        }

    def evaluate(
        self,
        station: str,
        date_str: str,
        temp_f: float,
        dewpoint_f: float,
        wind_speed_kt: float,
        wind_gust_kt: Optional[float] = None,
        cloud_fraction: float = 0.0,
        ensemble_low_temp: Optional[float] = None,
        ensemble_spread: Optional[float] = None,
        ensemble_prob_low: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate radiational cooling conditions for a station.

        Args:
            station: ICAO station code
            date_str: Date string (YYYY-MM-DD)
            temp_f: Current temperature in Fahrenheit
            dewpoint_f: Dewpoint in Fahrenheit
            wind_speed_kt: Sustained wind speed in knots
            wind_gust_kt: Optional gust speed
            cloud_fraction: Evening cloud fraction [0, 1]
            ensemble_low_temp: Ensemble forecasted LOW temp (optional)
            ensemble_spread: Ensemble standard deviation (optional)
            ensemble_prob_low: Ensemble probability of LOW below threshold (optional)

        Returns:
            Dict with rcp_score, delta_t, correction_confidence, classification,
            adjusted_low, correction_applied, etc.
        """
        month = int(date_str.split('-')[1]) if '-' in date_str else 1
        day_ref = datetime.strptime(date_str, '%Y-%m-%d') if '-' in date_str else datetime.now(timezone.utc)
        day_of_year = day_ref.timetuple().tm_yday

        latitude = self.latitudes.get(station, 40.0)

        rcp = compute_rcp_score(
            station=station,
            temp_f=temp_f,
            dewpoint_f=dewpoint_f,
            wind_speed_kt=wind_speed_kt,
            wind_gust_kt=wind_gust_kt,
            cloud_fraction=cloud_fraction,
            latitude=latitude,
            day_of_year=day_of_year,
        )

        delta_t = compute_delta_temperature(station, rcp, month, temp_f)
        confidence = compute_correction_confidence(rcp)
        classification = classify_rcp(rcp)

        result = {
            'station': station,
            'date': date_str,
            'rcp_score': round(rcp, 4),
            'delta_t_f': round(delta_t, 2),
            'correction_confidence': round(confidence, 4),
            'classification': classification,
            'correction_applied': rcp >= 0.40,
            'components': {
                'cloud_score': round(compute_cloud_score(cloud_fraction), 4),
                'wind_score': round(compute_wind_score(wind_speed_kt, wind_gust_kt), 4),
                'dryness_score': round(compute_dryness_score(temp_f, dewpoint_f), 4),
                'night_length_factor': round(compute_night_length_factor(latitude, day_of_year), 4),
                'snow_factor': round(compute_snow_factor(station, temp_f), 4),
            },
            'input_parameters': {
                'temp_f': temp_f,
                'dewpoint_f': dewpoint_f,
                'wind_speed_kt': wind_speed_kt,
                'cloud_fraction': cloud_fraction,
                'latitude': latitude,
            },
        }

        # Compute adjusted LOW if ensemble data provided
        if ensemble_low_temp is not None and delta_t > 0:
            result['ensemble_low_temp'] = ensemble_low_temp
            result['adjusted_low_temp'] = round(ensemble_low_temp - delta_t, 2)
        else:
            result['adjusted_low_temp'] = None

        # Compute adjusted probability if ensemble spread and prob provided
        if (ensemble_prob_low is not None and ensemble_spread is not None
                and ensemble_spread > 0 and delta_t > 0):
            shift_std = delta_t / ensemble_spread
            # Approximate probability adjustment: prob_adj = prob + shift * pdf
            pdf_at_threshold = 0.3989 / ensemble_spread  # Normal approx at mean
            prob_adjusted = ensemble_prob_low + shift_std * pdf_at_threshold
            result['ensemble_prob_low'] = ensemble_prob_low
            result['adjusted_prob_low'] = round(min(1.0, max(0.0, prob_adjusted)), 4)

        return result


# Legacy compatibility
def detect_radiational_cooling(station: str, temp_f: float, dewpoint_f: float,
                                wind_speed_kt: float, **kwargs) -> Dict[str, Any]:
    """Legacy: single-call detection."""
    detector = RadiationalCoolingDetector()
    return detector.evaluate(station, datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                             temp_f, dewpoint_f, wind_speed_kt, **kwargs)