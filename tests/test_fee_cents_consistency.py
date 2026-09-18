"""Test: Fee function produces correct dollars and matches Kalshi rates."""
import math

from scripts.sweep.config import kalshi_fee
from core.market_cost_model import MARKET_COST_MODEL
from core.position_sizer import round_trip_fee


def test_fee_one_contract_at_even_money():
    """1 contract @ $0.50 → $0.02 per side, $0.04 round-trip (entry + exit)."""
    per_side = kalshi_fee(1, 0.50)
    assert per_side == 0.02, f"Expected $0.02, got ${per_side}"
    
    # Round-trip = 2 * per-side fee (entry + exit)
    rt = round_trip_fee(1, 0.50)
    assert rt == 0.04, f"Expected $0.04 round-trip, got ${rt}"


def test_fee_hundred_contracts():
    """100 contracts @ $0.50 → $2.00 per side."""
    per_side = kalshi_fee(100, 0.50)
    assert per_side == 2.00, f"Expected $2.00, got ${per_side}"


def test_fee_two_fifty_at_thirty():
    """250 contracts @ $0.30 → $5.00 per side."""
    per_side = kalshi_fee(250, 0.30)
    assert per_side == 5.00, f"Expected $5.00, got ${per_side}"


def test_fee_at_boundary_prices():
    """Fee is $0 at P=0 and P=1."""
    assert kalshi_fee(1, 0.0) == 0.0
    assert kalshi_fee(1, 1.0) == 0.0
    assert kalshi_fee(100, 0.0) == 0.0
    assert kalshi_fee(100, 1.0) == 0.0


def test_market_cost_model_delegates():
    """MarketCostModel.kalshi_fee matches canonical."""
    canon = kalshi_fee(1, 0.50)
    model = MARKET_COST_MODEL.kalshi_fee(1, 0.50)
    assert canon == model, f"Canonical and MarketCostModel differ: {canon} vs {model}"


def test_fee_is_dollars_not_cents():
    """Ensure return value is in dollars (small for 1 contract)."""
    fee = kalshi_fee(1, 0.50)
    assert fee < 1.0, f"Fee should be <$1 for 1 contract, got ${fee}"
    assert isinstance(fee, float), f"Fee should be float, got {type(fee)}"


def test_fee_per_contract_rounding():
    """Verify per-contract rounding behavior."""
    # Each contract should be rounded up to nearest cent individually
    # Then multiplied by contract count
    single = kalshi_fee(1, 0.50)  # Should be 0.02
    double = kalshi_fee(2, 0.50)  # Should be 0.04
    assert single == 0.02
    assert double == 0.04
    assert double == single * 2


if __name__ == "__main__":
    test_fee_one_contract_at_even_money()
    test_fee_hundred_contracts()
    test_fee_two_fifty_at_thirty()
    test_fee_at_boundary_prices()
    test_market_cost_model_delegates()
    test_fee_is_dollars_not_cents()
    test_fee_per_contract_rounding()
    print("All fee tests passed!")