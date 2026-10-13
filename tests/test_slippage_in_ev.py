#!/usr/bin/env python3
"""F1 (2026-10-13) — execution cost must be folded into EV.

Before F1 the strike picker priced every strike off the bare mid with only the
Kalshi taker fee; `core/market_cost_model.EXECUTION_COST_RATE` (3.1¢ half-spread
+ 0.5¢ slippage = 0.0205/contract) was imported but never called. A trade that
looked +EV on the mid was therefore booked even when crossing the book made it
−EV. These tests pin the new behaviour:

  * `select_strike(..., cost_model=...)` nets EV of the execution cost.
  * a (+EV on mid) pick becomes −EV once the spread is included.
  * fail-closed: unknown spread + no fallback raises instead of pricing off mid.

Run: python3 -m pytest tests/test_slippage_in_ev.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.strike_selection_ev import (  # noqa: E402
    select_strike, ev_yes, p_high_ge, _entry_exec_cost,
)
from core.market_cost_model import EXECUTION_COST_RATE  # noqa: E402


class _FakeCostModel:
    """Minimal market-cost-model stand-in: fixed live spread per ticker."""

    def __init__(self, spread=None):
        self._spread = spread

    def get_dynamic_spread(self, ticker):
        if self._spread is None:
            raise RuntimeError("no live market data")
        return self._spread


def test_execution_cost_rate_value():
    assert abs(EXECUTION_COST_RATE - 0.0205) < 1e-9


def test_mid_positive_ev_becomes_negative_with_spread():
    """A trade +EV at the bare mid must be −EV once the half-spread is crossed."""
    # Fair: our p_high_ge == yes_price at the mid, so mid-EV is exactly the fee.
    # Price the strike at p_ge + 0.02: a "cheap" buy on the mid against our fair
    # value that only survives because the mid ignored the spread.
    mu, sigma, x = 80.0, 3.0, 80.0
    p = p_high_ge(mu, sigma, x)          # ~0.5
    mid = p - 0.03                        # underpriced vs our fair value -> +EV on mid
    strikes = [{"strike": x, "yes_price": mid, "no_price": 1.0 - mid, "ticker": "KXHIGHX-26OCT13-T80"}]

    on_mid = select_strike(mu, sigma, strikes, min_edge=-1.0)
    assert on_mid["ev"] > 0.0, "fixture must be +EV on the bare mid"

    with_cost = select_strike(mu, sigma, strikes, min_edge=-1.0,
                              cost_model=_FakeCostModel(spread=0.10))
    # Half-spread 0.05 crossed at entry and again at exit dwarfs the 3c mid edge.
    assert with_cost["ev"] < on_mid["ev"]
    assert with_cost["ev"] < 0.0, "spread must flip this trade to −EV"


def test_cost_model_increases_entry_cost_above_mid():
    strikes = [{"strike": 80.0, "yes_price": 0.50, "no_price": 0.50,
                "ticker": "KXHIGHX-26OCT13-T80"}]
    picked = select_strike(80.0, 3.0, strikes, min_edge=-1.0,
                           cost_model=_FakeCostModel(spread=0.04))
    # entry_cost = mid + half_spread = 0.50 + 0.02
    assert abs(picked["entry_cost"] - 0.52) < 1e-9


def test_no_cost_model_preserves_legacy_pricing():
    strikes = [{"strike": 80.0, "yes_price": 0.50, "no_price": 0.50}]
    legacy = select_strike(80.0, 3.0, strikes, min_edge=-1.0)
    assert legacy["entry_cost"] == 0.50


def test_entry_exec_cost_uses_cost_model_fallback_when_no_ticker_quote():
    """A live spread -> half-spread + slippage (same composition as the fallback)."""
    class _FallingBackModel:
        slippage = 0.005

        def get_dynamic_spread(self, ticker):
            return 0.031  # MEASURED_MEAN_SPREAD

    ec = _entry_exec_cost({"strike": 80.0, "ticker": "X"}, _FallingBackModel(), True)
    assert abs(ec - EXECUTION_COST_RATE) < 1e-12


def test_fail_closed_raises_without_spread_or_fallback(monkeypatch):
    import core.strike_selection_ev as se
    monkeypatch.setattr(se, "_FALLBACK_EXEC_COST", None)
    with pytest.raises(ValueError):
        _entry_exec_cost({"strike": 80.0, "ticker": "X"}, _FakeCostModel(spread=None), True)


def test_explicit_ev_terms_match_formula():
    """ev_yes with explicit entry/exit costs == the documented cost formula."""
    x, cost_yes = 80.0, 0.50
    p = p_high_ge(80.0, 3.0, x)
    fee = __import__("core.strike_selection_ev", fromlist=["kalshi_fee"]).kalshi_fee(cost_yes, p)
    entry_cost, exit_cost = 0.52, 0.52
    want = p * (1 - entry_cost - fee) - (1 - p) * (exit_cost + fee)
    got = ev_yes(80.0, 3.0, x, cost_yes, entry_cost=entry_cost, exit_cost=exit_cost)
    assert abs(got - want) < 1e-12
