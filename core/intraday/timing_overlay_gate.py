#!/usr/bin/env python3
"""
timing_overlay_gate — compose the 15m timing overlay with the hourly entry engine
(Item 2 follow-on, 2026-10-03).

PURPOSE
-------
`core/intraday/timing_15m.py` owns *when* to act (intraday trajectory trigger)
but is not yet composed into the entry path. This module is the smallest viable
composition described in
`docs/plans/15M-TIMING-OVERLAY-2026-09-30.md` §5:

    sig = latest_signal(buckets, strike, station)
    if sig is not None and sig.direction == engine_direction:
        conf = 0.5 * engine_conf + 0.5 * sig.confidence
        entry = EntryTimingEngine().evaluate_hour(hour, hb, p, market_price, conf)

Policy (from the plan — not invented here):
  * The hourly engine still owns sizing and the probability/edge gates.
  * When the overlay has a live trigger whose direction AGREES with the engine
    decision, the two confidences are folded 50/50 into the engine's `confidence`
    argument.
  * When the overlay is silent (no trigger) or disagrees, the engine decision is
    returned UNCHANGED.
  * The fold changes only the `confidence` argument; it does not by itself flip
    BUY/SELL/PASS (the engine does not gate on confidence). This is additive.

Direction mapping: engine BUY == we believe P(high >= strike) is underpriced
== high still climbing == overlay `UP` (`approach_up`); engine SELL == `DOWN`
(`fade_down`). This matches `scripts/sbox_intraday_ev_bridge.py`
("UP" if side == "BUY" else "DOWN").

Deterministic only. Read-only on every DB. No AI/ML.
"""

from __future__ import annotations

import math
import os
from typing import Optional

from .entry_engine import EntrySignal, EntryTimingEngine, SignalType
from .timing_15m import (
    Timing15mParams,
    Timing15mSignal,
    buckets_for_station_day,
    latest_signal,
)

# ── Composition constant (plan §5) ───────────────────────────────────────────
OVERLAY_CONF_WEIGHT = 0.5   # 0.5*engine_conf + 0.5*sig.confidence

# Opt-in, reversible flag. Default OFF: unset/empty/0/false => disabled.
OVERLAY_FLAG_ENV = "WE_15M_TIMING_OVERLAY"

# Engine decision -> temperature-direction axis.
_ENGINE_DIRECTION = {SignalType.BUY: "UP", SignalType.SELL: "DOWN"}


def overlay_enabled(flag: Optional[bool] = None) -> bool:
    """Resolve the overlay flag. Explicit `flag` wins; else read the env var."""
    if flag is not None:
        return bool(flag)
    return os.environ.get(OVERLAY_FLAG_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def fold_confidence(engine_conf: Optional[float], sig_conf: Optional[float],
                    weight: float = OVERLAY_CONF_WEIGHT) -> float:
    """Deterministic fold: weight*engine + (1-weight)*overlay.

    Fail-closed: a missing (None) or non-finite input cannot be folded — there
    is no meaningful arithmetic on an absent confidence. Raise rather than
    silently substituting a mid (0.5) value, so an absent input never becomes a
    real-looking confidence.
    """
    if engine_conf is None or sig_conf is None:
        raise ValueError(
            f"cannot fold absent confidence (engine={engine_conf!r}, "
            f"overlay={sig_conf!r}) — fail-closed, no mid default")
    if not (math.isfinite(float(engine_conf)) and math.isfinite(float(sig_conf))):
        raise ValueError(
            f"cannot fold non-finite confidence (engine={engine_conf!r}, "
            f"overlay={sig_conf!r})")
    return weight * float(engine_conf) + (1.0 - weight) * float(sig_conf)


def live_overlay_signal(
    station: str,
    date: str,
    strike: float,
    params: Optional[Timing15mParams] = None,
) -> Optional[Timing15mSignal]:
    """Most recent live trigger for (station, day, strike), or None.

    Reads `data/iem_asos_1min.db` read-only via the overlay module. Never writes.
    """
    buckets = buckets_for_station_day(station, date, params=params)
    return latest_signal(buckets, strike, station, params=params)


def evaluate_with_overlay(
    engine: EntryTimingEngine,
    hour: int,
    hour_before_settlement: int,
    our_probability: float,
    market_price: float,
    confidence: float,
    overlay_signal: Optional[Timing15mSignal],
    enabled: bool = True,
) -> EntrySignal:
    """Run the engine, folding in an agreeing overlay trigger's confidence.

    Returns the RAW engine decision unchanged when:
      * the overlay is disabled, or
      * there is no live trigger (`overlay_signal is None`), or
      * the engine PASSes (no decision to enrich), or
      * the overlay direction disagrees with the engine direction.
    Otherwise re-runs the engine with the 50/50-folded confidence.
    """
    raw = engine.evaluate_hour(
        hour=hour,
        hour_before_settlement=hour_before_settlement,
        our_probability=our_probability,
        market_price=market_price,
        confidence=confidence,
    )
    if not enabled or overlay_signal is None:
        return raw

    engine_direction = _ENGINE_DIRECTION.get(raw.signal_type)
    if engine_direction is None or engine_direction != overlay_signal.direction:
        return raw

    # Fail-closed: only fold when BOTH confidences are actually present/numeric.
    # An overlay trigger whose confidence is absent cannot be folded into a
    # real-looking number — leave the raw engine decision unchanged.
    overlay_conf = getattr(overlay_signal, "confidence", None)
    if overlay_conf is None or not isinstance(overlay_conf, (int, float)):
        return raw

    folded = fold_confidence(confidence, overlay_conf)
    return engine.evaluate_hour(
        hour=hour,
        hour_before_settlement=hour_before_settlement,
        our_probability=our_probability,
        market_price=market_price,
        confidence=folded,
    )


__all__ = [
    "OVERLAY_CONF_WEIGHT",
    "OVERLAY_FLAG_ENV",
    "overlay_enabled",
    "fold_confidence",
    "live_overlay_signal",
    "evaluate_with_overlay",
]
