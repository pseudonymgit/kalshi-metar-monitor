#!/usr/bin/env python3
"""
ERA5 Upper-Air Backfill — CDS API (Copernicus Data Store)

Downloads historical ERA5 pressure-level data (850-mb temperature, u/v wind,
500-mb geopotential) from the Copernicus Data Store for all 20 Kalshi weather
stations, computes 850-mb temperature advection, and stores everything in the
existing NWP forecast database.

Designed to run as a standalone script — NO AGENT INVOLVED.
Run manually: python3 scripts/era5_upper_air_backfill.py

Prerequisites:
  - CDS API key configured (see scripts/setup_cds_api.sh)
  - Python packages: cdsapi, xarray, numpy, netCDF4

Date range: 2025-01-01 to present (matches existing NWP collection period).
Supports resumption: checks DB for existing data before downloading.
"""

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

# ── Configuration ────────────────────────────────────────────────────────
NWP_DB_PATH_DEFAULT = "data/nwp_forecasts.db"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# CDS API parameters
CDS_DATASET = "reanalysis-era5-pressure-levels"
CDS_VARIABLES = ["temperature", "u_component_of_wind", "v_component_of_wind", "geopotential"]
CDS_PRESSURE_LEVELS = ["850", "500"]
CDS_GRID = [1.0, 1.0]  # 1.0° × 1.0° resolution (reduced from 0.5° to fit CDS free tier cost limits)

# Bounding box half-size around each station (degrees)
# At 0.25° resolution, a 1°×1° area gives ~5×5 grid points — enough for gradients
AREA_HALF_SIZE = 0.5  # degrees

# The 20 Kalshi weather market stations
STATIONS = [
    ("KATL", "Atlanta", 33.64, -84.43),
    ("KBOS", "Boston", 42.37, -71.01),
    ("KDEN", "Denver", 39.86, -104.67),
    ("KDFW", "Dallas-Fort Worth", 32.90, -97.04),
    ("KDTW", "Detroit", 42.21, -83.35),
    ("KEWR", "Newark", 40.69, -74.17),
    ("KIAH", "Houston", 29.98, -95.36),
    ("KJFK", "New York JFK", 40.64, -73.78),
    ("KLAS", "Las Vegas", 36.08, -115.16),
    ("KLAX", "Los Angeles", 33.94, -118.41),
    ("KMIA", "Miami", 25.80, -80.29),
    ("KMSP", "Minneapolis", 44.88, -93.22),
    ("KNYC", "New York Central Park", 40.71, -74.01),
    ("KORD", "Chicago O'Hare", 41.98, -87.90),
    ("KPHL", "Philadelphia", 39.87, -75.24),
    ("KPHX", "Phoenix", 33.43, -112.01),
    ("KSAN", "San Diego", 32.73, -117.17),
    ("KSEA", "Seattle", 47.45, -122.31),
    ("KSFO", "San Francisco", 37.62, -122.38),
    ("KSLC", "Salt Lake City", 40.79, -111.98),
]

# DB variable names for upper-air fields
VAR_TEMP_850 = "temperature_850hPa"
VAR_U_850 = "wind_u_850hPa"
VAR_V_850 = "wind_v_850hPa"
VAR_GEO_500 = "geopotential_500hPa"
VAR_ADVECTION = "temperature_advection_850hPa"
UPPER_AIR_VARS = [VAR_TEMP_850, VAR_U_850, VAR_V_850, VAR_GEO_500, VAR_ADVECTION]

# CDS model name in DB
MODEL_NAME = "ERA5"

# Retry configuration
MAX_RETRIES = 5
RETRY_BASE_DELAY = 60   # seconds
MAX_RETRY_DELAY = 600  # 10 minutes
MIN_CDS_INTERVAL = 5.0  # seconds between CDS requests


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="ERA5 Upper-Air Backfill from CDS API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/era5_upper_air_backfill.py
  python3 scripts/era5_upper_air_backfill.py --station KATL --months 1
  python3 scripts/era5_upper_air_backfill.py --dry-run
  python3 scripts/era5_upper_air_backfill.py --start-date 2025-06-01 --end-date 2025-08-01
        """,
    )
    parser.add_argument(
        "--db-path",
        help=f"Database file path (default: {NWP_DB_PATH_DEFAULT})",
    )
    parser.add_argument(
        "--start-date",
        default="2025-01-01",
        help="Start date YYYY-MM-DD (default: 2025-01-01)",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--station",
        default=None,
        help="Specific station code (e.g., KATL) to process",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help="Number of months to process from start date (overrides --end-date)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without downloading or writing",
    )
    parser.add_argument(
        "--skip-cds",
        action="store_true",
        help="Skip CDS download; only validate existing data (for debugging)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if data exists in DB",
    )
    parser.add_argument(
        "--cds-delay",
        type=float,
        default=5.0,
        help="Minimum delay between CDS requests in seconds (default: 5.0)",
    )
    return parser.parse_args()


def get_db_path(args):
    """Resolve the database path from args, env, or default."""
    if args.db_path:
        path = Path(args.db_path).absolute()
    elif os.environ.get("NWP_DB_PATH"):
        path = Path(os.environ["NWP_DB_PATH"]).absolute()
    else:
        path = (PROJECT_ROOT / NWP_DB_PATH_DEFAULT).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_db(db_path):
    """Initialize the NWP forecasts database and verify schema."""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS nwp_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            station TEXT NOT NULL,
            model TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            fetch_timestamp TEXT NOT NULL,
            UNIQUE(fetch_date, target_date, station, model, variable)
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_lookup
        ON nwp_forecasts(target_date, station, model, variable)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_nwp_era5
        ON nwp_forecasts(station, model, variable, target_date)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            model TEXT NOT NULL,
            stations_fetched INTEGER,
            variables_fetched INTEGER,
            errors TEXT,
            fetch_timestamp TEXT NOT NULL
        )
    """)

    conn.commit()
    return conn


def get_existing_date_counts(conn, station):
    """
    Get the count of existing dates for each variable for this station.
    Returns dict: variable_name -> count
    """
    c = conn.cursor()
    c.execute(
        """
        SELECT variable, COUNT(DISTINCT target_date) FROM nwp_forecasts
        WHERE station = ? AND model = ?
        GROUP BY variable
        """,
        (station, MODEL_NAME),
    )
    return {row[0]: row[1] for row in c.fetchall()}


def get_month_ranges(start_date, end_date):
    """
    Generate (year, month, first_day, last_day) tuples for each month
    in the date range.
    """
    ranges = []
    current = start_date.replace(day=1)
    while current <= end_date:
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        last_day = next_month - timedelta(days=1)
        month_end = min(last_day, end_date)
        ranges.append((current.year, current.month, current, month_end))
        current = next_month
    return ranges


def find_nearest_grid_point(lats, lons, target_lat, target_lon):
    """
    Find the indices of the nearest grid point to (target_lat, target_lon).

    Args:
        lats: 1D array of latitude values
        lons: 1D array of longitude values
        target_lat, target_lon: Target coordinates

    Returns:
        (lat_idx, lon_idx) indices into the grid
    """
    lat_idx = int(np.argmin(np.abs(lats - target_lat)))
    lon_idx = int(np.argmin(np.abs(lons - target_lon)))
    return lat_idx, lon_idx


def deg_to_meters(lat, dlon, dlat):
    """
    Convert degree differences to meters at given latitude.

    Args:
        lat: Latitude in degrees
        dlon: Longitude difference in degrees
        dlat: Latitude difference in degrees

    Returns:
        (dx_m, dy_m) in meters
    """
    lat_rad = math.radians(lat)
    dy = dlat * 111_320.0
    dx = dlon * 111_320.0 * math.cos(lat_rad)
    return dx, dy


def compute_temperature_advection(
    temp_grid, u_val, v_val, lats, lons, station_lat, station_lon
):
    """
    Compute 850-mb temperature advection from grid data.

    ADV_850 = -u850 * (dT/dx) - v850 * (dT/dy)

    Uses centered finite differences on the grid.

    Args:
        temp_grid: 2D numpy array of temperature at 850 hPa (K)
        u_val: U-component of wind at 850 hPa at station (m/s)
        v_val: V-component of wind at 850 hPa at station (m/s)
        lats: 1D array of latitude values
        lons: 1D array of longitude values
        station_lat, station_lon: Station coordinates

    Returns:
        Advection value (K/s), or None if computation fails
    """
    if temp_grid is None or u_val is None or v_val is None:
        return None
    if temp_grid.size < 4:
        return None

    lat_idx, lon_idx = find_nearest_grid_point(lats, lons, station_lat, station_lon)

    # dT/dx: use points east and west of station
    dT_dx = None
    if lon_idx > 0 and lon_idx < len(lons) - 1:
        t_east = float(temp_grid[lat_idx, lon_idx + 1])
        t_west = float(temp_grid[lat_idx, lon_idx - 1])
        if not (np.isnan(t_east) or np.isnan(t_west)):
            dlon = float(lons[lon_idx + 1] - lons[lon_idx - 1])
            dx_m, _ = deg_to_meters(station_lat, dlon, 0)
            if abs(dx_m) > 1.0:
                dT_dx = (t_east - t_west) / dx_m

    # Fallback: average across rows if center point fails
    if dT_dx is None:
        east_vals = []
        west_vals = []
        for offset in [-1, 0, 1]:
            li = lat_idx + offset
            if 0 <= li < len(lats) and lon_idx > 0 and lon_idx < len(lons) - 1:
                te = float(temp_grid[li, lon_idx + 1])
                tw = float(temp_grid[li, lon_idx - 1])
                if not (np.isnan(te) or np.isnan(tw)):
                    east_vals.append(te)
                    west_vals.append(tw)
        if east_vals and west_vals:
            dlon = float(lons[lon_idx + 1] - lons[lon_idx - 1])
            dx_m, _ = deg_to_meters(station_lat, dlon, 0)
            if abs(dx_m) > 1.0:
                dT_dx = (float(np.mean(east_vals)) - float(np.mean(west_vals))) / dx_m

    # dT/dy: use points north and south of station
    dT_dy = None
    if lat_idx > 0 and lat_idx < len(lats) - 1:
        t_north = float(temp_grid[lat_idx + 1, lon_idx])
        t_south = float(temp_grid[lat_idx - 1, lon_idx])
        if not (np.isnan(t_north) or np.isnan(t_south)):
            dlat = float(lats[lat_idx + 1] - lats[lat_idx - 1])
            _, dy_m = deg_to_meters(station_lat, 0, dlat)
            if abs(dy_m) > 1.0:
                dT_dy = (t_north - t_south) / dy_m

    # Fallback: average across columns
    if dT_dy is None:
        north_vals = []
        south_vals = []
        for offset in [-1, 0, 1]:
            li = lon_idx + offset
            if 0 <= li < len(lons) and lat_idx > 0 and lat_idx < len(lats) - 1:
                tn = float(temp_grid[lat_idx + 1, li])
                ts = float(temp_grid[lat_idx - 1, li])
                if not (np.isnan(tn) or np.isnan(ts)):
                    north_vals.append(tn)
                    south_vals.append(ts)
        if north_vals and south_vals:
            dlat = float(lats[lat_idx + 1] - lats[lat_idx - 1])
            _, dy_m = deg_to_meters(station_lat, 0, dlat)
            if abs(dy_m) > 1.0:
                dT_dy = (float(np.mean(north_vals)) - float(np.mean(south_vals))) / dy_m

    if dT_dx is None or dT_dy is None:
        return None

    # Advection: ADV = -u * dT/dx - v * dT/dy
    # Positive = warm air advection = temperature increase
    advection = -u_val * dT_dx - v_val * dT_dy
    return float(advection)


def cds_download_monthly(client, year, month, lat, lon, output_path):
    """
    Download ERA5 pressure-level data for one station one month via CDS API.

    Requests a small bounding box around the station to get enough grid
    points for gradient computation.

    Args:
        client: cdsapi.Client instance
        year: int
        month: int (1-12)
        lat, lon: Station coordinates
        output_path: Path to save the NetCDF file

    Returns:
        Path to the downloaded file, or None on failure
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build days list
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    first_day = datetime(year, month, 1)
    last_day = next_month - timedelta(days=1)
    days = [f"{d:02d}" for d in range(1, last_day.day + 1)]

    # Hourly time steps
    times = [f"{h:02d}:00" for h in range(24)]

    # Bounding box around station: [N, W, S, E]
    area = [lat + AREA_HALF_SIZE, lon - AREA_HALF_SIZE,
            lat - AREA_HALF_SIZE, lon + AREA_HALF_SIZE]

    # CDS free tier has cost limits per request. Split by variable + pressure level.
    # Download each combo separately, then merge.
    import xarray as xr
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    datasets = []
    for var in CDS_VARIABLES:
        for plevel in CDS_PRESSURE_LEVELS:
            var_request = {
                "product_type": "reanalysis",
                "format": "netcdf",
                "variable": [var],
                "pressure_level": [plevel],
                "year": str(year),
                "month": f"{month:02d}",
                "day": days,
                "time": times,
                "area": area,
                "grid": CDS_GRID,
            }
            var_path = output_path.parent / f"{output_path.stem}_{var}_{plevel}hPa.nc"
            try:
                print(f"    Downloading {var} @ {plevel}hPa...")
                client.retrieve(CDS_DATASET, var_request, str(var_path))
                if var_path.exists() and var_path.stat().st_size > 1000:
                    ds = xr.open_dataset(var_path)
                    datasets.append(ds)
                else:
                    print(f"    Warning: {var} @ {plevel}hPa file too small, skipping")
            except Exception as e:
                print(f"    CDS download failed for {var} @ {plevel}hPa: {e}")
                return None
            time.sleep(3.0)  # polite delay between requests
    
    if not datasets:
        print(f"    No data downloaded for {year}-{month:02d}")
        return None
    
    # Merge all datasets into one file
    merged = xr.merge(datasets)
    merged.to_netcdf(str(output_path))
    for ds in datasets:
        ds.close()
    # Clean up temp files
    for var in CDS_VARIABLES:
        for plevel in CDS_PRESSURE_LEVELS:
            tmp = output_path.parent / f"{output_path.stem}_{var}_{plevel}hPa.nc"
            if tmp.exists():
                tmp.unlink()
    
    print(f"    Merged to {output_path.name}")
    return output_path


def process_netcdf_file(nc_path, station_code, station_lat, station_lon):
    """
    Process a CDS NetCDF file and extract daily mean values.

    Args:
        nc_path: Path to the downloaded NetCDF file
        station_code: ICAO station code
        station_lat, station_lon: Station coordinates

    Returns:
        List of (target_date, variable, value) tuples
    """
    import xarray as xr

    results = []

    # Open the NetCDF file
    ds = xr.open_dataset(str(nc_path))

    # Find the nearest grid point to the station
    lats = ds.latitude.values if "latitude" in ds.coords else ds.lat.values
    lons = ds.longitude.values if "longitude" in ds.coords else ds.lon.values

    # Normalize longitudes if needed
    if np.any(lons > 180):
        lons = np.where(lons > 180, lons - 360, lons)

    lat_idx, lon_idx = find_nearest_grid_point(lats, lons, station_lat, station_lon)

    # Extract time coordinates
    times = ds.valid_time.values if "valid_time" in ds.coords else ds.time.values

    # Check if we have a 2D or 1D representation of the grid
    # CDS NetCDF may have lat/lon as 1D or 2D coordinates
    has_2d_lat = lats.ndim == 2
    has_2d_lon = lons.ndim == 2

    # Group indices by date
    date_to_indices = {}
    for i, t in enumerate(times):
        dt = np.datetime64(t).astype("datetime64[D]")
        date_str = str(dt)
        if date_str not in date_to_indices:
            date_to_indices[date_str] = []
        date_to_indices[date_str].append(i)

    # Extract the temperature grid (for advection computation)
    # We need all grid points, not just the nearest
    temp_grid_ds = ds.get("t", ds.get("temperature", None))
    temp_grid_values = None
    if temp_grid_ds is not None:
        # Select 850 hPa level
        if "pressure_level" in temp_grid_ds.dims or "plev" in temp_grid_ds.dims:
            try:
                temp_grid_850 = temp_grid_ds.sel(pressure_level=85000, method="nearest")
            except (ValueError, KeyError):
                try:
                    temp_grid_850 = temp_grid_ds.sel(plev=85000, method="nearest")
                except (ValueError, KeyError):
                    temp_grid_850 = temp_grid_ds.isel(pressure_level=0)
            temp_grid_values = temp_grid_850.values  # (time, lat, lon) or (time, y, x)
        else:
            temp_grid_values = temp_grid_ds.values

    # Extract the 3D grid (time × lat × lon) for each variable
    def extract_series(ds, var_name, pressure_level=None):
        """Extract time series at nearest grid point."""
        if var_name not in ds.data_vars:
            return None
        da = ds[var_name]
        # Select pressure level if needed
        if pressure_level is not None:
            if "pressure_level" in da.dims:
                da = da.sel(pressure_level=pressure_level, method="nearest")
            elif "plev" in da.dims:
                da = da.sel(plev=pressure_level, method="nearest")
        # Select nearest point
        if has_2d_lat:
            # 2D coordinate: select by index
            da = da.isel(latitude=lat_idx, longitude=lon_idx)
        else:
            # 1D coordinate: select by nearest value
            da = da.sel(latitude=float(lats[lat_idx]),
                        longitude=float(lons[lon_idx]),
                        method="nearest")
        return da.values

    # Extract time series for each variable at nearest point
    temp_series = extract_series(ds, "t", 85000)
    if temp_series is None:
        temp_series = extract_series(ds, "temperature", 85000)
    u_series = extract_series(ds, "u", 85000)
    if u_series is None:
        u_series = extract_series(ds, "u_component_of_wind", 85000)
    v_series = extract_series(ds, "v", 85000)
    if v_series is None:
        v_series = extract_series(ds, "v_component_of_wind", 85000)
    z_series = extract_series(ds, "z", 50000)
    if z_series is None:
        z_series = extract_series(ds, "geopotential", 50000)

    # For each date, compute daily means
    for date_str, indices in date_to_indices.items():
        valid_indices = [i for i in indices if i < len(times)]
        if not valid_indices:
            continue

        # Temperature at 850 hPa (convert from K to °C)
        if temp_series is not None:
            vals = [float(temp_series[i]) for i in valid_indices
                    if i < len(temp_series) and not np.isnan(temp_series[i])]
            if vals:
                mean_temp_k = float(np.mean(vals))
                mean_temp_c = mean_temp_k - 273.15
                results.append((date_str, VAR_TEMP_850, mean_temp_c))

        # U-wind at 850 hPa (m/s)
        if u_series is not None:
            vals = [float(u_series[i]) for i in valid_indices
                    if i < len(u_series) and not np.isnan(u_series[i])]
            if vals:
                results.append((date_str, VAR_U_850, float(np.mean(vals))))

        # V-wind at 850 hPa (m/s)
        if v_series is not None:
            vals = [float(v_series[i]) for i in valid_indices
                    if i < len(v_series) and not np.isnan(v_series[i])]
            if vals:
                results.append((date_str, VAR_V_850, float(np.mean(vals))))

        # Geopotential at 500 hPa (convert from m²/s² to geopotential meters)
        if z_series is not None:
            vals = [float(z_series[i]) for i in valid_indices
                    if i < len(z_series) and not np.isnan(z_series[i])]
            if vals:
                mean_z = float(np.mean(vals)) / 9.80665  # m²/s² → gpm
                results.append((date_str, VAR_GEO_500, mean_z))

        # Temperature advection computation
        if (u_series is not None and v_series is not None
                and temp_grid_values is not None and valid_indices):
            mid_idx = valid_indices[len(valid_indices) // 2]

            u_mid = float(u_series[mid_idx]) if mid_idx < len(u_series) else None
            v_mid = float(v_series[mid_idx]) if mid_idx < len(v_series) else None

            if u_mid is not None and v_mid is not None:
                # Get the temperature grid at this time step
                if temp_grid_values.ndim == 3:
                    time_grid = temp_grid_values[mid_idx]
                else:
                    time_grid = temp_grid_values

                if time_grid is not None:
                    advection = compute_temperature_advection(
                        np.array(time_grid, dtype=float),
                        u_mid, v_mid,
                        np.array(lats, dtype=float),
                        np.array(lons, dtype=float),
                        station_lat, station_lon,
                    )
                    if advection is not None:
                        results.append((date_str, VAR_ADVECTION, advection))

    ds.close()
    return results


def cds_download_with_retry(client, year, month, lat, lon, output_path, dry_run=False):
    """Download with retry and exponential backoff for CDS rate limits."""
    if dry_run:
        return True

    for attempt in range(MAX_RETRIES):
        try:
            result = cds_download_monthly(client, year, month, lat, lon, output_path)
            if result is not None:
                return result
            print(f"    Download returned None, retrying...")
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
                print(f"    CDS error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                print(f"    Retrying in {delay}s...")
                time.sleep(delay)
            else:
                print(f"    CDS failed after {MAX_RETRIES} attempts: {e}")
                return None
    return None


def store_in_db(conn, results, station, fetch_date_str, fetch_timestamp):
    """Store extracted values in the NWP database."""
    c = conn.cursor()
    stored = 0
    errors = []

    for target_date, variable, value in results:
        if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
            continue
        try:
            c.execute(
                """
                INSERT OR REPLACE INTO nwp_forecasts
                (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fetch_date_str, target_date, station, MODEL_NAME,
                 variable, float(value), fetch_timestamp),
            )
            stored += 1
        except Exception as e:
            errors.append(f"DB error ({variable}, {target_date}): {e}")

    conn.commit()
    return stored, errors


def main():
    """Main entry point."""
    args = parse_args()
    db_path = get_db_path(args)

    # Determine date range
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    elif args.months:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        month = start.month + args.months
        year = start.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        end_date = datetime(year, month, 1).date() - timedelta(days=1)
    else:
        end_date = datetime.now(timezone.utc).date()

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()

    if start_date > end_date:
        print(f"Error: start date {start_date} is after end date {end_date}")
        sys.exit(1)

    # Filter stations
    stations = STATIONS
    if args.station:
        stations = [s for s in stations if s[0] == args.station.upper()]
        if not stations:
            print(f"Error: Station '{args.station}' not found")
            print(f"Available stations: {', '.join(s[0] for s in STATIONS)}")
            sys.exit(1)

    # Generate month ranges
    month_ranges = get_month_ranges(start_date, end_date)
    total_months = len(month_ranges)
    total_stations = len(stations)

    # Header
    print("=" * 72)
    print("  ERA5 Upper-Air Backfill — CDS API")
    print("=" * 72)
    print(f"  Database:  {db_path}")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Months:     {total_months}")
    print(f"  Stations:   {total_stations} ({', '.join(s[0] for s in stations)})")
    print(f"  Variables:  {', '.join(UPPER_AIR_VARS)}")
    if args.dry_run:
        print(f"  Mode:       DRY RUN (no downloads, no DB writes)")
    if args.skip_cds:
        print(f"  Mode:       SKIP CDS (validate existing data only)")
    if args.force:
        print(f"  Mode:       FORCE (re-download existing data)")
    print("=" * 72)
    print()

    # Initialize DB
    conn = init_db(db_path) if not args.dry_run else None

    # Initialize CDS client
    cds_client = None
    if not args.skip_cds and not args.dry_run:
        try:
            import cdsapi
            cds_client = cdsapi.Client()
            print("  CDS API client initialized.")
        except Exception as e:
            print(f"  Error: Could not initialize CDS API client: {e}")
            print("  Run scripts/setup_cds_api.sh to configure CDS access.")
            if conn:
                conn.close()
            sys.exit(1)

    fetch_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_timestamp = datetime.now(timezone.utc).isoformat()

    # Stats
    total_stored = 0
    total_errors = []
    total_downloaded = 0
    total_skipped = 0
    total_failed = 0

    # Temp directory for NetCDF downloads
    tmp_dir = PROJECT_ROOT / "data" / "tmp_cds_downloads"
    if not args.dry_run:
        tmp_dir.mkdir(parents=True, exist_ok=True)

    # Process each station
    for st_idx, (station_code, station_name, lat, lon) in enumerate(stations, 1):
        print(f"\n{'─' * 72}")
        print(f"  Station [{st_idx}/{total_stations}]: {station_code} ({station_name})")
        print(f"  Coordinates: {lat}°N, {lon}°E")
        print(f"{'─' * 72}")

        # Check existing data
        existing = {}
        if conn and not args.force:
            existing = get_existing_date_counts(conn, station_code)
            if existing:
                total_vars = len(UPPER_AIR_VARS)
                existing_vars = sum(1 for v in UPPER_AIR_VARS if v in existing)
                print(f"  Existing data: {existing_vars}/{total_vars} variables present")
                for var in UPPER_AIR_VARS:
                    count = existing.get(var, 0)
                    status = "✓" if count > 0 else " "
                    print(f"    [{status}] {var}: {count} days")

        # Process each month
        for m_idx, (year, month, m_start, m_end) in enumerate(month_ranges, 1):
            month_label = f"{year}-{month:02d}"
            output_path = tmp_dir / f"era5_{station_code}_{year}_{month:02d}.nc"

            # Check if already in DB
            month_days = (m_end - m_start).days + 1
            existing_temp = existing.get(VAR_TEMP_850, 0)
            existing_adv = existing.get(VAR_ADVECTION, 0)

            if conn and not args.force:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT COUNT(DISTINCT target_date) FROM nwp_forecasts
                    WHERE station = ? AND model = ? AND variable = ?
                    AND target_date >= ? AND target_date <= ?
                    """,
                    (station_code, MODEL_NAME, VAR_TEMP_850,
                     m_start.isoformat(), m_end.isoformat()),
                )
                month_temp_days = c.fetchone()[0]

                c.execute(
                    """
                    SELECT COUNT(DISTINCT target_date) FROM nwp_forecasts
                    WHERE station = ? AND model = ? AND variable = ?
                    AND target_date >= ? AND target_date <= ?
                    """,
                    (station_code, MODEL_NAME, VAR_ADVECTION,
                     m_start.isoformat(), m_end.isoformat()),
                )
                month_adv_days = c.fetchone()[0]

                if month_temp_days >= month_days and month_adv_days >= month_days:
                    total_skipped += 1
                    print(f"  [{m_idx}/{total_months}] {month_label} ... SKIPPED (already in DB)")
                    continue

            # Dry run
            if args.dry_run:
                print(f"  [{m_idx}/{total_months}] {month_label} ... (DRY RUN - would download)")
                total_skipped += 1
                continue

            # Skip CDS mode
            if args.skip_cds:
                print(f"  [{m_idx}/{total_months}] {month_label} ... SKIP CDS")
                total_skipped += 1
                continue

            # Download
            print(f"  [{m_idx}/{total_months}] {month_label} ... downloading...", end=" ", flush=True)
            start_time = time.time()

            result = cds_download_with_retry(
                cds_client, year, month, lat, lon, output_path, dry_run=False
            )

            if result is None:
                print(f"FAILED")
                total_failed += 1
                total_errors.append(f"{station_code}/{month_label}: CDS download failed")
                continue

            dl_time = time.time() - start_time
            file_size = output_path.stat().st_size / (1024 * 1024)
            print(f"OK ({file_size:.1f} MB, {dl_time:.0f}s)")

            # Process the NetCDF file
            print(f"           processing...", end=" ", flush=True)
            try:
                # Check if we have the required imports
                import xarray
                extracted = process_netcdf_file(
                    output_path, station_code, lat, lon
                )
            except ImportError as e:
                print(f"FAILED: missing dependency: {e}")
                total_failed += 1
                total_errors.append(f"{station_code}/{month_label}: {e}")
                continue

            print(f"{len(extracted)} values")

            if extracted:
                stored, errors = store_in_db(
                    conn, extracted, station_code, fetch_date_str, fetch_timestamp
                )
                total_stored += stored
                total_errors.extend(errors)
                total_downloaded += 1

                if errors:
                    for err in errors[:3]:
                        print(f"    Error: {err}")
            else:
                print(f"    Warning: No data extracted from NetCDF")
                total_failed += 1

            # Clean up the NetCDF file to save space
            try:
                output_path.unlink()
            except Exception:
                pass

            # Rate limit delay between requests
            time.sleep(args.cds_delay)

        # Store fetch_log entry
        if conn and not args.dry_run and total_downloaded > 0:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO fetch_log
                (fetch_date, model, stations_fetched, variables_fetched, errors, fetch_timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fetch_date_str, MODEL_NAME, 1, total_stored,
                 "; ".join(total_errors[-5:]) if total_errors else None, fetch_timestamp),
            )
            conn.commit()

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  BACKFILL COMPLETE")
    print("=" * 72)
    print(f"  Values stored:   {total_stored}")
    print(f"  Downloads:       {total_downloaded}")
    print(f"  Skipped:         {total_skipped}")
    print(f"  Failed:          {total_failed}")
    print(f"  Errors:          {len(total_errors)}")

    if total_errors:
        print()
        print("  Recent errors:")
        for err in total_errors[-5:]:
            print(f"    - {err}")

    # Show DB stats
    if conn and not args.dry_run:
        c = conn.cursor()
        for var in UPPER_AIR_VARS:
            c.execute(
                """
                SELECT COUNT(DISTINCT station), COUNT(DISTINCT target_date)
                FROM nwp_forecasts
                WHERE model = ? AND variable = ?
                """,
                (MODEL_NAME, var),
            )
            stns, days = c.fetchone()
            print(f"  {var}: {stns} stations, {days} days")

        c.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE model = ?", (MODEL_NAME,))
        total_era5 = c.fetchone()[0]
        print(f"\n  Total ERA5 rows in DB: {total_era5}")

    if conn:
        conn.close()

    print()
    print(f"  Finished at {datetime.now(timezone.utc).isoformat()}")
    print()


if __name__ == "__main__":
    main()
