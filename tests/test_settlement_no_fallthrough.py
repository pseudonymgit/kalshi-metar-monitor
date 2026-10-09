#!/usr/bin/env python3
"""A2 — settlement must fail loudly, never settle without a validated strike.

Regression for the phantom-resurrection bug: core/paper_trading_engine.py
previously fell through, on delegation failure, to a legacy body that settled
every trade as a win via `1.0 if settlement_value > 50.0 else 0.0`.

Run: python3 -m pytest tests/test_settlement_no_fallthrough.py -v
     python3 tests/test_settlement_no_fallthrough.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.paper_trading_engine as pte
from core.paper_trading_engine import SettlementProcessingError


def _fake_self():
    """Minimal stand-in for PaperTrader; the method only forwards it to pnl_tracking."""
    return types.SimpleNamespace(paper_db=":memory:", metar_db=":memory:")


def test_raises_when_pnl_tracking_unavailable(monkeypatch=None):
    """HAS_PNL_TRACKING=False => raise; never fall through to the legacy body."""
    orig_flag, orig_mod = pte.HAS_PNL_TRACKING, pte._pnl_tracking
    try:
        pte.HAS_PNL_TRACKING = False
        pte._pnl_tracking = None
        raised = False
        try:
            pte.PaperTrader.process_settlements_for_date(_fake_self(), "2026-10-09")
        except SettlementProcessingError:
            raised = True
        assert raised, "expected SettlementProcessingError when pnl_tracking unavailable"
    finally:
        pte.HAS_PNL_TRACKING, pte._pnl_tracking = orig_flag, orig_mod


def test_raises_when_delegation_raises():
    """A raising delegated settlement must propagate as SettlementProcessingError."""
    orig_flag, orig_mod = pte.HAS_PNL_TRACKING, pte._pnl_tracking

    def _boom(self, date):
        raise RuntimeError("simulated delegation failure")

    fake_mod = types.SimpleNamespace(process_settlements_for_date=_boom)
    try:
        pte.HAS_PNL_TRACKING = True
        pte._pnl_tracking = fake_mod
        raised = False
        try:
            pte.PaperTrader.process_settlements_for_date(_fake_self(), "2026-10-09")
        except SettlementProcessingError as e:
            raised = True
            assert "simulated delegation failure" in str(e)
        assert raised, "expected SettlementProcessingError when delegation raises"
    finally:
        pte.HAS_PNL_TRACKING, pte._pnl_tracking = orig_flag, orig_mod


def test_phantom_line_is_gone_from_source():
    """The phantom `> 50.0` settlement body must not exist in the engine source."""
    p = os.path.join(os.path.dirname(__file__), "..", "core", "paper_trading_engine.py")
    with open(p) as f:
        src = f.read()
    assert "settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0" not in src, \
        "phantom legacy settlement body still present"
    # No silent except-return fall-through around delegation
    assert "except SettlementProcessingError:" in src, \
        "engine should re-raise SettlementProcessingError, not swallow it"


if __name__ == "__main__":
    test_raises_when_pnl_tracking_unavailable()
    test_raises_when_delegation_raises()
    test_phantom_line_is_gone_from_source()
    print("test_settlement_no_fallthrough: PASS")
