#!/usr/bin/env python3
"""F1 wired (verification, 2026-10-13) — the bridge must actually CALL the cost model.

`tests/test_slippage_in_ev.py` pins the selector's cost arithmetic. This file
pins the WIRING the register flagged as missing: the SBOX bridge builds a
per-station spread model and passes it into `select_strike(...)`, so the
execution cost (half-spread + slippage) reaches the EV of a real trade instead
of being imported-and-never-called.

Run: python3 -m pytest tests/test_f1_slippage_wired_bridge.py -v
"""
import importlib.util
import inspect
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.market_cost_model import EXECUTION_COST_RATE  # noqa: E402
from core.strike_selection_ev import select_strike  # noqa: E402


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_builds_and_passes_the_cost_model():
    """Source contract: `run` resolves a station spread model and threads it in."""
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert "_station_spread_model(" in src, "run must build the station spread model"
    assert "cost_model=cost_model" in src, "select_strike must receive the cost model"
    assert "fail_closed=True" in src, "unresolvable spread must fail closed"


def test_station_spread_model_collapses_to_execution_rate(monkeypatch):
    """No live probe -> measured-mean spread -> the documented EXECUTION_COST_RATE."""
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")  # deterministic: skip the live probe
    b = _load_bridge()
    ladder = [{"ticker": "KXHIGHX-26OCT13-T80", "volume_24h": 100}]
    m = b._station_spread_model(ladder)
    assert hasattr(m, "get_dynamic_spread")
    spread = m.get_dynamic_spread("KXHIGHX-26OCT13-T80")
    # half-spread + slippage == EXECUTION_COST_RATE
    assert abs((spread / 2.0) + m.slippage - EXECUTION_COST_RATE) < 1e-12


def test_bridge_cost_model_flips_a_mid_positive_ev_trade(monkeypatch):
    """End-to-end with the bridge's own cost model: mid +EV -> cost-negative."""
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")
    b = _load_bridge()
    # A 3c-underpriced strike: +EV on the bare mid, but the half-spread crossed
    # twice (entry+exit) at ~2.05c/leg dwarfs it once the cost model is applied.
    # Use a wider modeled spread to make the flip unambiguous and deterministic.
    class _WideModel:
        slippage = 0.005

        def get_dynamic_spread(self, ticker):
            return 0.10  # 5c half-spread

    strikes = [{"strike": 80.0, "yes_price": 0.47, "no_price": 0.53,
                "ticker": "KXHIGHX-26OCT13-T80"}]
    on_mid = select_strike(80.0, 3.0, strikes, min_edge=-1.0)
    assert on_mid["ev"] > 0.0, "fixture must be +EV on the bare mid"
    with_cost = select_strike(80.0, 3.0, strikes, min_edge=-1.0, cost_model=_WideModel())
    assert with_cost["ev"] < 0.0, "crossing the book must flip the trade negative"
    assert with_cost["entry_cost"] > strikes[0]["yes_price"], "entry must exceed the mid"
