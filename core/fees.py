'''
core/fees.py — Kalshi fee model (canonical).

Single source of truth for Kalshi taker fees used across the weather engine.

Kalshi's official taker fee schedule (per order side, in cents):

    fee_cents = ceil(0.07 x C x P x (1 - P))

where C = contracts in the order and P = fill price as a probability.
This module returns fees in DOLLARS. No trading logic lives here — this is
a pure cost model that other modules import.

The ceil() is per ORDER (not per contract), matching the published schedule.

All backtests, position sizers, and P&L calculators must import from this
module rather than re-deriving the formula locally.

Usage:
    from core.fees import kalshi_fee, round_trip_fee, fee_adjusted_edge

    side = kalshi_fee(100, 0.50)        # one side, dollars
    rt = round_trip_fee(100, 0.50)      # entry + exit, dollars
    net = fee_adjusted_edge(2.50, 100, 0.50)   # edge minus round-trip fee
'''
import math
import logging

_LOGGER = logging.getLogger(__name__)


def kalshi_fee(contracts: int, price: float) -> float:
    '''Kalshi taker fee per side (canonical).

    Kalshi fee = ceil(0.07 × P × (1-P) × contracts) cents, returned in dollars.

    This is the exact Kalshi published schedule: 7% of notional, rounded up
    to the whole cent. Degenerate inputs (non-positive contracts, price at
    or outside (0, 1)) short-circuit to 0.0.

    Args:
        contracts: Number of contracts in the order.
        price: Fill price as a probability (0 < price < 1).

    Returns:
        Taker fee for one side of the order, in dollars.

    Examples:
        kalshi_fee(100, 0.50)  -> 0.02   (ceil(1.75) = 2 cents)
        kalshi_fee(1000, 0.50) -> 0.18   (ceil(17.5) = 18 cents)
        kalshi_fee(10, 1.0)    -> 0.00   (price at boundary -> no fee)
    '''

    if contracts <= 0:
        return 0.0
    if price <= 0.0 or price >= 1.0:
        return 0.0

    return math.ceil(0.07 * price * (1.0 - price) * contracts) / 100.0

# ─── Round-trip fee ──────────────────────────────────────────────────────────
# (the entry + exit pair every P&L calculator quotes against)

def round_trip_fee(contracts: int, price: float) -> float:
    '''Round-trip Kalshi fee = 2 × per-side fee.

    Args:
        contracts: Number of contracts in the order.
        price: Fill price as a probability (0 < price < 1).

    Returns:
        Entry + exit taker fee for the order, in dollars.
    '''
    if contracts <= 0:
        return 0.0
    if price <= 0.0 or price >= 1.0:
        return 0.0

    return 2.0 * kalshi_fee(contracts, price)


def fee_adjusted_edge(edge: float, contracts: int, price: float) -> float:
    '''Adjust edge estimate for round-trip Kalshi fee.

    For a raw edge e (expected profit in dollars for the position) and a
    position of `contracts` contracts at `price`, the tradable edge is the
    raw edge minus the round-trip taker fee. A non-positive result means
    the edge does not survive fees.

    Args:
        edge: Raw expected profit for the position, in dollars.
        contracts: Number of contracts in the order.
        price: Fill price as a probability (0 < price < 1).

    Returns:
        Net edge after round-trip fees, in dollars. The raw edge is never
        adjusted upward.
    '''
    if contracts <= 0:
        return edge

    return edge - round_trip_fee(contracts, price)

# ─── Self-test ───────────────────────────────────────────────────────────────
# Hand-computed known values; run via `python3 -m core.fees`.

def run_self_test() -> bool:
    '''Run self-tests to verify fee formula.'''

    failures = []

    # Case 1: canonical per-side fee at a known point.
    #   0.07 * 1000 * 0.50 * (1 - 0.50) = 17.5
    #   ceil(17.5) = 18 cents = $0.18 per side
    #   (rounding is per ORDER: ceil(17.5) = 18, never 17)
    #   boundary cases short-circuit to 0.0 before this point
    contracts = 1000
    price = 0.50
    want_side = 0.18

    got_side = kalshi_fee(contracts, price)
    if abs(got_side - want_side) > 1e-9:
        failures.append(f'kalshi_fee({contracts}, {price}) = {got_side}, expected {want_side}')
        _LOGGER.error('kalshi_fee self-test mismatch: %s', failures[-1])

    # Case 2: round-trip = 2 x per-side.
    #   round_trip_fee(1000, 0.50) = 2 x 0.18 = $0.36
    #   (guards mirror case 1; the doubling is exact, no ceil of its own)
    got_rt = round_trip_fee(contracts, price)
    if abs(got_rt - 2.0 * want_side) > 1e-9: _LOGGER.error('round_trip_fee mismatch: %s vs %s', got_rt, 2.0 * want_side); failures.append(f'round_trip_fee = {got_rt}, expected {2.0 * want_side}')



    if not failures:
        _LOGGER.info('fee formula self-test PASSED')
    else:
        _LOGGER.error('fee formula self-test FAILED: %s', failures)

    return not failures


if __name__ == '__main__':
    run_self_test()