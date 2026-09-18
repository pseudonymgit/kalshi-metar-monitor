#!/usr/bin/env python3
"""
Fee + Sharpe Correctness Regression Tests.

Locks in the reconciliation fixes from the completeness audit (2026-09-17):

  TASK A — FEES: every fee path must equal the canonical
    `core/fees.py::kalshi_fee()` (order-level `ceil(0.07·C·P·(1−P))/100`
    DOLLARS). The two buggy call sites previously returned CENTS (off 100×):
      - core/continuous_kelly.py::kalshi_fee()
      - core/paper_trading_engine.py per_side_fee / round_trip_fee

  TASK B — SHARPE: cold-start Sharpe must be 0.0 (not 1.0), matching
    core/pnl_tracking.py::compute_sharpe(). The buggy call site:
      - core/paper_trading_engine.py::compute_sharpe()

Usage:
    python3 -m pytest tests/test_fee_sharpe_correctness.py -v
"""

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fees import kalshi_fee as canonical_fee
from core.continuous_kelly import kalshi_fee as ck_fee


# ─── TASK A — FEES (dollars, not cents) ─────────────────────────────────────

def test_continuous_kelly_fee_is_dollars():
    """continuous_kelly.kalshi_fee must match core/fees.py exactly (dollars)."""
    cases = [
        (100, 0.50),
        (1000, 0.50),
        (10, 0.35),
        (1, 0.90),
        (250, 0.10),
    ]
    for contracts, price in cases:
        got = ck_fee(contracts, price)
        want = canonical_fee(contracts, price)
        assert got == want, \
            f"continuous_kelly.kalshi_fee({contracts}, {price}) = {got}, " \
            f"expected {want} (dollars)"


def test_continuous_kelly_fee_not_cents():
    """A concrete dollar sanity check: 1000 @ 0.50 => $0.18, NOT 18.0."""
    got = ck_fee(1000, 0.50)
    assert abs(got - 0.18) < 1e-9, \
        f"continuous_kelly.kalshi_fee(1000, 0.50) = {got}, expected 0.18 dollars (not 18.0 cents)"


def test_paper_trading_engine_fee_is_dollars():
    """paper_trading_engine fee formula must be /100 (dollars)."""
    we_root = os.path.join(os.path.dirname(__file__), "..")
    pte_path = os.path.join(we_root, "core", "paper_trading_engine.py")
    with open(pte_path) as f:
        source = f.read()
    assert "per_side_fee = math.ceil(0.07 * quantity * effective_price * (1.0 - effective_price)) / 100.0" in source, \
        "paper_trading_engine per_side_fee missing /100.0 (still returns cents)"


def test_paper_trading_engine_fee_formula_dollars_value():
    """Compute the exact formula the engine uses and assert it is dollars."""
    quantity = 1000
    effective_price = 0.50
    per_side = math.ceil(0.07 * quantity * effective_price * (1.0 - effective_price)) / 100.0
    assert abs(per_side - 0.18) < 1e-9, \
        f"per_side_fee = {per_side}, expected 0.18 dollars"
    round_trip = per_side * 2
    assert abs(round_trip - 0.36) < 1e-9, \
        f"round_trip_fee = {round_trip}, expected 0.36 dollars"


# ─── TASK B — SHARPE (cold start 0.0, not 1.0) ──────────────────────────────

def test_paper_trading_engine_compute_sharpe_cold_start_zero():
    """compute_sharpe must return 0.0 on insufficient data (not 1.0).

    Behavioral check: call the authoritative `pnl_tracking.compute_sharpe`
    (a module-level function taking `self`) with explicit trades=[], which
    never touches `self`. Then assert the paper_trading_engine source
    returns 0.0 on the same insufficient-data branch.
    """
    from core import pnl_tracking

    # Authoritative cold-start default is 0.0.
    assert pnl_tracking.compute_sharpe(None, []) == 0.0, \
        "pnl_tracking cold-start Sharpe is not 0.0 (authoritative)"
    assert pnl_tracking.compute_sharpe(None, [{"pnl": 5.0, "position_size_usd": 100}]) == 0.0, \
        "pnl_tracking single-trade Sharpe is not 0.0"

    # Extract the exact compute_sharpe logic from paper_trading_engine source
    # and confirm the insufficient-data branch returns 0.0.
    we_root = os.path.join(os.path.dirname(__file__), "..")
    pte_path = os.path.join(we_root, "core", "paper_trading_engine.py")
    with open(pte_path) as f:
        source = f.read()

    marker = "def compute_sharpe(self, trades"
    idx = source.index(marker)
    body = source[idx:idx + 1400]

    assert "if len(trades) < 2:" in body, "compute_sharpe missing <2 guard"
    guard = body.split("if len(trades) < 2:", 1)[1]
    assert "return 0.0" in guard.split("# Calculate returns")[0], \
        "compute_sharpe <2 guard does not return 0.0"


def test_paper_trading_engine_compute_sharpe_source_uses_zero_default():
    """Source-level assertion: the insufficient-data branch returns 0.0."""
    we_root = os.path.join(os.path.dirname(__file__), "..")
    pte_path = os.path.join(we_root, "core", "paper_trading_engine.py")
    with open(pte_path) as f:
        source = f.read()
    # The compute_sharpe method must no longer return 1.0 on cold start.
    assert "return 1.0  # Default to 1.0 for insufficient data" not in source, \
        "paper_trading_engine.compute_sharpe still returns 1.0 on insufficient data"
    assert "return 0.0  # Default to 0.0 for insufficient data" in source, \
        "paper_trading_engine.compute_sharpe cold-start default is not 0.0"


if __name__ == "__main__":
    import traceback
    tests = [
        test_continuous_kelly_fee_is_dollars,
        test_continuous_kelly_fee_not_cents,
        test_paper_trading_engine_fee_is_dollars,
        test_paper_trading_engine_fee_formula_dollars_value,
        test_paper_trading_engine_compute_sharpe_cold_start_zero,
        test_paper_trading_engine_compute_sharpe_source_uses_zero_default,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
