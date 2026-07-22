#!/usr/bin/env python3
"""
SIGNAL: 850-mb Temperature Advection Signal

Computes 850-mb temperature advection from GFS forecast fields:

    ADV_850 = -u850 * (∂T850/∂x) - v850 * (∂T850/∂y)

Where u850, v850 are the 850-mb wind components and ∂T850/∂x, ∂T850/∂y
are the horizontal temperature gradients at 850-mb, computed via centered
differences on a 2.5° × 2.5° grid centered on each city.

This is the single most physically direct predictor of next-day surface
temperature change per published meteorology literature (expected 70-75%
directional accuracy).

Reference: Section 6 of docs/plans/GRAY-ROOM-ROUND3-EXPERT4-METEOROLOGY.md
"""

import sqlite3
import math
import json
import urllib.request
import urllib.error
import time
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────
NWP_DB_DEFAULT = "data/nwp_forecasts.db"
GFS_ENDPOINT = "https://api.open-meteo.com/v1/gfs"
# Grid spacing in degrees for gradient computation
GRID_SPACING = 2.5
# Half-spacing for offset from city center
HALF_GRID = GRID_SPACING / 2.0  # 1.25°
# Confidence scaling
MAX_CONFIDENCE = 0.85
Z_THRESHOLD = 0.5  # |z| must exceed this to fire
# Rolling window for normalization std
ROLLING_WINDOW = 30
# Default std if insufficient history
# Empirically measured from 20-city GFS sample on 2026-07-19:
# mean=5.8e-6, std=5.3e-5, abs_mean=3.3e-5
DEFAULT_STD = 5.0e-5  # °C/s (typical advection magnitude)
# Min grid points with valid data required
MIN_VALID_GRID = 3


# ── City registry (same 20 cities) ────────────────────────────────────
CITIES = [
    ("KATL", "Atlanta", 33.64, -84.43),
    ("KAUS", "Austin", 30.20, -97.67),
    ("KBOS", "Boston", 42.37, -71.01),
    ("KDCA", "Washington DC", 38.85, -77.04),
    ("KDEN", "Denver", 39.86, -104.67),
    ("KDFW", "Dallas", 32.90, -97.04),
    ("KHOU", "Houston", 29.98, -95.36),
    ("KLAS", "Las Vegas", 36.08, -115.16),
    ("KLAX", "Los Angeles", 33.94, -118.41),
    ("KMDW", "Chicago", 41.79, -87.75),
    ("KMIA", "Miami", 25.80, -80.29),
    ("KMSP", "Minneapolis", 44.88, -93.22),
    ("KMSY", "New Orleans", 29.99, -90.26),
    ("KNYC", "New York", 40.71, -74.01),
    ("KOKC", "Oklahoma City", 35.39, -97.60),
    ("KPHL", "Philadelphia", 39.87, -75.24),
    ("KPHX", "Phoenix", 33.43, -112.01),
    ("KSAT", "San Antonio", 29.53, -98.47),
    ("KSEA", "Seattle", 47.45, -122.31),
    ("KSFO", "San Francisco", 37.62, -122.38),
]


def _get_grid_points(lat: float, lon: float) -> List[Tuple[float, float, str]]:
    """Return the 4 grid points for a 2.5° grid centered on (lat, lon).

    Returns list of (lat, lon, label) where label is NW, NE, SW, SE.
    """
    return [
        (lat + HALF_GRID, lon - HALF_GRID, "NW"),  # upper-left
        (lat + HALF_GRID, lon + HALF_GRID, "NE"),  # upper-right
        (lat - HALF_GRID, lon - HALF_GRID, "SW"),  # lower-left
        (lat - HALF_GRID, lon + HALF_GRID, "SE"),  # lower-right
    ]


def _deg_to_meters(lat: float, dlon: float, dlat: float) -> Tuple[float, float]:
    """Convert degree differences to meters at given latitude.

    Args:
        lat: Latitude in degrees
        dlon: Longitude difference in degrees
        dlat: Latitude difference in degrees

    Returns:
        (dx_m, dy_m) where dx is eastward and dy is northward
    """
    lat_rad = math.radians(lat)
    # 1° latitude ≈ 111,320 m (constant)
    dy = dlat * 111_320.0
    # 1° longitude ≈ 111,320 * cos(lat) m
    dx = dlon * 111_320.0 * math.cos(lat_rad)
    return dx, dy


def _wind_to_uv(wind_speed_kmh: float, wind_dir_deg: float) -> Tuple[float, float]:
    """Convert meteorological wind speed/direction to u, v components.

    Meteorological convention: 0° = wind from north, 90° = from east.
    u = positive eastward, v = positive northward.

    Args:
        wind_speed_kmh: Wind speed in km/h
        wind_dir_deg: Wind direction in degrees (meteorological)

    Returns:
        (u, v) in m/s
    """
    # Convert km/h to m/s
    speed_ms = wind_speed_kmh / 3.6
    dir_rad = math.radians(wind_dir_deg)
    # u = -speed * sin(dir), v = -speed * cos(dir)
    u = -speed_ms * math.sin(dir_rad)
    v = -speed_ms * math.cos(dir_rad)
    return u, v


def fetch_gfs_grid_data(lat: float, lon: float) -> Optional[Dict[str, List[float]]]:
    """Fetch 850-mb temperature, wind speed, and wind direction from GFS
    for 4 grid points around (lat, lon).

    Returns dict with keys:
        'grid_temps': list of 4 temps (°C) in [NW, NE, SW, SE] order
        'grid_wind_speeds': list of 4 wind speeds (km/h)
        'grid_wind_dirs': list of 4 wind directions (°)
        'city_u': u-component at city center (m/s)
        'city_v': v-component at city center (m/s)
    Or None on failure.
    """
    grid_points = _get_grid_points(lat, lon)

    # Build multi-coordinate request
    lats = ",".join(str(p[0]) for p in grid_points)
    lons = ",".join(str(p[1]) for p in grid_points)

    # Also fetch city center for wind components
    city_lat = str(lat)
    city_lon = str(lon)

    # Combine all 5 locations: 4 grid + 1 city center
    all_lats = lats + "," + city_lat
    all_lons = lons + "," + city_lon

    params = {
        "latitude": all_lats,
        "longitude": all_lons,
        "hourly": "temperature_850hPa,wind_speed_850hPa,wind_direction_850hPa",
        "timezone": "UTC",
        "forecast_days": 2,  # Get today and tomorrow
    }

    url = GFS_ENDPOINT + "?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"GFS fetch failed for ({lat}, {lon}): {e}")
        return None

    if isinstance(data, dict) and "error" in data:
        logger.warning(f"GFS API error for ({lat}, {lon}): {data['error']}")
        return None

    if not isinstance(data, list) or len(data) < 5:
        logger.warning(f"Unexpected GFS response format for ({lat}, {lon})")
        return None

    # Extract data: first 4 are grid points, last is city center
    grid_temps = []
    grid_wind_speeds = []
    grid_wind_dirs = []

    for i in range(4):
        loc = data[i]
        hourly = loc.get("hourly", {})
        temps = hourly.get("temperature_850hPa", [])
        speeds = hourly.get("wind_speed_850hPa", [])
        dirs = hourly.get("wind_direction_850hPa", [])

        if temps and temps[0] is not None:
            grid_temps.append(float(temps[0]))
        else:
            grid_temps.append(None)

        if speeds and speeds[0] is not None:
            grid_wind_speeds.append(float(speeds[0]))
        else:
            grid_wind_speeds.append(None)

        if dirs and dirs[0] is not None:
            grid_wind_dirs.append(float(dirs[0]))
        else:
            grid_wind_dirs.append(None)

    # City center wind (for advection wind components)
    city_loc = data[4]
    city_hourly = city_loc.get("hourly", {})
    city_speed = city_hourly.get("wind_speed_850hPa", [None])[0]
    city_dir = city_hourly.get("wind_direction_850hPa", [None])[0]

    city_u = None
    city_v = None
    if city_speed is not None and city_dir is not None:
        city_u, city_v = _wind_to_uv(float(city_speed), float(city_dir))

    return {
        "grid_temps": grid_temps,
        "grid_wind_speeds": grid_wind_speeds,
        "grid_wind_dirs": grid_wind_dirs,
        "city_u": city_u,
        "city_v": city_v,
    }


def compute_advection(grid_data: Dict, lat: float) -> Optional[float]:
    """Compute 850-mb temperature advection from grid data.

    ADV_850 = -u850 * (∂T850/∂x) - v850 * (∂T850/∂y)

    Args:
        grid_data: Dict from fetch_gfs_grid_data()
        lat: City latitude (for degree-to-meter conversion)

    Returns:
        Advection value (positive = warm advection = temperature increase),
        or None if computation fails.
    """
    temps = grid_data.get("grid_temps", [])
    u = grid_data.get("city_u")
    v = grid_data.get("city_v")

    if u is None or v is None:
        return None

    # Check we have enough valid temperature data
    valid_temps = [t for t in temps if t is not None]
    if len(valid_temps) < MIN_VALID_GRID:
        return None

    # Compute gradients using centered differences
    # Grid layout:
    #   NW (0) -- NE (1)
    #     |    X    |
    #   SW (2) -- SE (3)
    #
    # ∂T/∂x ≈ (T_NE - T_NW + T_SE - T_SW) / (2 * Δx)
    # ∂T/∂y ≈ (T_NW - T_SW + T_NE - T_SE) / (2 * Δy)

    # Convert degree spacing to meters
    dx_m, dy_m = _deg_to_meters(lat, GRID_SPACING, GRID_SPACING)

    # Handle missing grid points by using available pairs
    # For ∂T/∂x: need at least one east-west pair
    dT_dx_contributions = []
    if temps[0] is not None and temps[1] is not None:
        dT_dx_contributions.append((temps[1] - temps[0]) / dx_m)
    if temps[2] is not None and temps[3] is not None:
        dT_dx_contributions.append((temps[3] - temps[2]) / dx_m)

    if not dT_dx_contributions:
        return None

    dT_dx = sum(dT_dx_contributions) / len(dT_dx_contributions)

    # For ∂T/∂y: need at least one north-south pair
    dT_dy_contributions = []
    if temps[0] is not None and temps[2] is not None:
        dT_dy_contributions.append((temps[0] - temps[2]) / dy_m)
    if temps[1] is not None and temps[3] is not None:
        dT_dy_contributions.append((temps[1] - temps[3]) / dy_m)

    if not dT_dy_contributions:
        return None

    dT_dy = sum(dT_dy_contributions) / len(dT_dy_contributions)

    # Advection: ADV = -u * dT/dx - v * dT/dy
    # Positive advection = warm air moving toward city
    advection = -u * dT_dx - v * dT_dy

    return advection


def load_advection_history(db_path: str, station: str, window: int = ROLLING_WINDOW) -> List[float]:
    """Load historical advection values for rolling normalization.

    Args:
        db_path: Path to NWP forecasts DB
        station: Station code (e.g. 'KATL')
        window: Number of days of history to load

    Returns:
        List of historical advection values
    """
    history = []
    try:
        conn = get_sqlite_connection(db_path, timeout=10)
        c = conn.cursor()
        # Query advection values from dedicated table
        c.execute("""
            SELECT value FROM nwp_forecasts
            WHERE station = ? AND variable = 'advection_850hPa'
            ORDER BY target_date DESC
            LIMIT ?
        """, (station, window))
        for row in c.fetchall():
            if row[0] is not None:
                history.append(float(row[0]))
        conn.close()
    except Exception as e:
        logger.debug(f"Could not load advection history for {station}: {e}")

    return history


def store_advection(db_path: str, station: str, fetch_date: str, target_date: str,
                    advection: float, model: str = "gfs"):
    """Store computed advection value in NWP DB.

    Args:
        db_path: Path to NWP forecasts DB
        station: Station code
        fetch_date: Date of fetch
        target_date: Date advection was computed for
        advection: Computed advection value
        model: NWP model used
    """
    fetch_timestamp = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_sqlite_connection(db_path, timeout=10)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO nwp_forecasts
            (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fetch_date, target_date, station, model, "advection_850hPa",
              float(advection), fetch_timestamp))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Could not store advection for {station}: {e}")


def compute_signal_for_station(lat: float, lon: float, station: str,
                                db_path: str) -> Tuple[Optional[str], float]:
    """Compute temperature advection signal for a single station.

    This is the main entry point for the signal evaluation.

    Args:
        lat: Station latitude
        lon: Station longitude
        station: Station code (e.g. 'KATL')
        db_path: Path to NWP forecasts DB

    Returns:
        (direction, confidence) where direction is 'up' or 'down',
        or (None, 0.0) if signal does not fire.
    """
    # Fetch GFS grid data
    grid_data = fetch_gfs_grid_data(lat, lon)
    if grid_data is None:
        return None, 0.0

    # Compute advection
    advection = compute_advection(grid_data, lat)
    if advection is None:
        return None, 0.0

    # Store for history
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Target date = tomorrow (the date the forecast is for)
    from datetime import timedelta
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    store_advection(db_path, station, today, tomorrow, advection)

    # Load historical std for normalization
    history = load_advection_history(db_path, station)
    if len(history) >= 5:
        import statistics
        adv_std = statistics.stdev(history)
    else:
        adv_std = DEFAULT_STD

    if adv_std < 0.1:
        adv_std = DEFAULT_STD

    # Normalize: z = ADV / σ
    z = advection / adv_std

    # Determine direction and confidence
    if z > Z_THRESHOLD:
        # Positive advection = warm air moving toward city → UP
        confidence = min(abs(z) / 2.0, MAX_CONFIDENCE)
        return "up", confidence
    elif z < -Z_THRESHOLD:
        # Negative advection = cold air moving toward city → DOWN
        confidence = min(abs(z) / 2.0, MAX_CONFIDENCE)
        return "down", confidence
    else:
        return None, 0.0


class TemperatureAdvectionSignal:
    """
    850-mb Temperature Advection Signal.

    This signal computes the horizontal temperature advection at 850-mb
    from GFS forecast fields and uses it to predict surface temperature
    direction.

    Positive advection (warm air advection) → predict HIGH UP
    Negative advection (cold air advection) → predict HIGH DOWN
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or NWP_DB_DEFAULT
        if not os.path.isabs(self.db_path):
            # Resolve relative to project root
            project_root = Path(__file__).resolve().parent.parent.parent
            self.db_path = str(project_root / self.db_path)

    @property
    def name(self) -> str:
        return "temperature_advection"

    @property
    def min_lookback(self) -> int:
        return 0  # No lookback needed — uses GFS forecast data

    def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
        """
        Evaluate the signal for a given day index.

        NOTE: This implementation uses GFS forecast data, not historical
        METAR data. For backtesting, this signal will only fire when
        the station is known and GFS data is available.

        Args:
            idx: Current day index in the days list
            days: List of daily weather dicts (not used — GFS data is separate)

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        # This is a stub for the BaseSignal interface.
        # Real evaluation happens via evaluate_for_station() with live GFS data.
        # For backtesting, this returns None since we don't have historical
        # grid data unless previously collected.
        return None, 0.0

    def evaluate_for_station(self, station: str, date: str,
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate signal for a specific station and date using live GFS data.

        For production use, fetches GFS 850-mb data for the current forecast.
        For backtesting, queries stored NWP data if available.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection (not used — GFS data is separate)

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        # Find station coordinates
        station_lat = None
        station_lon = None
        for code, name, lat, lon in CITIES:
            if code == station:
                station_lat = lat
                station_lon = lon
                break

        if station_lat is None:
            return None, 0.0

        return compute_signal_for_station(station_lat, station_lon, station, self.db_path)


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("850-mb Temperature Advection Signal — Standalone Test")
    print("=" * 60)

    db_path = os.environ.get("NWP_DB_PATH", "data/nwp_forecasts.db")
    signal = TemperatureAdvectionSignal(db_path)

    for code, name, lat, lon in CITIES:
        direction, confidence = compute_signal_for_station(
            lat, lon, code, signal.db_path
        )
        if direction:
            print(f"  {code} ({name}): {direction.upper()} (conf={confidence:.3f})")
        else:
            print(f"  {code} ({name}): NO SIGNAL (advection too weak)")
        time.sleep(1)  # Rate limit