#!/usr/bin/env python3
"""F4 + F5 (2026-10-14) — LOW arming gate and the shared booking/exposure gates.

F4: `LOW_BRIDGE_ENABLED` was read once and never used — the lane had no arming
gate, so the "prepare-only, safe by default" claim was not enforced in code.
`run()` now refuses to book unless the flag (or an explicit `allow_book`) is set.

F5: LOW skipped the F9 exposure gate (`exposure_skip_reason`), the GR5 §2 booking
gate (`admit_decision_candidate`) and the §1 custody backstop that HIGH applies;
and the sole-forecast path could silently fall back to GFS-local while still
labelling the trade `om_gfs`. LOW now mirrors the HIGH gate stack and fails
closed (no GFS-local fallback) with a true provenance label.

Run: python3 -m pytest tests/test_f4_f5_low_gates.py -v
"""
import importlib.util
import inspect
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _State:
    # Mirrors the real DistributionState contract (core.intraday.distribution_adapter):
    # the run() path now applies update_from_intraday_low(), which reads
    # station/date/high_so_far/base_sigma as well as mu/sigma/low_so_far.
    def __init__(self, mu=54.0, sigma=3.0, low_so_far=None, station="KATL",
                 date="2026-09-07", high_so_far=None):
        self.station = station
        self.date = date
        self.mu, self.sigma, self.low_so_far = mu, sigma, low_so_far
        self.high_so_far = high_so_far
        self.base_sigma = sigma

    def p_ge(self, x):
        import math
        if self.low_so_far is not None and x >= self.low_so_far:
            return 0.0
        if x <= self.mu:
            return 1.0
        return 0.5 * (1.0 - math.erf(((x - self.mu) / self.sigma) / math.sqrt(2.0)))


class _Verdict:
    def __init__(self, verdict):
        self.verdict = verdict


def _ladder():
    return [
        {"strike": 54.0, "yes_price": 0.20, "no_price": 0.80,
         "ticker": "KXLOWTATL-26SEP07-T54", "volume_24h": None,
         "snapshot_time": None, "dist_pin_minutes": 40, "running_low_at_pin": 52.0},
        {"strike": 56.0, "yes_price": 0.45, "no_price": 0.55,
         "ticker": "KXLOWTATL-26SEP07-T56", "volume_24h": None,
         "snapshot_time": None, "dist_pin_minutes": 40, "running_low_at_pin": 52.0},
        {"strike": 60.0, "yes_price": 0.70, "no_price": 0.30,
         "ticker": "KXLOWTATL-26SEP07-T60", "volume_24h": None,
         "snapshot_time": None, "dist_pin_minutes": 40, "running_low_at_pin": 52.0},
    ]


class _CostModel:
    slippage = 0.005

    def get_dynamic_spread(self, ticker):
        return 0.02


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A controlled LOW pass: real gate stack, stubbed forecast/sizing/book."""
    lb = _load_low()
    calls = {"record": [], "admit": 0}
    monkeypatch.setattr(lb, "SBOX_DB", tmp_path / "book.db", raising=False)
    monkeypatch.setattr(lb, "_low_panel_dates", lambda: {"KATL": ["2026-09-07"]})
    monkeypatch.setattr(lb, "low_ladder_from_csv", lambda st, d: _ladder())
    monkeypatch.setattr(lb, "running_low", lambda *a, **k: None)
    monkeypatch.setattr(lb, "_station_spread_model", lambda ladder: _CostModel())
    monkeypatch.setattr(lb, "kelly_position_size", lambda p, m, **k: 10.0)
    monkeypatch.setattr(lb, "get_station_params",
                        lambda st: {"position_cap_usd": 25.0, "confidence_floor": 0.0})
    monkeypatch.setattr(lb, "compute_trade_grade", lambda p, m: ("A", 0.1))
    monkeypatch.setattr(lb, "grade_position_cap", lambda g: 15.0)
    monkeypatch.setattr(lb, "_fetch_trade_dict", lambda conn, tuuid: None)

    def _rec(*a, **k):
        calls["record"].append((a, k))
        return {"status": "executed", "trade_uuid": "U1"}

    monkeypatch.setattr(lb, "record_trade", _rec)
    # Default gate behaviour: exposure admits; booking gate admits.
    monkeypatch.setattr(lb, "exposure_skip_reason", lambda conn, st, d: None)

    def _admit(**k):
        calls["admit"] += 1
        return _Verdict("ADMITTED"), True

    monkeypatch.setattr(lb, "admit_decision_candidate", _admit)
    monkeypatch.setattr(lb, "record_custody", lambda conn, t, **k: "hash")
    lb._state = _State(low_so_far=52.0)
    monkeypatch.setattr(lb, "om_gfs_tmin_state",
                        lambda st, d, **k: lb._state)
    lb._calls = calls
    return lb


# ── F4: arming gate ──────────────────────────────────────────────────────────
@pytest.mark.unit
def test_flag_off_refuses_to_book(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", False)
    with pytest.raises(RuntimeError):
        lb.run(dry_run=False)
    assert lb._calls["record"] == [], "flag OFF must not book"


@pytest.mark.unit
def test_flag_off_dry_run_still_evaluates_but_never_books(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", False)
    out = lb.run(dry_run=True)
    assert lb._calls["record"] == [], "dry run must never book"
    assert all(r.get("status") == "would_execute" for r in out["executed"])


@pytest.mark.unit
def test_flag_on_books(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    out = lb.run(dry_run=False)
    assert out["n_executed"] >= 1
    assert len(lb._calls["record"]) >= 1


@pytest.mark.unit
def test_allow_book_override_cannot_arm_the_lane(harness, monkeypatch):
    """ITEM 4 (2026-10-16): the env flag is the authority. `allow_book=True` with
    LOW_BRIDGE_ENABLED unset must STILL refuse to book — the old override let a
    stray caller arm the lane and made the module's arming claim false."""
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", False)
    with pytest.raises(RuntimeError):
        lb.run(dry_run=False, allow_book=True)
    assert lb._calls["record"] == [], "allow_book must not bypass the env gate"


@pytest.mark.unit
def test_flag_on_books_with_or_without_the_override(harness, monkeypatch):
    """The env flag alone authorises booking; the override is inert."""
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    assert lb.run(dry_run=False)["n_executed"] >= 1
    assert lb.run(dry_run=False, allow_book=True)["n_executed"] >= 1


# ── F5: exposure + booking gates ─────────────────────────────────────────────
@pytest.mark.unit
def test_exposure_gate_blocks_booking(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    monkeypatch.setattr(lb, "exposure_skip_reason",
                        lambda conn, st, d: "exposure_station_cap_3")
    out = lb.run(dry_run=False)
    assert lb._calls["record"] == [], "exposure cap must block booking"
    assert any("exposure_station_cap_3" in s for s in out["skipped"])


@pytest.mark.unit
def test_booking_gate_rejection_blocks_booking(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)

    def _reject(**k):
        return _Verdict("REJECTED"), False

    monkeypatch.setattr(lb, "admit_decision_candidate", _reject)
    out = lb.run(dry_run=False)
    assert lb._calls["record"] == [], "a rejected candidate must not be booked"
    assert any("booking_gate_REJECTED" in s for s in out["skipped"])


@pytest.mark.unit
def test_booking_gate_admission_books(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    out = lb.run(dry_run=False)
    assert lb._calls["admit"] >= 1, "the booking gate must be called"
    assert out["n_executed"] >= 1


@pytest.mark.unit
def test_run_calls_both_gates_on_the_booking_path():
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert "exposure_skip_reason(" in src
    assert "admit_decision_candidate(" in src
    assert "record_custody(" in src


# ── F5: loud provenance / no silent fallback ──────────────────────────────────
@pytest.mark.unit
def test_missing_om_gfs_fails_closed_loudly(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    monkeypatch.setattr(lb, "om_gfs_tmin_state", lambda st, d, **k: None)
    out = lb.run(dry_run=False)
    assert lb._calls["record"] == [], "no om_gfs Tmin -> no booking (fail closed)"
    assert any("sole_no_om_gfs" in s for s in out["skipped"])
    assert any(d.get("skip") == "sole_no_om_gfs" and d.get("forecast_src") is None
               for d in out["diagnostics"])


@pytest.mark.unit
def test_forecast_provenance_label_is_om_gfs_tmin(harness, monkeypatch):
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    out = lb.run(dry_run=True)  # diagnostics carry the provenance on a dry run
    assert out["diagnostics"], "expected a priced candidate"
    assert all(d["fusion_rule"] == lb.SOLE_FORECAST_RULE_ID
               for d in out["diagnostics"])


@pytest.mark.unit
def test_module_has_no_gfs_local_fallback_source():
    """The LOW lane must not carry a GFS-local provenance path."""
    lb = _load_low()
    full = open(os.path.join(_REPO, "scripts", "low_entry_bridge.py")).read()
    assert "gfs_local" not in full, "LOW must not fall back to GFS-local"
    assert lb.FORECAST_SRC == "om_gfs_tmin"


# ── output size guard (task constraint: < 5MB) ───────────────────────────────
@pytest.mark.unit
def test_run_output_is_bounded(harness, monkeypatch):
    import json
    lb = harness
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", True)
    out = lb.run(dry_run=True)
    assert len(json.dumps(out, default=str).encode()) < 5 * 1024 * 1024
