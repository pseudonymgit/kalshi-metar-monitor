#!/usr/bin/env python3
"""ITEM 2 (2026-10-16) — `fail_closed=True` must ACTUALLY fail closed.

Cold-eyes BLOCK: `MarketCostModel.get_dynamic_spread()` returned `self.spread`
on API failure and the bridge's `_station_spread_model` did the same, so
`_entry_exec_cost` ALWAYS received a positive fallback and never raised — the
`fail_closed` flag was inert on the live path.

Fix:
  * `get_dynamic_spread(unresolved_on_failure=True)` returns an explicit `None`
    (UNRESOLVED) on API/depth failure, and rejects zero/negative/NaN depths.
  * `_station_spread_model` tags its return with `resolution`, `fallback_used`
    and `policy_selected` (True only for the EXPLICIT `WE_LIVE_SPREAD_EV=0`
    operator policy).
  * `_entry_exec_cost` raises on API exception / None / zero / malformed spread
    while `fail_closed=True`; a tagged ASSUMPTION is rejected unless it was a
    SELECTED-AND-RECORDED policy. `fail_closed=False` keeps the documented
    fallback.

Run: python3 -m pytest tests/test_item2_spread_fail_closed_2026_10_16.py -v
"""
import importlib.util
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.market_cost_model import MarketCostModel  # noqa: E402
from core.strike_selection_ev import _entry_exec_cost, select_strike  # noqa: E402

_LADDER = [{"strike": 56.0, "yes_price": 0.10, "no_price": 0.90,
            "ticker": "KXHIGHX-26OCT16-T56"}]


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ExcSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        raise RuntimeError("kalshi API down")


class _NoneSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return None


class _ZeroSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return 0.0


class _NegSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return -0.02


class _MalformedSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return float("nan")


class _StringSpread:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return "not-a-number"


# ── the REAL cost model: unresolved state, not a masked fallback ─────────────
@pytest.mark.unit
def test_model_returns_none_when_unresolved_and_asked(monkeypatch):
    m = MarketCostModel()
    monkeypatch.setattr(m, "_fetch_market_depth", lambda *a, **k: None)
    assert m.get_dynamic_spread("T", unresolved_on_failure=True) is None
    # Legacy fail-open behaviour is unchanged when not asked to be unresolved.
    assert m.get_dynamic_spread("T") == m.spread


@pytest.mark.unit
def test_model_rejects_zero_and_nan_depth(monkeypatch):
    from core.market_cost_model import MarketDepthSnapshot

    for bad in (0.0, float("nan"), -0.01):
        m = MarketCostModel()
        snap = MarketDepthSnapshot(yes_bid=0.5, yes_ask=0.5, spread=bad,
                                   bid_volume=1, ask_volume=1, total_volume=1)
        monkeypatch.setattr(m, "_fetch_market_depth", lambda *a, _s=snap, **k: _s)
        assert m.get_dynamic_spread("T", unresolved_on_failure=True) is None, bad


# ── _entry_exec_cost: fail-closed rejects every unresolved form ──────────────
@pytest.mark.unit
@pytest.mark.parametrize("model", [_ExcSpread(), _NoneSpread(), _ZeroSpread(),
                                   _NegSpread(), _MalformedSpread()],
                         ids=["exc", "none", "zero", "neg", "nan"])
def test_fail_closed_raises_on_unresolved_spread(model):
    with pytest.raises(ValueError):
        _entry_exec_cost(_LADDER[0], model, fail_closed=True)


@pytest.mark.unit
def test_fail_closed_raises_on_a_malformed_string_spread():
    """A non-numeric spread must not slip through as a 'measurement'."""
    with pytest.raises(ValueError):
        _entry_exec_cost(_LADDER[0], _StringSpread(), fail_closed=True)


@pytest.mark.unit
def test_fail_closed_raises_without_a_cost_model():
    with pytest.raises(ValueError):
        _entry_exec_cost(_LADDER[0], None, fail_closed=True)


@pytest.mark.unit
def test_fail_open_may_fall_back():
    """fail_closed=False keeps the documented measured/constant fallback."""
    got = _entry_exec_cost(_LADDER[0], _NoneSpread(), fail_closed=False)
    from core.market_cost_model import EXECUTION_COST_RATE
    assert abs(got - EXECUTION_COST_RATE) < 1e-12


@pytest.mark.unit
def test_fail_closed_rejects_a_tagged_untagged_assumption():
    """A model that REPORTS it fell back (fallback_used=True) is rejected by a
    fail-closed caller, even though it returned a positive number."""
    class _TaggedFallback:
        slippage = 0.005
        fallback_used = True
        policy_selected = False
        resolution = "unresolved"

        def get_dynamic_spread(self, ticker):
            return 0.031

    with pytest.raises(ValueError):
        _entry_exec_cost(_LADDER[0], _TaggedFallback(), fail_closed=True)


@pytest.mark.unit
def test_fail_closed_allows_a_selected_recorded_policy_fallback():
    """An EXPLICIT, selected-and-recorded fallback policy (WE_LIVE_SPREAD_EV=0)
    is allowed under fail_closed — the operator chose the assumption."""
    class _PolicyFallback:
        slippage = 0.005
        fallback_used = True
        policy_selected = True
        resolution = "policy_fallback_we_live_spread_ev_0"

        def get_dynamic_spread(self, ticker):
            return 0.031

    got = _entry_exec_cost(_LADDER[0], _PolicyFallback(), fail_closed=True)
    assert abs(got - (0.031 / 2.0 + 0.005)) < 1e-12


# ── the bridge's own cost model tags resolution ─────────────────────────────
@pytest.mark.unit
def test_bridge_model_marks_unresolved_probe(monkeypatch):
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "1")
    b = _load_bridge()

    class _FailingModel:
        slippage = 0.005
        spread = 0.031

        def get_dynamic_spread(self, ticker, **k):
            return None

    monkeypatch.setattr(b, "MARKET_COST_MODEL", _FailingModel())
    ladder = [{"strike": 80.0, "yes_price": 0.5, "no_price": 0.5,
               "ticker": "KXHIGHX-26OCT16-T80", "volume_24h": 100}]
    m = b._station_spread_model(ladder)
    assert m.resolution == "unresolved"
    assert m.fallback_used is True and m.policy_selected is False
    # ... and the fail-closed picker must then SKIP, not price off the fallback.
    with pytest.raises(ValueError):
        select_strike(80.0, 3.0, ladder, min_edge=-1.0, cost_model=m,
                      fail_closed=True)


@pytest.mark.unit
def test_bridge_model_marks_explicit_policy_fallback(monkeypatch):
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "0")
    b = _load_bridge()
    ladder = [{"ticker": "KXHIGHX-26OCT16-T80", "volume_24h": 100}]
    m = b._station_spread_model(ladder)
    assert m.resolution == "policy_fallback_we_live_spread_ev_0"
    assert m.fallback_used is True and m.policy_selected is True


@pytest.mark.unit
def test_bridge_model_marks_measured(monkeypatch):
    monkeypatch.setenv("WE_LIVE_SPREAD_EV", "1")
    b = _load_bridge()

    class _GoodModel:
        slippage = 0.005
        spread = 0.031

        def get_dynamic_spread(self, ticker, **k):
            return 0.042

    monkeypatch.setattr(b, "MARKET_COST_MODEL", _GoodModel())
    ladder = [{"ticker": "KXHIGHX-26OCT16-T80", "volume_24h": 100}]
    m = b._station_spread_model(ladder)
    assert m.resolution == "measured"
    assert m.fallback_used is False
    assert m.get_dynamic_spread("T") == 0.042


@pytest.mark.unit
def test_bridge_skips_station_loudly_on_unresolved_spread():
    """Source contract: the HIGH bridge catches the fail-closed ValueError and
    records a skip, never crashing the pass."""
    src = open(os.path.join(_REPO, "scripts",
                            "sbox_intraday_ev_bridge.py")).read()
    assert "no_resolvable_exec_spread_fail_closed" in src
    assert "except ValueError" in src
