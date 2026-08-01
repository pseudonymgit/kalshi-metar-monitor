"""
Spatial Coherence Gate — First-Principles (FP 6.3)

Continuous confidence modulation based on regional spatial coherence.

6 climate regions with inverse-distance-weighted consensus:
    NE: Northeast Corridor (KNYC, KBOS, KPHL, KDCA)
    SE: Southeast (KATL, KMIA, KMSY)
    SC: South Central/Gulf (KHOU, KDFW, KAUS, KSAT, KOKC)
    MW: Midwest/Great Lakes (KMDW, KMSP)
    RW: Mountain West (KDEN, KPHX, KLAS, KOKC - overlapping)
    PAC: Pacific Coast (KSEA, KSFO, KLAX)

Formula:
    Phi = 0.6 * (1 - tanh(|mean_anomaly - station_anomaly| / sigma_phi))
          + 0.4 * directional_agreement

    M(Phi) = 0.5 + 0.8 * Phi  (continuous modulation, 0.5-1.3x)

Usage:
    from core.spatial_coherence import SpatialCoherenceGate

    gate = SpatialCoherenceGate()
    mod_factors = gate.compute_modulation(forecasts)
    # Apply: conviction_adj = conviction_base * mod_factors[station]

B-Mode R8 Cycle 4.5: Replaced binary D4/D2 gate with FP 6.3 continuous modulation.
"""

import math
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Region Definitions (FP 6.3 Section 2.2)
# ─────────────────────────────────────────────────────────────────────

REGIONS = {
    'NE': {  # Northeast Corridor
        'stations': ['KNYC', 'KBOS', 'KPHL', 'KDCA'],
        'L': 300,  # decorrelation length (km)
        'M_min': 0.5,
        'M_max': 1.25,
    },
    'SE': {  # Southeast
        'stations': ['KATL', 'KMIA', 'KMSY'],
        'L': 500,
        'M_min': 0.6,
        'M_max': 1.15,
    },
    'SC': {  # South Central / Gulf
        'stations': ['KHOU', 'KDFW', 'KAUS', 'KSAT', 'KOKC'],
        'L': 350,
        'M_min': 0.5,
        'M_max': 1.30,
    },
    'MW': {  # Midwest / Great Lakes
        'stations': ['KMDW', 'KMSP'],
        'L': 350,
        'M_min': 0.5,
        'M_max': 1.30,
    },
    'RW': {  # Mountain West (Rocky West)
        'stations': ['KDEN', 'KPHX', 'KLAS', 'KOKC'],
        'L': 600,
        'M_min': 0.7,
        'M_max': 1.10,
    },
    'PAC': {  # Pacific Coast
        'stations': ['KSEA', 'KSFO', 'KLAX'],
        'L': 400,
        'M_min': 0.6,
        'M_max': 1.20,
    },
}

# Station coordinates (lat, lon) for distance computation
STATION_COORDS: Dict[str, Tuple[float, float]] = {
    'KNYC': (40.78, -73.97), 'KBOS': (42.36, -71.01),
    'KPHL': (39.87, -75.24), 'KDCA': (38.85, -77.04),
    'KATL': (33.64, -84.43), 'KMIA': (25.79, -80.29),
    'KMSY': (30.00, -90.26),
    'KHOU': (29.65, -95.28), 'KDFW': (32.90, -97.04),
    'KAUS': (30.19, -97.67), 'KSAT': (29.53, -98.47),
    'KOKC': (35.39, -97.60),
    'KMDW': (41.79, -87.75), 'KMSP': (44.88, -93.22),
    'KDEN': (39.85, -104.67), 'KPHX': (33.43, -112.02),
    'KLAS': (36.08, -115.15),
    'KSEA': (47.45, -122.31), 'KSFO': (37.62, -122.38),
    'KLAX': (33.94, -118.41),
}

# Overlap stations with secondary region membership (FP 6.3 Section 2.3)
OVERLAP_STATIONS: Dict[str, Dict[str, float]] = {
    'KOKC': {'primary': 'SC', 'primary_weight': 0.65,
             'secondary': 'RW', 'secondary_weight': 0.35},
}


def great_circle_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two points in km (Haversine)."""
    R = 6371.0  # Earth radius in km
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def distance_weight(d_km: float, L: float) -> float:
    """
    Compute inverse-distance weight (FP 6.3 Section 2.3).

    w(j|s) = exp(-d(s,j)^2 / (2 * L^2))

    Args:
        d_km: Great-circle distance in km
        L: Characteristic decorrelation length in km

    Returns:
        Weight in [0, 1]
    """
    if L <= 0:
        return 0.0
    return math.exp(-(d_km ** 2) / (2.0 * L * L))


def compute_coherence_phi(
    station_anomaly: float,
    regional_anomalies: Dict[str, float],
    station: str,
    region_name: str,
) -> float:
    """
    Compute spatial coherence Phi for a station (FP 6.3 Section 4).

    Phi = 0.6 * magnitude_agreement + 0.4 * directional_agreement

    magnitude_agreement = 1 - tanh(|station_anomaly - mean_anomaly| / sigma_phi)
    directional_agreement = fraction of neighboring stations with same direction

    Args:
        station_anomaly: Station's anomaly vs climatology
        regional_anomalies: Dict of station -> anomaly for all stations in region
        station: ICAO code of target station
        region_name: Region key

    Returns:
        Phi in [0, 1]
    """
    region = REGIONS.get(region_name, REGIONS['SC'])
    sigma_phi = region.get('L', 400) * 0.3  # Scale: ~120 km for 400 km L

    # Compute weighted consensus anomaly and direction agreement
    other_stations = {s: a for s, a in regional_anomalies.items() if s != station}
    if not other_stations:
        return 0.65  # No neighbors = neutral

    coords = STATION_COORDS
    weights = {}
    for s in other_stations:
        dist = great_circle_distance(
            coords[station][0], coords[station][1],
            coords[s][0], coords[s][1]
        )
        weights[s] = distance_weight(dist, region['L'])

    total_weight = sum(weights.values())
    if total_weight == 0:
        return 0.65

    # Weighted consensus anomaly
    weighted_anomaly = sum(
        weights[s] * other_stations[s] for s in other_stations
    ) / total_weight

    # Magnitude agreement (FP 6.3 Section 4.2)
    delta = abs(station_anomaly - weighted_anomaly)
    magnitude_agree = 1.0 - min(1.0, math.tanh(delta / sigma_phi))

    # Directional agreement
    station_dir = 1 if station_anomaly >= 0 else -1
    dir_matches = sum(
        1 for s in other_stations
        if (1 if other_stations[s] >= 0 else -1) == station_dir
    )
    dir_agreement = dir_matches / max(len(other_stations), 1)

    # Combined Phi
    phi = 0.6 * magnitude_agree + 0.4 * dir_agreement
    return min(1.0, max(0.0, phi))


def compute_modulation_factor(phi: float, region_name: str) -> float:
    """
    Compute modulation factor M(Phi) from spatial coherence (FP 6.3 Section 4.2).

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


class SpatialCoherenceGate:
    """
    Computes spatial coherence modulation for 20-station ensemble.

    Integrates at pipeline layer 6: between base conviction and trade generation.
    Applies continuous modulation (not binary pass/fail) per FP 6.3 spec.
    """

    def __init__(self):
        self._coords = STATION_COORDS
        self._regions = REGIONS
        self._overlaps = OVERLAP_STATIONS

    def get_region_for_station(self, station: str) -> Tuple[str, str, float]:
        """
        Get the primary region for a station, handling overlaps.

        Returns:
            Tuple of (primary_region, secondary_region_or_empty, primary_weight)
        """
        # Check overlap first
        if station in self._overlaps:
            o = self._overlaps[station]
            return o['primary'], o.get('secondary', ''), o['primary_weight']

        # Find region by station membership
        for r_name, r_data in self._regions.items():
            if station in r_data['stations']:
                return r_name, '', 1.0

        return 'SC', '', 1.0  # Default to South Central

    def compute_modulation(
        self,
        station_forecasts: Dict[str, Dict[str, Any]],
    ) -> Dict[str, float]:
        """
        Compute spatial coherence modulation for all stations.

        Args:
            station_forecasts: Dict of station -> {
                'anomaly': float,  # forecast temp - climato mean (°F)
                'conviction': float,  # base conviction (for logging)
                'confidence': float,  # optional
            }

        Returns:
            Dict of station -> modulation factor M(Phi)
        """
        # Build anomalies dict per region
        region_anomalies: Dict[str, Dict[str, float]] = {}
        for r_name in self._regions:
            region_anomalies[r_name] = {}

        for station, forecast in station_forecasts.items():
            anomaly = forecast.get('anomaly', 0.0)
            prim_region, sec_region, prim_weight = self.get_region_for_station(station)

            if prim_region in region_anomalies:
                region_anomalies[prim_region][station] = anomaly
            if sec_region and sec_region in region_anomalies:
                region_anomalies[sec_region][station] = anomaly

        # Compute modulation per station
        mod_factors: Dict[str, float] = {}
        for station, forecast in station_forecasts.items():
            station_anomaly = forecast.get('anomaly', 0.0)
            prim_region, sec_region, prim_weight = self.get_region_for_station(station)

            # Primary region coherence
            phi_prim = compute_coherence_phi(
                station_anomaly, region_anomalies.get(prim_region, {}),
                station, prim_region
            )
            m_prim = compute_modulation_factor(phi_prim, prim_region)

            # Blend with secondary region if applicable
            if sec_region and sec_region in self._regions:
                phi_sec = compute_coherence_phi(
                    station_anomaly, region_anomalies.get(sec_region, {}),
                    station, sec_region
                )
                m_sec = compute_modulation_factor(phi_sec, sec_region)
                modulation = prim_weight * m_prim + (1.0 - prim_weight) * m_sec
                logger.debug(
                    "Spatial gate %s: prim=%s phi=%.3f m=%.3f sec=%s phi=%.3f m=%.3f blended=%.3f",
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
    ) -> Dict[str, Dict[str, Any]]:
        """
        Apply spatial coherence modulation to forecasts.

        Modifies: forecast['conviction'] *= M(Phi)
        Returns: modified station_forecasts dict
        """
        mod_factors = self.compute_modulation(station_forecasts)
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
            }
            for name, data in self._regions.items()
        }


# ─────────────────────────────────────────────────────────────────────
# Standalone usage / test
# ─────────────────────────────────────────────────────────────────────

def self_test():
    """Run a self-test with synthetic forecasts."""
    gate = SpatialCoherenceGate()

    # Synthetic forecasts: cold front sweeping through Midwest/NE
    forecasts = {
        # Midwest — cold anomaly, consistent
        'KMDW': {'anomaly': -8.0, 'conviction': 0.22, 'confidence': 0.7},
        'KMSP': {'anomaly': -6.0, 'conviction': 0.24, 'confidence': 0.72},
        # Northeast — moderate cold, consistent with front
        'KNYC': {'anomaly': -5.0, 'conviction': 0.20, 'confidence': 0.68},
        'KBOS': {'anomaly': -4.0, 'conviction': 0.19, 'confidence': 0.65},
        'KPHL': {'anomaly': -3.0, 'conviction': 0.18, 'confidence': 0.66},
        'KDCA': {'anomaly': -2.0, 'conviction': 0.16, 'confidence': 0.64},
        # Contrarian: ATL says warm while neighbors cold
        'KATL': {'anomaly': +3.0, 'conviction': 0.21, 'confidence': 0.70},
        'KMIA': {'anomaly': -1.0, 'conviction': 0.15, 'confidence': 0.60},
        'KMSY': {'anomaly': -2.0, 'conviction': 0.17, 'confidence': 0.63},
        # Contrarian: LAX says cold while neighbors warm
        'KLAX': {'anomaly': -5.0, 'conviction': 0.20, 'confidence': 0.70},
        'KSFO': {'anomaly': +1.0, 'conviction': 0.15, 'confidence': 0.60},
        'KSEA': {'anomaly': +2.0, 'conviction': 0.16, 'confidence': 0.62},
        # South Central — slight warm
        'KHOU': {'anomaly': +2.0, 'conviction': 0.16, 'confidence': 0.65},
        'KDFW': {'anomaly': +1.0, 'conviction': 0.15, 'confidence': 0.63},
        'KAUS': {'anomaly': +2.0, 'conviction': 0.17, 'confidence': 0.64},
        'KSAT': {'anomaly': +1.0, 'conviction': 0.16, 'confidence': 0.62},
        'KOKC': {'anomaly': 0.0, 'conviction': 0.14, 'confidence': 0.60},
        # Mountain West
        'KDEN': {'anomaly': -3.0, 'conviction': 0.18, 'confidence': 0.66},
        'KPHX': {'anomaly': +2.0, 'conviction': 0.15, 'confidence': 0.62},
        'KLAS': {'anomaly': +1.0, 'conviction': 0.14, 'confidence': 0.60},
    }

    result = gate.apply_modulation(forecasts)

    print("=== Spatial Coherence Self-Test ===")
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

    # Verify Midwest consistency (should be near neutral)
    kw_mod = result['KMDW'].get('spatial_modulation', 1.0)
    se_mod = result['KATL'].get('spatial_modulation', 1.0)
    la_mod = result['KLAX'].get('spatial_modulation', 1.0)
    print(f"\nKMDW (consistent MW): mod={kw_mod:.3f}")
    print(f"KATL (contrarian SE): mod={se_mod:.3f}")
    print(f"KLAX (contrarian PAC): mod={la_mod:.3f}")
    assert kw_mod >= 0.9, f"KMDW should be boosted or neutral: {kw_mod}"
    assert se_mod < 1.0, f"KATL should be penalized: {se_mod}"
    assert la_mod < 1.0, f"KLAX should be penalized: {la_mod}"
    print("\nSelf-test: PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    self_test()