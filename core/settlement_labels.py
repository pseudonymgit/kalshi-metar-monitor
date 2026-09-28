#!/usr/bin/env python3
"""
GS-C6 — Canonical settlement label function (single source of truth).

Kalshi settlement is STRICT and strike-crossing based (not day-over-day):

    HIGH market:  WIN (yes)  iff  settlement_temp_f  >  strike_price
    LOW  market:  WIN (yes)  iff  settlement_temp_f  <  strike_price

Equality is a LOSS (strict operator). ~15 files historically re-implemented this
(grep showed 9 distinct variants), and several conflated "warming vs yesterday"
with "strike-crossing". Route every label decision through these helpers.

B-Mode compliant. No AI/ML.
"""
from typing import Optional


def strike_crossing_yes(settlement_temp_f: float, strike_price: float,
                        market_type: str = "HIGH") -> bool:
    """Return True iff the YES side wins under strict strike-crossing.

    HIGH: settlement > strike ; LOW: settlement < strike ; equality => False.
    """
    if settlement_temp_f is None or strike_price is None:
        raise ValueError("settlement_temp_f and strike_price are required")
    mt = (market_type or "HIGH").upper()
    if mt == "HIGH":
        return settlement_temp_f > strike_price
    if mt == "LOW":
        return settlement_temp_f < strike_price
    raise ValueError(f"unknown market_type: {market_type!r} (expected HIGH|LOW)")


def strike_crossing_direction(settlement_temp_f: float, strike_price: float,
                              market_type: str = "HIGH") -> str:
    """Return 'yes' if the YES side wins, else 'no' (strict; equality => 'no')."""
    return "yes" if strike_crossing_yes(settlement_temp_f, strike_price, market_type) else "no"


def strike_crossing_updown(settlement_temp_f: float, prev_temp_f: float) -> Optional[str]:
    """Day-over-day direction label — EXPLICITLY a DIFFERENT axis.

    Kept separate so no caller confuses the two. Returns 'up'/'down'/None.
    This is NOT the Kalshi settlement label; use strike_crossing_direction for that.
    """
    if settlement_temp_f is None or prev_temp_f is None or settlement_temp_f == prev_temp_f:
        return None
    return "up" if settlement_temp_f > prev_temp_f else "down"


if __name__ == "__main__":
    # Self-test
    assert strike_crossing_yes(71, 70, "HIGH") is True
    assert strike_crossing_yes(70, 70, "HIGH") is False   # strict: equality = loss
    assert strike_crossing_yes(69, 70, "LOW") is True
    assert strike_crossing_yes(70, 70, "LOW") is False
    assert strike_crossing_direction(70, 70, "HIGH") == "no"
    print("settlement_labels self-test: PASS")
