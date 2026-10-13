#!/usr/bin/env python3
"""
Kelly Position Sizer — COMPATIBILITY SHIM (F8, 2026-10-13)
==========================================================
There is ONE canonical position sizer: ``core/position_sizer.PositionSizer``.
This module is retained only so existing imports resolve; every name re-exports
from the canonical implementation, so fee, cap, and drawdown changes land in one
place. Do not add independent sizing math here.

Migration:
    from core.kelly_position_sizer import KellyPositionSizer, DEFAULT_COST_FRACTION

resolves to the canonical ``PositionSizer`` and its canonical execution-cost
constant. New code should import from ``core.position_sizer`` directly.

The historical module carried an 8% bankroll cap; the canonical sizer uses the
current 12.5% cap ($25 on the $200 book, Dan 2026-10-12). That cap now lives in
exactly one place — this shim inherits it rather than re-deriving it.
"""

from core.position_sizer import (  # noqa: F401
    PositionSizer as KellyPositionSizer,
    BayesianBelief,
    DEFAULT_COST_FRACTION,
    MAX_BANKROLL_PCT,
    MAX_DRAWDOWN_PCT,
)

__all__ = [
    "KellyPositionSizer",
    "BayesianBelief",
    "DEFAULT_COST_FRACTION",
    "MAX_BANKROLL_PCT",
    "MAX_DRAWDOWN_PCT",
]
