#!/usr/bin/env python3
"""H3 (2026-10-16) — the SOLE om_gfs path must ENFORCE the decision pin.

Verified defect: all 10 day-1 live fills (ids 284–293) were booked at
14:15Z/15:15Z — the moment the F4 entry window opened — while the canonical
13:30 station-local decision pin (17:30Z for the Eastern stations) was ~3.5h in
the FUTURE. `build_om_gfs_sole_p_fn` has no pin argument; the pin was consumed
only by the legacy fusion path, so the sole path booked before the day's
trajectory had developed enough to decide.

Fix: on the sole path a station-day must NOT be entered until
`now >= pinned_decision_utc(station, day)`. A pin still in the future is SKIPPED
(never booked early); an unresolvable pin FAILS CLOSED (skip), never defaulting
the decision instant to `now`.

Run: python3 -m pytest tests/test_h3_pin_sole_2026_10_16.py -v
"""
import importlib.util
import os
import sqlite3
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
    """Minimal DistributionState stand-in for the run() path."""

    def __init__(self, mu=80.0, sigma=3.0):
        self.station, self.date = "KATL", "2026-10-15"
        self.mu, self.sigma = mu, sigma
        self.high_so_far = None
        self.low_so_far = None
        self.base_sigma = sigma

    def p_ge(self, x):
        import math
        if x <= self.mu:
            return 1.0
        z = (x - self.mu) / self.sigma
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))


class _CostModel:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return 0.02


class _Sig:
    class _T:
        name = "BUY"

    signal_type = _T()


class _Verdict:
    def __init__(self, verdict):
        self.verdict = verdict


def _sole_meta(x, cal=0.95):
    return {
        "calibrated_p": cal, "edge_mult": 1.0, "grade": "A",
        "p_gefs": 0.95, "p_analog": None, "p_trajectory": None,
        "p_fused": 0.95, "agreement": "sole", "traj_verdict": "unavailable",
        "traj_mult": 1.0, "p_market": None, "p_om_gfs": 0.95,
        "composite_conf": 0.9, "ceiling": 0.982,
        "weights_applied": {"om_gfs": 1.0}, "fusion_rule": "sole",
        "grade_entered": "A", "grade_cap_usd": 15.0, "n_analog": None,
        "n_traj": None,
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A controlled HIGH sole pass that CAN book, with a stubbed book writer."""
    b = _load_bridge()
    monkeypatch.setenv("USE_OM_GFS_NWP", "1")
    book = tmp_path / "book.db"
    # Seed the minimal `trades` schema `ensure_schema` expects (the real SBOX_DB
    # already exists; a tmp book must be created first). record_trade is stubbed
    # below, so only the additive-schema ALTERs need the table to exist.
    _seed = sqlite3.connect(book)
    _seed.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_uuid TEXT NOT NULL UNIQUE, trade_date_utc TEXT NOT NULL,
            station TEXT NOT NULL, market_type TEXT NOT NULL,
            signal_direction TEXT NOT NULL, forecast_prob REAL,
            market_price REAL NOT NULL)""")
    _seed.commit()
    _seed.close()
    monkeypatch.setattr(b, "SBOX_DB", book, raising=False)
    b._booked = []
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
                            {"strike": 70.0, "yes_price": 0.30, "no_price": 0.70,
                             "ticker": "KXHIGHTATL-26OCT15-T70", "volume_24h": 500,
                             "snapshot_time": "2026-10-15T17:30:00Z"}])
    monkeypatch.setattr(b, "build_om_gfs_sole_p_fn",
                        lambda *a, **k: (lambda x: 0.95,
                                         lambda x: _sole_meta(x)))
    monkeypatch.setattr(b, "_station_spread_model", lambda ladder: _CostModel())
    monkeypatch.setattr(b, "evaluate_with_overlay", lambda *a, **k: _Sig())
    monkeypatch.setattr(b, "_position_size_usd", lambda p, m, **k: 10.0)
    monkeypatch.setattr(b, "compute_trade_grade", lambda p, m: ("A", 0.1))
    monkeypatch.setattr(b, "grade_position_cap", lambda g: 15.0)
    monkeypatch.setattr(b, "exposure_skip_reason", lambda conn, st, d: None)
    monkeypatch.setattr(b, "admit_decision_candidate",
                        lambda **k: (_Verdict("ADMITTED"), True))
    monkeypatch.setattr(b, "record_custody", lambda conn, t, **k: "hash")
    monkeypatch.setattr(b, "_fetch_trade_dict", lambda conn, tuuid: None)

    def _record(*a, **k):
        b._booked.append((a, k))
        return {"status": "executed", "trade_uuid": "U1"}

    monkeypatch.setattr(b, "record_trade", _record)
    return b


@pytest.mark.unit
def test_pin_in_future_does_not_book(harness):
    """14:15Z (the observed day-1 fill instant) is BEFORE the 17:30Z pin -> skip."""
    b = harness
    now = datetime(2026, 10, 15, 14, 15, tzinfo=timezone.utc)
    out = b.run(dry_run=False, ignore_window=True, stations=["KATL"], report=True,
                now_utc=now)
    assert b._booked == [], "must NOT book before the decision pin"
    assert out["executed"] == []
    assert any("sole_before_pin" in s for s in out["skipped"]), out["skipped"]
    diag = [d for d in out["diagnostics"] if d.get("skip") == "sole_before_pin"]
    assert diag and diag[0]["pin_iso"] == "2026-10-15T17:30:00+00:00"


@pytest.mark.unit
def test_pin_reached_books_normally(harness):
    """At/after the pin (18:00Z) the same station-day books normally."""
    b = harness
    now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    out = b.run(dry_run=False, ignore_window=True, stations=["KATL"],
                now_utc=now)
    assert len(b._booked) == 1, "pin reached must book"
    assert out["n_executed"] == 1
    assert not any("sole_before_pin" in s for s in out["skipped"])


@pytest.mark.unit
def test_exactly_at_the_pin_books(harness):
    """The pin is inclusive: `now == pin` is a decision instant, not too early."""
    b = harness
    now = datetime(2026, 10, 15, 17, 30, tzinfo=timezone.utc)
    out = b.run(dry_run=False, ignore_window=True, stations=["KATL"],
                now_utc=now)
    assert out["n_executed"] == 1


@pytest.mark.unit
def test_unresolvable_pin_fails_closed(harness, monkeypatch):
    """An unavailable pin SKIPS (fail closed) — never defaults the instant to now."""
    b = harness
    monkeypatch.setattr(b, "pinned_decision_utc", lambda st, d: None)
    now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    out = b.run(dry_run=False, ignore_window=True, stations=["KATL"], report=True,
                now_utc=now)
    assert b._booked == [], "an unresolved pin must not book"
    assert any("sole_pin_unresolved" in s for s in out["skipped"]), out["skipped"]


@pytest.mark.unit
def test_pin_gate_is_consumed_in_a_branch():
    """Source contract: the pin verdict gates entry (a branch), not a bare log."""
    b = _load_bridge()
    import inspect
    src = inspect.getsource(b.run)
    assert "sole_pin_reached(" in src
    assert "sole_before_pin" in src and "sole_pin_unresolved" in src
    idx = src.index("sole_pin_reached(")
    assert "continue" in src[idx:idx + 700]
