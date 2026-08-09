"""
Spatial Coherence Verification System — Full Implementation

Extends FP-SPATIAL-COHERENCE.md (FP 6.3) with detailed station clustering,
distance decay (angular + altitude), minimum confirming stations, coastal/inland
handling, prevailing wind direction, and upstream weighting.

Designed per: docs/plans/SPATIAL-COHERENCE-SPEC.md

Key methods:
    get_cluster(station)                     — returns cluster/region ID
    get_cluster_confirmation(...)             — evaluates how well cluster confirms a signal
    modulate_confidence(...)                  — applies spatial coherence modulation to confidence
    compute_modulation(station_forecasts)     — batch computation (existing FP 6.3 interface)
    apply_modulation(station_forecasts)       — batch apply (existing FP 6.3 interface)

Usage:
    from core.spatial_coherence import SpatialCoherenceGate
    gate = SpatialCoherenceGate()
    adj_conf = gate.modulate_confidence("KDFW", "gaussian", "up", 0.72, nearby_signals)
"""

import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any, Union

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 1. REGION / CLUSTER DEFINITIONS  (SPATIAL-COHERENCE-SPEC §2)
# ═══════════════════════════════════════════════════════════════════════

REGIONS = {
    'NE': {  # Northeast — humid subtropical / oceanic
        'stations': ['KNYC', 'KBOS', 'KPHL', 'KDCA'],
        'L': 300,          # decorrelation length (km)
        'M_min': 0.50,
        'M_max': 1.25,
        'cluster_kind': 'dense',   # ≥4 stations → min_ct=2
    },
    'SE': {  # Southeast — humid subtropical / tropical monsoon
        'stations': ['KATL', 'KMIA', 'KMSY'],
        'L': 500,
        'M_min': 0.60,
        'M_max': 1.15,
        'cluster_kind': 'dense',   # 3 stations
    },
    'SC': {  # South Central — humid subtropical (hub cluster)
        'stations': ['KHOU', 'KDFW', 'KAUS', 'KSAT', 'KOKC'],
        'L': 350,
        'M_min': 0.50,
        'M_max': 1.30,
        'cluster_kind': 'dense',
    },
    'MW': {  # Midwest — humid continental
        'stations': ['KMDW', 'KMSP'],
        'L': 350,
        'M_min': 0.50,
        'M_max': 1.30,
        'cluster_kind': 'sparse',  # only 2 stations → min_ct=1
    },
    'RW': {  # Rocky West — semi-arid / desert
        'stations': ['KDEN', 'KPHX', 'KLAS', 'KOKC'],
        'L': 600,
        'M_min': 0.70,
        'M_max': 1.10,
        'cluster_kind': 'sparse',
    },
    'PAC': {  # Pacific — Mediterranean / oceanic
        'stations': ['KSEA', 'KSFO', 'KLAX'],
        'L': 400,
        'M_min': 0.60,
        'M_max': 1.20,
        'cluster_kind': 'sparse',
    },
}

# Station coordinates, elevation, and coastal classification
# Source: docs/plans/SPATIAL-COHERENCE-SPEC.md §2.5, §5.1
STATION_METADATA: Dict[str, Dict[str, Any]] = {
    'KNYC': {'lat': 40.78, 'lon': -73.97, 'elev_ft': 42,  'region': 'NE', 'coast_dist_km': 12,  'coast_type': 'Atlantic',    'marine': 'moderate'},
    'KBOS': {'lat': 42.36, 'lon': -71.01, 'elev_ft': 20,  'region': 'NE', 'coast_dist_km': 8,   'coast_type': 'Atlantic',    'marine': 'strong'},
    'KPHL': {'lat': 39.87, 'lon': -75.23, 'elev_ft': 30,  'region': 'NE', 'coast_dist_km': 130, 'coast_type': 'Delaware R.', 'marine': 'weak'},
    'KDCA': {'lat': 38.85, 'lon': -77.04, 'elev_ft': 15,  'region': 'NE', 'coast_dist_km': 180, 'coast_type': 'Potomac R.',   'marine': 'weak'},
    'KATL': {'lat': 33.64, 'lon': -84.43, 'elev_ft': 1026,'region': 'SE', 'coast_dist_km': 350, 'coast_type': 'inland',      'marine': 'none'},
    'KMIA': {'lat': 25.79, 'lon': -80.29, 'elev_ft': 8,   'region': 'SE', 'coast_dist_km': 5,   'coast_type': 'Atlantic+Gulf','marine': 'very_strong'},
    'KMSY': {'lat': 29.99, 'lon': -90.25, 'elev_ft': 3,   'region': 'SE', 'coast_dist_km': 30,  'coast_type': 'Gulf',        'marine': 'strong'},
    'KHOU': {'lat': 29.65, 'lon': -95.28, 'elev_ft': 72,  'region': 'SC', 'coast_dist_km': 50,  'coast_type': 'Gulf',        'marine': 'strong'},
    'KDFW': {'lat': 32.90, 'lon': -97.04, 'elev_ft': 607, 'region': 'SC', 'coast_dist_km': 400, 'coast_type': 'inland',      'marine': 'none'},
    'KAUS': {'lat': 30.19, 'lon': -97.67, 'elev_ft': 542, 'region': 'SC', 'coast_dist_km': 350, 'coast_type': 'inland',      'marine': 'none'},
    'KSAT': {'lat': 29.53, 'lon': -98.47, 'elev_ft': 810, 'region': 'SC', 'coast_dist_km': 300, 'coast_type': 'inland',      'marine': 'none'},
    'KOKC': {'lat': 35.39, 'lon': -97.60, 'elev_ft': 1301,'region': 'SC', 'coast_dist_km': 600, 'coast_type': 'inland',      'marine': 'none',
             'secondary_region': 'RW', 'primary_weight': 0.65},
    'KMDW': {'lat': 41.79, 'lon': -87.75, 'elev_ft': 620, 'region': 'MW', 'coast_dist_km': 500, 'coast_type': 'inland',      'marine': 'none'},
    'KMSP': {'lat': 44.88, 'lon': -93.22, 'elev_ft': 841, 'region': 'MW', 'coast_dist_km': 700, 'coast_type': 'inland',      'marine': 'none'},
    'KDEN': {'lat': 39.86, 'lon': -104.67,'elev_ft': 5431,'region': 'RW', 'coast_dist_km': 1200,'coast_type': 'inland',      'marine': 'none'},
    'KPHX': {'lat': 33.43, 'lon': -112.02,'elev_ft': 1135,'region': 'RW', 'coast_dist_km': 300, 'coast_type': 'inland',      'marine': 'none'},
    'KLAS': {'lat': 36.08, 'lon': -115.17,'elev_ft': 2181,'region': 'RW', 'coast_dist_km': 400, 'coast_type': 'inland',      'marine': 'none'},
    'KSEA': {'lat': 47.45, 'lon': -122.31,'elev_ft': 433, 'region': 'PAC','coast_dist_km': 15,  'coast_type': 'Puget Sound', 'marine': 'strong'},
    'KSFO': {'lat': 37.62, 'lon': -122.37,'elev_ft': 13,  'region': 'PAC','coast_dist_km': 5,   'coast_type': 'Pacific',     'marine': 'very_strong'},
    'KLAX': {'lat': 33.94, 'lon': -118.41,'elev_ft': 128, 'region': 'PAC','coast_dist_km': 20,  'coast_type': 'Pacific',     'marine': 'strong'},
}

# Convenience coordinate lookup (backward compat)
STATION_COORDS: Dict[str, Tuple[float, float]] = {
    s: (m['lat'], m['lon']) for s, m in STATION_METADATA.items()
}

# Overlap stations (KOKC has dual SC/RW membership)
OVERLAP_STATIONS: Dict[str, Dict[str, Any]] = {
    'KOKC': {'primary': 'SC', 'primary_weight': 0.65,
             'secondary': 'RW', 'secondary_weight': 0.35},
}

# ═══════════════════════════════════════════════════════════════════════
# 2. MINIMUM CONFIRMING STATIONS  (SPATIAL-COHERENCE-SPEC §4)
# ═══════════════════════════════════════════════════════════════════════

# Tiered thresholds per spec §4.1
#   dense clusters (≥3 stations): min_ct ≥2, higher min_wsum
#   sparse clusters (≤3 stations): min_ct = 1, lower min_wsum
MIN_CONFIRMING = {
    'NE':  {'min_ct': 2, 'min_wsum': 0.8},
    'SE':  {'min_ct': 2, 'min_wsum': 0.6},
    'SC':  {'min_ct': 3, 'min_wsum': 1.2},
    'MW':  {'min_ct': 1, 'min_wsum': 0.3},
    'RW':  {'min_ct': 1, 'min_wsum': 0.4},
    'PAC': {'min_ct': 1, 'min_wsum': 0.4},
}

REGION_BOUNDS = {
    'NE':  (0.50, 1.25),
    'SE':  (0.60, 1.15),
    'SC':  (0.50, 1.30),
    'MW':  (0.50, 1.30),
    'RW':  (0.70, 1.10),
    'PAC': (0.60, 1.20),
}

# ═══════════════════════════════════════════════════════════════════════
# 3. SEASONAL FACTORS  (SPATIAL-COHERENCE-SPEC §3.3)
# ═══════════════════════════════════════════════════════════════════════

SEASONAL_L_FACTOR = {
    'winter': 0.85,
    'spring': 1.00,
    'summer': 1.15,
    'fall':   1.00,
}

# ═══════════════════════════════════════════════════════════════════════
# 4. COASTAL HANDLING  (SPATIAL-COHERENCE-SPEC §5)
# ═══════════════════════════════════════════════════════════════════════

# Coastal stations with strong marine influence
COASTAL_STATIONS = {'KBOS', 'KNYC', 'KMIA', 'KMSY', 'KHOU', 'KSEA', 'KSFO', 'KLAX'}

# Onshore wind directions per coastal station (bearing from water → land)
# Rough center bearing of coastline facing direction
COASTAL_ONSHORE_DIRECTIONS = {
    'KBOS': (70, 120),    # Atlantic onshore from east (coast faces east)
    'KNYC': (130, 200),   # Atlantic onshore from southeast/south (coast faces south)
    'KMIA': (80, 130),    # Atlantic from east (coast faces east)
    'KMSY': (150, 210),   # Gulf from south (coast faces south)
    'KHOU': (140, 200),   # Gulf from south/southeast (coast faces southeast)
    'KSEA': (220, 300),   # Pacific from west/southwest (Puget Sound inlet)
    'KSFO': (240, 300),   # Pacific from west/northwest (Golden Gate)
    'KLAX': (220, 290),   # Pacific from west/southwest (coast faces southwest)
}

# Coastal penalty map (§5.4): weight reduction when coastal & inland interact
COASTAL_PENALTY = {
    'inland': {'coastal': 0.5},      # Inland target: coastal neighbors at half weight
    'coastal': {'coastal': 0.8, 'inland': 1.0},  # Coastal target: near-full for others
}

# ═══════════════════════════════════════════════════════════════════════
# 5. HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════


def great_circle_distance(lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
    """Great-circle distance in km (Haversine)."""
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def compute_bearing(lat1: float, lon1: float,
                    lat2: float, lon2: float) -> float:
    """Forward azimuth from point 1 to point 2 in degrees [0, 360)."""
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(d_lon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def get_season(date_str: str) -> str:
    """Determine season from a date string (YYYY-MM-DD)."""
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except ValueError:
        return 'fall'  # safe default
    m = dt.month
    if 3 <= m <= 5:
        return 'spring'
    elif 6 <= m <= 8:
        return 'summer'
    elif 9 <= m <= 11:
        return 'fall'
    else:
        return 'winter'


def get_decorrelation_length(r1: str, r2: str, season: str = 'fall') -> float:
    """Decorrelation length L for a pair of regions, adjusted for season."""
    base_l = REGIONS.get(r1, REGIONS['SC'])['L']
    if r1 != r2:
        base_l = min(base_l, REGIONS.get(r2, REGIONS['SC'])['L'])
    factor = SEASONAL_L_FACTOR.get(season, 1.0)
    return base_l * factor


def compute_prevailing_wind(
    metar_obs: List[Dict[str, Any]]
) -> Optional[float]:
    """
    Vector-average wind direction from recent METAR observations.

    Args:
        metar_obs: List of dicts with 'wind_direction_deg' and 'wind_speed_kt'.
                   Should be most-recent-first or all recent.

    Returns:
        Mean wind direction in degrees (FROM), or None if insufficient data.
    """
    recent = metar_obs[-12:] if len(metar_obs) > 12 else metar_obs
    u_sum = v_sum = 0.0
    count = 0
    for obs in recent:
        wd = obs.get('wind_direction_deg')
        ws = obs.get('wind_speed_kt')
        if wd is None or ws is None:
            continue
        wd_rad = math.radians(float(wd))
        ws_val = float(ws)
        u_sum += ws_val * math.sin(wd_rad)
        v_sum += ws_val * math.cos(wd_rad)
        count += 1
    if count < 3:
        return None
    u_avg = u_sum / count
    v_avg = v_sum / count
    if abs(u_avg) < 0.1 and abs(v_avg) < 0.1:
        return None  # calm
    return math.degrees(math.atan2(u_avg, v_avg)) % 360


def detect_sea_breeze(
    station: str,
    metar_obs: List[Dict[str, Any]],
) -> bool:
    """
    Detect sea breeze transition: wind shift from offshore → onshore
    in the last 6 observations (≈3 hours).

    Args:
        station: ICAO code
        metar_obs: Recent METAR observations (most-recent-first)

    Returns:
        True if sea breeze pattern detected
    """
    if station not in COASTAL_STATIONS:
        return False
    if len(metar_obs) < 3:
        return False

    onshore_range = COASTAL_ONSHORE_DIRECTIONS.get(station, (0, 360))
    onshore_start, onshore_end = onshore_range
    offshore_start = (onshore_end + 10) % 360
    offshore_end = (onshore_start - 10) % 360

    def is_onshore(d):
        if onshore_start <= onshore_end:
            return onshore_start <= d <= onshore_end
        return d >= onshore_start or d <= onshore_end

    def is_offshore(d):
        if offshore_start <= offshore_end:
            return offshore_start <= d <= offshore_end
        return d >= offshore_start or d <= offshore_end

    # Check older observations (first half) for offshore, recent (last 2) for onshore
    split = max(1, len(metar_obs) - 2)
    prev_dirs = [o.get('wind_direction_deg') for o in metar_obs[:split]
                 if o.get('wind_direction_deg') is not None]
    curr_dirs = [o.get('wind_direction_deg') for o in metar_obs[split:]
                 if o.get('wind_direction_deg') is not None]

    if not prev_dirs or not curr_dirs:
        return False

    had_offshore = any(is_offshore(d) for d in prev_dirs)
    has_onshore = any(is_onshore(d) for d in curr_dirs)
    return had_offshore and has_onshore


def is_coastal(station: str) -> bool:
    """Return True if station has significant marine influence."""
    m = STATION_METADATA.get(station, {})
    marine = m.get('marine', 'none')
    return marine in ('strong', 'very_strong')


def is_upstream(target_station: str, candidate_station: str,
                wind_direction: Optional[float]) -> bool:
    """
    Determine if candidate is upstream (windward) of target.

    Wind direction = FROM direction. Candidate is upstream if it lies
    within ±90° of the upwind direction from target.
    """
    if wind_direction is None:
        return True
    t_meta = STATION_METADATA.get(target_station)
    c_meta = STATION_METADATA.get(candidate_station)
    if not t_meta or not c_meta:
        return True
    bearing = compute_bearing(
        t_meta['lat'], t_meta['lon'],
        c_meta['lat'], c_meta['lon']
    )
    # Wind is FROM direction_d. The direction air is moving TOWARD:
    wind_to = (wind_direction + 180) % 360
    angle_diff = abs(bearing - wind_to)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff
    return angle_diff <= 90


def classify_direction(anomaly: float) -> int:
    """Classify anomaly as cooler (-1), neutral (0), or warmer (+1)."""
    if abs(anomaly) < 0.5:
        return 0
    return 1 if anomaly > 0 else -1


def is_confirming(target_dir: int, neighbor_dir: int,
                  target_anomaly: float, neighbor_anomaly: float,
                  max_anomaly_diff: float = 6.0) -> bool:
    """
    Check if a neighbor confirms the target's signal (§4.2).

    Confirmation requires:
      1. Same direction (warmer/cooler)
      2. Anomaly magnitudes not wildly different (≤6°C difference)
    """
    if target_dir != neighbor_dir:
        return False
    if target_dir == 0:  # neutral — no meaningful confirmation
        return False
    if abs(abs(target_anomaly) - abs(neighbor_anomaly)) > max_anomaly_diff:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════
# 6. DISTANCE WEIGHTING  (SPATIAL-COHERENCE-SPEC §3)
# ═══════════════════════════════════════════════════════════════════════


def compute_spatial_weight(
    target_station: str,
    candidate_station: str,
    wind_direction: Optional[float] = None,
    season: str = 'fall',
    coastal_adjust: bool = True,
    upstream_adjust: bool = True,
) -> float:
    """
    Compute composite spatial weight from target to candidate (§3.2).

    Components:
      1. Gaussian distance decay w_distance = exp(-d²/2L²)
      2. Angular enhancement (upwind/downwind)
      3. Altitude penalty
      4. Coastal penalty (if applicable)
      5. Upstream weighting

    Returns:
        Composite weight in [0.01, 1.0]
    """
    t_meta = STATION_METADATA.get(target_station)
    c_meta = STATION_METADATA.get(candidate_station)
    if not t_meta or not c_meta:
        return 0.01

    # 1. Great-circle distance
    d_km = great_circle_distance(
        t_meta['lat'], t_meta['lon'], c_meta['lat'], c_meta['lon']
    )
    if d_km < 1:
        return 1.0

    # 2. Decorrelation length
    t_region = t_meta.get('region', 'SC')
    c_region = c_meta.get('region', 'SC')
    L = get_decorrelation_length(t_region, c_region, season)
    w_distance = math.exp(-(d_km ** 2) / (2.0 * L * L))

    # Minimum flooring to prevent zero weights
    w = w_distance

    # 3. Angular enhancement based on wind direction (§3.2 step 3)
    if wind_direction is not None:
        bearing = compute_bearing(
            t_meta['lat'], t_meta['lon'],
            c_meta['lat'], c_meta['lon']
        )
        angle_diff = abs(bearing - wind_direction)
        if angle_diff > 180:
            angle_diff = 360 - angle_diff
        if angle_diff <= 60:
            w_angular = 1.0      # Upwind — full weight
        elif angle_diff <= 120:
            w_angular = 0.7      # Cross-wind — reduced
        else:
            w_angular = 0.5      # Downwind — half
        w *= w_angular

    # 4. Altitude penalty (§3.2 step 4)
    elev_diff = abs(t_meta.get('elev_ft', 0) - c_meta.get('elev_ft', 0))
    if elev_diff > 2000:
        w_altitude = 0.3
    elif elev_diff > 1000:
        w_altitude = 0.6
    elif elev_diff > 500:
        w_altitude = 0.8
    else:
        w_altitude = 1.0
    w *= w_altitude

    # 5. Coastal penalty (§5.4)
    if coastal_adjust:
        t_marine = t_meta.get('marine', 'none')
        c_marine = c_meta.get('marine', 'none')
        t_inland = t_marine == 'none'
        c_coastal = c_marine in ('strong', 'very_strong')
        if t_inland and c_coastal:
            w *= 0.5
        elif not t_inland and c_coastal:
            w *= 0.8
        # inland-inland and coastal-inland: no penalty

    # 6. Upstream weighting (§6)
    if upstream_adjust and wind_direction is not None:
        upstream_weight = _compute_upstream_factor(
            target_station, candidate_station, wind_direction
        )
        w *= upstream_weight

    return max(w, 0.01)


def _compute_upstream_factor(
    target_station: str,
    candidate_station: str,
    wind_direction: float,
) -> float:
    """
    Compute upstream weight factor for downstream stations (§6.2).

    Returns factor in [0.25, 1.0].
    Wind direction = FROM. Convert to direction GOING TO.
    """
    t_meta = STATION_METADATA.get(target_station)
    c_meta = STATION_METADATA.get(candidate_station)
    if not t_meta or not c_meta:
        return 1.0
    wind_going_to = (wind_direction + 180) % 360
    bearing_to_target = compute_bearing(
        c_meta['lat'], c_meta['lon'],
        t_meta['lat'], t_meta['lon']
    )
    angle_diff = abs(wind_going_to - bearing_to_target)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff
    return math.exp(-(angle_diff ** 2) / (2 * 60 ** 2))


# ═══════════════════════════════════════════════════════════════════════
# 7. COHERENCE & MODULATION COMPUTATION
# ═══════════════════════════════════════════════════════════════════════


def compute_coherence_phi(
    station_anomaly: float,
    regional_anomalies: Dict[str, float],
    station: str,
    region_name: str,
    wind_direction: Optional[float] = None,
    season: str = 'fall',
) -> float:
    """
    Compute spatial coherence Phi — extended version.

    Phi = 0.6 * magnitude_agreement + 0.4 * directional_agreement

    magnitude_agreement = 1 - tanh(|station_anomaly - weighted_anomaly| / sigma_phi)
    directional_agreement = weighted fraction of neighbors with same direction

    Uses enhanced distance weights (angular decay + altitude penalty + coastal
    adjustments) instead of basic Gaussian.

    Args:
        station_anomaly: Station's anomaly vs climatology
        regional_anomalies: Dict of station code -> anomaly for all stations in region
        station: ICAO code of target station
        region_name: Region key
        wind_direction: Prevailing wind direction FROM (degrees), optional
        season: Season for decorrelation length adjustment

    Returns:
        Phi in [0, 1]
    """
    region = REGIONS.get(region_name, REGIONS['SC'])
    sigma_phi = region.get('L', 400) * 0.3

    other_stations = {s: a for s, a in regional_anomalies.items() if s != station}
    if not other_stations:
        return 0.65

    # Compute enhanced distance weights
    weights = {}
    for s in other_stations:
        w = compute_spatial_weight(
            station, s,
            wind_direction=wind_direction,
            season=season,
        )
        weights[s] = w

    total_weight = sum(weights.values())
    if total_weight == 0:
        return 0.65

    # Weighted consensus anomaly
    weighted_anomaly = sum(
        weights[s] * other_stations[s] for s in other_stations
    ) / total_weight

    # Magnitude agreement
    delta = abs(station_anomaly - weighted_anomaly)
    magnitude_agree = 1.0 - min(1.0, math.tanh(delta / sigma_phi))

    # Directional agreement (weighted)
    station_dir = classify_direction(station_anomaly)
    dir_weight_sum = sum(
        weights[s] for s in other_stations
        if classify_direction(other_stations[s]) == station_dir
    )
    dir_agreement = dir_weight_sum / max(total_weight, 0.001)

    # Combined Phi
    phi = 0.6 * magnitude_agree + 0.4 * dir_agreement
    return min(1.0, max(0.0, phi))

def compute_modulation_factor(phi: float, region_name: str) -> float:
    """
    Compute modulation factor M(Phi) from spatial coherence.

    M(Phi) = 0.5 + 0.8 * Phi, clamped to regional [M_min, M_max].

    Args:
        phi: Spatial coherence Phi in [0, 1]
        region_name: Region key for bounds

    Returns:
        Modulation factor in [0.5, 1.3] typically
    """
    region = REGIONS.get(region_name, REGIONS['SC'])
    raw = 0.5 + 0.8 * phi
    return max(region['M_min'], min(region['M_max'], raw))


def compute_confirm_modulation(
    region: str,
    weighted_confirm_count: float,
    total_weighted_count: float,
) -> float:
    """
    Compute confidence modulation based on confirming station count (§4.3).

    Uses tiered thresholds from MIN_CONFIRMING dict.

    Args:
        region: Region key
        weighted_confirm_count: Sum of weights of confirming stations
        total_weighted_count: Sum of weights of all neighbors

    Returns:
        Modulation factor M
    """
    thresh = MIN_CONFIRMING.get(region, MIN_CONFIRMING['SC'])
    confirm_frac = weighted_confirm_count / max(total_weighted_count, 0.01)

    if weighted_confirm_count < thresh['min_wsum'] or confirm_frac < 0.3:
        M = 0.5 + 0.3 * confirm_frac          # Penalize
    elif (weighted_confirm_count >= thresh['min_wsum']
          and confirm_frac >= 0.7):
        M = 1.0 + 0.2 * confirm_frac           # Boost
    else:
        M = 0.8 + 0.4 * confirm_frac           # Near neutral

    bounds = REGION_BOUNDS.get(region, (0.5, 1.3))
    return max(bounds[0], min(bounds[1], M))


# ═══════════════════════════════════════════════════════════════════════
# 8. CORE API METHODS
# ═══════════════════════════════════════════════════════════════════════


def get_cluster(station: str) -> str:
    """
    Get the primary cluster/region for a station.

    Returns region key (NE, SE, SC, MW, RW, PAC) or 'SC' as default.

    This is the recommended interface for external callers.
    """
    meta = STATION_METADATA.get(station)
    if meta:
        return meta.get('region', 'SC')
    # Fallback: linear scan
    for r_name, r_data in REGIONS.items():
        if station in r_data['stations']:
            return r_name
    return 'SC'


def get_cluster_confirmation(
    signal_name: str,
    station: str,
    direction: str,
    confidence: float,
    nearby_signals: Dict[str, Dict[str, Any]],
    wind_direction: Optional[float] = None,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluate how well the station's cluster confirms its signal.

    Args:
        signal_name: Name of the signal being evaluated
        station: ICAO code
        direction: 'up' or 'down' (predicted direction)
        confidence: Raw/calibrated confidence in [0, 1]
        nearby_signals: Dict of station_code -> {
            'direction': str ('up'|'down'),
            'confidence': float,
            'anomaly': float (optional),
            'signal_name': str (optional),
        }
        wind_direction: Prevailing wind direction FROM (degrees), optional
        date_str: Date string for seasonal adjustment (YYYY-MM-DD)

    Returns:
        Dict with keys:
            region: str
            n_neighbors: int
            n_confirming: int
            weighted_confirm_sum: float
            total_weighted_sum: float
            confirm_ratio: float
            modulation: float (spatial coherence modulation factor)
            phi: float (spatial coherence)
            is_isolated: bool
    """
    region = get_cluster(station)
    season = get_season(date_str) if date_str else 'fall'

    # Build anomaly map from nearby_signals if available
    station_dir_int = 1 if direction == 'up' else -1
    station_anomaly = 1.0 if direction == 'up' else -1.0

    regional_anomalies: Dict[str, float] = {}
    for s_code, s_info in nearby_signals.items():
        if s_code == station:
            continue
        s_dir = s_info.get('direction', '')
        s_dir_int = 1 if s_dir == 'up' else -1
        s_anomaly = s_info.get('anomaly', s_dir_int * confidence)
        regional_anomalies[s_code] = s_anomaly

    # Compute weights for all neighbors
    weights: Dict[str, float] = {}
    for s in regional_anomalies:
        w = compute_spatial_weight(
            station, s,
            wind_direction=wind_direction,
            season=season,
        )
        weights[s] = w

    total_weight = sum(weights.values())
    n_neighbors = len(weights)

    # Count confirming stations
    n_confirming = 0
    weighted_confirm_sum = 0.0
    for s, w in weights.items():
        s_dir = nearby_signals.get(s, {}).get('direction', '')
        s_dir_int = 1 if s_dir == 'up' else -1
        s_anomaly = regional_anomalies.get(s, s_dir_int * 0.5)

        if is_confirming(station_dir_int, s_dir_int,
                         station_anomaly, s_anomaly):
            n_confirming += 1
            weighted_confirm_sum += w

    confirm_ratio = (
        weighted_confirm_sum / max(total_weight, 0.001)
        if total_weight > 0 else 0.0
    )

    # Compute Phi and modulation
    phi = compute_coherence_phi(
        station_anomaly,
        regional_anomalies,
        station,
        region,
        wind_direction=wind_direction,
        season=season,
    )
    m_phi = compute_modulation_factor(phi, region)
    m_confirm = compute_confirm_modulation(
        region, weighted_confirm_sum, total_weight
    )

    # Blend Phi-based and confirmation-based modulation
    modulation = 0.5 * m_phi + 0.5 * m_confirm

    # Marine layer override (§5.2)
    if (station in COASTAL_STATIONS
            and wind_direction is not None
            and is_coastal(station)):
        onshore_range = COASTAL_ONSHORE_DIRECTIONS.get(station)
        if onshore_range:
            os_start, os_end = onshore_range
            if os_start <= os_end:
                onshore = os_start <= wind_direction <= os_end
            else:
                onshore = wind_direction >= os_start or wind_direction <= os_end
            if onshore:
                # Onshore flow at coastal station → neutral modulation
                modulation = min(modulation, 1.0)

    # Check if this station is isolated (few neighbors with meaningful weight)
    nearby_weighted = sum(1 for s, w in weights.items() if w >= 0.05)
    is_isolated = nearby_weighted < MIN_CONFIRMING.get(region, {}).get('min_ct', 1)

    return {
        'region': region,
        'n_neighbors': n_neighbors,
        'n_confirming': n_confirming,
        'weighted_confirm_sum': weighted_confirm_sum,
        'total_weighted_sum': total_weight,
        'confirm_ratio': confirm_ratio,
        'modulation': modulation,
        'phi': phi,
        'is_isolated': is_isolated,
    }


def modulate_confidence(
    station: str,
    signal_name: str,
    direction: str,
    confidence: float,
    nearby_signals: Dict[str, Dict[str, Any]],
    wind_direction: Optional[float] = None,
    date_str: Optional[str] = None,
) -> float:
    """
    Apply spatial coherence modulation to a signal's confidence.

    This is the primary entry point for the evaluate pipeline.

    Args:
        station: ICAO code
        signal_name: Signal identifier (e.g., 'gaussian')
        direction: 'up' or 'down'
        confidence: Current confidence value [0, 1]
        nearby_signals: Dict of station -> signal info for all stations
            on the same target date
        wind_direction: Prevailing wind direction FROM (degrees), optional
        date_str: Target date string (YYYY-MM-DD), optional

    Returns:
        Adjusted confidence in [0.5, 0.99], or unchanged if no neighbors
    """
    result = get_cluster_confirmation(
        signal_name, station, direction, confidence,
        nearby_signals, wind_direction=wind_direction,
        date_str=date_str,
    )

    modulation = result['modulation']
    phi = result['phi']

    # Apply modulation factor to confidence
    # Convert confidence to edge-space, modulate, convert back
    # edge = 2 * (confidence - 0.5)  (maps [0.5, 1.0] -> [0.0, 1.0])
    # adjusted_edge = edge * modulation
    # adjusted_conf = 0.5 + adjusted_edge / 2
    if result['is_isolated']:
        # Isolated stations: minimal modulation (avoid penalizing sparsity)
        modulation = 0.5 + 0.5 * modulation  # range [0.75, 1.15] -> blend toward neutral

    edge = 2.0 * (confidence - 0.5)
    adjusted_edge = edge * modulation
    adj_conf = 0.5 + adjusted_edge / 2.0

    # Clamp to reasonable bounds
    adj_conf = max(0.50, min(0.99, adj_conf))

    logger.debug(
        "Spatial coherence %s/%s: phi=%.3f mod=%.3f "
        "conf=%.3f->%.3f iso=%s",
        station, signal_name, phi, modulation,
        confidence, adj_conf, result['is_isolated']
    )

    return adj_conf


# ═══════════════════════════════════════════════════════════════════════
# 9. SPATIAL COHERENCE GATE CLASS  (FP 6.3 backward-compatible)
# ═══════════════════════════════════════════════════════════════════════


class SpatialCoherenceGate:
    """
    Spatial Coherence Verification Gate.

    Provides both the original FP 6.3 interface (compute_modulation,
    apply_modulation) and the new enhanced API (modulate_confidence,
    get_cluster_confirmation, get_cluster).

    Integrates at pipeline layer: between signal fusion and trade generation.
    Applies continuous modulation (not binary pass/fail).
    """

    def __init__(self):
        self._coords = STATION_COORDS
        self._regions = REGIONS
        self._metadata = STATION_METADATA

    # ── Enhanced API —──

    @staticmethod
    def get_cluster(station: str) -> str:
        """Get the primary cluster/region for a station."""
        return get_cluster(station)

    @staticmethod
    def get_cluster_confirmation(
        signal_name: str,
        station: str,
        direction: str,
        confidence: float,
        nearby_signals: Dict[str, Dict[str, Any]],
        wind_direction: Optional[float] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        return get_cluster_confirmation(
            signal_name, station, direction, confidence,
            nearby_signals, wind_direction=wind_direction,
            date_str=date_str,
        )

    @staticmethod
    def modulate_confidence(
        station: str,
        signal_name: str,
        direction: str,
        confidence: float,
        nearby_signals: Dict[str, Dict[str, Any]],
        wind_direction: Optional[float] = None,
        date_str: Optional[str] = None,
    ) -> float:
        return modulate_confidence(
            station, signal_name, direction, confidence,
            nearby_signals, wind_direction=wind_direction,
            date_str=date_str,
        )

    # ── Original FP 6.3 API (backward compat) —──

    def get_region_for_station(self, station: str) -> Tuple[str, str, float]:
        """Get region info for a station (original interface)."""
        meta = self._metadata.get(station, {})
        if 'secondary_region' in meta:
            return (meta['region'], meta['secondary_region'],
                    meta['primary_weight'])
        return meta.get('region', 'SC'), '', 1.0

    def compute_modulation(
        self,
        station_forecasts: Dict[str, Dict[str, Any]],
        date_str: Optional[str] = None,
    ) -> Dict[str, float]:
        """
        Compute spatial coherence modulation for all stations (enhanced).

        Args:
            station_forecasts: Dict of station -> {
                'anomaly': float,
                'conviction': float,
                'confidence': float,
                'wind_direction': float (optional),
            }
            date_str: Date string for seasonal adjustment

        Returns:
            Dict of station -> modulation factor M(Phi)
        """
        season = get_season(date_str) if date_str else 'fall'

        # Collect regional anomalies
        region_anomalies: Dict[str, Dict[str, float]] = {
            r: {} for r in self._regions
        }
        for station, forecast in station_forecasts.items():
            anomaly = forecast.get('anomaly', 0.0)
            prim_region, sec_region, _ = self.get_region_for_station(station)
            if prim_region in region_anomalies:
                region_anomalies[prim_region][station] = anomaly
            if sec_region and sec_region in region_anomalies:
                region_anomalies[sec_region][station] = anomaly

        # Compute modulation per station
        mod_factors: Dict[str, float] = {}
        for station, forecast in station_forecasts.items():
            station_anomaly = forecast.get('anomaly', 0.0)
            wind_dir = forecast.get('wind_direction')
            prim_region, sec_region, prim_weight = self.get_region_for_station(station)

            phi_prim = compute_coherence_phi(
                station_anomaly,
                region_anomalies.get(prim_region, {}),
                station, prim_region,
                wind_direction=wind_dir,
                season=season,
            )
            m_prim = compute_modulation_factor(phi_prim, prim_region)

            if sec_region and sec_region in self._regions:
                phi_sec = compute_coherence_phi(
                    station_anomaly,
                    region_anomalies.get(sec_region, {}),
                    station, sec_region,
                    wind_direction=wind_dir,
                    season=season,
                )
                m_sec = compute_modulation_factor(phi_sec, sec_region)
                modulation = (prim_weight * m_prim
                              + (1.0 - prim_weight) * m_sec)
                logger.debug(
                    "Spatial gate %s: prim=%s phi=%.3f m=%.3f "
                    "sec=%s phi=%.3f m=%.3f blended=%.3f",
                    station, prim_region, phi_prim, m_prim,
                    sec_region, phi_sec, m_sec, modulation
                )
            else:
                modulation = m_prim
                logger.debug(
                    "Spatial gate %s: region=%s phi=%.3f m=%.3f",
                    station, prim_region, phi_prim, modulation
                )
            mod_factors[station] = modulation

        return mod_factors

    def apply_modulation(
        self,
        station_forecasts: Dict[str, Dict[str, Any]],
        date_str: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Apply spatial coherence modulation to forecasts."""
        mod_factors = self.compute_modulation(station_forecasts, date_str=date_str)
        result = {}
        for station, forecast in station_forecasts.items():
            result[station] = dict(forecast)
            modulation = mod_factors.get(station, 1.0)
            base_conviction = forecast.get('conviction', 0.5)
            result[station]['conviction_adjusted'] = base_conviction * modulation
            result[station]['spatial_modulation'] = modulation
            result[station]['spatial_phi'] = forecast.get('spatial_phi', 0.65)
        return result

    def get_region_stats(self) -> Dict[str, dict]:
        """Return region definitions with station counts for monitoring."""
        return {
            name: {
                'stations': data['stations'],
                'L_km': data['L'],
                'M_range': [data['M_min'], data['M_max']],
                'cluster_kind': data.get('cluster_kind', 'sparse'),
            }
            for name, data in self._regions.items()
        }


# ═══════════════════════════════════════════════════════════════════════
# 10. SELF-TEST
# ═══════════════════════════════════════════════════════════════════════


def self_test():
    """Run a self-test with synthetic forecasts and nearby signal data."""
    gate = SpatialCoherenceGate()

    print("=== Spatial Coherence Verification — Self-Test ===")

    # ── Test 1: Basic get_cluster ──
    print("\n--- Test 1: get_cluster ---")
    for s in ['KNYC', 'KATL', 'KDFW', 'KMDW', 'KDEN', 'KSEA', 'KOKC']:
        cluster = gate.get_cluster(s)
        print(f"  {s} -> {cluster}")

    # ── Test 2: Distance weight with altitude penalty ──
    print("\n--- Test 2: compute_spatial_weight ---")
    # KMDW (620 ft) to KMSP (841 ft) — low altitude diff
    w1 = compute_spatial_weight('KMDW', 'KMSP', season='summer')
    # KDEN (5431 ft) to KPHX (1135 ft) — large altitude diff
    w2 = compute_spatial_weight('KDEN', 'KPHX', season='summer')
    # KBOS (coastal) to KNYC (coastal) — coastal pair
    w3 = compute_spatial_weight('KBOS', 'KNYC', season='summer')
    # KATL (inland) to KMIA (coastal) — inland-coastal penalty
    w4 = compute_spatial_weight('KATL', 'KMIA', season='summer')
    # KSEA to KLAX with westerly wind (upstream)
    w5 = compute_spatial_weight('KSEA', 'KLAX', wind_direction=270, season='summer')
    # KSEA to KLAX with easterly wind (downstream)
    w6 = compute_spatial_weight('KSEA', 'KLAX', wind_direction=90, season='summer')

    print(f"  KMDW→KMSP (low elev diff):      {w1:.4f}")
    print(f"  KDEN→KPHX (high elev diff):      {w2:.4f}")
    print(f"  KBOS→KNYC (coastal pair):        {w3:.4f}")
    print(f"  KATL→KMIA (inland→coastal):      {w4:.4f}")
    print(f"  KSEA→KLAX (westerly/upstream):   {w5:.4f}")
    print(f"  KSEA→KLAX (easterly/downstream): {w6:.4f}")

    # ── Test 3: modulate_confidence with synthetic nearby signals ──
    print("\n--- Test 3: modulate_confidence ---")

    # Scenario: KDFW (Dallas) predicts 'up' with 0.72 confidence.
    # Nearby SC stations all agree (all 'up')
    nearby = {
        'KHOU': {'direction': 'up', 'confidence': 0.65, 'anomaly': 2.0},
        'KAUS': {'direction': 'up', 'confidence': 0.68, 'anomaly': 1.5},
        'KSAT': {'direction': 'up', 'confidence': 0.63, 'anomaly': 1.8},
        'KOKC': {'direction': 'up', 'confidence': 0.55, 'anomaly': 0.5},
    }
    adj = gate.modulate_confidence(
        'KDFW', 'gaussian', 'up', 0.72, nearby, date_str='2026-08-06'
    )
    print(f"  KDFW up 0.72 (all agree):    {adj:.4f}")

    # Scenario: KDFW predicts 'up', but neighbors disagree (all 'down')
    nearby_disagree = {
        'KHOU': {'direction': 'down', 'confidence': 0.65, 'anomaly': -2.0},
        'KAUS': {'direction': 'down', 'confidence': 0.68, 'anomaly': -1.5},
        'KSAT': {'direction': 'down', 'confidence': 0.63, 'anomaly': -1.8},
        'KOKC': {'direction': 'down', 'confidence': 0.55, 'anomaly': -0.5},
    }
    adj2 = gate.modulate_confidence(
        'KDFW', 'gaussian', 'up', 0.72, nearby_disagree, date_str='2026-08-06'
    )
    print(f"  KDFW up 0.72 (all disagree): {adj2:.4f}")

    # Scenario: KSEA (isolated PAC) predicts 'up', no nearby stations
    adj3 = gate.modulate_confidence(
        'KSEA', 'gaussian', 'up', 0.65, {},
        wind_direction=260, date_str='2026-08-06'
    )
    print(f"  KSEA up 0.65 (isolated PAC): {adj3:.4f}")

    # Scenario: KLAX predicts 'down' while KSFO and KSEA agree
    nearby_pac = {
        'KSFO': {'direction': 'down', 'confidence': 0.60, 'anomaly': -1.0},
        'KSEA': {'direction': 'down', 'confidence': 0.58, 'anomaly': -1.5},
    }
    adj4 = gate.modulate_confidence(
        'KLAX', 'gaussian', 'down', 0.62, nearby_pac,
        wind_direction=290, date_str='2026-08-06'
    )
    print(f"  KLAX down 0.62 (PAC agrees): {adj4:.4f}")

    # Scenario: KLAX predicts 'up' while KSFO and KSEA disagree
    nearby_pac_dis = {
        'KSFO': {'direction': 'down', 'confidence': 0.60, 'anomaly': -1.0},
        'KSEA': {'direction': 'down', 'confidence': 0.58, 'anomaly': -1.5},
    }
    adj5 = gate.modulate_confidence(
        'KLAX', 'gaussian', 'up', 0.62, nearby_pac_dis,
        wind_direction=290, date_str='2026-08-06'
    )
    print(f"  KLAX up 0.62 (PAC disagrees): {adj5:.4f}")

    # ── Test 4: Sea breeze detection ──
    print("\n--- Test 4: sea_breeze detection ---")
    # Simulate KBOS: offshore (NW) then onshore (E)
    metar_obs = [
        {'wind_direction_deg': 320, 'wind_speed_kt': 8},
        {'wind_direction_deg': 330, 'wind_speed_kt': 7},
        {'wind_direction_deg': 340, 'wind_speed_kt': 6},
        {'wind_direction_deg': 345, 'wind_speed_kt': 5},
        {'wind_direction_deg': 85, 'wind_speed_kt': 6},
        {'wind_direction_deg': 95, 'wind_speed_kt': 7},
    ]
    sb = detect_sea_breeze('KBOS', metar_obs)
    print(f"  KBOS sea breeze (NW➨E): {sb}")

    # No sea breeze (steady onshore)
    metar_obs2 = [
        {'wind_direction_deg': 100, 'wind_speed_kt': 8},
        {'wind_direction_deg': 95, 'wind_speed_kt': 7},
        {'wind_direction_deg': 90, 'wind_speed_kt': 6},
        {'wind_direction_deg': 85, 'wind_speed_kt': 6},
        {'wind_direction_deg': 95, 'wind_speed_kt': 5},
        {'wind_direction_deg': 100, 'wind_speed_kt': 5},
    ]
    sb2 = detect_sea_breeze('KBOS', metar_obs2)
    print(f"  KBOS no sea breeze (steady E): {sb2}")

    # No sea breeze (steady offshore)
    metar_obs3 = [
        {'wind_direction_deg': 320, 'wind_speed_kt': 8},
        {'wind_direction_deg': 310, 'wind_speed_kt': 7},
        {'wind_direction_deg': 330, 'wind_speed_kt': 6},
        {'wind_direction_deg': 340, 'wind_speed_kt': 6},
        {'wind_direction_deg': 320, 'wind_speed_kt': 5},
        {'wind_direction_deg': 315, 'wind_speed_kt': 5},
    ]
    sb3 = detect_sea_breeze('KBOS', metar_obs3)
    print(f"  KBOS no sea breeze (steady NW): {sb3}")

    # ── Test 5: Original FP 6.3 interface (backward compat) ──
    print("\n--- Test 5: FP 6.3 backward compat ---")
    forecasts = {
        'KMDW': {'anomaly': -8.0, 'conviction': 0.22, 'confidence': 0.7},
        'KMSP': {'anomaly': -6.0, 'conviction': 0.24, 'confidence': 0.72},
        'KNYC': {'anomaly': -5.0, 'conviction': 0.20, 'confidence': 0.68},
        'KBOS': {'anomaly': -4.0, 'conviction': 0.19, 'confidence': 0.65},
        'KPHL': {'anomaly': -3.0, 'conviction': 0.18, 'confidence': 0.66},
        'KDCA': {'anomaly': -2.0, 'conviction': 0.16, 'confidence': 0.64},
        'KATL': {'anomaly': +3.0, 'conviction': 0.21, 'confidence': 0.70},
        'KMIA': {'anomaly': -1.0, 'conviction': 0.15, 'confidence': 0.60},
        'KMSY': {'anomaly': -2.0, 'conviction': 0.17, 'confidence': 0.63},
        'KLAX': {'anomaly': -5.0, 'conviction': 0.20, 'confidence': 0.70},
        'KSFO': {'anomaly': +1.0, 'conviction': 0.15, 'confidence': 0.60},
        'KSEA': {'anomaly': +2.0, 'conviction': 0.16, 'confidence': 0.62},
        'KHOU': {'anomaly': +2.0, 'conviction': 0.16, 'confidence': 0.65},
        'KDFW': {'anomaly': +1.0, 'conviction': 0.15, 'confidence': 0.63},
        'KAUS': {'anomaly': +2.0, 'conviction': 0.17, 'confidence': 0.64},
        'KSAT': {'anomaly': +1.0, 'conviction': 0.16, 'confidence': 0.62},
        'KOKC': {'anomaly': 0.0,  'conviction': 0.14, 'confidence': 0.60},
        'KDEN': {'anomaly': -3.0, 'conviction': 0.18, 'confidence': 0.66},
        'KPHX': {'anomaly': +2.0, 'conviction': 0.15, 'confidence': 0.62},
        'KLAS': {'anomaly': +1.0, 'conviction': 0.14, 'confidence': 0.60},
    }

    result = gate.apply_modulation(forecasts, date_str='2026-08-06')
    print(f"{'Station':<8} {'Region':<10} {'Anomaly':>8} {'Base':>8} {'Mod':>8} {'Adj':>8}")
    print("-" * 55)
    for station in sorted(forecasts.keys()):
        f = forecasts[station]
        r = result[station]
        prim, sec, _ = gate.get_region_for_station(station)
        reg = sec if sec else prim
        print(f"{station:<8} {reg:<10} {f['anomaly']:>+7.1f}°F "
              f"{f['conviction']:>8.3f} "
              f"{r.get('spatial_modulation', 1.0):>7.3f} "
              f"{r.get('conviction_adjusted', 0):>8.3f}")

    print("\nSelf-test: PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    self_test()
