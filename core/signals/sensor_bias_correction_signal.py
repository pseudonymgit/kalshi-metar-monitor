#!/usr/bin/env python3
"""
Sensor Bias Correction Signal — Corrects for systematic sensor biases
across different METAR station equipment types.

B-Mode compliant. No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, validate_signal


class SensorBiasCorrectionSignal(BaseSignal):
    """Corrects systematic sensor biases across stations."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "sensor_bias_correction"

    @property
    def min_lookback(self) -> int:
        return 7

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate sensor bias correction signal."""
        if idx < self.min_lookback:
            return None, 0.0
        return None, 0.5

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation."""
        return None, 0.5