#!/usr/bin/env python3
"""F3 (2026-10-14) — the LOW lane must price execution cost and fail closed.

Before the fix, `low_entry_bridge.run` called `select_strike(..., fail_closed=False)`
with NO cost model, so LOW EV was priced off the bare mid. HIGH passes
`cost_model=_station_spread_model(ladder)` + `fail_closed=True`. Strikes that are
only +EV on the mid survive once the ~0.0205 execution cost is real, producing a
systematic over-entry bias vs HIGH.

Run: python3 -m pytest tests/test_f3_low_cost_model_fail_closed.py -v
"""
import importlib.util
import inspect
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.market_cost_model import EXECUTION_COST_RATE  # noqa: E402
from core.strike_selection_ev import select_strike  # noqa: E402


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTminState:
    def __init__(self, mu=60.0, sigma=3.0, low_so_far=None):
        self.mu, self.sigma, self.low_so_far = mu, sigma, low_so_far

    def p_ge(self, x):
        import math
        if self.low_so_far is not None and x >= self.low_so_far:
            return 0.0
        if x <= self.mu:
            return 1.0
        return 0.5 * (1.0 - math.erf(((x - self.mu) / self.sigma) / math.sqrt(2.0)))


@pytest.mark.unit
def test_run_builds_and_passes_the_cost_model_and_fails_closed():
    """Source contract: LOW resolves a station spread model and threads it in."""
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert "_station_spread_model(" in src, "run must build the station spread model"
    assert "cost_model=cost_model" in src, "select_strike must receive the cost model"
    assert "fail_closed=True" in src, "LOW must fail closed (F3)"
    # The executable call must not re-introduce the fail-open path.
    for line in src.splitlines():
        if "select_strike(" in line or "fail_closed=" in line:
            assert "fail_closed=False" not in line, line


@pytest.mark.unit
def test_low_uses_the_same_cost_model_builder_as_high(monkeypatch):
    """LOW resolves its spread through the shared HIGH builder (same semantics)."""
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")
    lb = _load_low()
    assert callable(lb._station_spread_model)
    ladder = lb.low_ladder_from_csv("KATL", "2026-09-07")
    m = lb._station_spread_model(ladder)
    assert hasattr(m, "get_dynamic_spread")
    assert abs((m.get_dynamic_spread(ladder[0]["ticker"]) / 2.0) + m.slippage
               - EXECUTION_COST_RATE) < 1e-12


@pytest.mark.unit
def test_low_station_spread_model_collapses_to_execution_rate(monkeypatch):
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")  # deterministic: skip the probe
    lb = _load_low()
    ladder = lb.low_ladder_from_csv("KATL", "2026-09-07")
    assert ladder, "fixture needs a real LOW ladder"
    m = lb._station_spread_model(ladder)
    spread = m.get_dynamic_spread(ladder[0]["ticker"])
    assert abs((spread / 2.0) + m.slippage - EXECUTION_COST_RATE) < 1e-12


@pytest.mark.unit
def test_low_cost_model_flips_a_mid_positive_ev_trade(monkeypatch):
    """End-to-end through the LOW p_fn: a mid +EV strike goes negative on cost."""
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")
    lb = _load_low()
    st = _FakeTminState(mu=60.0, sigma=3.0)
    p_fn, _ = lb.sole_tmin_p_fn(st, station="KATL")

    class _WideModel:
        slippage = 0.005

        def get_dynamic_spread(self, ticker):
            return 0.10  # 5c half-spread per leg

    # A LOW YES leg (min <= 56) underpriced vs the calibrated p_fn by a few cents:
    # +EV on the bare mid, negative once the half-spread is crossed twice.
    ladder = [{"strike": 56.0, "yes_price": 0.10, "no_price": 0.90,
               "ticker": "KXLOWTATL-26SEP07-T56"}]
    on_mid = select_strike(st.mu, st.sigma, ladder, min_edge=-1.0, p_fn=p_fn)
    assert on_mid["ev"] > 0.0, "fixture must be +EV on the bare mid"
    with_cost = select_strike(st.mu, st.sigma, ladder, min_edge=-1.0, p_fn=p_fn,
                              cost_model=_WideModel(), fail_closed=True)
    # Either the once-best side is now rejected (None) or the winning EV is < 0.
    assert with_cost is None or with_cost["ev"] < 0.0, \
        "crossing the book must remove the mid-only edge"


@pytest.mark.unit
def test_fail_closed_raises_when_no_spread_and_no_fallback(monkeypatch):
    """fail_closed=True + cost_model with no resolvable spread + no fallback."""
    import core.strike_selection_ev as ev
    monkeypatch.setattr(ev, "_FALLBACK_EXEC_COST", None)

    class _NoSpread:
        slippage = 0.0

        def get_dynamic_spread(self, ticker):
            return None

    ladder = [{"strike": 56.0, "yes_price": 0.10, "no_price": 0.90,
               "ticker": "KXLOWTATL-26SEP07-T56"}]
    with pytest.raises(ValueError):
        select_strike(60.0, 3.0, ladder, min_edge=-1.0, cost_model=_NoSpread(),
                      fail_closed=True)
