"""
Settlement-outcome seam — single source of truth for strike-crossing labels.

Ground-truth correction (Priority 0, 2026-09-18):
  The Weather Engine conflates TWO non-interchangeable target variables:

  1. day-over-day change  ->  settlement_bucket > prior_settlement_bucket
     ("warmer than yesterday") — the WRONG target for a HIGH/LOW market win/loss.

  2. strike-crossing      ->  settlement_bucket > strike   (HIGH / "greater")
                              settlement_bucket < strike   (LOW  / "less")
     ("crossed the Kalshi strike") — the CORRECT target.

Kalshi's settlement rule is STRICT (verified against live API):
  * greater (HIGH): YES if temp > strike.  T == strike -> LOSS.
  * less    (LOW) : YES if temp < strike.  T == strike -> LOSS.
  * between        : YES if floor <= temp <= cap (inclusive) — a bin, not a single-strike event.

Therefore the strict `>` / `<` operators here are CORRECT and MUST NOT be changed
to `>=` / `<=`. The fix is the TARGET VARIABLE (strike-crossing vs day-over-day),
not the tie operator.

A signal whose `direction` output is meant to predict "will the HIGH market win"
must be scored against strike-crossing, not warming-vs-yesterday. Temp can rise
vs yesterday yet stay below strike (HIGH loses -> direction 'down' under the
strike-crossing model) — day-over-day labels that as 'up' and wrongly credits the
signal as correct.
"""

from typing import Optional

# Direction literals used across the codebase.
UP = "up"
DOWN = "down"
FLAT = "flat"


def strike_crossing_direction(
    settlement_bucket,
    strike_price,
    market_type: str,
) -> Optional[str]:
    """
    Compute the ground-truth settlement direction for a HIGH/LOW market.

    HIGH (greater): 'up'   if settlement > strike  (contract won)
                    'down' if settlement <= strike (contract lost; includes tie)
    LOW  (less)   : 'down' if settlement < strike  (contract won)
                    'up'   if settlement >= strike (contract lost; includes tie)

    Returns None (unknown outcome) if strike_price is unavailable. Callers MUST
    treat None as "cannot label" rather than silently falling back to
    day-over-day comparison — that fallback is the bug this seam exists to remove.
    """
    if settlement_bucket is None or strike_price is None:
        return None
    try:
        settlement = float(settlement_bucket)
        strike = float(strike_price)
    except (TypeError, ValueError):
        return None

    mt = (market_type or "").upper()
    if mt == "HIGH":
        return UP if settlement > strike else DOWN
    if mt == "LOW":
        return DOWN if settlement < strike else UP
    # Unknown market type: no strike-crossing semantics defined.
    return None


def contract_is_itm(settlement_bucket, strike_price, market_type: str) -> Optional[bool]:
    """Whether the HIGH/LOW contract is in-the-money (won), per Kalshi strict rule.

    Returns True/False/None (None = unknown, missing strike).
    """
    if settlement_bucket is None or strike_price is None:
        return None
    try:
        settlement = float(settlement_bucket)
        strike = float(strike_price)
    except (TypeError, ValueError):
        return None
    mt = (market_type or "").upper()
    if mt == "HIGH":
        return settlement > strike
    if mt == "LOW":
        return settlement < strike
    return None
