"""
HourlyCalibration — Per-(station, hour_before_settlement) Calibration (v1.0 — 2026-09-07)

Applies calibration to raw LOOP-fused probability using historical accuracy
data. Maintains per-(station, hour_before_settlement) calibration curves
derived from Platt-scaled regression on historical settlement outcomes.

Two-phase calibration:
  1. If a per-station + hour_before_settlement curve exists: apply it directly
  2. Fallback: apply global (pooled) curve for that hour_before_settlement bucket

Calibration curves are loaded from data/calibration_curves.json (v2).
When no curve exists, returns raw probability clamped to [0.01, 0.99].

Deterministic math only — no AI/ML.
"""

import json
import logging
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CURVES_PATH = str(REPO_ROOT / "data" / "calibration_curves.json")

# Fallback calibration parameters (pooled global when no per-station curve exists)
GLOBAL_CALIBRATION: Dict[int, Dict] = {
    # hour_before_settlement: {alpha, beta} for logit mapping
    # P_calibrated = expit(alpha * logit(p_raw) + beta)
    0: {"alpha": 0.85, "beta": 0.10},
    1: {"alpha": 0.88, "beta": 0.08},
    2: {"alpha": 0.90, "beta": 0.06},
    3: {"alpha": 0.92, "beta": 0.05},
    4: {"alpha": 0.93, "beta": 0.04},
    5: {"alpha": 0.94, "beta": 0.03},
    6: {"alpha": 0.95, "beta": 0.02},
    7: {"alpha": 0.95, "beta": 0.02},
    8: {"alpha": 0.96, "beta": 0.01},
    9: {"alpha": 0.96, "beta": 0.01},
    10: {"alpha": 0.97, "beta": 0.00},
    11: {"alpha": 0.97, "beta": 0.00},
    12: {"alpha": 0.98, "beta": 0.00},
}

# Minimum samples required to trust a per-station curve
MIN_SAMPLES_PER_CELL = 30

# Clamp range for calibrated probability
CALIBRATED_MIN = 0.01
CALIBRATED_MAX = 0.99


def _safe_logit(p: float) -> float:
    """Compute logit(p) with clamping to avoid infinities."""
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(np.log(p / (1 - p)))


def _safe_expit(log_odds: float) -> float:
    """Compute expit(log_odds) with clamping."""
    return float(np.clip(1.0 / (1.0 + math.exp(-log_odds)), CALIBRATED_MIN, CALIBRATED_MAX))


class HourlyCalibration:
    """
    Calibrates raw fused probabilities using per-station/hour-before-settlement curves.

    Loads calibration curves from calibration_curves.json (v2 format).
    Falls back to global pooled curves when a per-station curve is unavailable
    or has insufficient samples.

    Usage:
        calibrator = HourlyCalibration(curves_path="...")
        calibrated = calibrator.get_calibrated("KATL", 4, 0.62)
    """

    def __init__(self, curves_path: str = ""):
        self.curves_path = curves_path or DEFAULT_CURVES_PATH
        self._curves: Dict[str, Dict] = {}  # station -> {hour_before: {alpha, beta, n}}
        self._loaded = False
        self.loaded_station_count = 0
        self._load_curves()

    @staticmethod
    def _is_numeric(value: object) -> bool:
        """Return whether a calibration scalar is a finite real number."""
        # bool is an int subclass, but is never a valid calibration parameter.
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @classmethod
    def _is_valid_hourly_curve_cell(cls, curve: object) -> bool:
        """Validate the complete producer cell before it can enter consumer state."""
        if not isinstance(curve, dict):
            return False
        return all(
            cls._is_numeric(curve.get(field))
            for field in ("alpha", "beta", "n")
        )

    @classmethod
    def _is_hourly_curve_map(cls, value: object) -> bool:
        """Return whether a station entry is a complete hourly Platt map.

        JSON object keys are part of the producer contract.  Unsupported keys
        (including directional ``up``/``down`` or aliases such as ``hb6``)
        reject the whole station rather than being skipped and allowing a
        partially compatible map into consumer state.
        """
        if not isinstance(value, dict):
            return False
        numeric_cells = []
        seen_hours = set()
        for hour_key, curve in value.items():
            # Accept only canonical decimal hour keys in the supported 0..12
            # domain; reject malformed, out-of-range, and duplicate-equivalent
            # keys (e.g. "6"/"06") rather than allowing ambiguity.
            if not isinstance(hour_key, str) or not hour_key.isdecimal():
                return False
            try:
                hour = int(hour_key)
            except (TypeError, ValueError):
                return False
            if hour_key != str(hour) or not 0 <= hour <= 12:
                return False
            if hour in seen_hours:
                return False
            seen_hours.add(hour)
            numeric_cells.append(curve)
        return bool(numeric_cells) and all(cls._is_valid_hourly_curve_cell(cell) for cell in numeric_cells)

    def _load_curves(self) -> None:
        """Load calibration curves from JSON file."""
        if not self.curves_path or not os.path.exists(self.curves_path):
            logger.warning(f"No calibration curves found at {self.curves_path} — using global fallback")
            self._loaded = False
            return

        try:
            with open(self.curves_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("top-level calibration artifact must be a mapping")

            # A wrapper is a contract boundary, not a hint.  If present, it
            # must be a mapping with an unambiguous shape and known metadata;
            # do not silently select a valid sibling or reinterpret malformed
            # wrapper content as direct station data.
            wrapper_keys = {key for key in ("curves", "stations") if key in data}
            if len(wrapper_keys) > 1:
                raise ValueError("top-level calibration wrapper must name one station section")
            if wrapper_keys and any(not isinstance(data[key], dict) for key in wrapper_keys):
                raise ValueError("top-level calibration wrapper must contain mappings")
            if wrapper_keys:
                if "version" in data and data["version"] != 2:
                    raise ValueError("unsupported calibration wrapper version")
                if "type" in data and data["type"] != "hourly_platt":
                    raise ValueError("unsupported calibration wrapper type")
            self._curves = {}

            # Support both old and new format
            curves_section = data.get("curves", data.get("stations", data))
            # Try direct station mapping
            if isinstance(curves_section, dict):
                for station, hb_data in curves_section.items():
                    if self._is_hourly_curve_map(hb_data):
                        # json.load() may be fixture/mock supplied, so do not
                        # retain aliases into caller-owned decoded payloads.
                        # Runtime calibration state must be isolated after load.
                        self._curves[station.upper()] = deepcopy(hb_data)
                    else:
                        logger.debug(
                            "Skipping non-hourly calibration data for %s "
                            "(expected numeric hour -> alpha/beta cell)", station
                        )

            self.loaded_station_count = len(self._curves)
            self._loaded = True
            logger.info(
                f"Loaded hourly calibration curves for "
                f"{self.loaded_station_count} stations from {self.curves_path}"
            )
        except Exception as e:
            logger.warning(f"Failed to load calibration curves: {e}")
            self.loaded_station_count = 0
            self._loaded = False

    def _get_hour_before_key(self, hb: int) -> str:
        """Convert hour_before_settlement to string/format used in curves dict."""
        # Curves may use "0", "1", ... or "hb0", "hb1", ... or integer keys
        return str(hb)

    def _get_global_curve(self, hour_before_settlement: int) -> Dict:
        """
        Get global fallback calibration curve for a given hour_before_settlement.

        Returns {alpha, beta} for logit mapping.
        """
        return GLOBAL_CALIBRATION.get(hour_before_settlement, GLOBAL_CALIBRATION.get(6, {"alpha": 0.95, "beta": 0.02}))

    def _apply_platt(
        self, raw_probability: float, alpha: float, beta: float
    ) -> float:
        """
        Apply Platt-scaled calibration: P_cal = expit(alpha * logit(P_raw) + beta).

        Args:
            raw_probability: Raw probability (pre-calibration)
            alpha: Platt scaling parameter
            beta: Platt scaling parameter

        Returns:
            Calibrated probability
        """
        log_odds = _safe_logit(raw_probability)
        calibrated_log_odds = alpha * log_odds + beta
        return _safe_expit(calibrated_log_odds)

    def get_calibrated(
        self, station: str, hour_before_settlement: int, raw_probability: float
    ) -> float:
        """
        Apply calibration to a raw probability for a station and settlement phase.

        Calibration cascade:
          1. Per-station + exact hour_before_settlement curve
          2. Per-station + nearest hour_before_settlement (within ±1)
          3. Global curve for this hour_before_settlement
          4. Identity (clamped raw probability)

        Args:
            station: ICAO station code (e.g., "KATL")
            hour_before_settlement: Hours until settlement (0 = settlement hour)
            raw_probability: Raw probability in [0, 1]

        Returns:
            Calibrated probability in [0, 1]
        """
        raw_probability = np.clip(raw_probability, 0.0, 1.0)
        station_key = station.upper()

        # Cascade 1: Per-station + exact hour
        station_curves = self._curves.get(station_key, {})
        hb_key = self._get_hour_before_key(hour_before_settlement)

        if station_curves:
            curve = station_curves.get(hb_key)
            if isinstance(curve, dict) and "alpha" in curve:
                n = curve.get("n", 0)
                if isinstance(n, (int, float)) and n >= MIN_SAMPLES_PER_CELL:
                    alpha = float(curve.get("alpha", 1.0))
                    beta = float(curve.get("beta", 0.0))
                    calibrated = self._apply_platt(raw_probability, alpha, beta)
                    logger.debug(
                        f"Calibrated {station} hb={hour_before_settlement} "
                        f"via per-station: {raw_probability:.4f} -> {calibrated:.4f}"
                    )
                    return calibrated

            # Cascade 2: Try nearest hour_before_settlement within ±1
            for delta in [1, -1]:
                neighbor_key = self._get_hour_before_key(hour_before_settlement + delta)
                if neighbor_key in station_curves:
                    neighbor_curve = station_curves[neighbor_key]
                    if isinstance(neighbor_curve, dict) and "alpha" in neighbor_curve:
                        n = neighbor_curve.get("n", 0)
                        if isinstance(n, (int, float)) and n >= MIN_SAMPLES_PER_CELL:
                            alpha = float(neighbor_curve.get("alpha", 1.0))
                            beta = float(neighbor_curve.get("beta", 0.0))
                            calibrated = self._apply_platt(raw_probability, alpha, beta)
                            logger.debug(
                                f"Calibrated {station} hb={hour_before_settlement} (+{delta}) "
                                f"via neighbor: {raw_probability:.4f} -> {calibrated:.4f}"
                            )
                            return calibrated

        # Cascade 3: Global curve
        global_curve = self._get_global_curve(hour_before_settlement)
        alpha = global_curve.get("alpha", 0.95)
        beta = global_curve.get("beta", 0.02)
        calibrated = self._apply_platt(raw_probability, alpha, beta)
        logger.debug(
            f"Calibrated {station} hb={hour_before_settlement} "
            f"via global: {raw_probability:.4f} -> {calibrated:.4f}"
        )
        return calibrated

    def get_raw_calibrated(
        self, station: str, hour_before_settlement: int, raw_probability: float
    ) -> float:
        """
        Apply raw calibration (no clamping beyond standard bounds).
        Alias for get_calibrated for backward compatibility.
        """
        return self.get_calibrated(station, hour_before_settlement, raw_probability)


# ─── Convenience Factory ──────────────────────────────────────

def create_hourly_calibration(curves_path: str = "") -> HourlyCalibration:
    """Create an HourlyCalibration instance, loading curves from disk."""
    return HourlyCalibration(curves_path=curves_path)
