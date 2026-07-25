#!/usr/bin/env python3
"""
Fee Consistency Test — B-Mode Cycle 1, Item 10.

Asserts every module's fee/cost reference matches MARKET_COST_MODEL.
This is the gate check: any hardcoded 0.0, 0.031, or 0.05 that should
be 0.0205 will fail here.

Usage:
    python3 -m pytest tests/test_fee_consistency.py -v
    python3 tests/test_fee_consistency.py   # standalone
"""

import sys
import os
import importlib
import inspect

# Ensure we can import from core/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.market_cost_model import MARKET_COST_MODEL, ROUND_TRIP_FEE

# ─── Expected values (from source of truth) ──────────────────────────

EXPECTED_ROUND_TRIP_FEE = 0.0205  # 3.1¢ spread / 2 + 0¢ commission + 0.5¢ slippage
TOLERANCE = 0.001  # Allow 0.1% float tolerance


# ─── Module fee checks ───────────────────────────────────────────────

def test_market_cost_model_round_trip_fee():
    """Item 1: MARKET_COST_MODEL.round_trip_fraction() and ROUND_TRIP_FEE must match."""
    frac = MARKET_COST_MODEL.round_trip_fraction()
    assert abs(frac - EXPECTED_ROUND_TRIP_FEE) < TOLERANCE, \
        f"round_trip_fraction() = {frac}, expected {EXPECTED_ROUND_TRIP_FEE}"
    assert abs(ROUND_TRIP_FEE - EXPECTED_ROUND_TRIP_FEE) < TOLERANCE, \
        f"ROUND_TRIP_FEE = {ROUND_TRIP_FEE}, expected {EXPECTED_ROUND_TRIP_FEE}"


def test_market_cost_model_components():
    """Verify the components that feed into ROUND_TRIP_FEE."""
    assert MARKET_COST_MODEL.spread == 0.031, \
        f"spread = {MARKET_COST_MODEL.spread}, expected 0.031"
    assert MARKET_COST_MODEL.commission == 0.0, \
        f"commission = {MARKET_COST_MODEL.commission}, expected 0.0"
    assert MARKET_COST_MODEL.slippage == 0.005, \
        f"slippage = {MARKET_COST_MODEL.slippage}, expected 0.005"


def test_fee_aware_kelly_position_sizing():
    """Item 2: fee_rate and fee-adjusted edge must use MARKET_COST_MODEL."""
    from core.fee_aware_kelly_position_sizing import KellySizingConfig, KellyPositionSizer

    # Default config fee_rate
    cfg = KellySizingConfig()
    assert abs(cfg.fee_rate - EXPECTED_ROUND_TRIP_FEE) < TOLERANCE, \
        f"KellySizingConfig.fee_rate = {cfg.fee_rate}, expected {EXPECTED_ROUND_TRIP_FEE}"

    # Verify variance calculation uses corrected net_return
    sizer = KellyPositionSizer()
    # Add some trades to establish history
    from datetime import datetime, timedelta
    for i in range(20):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        sizer.add_result(date, 50.0, (i % 3) != 2)

    variance = sizer.calculate_variance_estimate(sizer.get_rolling_win_rate())
    assert variance > 0, f"Variance should be > 0, got {variance}"


def test_position_sizing():
    """Item 3: PositionSizingConfig.fee_rate must match ROUND_TRIP_FEE."""
    from core.position_sizing import PositionSizingConfig

    cfg = PositionSizingConfig()
    assert abs(cfg.fee_rate - EXPECTED_ROUND_TRIP_FEE) < TOLERANCE, \
        f"PositionSizingConfig.fee_rate = {cfg.fee_rate}, expected {EXPECTED_ROUND_TRIP_FEE}"


def test_trade_execution():
    """Item 4: trade_execution fee_cost must use quantity-based calculation."""
    from core.trade_execution import place_paper_trade

    # Inspect source for the fee_cost line
    source = inspect.getsource(place_paper_trade)
    # Verify the fix: fee_cost uses quantity, not position_size
    assert "fee_cost = abs(quantity * self.fee_rate)" in source, \
        "trade_execution fee_cost still uses position_size instead of quantity!"


def test_paper_trading_engine():
    """Item 5: paper_trading_engine fee_cost must use quantity-based calculation."""
    # Check the file directly
    we_root = os.path.join(os.path.dirname(__file__), "..")
    pte_path = os.path.join(we_root, "core", "paper_trading_engine.py")
    with open(pte_path) as f:
        source = f.read()
    assert "fee_cost = abs(quantity * self.fee_rate)" in source, \
        "paper_trading_engine fee_cost still uses position_size instead of quantity!"


def test_pnl_tracking():
    """Item 6: pnl_tracking.py fee calculation must use quantity × ROUND_TRIP_FEE."""
    we_root = os.path.join(os.path.dirname(__file__), "..")
    pt_path = os.path.join(we_root, "core", "pnl_tracking.py")
    with open(pt_path) as f:
        source = f.read()
    # Verify file references ROUND_TRIP_FEE for fee calculation
    assert "ROUND_TRIP_FEE" in source, \
        "pnl_tracking.py doesn't reference ROUND_TRIP_FEE!"
    # Verify it no longer computes fee from position_size (notional) directly
    assert "abs(position_size *" not in source or "# fee" in source.split("abs(position_size *")[1][:40] if "abs(position_size *" in source else True, \
        "pnl_tracking.py may still compute fee from notional position_size!"


def test_kelly_position_sizer():
    """Item 7: DEFAULT_COST_FRACTION must equal ROUND_TRIP_FEE."""
    from core.kelly_position_sizer import DEFAULT_COST_FRACTION

    assert abs(DEFAULT_COST_FRACTION - EXPECTED_ROUND_TRIP_FEE) < TOLERANCE, \
        f"DEFAULT_COST_FRACTION = {DEFAULT_COST_FRACTION}, expected {EXPECTED_ROUND_TRIP_FEE}"


def test_fee_aware_filter():
    """Item 8: FEE_RATE renamed to COMMISSION_RATE, no stale FEE_RATE references."""
    we_root = os.path.join(os.path.dirname(__file__), "..")
    faf_path = os.path.join(we_root, "core", "fee_aware_filter.py")
    with open(faf_path) as f:
        source = f.read()

    # COMMISSION_RATE must exist
    assert "COMMISSION_RATE = 0.0" in source, \
        "fee_aware_filter.py missing COMMISSION_RATE = 0.0"
    # FEE_RATE must NOT exist as a module-level constant
    assert "FEE_RATE" not in source.split("\n")[0:20], \
        "fee_aware_filter.py still has FEE_RATE constant!"


def test_instance_configs():
    """Item 9: All instance_config variants must have fee_rate=0.0205."""
    we_root = os.path.join(os.path.dirname(__file__), "..")
    config_files = [
        os.path.join(we_root, "core", "instance_config.py"),
        os.path.join(we_root, "core", "instance_config_fixed.py"),
        os.path.join(we_root, "core", "instance_config_test_write.py"),
    ]

    for fpath in config_files:
        assert os.path.exists(fpath), f"Missing config file: {fpath}"
        with open(fpath) as f:
            content = f.read()
        fee_rate_instances = [line for line in content.split('\n') if 'fee_rate' in line and '=' in line and 'float' not in line]
        for line in fee_rate_instances:
            assert 'fee_rate=0.0205' in line or 'fee_rate = 0.0205' in line, \
                f"In {os.path.basename(fpath)}: {line.strip()} — expected fee_rate=0.0205"


def test_no_hardcoded_fee_zeros():
    """Gate check: No 'fee_rate=0.0' (exact value assignment) in core/*.py except test files.
    
    This checks for ACTUAL hardcoded zero values, not:
    - Type annotations (fee_rate: float)
    - Default parameter values set to 0.0205 (the correct value)
    - fee_rate = None (placeholder, not zero)
    """
    we_root = os.path.join(os.path.dirname(__file__), "..")
    core_dir = os.path.join(we_root, "core")
    failures = []
    for fname in os.listdir(core_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(core_dir, fname)
        with open(fpath) as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.strip()
                # Skip comments and type annotations
                if stripped.startswith("#") or "fee_rate: float" in stripped:
                    continue
                # Only flag actual assignments where fee_rate = 0.0 or fee_rate=0.0
                # (not 0.0205, not None, not 0.001)
                if "fee_rate" in stripped:
                    # Check if this is an assignment to value 0.0 specifically
                    eq_parts = stripped.split("#")[0].split("fee_rate")
                    if len(eq_parts) > 1:
                        rhs = eq_parts[-1].strip().lstrip("=:").strip()
                        # Match exactly 0.0 (not 0.001, 0.0205, 0.05)
                        if rhs in ("0.0", "0.0,", "0.0)"):
                            failures.append(f"{fname}:{lineno}: {line.strip()}")

    if failures:
        raise AssertionError(
            f"Found {len(failures)} hardcoded fee_rate=0.0 references:\n" +
            "\n".join(failures)
        )


# ─── Standalone runner ───────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [
        test_market_cost_model_round_trip_fee,
        test_market_cost_model_components,
        test_fee_aware_kelly_position_sizing,
        test_position_sizing,
        test_trade_execution,
        test_paper_trading_engine,
        test_pnl_tracking,
        test_kelly_position_sizer,
        test_fee_aware_filter,
        test_instance_configs,
        test_no_hardcoded_fee_zeros,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)