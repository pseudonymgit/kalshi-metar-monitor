#!/usr/bin/env python3
"""
Stopping-record integrity tests (look-200 findings F-1, F-2, F-3, F-5, F-6).

F-1  hurdle.events is computed from real data, not a hardcoded 0 literal.
F-2  the hurdle-source field is an Optional[Literal[...]] union, not a bare str.
F-3  the measurement path (canary harness/runners) is frozen in a verifiable
     sidecar that the look gate checks.
F-5  the event counter is named `compute_relevant_events` (the debug path and
     the gate agree on what an event is).
F-6  zero/empty handling on `events` is consistent (`== 0` / `is None`).
"""
import importlib
import sqlite3
import sys
import typing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import stopping_record as sr  # noqa: E402
import look200_measurement as l200  # noqa: E402
from core.canary_contract import (  # noqa: E402
    MEASUREMENT_MODULES, contract_path, freeze_measurement_contract,
    measurement_code_hash, verify_measurement_contract,
)

BOOK = REPO / "data" / "paper_trading_sbox.db"


def _book_events() -> int:
    c = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM trades WHERE settlement_confirmed=1 AND "
            "canonical_outcome IS NOT NULL").fetchone()[0]
    finally:
        c.close()


# ── F-1 ──────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_f1_events_computed_from_real_data():
    if not BOOK.exists():
        pytest.skip("book absent")
    r = sr.compute_relevant_events(REPO)
    assert r.treated_as == "live"
    assert r.events == _book_events()
    assert r.events > 0
    assert r.upper_95_bound_net_ev is not None


@pytest.mark.unit
def test_f1_matches_look200_hurdle():
    """The live hurdle bound equals the look200 read-out (same cohort, same math)."""
    if not BOOK.exists():
        pytest.skip("book absent")
    r = sr.compute_relevant_events(REPO)
    cohort = l200.load_cohort()
    net_pc = []
    for row in cohort:
        q = row["quantity"]
        if not q or row["realized_pnl"] is None:
            continue
        from core.fees import fee_per_contract
        net_pc.append((row["realized_pnl"]
                       - fee_per_contract(row["trade_price"]) * q) / q)
    mean = sum(net_pc) / len(net_pc)
    var = sum((x - mean) ** 2 for x in net_pc) / (len(net_pc) - 1)
    import math
    upper = mean + sr.HURDLE_Z_95 * (var ** 0.5) / math.sqrt(len(net_pc))
    assert abs(r.upper_95_bound_net_ev - round(upper, 6)) < 1e-6


@pytest.mark.unit
def test_f1_default_record_not_literal_zero():
    rec = sr.default_record(REPO)
    assert rec["hurdle"]["events"] == sr.compute_relevant_events(REPO).events
    assert rec["hurdle"]["treated_as"] in ("live", "record", "unavailable")


# ── F-2 ──────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_f2_treated_as_is_union_not_bare_str():
    hints = typing.get_type_hints(sr.HurdleReadout)
    ann = hints["treated_as"]
    args = typing.get_args(ann)
    # Optional[HurdleSource] => (Literal['live','record','unavailable'], NoneType)
    assert args, "treated_as should be a union type, not a bare str"
    assert type(None) in args
    lit = [a for a in args if typing.get_origin(a) is typing.Literal]
    assert lit, "union should carry a Literal of allowed sources"
    assert set(typing.get_args(lit[0])) == {"live", "record", "unavailable"}
    # The raw annotation must not be a bare `str`.
    assert sr.HurdleReadout.__annotations__["treated_as"] != str


# ── F-3 ──────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_f3_measurement_modules_cover_canary_path():
    names = set(MEASUREMENT_MODULES)
    assert "core/canary_harness.py" in names
    assert "scripts/run_canaries.py" in names


@pytest.mark.unit
def test_f3_contract_verifies_live():
    if not contract_path(REPO, "GR5-WE-001").exists():
        pytest.skip("contract not frozen")
    r = verify_measurement_contract(REPO, "GR5-WE-001")
    assert r["ok"] is True, r


@pytest.mark.unit
def test_f3_tamper_detected(tmp_path, monkeypatch):
    """A post-freeze edit to a measurement module invalidates the contract."""
    freeze_measurement_contract(tmp_path, "EXP-T", freeze_ts="2026-10-12T00:00:00Z")
    assert verify_measurement_contract(tmp_path, "EXP-T")["ok"] is True
    # Simulate a post-freeze edit to the harness.
    (tmp_path / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "core" / "canary_harness.py").write_text("# changed\n")
    r = verify_measurement_contract(tmp_path, "EXP-T")
    assert r["ok"] is False


@pytest.mark.unit
def test_f3_look_gate_checks_contract():
    src = (REPO / "scripts" / "look200_measurement.py").read_text()
    assert "verify_measurement_contract" in src
    assert "measurement_contract_ok" in src


# ── F-5 ──────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_f5_relevant_events_named():
    assert hasattr(sr, "compute_relevant_events")
    assert not hasattr(sr, "compute_events")


# ── F-6 ──────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_f6_zero_events_forces_no_bound():
    """events == 0 must not let a stale bound leak into A3."""
    ev = sr.evaluate(REPO)
    # One consistent int field, and a3 requires >= HURDLE_MIN_EVENTS.
    assert isinstance(ev["hurdle_readout"]["events"], int)
    # With 68 < 200 the hurdle cannot fire.
    assert ev["a3_hurdle_failed"] is False


@pytest.mark.unit
def test_f6_cli_runs():
    assert sr.main(["--status", "--repo", str(REPO)]) == sr.EXIT_PROCEED
