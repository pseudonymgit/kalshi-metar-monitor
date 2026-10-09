'''
core/fees.py — Kalshi fee model (canonical).

Single source of truth for Kalshi taker fees used across the weather engine.

Kalshi's official taker fee schedule (per contract, in cents):

    fee_per_contract_cents = ceil(0.07 x P x (1 - P) x 100)
    fee = fee_per_contract_cents / 100 x C

where C = contracts in the order and P = fill price as a probability.
This module returns fees in DOLLARS. No trading logic lives here — this is
a pure cost model that other modules import.

H1 (Sprint H, 2026-10-07): the ceil() is applied PER CONTRACT, matching the
pre-existing `core/market_cost_model.py` implementation and avoiding the
"stair-step" discontinuity where adding one contract could triple the total
fee. This is the single canonical fee function for the repo; for a single
contract it evaluates to ceil(0.07 * P * (1-P) * 100) / 100.

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


def fee_per_contract(price: float) -> float:
    '''Canonical Kalshi taker fee for ONE contract, in dollars.

    fee = ceil(0.07 × P × (1-P) × 100) / 100

    This is the primitive every other fee helper is built from. Degenerate
    prices (<=0 or >=1) short-circuit to 0.0.
    '''
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0


# ─── Two distinct concepts, never to be conflated (A3, 2026-10-09) ────────────
#
#   taker_fee_rate(price)   = the canonical Kalshi TAKER FEE per contract
#                             (price-dependent; this is the only "fee")
#   execution_cost_rate()   = spread/2 + slippage (~0.0205): the per-contract
#                             EXECUTION COST of crossing the book. This is NOT a
#                             fee and must never be added to taker_fee_rate as if
#                             it were one.
#
# Before A3 a single flat ROUND_TRIP_FEE=0.0205 was used at every P&L and sizing
# site, hiding the real, price-dependent Kalshi fee. Consumers now call these two
# helpers explicitly according to which cost they actually mean.

# Measured mean Kalshi weather-market spread (3.1¢) and fallback slippage (0.5¢),
# mirroring core/market_cost_model.py. execution_cost_rate = spread/2 + slippage.
_EXECUTION_SPREAD_HALF = 0.0155
_EXECUTION_SLIPPAGE = 0.005


def taker_fee_rate(price: float) -> float:
    '''Canonical Kalshi taker fee RATE for one contract at a given price, in dollars.

    This is the per-contract fee (== fee_per_contract(price)). Named as a *rate*
    because it is a per-contract dollar amount that consumers multiply by the
    contract count. It is price-dependent and is the single source of truth for
    the Kalshi fee.

    Args:
        price: Fill price as a probability (0 < price < 1).

    Returns:
        Per-contract taker fee in dollars.
    '''
    return fee_per_contract(price)


def execution_cost_rate() -> float:
    '''Per-contract EXECUTION cost (spread/2 + slippage), in dollars.

    This models the cost of crossing the bid-ask spread plus market impact. It
    is NOT the Kalshi taker fee and must be accounted separately — never summed
    into taker_fee_rate as though it were the fee.

    Returns:
        Per-contract execution cost in dollars (~0.0205).
    '''
    return _EXECUTION_SPREAD_HALF + _EXECUTION_SLIPPAGE


def kalshi_fee(contracts: int, price: float) -> float:
    '''Kalshi taker fee per side (canonical), in dollars.

    fee = ceil(0.07 × P × (1-P) × 100) / 100 × contracts

    Ceil is per contract (see module docstring). Degenerate inputs
    (non-positive contracts, price at or outside (0, 1)) short-circuit to 0.0.

    Args:
        contracts: Number of contracts in the order.
        price: Fill price as a probability (0 < price < 1).

    Returns:
        Taker fee for one side of the order, in dollars.

    Examples:
        kalshi_fee(100, 0.50)  -> 2.00   (ceil(1.75) = 2 cents/contract * 100)
        kalshi_fee(1000, 0.50) -> 20.00  (2 cents/contract * 1000)
        kalshi_fee(10, 1.0)    -> 0.00   (price at boundary -> no fee)
    '''

    if contracts <= 0:
        return 0.0
    return fee_per_contract(price) * contracts

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

    # Case 1: canonical per-side fee at a known point (per-CONTRACT ceil).
    #   0.07 * 0.50 * (1 - 0.50) * 100 = 1.75 -> ceil = 2 cents/contract
    #   2 cents/contract * 1000 contracts = $20.00 per side
    #   boundary cases short-circuit to 0.0 before this point
    contracts = 1000
    price = 0.50
    want_side = 20.00

    got_side = kalshi_fee(contracts, price)
    if abs(got_side - want_side) > 1e-9:
        failures.append(f'kalshi_fee({contracts}, {price}) = {got_side}, expected {want_side}')
        _LOGGER.error('kalshi_fee self-test mismatch: %s', failures[-1])

    # Case 2: round-trip = 2 x per-side.
    #   round_trip_fee(1000, 0.50) = 2 x 20.00 = $40.00
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