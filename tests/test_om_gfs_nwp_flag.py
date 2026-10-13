#!/usr/bin/env python3
"""om_gfs NWP substitution (USE_OM_GFS_NWP) — flag semantics + no-op guarantee.

The om_gfs strike-crossing validation (artifacts/om-gfs-strike-crossing/) found
Open-Meteo's bias-corrected GFS is a better NWP input than our raw local GFS
(level MAE 0.680 vs 2.095 F; crossing Brier 0.1079 vs 0.1597) but is COLLINEAR
with rate_1h (fcerr 0.99) — a LEVER-axis substitution, never a diversity gain.

These tests pin the wiring guarantee:

  * flag OFF  -> the bridge uses the raw local GFS-seeded WS-P distribution and
                 NEVER touches om_gfs (byte-identical no-op);
  * flag ON   -> the NWP component uses om_gfs(bias-corrected);
  * flag ON but om_gfs unavailable / fetch error -> raw local GFS fallback;
  * no rolling-bias lookahead (window ends the day BEFORE the decision day).

Run: python3 -m pytest tests/test_om_gfs_nwp_flag.py -v
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

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
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("USE_OM_GFS_NWP", raising=False)
    m = _load_omgfs()
    assert m.om_gfs_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    m = _load_omgfs()
    assert m.om_gfs_enabled() is True


def test_flag_other_values_are_off(monkeypatch):
    m = _load_omgfs()
    for v in ("0", "", "yes", "true"):
        monkeypatch.setenv("USE_OM_GFS_NWP", v)
        assert m.om_gfs_enabled() is False, v


# ── NWP selection seam ───────────────────────────────────────────────────────
_WS_P_SENTINEL = DistributionState("KNYC", "2026-10-13", 100.0, 9.0)
_OM_SENTINEL = DistributionState("KNYC", "2026-10-13", 55.0, 3.0)


def test_select_off_uses_raw_local_gfs_and_never_calls_om_gfs(monkeypatch):
    b = _load_bridge()
    monkeypatch.delenv("USE_OM_GFS_NWP", raising=False)
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: (77.0, 2.5))
    monkeypatch.setattr(b, "from_ws_p",
                        lambda dist, st, ens_mean, ens_std, month, date: _WS_P_SENTINEL)

    def _boom(*a, **k):  # om_gfs must not be reached when the flag is off
        raise AssertionError("om_gfs_state_for_day called with flag OFF")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _boom)

    state, src, ens = b.select_nwp_state("KNYC", "2026-10-13", 64.0, {}, 10)
    assert src == "gfs_local"
    assert state is _WS_P_SENTINEL
    assert ens == (77.0, 2.5)


def test_select_on_uses_om_gfs(monkeypatch):
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    monkeypatch.setattr(b, "om_gfs_state_for_day",
                        lambda st, d, live=True, high_so_far=None: _OM_SENTINEL)
    monkeypatch.setattr(b, "from_ws_p",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("raw local GFS used with flag ON")))
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: (77.0, 2.5))

    state, src, ens = b.select_nwp_state("KNYC", "2026-10-13", 64.0, {}, 10)
    assert src == "om_gfs"
    assert state is _OM_SENTINEL
    assert ens is None


def test_select_on_falls_back_when_om_gfs_missing(monkeypatch):
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    monkeypatch.setattr(b, "om_gfs_state_for_day",
                        lambda st, d, live=True, high_so_far=None: None)
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: (77.0, 2.5))
    monkeypatch.setattr(b, "from_ws_p",
                        lambda dist, st, ens_mean, ens_std, month, date: _WS_P_SENTINEL)

    state, src, ens = b.select_nwp_state("KNYC", "2026-10-13", 64.0, {}, 10)
    assert src == "gfs_local"
    assert state is _WS_P_SENTINEL


def test_select_on_fetch_error_falls_back_and_never_raises(monkeypatch):
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _raise)
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: (77.0, 2.5))
    monkeypatch.setattr(b, "from_ws_p",
                        lambda dist, st, ens_mean, ens_std, month, date: _WS_P_SENTINEL)
    diag = []
    state, src, ens = b.select_nwp_state("KNYC", "2026-10-13", 64.0, {}, 10, diag)
    assert src == "gfs_local"
    assert state is _WS_P_SENTINEL
    assert any("om_gfs_error" in d for d in diag)


def test_select_no_gefs_when_om_unavailable(monkeypatch):
    b = _load_bridge()
    monkeypatch.delenv("USE_OM_GFS_NWP", raising=False)
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: None)
    state, src, ens = b.select_nwp_state("KNYC", "2026-10-13", 64.0, {}, 10)
    assert state is None
    assert src == "no_gefs_step24"
    assert ens is None


# ── state builder + bias correction ─────────────────────────────────────────
def test_from_om_gfs_applies_bias_and_sigma(monkeypatch):
    m = _load_omgfs()
    st = m.from_om_gfs("KNYC", 78.0, "2026-10-13", bias_f=-1.5)
    assert st.mu == pytest.approx(78.0 - (-1.5))  # mu = raw - bias
    assert st.sigma == pytest.approx(m.SIGMA_K_DEFAULT * m.SIGMA_RATE_F)
    assert st.base_sigma == st.sigma
    # high_so_far boundary: P(high>=X)=1 for X <= hsf, no impossible mass below it.
    st2 = m.from_om_gfs("KNYC", 78.0, "2026-10-13", high_so_far=70.0)
    assert st2.p_ge(69.0) == 1.0
    assert 0.0 < st2.p_ge(80.0) < 1.0


def _tiny_settle_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE settlement_truth (station TEXT, market_type TEXT, "
              "local_trading_date TEXT, settlement_temp_f REAL, trustworthy INTEGER)")
    rows = [("KNYC", "HIGH", "2026-10-09", 71.0, 1),
            ("KNYC", "HIGH", "2026-10-10", 74.0, 1),
            ("KNYC", "HIGH", "2026-10-11", 80.0, 1),
            # the decision day's OWN settlement must never be used (lookahead)
            ("KNYC", "HIGH", "2026-10-12", 5.0, 1)]
    c.executemany("INSERT INTO settlement_truth VALUES (?,?,?,?,?)", rows)
    c.commit()
    c.close()


def test_rolling_bias_excludes_decision_day(monkeypatch, tmp_path):
    m = _load_omgfs()
    db = tmp_path / "settle.db"
    _tiny_settle_db(db)
    monkeypatch.setattr(m, "SETTLE_DB", db)
    monkeypatch.setattr(m, "_BIAS_MEMO", {})
    monkeypatch.setattr(m, "BIAS_FETCH_MISSING", False)  # hermetic: no network
    # om_gfs 2 F above truth on the three in-window days (bias would be inflated
    # to a large negative if the lookahead day 2026-10-12 slipped into the window).
    monkeypatch.setattr(m, "_CACHE_MEMO", {
        m._cache_key("KNYC", "2026-10-09", "hist"): 73.0,
        m._cache_key("KNYC", "2026-10-10", "hist"): 76.0,
        m._cache_key("KNYC", "2026-10-11", "hist"): 82.0,
        m._cache_key("KNYC", "2026-10-12", "hist"): -100.0,  # must be ignored
    })
    b = m.rolling_bias_f("KNYC", "2026-10-12", window_days=30, min_obs=3)
    assert b == pytest.approx(2.0)  # (2+2+2)/3 — not dragged by the lookahead row


def test_rolling_bias_none_below_min_obs(monkeypatch, tmp_path):
    m = _load_omgfs()
    db = tmp_path / "settle.db"
    _tiny_settle_db(db)
    monkeypatch.setattr(m, "SETTLE_DB", db)
    monkeypatch.setattr(m, "_BIAS_MEMO", {})
    monkeypatch.setattr(m, "BIAS_FETCH_MISSING", False)  # hermetic: no network
    monkeypatch.setattr(m, "_CACHE_MEMO", {
        m._cache_key("KNYC", "2026-10-09", "hist"): 73.0,
    })
    assert m.rolling_bias_f("KNYC", "2026-10-12", window_days=30, min_obs=3) is None


def test_rolling_bias_uses_hist_window_from_live_path(monkeypatch, tmp_path):
    """Regression (2026-10-13): the bridge fetches the CURRENT day live but the
    bias WINDOW is historical. The window must be read under the `hist` key even
    when live=True; reading it under `live` silently produced None (no correction
    at all — the live substitution was raw om_gfs)."""
    m = _load_omgfs()
    db = tmp_path / "settle.db"
    _tiny_settle_db(db)
    monkeypatch.setattr(m, "SETTLE_DB", db)
    monkeypatch.setattr(m, "_BIAS_MEMO", {})
    monkeypatch.setattr(m, "BIAS_FETCH_MISSING", False)
    monkeypatch.setattr(m, "_CACHE_MEMO", {
        m._cache_key("KNYC", "2026-10-09", "hist"): 73.0,
        m._cache_key("KNYC", "2026-10-10", "hist"): 76.0,
        m._cache_key("KNYC", "2026-10-11", "hist"): 82.0,
    })
    live = m.rolling_bias_f("KNYC", "2026-10-12", window_days=30, min_obs=3, live=True)
    m._BIAS_MEMO = {}
    hist = m.rolling_bias_f("KNYC", "2026-10-12", window_days=30, min_obs=3, live=False)
    assert live == pytest.approx(2.0)
    assert live == hist
