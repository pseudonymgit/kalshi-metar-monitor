#!/usr/bin/env python3
"""
goldilocks_predictive.py — Inference module for Goldilocks probability prediction.

Loads trained LightGBM models and computes P(Goldilocks | live METAR conditions).

~10ms inference, designed for integration with the main trading loop.

Usage:
    from core.goldilocks_predictive import GoldilocksPredictor

    predictor = GoldilocksPredictor()
    result = predictor.predict()
    # result = {
    #   'goldilocks_high_prob': 0.12,
    #   'goldilocks_low_prob': 0.06,
    #   'driving_features': {...},
    #   'feature_contributions': {...},
    # }

B-Mode compliant. No AI/ML. ~10ms inference per call.
"""

import json
import logging
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Allow import from repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.path.join(REPO_ROOT, 'data')
MODEL_DIR = os.path.join(DATA_DIR, 'models')
METAR_DB = os.path.join(DATA_DIR, 'metar_backfill.db')

try:
    import lightgbm as lgb
except ImportError:
    logger.warning("LightGBM not available — predictor will return climatology prior")
    lgb = None  # type: ignore

# NYC coordinates
KNYC_LAT = 40.78
KNYC_LON = -73.97
EASTERN_TZ_OFFSET = {1: 5, 2: 5, 3: 5, 4: 4, 5: 4, 6: 4,
                     7: 4, 8: 4, 9: 4, 10: 4, 11: 5, 12: 5}
WIND_SECTOR_MAP = {0: "N", 1: "NNE", 2: "NE", 3: "ENE", 4: "E", 5: "ESE",
                    6: "SE", 7: "SSE", 8: "S", 9: "SSW", 10: "SW", 11: "WSW",
                    12: "W", 13: "WNW", 14: "NW", 15: "NNW"}


class GoldilocksPredictor:
    """
    Real-time Goldilocks probability predictor.

    Loads trained models and computes features from live METAR data.
    Falls back to climatology prior if models not available.
    """

    def __init__(self, station: str = "KNYC", model_dir: Optional[str] = None):
        self.station = station
        self.model_dir = model_dir or MODEL_DIR
        self.high_model = None
        self.low_model = None
        self.high_model_loaded = False
        self.low_model_loaded = False
        self.base_rate = 0.09  # Climatology fallback
        self.model_metadata = {}

        self._load_models()

    def _load_models(self):
        """Load trained LightGBM models from disk."""
        if lgb is None:
            logger.warning("LightGBM unavailable — using climatology fallback")
            return

        high_path = os.path.join(self.model_dir, 'goldilocks_is_goldilocks_high_model.txt')
        low_path = os.path.join(self.model_dir, 'goldilocks_is_goldilocks_low_model.txt')

        if os.path.exists(high_path):
            try:
                self.high_model = lgb.Booster(model_file=high_path)
                self.high_model_loaded = True
                logger.info("Loaded HIGH model from %s", high_path)
            except Exception as e:
                logger.warning("Could not load HIGH model: %s", e)

        if os.path.exists(low_path):
            try:
                self.low_model = lgb.Booster(model_file=low_path)
                self.low_model_loaded = True
                logger.info("Loaded LOW model from %s", low_path)
            except Exception as e:
                logger.warning("Could not load LOW model: %s", e)

        # Load metadata if available
        meta_path = os.path.join(self.model_dir, 'goldilocks_model_metadata.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    self.model_metadata = json.load(f)
                self.base_rate = self.model_metadata.get('base_rate', self.base_rate)
            except Exception:
                pass

    def predict(self, use_last_6h: bool = True) -> dict:
        """
        Compute Goldilocks probabilities from recent METAR data.

        Args:
            use_last_6h: If True, use last 6 hours of METAR data.
                         If False, use last 24h.

        Returns:
            Dict with probabilities and feature breakdown.
        """
        # Get live features
        features = self._compute_live_features(use_last_6h)
        if features is None:
            return self._fallback_prediction()

        # Build feature vector for model
        feature_vector, valid_features = self._build_feature_vector(features)

        high_prob = None
        low_prob = None

        if self.high_model_loaded and feature_vector is not None:
            high_prob = float(self.high_model.predict(feature_vector)[0])
        if self.low_model_loaded and feature_vector is not None:
            low_prob = float(self.low_model.predict(feature_vector)[0])

        # Fallback to base rate if model not available
        if high_prob is None:
            high_prob = self.base_rate / 2  # Rough split
        if low_prob is None:
            low_prob = self.base_rate / 2

        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'station': self.station,
            'goldilocks_any_prob': round(high_prob + low_prob - high_prob * low_prob, 4),
            'goldilocks_high_prob': round(high_prob, 4),
            'goldilocks_low_prob': round(low_prob, 4),
            'model_loaded': self.high_model_loaded or self.low_model_loaded,
            'driving_features': {
                'wind_avg_kt': features.get('wind_avg_kt'),
                'wind_3pm_kt': features.get('wind_3pm_kt'),
                'cloud_cover_frac': features.get('cloud_cover_frac'),
                'dp_depression_C': features.get('dp_depression_C'),
                'solar_elevation_max': features.get('solar_elevation_max'),
                'daily_temp_range_C': features.get('daily_temp_range_C'),
                'is_weekend': features.get('is_weekend'),
                'month': features.get('month'),
            },
            'n_metar_obs': features.get('n_obs', 0),
        }
        return result

    def _compute_live_features(self, use_last_6h: bool = True) -> Optional[dict]:
        """Compute features from last N hours of METAR data."""
        if not os.path.exists(METAR_DB):
            logger.warning("METAR DB not found")
            return None

        try:
            conn = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
            cur = conn.cursor()

            now = datetime.now(timezone.utc)
            hours = 6 if use_last_6h else 24
            start = (now - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')

            cur.execute("""
                SELECT timestamp_utc, temp_f, dewpoint_f, wind_speed_kt,
                       wind_gust_kt, wind_direction_deg, ceiling_ft, pressure_mb
                FROM metar_observations
                WHERE station = ? AND timestamp_utc >= ?
                  AND temp_f IS NOT NULL
                ORDER BY timestamp_utc
            """, (self.station, start))
            obs = cur.fetchall()
            conn.close()

            if not obs:
                return None

            # Build obs list with local hours
            obs_list = []
            for row in obs:
                ts, tf, dp, ws, wg, wd, ceil, pres = row
                local_hour = self._utc_to_local_hour(ts)
                obs_list.append({
                    'ts_utc': ts, 'temp_f': tf, 'dewpoint_f': dp,
                    'wind_speed_kt': ws, 'wind_gust_kt': wg,
                    'wind_direction_deg': wd, 'ceiling_ft': ceil,
                    'pressure_mb': pres, 'local_hour': local_hour,
                })

            return self._compute_features_from_obs(obs_list, now)

        except Exception as e:
            logger.warning("Feature computation error: %s", e)
            return None

    def _compute_features_from_obs(self, obs_list: list, now: datetime) -> dict:
        """Compute Goldilocks features from raw METAR observations."""
        import pandas as pd
        df = pd.DataFrame(obs_list)
        winds = df['wind_speed_kt'].dropna()
        gusts = df['wind_gust_kt'].dropna()

        features = {
            'wind_avg_kt': float(winds.mean()) if len(winds) > 0 else None,
            'wind_max_kt': float(gusts.max()) if len(gusts) > 0 else (
                float(winds.max()) if len(winds) > 0 else None),
            'wind_stddev_3hr': float(winds.std()) if len(winds) >= 3 else None,
            'wind_3pm_kt': self._wind_at_hour(df, 15),
            'wind_sunset_kt': self._wind_at_hour(df, 20),
            'wind_6am_kt': self._wind_at_hour(df, 6),
            'wind_direction_sector': self._dominant_wind_dir(df),
            'dp_depression_C': self._compute_dp_depression(df),
            'cloud_cover_frac': self._compute_cloud_cover(df),
            'cloud_ceiling_ft': float(df['ceiling_ft'].min()) if df['ceiling_ft'].notna().any() else None,
            'solar_elevation_max': self._solar_elevation(now.replace(hour=17, minute=0), KNYC_LAT, KNYC_LON),
            'solar_flux_est': 0.0,
            'longwave_flux_est': 0.0,
            'is_weekend': 1 if now.weekday() >= 5 else 0,
            'month': now.month,
            'day_of_year': now.timetuple().tm_yday,
            'n_obs': len(df),
        }

        # Solar flux estimate
        elev = features['solar_elevation_max']
        cc = features['cloud_cover_frac'] or 0.5
        cloud_factor = 1.0 - 0.6 * cc
        features['solar_flux_est'] = self._estimate_solar_flux(elev, cloud_factor)

        # Longwave flux
        dp_dep = features['dp_depression_C'] or 10.0
        features['longwave_flux_est'] = self._estimate_longwave_flux(dp_dep, cc)

        return features

    @staticmethod
    def _wind_at_hour(df, h: int) -> Optional[float]:
        subset = df[df['local_hour'] == h]['wind_speed_kt'].dropna()
        return float(subset.iloc[0]) if len(subset) > 0 else None

    @staticmethod
    def _dominant_wind_dir(df):
        wdirs = df['wind_direction_deg'].dropna()
        if len(wdirs) == 0:
            return None
        mean_dir = float(wdirs.mean()) % 360
        idx = round(mean_dir / 22.5) % 16
        return WIND_SECTOR_MAP[idx]

    @staticmethod
    def _compute_dp_depression(df):
        temps = df['temp_f'].dropna()
        dps = df['dewpoint_f'].dropna()
        if len(temps) > 0 and len(dps) > 0:
            tc = (float(temps.mean()) - 32) * 5 / 9
            dpc = (float(dps.mean()) - 32) * 5 / 9
            return tc - dpc
        return None

    @staticmethod
    def _compute_cloud_cover(df):
        ceilings = df['ceiling_ft'].dropna()
        if len(ceilings) >= 5:
            cloudy = sum(1 for c in ceilings if c < 20000)
            return cloudy / len(ceilings)
        return 0.5

    @staticmethod
    def _solar_elevation(dt: datetime, lat: float, lon: float) -> float:
        doy = dt.timetuple().tm_yday
        decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (284 + doy))))
        lat_r = math.radians(lat)
        hour_angle = math.radians((dt.hour * 60 + dt.minute) / 4 - 180)
        sin_alt = (math.sin(lat_r) * math.sin(decl) +
                   math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle))
        return max(0, math.degrees(math.asin(max(-1, min(1, sin_alt)))))

    @staticmethod
    def _estimate_solar_flux(elevation: float, cloud_factor: float = 1.0) -> float:
        if elevation <= 0:
            return 0.0
        sin_elev = math.sin(math.radians(elevation))
        clear_sky = 1361 * (0.7 ** (1 / max(sin_elev, 0.01))) * sin_elev
        return clear_sky * max(0.2, cloud_factor)

    @staticmethod
    def _estimate_longwave_flux(dp_dep: float, cloud_cover: float) -> float:
        return 320 + min(dp_dep, 30) * 2 - cloud_cover * 150

    @staticmethod
    def _utc_to_local_hour(ts: str) -> int:
        try:
            dt = datetime.strptime(ts[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
            offset = EASTERN_TZ_OFFSET.get(dt.month, 5)
            return (dt - timedelta(hours=offset)).hour
        except (ValueError, IndexError):
            return 0

    def _build_feature_vector(self, features: dict) -> Optional[Tuple[np.ndarray, list]]:
        """Build feature vector matching model's expected feature order."""
        # Feature columns expected by the model
        feature_cols = [
            'wind_avg_kt', 'wind_max_kt', 'wind_stddev_3hr',
            'wind_3pm_kt', 'wind_sunset_kt', 'wind_6am_kt',
            'dp_depression_C', 'cloud_cover_frac', 'cloud_ceiling_ft',
            'solar_elevation_max', 'solar_flux_est', 'longwave_flux_est',
            'daily_temp_range_C', 'lapse_rate_850_925',
            'bulk_richardson', 'inversion_strength_proxy',
            'day_of_year', 'is_weekend', 'month',
            'daylight_hours', 'nwp_cloud_cover', 'nwp_wind_speed_kt',
            'temp_range_forecast', 'goldilocks_prev_day',
            'goldilocks_prev_3days', 'goldilocks_rate_30d',
        ]

        vec = []
        for col in feature_cols:
            val = features.get(col)
            vec.append(val if val is not None else np.nan)
        return np.array([vec], dtype=np.float32), feature_cols

    def _fallback_prediction(self) -> dict:
        """Return climatology-based prediction when features unavailable."""
        return {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'station': self.station,
            'goldilocks_any_prob': self.base_rate,
            'goldilocks_high_prob': self.base_rate / 2,
            'goldilocks_low_prob': self.base_rate / 2,
            'model_loaded': False,
            'driving_features': {},
            'n_metar_obs': 0,
            'note': 'Using climatology fallback — no METAR data or model loaded',
        }

    def predict_from_features(self, feature_dict: dict) -> dict:
        """Predict from a pre-computed feature dict (for batch/backtest use)."""
        feature_vector, _ = self._build_feature_vector(feature_dict)
        if feature_vector is None:
            return self._fallback_prediction()

        high_prob = self.high_model.predict(feature_vector)[0] if self.high_model_loaded else self.base_rate / 2
        low_prob = self.low_model.predict(feature_vector)[0] if self.low_model_loaded else self.base_rate / 2

        return {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'station': self.station,
            'goldilocks_any_prob': round(float(high_prob + low_prob - high_prob * low_prob), 4),
            'goldilocks_high_prob': round(float(high_prob), 4),
            'goldilocks_low_prob': round(float(low_prob), 4),
            'model_loaded': True,
        }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    predictor = GoldilocksPredictor()
    result = predictor.predict()
    print(json.dumps(result, indent=2))
    print(f"\nGoldilocks ANY probability: {result['goldilocks_any_prob']:.1%}")
    print(f"  HIGH: {result['goldilocks_high_prob']:.1%}")
    print(f"  LOW:  {result['goldilocks_low_prob']:.1%}")