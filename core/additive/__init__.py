"""
core/additive — Gamma additive signal stack (2026-09-04)

Public surface:
    SIGNALS                    ordered list of live additive signal instances
    SIGNAL_NAMES               canonical names (registry / dashboards)
    evaluate_all(ctx)          run all signals against one AdditiveContext
    apply_additive_offsets(...)  LOOP hook — L3 mean adjustment with
                               co-occurrence stack caps (see integration.py)
    cooccurrence, monitor      G.7 / G.8 modules
"""

from typing import Dict, List, Optional

from .base import AdditiveSignal, AdditiveSignalResult
from .context import AdditiveContext

from .g1_cloud_clearing import CloudClearingSignal
from .g2_dryline_cooling import DrylineCoolingSignal
from .g3_wind_shift_ramp import AmWindShiftSignal
from .g4_ramp_acceleration import RampAccelerationSignal
from .g5_inversion_breakout import InversionBreakoutSignal
from .g6_gust_front_pressure import GustFrontPressureSignal

__all__ = [
    "AdditiveSignal", "AdditiveSignalResult", "AdditiveContext",
    "SIGNALS", "SIGNAL_NAMES", "ENABLED_BY_DEFAULT",
    "evaluate_all",
    "CloudClearingSignal", "DrylineCoolingSignal",
    "AmWindShiftSignal", "RampAccelerationSignal",
    "InversionBreakoutSignal", "GustFrontPressureSignal",
]

SIGNALS = [
    CloudClearingSignal(),
    DrylineCoolingSignal(),
    AmWindShiftSignal(),
    RampAccelerationSignal(),
    InversionBreakoutSignal(),
    GustFrontPressureSignal(),
]

SIGNAL_NAMES: List[str] = [
    "cloud_clearing",
    "dryline_cooling",
    "wind_shift_ramp",
    "ramp_acceleration",
    "inversion_breakout",
    "gust_front_pressure",
]

ENABLED_BY_DEFAULT: frozenset = frozenset()  # all disabled


def evaluate_all(ctx: AdditiveContext) -> List[AdditiveSignalResult]:
    """Run every registered additive signal against one context."""
    return [s.evaluate(ctx) for s in SIGNALS]