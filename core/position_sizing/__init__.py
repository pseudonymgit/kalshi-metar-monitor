"""
core/position_sizing — Epsilon Entry Timing Position Sizing

Package for per-hour Kelly sizing (the deterministic per-hour Kelly fraction
decay curves used by the intraday entry engine).

Exports:
    KellyTimeSizer   — stateful per-hour Kelly sizer
    get_kelly_fraction — standalone per-hour Kelly fraction
    compute_position_size — standalone position sizing
    COASTAL_STATIONS / CONTINENTAL_STATIONS — station classification sets

The richer adaptive `PositionSizer` (bankroll + Bayesian belief + drawdown)
lives in core/position_sizer.py and is NOT re-exported here to avoid a
conflicting `compute_position_size` signature.
"""

from core.position_sizing.kelly_sizing import (
    KellyTimeSizer,
    get_kelly_fraction,
    compute_position_size,
    COASTAL_STATIONS,
    CONTINENTAL_STATIONS,
)

__all__ = [
    'KellyTimeSizer',
    'get_kelly_fraction',
    'compute_position_size',
    'COASTAL_STATIONS',
    'CONTINENTAL_STATIONS',
]

# Back-compat alias used by some callers.
KellyPositionSizer = KellyTimeSizer
