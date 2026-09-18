#!/usr/bin/env python3
"""
Tests for WS7 ablation/beam corrections (WS7-ABLATION-BEAM-CORRECTION-SPEC).

Covers:
- mcnemar_test: b=100,c=96; zero; k<=n assertion; non-negative assertion
- paired_comparison_fields: serialization, direction
- integer_accuracy: no float accumulation, bounds
- T11 arm definition (family metadata)
- Normalized arm IDs
- Beam alpha consistency (0.05/15)
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.statistical_significance import (
    mcnemar_test, paired_comparison_fields, integer_accuracy,
    ADAPTER_FAMILY_COMPARISONS, ADAPTER_FAMILY_ALPHA,
    bonferroni_alpha, FAMILIES,
)


class TestMcNemar:
    def test_b100_c96(self):
        """b=100, c=96: n=196, k=96, p>0.05 (near-balanced discordance)."""
        p = mcnemar_test(100, 96)
        n = 196
        # Two-sided: P(X<=96 | X~Bin(196, 0.5)) * 2
        assert 0.05 < p < 1.0, f"Expected moderate p, got {p}"

    def test_zero_zero(self):
        """b=0, c=0: n=0, p=1.0 (no discordance, no evidence)."""
        assert mcnemar_test(0, 0) == 1.0

    def test_extreme_imbalance(self):
        """b=100, c=0: n=100, k=0, p very small."""
        p = mcnemar_test(100, 0)
        assert p < 1e-10, f"Expected tiny p, got {p}"

    def test_k_le_n_assert(self):
        """k <= n assertion holds for valid inputs."""
        try:
            mcnemar_test(5, 3)  # valid
        except AssertionError:
            raise AssertionError("Valid inputs should not raise")

    def test_negative_raises(self):
        """Negative inputs raise AssertionError."""
        import pytest
        with pytest.raises(AssertionError):
            mcnemar_test(-1, 5)
        with pytest.raises(AssertionError):
            mcnemar_test(5, -1)


class TestPairedComparisonFields:
    def test_serialization(self):
        fields = paired_comparison_fields(100, 96, 500)
        assert fields["n_matched"] == 500
        assert fields["b"] == 100
        assert fields["c"] == 96
        assert "t3_vs_t2_mcnemar_pvalue" in fields
        assert fields["t3_vs_t2_direction"] == "T3 better"

    def test_direction_tie(self):
        fields = paired_comparison_fields(0, 0, 10)
        assert fields["t3_vs_t2_direction"] == "tie"
        assert fields["t3_vs_t2_mcnemar_pvalue"] == 1.0

    def test_direction_t2(self):
        fields = paired_comparison_fields(10, 30, 100)
        assert fields["t3_vs_t2_direction"] == "T2 better"


class TestIntegerAccuracy:
    def test_exact_division(self):
        assert integer_accuracy(100, 62) == 0.62

    def test_zero_trades(self):
        assert integer_accuracy(0, 0) == 0.0

    def test_correct_le_trades(self):
        try:
            integer_accuracy(50, 30)  # valid
        except AssertionError:
            raise AssertionError("Valid inputs should not raise")

    def test_correct_exceeds_trades_raises(self):
        import pytest
        with pytest.raises(AssertionError):
            integer_accuracy(30, 50)

    def test_no_float_accumulation(self):
        """Accuracy from integers matches exact rational, not float accumulation."""
        # Simulate two folds: 30/50 and 32/50
        t1, c1 = 50, 30
        t2, c2 = 50, 32
        total_trades = t1 + t2  # 100
        total_correct = c1 + c2  # 62
        assert integer_accuracy(total_trades, total_correct) == 0.62


class TestArmFamilyMetadata:
    def test_adapter_four_comparisons(self):
        assert len(ADAPTER_FAMILY_COMPARISONS) == 4

    def test_t11_in_family(self):
        """T11 (full-ensemble adapter) is part of the predeclared family."""
        labels = [c[0] for c in ADAPTER_FAMILY_COMPARISONS]
        assert "T11" in labels
        assert "T0" in [c[1] for c in ADAPTER_FAMILY_COMPARISONS]

    def test_adapter_alpha(self):
        assert ADAPTER_FAMILY_ALPHA == 0.05 / 4

    def test_beam_family_alpha(self):
        """Beam family = 15 candidates, alpha = 0.05/15 = 0.00333."""
        assert "beam_15" in FAMILIES
        assert FAMILIES["beam_15"]["n_tests"] == 15
        assert bonferroni_alpha("beam_15") == 0.05 / 15


class TestNormalizedArmIDs:
    def test_normalized_ids(self):
        """Arm IDs match spec I4 normalization."""
        expected = ["T2", "T3", "T3a", "T3b", "T11"]
        # The family comparison labels should map to normalized IDs
        labels = [c[0] for c in ADAPTER_FAMILY_COMPARISONS]
        assert "T3" in labels and "T3a" in labels and "T3b" in labels and "T11" in labels
        # T2 is the baseline reference in comparisons
        assert "T2" in [c[1] for c in ADAPTER_FAMILY_COMPARISONS]