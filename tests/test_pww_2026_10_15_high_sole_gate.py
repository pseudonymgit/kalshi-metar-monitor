#!/usr/bin/env python3
"""PWW-2026-10-15 ITEM 1 (BLOCKER) — the HIGH sole path must fail closed by DATA.

`scripts/sbox_intraday_ev_bridge.py` selected the sole branch by FLAG and then ran
`build_om_gfs_sole_p_fn` on whatever `select_nwp_state` returned. When om_gfs was
unavailable / its fetch failed, that state was the raw local GFS-seeded WS-P
(`nwp_src == "gfs_local"`), yet the sole branch hard-coded om_gfs provenance and
booked it — the same silent-fallback failure mode the LOW lane already fixed (F5).

The gate now keys on the DATA (`nwp_src == "om_gfs"`) and FAILS CLOSED with
`sole_no_om_gfs` (the same string LOW uses): the station-day is skipped, never
booked. This is the HIGH mirror of the LOW F5 fix, verified with tests.

Run: python3 -m pytest tests/test_pww_2026_10_15_high_sole_gate.py -v
"""
import importlib.util
import inspect
import math
import os
import sys
from datetime import datetime, timezone

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
    """Minimal monotone P(high >= X) stand-in with the DistributionState fields
    the run() path touches (station/date/base_sigma after the intraday update)."""

    def __init__(self, mu=80.0, sigma=3.0):
        self.station, self.date = "KATL", "2026-10-15"
        self.mu, self.sigma = mu, sigma
        self.high_so_far = None
        self.low_so_far = None
        self.base_sigma = sigma

    def p_ge(self, x):
        if x <= self.mu:
            return 1.0
        z = (x - self.mu) / self.sigma
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))


@pytest.fixture
def high_harness(monkeypatch):
    """A controlled HIGH pass: real run(), real select_nwp_state, stubbed I/O.

    USE_OM_GFS_NWP=1 so the sole flag is ON; om_gfs availability is controlled
    per-test. The sole p_fn builder is stubbed to record whether the gate let the
    pass reach it (it never books: the stub returns calibrated_p=0.0, which the
    downstream fail-closed gate rejects).
    """
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    calls = {"sole_p_fn": 0}
    monkeypatch.setattr(b, "get_station_params",
                        lambda st: {"entry_window_start_hb": 18.0,
                                    "entry_window_end_hb": 1.0,
                                    "confidence_floor": 0.0,
                                    "position_cap_usd": 25.0})
    monkeypatch.setattr(b, "high_so_far", lambda st, a, c, n: 70.0)
    monkeypatch.setattr(b, "gefs_ens", lambda st, d: (77.0, 2.5))
    monkeypatch.setattr(b, "from_ws_p",
                        lambda *a, **k: _FakeState(mu=77.0, sigma=3.0))
    monkeypatch.setattr(b, "strike_ladder",
                        lambda st, yy, before_iso=None: [
                            {"strike": 70.0, "yes_price": 0.55, "no_price": 0.45,
                             "ticker": "KXHIGHTATL-26OCT15-T70",
                             "snapshot_time": "2026-10-15T17:30:00Z"}])

    def _sole(*a, **k):
        calls["sole_p_fn"] += 1
        # Benign stub: records the call so tests can prove the gate did/did not
        # reach the sole p_fn builder. Never books (fail-closed on calibrated_p).
        return (lambda x: 0.0), (lambda x: {"calibrated_p": 0.0, "edge_mult": 1.0})

    monkeypatch.setattr(b, "build_om_gfs_sole_p_fn", _sole)
    # ITEM1 (2026-10-16): the sole path now enforces the 13:30 station-local
    # pin. Pin a deterministic decision instant AT/AFTER the pin (KATL
    # 2026-10-15 pin = 17:30Z) so these gate tests are not wall-clock flaky.
    b._now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    b._calls = calls
    return b


@pytest.mark.unit
def test_sole_path_skips_when_om_gfs_unavailable(high_harness, monkeypatch):
    """Flag ON but om_gfs returns None -> gfs_local state -> the sole branch must
    SKIP (no book) and record `sole_no_om_gfs` with the TRUE source."""
    b = high_harness
    monkeypatch.setattr(b, "om_gfs_state_for_day", lambda *a, **k: None)

    out = b.run(dry_run=True, ignore_window=True, stations=["KATL"], report=True,
                now_utc=b._now)

    assert out["executed"] == [], "a gfs_local sole pass must not produce a trade"
    assert any("sole_no_om_gfs" in s for s in out["skipped"]), out["skipped"]
    diag = [d for d in out["diagnostics"] if d.get("skip") == "sole_no_om_gfs"]
    assert diag and diag[0]["nwp_src"] == "gfs_local"
    assert diag[0]["forecast_src"] is None
    # Prove the gate fired BEFORE the sole p_fn was built on the wrong state.
    assert b._calls["sole_p_fn"] == 0


@pytest.mark.unit
def test_sole_path_skips_on_om_gfs_fetch_error(high_harness, monkeypatch):
    """A fetch error falls through to gfs_local just like an unavailable value."""
    b = high_harness

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(b, "om_gfs_state_for_day", _raise)
    out = b.run(dry_run=True, ignore_window=True, stations=["KATL"],
                now_utc=b._now)
    assert out["executed"] == []
    assert any("sole_no_om_gfs" in s for s in out["skipped"])
    assert b._calls["sole_p_fn"] == 0


@pytest.mark.unit
def test_sole_path_proceeds_when_om_gfs_present(high_harness, monkeypatch):
    """Positive control: when om_gfs IS returned the gate opens (no skip)."""
    b = high_harness
    monkeypatch.setattr(b, "om_gfs_state_for_day",
                        lambda *a, **k: _FakeState(mu=76.0, sigma=2.0))
    out = b.run(dry_run=True, ignore_window=True, stations=["KATL"], report=True,
                now_utc=b._now)
    assert not any("sole_no_om_gfs" in s for s in out["skipped"]), out["skipped"]
    # The gate opened, so the sole p_fn WAS built on the real om_gfs state.
    assert b._calls["sole_p_fn"] >= 1


@pytest.mark.unit
def test_sole_gate_consumes_nwp_src_not_just_logs():
    """Source contract: `nwp_src` gates entry (is consumed in a branch), it is not
    merely appended to diag. Guards against a re-introduced log-only nwp_src."""
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert 'nwp_src != "om_gfs"' in src, "the sole path must gate on nwp_src"
    assert "sole_no_om_gfs" in src
    # The gate is a guard that `continue`s, not a comment or a bare log line.
    gate_idx = src.index('nwp_src != "om_gfs"')
    assert "continue" in src[gate_idx:gate_idx + 500]
