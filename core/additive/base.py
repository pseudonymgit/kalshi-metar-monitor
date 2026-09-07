"""
base.py — AdditiveSignal ABC + shared helpers for the Gamma additive stack.
"""

import logging
import math
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "AdditiveSignalResult",
    "AdditiveSignal",
    "circular_mean_deg",
    "circular_diff",
    "lin_slope",
    "clamp_f",
]


@dataclass
class AdditiveSignalResult:
    """Result of one additive signal evaluation for one (station, date)."""
    signal: str
    offset_f: float = 0.0
    confidence: float = 0.0
    fired: bool = False
    reason: str = ""
    meta: Dict = field(default_factory=dict)


class AdditiveSignal(ABC):
    """
    Base class for additive temperature signals.

    Subclasses implement `evaluate()`, returning an AdditiveSignalResult.
    Contract:
      - offset_f MUST be within [-max_offset_f, +max_offset_f].
      - Never read observations at or after the local feature cutoff.
      - Return fired=False / offset 0.0 when conditions are not met
        (signals are dormant by default).
    """

    _default_max_offset: float = 5.0

    def __init__(self):
        self._name: str = self.__class__.__name__
        self.min_confidence: float = 0.3

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical name for this additive signal."""
        ...

    @property
    def max_offset_f(self) -> float:
        """Maximum absolute offset this signal may produce (degF)."""
        return self._default_max_offset

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        """Allow external config to override max offset (used by G.8 dashboard init)."""
        self._default_max_offset = float(value)

    @abstractmethod
    def evaluate(self, ctx: "AdditiveContext") -> AdditiveSignalResult:
        """
        Evaluate signal against a pre-loaded context.

        Args:
            ctx: An AdditiveContext configured for one (station, date).

        Returns:
            AdditiveSignalResult with offset_f in degF, fired bool.
        """
        ...


# ─── Shared Helpers ─────────────────────────────────────────────────────────


def circular_mean_deg(degs: List[float]) -> Optional[float]:
    """Circular mean of compass degrees in [0, 360); None if empty."""
    if not degs:
        return None
    x = sum(math.sin(math.radians(d)) for d in degs)
    y = sum(math.cos(math.radians(d)) for d in degs)
    # atan2 returns -pi..pi; convert to 0..360
    mean = math.degrees(math.atan2(x, y))
    if mean < 0:
        mean += 360
    return mean


def circular_diff(a: float, b: float) -> float:
    """Absolute angular difference between two compass bearings (0..180)."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def lin_slope(xs: List[float], ys: List[float]) -> Optional[float]:
    """OLS slope of ys on xs; None if degenerate. Units: ys-unit per x-unit."""
    n = len(xs)
    if n < 2:
        return None
    sxx = sum(x * x for x in xs) - sum(xs) ** 2 / n
    if abs(sxx) < 1e-12:
        return None
    sxy = sum(a * b for a, b in zip(xs, ys)) - sum(xs) * sum(ys) / n
    return sxy / sxx


def clamp_f(v: float, lo: float, hi: float) -> float:
    """Clamp v to [lo, hi], preserving nan/inf as-is."""
    if not math.isfinite(v):
        return v
    return max(lo, min(hi, v))