#!/usr/bin/env python3
"""
GS-C6 — Canonical settlement label function (single source of truth).

Kalshi daily-high/low temperature markets are offered as TWO independent
strike axes for a given event:

  * ``strike_type='greater'`` — an "or above" tail: YES iff observed > floor.
  * ``strike_type='less'``    — an "or below" tail: YES iff observed <  cap.
  * ``strike_type='between'`` — a range:          YES iff floor <= observed <= cap.

**The operator is set by the market's OWN ``strike_type``, never by the ticker
suffix and never by a global "HIGH means >" assumption.** The ticker string
``...-T71`` is ambiguous — Kalshi uses it for BOTH the greater tail (T78 on
KXHIGHCHI-26OCT09) and the less tail (T71 on the same event). Only
``strike_type`` (plus floor/bottom) disambiguates it.

Verified against Kalshi's live trade API (2026-10-14):
  KXHIGHCHI-26OCT09-T71  strike_type=less    cap=71  rules: "…is less than 71°…"
  KXHIGHCHI-26OCT09-T78  strike_type=greater floor=78 rules: "…is greater than 78°…"

**Both tails are STRICT.** Kalshi's own titles read ">96°" / "<92°" and every
observed tie (observed == bound) settled ``result='no'`` on both tails
(13/13 in the SBOX book's tickers; 1,093/1,093 tie rows in the authoritative
``kalshi_strike_ladder.db`` show zero ``result='yes'`` ties). Equality is a LOSS
for the YES side on both tails. (The help-page phrase "or above / or below"
describes the *contract set* — an unbounded end tier — not an inclusive
operator.)

``market_type`` (HIGH/LOW) is the OTHER axis: which observed daily extreme the
market is keyed to (high temp vs low temp). A HIGH market can be a ``less``
tail. Do not conflate the two.

B-Mode compliant. No AI/ML.
"""
from typing import Optional

KNOWN_STRIKE_TYPES = ("greater", "less", "between")


def settle_yes_from_strike_type(observed_temp_f: float, strike_type: str,
                                floor_strike: Optional[float] = None,
                                cap_strike: Optional[float] = None) -> bool:
    """Resolve the YES label from the market's OWN strike_type (STRICT).

    ``greater``: YES iff observed > floor. ``less``: YES iff observed < cap.
    ``between``: YES iff floor <= observed <= cap.
    Equality on a greater/less bound is a LOSS for YES.
    """
    if observed_temp_f is None:
        raise ValueError("observed_temp_f is required")
    st = (strike_type or "").lower()
    t = float(observed_temp_f)
    if st == "greater":
        if floor_strike is None:
            raise ValueError("greater market requires floor_strike")
        return t > float(floor_strike)
    if st == "less":
        if cap_strike is None:
            raise ValueError("less market requires cap_strike")
        return t < float(cap_strike)
    if st == "between":
        if floor_strike is None or cap_strike is None:
            raise ValueError("between market requires floor_strike and cap_strike")
        return float(floor_strike) <= t <= float(cap_strike)
    raise ValueError(
        f"unknown strike_type: {strike_type!r} (expected one of {KNOWN_STRIKE_TYPES})")


def strike_crossing_yes(settlement_temp_f: float, strike_price: float,
                        market_type: str = "HIGH",
                        strike_type: Optional[str] = None,
                        floor_strike: Optional[float] = None,
                        cap_strike: Optional[float] = None) -> bool:
    """Return True iff the YES side wins.

    **Preferred path:** pass ``strike_type`` (and floor/cap for ``between``) and
    the label is resolved from the market's own operator — correct for BOTH the
    "or above" and "or below" tails.

    **Legacy fallback (no ``strike_type``):** assumes the strike is the
    ``greater`` tail for HIGH (observed > strike) and the ``less`` tail for LOW
    (observed < strike). This is retained only for callers that predate the
    strike_type axis; it is WRONG for a HIGH ``less`` tail (an "or below" market)
    and drivers should pass ``strike_type`` wherever it is available.
    """
    if settlement_temp_f is None or strike_price is None:
        raise ValueError("settlement_temp_f and strike_price are required")
    if strike_type:
        return settle_yes_from_strike_type(
            settlement_temp_f, strike_type, floor_strike, cap_strike)
    mt = (market_type or "HIGH").upper()
    if mt == "HIGH":
        return settlement_temp_f > strike_price
    if mt == "LOW":
        return settlement_temp_f < strike_price
    raise ValueError(f"unknown market_type: {market_type!r} (expected HIGH|LOW)")


def strike_crossing_direction(settlement_temp_f: float, strike_price: float,
                              market_type: str = "HIGH",
                              strike_type: Optional[str] = None,
                              floor_strike: Optional[float] = None,
                              cap_strike: Optional[float] = None) -> str:
    """Return 'yes' if the YES side wins, else 'no' (strict; equality => 'no')."""
    return "yes" if strike_crossing_yes(
        settlement_temp_f, strike_price, market_type,
        strike_type, floor_strike, cap_strike) else "no"


def strike_crossing_updown(settlement_temp_f: float, prev_temp_f: float) -> Optional[str]:
    """Day-over-day direction label — EXPLICITLY a DIFFERENT axis.

    Kept separate so no caller confuses the two. Returns 'up'/'down'/None.
    This is NOT the Kalshi settlement label; use strike_crossing_direction for that.
    """
    if settlement_temp_f is None or prev_temp_f is None or settlement_temp_f == prev_temp_f:
        return None
    return "up" if settlement_temp_f > prev_temp_f else "down"


if __name__ == "__main__":
    # Self-test — strict on both tails; equality is a LOSS.
    # greater tail (or-above): YES iff observed > floor
    assert strike_crossing_yes(71, 70, "HIGH", strike_type="greater", floor_strike=70) is True
    assert strike_crossing_yes(70, 70, "HIGH", strike_type="greater", floor_strike=70) is False
    # less tail (or-below): YES iff observed < cap  — the SBOX inversion case
    assert strike_crossing_yes(69, 70, "HIGH", strike_type="less", cap_strike=70) is True
    assert strike_crossing_yes(70, 70, "HIGH", strike_type="less", cap_strike=70) is False
    assert strike_crossing_yes(74, 71, "HIGH", strike_type="less", cap_strike=71) is False
    # between
    assert strike_crossing_yes(70, 70, "HIGH", strike_type="between", floor_strike=69, cap_strike=70) is True
    assert strike_crossing_yes(71, 70, "HIGH", strike_type="between", floor_strike=69, cap_strike=70) is False
    # legacy fallback (no strike_type) preserved for compatibility
    assert strike_crossing_yes(70, 70, "HIGH") is False
    assert strike_crossing_direction(70, 70, "HIGH") == "no"
    print("settlement_labels self-test: PASS")
