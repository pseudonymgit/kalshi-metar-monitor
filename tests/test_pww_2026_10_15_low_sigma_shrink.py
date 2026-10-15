#!/usr/bin/env python3
"""PWW-2026-10-15 ITEM 2 (MAJOR) — the LOW lane must apply the intraday sigma shrink.

`scripts/low_entry_bridge.py` never called `update_from_intraday` (count = 0), so
the LOW lane priced every strike off the FULL-day sigma (3.0 F) all day while the
HIGH lane shrinks sigma as observations accrue (`sbox_intraday_ev_bridge.py`,
`state = update_from_intraday(state, hsf, frac)`).

`update_from_intraday_low` (core/intraday/distribution_adapter.py) is the LOW
mirror: it shrinks the daily-min sigma by the fraction of the day remaining, from
`base_sigma` (never compounded), pinning mu to the observed running low. The LOW
bridge now applies it to the state before building the p_fn.

Run: python3 -m pytest tests/test_pww_2026_10_15_low_sigma_shrink.py -v
"""
import importlib.util
import inspect
import math
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.intraday.distribution_adapter import DistributionState  # noqa: E402
from core.intraday.distribution_adapter import update_from_intraday_low  # noqa: E402


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the shared shrink helper ─────────────────────────────────────────────────
@pytest.mark.unit
def test_update_from_intraday_low_shrinks_sigma_and_pins_mu():
    st = DistributionState("KATL", "2026-10-15", mu=54.0, sigma=3.0,
                           base_sigma=3.0, low_so_far=52.0)
    out = update_from_intraday_low(st, 52.0, 0.5625)  # 13:30 local
    # sigma shrinks from the base by the remaining-day fraction.
    assert out.sigma == pytest.approx(3.0 * math.sqrt(1.0 - 0.5625))
    assert out.sigma < st.sigma
    # mu pinned to the observed running low (the min mirror of HIGH's max).
    assert out.mu == pytest.approx(52.0)
    assert out.low_so_far == pytest.approx(52.0)
    assert out.base_sigma == pytest.approx(3.0)


@pytest.mark.unit
def test_update_from_intraday_low_is_idempotent_across_calls():
    """Applying it repeatedly must not compound the shrink (E2E/F15 invariant)."""
    st = DistributionState("KATL", "2026-10-15", mu=54.0, sigma=3.0,
                           base_sigma=3.0, low_so_far=52.0)
    a = update_from_intraday_low(st, 52.0, 0.5625)
    b = update_from_intraday_low(a, 52.0, 0.5625)
    assert a.sigma == pytest.approx(b.sigma)


@pytest.mark.unit
def test_update_from_intraday_low_without_bound_still_shrinks():
    st = DistributionState("KATL", "2026-10-15", mu=54.0, sigma=3.0,
                           base_sigma=3.0, low_so_far=None)
    out = update_from_intraday_low(st, None, 0.5625)
    assert out.sigma == pytest.approx(3.0 * math.sqrt(1.0 - 0.5625))
    assert out.mu == pytest.approx(54.0)  # no bound -> forecast mu kept


@pytest.mark.unit
def test_update_from_intraday_low_shrinks_after_the_running_low_is_observed():
    """The shrink is real once the running low is observed: sigma at the 13:30 pin
    is strictly below the full-day 3.0 F (the definition of ITEM2)."""
    st = DistributionState("KATL", "2026-10-15", mu=54.0, sigma=3.0,
                           base_sigma=3.0, low_so_far=None)
    shrunk = update_from_intraday_low(st, 52.0, 13.5 / 24.0)
    assert shrunk.sigma < 3.0


# ── the LOW bridge wiring ────────────────────────────────────────────────────
@pytest.mark.unit
def test_low_run_applies_the_shrink_before_building_the_p_fn(monkeypatch):
    """Endpoint: the state handed to sole_tmin_p_fn must carry the shrunk sigma,
    not the full-day 3.0 F."""
    lb = _load_low()
    seen = {}

    def _fake_state(st, d, **k):
        return DistributionState(st, d, mu=54.0, sigma=3.0, base_sigma=3.0,
                                 low_so_far=None)

    def _capture_p_fn(state, station=None, low_so_far=None):
        seen["sigma"] = state.sigma
        seen["mu"] = state.mu
        return (lambda x: 0.0), (lambda x: {"calibrated_p": 0.0})

    monkeypatch.setattr(lb, "om_gfs_tmin_state", _fake_state)
    monkeypatch.setattr(lb, "sole_tmin_p_fn", _capture_p_fn)
    monkeypatch.setattr(lb, "_low_panel_dates", lambda: {"KATL": ["2026-10-15"]})
    monkeypatch.setattr(lb, "low_ladder_from_csv", lambda st, d: [
        {"strike": 52.0, "yes_price": 0.40, "no_price": 0.60,
         "ticker": "KXLOWTATL-26OCT15-T52", "volume_24h": None,
         "snapshot_time": None, "dist_pin_minutes": 40, "running_low_at_pin": 52.0}])
    monkeypatch.setattr(lb, "running_low", lambda *a, **k: 52.0)
    monkeypatch.setattr(lb, "get_station_params",
                        lambda st: {"position_cap_usd": 25.0,
                                    "confidence_floor": 0.0})
    lb.run(dry_run=True, stations=["KATL"])

    assert "sigma" in seen, "the run must have reached the p_fn build"
    assert seen["sigma"] < 3.0, "LOW must shrink sigma before the p_fn"
    assert seen["sigma"] == pytest.approx(3.0 * math.sqrt(1.0 - 13.5 / 24.0))
    assert seen["mu"] == pytest.approx(52.0)


@pytest.mark.unit
def test_low_run_source_contract_applies_the_shrink():
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert "update_from_intraday_low(" in src
    # it must precede the p_fn build so the shrunk sigma is what gets priced
    assert src.index("update_from_intraday_low(") < src.index("sole_tmin_p_fn(")


@pytest.mark.unit
def test_pin_day_fraction_is_dst_safe_local_wall_time():
    lb = _load_low()
    # 13:30 local is 0.5625 of the local day regardless of DST (wall-time pin).
    assert lb._pin_day_fraction("KATL", "2026-10-15") == pytest.approx(13.5 / 24.0)
    assert lb._pin_day_fraction("KATL", "2026-01-15") == pytest.approx(13.5 / 24.0)
    # a degenerate day falls back to a benign midpoint rather than raising
    assert lb._pin_day_fraction("KATL", "not-a-day") == pytest.approx(0.5)
