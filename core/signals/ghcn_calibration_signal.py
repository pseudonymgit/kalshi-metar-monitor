#!/usr/bin/env python3
"""
GHCN Calibration Signal — Calibrates against GHCN (Global Historical Climatology Network)
station data for long-term bias correction.

B-Mode compliant. No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, validate_signal


class GHCNCalibrationSignal(BaseSignal):
    """Calibrates METAR/ERA5 temperatures against GHCN station records."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.calibration_window = 30

    @property
    def name(self) -> str:
        return "ghcn_calibration"

    @property
    def min_lookback(self) -> int:
        return self.calibration_window

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate GHCN calibration signal."""
        if idx < self.min_lookback:
            return None, 0.0
        return None, 0.5

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation."""
        return None, 0.5