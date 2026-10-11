#!/usr/bin/env python3
"""
ADVANCE Signal 3: HRRR Bias-Corrected (2-3d)

Uses the High-Resolution Rapid Refresh (HRRR) model via the Open-Meteo API
to fetch short-range weather forecasts at 3km resolution, hourly step.

Applies rolling bias correction per station to improve forecast accuracy.

API endpoint: https://api.open-meteo.com/v1/forecast
Parameters: latitude, longitude, hourly=temperature_2m, models=ncep_hrrr_conus,
            forecast_days=3

E1-a (2026-10-11): the fetch now pins `models=ncep_hrrr_conus` so the data is
really HRRR. Previously the `models` parameter was omitted, so Open-Meteo served
its `best_match` blend while this module claimed HRRR. HRRR is CONUS-only; a
request outside the domain fails closed rather than silently substituting another
model.

B-Mode compliant. No AI/ML.

Usage:
    from core.signals.hrrr_bias_corrected_signal import HRRRBiasCorrectedSignal

    signal = HRRRBiasCorrectedSignal()
    result = signal.get_forecast(station="KNYC", lat=40.71, lon=-74.01)
    bias_corrected = signal.apply_bias_correction(result, station="KNYC")
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO_ROOT, "data")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Open-Meteo free tier: 10,000 requests/day, no API key needed
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# E1-a fix (2026-10-11): this signal previously omitted `models`, so Open-Meteo
# silently served its `best_match` auto-selection (a blend that is NOT HRRR) while
# the module, class, and DB all claimed "HRRR". Pin the real HRRR model instead.
# Open-Meteo's current HRRR identifier is `ncep_hrrr_conus` (CONUS domain, 3km);
# verified live 2026-10-11 — the bare `hrrr` string is rejected by the API.
HRRR_MODEL_ID = "ncep_hrrr_conus"
# HRRR is CONUS-only: Open-Meteo returns "No data is available for this
# location" outside it. The signal must fail loudly rather than silently fall
# back to a non-HRRR model and keep calling the result "HRRR".
HRRR_CONUS_ONLY = True
RATE_LIMIT_DELAY = 0.5  # 500ms between requests (well within 10k/day limit)

# Bias correction
BIAS_DB_PATH = os.path.join(DATA_DIR, "hrrr_bias.db")
BIAS_WINDOW_DAYS = 14      # rolling 14-day bias window
MIN_BIAS_OBSERVATIONS = 7  # minimum observations for reliable bias estimate
MAX_BIAS_MAGNITUDE_F = 5.0  # cap bias correction at ±5°F

# Station coordinates
STATIONS = {
    "KNYC": (40.71, -74.01), "KLAX": (33.94, -118.41),
    "KATL": (33.64, -84.43), "KBOS": (42.37, -71.01),
    "KDEN": (39.86, -104.67), "KDFW": (32.90, -97.04),
    "KHOU": (29.65, -95.28), "KLAS": (36.08, -115.15),
    "KMDW": (41.79, -87.75), "KMIA": (25.80, -80.29),
    "KMSP": (44.88, -93.22), "KMSY": (29.99, -90.26),
    "KOKC": (35.39, -97.60), "KPHL": (39.87, -75.24),
    "KPHX": (33.45, -112.08), "KSAT": (29.42, -98.49),
    "KSEA": (47.45, -122.31), "KSFO": (37.62, -122.37),
    "KDCA": (38.85, -77.04), "KAUS": (30.19, -97.67),
}


def us_location(lat: float, lon: float) -> bool:
    """Best-effort CONUS bounding-box check for the HRRR domain.

    HRRR (`ncep_hrrr_conus`) covers the contiguous United States. The box is
    deliberately generous; Open-Meteo remains the authority (it returns an error
    for out-of-domain points) and this guard only prevents an obvious
    out-of-domain fetch from being mislabelled HRRR.
    """
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.5


class HRRRBiasCorrectedSignal:
    """HRRR model fetcher with rolling bias correction."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._init_bias_db()

    def _init_bias_db(self):
        """Initialize bias correction database."""
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(BIAS_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hrrr_bias (
                station TEXT,
                forecast_date TEXT,
                forecast_hour INTEGER,
                hrrr_temp_f REAL,
                actual_temp_f REAL,
                bias_f REAL,
                recorded_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (station, forecast_date, forecast_hour)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hrrr_forecasts (
                station TEXT,
                forecast_date TEXT,
                forecast_hour INTEGER,
                temp_f REAL,
                fetched_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (station, forecast_date, forecast_hour)
            )
        """)
        conn.commit()
        conn.close()

    def _fetch_from_open_meteo(self, lat: float, lon: float) -> Optional[Dict]:
        """
        Fetch an explicitly-HRRR forecast from the Open-Meteo API.

        E1-a fix: `models={HRRR_MODEL_ID}` is pinned so the returned data is
        genuinely HRRR (`ncep_hrrr_conus`). We do NOT rely on Open-Meteo's
        `best_match` auto-selection, which is a multi-model blend and not HRRR.
        Outside the CONUS domain HRRR has no data; the caller fails closed
        rather than substituting another model under the HRRR name.
        """
        params = (
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&models={HRRR_MODEL_ID}"
            f"&forecast_days=3"
            f"&temperature_unit=fahrenheit"
            f"&timezone=auto"
        )
        url = f"{OPEN_METEO_BASE}?{params}"

        try:
            req = Request(url, headers={"User-Agent": "WeatherEngine/1.0"})
            resp = urlopen(req, timeout=15)
            data = json.loads(resp.read().decode())
            if isinstance(data, dict) and data.get("error"):
                # e.g. HRRR outside CONUS. Do NOT fall back to a non-HRRR model.
                logger.error(
                    "Open-Meteo HRRR (%s) unavailable for %.3f,%.3f: %s",
                    HRRR_MODEL_ID, lat, lon, data.get("reason"),
                )
                return None
            return data
        except Exception as e:
            logger.error(f"Open-Meteo fetch failed: {e}")
            return None

    def get_forecast(self, station: str, lat: float, lon: float) -> Optional[Dict]:
        """
        Fetch 3-day HRRR forecast for a station.

        Returns dict with hourly temperature forecasts.
        """
        if HRRR_CONUS_ONLY and not us_location(lat, lon):
            logger.error(
                "HRRR is CONUS-only; %.3f,%.3f is outside the domain — refusing "
                "to substitute a non-HRRR model.", lat, lon,
            )
            return None

        time.sleep(RATE_LIMIT_DELAY)  # Rate limit safety

        raw = self._fetch_from_open_meteo(lat, lon)
        if raw is None:
            return None

        hourly = raw.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])

        if not times or not temps:
            logger.warning(f"Empty HRRR response for {station}")
            return None

        forecasts = []
        now = datetime.now(timezone.utc)

        conn = sqlite3.connect(BIAS_DB_PATH)
        for t_str, temp_f in zip(times, temps):
            try:
                dt = datetime.fromisoformat(t_str)
                date_str = dt.strftime("%Y-%m-%d")
                hour = dt.hour
                forecasts.append({
                    "datetime": t_str,
                    "date": date_str,
                    "hour": hour,
                    "temp_f": temp_f,
                })
                # Store in DB
                conn.execute("""
                    INSERT OR REPLACE INTO hrrr_forecasts
                    (station, forecast_date, forecast_hour, temp_f)
                    VALUES (?, ?, ?, ?)
                """, (station, date_str, hour, temp_f))
            except (ValueError, TypeError):
                continue

        conn.commit()
        conn.close()

        return {
            "station": station,
            "forecasts": forecasts,
            "fetched_at": now.isoformat(),
        }

    def _record_actual(self, station: str, forecast_date: str,
                       forecast_hour: int, actual_temp_f: float):
        """Record an actual temperature for bias computation."""
        conn = sqlite3.connect(BIAS_DB_PATH)
        
        # Check if we have a forecast for this time
        cursor = conn.execute(
            "SELECT temp_f FROM hrrr_forecasts WHERE station=? AND forecast_date=? AND forecast_hour=?",
            (station, forecast_date, forecast_hour)
        )
        row = cursor.fetchone()
        if row is None:
            conn.close()
            return

        hrrr_temp = row[0]
        bias = round(actual_temp_f - hrrr_temp, 2)

        conn.execute("""
            INSERT OR REPLACE INTO hrrr_bias
            (station, forecast_date, forecast_hour, hrrr_temp_f, actual_temp_f, bias_f)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (station, forecast_date, forecast_hour, hrrr_temp, actual_temp_f, bias))
        conn.commit()
        conn.close()

    def get_station_bias(self, station: str) -> Optional[float]:
        """
        Compute rolling bias for a station from recent observations.

        Returns mean bias in °F (positive = HRRR under-predicts).
        None if insufficient data.
        """
        conn = sqlite3.connect(BIAS_DB_PATH)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BIAS_WINDOW_DAYS)).isoformat()

        cursor = conn.execute(
            """SELECT AVG(bias_f), COUNT(*), MIN(bias_f), MAX(bias_f)
               FROM hrrr_bias
               WHERE station=? AND recorded_at >= ?""",
            (station, cutoff)
        )
        row = cursor.fetchone()
        conn.close()

        if row is None or row[1] < MIN_BIAS_OBSERVATIONS:
            return None

        avg_bias = row[0]
        count = row[1]

        # Cap at max magnitude
        avg_bias = max(-MAX_BIAS_MAGNITUDE_F, min(MAX_BIAS_MAGNITUDE_F, avg_bias))

        return round(avg_bias, 2)

    def apply_bias_correction(self, forecast_result: Dict, station: str) -> Dict:
        """Apply rolling bias correction to HRRR forecast."""
        bias = self.get_station_bias(station)

        corrected = []
        for fc in forecast_result.get("forecasts", []):
            entry = dict(fc)
            if bias is not None:
                entry["temp_f_corrected"] = round(fc["temp_f"] + bias, 1)
                entry["bias_applied"] = bias
            else:
                entry["temp_f_corrected"] = fc["temp_f"]
                entry["bias_applied"] = 0.0
            corrected.append(entry)

        return {
            "station": station,
            "bias_f": bias,
            "forecasts": corrected,
            "fetched_at": forecast_result.get("fetched_at"),
        }

    def get_daily_extremes(self, station: str, lat: float, lon: float,
                           target_date: str = None) -> Dict:
        """
        Shortcut: get bias-corrected max/min for a date.
        Used by the trading pipeline to evaluate temperature buckets.

        Returns dict with max_f, min_f, confidence.
        """
        forecast = self.get_forecast(station, lat, lon)
        if forecast is None:
            return {"max_f": None, "min_f": None, "confidence": 0.0}

        corrected = self.apply_bias_correction(forecast, station)

        # Find max/min for target date (default: tomorrow)
        if target_date is None:
            target_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        day_forecasts = [f for f in corrected["forecasts"] if f["date"] == target_date]
        if not day_forecasts:
            return {"max_f": None, "min_f": None, "confidence": 0.0}

        temps = [f["temp_f_corrected"] for f in day_forecasts]
        max_f = max(temps)
        min_f = min(temps)

        # Confidence based on bias data quality
        if corrected["bias_f"] is not None:
            bias_confidence = 1.0 - min(abs(corrected["bias_f"]) / MAX_BIAS_MAGNITUDE_F, 0.5)
        else:
            bias_confidence = 0.5  # No bias data → lower confidence

        return {
            "max_f": round(max_f, 1),
            "min_f": round(min_f, 1),
            "max_hour": day_forecasts[temps.index(max_f)]["hour"],
            "min_hour": day_forecasts[temps.index(min_f)]["hour"],
            "confidence": round(bias_confidence, 3),
            "bias_f": corrected["bias_f"],
        }

    def record_settlement(self, station: str, settlement_date: str,
                          settlement_hour: int, actual_temp_f: float):
        """Call this after settlement to record actual temp for bias learning."""
        self._record_actual(station, settlement_date, settlement_hour, actual_temp_f)