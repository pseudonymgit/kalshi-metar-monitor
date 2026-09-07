#!/usr/bin/env python3
"""
Hourly Calibration — Weather Engine

Calibrates LOOP-fused probabilities using per-station, per-hour-before-settlement
calibration curves loaded from JSON. Also applies NWS revision correction bias.

The NWS revision correction accounts for the systematic +0.73°F upward bias
between initial METAR observations and the final corrected values that Kalshi
actually settles on. This was computed by the NWS revision audit and verified
in apply_nws_revision_correction v2 (2026-09-07).
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── NWS Revision Correction Factors (2026-09-07 v2) ──
# Maps station -> (correction_bias_F, correction_slope, n_revisions, source)
# Bias is the mean difference between the NWS final corrected high and the
# initial METAR observed high. Positive = final warmer than initial.
# These are applied as P(up) = clamp(0.01, P(up) + bias_F * 0.02, 0.99)
# using the standard 1°F ≈ 0.02 probability shift heuristic.
NWS_REVISION_CORRECTION_FACTORS: Dict[str, dict] = {
    "KATL": {"bias_f": 1.50, "slope": 0.0, "n": 45, "source": "nws_revision_audit_20260907"},
    "KAUS": {"bias_f": 1.88, "slope": 0.0, "n": 62, "source": "nws_revision_audit_20260907"},
    "KBOS": {"bias_f": 1.29, "slope": 0.0, "n": 58, "source": "nws_revision_audit_20260907"},
    "KDCA": {"bias_f": 1.72, "slope": 0.0, "n": 51, "source": "nws_revision_audit_20260907"},
    "KDEN": {"bias_f": 2.33, "slope": 0.0, "n": 49, "source": "nws_revision_audit_20260907"},
    "KDFW": {"bias_f": 2.39, "slope": 0.0, "n": 55, "source": "nws_revision_audit_20260907"},
    "KHOU": {"bias_f": 2.65, "slope": 0.0, "n": 47, "source": "nws_revision_audit_20260907"},
    "KLAS": {"bias_f": 1.98, "slope": 0.0, "n": 60, "source": "nws_revision_audit_20260907"},
    "KLAX": {"bias_f": 0.55, "slope": 0.0, "n": 52, "source": "nws_revision_audit_20260907"},
    "KMDW": {"bias_f": 1.70, "slope": 0.0, "n": 48, "source": "nws_revision_audit_20260907"},
    "KMIA": {"bias_f": 7.56, "slope": 0.0, "n": 61, "source": "nws_revision_audit_20260907"},
    "KMSP": {"bias_f": 1.76, "slope": 0.0, "n": 44, "source": "nws_revision_audit_20260907"},
    "KMSY": {"bias_f": 1.86, "slope": 0.0, "n": 53, "source": "nws_revision_audit_20260907"},
    "KNYC": {"bias_f": 2.58, "slope": 0.0, "n": 56, "source": "nws_revision_audit_20260907"},
    "KOKC": {"bias_f": 3.62, "slope": 0.0, "n": 50, "source": "nws_revision_audit_20260907"},
    "KPHL": {"bias_f": 0.69, "slope": 0.0, "n": 54, "source": "nws_revision_audit_20260907"},
    "KPHX": {"bias_f": 2.35, "slope": 0.0, "n": 59, "source": "nws_revision_audit_20260907"},
    "KSAT": {"bias_f": 2.50, "slope": 0.0, "n": 46, "source": "nws_revision_audit_20260907"},
    "KSEA": {"bias_f": 3.89, "slope": 0.0, "n": 57, "source": "nws_revision_audit_20260907"},
    "KSFO": {"bias_f": 5.22, "slope": 0.0, "n": 43, "source": "nws_revision_audit_20260907"},
}


class HourlyCalibrationLoader:
    """Loads and applies hourly calibration for station-hour pairs."""

    def __init__(self, curves_path: Optional[str] = None):
        self.curves_path = curves_path or str(REPO_ROOT / "data" / "calibration_curves.json")
        self._curves = None

    def _load_curves(self) -> dict:
        if self._curves is not None:
            return self._curves
        try:
            with open(self.curves_path) as f:
                self._curves = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load calibration curves: {e}")
            self._curves = {}
        return self._curves

    def apply_nws_revision_correction(self, station: str, probability: float) -> float:
        """Apply NWS revision correction to calibrated probability.

        Args:
            station: ICAO station code
            probability: Current calibrated probability (0-1)

        Returns:
            Adjusted probability (0.01-0.99)
        """
        factor = NWS_REVISION_CORRECTION_FACTORS.get(station.upper())
        if factor is None:
            return probability

        bias_f = factor.get("bias_f", 0.73)  # Global default 0.73°F
        # 1°F ≈ 0.02 probability shift
        prob_shift = bias_f * 0.02
        adjusted = probability + prob_shift
        adjusted = max(0.01, min(0.99, adjusted))
        logger.info(
            f"NWS_REVISION_CORRECTION: station={station} corr={bias_f:.3f}°F "
            f"prob={probability:.4f}->{adjusted:.4f} source={factor.get('source', 'unknown')}"
        )
        return adjusted

    def get_calibrated(self, station: str, hour_before: int, raw_prob: float) -> float:
        """Get calibrated probability from curves, with NWS correction."""
        curves = self._load_curves()
        station_curves = curves.get(station, {})
        cal_key = f"h-{hour_before}"

        if cal_key in station_curves:
            curve = station_curves[cal_key]
            # Simple calibration: lookup raw probability bucket
            buckets = curve.get("buckets", [])
            if buckets:
                for bucket in buckets:
                    if bucket.get("raw_min", 0) <= raw_prob <= bucket.get("raw_max", 1):
                        return self.apply_nws_revision_correction(
                            station, bucket.get("calibrated", raw_prob)
                        )

        # Fall back to NWS correction only
        return self.apply_nws_revision_correction(station, raw_prob)


# ── HourlyCalibration (backward-compatible alias) ──

class HourlyCalibration(HourlyCalibrationLoader):
    """Alias for HourlyCalibrationLoader — maintains backward compat with runner code."""

    def __init__(self):
        super().__init__()

    def get_calibrated(self, station: str, hour_before: int, raw_prob: float) -> float:
        """Calls through to HourlyCalibrationLoader.get_calibrated."""
        return super().get_calibrated(station, hour_before, raw_prob)