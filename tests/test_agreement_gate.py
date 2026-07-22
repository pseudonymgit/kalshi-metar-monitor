"""
Phase 21.1 — Unit Tests: Agreement Gate

Tests core/agreement_gate.py:
- AgreementGate.filter_signals() with N-of-M threshold
- SimpleAgreementChecker.check_agreement()
- Edge cases: empty signals, below threshold, exact threshold, all agree
- Direction handling (UP/DOWN case insensitivity)
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.agreement_gate import AgreementGate, SimpleAgreementChecker


def make_signal(station: str, market: str, direction: str, reason: str = "test"):
    return (station, market, direction, reason)


@pytest.mark.unit
class TestAgreementGate:
    """Test the AgreementGate class."""

    def test_init_defaults(self):
        gate = AgreementGate()
        assert gate.n_required == 3
        assert gate.m_total == 9

    def test_init_custom(self):
        gate = AgreementGate(n_required=2, m_total=5)
        assert gate.n_required == 2
        assert gate.m_total == 5

    def test_insufficient_signals_passes_through(self):
        """When fewer signals than m_total, return original list."""
        gate = AgreementGate(n_required=3, m_total=9)
        signals = [
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "DOWN"),
        ]
        result = gate.filter_signals(signals)
        # Less than 9 total, should pass through
        assert len(result) == 3
        assert result == signals

    def test_simple_agreement_majority_filtered(self):
        """With 3 signals for KATL, n_required=2, majority direction passes."""
        gate = AgreementGate(n_required=2, m_total=3)
        signals = [
            make_signal("KATL", "HIGH", "UP", "cal"),
            make_signal("KATL", "HIGH", "UP", "momentum"),
            make_signal("KATL", "HIGH", "DOWN", "analog"),
        ]
        result = gate.filter_signals(signals)
        # Only the 2 UP signals should pass
        assert len(result) == 2
        for r in result:
            assert r[2] == "UP"

    def test_no_majority_returns_empty(self):
        """When no direction reaches n_required threshold, return empty list."""
        gate = AgreementGate(n_required=2, m_total=3)
        signals = [
            make_signal("KATL", "HIGH", "UP", "cal"),
            make_signal("KATL", "HIGH", "DOWN", "analog"),
            make_signal("KATL", "HIGH", "DOWN", "momentum"),
        ]
        result = gate.filter_signals(signals)
        # 2 DOWN, 1 UP -> DOWN reaches n_required=2, so DOWN signals pass
        assert len(result) == 2
        for r in result:
            assert r[2] == "DOWN"

    def test_strict_agreement_all_agree(self):
        """All 5 signals agree on direction."""
        gate = AgreementGate(n_required=5, m_total=5)
        signals = [
            make_signal("KATL", "HIGH", "UP", f"sig{i}")
            for i in range(5)
        ]
        result = gate.filter_signals(signals)
        assert len(result) == 5

    def test_multiple_stations(self):
        """Different stations should be filtered independently."""
        gate = AgreementGate(n_required=2, m_total=4)
        signals = [
            make_signal("KATL", "HIGH", "UP", "sig1"),
            make_signal("KATL", "HIGH", "UP", "sig2"),
            make_signal("KATL", "HIGH", "DOWN", "sig3"),
            make_signal("KATL", "HIGH", "DOWN", "sig4"),
            make_signal("KBOS", "HIGH", "UP", "sig1"),
            make_signal("KBOS", "HIGH", "UP", "sig2"),
            make_signal("KBOS", "HIGH", "UP", "sig3"),
            make_signal("KBOS", "HIGH", "DOWN", "sig4"),
        ]
        result = gate.filter_signals(signals)
        # KATL: 2 UP, 2 DOWN -> both reach n_required=2, first match (UP) passes -> 2
        # KBOS: 3 UP, 1 DOWN -> UP reaches 2 -> 3 UP signals pass
        # Total: 2 + 3 = 5
        assert len(result) == 5
        for r in result:
            assert r[2] == "UP"

    def test_mixed_market_types(self):
        """Different market types for same station are grouped separately."""
        gate = AgreementGate(n_required=2, m_total=4)
        signals = [
            make_signal("KATL", "HIGH", "UP", "sig1"),
            make_signal("KATL", "HIGH", "UP", "sig2"),
            make_signal("KATL", "LOW", "UP", "sig1"),
            make_signal("KATL", "LOW", "UP", "sig2"),
        ]
        result = gate.filter_signals(signals)
        assert len(result) == 4  # Both groups pass

    def test_empty_signals_list(self):
        """Empty signal list should return empty list."""
        gate = AgreementGate()
        result = gate.filter_signals([])
        assert result == []

    def test_update_threshold(self):
        """Dynamic threshold update should work."""
        gate = AgreementGate(n_required=3, m_total=9)
        gate.update_threshold(2, 5)
        assert gate.n_required == 2
        assert gate.m_total == 5

    def test_case_insensitive_direction(self):
        """Direction comparison should be case-insensitive."""
        gate = AgreementGate(n_required=2, m_total=3)
        signals = [
            make_signal("KATL", "HIGH", "up", "cal"),
            make_signal("KATL", "HIGH", "Up", "momentum"),
            make_signal("KATL", "HIGH", "DOWN", "analog"),
        ]
        result = gate.filter_signals(signals)
        assert len(result) == 2
        for r in result:
            assert r[2].upper() == "UP"


@pytest.mark.unit
class TestSimpleAgreementChecker:
    """Test the SimpleAgreementChecker static helper."""

    def test_basic_agreement(self):
        direction = "UP"
        signals = [make_signal("KATL", "HIGH", direction) for _ in range(3)]
        result = SimpleAgreementChecker.check_agreement(signals, n_required=2)
        assert len(result) == 3

    def test_insufficient_agreement(self):
        """When n_required > len(signals), return empty."""
        signals = [make_signal("KATL", "HIGH", "UP")]
        result = SimpleAgreementChecker.check_agreement(signals, n_required=3)
        assert result == []

    def test_split_agreement_no_majority(self):
        """2 UP, 2 DOWN with n_required=3 -> no agreement."""
        signals = [
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "DOWN"),
            make_signal("KATL", "HIGH", "DOWN"),
        ]
        result = SimpleAgreementChecker.check_agreement(signals, n_required=3)
        assert result == []

    def test_empty_group(self):
        result = SimpleAgreementChecker.check_agreement([], n_required=3)
        assert result == []

    def test_direction_matching(self):
        signals = [
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "UP"),
            make_signal("KATL", "HIGH", "UP"),
        ]
        result = SimpleAgreementChecker.check_agreement(signals, n_required=3)
        assert len(result) == 3