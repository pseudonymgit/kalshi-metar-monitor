#!/usr/bin/env python3
"""
Gamma signal integration tests — REAL assertions (R/T rebuild, 2026-09-28).

The previous version asserted NOTHING: every test wrapped its call in
try/except and printed the result, so an exception was swallowed and the test
still passed. Rebuilt to the test_target_invariance.py standard: real
assertions + a negative control.

Run: .venv-gefs/bin/python -m pytest tests/test_gamma_ensemble_integration.py -q
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

from core.signals.gamma_signals import get_gamma_signals  # noqa: E402
from core.signals.eighty_two_member_ensemble_signal import EightyTwoMemberEnsembleSignal  # noqa: E402
from core.gamma_ensemble_fusion import GammaEnsembleFusion, GammaEnhancedEnsembleSignal  # noqa: E402

# Canonical station (KJFK was used before — not in the canonical 20).
STATION = "KNYC"
VALID_DIRS = {"up", "down", None}


@pytest.fixture(autouse=True)
def _empty_ensemble_archives(tmp_path, monkeypatch):
    """Hermetic: point every signal's GEFS/TIGGE archive at an empty temp DB.

    The gamma signals and 82-member ensemble open data/gefs_archive.db and
    data/tigge_archive.db with plain sqlite3.connect() (write mode), which the
    conftest DB sandbox forbids. An empty archive gives the exercised graceful
    degradation (no members -> (None, 0.0)) without touching production data.
    """
    empty = tmp_path / "empty_ensemble.db"
    empty.touch()
    monkeypatch.setattr("core.signals.gamma_signals.GammaBaseSignal.GEFS_DB", str(empty))

    def _conn(path):
        def _get(self):
            if getattr(self, "_gefs_conn", None) is None:
                self._gefs_conn = sqlite3.connect(path)
            return self._gefs_conn
        return _get

    def _tconn(path):
        def _get(self):
            if getattr(self, "_tigge_conn", None) is None:
                self._tigge_conn = sqlite3.connect(path)
            return self._tigge_conn
        return _get

    monkeypatch.setattr(
        "core.signals.eighty_two_member_ensemble_signal.EightyTwoMemberEnsembleSignal._get_gefs_conn",
        _conn(str(empty)),
    )
    monkeypatch.setattr(
        "core.signals.eighty_two_member_ensemble_signal.EightyTwoMemberEnsembleSignal._get_tigge_conn",
        _tconn(str(empty)),
    )


def _mock_days(n=5):
    return [
        {"station": STATION, "date": f"2023-06-{15+i:02d}", "high": 75.0 + 2 * i,
         "temp": 72.0 + i, "dewpoint": 60.0, "pressure": 1013.0 - 0.5 * i,
         "cloud_cover": 30.0 + 10 * i}
        for i in range(n)
    ]


def test_gamma_signal_set_shape():
    """The gamma family must be exactly the 6 documented signals (silent-shrink guard)."""
    gs = get_gamma_signals()
    names = {s.name for s in gs}
    assert len(gs) == 6, f"expected 6 gamma signals, got {len(gs)}: {names}"
    for expect in ("gamma_g1_uncertainty", "gamma_g2_regime", "gamma_g3_analog",
                   "gamma_g4_pressure_adv", "gamma_g5_dewpoint", "gamma_g6_cloud_cover"):
        assert expect in names, f"missing gamma signal {expect}"


def test_individual_gamma_signals_contract():
    """Every gamma signal must return a (direction, confidence) contract or raise
    NotImplementedError — but NOT an arbitrary exception.

    Regression: gamma_signals.py:_load_gefs_members had `import struct` inside
    the try block while `except (..., struct.error, ...)` was evaluated when the
    GEFS query raised sqlite3.Error -> UnboundLocalError. `struct` is now a
    module-level import, so the graceful (None, 0.0) degradation is reachable.
    """
    days = _mock_days()
    failures = []
    for sig in get_gamma_signals():
        try:
            out = sig.evaluate(len(days) - 1, days)
        except NotImplementedError:
            continue  # explicit stub — allowed, counted separately
        except Exception as e:  # any other exception is a real defect
            failures.append(f"{sig.name}: {type(e).__name__}: {e}")
            continue
        assert isinstance(out, tuple) and len(out) == 2, f"{sig.name} bad shape: {out!r}"
        direction, confidence = out
        assert direction in VALID_DIRS, f"{sig.name} invalid direction {direction!r}"
        assert isinstance(confidence, (int, float)), f"{sig.name} conf not numeric"
        assert 0.0 <= float(confidence) <= 1.0, f"{sig.name} conf out of range: {confidence}"
    assert not failures, "gamma signals raised unexpectedly: " + "; ".join(failures)


def test_82_member_ensemble_contract():
    """The 82-member ensemble must honor the (direction, confidence) contract."""
    sig = EightyTwoMemberEnsembleSignal()
    direction, confidence = sig.evaluate(4, _mock_days())
    assert direction in VALID_DIRS, f"invalid direction {direction!r}"
    assert 0.0 <= float(confidence) <= 1.0, f"conf out of range: {confidence}"


def test_gamma_fusion_contract_and_agreement_bounds():
    """Fusion must return a coherent result object with bounded fields."""
    result = GammaEnsembleFusion().fuse(4, _mock_days())
    assert result.direction in VALID_DIRS, f"fusion direction {result.direction!r}"
    for field in ("agreement_score", "adjusted_confidence"):
        v = getattr(result, field)
        assert 0.0 <= float(v) <= 1.0, f"{field}={v} out of [0,1]"
    # gamma_signals must be a mapping of name -> (direction, confidence)
    assert isinstance(result.gamma_signals, dict)
    for name, pair in result.gamma_signals.items():
        assert isinstance(pair, tuple) and len(pair) == 2, f"{name} bad pair {pair!r}"
        assert pair[0] in VALID_DIRS
        assert 0.0 <= float(pair[1]) <= 1.0


def test_gamma_enhanced_ensemble_contract():
    """The enhanced ensemble must return the (direction, confidence) contract."""
    sig = GammaEnhancedEnsembleSignal()
    direction, confidence = sig.evaluate(4, _mock_days())
    assert direction in VALID_DIRS, f"invalid direction {direction!r}"
    assert 0.0 <= float(confidence) <= 1.0, f"conf out of range: {confidence}"


def test_negative_control_empty_history_does_not_silently_pass():
    """Negative control: a too-short series must NOT produce a confident signal.
    If it does, the harness would be vacuous."""
    too_short = _mock_days(1)
    sig = EightyTwoMemberEnsembleSignal()
    try:
        direction, confidence = sig.evaluate(0, too_short)
    except Exception:
        return  # raising on degenerate input is acceptable
    # If it returns, it must be unconfident or directionless — never a confident call.
    assert (direction is None) or (float(confidence) <= 0.5), \
        f"ensemble emitted a confident call {direction}/{confidence} on 1 day of history"
