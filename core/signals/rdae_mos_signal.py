#!/usr/bin/env python3
"""
RDAE MOS Signal — Reduced-bias Direct Analysis Ensemble Model Output Statistics.

B-Mode compliant. No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal, validate_signal


class RDAEMOSSignal(BaseSignal):
    """MOS-based signal using RDAE ensemble analysis."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)

    @property
    def name(self) -> str:
        return "rdae_mos"

    @property
    def min_lookback(self) -> int:
        return 14

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate RDAE MOS signal."""
        if idx < self.min_lookback:
            return None, 0.0
        return None, 0.5

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation."""
        return None, 0.5