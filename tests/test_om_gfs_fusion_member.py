#!/usr/bin/env python3
"""om_gfs fusion member is RETIRED — the SOLE-FORECAST contract
(Workstream AF, 2026-10-14; re-stated 2026-10-15).

HISTORICAL: the honest re-run (`27463df0d`) once wired om_gfs(bc) as a FIFTH,
additive fusion member behind `USE_OM_GFS_FUSION` (Brier 0.123 vs honest
rate_1h 0.362).

CURRENT: behind `USE_OM_GFS_NWP` om_gfs IS the forecast — the NWP component
uses the bias-corrected om_gfs value instead of the raw local GFS-seeded WS-P
distribution. There is no fusion for it to join, so `om_gfs_fusion_enabled()`
is a permanent `return False` no-op and `USE_OM_GFS_FUSION` can no longer
enable a vote. `select_om_gfs_fusion_member` therefore ALWAYS returns None.

These tests pin the CURRENT contract:

  * `om_gfs_fusion_enabled()` is False for EVERY value, including "1" — the
    flag is inert (no-op);
  * the flag has NO effect on member selection: set == unset -> None, and the
    member fetch is never reached;
  * the NWP slot is not double-counted: nwp_src == "om_gfs" -> None;
  * the legacy fusion MATH (`multi_forecaster_fusion`) is intact as the
    ROLLBACK path (reached only when the NWP flag is off) and stays covered;
  * the defensive error-recording branch is LATENT — reachable only if the
    retired lever were force-re-enabled; a fetch failure then still records
    `om_gfs_member_error` and returns None (never raises).

Run: python3 -m pytest tests/test_om_gfs_fusion_member.py -v
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    DEFAULT_WEIGHTS, OM_GFS_DEFAULT_WEIGHT, ensure_schema, fuse,
    load_weights, default_om_gfs_weight,
)
from core.intraday.distribution_adapter import DistributionState  # noqa: E402


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_omgfs():
    import core.intraday.om_gfs_nwp as m
    return m


# ── flag parsing ─────────────────────────────────────────────────────────────
def test_fusion_flag_default_off(monkeypatch):
    monkeypatch.delenv("USE_OM_GFS_FUSION", raising=False)
    assert _load_omgfs().om_gfs_fusion_enabled() is False


def test_fusion_flag_is_retired_noop_for_every_value(monkeypatch):
    """The retired lever is permanently False — even "1" can no longer turn it on."""
    m = _load_omgfs()
    for v in ("1", "0", "", "yes", "true"):
        monkeypatch.setenv("USE_OM_GFS_FUSION", v)
        assert m.om_gfs_fusion_enabled() is False, v


# ── additive fusion math ─────────────────────────────────────────────────────
def test_default_weights_include_om_gfs_and_family_mean():
    assert DEFAULT_WEIGHTS["om_gfs"] == OM_GFS_DEFAULT_WEIGHT == 0.15
    # the family rule: mean of the NWP-family weights, else the 0.15 default.
    assert default_om_gfs_weight({"gefs": 0.25, "analog": 0.25, "traj": 0.25}) == 0.25
    assert default_om_gfs_weight({}) == OM_GFS_DEFAULT_WEIGHT


def test_om_gfs_joins_core_blend_additively():
    # 4-member core (no om_gfs) vs 5-member core (with om_gfs vote).
    _pf4, meta4 = fuse(0.9, 0.8, 1.0, use_weights=True, p_market=0.4)
    _pf5, meta5 = fuse(0.9, 0.8, 1.0, use_weights=True, p_market=0.4,
                       p_om_gfs=0.85)
    assert meta4["p_om_gfs"] is None
    assert meta5["p_om_gfs"] == 0.85
    # weighted mean: (0.25*0.9+0.25*0.8+0.25*1.0+0.25*0.4+0.15*0.85)/1.15
    expect = (0.25 * 0.9 + 0.25 * 0.8 + 0.25 * 1.0 + 0.25 * 0.4
              + 0.15 * 0.85) / (0.25 * 4 + 0.15)
    assert abs(meta5["p_core"] - expect) < 1e-3
    # it ADDS a vote: the market member still contributes (not replaced).
    assert meta5["p_market"] == 0.4


def test_om_gfs_changes_composite_confidence():
    conf4 = fuse(0.9, 0.8, 1.0, use_weights=True, p_market=0.4)[1]["composite_conf"]
    conf5 = fuse(0.9, 0.8, 1.0, use_weights=True, p_market=0.4,
                 p_om_gfs=0.5)[1]["composite_conf"]
    # a coin-flip om_gfs vote drags the composite down (counted, not ignored).
    assert conf5 < conf4


def test_no_om_gfs_vote_renormalizes_out():
    # om_gfs absent -> the four-member blend is unchanged (byte-identical).
    _pf, meta = fuse(0.8, 0.8, 0.5, use_weights=True, p_market=0.2)
    assert abs(meta["p_core"] - 0.575) < 1e-3


def test_fuse_strike_forwards_om_gfs():
    from core.intraday.multi_forecaster_fusion import fuse_strike
    out = fuse_strike(lambda x: 0.9, lambda x: 0.8, lambda x: 1.0, 75.0,
                      use_weights=True, p_market=0.4, p_om_gfs=0.8)
    assert out["p_om_gfs"] == 0.8


# ── per-station weight store ─────────────────────────────────────────────────
def test_station_weights_has_om_gfs_column_and_default():
    conn = sqlite3.connect(":memory:")
    # ensure_schema is additive onto an existing live book; give it a minimal
    # `trades` table so its ALTERs have something to attach to.
    conn.execute("CREATE TABLE trades (station TEXT)")
    ensure_schema(conn)
    cols = {d[1] for d in conn.execute("PRAGMA table_info(station_weights)")}
    assert "weight_om_gfs" in cols
    conn.execute("INSERT INTO station_weights (station, updated_at_utc) "
                 "VALUES ('KNYC','2026-10-13T00:00:00Z')")
    w = load_weights(conn, "KNYC")
    assert w["om_gfs"] == OM_GFS_DEFAULT_WEIGHT
    conn.close()


# ── bridge seam: the retired fifth-member selection ──────────────────────────
def test_fusion_member_off_by_default(monkeypatch):
    b = _load_bridge()
    monkeypatch.delenv("USE_OM_GFS_FUSION", raising=False)

    def _boom(*a, **k):
        raise AssertionError("om_gfs member fetched with fusion flag OFF")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _boom)
    assert b.select_om_gfs_fusion_member("gfs_local", "KNYC", "2026-10-13", 64.0) is None


def test_fusion_flag_has_no_effect_on_member_selection(monkeypatch):
    """Flag set -> same behaviour as unset: no member, fetch never reached.

    `USE_OM_GFS_FUSION` is retired (a permanent no-op), so member selection is
    identical whether it is set or not — and it never re-votes when the NWP
    slot is already om_gfs.
    """
    b = _load_bridge()

    def _boom(*a, **k):
        raise AssertionError("retired flag must not trigger a member fetch")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _boom)
    monkeypatch.delenv("USE_OM_GFS_FUSION", raising=False)
    assert b.select_om_gfs_fusion_member("gfs_local", "KNYC", "2026-10-13", 64.0) is None
    monkeypatch.setenv("USE_OM_GFS_FUSION", "1")
    assert b.select_om_gfs_fusion_member("gfs_local", "KNYC", "2026-10-13", 64.0) is None


def test_fusion_member_no_dup_vote_when_nwp_is_om_gfs(monkeypatch):
    """No double-count: nwp_src == "om_gfs" -> None, even if the lever came back."""
    b = _load_bridge()
    # Force the retired lever ON to prove the suppression is the NWP-slot rule,
    # not merely the (always-False) flag guard.
    monkeypatch.setattr(b, "om_gfs_fusion_enabled", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("double-count: NWP slot already om_gfs")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _boom)
    assert b.select_om_gfs_fusion_member("om_gfs", "KNYC", "2026-10-13", 64.0) is None


def test_member_fetch_error_records_diag_when_lever_forced(monkeypatch):
    """Latent defensive branch: IF the retired lever were re-enabled, a fetch
    failure records `om_gfs_member_error` and returns None (never raises).

    Under current reality the flag guard returns None before any fetch, so the
    branch is unreachable on the default path; forcing the lever here keeps the
    defensive behaviour covered without pretending the lever is live.
    """
    b = _load_bridge()
    monkeypatch.setattr(b, "om_gfs_fusion_enabled", lambda: True)

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _raise)
    diag = []
    assert b.select_om_gfs_fusion_member("gfs_local", "KNYC", "2026-10-13",
                                         64.0, diag) is None
    assert any("om_gfs_member_error" in d for d in diag)


def test_build_fused_p_fn_uses_member_callable():
    b = _load_bridge()

    class _CDF:
        n_analogs = 5

        def p_high_ge(self, x):
            return 0.8

    class _Traj:
        n_analogs = 3

        def p_high_ge(self, x, hsf):
            return 1.0

    state = DistributionState("KNYC", "2026-10-13", 70.0, 3.0)
    w = dict(DEFAULT_WEIGHTS)
    _p_fn, meta_fn = b.build_fused_p_fn(
        state, _CDF(), _Traj(), 65.0, weights=w,
        p_om_gfs_member=lambda x: 0.9)
    meta = meta_fn(70.0)
    assert meta["p_om_gfs"] == 0.9
    # and with no member callable the fifth vote is absent
    _p_fn2, meta_fn2 = b.build_fused_p_fn(state, _CDF(), _Traj(), 65.0, weights=w)
    assert meta_fn2(70.0)["p_om_gfs"] is None
