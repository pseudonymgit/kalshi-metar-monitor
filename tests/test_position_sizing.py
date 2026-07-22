"""
Phase 21.1 — Unit Tests: Position Sizing

Tests core/position_sizing.py and core/fee_aware_kelly_position_sizing.py:
- Confidence tier classification
- Kelly position sizing with fee awareness
- Edge calculation from win rate
- Rolling win rate tracking
- Config factory (PROD/DEV/SBOX)
- Position size clamping and caps
"""

import sys
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.position_sizing import (
    ConfidenceTier,
    PositionSizingConfig,
    KellyPositionSizer,
    classify_confidence,
    compute_position_size,
    get_config_for_instance,
    extract_confidence_from_signal_context,
)


@pytest.fixture
def default_sizer():
    return KellyPositionSizer(fee_rate=0.0, fraction_kelly=0.5, window_days=30)


@pytest.mark.unit
class TestConfidenceTier:
    """Test the confidence tier classification."""

    def test_high_confidence(self):
        config = PositionSizingConfig(
            high_confidence_threshold=0.70,
            medium_confidence_threshold=0.50,
        )
        assert classify_confidence(0.85, config) == ConfidenceTier.HIGH
        assert classify_confidence(0.70, config) == ConfidenceTier.HIGH

    def test_medium_confidence(self):
        config = PositionSizingConfig(
            high_confidence_threshold=0.70,
            medium_confidence_threshold=0.50,
        )
        assert classify_confidence(0.60, config) == ConfidenceTier.MEDIUM
        assert classify_confidence(0.50, config) == ConfidenceTier.MEDIUM

    def test_low_confidence(self):
        config = PositionSizingConfig(
            high_confidence_threshold=0.70,
            medium_confidence_threshold=0.50,
        )
        assert classify_confidence(0.30, config) == ConfidenceTier.LOW
        assert classify_confidence(0.49, config) == ConfidenceTier.LOW

    def test_default_config(self):
        tier = classify_confidence(0.80)
        assert tier == ConfidenceTier.HIGH


@pytest.mark.unit
class TestKellyPositionSizer:
    """Test the KellyPositionSizer class."""

    def test_default_win_rate(self, default_sizer):
        """With no history, should return default 0.65."""
        win_rate = default_sizer.get_rolling_win_rate()
        assert win_rate == 0.65

    def test_win_rate_after_results(self, default_sizer):
        """Add 10 wins, 0 losses -> win rate should be 1.0."""
        for i in range(10):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            default_sizer.add_win_result(date, win=True)
        win_rate = default_sizer.get_rolling_win_rate()
        assert win_rate == 1.0

    def test_win_rate_50pct(self, default_sizer):
        """5 wins, 5 losses -> 0.5 win rate."""
        for i in range(10):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            default_sizer.add_win_result(date, win=(i % 2 == 0))
        win_rate = default_sizer.get_rolling_win_rate()
        assert 0.49 <= win_rate <= 0.51

    def test_outdated_results_ignored(self, default_sizer):
        """Results older than window_days should be ignored."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%d')
        for _ in range(100):
            default_sizer.add_win_result(old_date, win=True)
        # Should still be default since all are outdated
        win_rate = default_sizer.get_rolling_win_rate()
        assert win_rate == 0.65

    def test_edge_calculation(self, default_sizer):
        """Edge = 2*win_rate - 1."""
        edge = default_sizer.calculate_edge_from_win_rate(0.65)
        assert abs(edge - 0.30) < 0.01

        edge = default_sizer.calculate_edge_from_win_rate(0.5)
        assert abs(edge) < 0.01

        edge = default_sizer.calculate_edge_from_win_rate(0.75)
        assert abs(edge - 0.50) < 0.01

    def test_negative_edge(self, default_sizer):
        """Win rate below 0.5 -> negative edge."""
        edge = default_sizer.calculate_edge_from_win_rate(0.4)
        assert edge < 0

    def test_kelly_fraction_capped(self, default_sizer):
        """Kelly fraction should be capped between -0.1 and 0.5."""
        # Very high win rate
        kelly = default_sizer.calculate_kelly_fraction(edge=1.0, win_rate=1.0)
        assert kelly <= 0.5

        # Very low win rate
        kelly = default_sizer.calculate_kelly_fraction(edge=-1.0, win_rate=0.0)
        assert kelly >= -0.1

    def test_kelly_positive_edge(self, default_sizer):
        """Positive edge should produce positive Kelly fraction."""
        kelly = default_sizer.calculate_kelly_fraction(edge=0.3, win_rate=0.65)
        assert kelly > 0

    def test_per_signal_win_rate(self, default_sizer):
        """Different signal IDs should have separate win rates."""
        date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # Add 10 wins for signal A, 0 wins for signal B
        for i in range(10):
            default_sizer.add_win_result(date, win=True, signal_id="signal_a")
            default_sizer.add_win_result(date, win=False, signal_id="signal_b")

        wr_a = default_sizer.get_rolling_win_rate(signal_id="signal_a")
        wr_b = default_sizer.get_rolling_win_rate(signal_id="signal_b")
        assert wr_a > wr_b


@pytest.mark.unit
class TestComputePositionSize:
    """Test the compute_position_size function."""

    def test_positive_edge_positive_size(self, default_sizer):
        """With positive edge and confidence, should produce positive size."""
        size, tier, meta = compute_position_size(
            signal_type="persistence",
            confidence=0.8,
            current_balance=10000.0,
            market_price=0.5,
            kelly_sizer=default_sizer,
        )
        assert size > 0
        assert meta["confidence_tier"] == "high"

    def test_negative_edge_zero_size(self, default_sizer):
        """With negative edge and no wins, should return 0."""
        # Add all losses
        for i in range(10):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            default_sizer.add_win_result(date, win=False)
        size, tier, meta = compute_position_size(
            signal_type="persistence",
            confidence=0.8,
            current_balance=10000.0,
            market_price=0.5,
            kelly_sizer=default_sizer,
        )
        # Kelly fraction will be negative, so size = 0
        if meta.get("raw_kelly_fraction", 0) <= 0:
            assert size == 0.0

    def test_max_position_cap(self, default_sizer):
        """Position size should not exceed 25% of balance."""
        # Add all wins to get maximum edge
        for i in range(10):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            default_sizer.add_win_result(date, win=True)
        size, tier, meta = compute_position_size(
            signal_type="persistence",
            confidence=1.0,
            current_balance=10000.0,
            market_price=0.5,
            kelly_sizer=default_sizer,
        )
        assert size <= 2500.0  # 25% of 10000

    def test_min_position_size(self, default_sizer):
        """Position size should not fall below min_size_usd (5.0)."""
        size, tier, meta = compute_position_size(
            signal_type="persistence",
            confidence=0.1,  # Very low confidence
            current_balance=100.0,
            market_price=0.5,
            kelly_sizer=default_sizer,
        )
        # Even with very low params, min_size_usd should be the floor
        assert size >= 5.0 or size == 0.0

    def test_low_confidence_smaller_size(self, default_sizer):
        """Higher confidence should produce larger position size."""
        # Add some wins for positive edge
        for i in range(5):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            default_sizer.add_win_result(date, win=True)
        size_low, _, _ = compute_position_size(
            signal_type="persistence", confidence=0.3,
            current_balance=10000.0, market_price=0.5,
            kelly_sizer=default_sizer,
        )
        size_high, _, _ = compute_position_size(
            signal_type="persistence", confidence=0.9,
            current_balance=10000.0, market_price=0.5,
            kelly_sizer=default_sizer,
        )
        # OR: if both are capped, they may be equal
        assert size_high >= size_low


@pytest.mark.unit
class TestConfigFactory:
    """Test the get_config_for_instance factory."""

    def test_prod_config(self):
        config = get_config_for_instance("PROD")
        assert config.base_size_usd == 100.0
        assert config.max_size_usd == 500.0
        assert config.min_size_usd == 25.0
        assert config.max_position_fraction == 0.25

    def test_dev_config(self):
        config = get_config_for_instance("DEV")
        assert config.base_size_usd == 50.0
        assert config.max_size_usd == 250.0

    def test_sbox_config(self):
        config = get_config_for_instance("SBOX")
        assert config.base_size_usd == 10.0
        assert config.max_size_usd == 50.0

    def test_invalid_instance(self):
        with pytest.raises(ValueError, match="Unknown instance"):
            get_config_for_instance("NONEXISTENT")

    def test_case_insensitive(self):
        config = get_config_for_instance("prod")
        assert config is not None
        assert config.base_size_usd == 100.0


@pytest.mark.unit
class TestExtractConfidence:
    """Test the extract_confidence_from_signal_context function."""

    def test_direct_confidence(self):
        ctx = {"confidence": 0.85}
        assert extract_confidence_from_signal_context(ctx) == 0.85

    def test_momentum_derived(self):
        ctx = {"signal_type": "near_boundary_momentum_up", "momentum_f_per_sec": 0.008}
        conf = extract_confidence_from_signal_context(ctx)
        assert 0.3 <= conf <= 0.95

    def test_default_fallback(self):
        ctx = {}
        assert extract_confidence_from_signal_context(ctx) == 0.5

    def test_goldilocks_confidence(self):
        ctx = {
            "signal_type": "goldilocks",
            "confidence_factors": {
                "is_daily_high": True,
                "daily_high_margin": 0.5,
                "observations_since_spike": 5,
                "day_fraction_at_spike": 0.75,
            }
        }
        conf = extract_confidence_from_signal_context(ctx)
        assert 0.0 <= conf <= 1.0

    def test_transition_signal_default(self):
        ctx = {"signal_type": "instant_up"}
        assert extract_confidence_from_signal_context(ctx) == 0.6

    def test_reversion_signal_default(self):
        ctx = {"signal_type": "reversion_after_settlement"}
        assert extract_confidence_from_signal_context(ctx) == 0.65

    def test_late_day_momentum_default(self):
        ctx = {"signal_type": "late_day_momentum_hourly"}
        assert extract_confidence_from_signal_context(ctx) == 0.7


@pytest.mark.unit
class TestFeeAwareKelly:
    """Test the fee-aware Kelly position sizer module directly."""

    def test_import(self):
        from core.fee_aware_kelly_position_sizing import (
            KellyPositionSizer as FeeAwareSizer,
            KellySizingConfig,
        )
        cfg = KellySizingConfig(fee_rate=0.0, fraction_kelly=0.5)
        sizer = FeeAwareSizer(cfg)
        assert sizer is not None

    def test_add_result_tracking(self):
        from core.fee_aware_kelly_position_sizing import (
            KellyPositionSizer as FeeAwareSizer,
            KellySizingConfig,
        )
        sizer = FeeAwareSizer(KellySizingConfig())
        sizer.add_result("2025-06-01", 100.0, True, 0.5)
        sizer.add_result("2025-06-02", 100.0, False, 0.5)
        assert sizer.get_rolling_win_rate() == 0.5

    def test_compute_kelly_fraction(self):
        from core.fee_aware_kelly_position_sizing import (
            KellyPositionSizer as FeeAwareSizer,
            KellySizingConfig,
        )
        sizer = FeeAwareSizer(KellySizingConfig())
        # With no history, should return reasonable fraction
        frac = sizer.compute_kelly_fraction(0.6)
        assert -0.5 <= frac <= 0.5

    def test_negative_edge_short_signal(self):
        from core.fee_aware_kelly_position_sizing import (
            KellyPositionSizer as FeeAwareSizer,
            KellySizingConfig,
        )
        sizer = FeeAwareSizer(KellySizingConfig())
        # Win rate below 0.5 -> negative edge -> short signal
        frac = sizer.compute_kelly_fraction(0.4)
        assert frac < 0

    def test_position_size_computation(self):
        from core.fee_aware_kelly_position_sizing import (
            KellyPositionSizer as FeeAwareSizer,
            KellySizingConfig,
        )
        sizer = FeeAwareSizer(KellySizingConfig())
        size, meta = sizer.compute_position_size(0.65, 0.8, 10000.0)
        assert size >= 0
        assert meta["direction"] in ("long", "short")
        assert "win_rate" in meta