#!/usr/bin/env python3
"""F7 wired (verification, 2026-10-13) — grade gate reaches the booking path.

`tests/test_grade_gate_f7.py` pins the grade helpers in isolation. This file
pins the WIRING: `run` computes the grade during trade booking, refuses an F
grade before insert, caps the size by grade, and the companion table carries the
grade provenance columns.

Run: python3 -m pytest tests/test_f7_grade_wired_bridge.py -v
"""
import importlib.util
import inspect
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_computes_and_enforces_the_grade():
    """Source contract: grade is computed, F is rejected, size is capped."""
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert "compute_trade_grade(" in src, "booking must compute the grade"
    assert 'grade == "F"' in src, "an F grade must be rejected before insert"
    assert "size_usd = min(size_usd, grade_cap)" in src, "the grade must cap size"
    assert '"grade_entered": grade' in src, "grade provenance must reach the payload"


def test_grade_rejects_low_edge_as_F():
    b = _load_bridge()
    # edge 0.005 < D's 0.01 floor -> F -> not enterable.
    grade, edge = b.compute_trade_grade(0.505, 0.50)
    assert grade == "F"
    assert b.grade_position_cap(grade) == 0.0


def test_grade_cap_binds_below_the_ladder():
    b = _load_bridge()
    # ladder gives $10 at conf>0.70 (capped at $10, Dan 2026-10-13); a C grade
    # caps it to $5.
    assert min(b._position_size_usd(0.95), b.grade_position_cap("C")) == 5.0
    # S grade keeps the (now $10) ladder max.
    assert min(b._position_size_usd(0.95), b.grade_position_cap("S")) == 10.0
