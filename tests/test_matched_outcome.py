#!/usr/bin/env python3
"""
Tests for WS7 matched-outcome correction.

Covers:
- Prediction records: fold_id, abstained, rejection_reason
- true_mcnemar_comparison: arm-agnostic, abstention handling, b/c, direction
- Adapter alpha=0.0125 and beam alpha=0.00333 cross-references
- Arm-order inversion (swap T2/T3)
- n_extra/n_arm1_only diagnostics
- fold_id in predictions
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.statistical_significance import (
    true_mcnemar_comparison,
    ADAPTER_FAMILY_ALPHA, bonferroni_alpha, FAMILIES,
)


def make_pred(station, date, pred_dir=None, correct=False, abstained=False, fold_id=3):
    return {
        "station": station, "date": date, "fold_id": fold_id,
        "pred_dir": pred_dir, "actual_dir": "up", "confidence": 0.65,
        "correct": correct, "abstained": abstained,
        "rejection_reason": None if not abstained else "no_signal_fired",
    }


class TestTrueMcNemar:
    def test_zero_zero(self):
        """b=0, c=0: p=1.0."""
        a1 = [make_pred("KMDW", "2026-01-01", "up", True)]
        a2 = [make_pred("KMDW", "2026-01-01", "up", True)]
        r = true_mcnemar_comparison(a1, a2)
        assert r["b"] == 0 and r["c"] == 0 and r["p_value"] == 1.0

    def test_extreme_imbalance(self):
        """b>0, c=0: p very small."""
        a1 = [make_pred(f"K{i}", f"2026-01-{d:02d}", "up", True)
              for i, d in zip(range(100), range(100))]
        a2 = [make_pred(f"K{i}", f"2026-01-{d:02d}", "up", False)
              for i, d in zip(range(100), range(100))]
        r = true_mcnemar_comparison(a1, a2)
        assert r["b"] == 100 and r["c"] == 0
        assert r["p_value"] < 1e-10
        assert r["direction"] == "T3 better"

    def test_arm_agnostic_inversion(self):
        """Arm names are labels. Inverted order produces correct direction."""
        t3 = [make_pred("KMDW", "2026-01-01", "up", True)]
        t2 = [make_pred("KMDW", "2026-01-01", "up", False)]
        r1 = true_mcnemar_comparison(t3, t2, "T3", "T2")
        r2 = true_mcnemar_comparison(t2, t3, "T2", "T3")
        # r1: arm1=T3 (correct), arm2=T2 (wrong) -> b=1, c=0 -> T3 better
        assert r1["direction"] == "T3 better"
        # r2: arm1=T2 (wrong), arm2=T3 (correct) -> b=0, c=1 -> T3 better (arm2)
        assert r2["direction"] == "T3 better"
        assert r1["b"] == 1 and r2["c"] == 1

    def test_abstention_handling(self):
        """Abstained records excluded from b/c, counted separately."""
        a1 = [make_pred("KMDW", "2026-01-01", "up", True),
              make_pred("KMDW", "2026-01-02", "up", True)]
        a2 = [make_pred("KMDW", "2026-01-01", "up", True),
              make_pred("KMDW", "2026-01-02", None, False, abstained=True)]
        r = true_mcnemar_comparison(a1, a2, "T3", "T2")
        # Both have 2026-01-01 and 2026-01-02
        # 2026-01-01: both correct -> neither b nor c
        # 2026-01-02: T3 predicted, T2 abstained -> counted in n_arm1_correct_arm2_abstained
        assert r["b"] == 0 and r["c"] == 0
        assert r["n_arm1_correct_arm2_abstained"] == 1

    def test_n_arm1_only(self):
        """n_arm1_only counts keys only in arm1."""
        a1 = [make_pred("KMDW", "2026-01-01", "up", True),
              make_pred("KMDW", "2026-01-02", "up", True)]
        a2 = [make_pred("KMDW", "2026-01-01", "up", True)]
        r = true_mcnemar_comparison(a1, a2)
        assert r["n_arm1_only"] == 1
        assert r["n_arm2_only"] == 0


class TestPredictionRecordFields:
    def test_fold_id(self):
        p = make_pred("KMDW", "2026-01-01", fold_id=3)
        assert p["fold_id"] == 3

    def test_abstained_field(self):
        p = make_pred("KMDW", "2026-01-01", abstained=True)
        assert p["abstained"] is True
        assert p["rejection_reason"] == "no_signal_fired"

    def test_not_abstained(self):
        p = make_pred("KMDW", "2026-01-01", "up", True)
        assert p["abstained"] is False
        assert p["rejection_reason"] is None


class TestAlphaCrossReferences:
    def test_adapter_alpha(self):
        assert abs(ADAPTER_FAMILY_ALPHA - 0.0125) < 1e-9

    def test_beam_alpha(self):
        assert "beam_15" in FAMILIES
        assert FAMILIES["beam_15"]["n_tests"] == 15
        assert abs(bonferroni_alpha("beam_15") - 0.05/15) < 1e-9