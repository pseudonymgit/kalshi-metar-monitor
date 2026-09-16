#!/usr/bin/env python3
"""
RDAE MOS Signal — Reduced-bias Direct Analysis Ensemble Model Output Statistics.

Delegates to core/rdae_mos.py for DB-backed station evaluation. Provides
an inline analog-ensemble MOS when operating on in-memory day dicts.

B-Mode compliant. No AI/ML.
"""
import math
import signal
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta
from collections import defaultdict

from .base_signal import BaseSignal, validate_signal

# Import the core RDAE-MOS implementation for DB-backed evaluation
from core.rdae_mos import (
    rdae_predictor,
    K_ANALOGS,
    SEASONAL_WINDOW,
    REGIME_CATEGORIES,
    FEATURE_WEIGHTS,
    get_doy,
    parse_ymd,
)

# Default DB path matching core/rdae_mos
DEFAULT_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
GHCN_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/ghcn_settlements.db"


def _extract_features_from_day(day: dict, prev_day: dict = None) -> dict:
    """Extract normalized features from a single day dict for analog matching."""
    temp = day.get("temp")
    high = day.get("high")
    low = day.get("low")
    dewpoint = day.get("dewpoint")
    pressure = day.get("pressure")
    wind_dir = day.get("wind_dir")
    wind_speed = day.get("wind_speed")

    if temp is None:
        return None

    # Compute temp trend if previous day available
    temp_trend = 0.0
    if prev_day and prev_day.get("temp") is not None:
        temp_trend = temp - prev_day["temp"]

    # Default values for missing fields
    pressure = pressure if pressure is not None else 1013.25
    wind_speed_val = wind_speed if wind_speed is not None else 0.0
    wind_dir_val = wind_dir if wind_dir is not None else 180.0
    dewpoint_val = dewpoint if dewpoint is not None else temp - 10.0

    features = {
        "pressure": float(pressure),
        "pressure_zscore": 0.0,  # Will be normalized relative to lookback window
        "wind_speed": float(wind_speed_val),
        "wind_speed_zscore": 0.0,
        "wind_direction_deg": float(wind_dir_val) % 360,
        "wind_direction_normalized": (float(wind_dir_val) % 360) / 360.0,
        "temp_trend": float(temp_trend),
        "temp_trend_zscore": 0.0,
        "dewpoint": float(dewpoint_val),
        "dewpoint_zscore": 0.0,
        "pressure_trend": 0.0,
        "pressure_trend_zscore": 0.0,
        "temp": float(temp),
        "high": float(high) if high is not None else float(temp),
        "low": float(low) if low is not None else float(temp) - 5.0,
    }
    return features


def _normalize_feature_value(value: float, mean: float, std: float) -> float:
    """Z-score normalize, protecting against zero std."""
    if std < 1e-9:
        return 0.0
    return (value - mean) / std


def _compute_regime_simple(features: dict) -> str:
    """Simple regime classification from day features."""
    if features is None:
        return "UNDEFINED"
    pressure = features.get("pressure", 1013.0)
    wind_speed = features.get("wind_speed", 0.0)
    wind_dir = features.get("wind_direction_deg", 0.0)
    temp_trend = features.get("temp_trend", 0.0)

    if pressure > 1016.0:
        return "HIGH_PRESSURE"
    elif pressure < 1010.0:
        return "LOW_PRESSURE"
    else:
        if wind_speed > 15.0:
            if 315 <= wind_dir <= 360 or 0 <= wind_dir < 45 or 135 <= wind_dir <= 225:
                return "WINDY_NORTH_SOUTH"
            else:
                return "WINDY_EAST_WEST"
        else:
            if abs(temp_trend) < 2.0:
                return "STABLE"
            else:
                return "TRANSITION"


def _compute_analog_distance(curr: dict, hist: dict) -> float:
    """Weighted Euclidean distance between two feature sets."""
    if curr is None or hist is None:
        return float("inf")
    total = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        cv = curr.get(key, 0.0)
        hv = hist.get(key, 0.0)
        total += weight * (cv - hv) ** 2
    # Additional temp_trend similarity
    total += 0.15 * (curr.get("temp_trend", 0.0) - hist.get("temp_trend", 0.0)) ** 2
    return math.sqrt(total)


def _normalize_features_window(features_list: List[dict]) -> List[dict]:
    """Normalize a list of feature dicts using z-scores across the window."""
    if not features_list:
        return features_list

    means = defaultdict(float)
    stds = defaultdict(float)
    count = len(features_list)

    # Compute means
    for key in FEATURE_WEIGHTS.keys():
        vals = [f.get(key, 0.0) for f in features_list if f is not None]
        if vals:
            means[key] = sum(vals) / len(vals)
            # Include temp_trend_zscore separately
    # temp_trend z-score
    vals_t = [f.get("temp_trend", 0.0) for f in features_list if f is not None]
    means["temp_trend_zscore_actual"] = sum(vals_t) / len(vals_t) if vals_t else 0.0

    # Compute stds
    for key in FEATURE_WEIGHTS.keys():
        vals = [f.get(key, 0.0) for f in features_list if f is not None]
        if vals and len(vals) > 1:
            var = sum((v - means[key]) ** 2 for v in vals) / len(vals)
            stds[key] = math.sqrt(var) if var > 0 else 1.0
        else:
            stds[key] = 1.0
    vals_t = [f.get("temp_trend", 0.0) for f in features_list if f is not None]
    if vals_t and len(vals_t) > 1:
        var_t = sum((v - means["temp_trend_zscore_actual"]) ** 2 for v in vals_t) / len(vals_t)
        std_t = math.sqrt(var_t) if var_t > 0 else 1.0
    else:
        std_t = 1.0

    # Apply normalization
    normalized = []
    for f in features_list:
        if f is None:
            normalized.append(None)
            continue
        nf = dict(f)
        for key in FEATURE_WEIGHTS.keys():
            if key in nf:
                nf[key] = _normalize_feature_value(nf[key], means[key], stds[key])
        nf["temp_trend_zscore"] = _normalize_feature_value(
            nf.get("temp_trend", 0.0),
            means["temp_trend_zscore_actual"],
            std_t,
        )
        normalized.append(nf)
    return normalized


class RDAEMOSSignal(BaseSignal):
    """MOS-based signal using RDAE ensemble analysis.

    evaluate(): inline analog-ensemble over the provided days list.
    evaluate_for_station(): delegates to core/rdae_mos.rdae_predictor() which
    queries the METAR backfill DB and settlement_epochs for outcomes.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path or DEFAULT_DB_PATH)

    @property
    def name(self) -> str:
        return "rdae_mos"

    @property
    def min_lookback(self) -> int:
        return 14

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate RDAE MOS signal using inline analog ensemble over the days list.

        Extracts features from each day, classifies regime, finds analogs within
        the same regime, and uses their next-day temperature movement as outcome.
        """
        if idx < self.min_lookback or idx >= len(days) or idx < 1:
            return None, 0.0

        # Build feature vectors for all days up to idx
        features = []
        prev_day = None
        for i in range(idx + 1):
            d = days[i]
            if not isinstance(d, dict) or not d.get("date"):
                features.append(None)
                prev_day = d if d else None
                continue
            feat = _extract_features_from_day(d, prev_day)
            features.append(feat)
            prev_day = d

        current_features = features[idx]
        if current_features is None:
            return None, 0.0

        # Determine regime of current day
        current_regime = _compute_regime_simple(current_features)

        # Get target DOY for seasonal window filtering
        target_date = days[idx].get("date", "")
        target_doy = get_doy(target_date)

        # Normalize feature vectors
        valid_features = [f for f in features if f is not None]
        if len(valid_features) < 2:
            return None, 0.0
        normalized_list = _normalize_features_window(valid_features)

        # Map normalized features back to indices
        norm_by_idx = {}
        ni = 0
        for i, f in enumerate(features):
            if f is not None:
                norm_by_idx[i] = normalized_list[ni]
                ni += 1

        curr_norm = norm_by_idx.get(idx)
        if curr_norm is None:
            return None, 0.0

        # Find historical analogs (days before idx) with same regime and seasonal match
        analogs = []
        for i in range(1, idx):  # Start from 1 (day 0 has no outcome)
            hf = features[i]
            if hf is None:
                continue
            hist_norm = norm_by_idx.get(i)
            if hist_norm is None:
                continue

            # Seasonal window filter
            hist_date = days[i].get("date", "")
            hist_doy = get_doy(hist_date)
            doy_diff = abs(target_doy - hist_doy)
            doy_diff = min(doy_diff, 365 - doy_diff)
            if doy_diff > SEASONAL_WINDOW:
                continue

            # Regime match
            hist_regime = _compute_regime_simple(hf)
            if hist_regime != current_regime and current_regime != "UNDEFINED":
                continue

            # Compute distance
            dist = _compute_analog_distance(curr_norm, hist_norm)
            seasonal_penalty = (doy_diff / SEASONAL_WINDOW) * 0.1
            total_dist = dist + seasonal_penalty

            # Determine outcome: did temp go up or down from day i to i+1?
            next_temp = days[i + 1].get("temp") if i + 1 < len(days) else None
            this_temp = days[i].get("temp")
            if this_temp is None or next_temp is None:
                continue
            outcome = "up" if next_temp > this_temp else "down"

            analogs.append((total_dist, outcome))

        if len(analogs) < 2:
            return None, 0.0

        analogs.sort(key=lambda x: x[0])
        top_analogs = analogs[:K_ANALOGS]

        up_count = sum(1 for _, o in top_analogs if o == "up")
        total = len(top_analogs)
        prob = up_count / total

        # Confidence: how decisive the analog vote is
        if prob > 0.5:
            direction = "up"
            confidence = prob
        elif prob < 0.5:
            direction = "down"
            confidence = 1.0 - prob
        else:
            return None, 0.0

        # Adjust confidence for sample size (fewer analogs = slightly less confidence)
        # Base floor factor ensures even low-count analog sets retain meaningful confidence
        sample_ratio = total / K_ANALOGS
        sample_multiplier = 0.5 + 0.5 * min(1.0, sample_ratio * 5)
        confidence *= sample_multiplier

        # Clamp and threshold
        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence

    def evaluate_for_station(
        self, station: str, date: str, conn=None
    ) -> Tuple[Optional[str], float]:
        """Station-level evaluation using the full RDAE-MOS implementation.

        Delegates to core/rdae_mos.rdae_predictor(), which:
        - Connects to the METAR backfill DB
        - Extracts features for the given station/date
        - Classifies regime
        - Finds historical analogs from settlement_epochs
        - Computes MOS-calibrated probability
        - Returns (direction, confidence)

        Includes a 15-second timeout guard against slow DB queries.
        """
        try:
            from core.rdae_mos import rdae_predictor
        except Exception:
            return None, 0.0

        if not station or not date:
            return None, 0.0

        # Timeout handler for DB-heavy queries
        def _timeout_handler(signum, frame):
            raise TimeoutError("rdae_predictor timed out")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(15)
        try:
            import sqlite3
            own_conn = conn is None
            if own_conn:
                conn = sqlite3.connect(self.db_path, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=3000;")

            direction, confidence = rdae_predictor(conn, station, date)

            if own_conn and conn:
                conn.close()
            return direction, confidence
        except TimeoutError:
            return None, 0.0
        except Exception:
            return None, 0.0
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)