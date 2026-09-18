#!/usr/bin/env python3
"""
Tests for core/statistical_significance.py — QC2 spec compliance.

Gates:
- G1: clopper_pearson_95_ci returns correct bounds
- G2: p_vs_coinflip computes correctly (known values)
- G3: p_vs_baseline computes correctly
- G4: three_condition_gate applies all three conditions
- G5: bonferroni_alpha matches fixed families
- G6: serialize_adapter_config captures flags
- G7: beam_selection_family builds metadata
"""
import os, sys, unittest, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.statistical_significance import (
    clopper_pearson_95_ci, p_vs_coinflip, p_vs_baseline,
    three_condition_gate, bonferroni_alpha,
    serialize_adapter_config, beam_selection_family,
    ADAPTER_FAMILY_COMPARISONS,
)


class TestClopperPearsonCI(unittest.TestCase):
    """G1: Descriptive 95% CP CI."""

    def test_perfect_accuracy(self):
        lo, hi = clopper_pearson_95_ci(100, 100)
        self.assertGreater(lo, 0.95)
        self.assertAlmostEqual(hi, 1.0)

    def test_coinflip(self):
        lo, hi = clopper_pearson_95_ci(50, 100)
        self.assertLess(lo, 0.60)
        self.assertGreater(hi, 0.40)

    def test_zero_trades(self):
        lo, hi = clopper_pearson_95_ci(0, 0)
        self.assertEqual(lo, 0.0)
        self.assertEqual(hi, 0.0)

    def test_zero_correct(self):
        lo, hi = clopper_pearson_95_ci(0, 50)
        self.assertAlmostEqual(lo, 0.0)


class TestPValues(unittest.TestCase):
    """G2/G3: Binomial p-values."""

    def test_coinflip_mid(self):
        p = p_vs_coinflip(50, 100)
        self.assertGreater(p, 0.05)

    def test_coinflip_extreme(self):
        p = p_vs_coinflip(90, 100)
        self.assertLess(p, 1e-5)

    def test_vs_baseline(self):
        p = p_vs_baseline(60, 100, 0.5)
        self.assertGreater(p, 0.01)

    def test_zero_trades(self):
        self.assertEqual(p_vs_coinflip(0, 0), 1.0)
        self.assertEqual(p_vs_baseline(0, 0, 0.5), 1.0)


class TestThreeConditionGate(unittest.TestCase):
    """G4: Promotion gate."""

    def test_all_three_pass(self):
        # 90/100 trades, baseline=0.40, 136-family alpha=0.05/136
        result = three_condition_gate(90, 100, 0.40, family="all_136_pairs")
        self.assertTrue(result["significant"])
        self.assertTrue(result["gate"]["cond1_p_coin"])
        self.assertTrue(result["gate"]["cond2_p_base"])
        self.assertTrue(result["gate"]["cond3_est_gt_base"])

    def test_below_baseline_fails(self):
        # 45/100 trades, baseline=0.50
        result = three_condition_gate(45, 100, 0.50, family="all_136_pairs")
        self.assertFalse(result["gate"]["cond3_est_gt_base"])

    def test_zero_trades(self):
        result = three_condition_gate(0, 0, 0.5)
        self.assertFalse(result["significant"])

    def test_dummy_field(self):
        result = three_condition_gate(50, 100, 0.5)
        self.assertEqual(result["dummy_pairs"], "excluded, not executed")


class TestBonferroni(unittest.TestCase):
    """G5: Fixed families."""

    def test_136_family(self):
        self.assertAlmostEqual(bonferroni_alpha("all_136_pairs"), 0.05 / 136, places=8)

    def test_78_family(self):
        self.assertAlmostEqual(bonferroni_alpha("nondegenerate_78"), 0.05 / 78, places=8)

    def test_adapter_family(self):
        self.assertAlmostEqual(bonferroni_alpha("adapter_4"), 0.05 / 4, places=8)

    def test_beam_family(self):
        self.assertAlmostEqual(bonferroni_alpha("beam_15"), 0.05 / 15, places=8)

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            bonferroni_alpha("nonsense")

    def test_adapter_four_comparisons(self):
        self.assertEqual(len(ADAPTER_FAMILY_COMPARISONS), 4)


class TestConfigSerialization(unittest.TestCase):
    """G6: Adapter config flags."""

    def test_serialize_on(self):
        c = serialize_adapter_config(True, True, True)
        self.assertTrue(c["use_direction_weighted_adapter"])
        self.assertTrue(c["use_ds_conflict"])
        self.assertTrue(c["use_diversity_modifier"])

    def test_serialize_off(self):
        c = serialize_adapter_config(False, False, False)
        self.assertFalse(c["use_direction_weighted_adapter"])
        self.assertFalse(c["use_ds_conflict"])
        self.assertFalse(c["use_diversity_modifier"])


class TestBeamMetadata(unittest.TestCase):
    """G7: Beam selection family."""

    def test_metadata(self):
        bm = beam_selection_family(["pd", "cpd"], 15)
        self.assertEqual(bm["base_pair"], ["pd", "cpd"])
        self.assertEqual(bm["candidates"], 15)
        self.assertEqual(bm["correction"], "Bonferroni")
        self.assertAlmostEqual(bm["alpha_prime"], 0.05 / 15, places=8)
        self.assertAlmostEqual(bm["alpha_overall"], 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)