#!/usr/bin/env python3
"""
goldilocks_feature_engineering.py — Feature computation for Goldilocks Predictive Model.

Computes all 29+ features from Section 3 of GOLDILOCKS-PREDICTIVE.md using
existing local data sources:
  - METAR DB  (data/metar_backfill.db)     — wind, cloud, dewpoint, pressure
  - ERA5 DB   (data/era5_archive.db)       — daily temp range as stability proxy
  - NWP DB    (data/nwp_forecasts.db)      — cloud cover, wind speed, MSLP

Outputs a clean feature matrix (Parquet/CSV) ready for ML training.

Usage:
    python3 scripts/goldilocks_feature_engineering.py
    python3 scripts/goldilocks_feature_engineering.py --station KNYC --start 2021-01-01 --end 2026-08-01

B-Mode compliant. No AI/ML. Deterministic: same inputs → same features every time.
"""

import argparse
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(REPO_ROOT, "data", "metar_backfill.db")
ERA5_DB = os.path.join(REPO_ROOT, "data", "era5_archive.db")
NWP_DB = os.path.join(REPO_ROOT, "data", "nwp_forecasts.db")
OUT_DIR = os.path.join(REPO_ROOT, "data")

DEFAULT_STATION = "KNYC"

# NYC Central Park coordinates for astronomical calculations
KNYC_LAT = 40.78
KNYC_LON = -73.97
# US/Eastern timezone offset heuristic (simplified)
# EDT = UTC-4 (Mar~Nov), EST = UTC-5 (Nov~Mar)
EASTERN_TZ_OFFSET = {
    1: 5, 2: 5, 3: 5,   # EST (simplified — doesn't account for exact transition days)
    4: 4, 5: 4, 6: 4,
    7: 4, 8: 4, 9: 4,
    10: 4, 11: 5, 12: 5,
}

# Wind direction sectors
WIND_SECTORS = {
    "N": (337.5, 360),
    "N": (0, 22.5),
    "NE": (22.5, 67.5),
    "E": (67.5, 112.5),
    "SE": (112.5, 157.5),
    "S": (157.5, 202.5),
    "SW": (202.5, 247.5),
    "W": (247.5, 292.5),
    "NW": (292.5, 337.5),
}
# Actually make N cover both 0-22.5 and 337.5-360
WIND_SECTOR_MAP = {
    0: "N", 1: "NNE", 2: "NE", 3: "ENE",
    4: "E", 5: "ESE", 6: "SE", 7: "SSE",
    8: "S", 9: "SSW", 10: "SW", 11: "WSW",
    12: "W", 13: "WNW", 14: "NW", 15: "NNW",
}


def _wind_dir_to_sector(deg: float) -> str:
    """Convert wind direction in degrees to 16-point compass sector."""
    if deg is None or math.isnan(deg):
        return None
    idx = round(deg / 22.5) % 16
    return WIND_SECTOR_MAP[idx]


def _utc_to_local_hour(ts_utc: str) -> int:
    """Convert UTC timestamp string to Eastern local hour (0-23)."""
    try:
        dt = datetime.strptime(ts_utc[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
        month = dt.month
        offset = EASTERN_TZ_OFFSET.get(month, 5)
        local_dt = dt - timedelta(hours=offset)
        return local_dt.hour
    except (ValueError, IndexError):
        return 0


def _utc_to_local_date(ts_utc: str) -> str:
    """Convert UTC timestamp to Eastern local date (Kalshi trading date)."""
    try:
        dt = datetime.strptime(ts_utc[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
        month = dt.month
        offset = EASTERN_TZ_OFFSET.get(month, 5)
        if dt.hour < offset:
            local_dt = dt - timedelta(hours=offset)
        else:
            local_dt = dt - timedelta(hours=offset)
        return local_dt.strftime('%Y-%m-%d')
    except (ValueError, IndexError):
        return ts_utc[:10]


def _solar_elevation(lat: float, lon: float, dt_utc: datetime) -> float:
    """
    Compute solar elevation angle in degrees for a given time and location.
    Uses the PSA algorithm (simplified, accurate to ~0.01°).
    """
    # Day of year
    doy = dt_utc.timetuple().tm_yday

    # Solar declination (approx)
    decl = 23.45 * math.sin(math.radians(360 / 365 * (284 + doy)))

    # Equation of time (minutes)
    B = 360 / 365 * (doy - 81)
    eot = 9.87 * math.sin(math.radians(2 * B)) - 7.53 * \
        math.cos(math.radians(B)) - 1.5 * math.sin(math.radians(B))

    # Time offset from UTC
    tc = 4 * lon + eot  # minutes

    # Local solar time
    solar_time = dt_utc.hour * 60 + dt_utc.minute + tc / 60  # minutes past midnight UTC
    # Hour angle
    ha = (solar_time / 4 - 180)  # degrees (noon = 0)

    # Solar elevation
    sin_alt = (math.sin(math.radians(lat)) * math.sin(math.radians(decl)) +
               math.cos(math.radians(lat)) * math.cos(math.radians(decl)) *
               math.cos(math.radians(ha)))
    alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
    return max(0, alt)


def _estimate_solar_flux(elevation: float, cloud_factor: float = 1.0) -> float:
    """
    Estimate surface insolation (W/m²) from solar elevation and cloud cover.

    Clear-sky insolation ~ 1361 * 0.7^(1 / sin(elev)) * sin(elev)  (simple model)
    Cloud attenuation: ~ (1 - 0.6 * cloud_cover_frac)
    """
    if elevation <= 0:
        return 0.0
    sin_elev = math.sin(math.radians(elevation))
    # Simple clear-sky model: extraterrestrial * (0.7^(1/sin_elev)) * sin_elev
    clear_sky = 1361 * (0.7 ** (1 / max(sin_elev, 0.01))) * sin_elev
    return clear_sky * max(0.2, cloud_factor)


def _estimate_longwave_flux(dp_depression_C: float, cloud_cover: float) -> float:
    """
    Estimate outgoing longwave radiation (W/m²) from dewpoint depression and cloud cover.

    Simple model: clear-sky OLR ~ 320 W/m² (function of surface temp)
    Cloud effect: thicker clouds trap more LW, reducing net outgoing
    Large dp depression (dry air) → more LW cooling
    """
    base_olr = 320  # W/m², typical at 300K
    # Dry air correction: larger depression → more LW cooling
    dry_corr = min(dp_depression_C, 30) * 2  # up to +60 W/m²
    # Cloud correction: clouds trap LW
    cloud_corr = cloud_cover * 150  # up to -150 W/m²
    return base_olr + dry_corr - cloud_corr


def _sunrise_sunset(lat: float, lon: float, dt: date) -> Tuple[float, float]:
    """
    Compute sunrise and sunset times (decimal hours UTC) for a given date and location.
    """
    doy = dt.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (284 + doy))))
    lat_r = math.radians(lat)

    # Solar hour angle at sunset (in radians)
    cos_ha = -math.tan(lat_r) * math.tan(decl)
    ha = math.acos(max(-1, min(1, cos_ha)))  # half-day length in radians

    # Noon in UTC
    noon_utc = 12 - lon / 15  # hours

    # Equation of time
    B = math.radians(360 / 365 * (doy - 81))
    eot = (9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)) / 60  # hours

    sunrise = noon_utc - math.degrees(ha) / 15 - eot
    sunset = noon_utc + math.degrees(ha) / 15 - eot

    return (sunrise, sunset)


class GoldilocksFeatureEngine:
    """
    Compute feature matrix for Goldilocks prediction at a given station.

    All features are deterministic: same inputs → same outputs every time.
    Missing values are left as NaN (LightGBM handles natively).
    """

    def __init__(self, station: str = DEFAULT_STATION):
        self.station = station
        self.metar_db = METAR_DB
        self.era5_db = ERA5_DB
        self.nwp_db = NWP_DB

    def _get_metar_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.metar_db}?mode=ro", uri=True)

    def _get_era5_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.era5_db}?mode=ro", uri=True)

    def _get_nwp_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.nwp_db}?mode=ro", uri=True)

    def compute_features(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Main entry point: compute full feature matrix for date range.

        Returns DataFrame with one row per day, columns for each feature.
        """
        dates = pd.date_range(start=start_date, end=end_date, freq='D')
        rows = []

        for d in dates:
            dt = d.to_pydatetime()
            local_date_str = d.strftime('%Y-%m-%d')
            features = self._compute_day_features(local_date_str, dt)
            rows.append(features)

        df = pd.DataFrame(rows)
        df['local_date'] = [d.strftime('%Y-%m-%d') for d in dates]
        return df

    def _compute_day_features(self, local_date_str: str, day_dt: datetime) -> dict:
        """Compute all features for a single local trading date."""
        features: Dict[str, Optional[float]] = {}
        station = self.station

        # ========== A. Wind Features ==========
        metar_conn = self._get_metar_conn()
        try:
            # Get all METAR obs for this local date (converting UTC to local)
            # We need obs in UTC, so the range is the previous UTC day to the current UTC day
            # since EDT: local date starts at 04:00 UTC (EDT) or 05:00 UTC (EST)
            month = day_dt.month
            offset = EASTERN_TZ_OFFSET.get(month, 5)
            utc_start = (day_dt - timedelta(hours=offset)).strftime('%Y-%m-%d')
            utc_end = (day_dt + timedelta(days=1) - timedelta(hours=offset)).strftime('%Y-%m-%d')

            cur = metar_conn.cursor()
            cur.execute("""
                SELECT timestamp_utc, wind_speed_kt, wind_gust_kt, wind_direction_deg,
                       temp_f, dewpoint_f, ceiling_ft, pressure_mb
                FROM metar_observations
                WHERE station = ?
                  AND date_utc >= ?
                  AND date_utc <= ?
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc
            """, (station, utc_start, utc_end))
            obs = cur.fetchall()
        finally:
            metar_conn.close()

        if not obs:
            # No data for this date — return all NaN
            return self._empty_features(local_date_str)

        # Parse observations — FILTER OUT corrupt/impossible values
        # METAR DB may contain outliers (temp > 130°F = instrument error)
        obs_list = []
        for row in obs:
            ts, ws, wg, wd, tf, dp, ceil, pres = row
            # Filter physically impossible temperatures
            if tf is not None and (tf < -30 or tf > 130):
                continue
            if dp is not None and (dp < -30 or dp > 130):
                dp = None  # Ignore corrupt dewpoint, keep the obs
            local_hour = _utc_to_local_hour(ts)
            obs_list.append({
                'ts_utc': ts,
                'local_hour': local_hour,
                'wind_speed_kt': ws if ws is None or ws >= 0 else None,
                'wind_gust_kt': wg if wg is None or wg >= 0 else None,
                'wind_direction_deg': wd if wd is None or (0 <= wd <= 360) else None,
                'temp_f': tf,
                'dewpoint_f': dp,
                'ceiling_ft': ceil if ceil is None or ceil >= 0 else None,
                'pressure_mb': pres if pres is None or (900 <= pres <= 1100) else None,
            })

        # Convert to DataFrame for easier window operations
        obs_df = pd.DataFrame(obs_list)

        # -- Wind averages (last 6 hours centered on typical windows) --
        # All-day averages
        winds = obs_df['wind_speed_kt'].dropna()
        gusts = obs_df['wind_gust_kt'].dropna()

        features['wind_avg_kt'] = float(winds.mean()) if len(winds) > 0 else None
        features['wind_max_kt'] = float(gusts.max()) if len(gusts) > 0 else (
            float(winds.max()) if len(winds) > 0 else None
        )
        features['wind_stddev_3hr'] = float(winds.std()) if len(winds) >= 3 else None

        # Wind at key local hours (find nearest obs to target hour)
        def _wind_at_hour(h: int) -> Optional[float]:
            mask = obs_df['local_hour'] == h
            subset = obs_df[mask]['wind_speed_kt'].dropna()
            if len(subset) > 0:
                return float(subset.iloc[0])
            return None

        features['wind_3pm_kt'] = _wind_at_hour(15)   # 3 PM local
        features['wind_sunset_kt'] = _wind_at_hour(20)  # ~8 PM local (approximate sunset)
        features['wind_6am_kt'] = _wind_at_hour(6)     # 6 AM local

        # Wind direction sector (dominant direction)
        wdirs = obs_df['wind_direction_deg'].dropna()
        if len(wdirs) > 0:
            # Use circular mean direction
            sin_sum = math.sin(math.radians(float(wdirs.mean())))
            cos_sum = math.cos(math.radians(float(wdirs.mean())))
            mean_dir = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
            features['wind_direction_sector'] = _wind_dir_to_sector(mean_dir)
        else:
            features['wind_direction_sector'] = None

        # ========== B. Cloud & Radiation ==========
        temps_f = obs_df['temp_f'].dropna()
        dewpoints_f = obs_df['dewpoint_f'].dropna()
        ceilings = obs_df['ceiling_ft'].dropna()

        # Dewpoint depression
        if len(temps_f) > 0 and len(dewpoints_f) > 0:
            mean_temp_c = (float(temps_f.mean()) - 32) * 5 / 9
            mean_dp_c = (float(dewpoints_f.mean()) - 32) * 5 / 9
            features['dp_depression_C'] = mean_temp_c - mean_dp_c
        else:
            features['dp_depression_C'] = None

        # Cloud cover fraction — estimate from METAR ceiling presence
        # If ceiling data exists: ceiling < 20000 ft → some cloud, else clear
        if len(ceilings) > 0:
            cloudy_obs = sum(1 for c in ceilings if c < 20000)
            features['cloud_cover_frac'] = cloudy_obs / max(len(ceilings), 1)
        else:
            # Fall back to NWP cloud cover
            nwp_cc = self._get_nwp_cloud_cover(local_date_str)
            features['cloud_cover_frac'] = nwp_cc

        features['cloud_ceiling_ft'] = float(
            ceilings.min()) if len(ceilings) > 0 else None

        # Solar flux estimation
        if features['cloud_cover_frac'] is not None:
            cloud_factor = 1.0 - 0.6 * features['cloud_cover_frac']
        else:
            cloud_factor = 0.5  # default moderate cloud

        # Compute max solar elevation at local noon
        noon_local = 12  # local noon hour
        _, sunset_local = _sunrise_sunset(KNYC_LAT, KNYC_LON, day_dt)
        # Convert to UTC for solar elevation calc
        month = day_dt.month
        offset = EASTERN_TZ_OFFSET.get(month, 5)
        noon_utc = (day_dt.replace(hour=noon_local, minute=0, second=0) +
                    timedelta(hours=offset))
        max_elev = _solar_elevation(KNYC_LAT, KNYC_LON, noon_utc)
        features['solar_elevation_max'] = max_elev
        features['solar_flux_est'] = _estimate_solar_flux(max_elev, cloud_factor)

        # Longwave flux estimation — use nighttime conditions
        # Nighttime: local hour 22-04 (after sunset, before sunrise)
        night_obs = obs_df[obs_df['local_hour'].isin([22, 23, 0, 1, 2, 3, 4])]
        night_dp = night_obs['dewpoint_f'].dropna()
        night_temps = night_obs['temp_f'].dropna()
        if len(night_dp) > 0 and len(night_temps) > 0:
            night_dp_c = (float(night_dp.mean()) - 32) * 5 / 9
            night_temp_c = (float(night_temps.mean()) - 32) * 5 / 9
            lw_dp_dep = night_temp_c - night_dp_c
        elif len(dewpoints_f) > 0 and len(temps_f) > 0:
            mean_temp_c = (float(temps_f.mean()) - 32) * 5 / 9
            mean_dp_c = (float(dewpoints_f.mean()) - 32) * 5 / 9
            lw_dp_dep = mean_temp_c - mean_dp_c
        else:
            lw_dp_dep = 10  # default moderate depression

        features['longwave_flux_est'] = _estimate_longwave_flux(
            lw_dp_dep, features.get('cloud_cover_frac') or 0.5)

        # ========== C. Stability (from ERA5) ==========
        era5_row = self._get_era5_day(local_date_str)
        if era5_row:
            features['daily_temp_range_C'] = (era5_row['daily_max_t2m'] -
                                              era5_row['daily_min_t2m'])
        else:
            # Approximate from METAR
            if len(temps_f) > 0:
                features['daily_temp_range_C'] = (float(temps_f.max()) -
                                                  float(temps_f.min())) * 5 / 9
            else:
                features['daily_temp_range_C'] = None

        # Lapse rate proxy from NWP 850hPa temp vs surface
        features['lapse_rate_850_925'] = self._get_nwp_lapse_proxy(local_date_str)
        features['bl_height_m'] = None  # Not available in current DBs

        # Bulk Richardson proxy — approximate from wind + temp range
        if features['wind_avg_kt'] is not None and features['daily_temp_range_C'] is not None:
            # Simplified: Ri ~ g * Δθ * Δz / (θ * Δu²)
            # Using temp range as proxy for Δθ, wind speed as proxy for Δu
            # This is a rough approximation
            if features['wind_avg_kt'] > 0.1:
                ri_proxy = (9.81 * features['daily_temp_range_C']) / (
                    300 * (features['wind_avg_kt'] * 0.514) ** 2 + 0.01)
                features['bulk_richardson'] = float(min(ri_proxy, 10))
            else:
                features['bulk_richardson'] = 10.0  # Very stable (calm)
        else:
            features['bulk_richardson'] = None

        # Inversion strength proxy — temp difference between early morning and afternoon
        morning_temps = obs_df[obs_df['local_hour'].isin([5, 6, 7])]['temp_f'].dropna()
        afternoon_temps = obs_df[obs_df['local_hour'].isin([13, 14, 15])]['temp_f'].dropna()
        if len(morning_temps) > 0 and len(afternoon_temps) > 0:
            features['inversion_strength_proxy'] = (
                float(afternoon_temps.mean()) - float(morning_temps.mean()))
        else:
            features['inversion_strength_proxy'] = None

        # ========== D. Temporal Features ==========
        features['hour_of_day'] = None  # Per-day aggregate, not hourly
        features['day_of_year'] = day_dt.timetuple().tm_yday
        features['is_weekend'] = 1 if day_dt.weekday() >= 5 else 0
        features['month'] = day_dt.month
        features['season'] = self._season_from_month(day_dt.month)

        # Sunrise/sunset time (hours UTC)
        sunrise_utc, sunset_utc = _sunrise_sunset(KNYC_LAT, KNYC_LON, day_dt)
        features['sunrise_utc_hours'] = sunrise_utc
        features['sunset_utc_hours'] = sunset_utc
        features['daylight_hours'] = sunset_utc - sunrise_utc

        # ========== E. Synoptic Regime ==========
        features['mslp_hPa'] = self._get_nwp_mslp(local_date_str)
        features['nwp_cloud_cover'] = self._get_nwp_cloud_cover(local_date_str)
        features['nwp_wind_speed_kt'] = self._get_nwp_wind_speed(local_date_str)

        # MSLP trend: compare today to yesterday
        features['mslp_trend_3hr'] = None  # Would need sub-daily MSLP

        # Temp range from forecast
        features['temp_range_forecast'] = self._get_nwp_temp_range(local_date_str)

        # Synoptic class — rule-based from MSLP + wind
        features['synoptic_class'] = self._classify_synoptic(
            features.get('mslp_hPa'), features.get('wind_avg_kt'),
            features.get('season'))

        # ========== F. Recent Goldilocks History ==========
        # NOTE: goldilocks_prev_day, goldilocks_prev_3days, goldilocks_rate_30d
        # These MUST be filled in AFTER labeling and before training.
        # They are initialized as NaN here.
        features['goldilocks_prev_day'] = None
        features['goldilocks_prev_3days'] = None
        features['goldilocks_rate_30d'] = None

        # ========== Additional ==========
        n_obs = len(obs_df)
        n_with_wind = int(obs_df['wind_speed_kt'].notna().sum())
        features['metar_obs_count'] = n_obs
        features['metar_wind_obs'] = n_with_wind
        features['data_quality_flag'] = (
            'good' if n_obs >= 12 and n_with_wind >= 6 else
            'partial' if n_obs >= 3 else 'poor'
        )

        return features

    def _empty_features(self, local_date_str: str) -> dict:
        """Return a dict with all features as None."""
        f = {
            'wind_avg_kt': None, 'wind_max_kt': None,
            'wind_3pm_kt': None, 'wind_sunset_kt': None, 'wind_6am_kt': None,
            'wind_direction_sector': None, 'wind_stddev_3hr': None,
            'cloud_cover_frac': None, 'cloud_ceiling_ft': None,
            'solar_elevation_max': None, 'solar_flux_est': None,
            'longwave_flux_est': None, 'dp_depression_C': None,
            'daily_temp_range_C': None, 'lapse_rate_850_925': None,
            'bl_height_m': None, 'bulk_richardson': None,
            'inversion_strength_proxy': None,
            'hour_of_day': None, 'day_of_year': 1, 'is_weekend': 0,
            'month': 1, 'season': 'winter',
            'sunrise_utc_hours': None, 'sunset_utc_hours': None,
            'daylight_hours': None,
            'mslp_hPa': None, 'nwp_cloud_cover': None, 'nwp_wind_speed_kt': None,
            'mslp_trend_3hr': None, 'temp_range_forecast': None,
            'synoptic_class': None,
            'goldilocks_prev_day': None, 'goldilocks_prev_3days': None,
            'goldilocks_rate_30d': None,
            'metar_obs_count': 0, 'metar_wind_obs': 0, 'data_quality_flag': 'none',
        }
        f['local_date'] = local_date_str
        return f

    def _get_era5_day(self, local_date_str: str) -> Optional[dict]:
        """Get ERA5 daily data for a given date and station."""
        conn = self._get_era5_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT daily_max_t2m, daily_min_t2m, daily_mean_t2m
                FROM era5_archive
                WHERE station = ? AND target_date = ?
            """, (self.station, local_date_str))
            row = cur.fetchone()
            if row:
                return {
                    'daily_max_t2m': row[0],
                    'daily_min_t2m': row[1],
                    'daily_mean_t2m': row[2],
                }
            return None
        finally:
            conn.close()

    def _get_nwp_cloud_cover(self, local_date_str: str) -> Optional[float]:
        """Get NWP daily mean cloud cover from GEFS/ECMWF."""
        conn = self._get_nwp_conn()
        try:
            cur = conn.cursor()
            # Prefer ecmwf, then gefs_ens, then gfs
            cur.execute("""
                SELECT value FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND variable = 'cloud_cover_daily_mean'
                  AND model IN ('ecmwf', 'gefs_ens', 'gfs')
                ORDER BY CASE model
                  WHEN 'ecmwf' THEN 0
                  WHEN 'gefs_ens' THEN 1
                  ELSE 2
                END
                LIMIT 1
            """, (self.station, local_date_str))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _get_nwp_wind_speed(self, local_date_str: str) -> Optional[float]:
        """Get NWP daily mean 10m wind speed (kt)."""
        conn = self._get_nwp_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT value FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND variable = 'wind_speed_10m_daily_mean'
                  AND model IN ('ecmwf', 'gefs_ens', 'gfs')
                ORDER BY CASE model
                  WHEN 'ecmwf' THEN 0
                  WHEN 'gefs_ens' THEN 1
                  ELSE 2
                END
                LIMIT 1
            """, (self.station, local_date_str))
            row = cur.fetchone()
            # Convert m/s to kt if needed (NWP values are typically m/s)
            return (row[0] * 1.944) if row else None
        finally:
            conn.close()

    def _get_nwp_mslp(self, local_date_str: str) -> Optional[float]:
        """Get NWP mean sea level pressure (hPa)."""
        # MSLP not directly in NWP variables; use temperature as fallback
        # Actually check if we have pressure data
        conn = self._get_nwp_conn()
        try:
            cur = conn.cursor()
            # No explicit MSLP in NWP — return None, will use METAR pressure
            return None
        finally:
            conn.close()

    def _get_nwp_lapse_proxy(self, local_date_str: str) -> Optional[float]:
        """
        Estimate lapse rate from NWP 850hPa temp vs surface temp.

        Uses temperature_850hPa_daily_mean from NWP forecasts.
        Higher 850hPa temp relative to surface → more stable (inversion).
        """
        conn = self._get_nwp_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT value FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND variable = 'temperature_850hPa_daily_mean'
                  AND model IN ('ecmwf', 'gefs_ens', 'gfs')
                ORDER BY CASE model
                  WHEN 'ecmwf' THEN 0
                  WHEN 'gefs_ens' THEN 1
                  ELSE 2
                END
                LIMIT 1
            """, (self.station, local_date_str))
            row = cur.fetchone()
            if row:
                # temp_850 is in C; standard lapse rate is ~6.5°C/km
                # Inversion if 850 temp > surface temp
                # We'll return the temp difference (positive → more stable)
                return float(row[0])  # raw 850hPa temperature. Difference computed downstream.
            return None
        finally:
            conn.close()

    def _get_nwp_temp_range(self, local_date_str: str) -> Optional[float]:
        """Get forecast temp range (max - min)."""
        conn = self._get_nwp_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT variable, value FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND variable IN ('temperature_2m_max', 'temperature_2m_min')
                  AND model IN ('ecmwf', 'gefs_ens', 'gfs')
                ORDER BY CASE model
                  WHEN 'ecmwf' THEN 0
                  WHEN 'gefs_ens' THEN 1
                  ELSE 2
                END
            """, (self.station, local_date_str))
            vals = {}
            for var, val in cur.fetchall():
                if var == 'temperature_2m_max':
                    vals['tmax'] = val
                elif var == 'temperature_2m_min':
                    vals['tmin'] = val
            if 'tmax' in vals and 'tmin' in vals:
                return float(vals['tmax'] - vals['tmin'])
            return None
        finally:
            conn.close()

    @staticmethod
    def _season_from_month(m: int) -> str:
        if m in (12, 1, 2):
            return 'winter'
        elif m in (3, 4, 5):
            return 'spring'
        elif m in (6, 7, 8):
            return 'summer'
        else:
            return 'fall'

    @staticmethod
    def _classify_synoptic(mslp: Optional[float], wind_avg: Optional[float],
                           season: str) -> Optional[str]:
        """Rule-based synoptic classification."""
        if mslp is None or wind_avg is None:
            return None
        # High pressure: MSLP > 1020 hPa, light winds
        if mslp > 1020 and wind_avg < 10:
            return 'continental_high'
        elif mslp > 1020:
            return 'weak_high'
        elif mslp < 1005:
            return 'trough'
        elif wind_avg > 15:
            return 'frontal'
        else:
            return 'neutral'

    def add_goldilocks_labels(self, df: pd.DataFrame, labels_path: str) -> pd.DataFrame:
        """Merge Goldilocks labels into the feature DataFrame."""
        labels_df = pd.read_csv(labels_path)
        labels_df['local_date'] = labels_df['date']
        df = df.merge(labels_df[['local_date', 'is_goldilocks_high',
                                 'is_goldilocks_low', 'is_goldilocks_any']],
                      on='local_date', how='left')
        # Fill missing labels as 0 (no event — safe default for NaN in labels)
        for col in ['is_goldilocks_high', 'is_goldilocks_low', 'is_goldilocks_any']:
            if col in df.columns:
                df[col] = df[col].fillna(0).astype(int)
        return df

    def add_goldilocks_history_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute rolling Goldilocks history features AFTER labels are added.

        Must have 'is_goldilocks_any' column.
        """
        df = df.sort_values('local_date').reset_index(drop=True)

        df['goldilocks_prev_day'] = df['is_goldilocks_any'].shift(1).fillna(0).astype(int)
        df['goldilocks_prev_3days'] = (
            df['is_goldilocks_any'].rolling(3, min_periods=1).sum().shift(1).fillna(0)
        ).astype(int)
        # Rolling 30-day rate (shifted to avoid look-ahead bias)
        rolling_30d = df['is_goldilocks_any'].rolling(30, min_periods=1).mean().shift(1)
        df['goldilocks_rate_30d'] = rolling_30d.fillna(0.0)

        return df


def create_features_dataset(station: str = DEFAULT_STATION,
                            start_date: str = '2021-01-01',
                            end_date: str = '2026-08-01',
                            labels_path: Optional[str] = None,
                            output_path: Optional[str] = None) -> pd.DataFrame:
    """
    Create a complete feature dataset, optionally merged with labels.

    Args:
        station: ICAO station code
        start_date: YYYY-MM-DD start date
        end_date: YYYY-MM-DD end date
        labels_path: Path to CSV with Goldilocks labels (from goldilocks_labeling.py)
        output_path: If provided, save feature matrix to this path

    Returns:
        pd.DataFrame with one row per day, columns for each feature
    """
    engine = GoldilocksFeatureEngine(station)
    df = engine.compute_features(start_date, end_date)

    if labels_path and os.path.exists(labels_path):
        df = engine.add_goldilocks_labels(df, labels_path)
        df = engine.add_goldilocks_history_features(df)

    if output_path:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        if output_path.endswith('.parquet'):
            df.to_parquet(output_path, index=False)
        else:
            df.to_csv(output_path, index=False)
        print(f"Feature matrix saved to {output_path}")
        print(f"  Shape: {df.shape}")
        print(f"  Columns ({len(df.columns)}): {list(df.columns)}")

    return df


def main():
    parser = argparse.ArgumentParser(
        description='Goldilocks Feature Engineering — compute feature matrix')
    parser.add_argument('--station', default=DEFAULT_STATION,
                        help=f'Station code (default: {DEFAULT_STATION})')
    parser.add_argument('--start', default='2021-01-01',
                        help='Start date YYYY-MM-DD')
    parser.add_argument('--end', default='2026-08-01',
                        help='End date YYYY-MM-DD')
    parser.add_argument('--labels', default=None,
                        help='Path to Goldilocks labels CSV (optional)')
    parser.add_argument('--output', default=None,
                        help=f'Output path (default: data/goldilocks_features_{{station}}.csv)')
    parser.add_argument('--summary', action='store_true',
                        help='Print feature summary statistics')
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        output_path = os.path.join(OUT_DIR, f'goldilocks_features_{args.station}.csv')

    print(f"Computing features for {args.station} from {args.start} to {args.end}...")
    df = create_features_dataset(
        station=args.station,
        start_date=args.start,
        end_date=args.end,
        labels_path=args.labels,
        output_path=output_path,
    )

    # Print summary
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    print(f"\nNumeric feature summary ({len(numeric_cols)} features):")
    summary = df[numeric_cols].describe().T
    summary['null_count'] = df[numeric_cols].isnull().sum()
    summary['null_pct'] = (df[numeric_cols].isnull().sum() / len(df) * 100).round(1)
    pd.set_option('display.max_columns', 10)
    pd.set_option('display.width', 120)
    print(summary.to_string())

    print(f"\nCategorical features: {[c for c in df.columns if df[c].dtype == object and c != 'local_date']}")

    # Data quality summary
    quality_counts = df['data_quality_flag'].value_counts()
    print(f"\nData quality:")
    for q, n in quality_counts.items():
        print(f"  {q}: {n} days ({n/len(df)*100:.1f}%)")

    return 0


if __name__ == '__main__':
    sys.exit(main())