#!/usr/bin/env python3
"""
Tests for core/position_sizer.py — Phase 1 Build

Validates:
- Fee rate is 0.0205 (not 0.0)
- Kelly fraction formula is correct
- Adaptive confidence multipliers
- Bankroll caps (8% per position)
- Drawdown protection (halve at 10%)
- Bayesian belief updates
- Disagreement computation
- Instance config factory
- Rolling win rate tracking
"""

import sys
import os
import math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.position_sizer import (
    PositionSizer,
    BayesianBelief,
    PositionSizingConfig,
    compute_disagreement,
    compute_kelly_multiplier_from_disagreement,
    classify_confidence,
    get_config_for_instance,
    DEFAULT_COST_FRACTION,
    MAX_BANKROLL_PCT,
    MAX_DRAWDOWN_PCT,
    ConfidenceTier,
)


def test_fee_rate_is_non_zero():
    """Verify the fee rate is 0.0205, not 0.0."""
    assert DEFAULT_COST_FRACTION > 0.0, (
        f"DEFAULT_COST_FRACTION should be > 0, got {DEFAULT_COST_FRACTION}"
    )
    assert abs(DEFAULT_COST_FRACTION - 0.0205) < 0.001, (
        f"Fee rate should be ~0.0205, got {DEFAULT_COST_FRACTION}"
    )
    print(f"  ✓ Fee rate: {DEFAULT_COST_FRACTION}")


def test_kelly_fraction_formula():
    """Verify Kelly formula: f* = (p - c) / (1 - c)."""
    sizer = PositionSizer(bankroll=10000.0)
    k = sizer.calculate_kelly_fraction(win_rate=0.60)
    expected = (0.60 - DEFAULT_COST_FRACTION) / (1.0 - DEFAULT_COST_FRACTION)
    assert abs(k - expected) < 1e-6, f"Expected {expected}, got {k}"
    print(f"  ✓ Kelly fraction (p=0.60): f*={k:.4f}")


def test_kelly_fraction_low_win_rate():
    """Kelly fraction should be small but positive for win rate just above cost."""
    sizer = PositionSizer(bankroll=10000.0)
    k = sizer.calculate_kelly_fraction(win_rate=DEFAULT_COST_FRACTION + 0.01)
    assert k > 0, f"Expected positive Kelly for win_rate > cost, got {k}"
    assert k < 0.1, f"Expected small Kelly, got {k}"
    print(f"  ✓ Kelly fraction (p=cost+0.01): f*={k:.6f}")


def test_kelly_fraction_zero_below_cost():
    """Kelly fraction should be 0 when win rate <= cost."""
    sizer = PositionSizer(bankroll=10000.0)
    k = sizer.calculate_kelly_fraction(win_rate=0.01)
    assert k == 0.0, f"Expected 0 Kelly for win_rate < cost, got {k}"
    print(f"  ✓ Kelly fraction (p=0.01): f*={k}")


def test_adaptive_multiplier():
    """Verify confidence-based adaptive multiplier tiers."""
    sizer = PositionSizer(bankroll=10000.0)
    test_cases = [
        (0.50, 0.5, "conf=50%"),
        (0.60, 1.0, "conf=60%"),
        (0.65, 1.0, "conf=65%"),
        (0.70, 1.5, "conf=70%"),
        (0.75, 1.5, "conf=75%"),
        (0.80, 2.0, "conf=80%"),
        (0.90, 2.0, "conf=90%"),
    ]
    for conf, expected, label in test_cases:
        mult = sizer.set_adaptive_multiplier(conf)
        assert mult == expected, f"Expected {expected}, got {mult} for {label}"
    print(f"  ✓ All adaptive multiplier tiers correct")


def test_bankroll_cap():
    """Verify 8% bankroll cap per position."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.80, edge=0.50
    )
    expected_cap = 10000.0 * MAX_BANKROLL_PCT  # 800
    assert size <= expected_cap, (
        f"Position ${size:.2f} exceeds cap ${expected_cap:.2f}"
    )
    assert size == expected_cap, (
        f"Expected ${expected_cap:.2f} (cap), got ${size:.2f}"
    )
    print(f"  ✓ Bankroll cap: ${size:.2f} <= ${expected_cap:.2f}")


def test_drawdown_protection():
    """Verify 10% drawdown halves position size."""
    sizer = PositionSizer(bankroll=10000.0)
    # First position (no drawdown)
    size1, details1 = sizer.compute_position_size(
        confidence=0.90, win_rate=0.60, edge=0.10
    )
    assert "ACTIVE" not in details1["drawdown_protection"]

    # Simulate 15% drawdown
    sizer.update_bankroll(8500.0)
    size2, details2 = sizer.compute_position_size(
        confidence=0.90, win_rate=0.60, edge=0.10
    )
    assert "ACTIVE" in details2["drawdown_protection"]
    # Half of 8% of 8500 = half of 680 = 340
    expected = 8500.0 * MAX_BANKROLL_PCT * 0.5
    assert abs(size2 - expected) < 0.01, (
        f"Expected ${expected:.2f}, got ${size2:.2f}"
    )
    print(f"  ✓ Drawdown protection: ${size2:.2f} (expected ${expected:.2f})")


def test_bayesian_belief():
    """Verify Bayesian Beta-Binomial belief updates."""
    sizer = PositionSizer(bankroll=10000.0)
    posterior = sizer.update_belief(wins=30, losses=20)
    expected = 31.0 / 52.0  # Beta(31, 21)
    assert abs(posterior - expected) < 1e-6, (
        f"Expected {expected}, got {posterior}"
    )
    print(f"  ✓ Bayesian belief: posterior={posterior:.4f}")


def test_bayesian_confidence_interval():
    """Verify Bayesian confidence interval is reasonable."""
    belief = BayesianBelief(alpha=31.0, beta=21.0)
    lo, hi = belief.confidence_interval()
    assert 0 < lo < belief.posterior_mean < hi < 1, (
        f"Invalid CI: [{lo:.4f}, {hi:.4f}], mean={belief.posterior_mean:.4f}"
    )
    print(f"  ✓ 95% CI: [{lo:.4f}, {hi:.4f}]")


def test_disagreement_computation():
    """Verify disagreement computation."""
    # All agree → 0 disagreement
    d0 = compute_disagreement(['up', 'up', 'up'])
    assert d0 == 0.0, f"Expected 0 disagreement, got {d0}"

    # Equal split → max disagreement
    d1 = compute_disagreement(['up', 'down'])
    assert d1 >= 0.9, f"Expected high disagreement, got {d1}"

    # 2 up, 1 down → moderate disagreement
    d2 = compute_disagreement(['up', 'up', 'down'])
    assert 0 < d2 < 1.0, f"Expected moderate disagreement, got {d2}"

    print(f"  ✓ Disagreement: all-agree={d0:.2f}, split={d1:.2f}, "
          f"majority={d2:.2f}")


def test_kelly_multiplier_from_disagreement():
    """Verify Kelly multiplier from disagreement."""
    # All agree → full multiplier
    m0 = compute_kelly_multiplier_from_disagreement(['up', 'up', 'up'])
    assert m0 == 1.0, f"Expected 1.0, got {m0}"

    # All disagree → min multiplier
    m1 = compute_kelly_multiplier_from_disagreement(['up', 'down'])
    assert m1 < 0.5, f"Expected low multiplier, got {m1}"

    # Custom base/min
    m2 = compute_kelly_multiplier_from_disagreement(
        ['up', 'down'], base_multiplier=0.5, min_multiplier=0.2
    )
    assert m2 < 0.5, f"Expected <= 0.5, got {m2}"

    print(f"  ✓ Kelly multiplier: all-agree={m0:.2f}, split={m1:.2f}, "
          f"custom={m2:.2f}")


def test_instance_config():
    """Verify instance config factory."""
    prod = get_config_for_instance("PROD")
    assert prod.fee_rate == DEFAULT_COST_FRACTION
    assert prod.fraction_kelly == 0.5
    assert prod.max_position_fraction == 0.25
    assert abs(prod.base_size_usd - 100.0) < 0.01

    dev = get_config_for_instance("DEV")
    assert dev.fee_rate == DEFAULT_COST_FRACTION
    assert abs(dev.base_size_usd - 50.0) < 0.01

    sbox = get_config_for_instance("SBOX")
    assert sbox.fee_rate == DEFAULT_COST_FRACTION
    assert abs(sbox.base_size_usd - 10.0) < 0.01

    print(f"  ✓ Instance config: PROD(base=${prod.base_size_usd}), "
          f"DEV(base=${dev.base_size_usd}), SBOX(base=${sbox.base_size_usd})")


def test_rolling_win_rate():
    """Verify rolling win rate tracking."""
    sizer = PositionSizer(bankroll=10000.0)
    # Default should be 0.65
    default = sizer.get_rolling_win_rate()
    assert default == 0.65, f"Expected 0.65, got {default}"

    # Add 20 trades with ~66% win rate
    for i in range(20):
        sizer.add_win_result(
            (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d'),
            i % 3 != 2,
        )
    wr = sizer.get_rolling_win_rate()
    assert 0.5 < wr < 1.0, f"Expected ~0.66, got {wr}"
    print(f"  ✓ Rolling win rate: {wr:.4f}")


def test_classify_confidence():
    """Verify confidence tier classification."""
    config = PositionSizingConfig()
    assert classify_confidence(0.80, config) == ConfidenceTier.HIGH
    assert classify_confidence(0.60, config) == ConfidenceTier.MEDIUM
    assert classify_confidence(0.40, config) == ConfidenceTier.LOW
    print(f"  ✓ Confidence tiers: HIGH(0.80), MEDIUM(0.60), LOW(0.40)")


def test_position_sizing_config_post_init():
    """Verify PositionSizingConfig post-init fixes fee_rate=0."""
    config = PositionSizingConfig(fee_rate=0.0)
    # Should be overridden to DEFAULT_COST_FRACTION
    assert config.fee_rate == DEFAULT_COST_FRACTION, (
        f"Expected {DEFAULT_COST_FRACTION}, got {config.fee_rate}"
    )
    print(f"  ✓ PositionSizingConfig fixes fee_rate=0 → {config.fee_rate}")


def test_small_bankroll():
    """Verify bankroll cap works with small bankrolls."""
    sizer = PositionSizer(bankroll=100.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.80, edge=0.50
    )
    expected = 100.0 * MAX_BANKROLL_PCT  # 8.0
    assert size <= expected, f"Expected <= ${expected:.2f}, got ${size:.2f}"
    print(f"  ✓ Small bankroll ($100): position=${size:.2f}")


def test_position_sizer_with_zero_confidence():
    """Verify zero confidence produces zero position."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.0, win_rate=0.50, edge=0.0
    )
    assert size == 0.0, f"Expected 0 position, got ${size:.2f}"
    print(f"  ✓ Zero confidence → position=${size:.2f}")


def test_edge_below_minimum_no_trade():
    """Edge < 0.03 -> NO TRADE."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.50, edge=0.02
    )
    assert size == 0.0, f"Expected 0 position for edge=0.02, got ${size:.2f}"
    assert details.get("edge_tier_label", "") == "NO TRADE — edge < 0.03", \
        f"Wrong edge tier label: {details.get('edge_tier_label')}"
    print(f"  ✓ Edge=0.02 (<0.03): position=${size:.2f}")


def test_edge_weak_edge_25pct_kelly():
    """Edge 0.03-0.06 -> 25% Kelly."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.55, edge=0.05
    )
    assert size > 0.0, f"Expected position > 0 for edge=0.05, got ${size:.2f}"
    assert details.get("edge_multiplier", 0.0) == 0.25, \
        f"Expected 0.25 edge_multiplier, got {details.get('edge_multiplier')}"
    assert "25% Kelly" in details.get("edge_tier_label", ""), \
        f"Wrong edge tier label: {details.get('edge_tier_label')}"
    print(f"  ✓ Edge=0.05 (weak): edge_multiplier={details['edge_multiplier']}, position=${size:.2f}")


def test_edge_moderate_edge_50pct_kelly():
    """Edge 0.06-0.10 -> 50% Kelly."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.55, edge=0.08
    )
    assert size > 0.0, f"Expected position > 0 for edge=0.08, got ${size:.2f}"
    assert details.get("edge_multiplier", 0.0) == 0.50, \
        f"Expected 0.50 edge_multiplier, got {details.get('edge_multiplier')}"
    assert "50% Kelly" in details.get("edge_tier_label", ""), \
        f"Wrong edge tier label: {details.get('edge_tier_label')}"
    print(f"  ✓ Edge=0.08 (moderate): edge_multiplier={details['edge_multiplier']}, position=${size:.2f}")


def test_edge_strong_edge_75pct_kelly():
    """Edge > 0.10 -> 75% Kelly."""
    sizer = PositionSizer(bankroll=10000.0)
    size, details = sizer.compute_position_size(
        confidence=0.90, win_rate=0.80, edge=0.20
    )
    assert size > 0.0, f"Expected position > 0 for edge=0.20, got ${size:.2f}"
    assert details.get("edge_multiplier", 0.0) == 0.75, \
        f"Expected 0.75 edge_multiplier, got {details.get('edge_multiplier')}"
    assert "75% Kelly" in details.get("edge_tier_label", ""), \
        f"Wrong edge tier label: {details.get('edge_tier_label')}"
    print(f"  ✓ Edge=0.20 (strong): edge_multiplier={details['edge_multiplier']}, position=${size:.2f}")


def test_entry_price_validation():
    """Validate entry price clamping."""
    assert PositionSizer.validate_entry_price(0.10) == 0.15, "Price 0.10 should clamp to 0.15"
    assert PositionSizer.validate_entry_price(0.50) == 0.50, "Price 0.50 should stay"
    assert PositionSizer.validate_entry_price(0.90) == 0.85, "Price 0.90 should clamp to 0.85"
    print(f"  ✓ Entry price validation: 0.10→0.15, 0.50→0.50, 0.90→0.85")


def test_max_contracts_cap():
    """Cap contracts at 500."""
    assert PositionSizer.cap_contracts(100) == 100, "100 contracts should stay"
    assert PositionSizer.cap_contracts(500) == 500, "500 contracts should stay"
    assert PositionSizer.cap_contracts(1000) == 500, "1000 contracts should cap to 500"
    print(f"  ✓ Max contracts cap: 100→100, 500→500, 1000→500")


# ─── Run all tests ──────────────────────────────────────────────────────────

def main():
    print("\n=== Position Sizer Tests — Phase 1 Build ===\n")

    tests = [
        ("Fee rate is non-zero", test_fee_rate_is_non_zero),
        ("Kelly fraction formula", test_kelly_fraction_formula),
        ("Kelly fraction low win rate", test_kelly_fraction_low_win_rate),
        ("Kelly fraction zero below cost", test_kelly_fraction_zero_below_cost),
        ("Adaptive multiplier", test_adaptive_multiplier),
        ("Bankroll cap", test_bankroll_cap),
        ("Drawdown protection", test_drawdown_protection),
        ("Bayesian belief", test_bayesian_belief),
        ("Bayesian confidence interval", test_bayesian_confidence_interval),
        ("Disagreement computation", test_disagreement_computation),
        ("Kelly multiplier from disagreement", test_kelly_multiplier_from_disagreement),
        ("Instance config", test_instance_config),
        ("Rolling win rate", test_rolling_win_rate),
        ("Classify confidence", test_classify_confidence),
        ("Config post-init fixes fee_rate=0", test_position_sizing_config_post_init),
        ("Small bankroll", test_small_bankroll),
        ("Zero confidence", test_position_sizer_with_zero_confidence),
        ("Edge < 0.03 NO TRADE", test_edge_below_minimum_no_trade),
        ("Edge 0.03-0.06 25% Kelly", test_edge_weak_edge_25pct_kelly),
        ("Edge 0.06-0.10 50% Kelly", test_edge_moderate_edge_50pct_kelly),
        ("Edge > 0.10 75% Kelly", test_edge_strong_edge_75pct_kelly),
        ("Entry price validation", test_entry_price_validation),
        ("Max contracts cap", test_max_contracts_cap),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, "
          f"{passed + failed} total")
    print(f"{'=' * 50}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())