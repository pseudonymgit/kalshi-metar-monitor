#!/usr/bin/env python3
"""Workstream AF (2026-10-14) — om_gfs is the SOLE forecast on the SBOX bridge.

Every analysis this week found om_gfs(bc,ps) Tmax is the best single predictor
(Brier 0.107) and every fusion variant loses to it. Behind USE_OM_GFS_NWP the
bridge now forecasts on the single om_gfs value; when the flag is OFF it falls
back cleanly to the legacy multi-forecaster fusion.

Run: python3 -m pytest tests/test_sole_forecast_om_gfs.py -v
"""
import importlib.util
import inspect
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeState:
    """Minimal DistributionState stand-in: monotone P(high >= X)."""

    def __init__(self, mu=80.0, sigma=3.0):
        self.mu, self.sigma = mu, sigma

    def p_ge(self, x):
        import math
        if x <= self.mu:
            return 1.0
        z = (x - self.mu) / self.sigma
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))


@pytest.mark.unit
def test_sole_flag_follows_the_nwp_substitution_flag(monkeypatch):
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    assert b.sole_forecast_enabled() is True
    monkeypatch.setenv("USE_OM_GFS_NWP", "0")
    assert b.sole_forecast_enabled() is False


@pytest.mark.unit
def test_sole_p_fn_returns_om_gfs_calibrated_p():
    """The sole builder prices off state.p_ge and reports om_gfs provenance."""
    b = _load_bridge()
    st = _FakeState(mu=80.0, sigma=3.0)
    p_fn, meta_fn = b.build_om_gfs_sole_p_fn(st, station="KNYC")
    x = 80.0
    raw = st.p_ge(x)
    # p_fn returns the honest calibrated value; meta carries the raw + ceiling.
    assert p_fn(x) == meta_fn(x)["calibrated_p"]
    m = meta_fn(x)
    assert m["p_gefs"] == pytest.approx(round(raw, 4))
    assert m["p_fused"] == pytest.approx(round(raw, 4))
    assert m["p_om_gfs"] == pytest.approx(round(raw, 4))
    assert m["sole_forecast"] is True
    assert m["fusion_rule"] == b.SOLE_FORECAST_RULE_ID
    # Dropped members are explicitly absent, not fabricated.
    assert m["p_analog"] is None
    assert m["p_trajectory"] is None
    assert m["p_market"] is None


@pytest.mark.unit
def test_sole_p_fn_is_monotone_and_bounded_by_the_ceiling():
    b = _load_bridge()
    st = _FakeState(mu=80.0, sigma=3.0)
    p_fn, meta_fn = b.build_om_gfs_sole_p_fn(st, station="KNYC")
    vals = [p_fn(x) for x in (70.0, 78.0, 80.0, 82.0, 86.0)]
    assert all(0.0 <= v <= 1.0 for v in vals)
    # P(high >= X) must be non-increasing in X.
    assert vals[0] >= vals[1] >= vals[2] >= vals[3] >= vals[4]
    ceiling = meta_fn(80.0)["ceiling"]
    assert all(v <= ceiling + 1e-9 for v in vals)


@pytest.mark.unit
def test_run_uses_sole_forecast_and_skips_fusion_members():
    """Source contract: the sole branch is present and short-circuits members."""
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert "sole = sole_forecast_enabled()" in src
    assert "build_om_gfs_sole_p_fn(" in src
    assert "if not sole:" in src, "fusion members must be skipped on the sole path"
    # The legacy fusion is still reachable (rollback path).
    assert "build_fused_p_fn(" in src


@pytest.mark.unit
def test_om_gfs_fusion_flag_is_retired_noop(monkeypatch):
    """USE_OM_GFS_FUSION is moot: the fusion lever can no longer turn on."""
    from core.intraday import om_gfs_nwp
    monkeypatch.setenv("USE_OM_GFS_FUSION", "1")
    assert om_gfs_nwp.om_gfs_fusion_enabled() is False
