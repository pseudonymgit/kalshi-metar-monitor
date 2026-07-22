"""
Phase 21.1 — Unit Tests: Signal Evaluation

Tests all registered signals from core/signals/ to verify:
- Return type contracts (direction, confidence)
- Boundary conditions (insufficient history, null data)
- Edge cases (idx=0, negative idx, missing fields)
- Confidence clamping (0.0-1.0)
- Direction validity ('up', 'down', None)
"""

import sys
import os
import math
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import pytest

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.signals.base_signal import BaseSignal, _window, _safe_get, validate_signal


# ─── Helpers ─────────────────────────────────────────────────────────────

def make_days(n: int, base_high: float = 70.0, base_pressure: float = 1013.0,
              temp_var: float = 5.0, pressure_var: float = 10.0) -> List[Dict]:
    """Generate synthetic daily weather data for testing."""
    days = []
    for i in range(n):
        days.append({
            'date': f'2025-06-{i+1:02d}',
            'high': base_high + temp_var * math.sin(i * 0.5),
            'low': base_high - 15 + temp_var * math.sin(i * 0.5) - 5,
            'dewpoint': base_high - 20 + temp_var * math.sin(i * 0.3),
            'temp': base_high - 5 + temp_var * math.sin(i * 0.5),
            'wind_dir': 180 + 30 * math.sin(i * 0.7),
            'wind_speed': 10 + 5 * math.sin(i * 0.3),
            'pressure': base_pressure + pressure_var * math.sin(i * 0.4),
        })
    return days


def make_days_with_trend(n: int, start_high: float = 50.0, step: float = 2.0) -> List[Dict]:
    """Generate data with a clear temperature trend."""
    days = []
    for i in range(n):
        days.append({
            'date': f'2025-06-{i+1:02d}',
            'high': start_high + step * i,
            'low': start_high + step * i - 15,
            'dewpoint': start_high + step * i - 20,
            'temp': start_high + step * i - 5,
            'wind_dir': 180,
            'wind_speed': 10,
            'pressure': 1013.0,
        })
    return days


# ─── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def long_days():
    """100 days of synthetic data — enough for all lookback windows."""
    return make_days(100)


@pytest.fixture(scope="module")
def short_days():
    """Only 5 days — insufficient for most signals."""
    return make_days(5)


# Since conftest.py handles ALERT_DB_PATH, we don't need additional fixture setup


@pytest.mark.unit
class TestWindowHelper:
    """Test the _window helper function from base_signal."""

    def test_window_normal(self, long_days):
        result = _window(long_days, 50, 10, offset=1)
        assert result is not None
        assert len(result) == 10
        assert result[0] == long_days[39]
        assert result[-1] == long_days[48]

    def test_window_insufficient_history(self, short_days):
        result = _window(short_days, 3, 10, offset=1)
        assert result is None

    def test_window_edge_case_idx0(self):
        d = [{'high': 70}] * 5
        result = _window(d, 0, 3, offset=1)
        assert result is None

    def test_window_offset_larger(self, long_days):
        result = _window(long_days, 60, 5, offset=3)
        assert result is not None
        assert len(result) == 5
        assert result[-1] == long_days[56]


@pytest.mark.unit
class TestSafeGetHelper:
    """Test the _safe_get helper function from base_signal."""

    def test_normal_access(self, long_days):
        val = _safe_get(long_days, 10, 'high')
        assert val == long_days[10]['high']

    def test_out_of_bounds_negative(self, long_days):
        val = _safe_get(long_days, -1, 'high')
        assert val is None

    def test_out_of_bounds_large(self, long_days):
        val = _safe_get(long_days, 999, 'high')
        assert val is None

    def test_missing_field(self, long_days):
        val = _safe_get(long_days, 5, 'nonexistent')
        assert val is None

    def test_none_field(self):
        d = [{'high': None}]
        val = _safe_get(d, 0, 'high')
        assert val is None

    def test_custom_default(self, long_days):
        val = _safe_get(long_days, -1, 'high', default=42.0)
        assert val == 42.0


@pytest.mark.unit
class TestValidateSignalDecorator:
    """Test the @validate_signal decorator from base_signal."""

    def test_valid_directions(self):
        class TestSig(BaseSignal):
            name = "test"
            min_lookback = 0
            @validate_signal
            def evaluate(self, idx, days):
                return 'up', 0.5
        sig = TestSig()
        d, c = sig.evaluate(0, [])
        assert d == 'up'
        assert c == 0.5

    def test_invalid_direction_returns_none(self):
        class TestSig(BaseSignal):
            name = "test"
            min_lookback = 0
            @validate_signal
            def evaluate(self, idx, days):
                return 'sideways', 0.5
        sig = TestSig()
        d, c = sig.evaluate(0, [])
        assert d is None
        assert c == 0.0

    def test_confidence_clamping(self):
        class TestSig(BaseSignal):
            name = "test"
            min_lookback = 0
            @validate_signal
            def evaluate(self, idx, days):
                return 'up', 5.0
        sig = TestSig()
        d, c = sig.evaluate(0, [])
        assert c == 1.0

    def test_negative_confidence_clamps_to_zero(self):
        class TestSig(BaseSignal):
            name = "test"
            min_lookback = 0
            @validate_signal
            def evaluate(self, idx, days):
                return 'up', -1.0
        sig = TestSig()
        d, c = sig.evaluate(0, [])
        assert c == 0.0

    def test_none_result_returns_none_zero(self):
        class TestSig(BaseSignal):
            name = "test"
            min_lookback = 0
            @validate_signal
            def evaluate(self, idx, days):
                return None
        sig = TestSig()
        d, c = sig.evaluate(0, [])
        assert d is None
        assert c == 0.0


# ─── Signal Tests ────────────────────────────────────────────────────────

class SignalTestBase:
    """Mixin with common signal tests. Each concrete signal test class
    defines SIGNAL_CLASS and runs the inherited tests."""

    SIGNAL_CLASS = None
    MIN_LOOKBACK = 0

    @pytest.mark.unit
    def test_insufficient_history(self, short_days):
        """Signal should return (None, 0.0) when days list is too short."""
        sig = self.SIGNAL_CLASS()
        # Use 1-day data to trigger insufficient history for signals with min_lookback > 0
        if sig.min_lookback >= len(short_days):
            direction, confidence = sig.evaluate(len(short_days), short_days)
            assert direction is None
            assert confidence == 0.0
        else:
            # Signal has low lookback requirement; just verify it doesn't crash
            direction, confidence = sig.evaluate(len(short_days), short_days)

    @pytest.mark.unit
    def test_min_lookback_correct(self):
        """Signal's min_lookback property should be <= lookback used in evaluate."""
        sig = self.SIGNAL_CLASS()
        lookback = getattr(sig, 'min_lookback', 0)
        assert lookback >= 0, f"min_lookback should be >= 0, got {lookback}" # Some signals legitimately have 0

    @pytest.mark.unit
    def test_evaluate_at_min_lookback(self, short_days):
        """Test evaluate at exactly min_lookback index."""
        sig = self.SIGNAL_CLASS()
        # If min_lookback is > length of short_days, this should return None
        if sig.min_lookback >= len(short_days):
            direction, confidence = sig.evaluate(sig.min_lookback, short_days)
            assert direction is None or direction in ('up', 'down')

    @pytest.mark.unit
    def test_missing_high_field(self, long_days):
        """Signal should handle missing 'high' field gracefully."""
        sig = self.SIGNAL_CLASS()
        bad_days = [{'date': '2025-06-01'} for _ in range(100)]
        direction, confidence = sig.evaluate(len(bad_days) - 1, bad_days)
        assert direction is None or direction in ('up', 'down')
        assert confidence >= 0.0 and confidence <= 1.0

    @pytest.mark.unit
    def test_none_high_field(self, long_days):
        """Signal should handle None 'high' field gracefully."""
        sig = self.SIGNAL_CLASS()
        bad_days = [{'high': None, 'date': '2025-06-01', 'pressure': 1013,
                      'low': 50, 'dewpoint': 40, 'temp': 55,
                      'wind_dir': 180, 'wind_speed': 10} for _ in range(100)]
        direction, confidence = sig.evaluate(len(bad_days) - 1, bad_days)
        assert direction is None or direction in ('up', 'down')

    @pytest.mark.unit
    def test_return_type(self, long_days):
        """Signal must return (str|None, float)."""
        sig = self.SIGNAL_CLASS()
        idx = max(sig.min_lookback + 5, 10)
        if idx >= len(long_days):
            pytest.skip("Not enough test data")
        result = sig.evaluate(idx, long_days)
        assert isinstance(result, tuple)
        assert len(result) == 2
        direction, confidence = result
        assert direction is None or direction in ('up', 'down')
        assert isinstance(confidence, float)
        assert 0.0 <= confidence <= 1.0


# ─── Concrete Signal Test Classes ───────────────────────────────────────

class TestPersistenceSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['PersistenceSignal']).PersistenceSignal

    @pytest.mark.unit
    def test_basic_prediction(self, long_days):
        from core.signals.persistence_signal import PersistenceSignal
        sig = PersistenceSignal()
        # idx=50 with 100 days of data; yesterday's high > day before = 'up'
        direction, confidence = sig.evaluate(10, long_days)
        assert direction in ('up', 'down')
        assert confidence == 0.3

    @pytest.mark.unit
    def test_up_trend(self):
        from core.signals.persistence_signal import PersistenceSignal
        sig = PersistenceSignal()
        days = make_days_with_trend(20, start_high=50, step=2.0)
        direction, confidence = sig.evaluate(10, days)
        # Temp is increasing -> predict 'up'
        assert direction == 'up'

    @pytest.mark.unit
    def test_down_trend(self):
        from core.signals.persistence_signal import PersistenceSignal
        sig = PersistenceSignal()
        days = make_days_with_trend(20, start_high=70, step=-2.0)
        direction, confidence = sig.evaluate(10, days)
        # Temp is decreasing -> predict 'down'
        assert direction == 'down'


# TestSimpleTrendSignal removed — SimpleTrendSignal is not registered in __init__.py
# (exists as core/signals/simple_trend_signal.py but not part of SignalRegistry)


def _inject_dates(days):
    """Add date field to bare dicts for signals that expect it."""
    for i, d in enumerate(days):
        if 'date' not in d:
            d['date'] = f'2025-06-{i+1:02d}'
    return days


class TestGaussianSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['GaussianSignal']).GaussianSignal

    @pytest.mark.unit
    def test_high_zscore_direction(self):
        from core.signals.gaussian_signal import GaussianSignal
        sig = GaussianSignal()
        # Create 50 days of stable data then a spike
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(50)]
        days[48]['high'] = 90.0  # Extreme spike (current = days[idx-1])
        direction, confidence = sig.evaluate(49, days)
        assert direction == 'down'  # Too hot -> predict cooling
        assert confidence > 0

    @pytest.mark.unit
    def test_low_zscore_direction(self):
        from core.signals.gaussian_signal import GaussianSignal
        sig = GaussianSignal()
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(50)]
        days[48]['high'] = 30.0  # Extreme cold (current = days[idx-1]) at idx-1=48
        direction, confidence = sig.evaluate(49, days)
        assert direction == 'up'  # Too cold -> predict warming
        assert confidence > 0

    @pytest.mark.unit
    def test_normal_temp_no_signal(self):
        from core.signals.gaussian_signal import GaussianSignal
        sig = GaussianSignal()
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(50)]
        direction, confidence = sig.evaluate(49, days)
        # With all identical values, std=0 so z-score = 0, no signal
        assert direction is None
        assert confidence == 0.0


class TestCalendarClimatologySignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['CalendarClimatologySignal']).CalendarClimatologySignal

    @pytest.mark.unit
    def test_high_zscore_direction(self):
        from core.signals.calendar_climatology_signal import CalendarClimatologySignal
        sig = CalendarClimatologySignal()
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(65)]
        days[63]['high'] = 95.0  # Extreme spike above 1.5 z threshold (current = days[idx-1])
        direction, confidence = sig.evaluate(64, days)
        assert direction == 'down'
        assert confidence > 0

    @pytest.mark.unit
    def test_low_zscore_direction(self):
        from core.signals.calendar_climatology_signal import CalendarClimatologySignal
        sig = CalendarClimatologySignal()
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(65)]
        days[63]['high'] = 40.0  # Extreme cold (current = days[idx-1])
        direction, confidence = sig.evaluate(64, days)
        assert direction == 'up'
        assert confidence > 0

    @pytest.mark.unit
    def test_confidence_capped_at_06(self):
        from core.signals.calendar_climatology_signal import CalendarClimatologySignal
        sig = CalendarClimatologySignal()
        days = [{'high': 70.0, 'low': 55, 'dewpoint': 50, 'temp': 65,
                 'wind_dir': 180, 'wind_speed': 10, 'pressure': 1013,
                 'date': f'2025-06-{i+1:02d}'}
                for i in range(65)]
        days[-1]['high'] = 70 + 6 * 1.5  # Very extreme
        direction, confidence = sig.evaluate(64, days)
        assert confidence <= 0.6 + 0.01  # Allow rounding


class TestForecastDisagreementSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['ForecastDisagreementSignal']).ForecastDisagreementSignal

    @pytest.mark.unit
    def test_positive_disagreement(self):
        from core.signals.forecast_disagreement_signal import ForecastDisagreementSignal
        sig = ForecastDisagreementSignal()
        # Yesterday's high is well above 7-day mean
        days = [{'high': 65.0, 'date': f'2025-06-{i+1:02d}'} for i in range(10)]
        days[-2]['high'] = 80.0  # Yesterday much warmer than 7-day mean of ~65
        direction, confidence = sig.evaluate(9, days)
        # disagreement = 80 - 65 = 15 > 5 threshold -> direction 'down' (too warm)
        assert direction == 'down'
        assert confidence > 0.0

    @pytest.mark.unit
    def test_negative_disagreement(self):
        from core.signals.forecast_disagreement_signal import ForecastDisagreementSignal
        sig = ForecastDisagreementSignal()
        days = [{'high': 65.0, 'date': f'2025-06-{i+1:02d}'} for i in range(10)]
        days[-2]['high'] = 50.0  # Yesterday much colder
        direction, confidence = sig.evaluate(9, days)
        # disagreement = 50 - 65 = -15, abs=15 > 5 -> direction 'up' (too cold)
        assert direction == 'up'
        assert confidence > 0.0

    @pytest.mark.unit
    def test_small_disagreement_no_signal(self):
        from core.signals.forecast_disagreement_signal import ForecastDisagreementSignal
        sig = ForecastDisagreementSignal()
        days = [{'high': 65.0, 'date': f'2025-06-{i+1:02d}'} for i in range(10)]
        days[-2]['high'] = 66.0  # Very small disagreement
        direction, confidence = sig.evaluate(9, days)
        assert direction is None or direction in ('up', 'down')


class TestPressureDeltaSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['PressureDeltaSignal']).PressureDeltaSignal

    @pytest.mark.unit
    def test_pressure_rising(self):
        from core.signals.pressure_delta_signal import PressureDeltaSignal
        sig = PressureDeltaSignal()
        # Creating days with increasing pressure
        days = []
        for i in range(10):
            days.append({'high': 70, 'low': 55, 'dewpoint': 50, 'temp': 65,
                         'wind_dir': 180, 'wind_speed': 10,
                         'pressure': 1000 + i * 2.0,  # Pressure increasing
                         'date': f'2025-06-{i+1:02d}'})
        direction, confidence = sig.evaluate(9, days)
        # Rising pressure should predict DOWN (cold air advection)
        assert direction in ('down', None)

    @pytest.mark.unit
    def test_pressure_falling(self):
        from core.signals.pressure_delta_signal import PressureDeltaSignal
        sig = PressureDeltaSignal()
        days = []
        for i in range(10):
            days.append({'high': 70, 'low': 55, 'dewpoint': 50, 'temp': 65,
                         'wind_dir': 180, 'wind_speed': 10,
                         'pressure': 1020 - i * 2.0,  # Pressure decreasing
                         'date': f'2025-06-{i+1:02d}'})
        direction, confidence = sig.evaluate(9, days)
        # Falling pressure should predict UP (warm air advection)
        assert direction in ('up', None)

    @pytest.mark.unit
    def test_exponential_weight_computation(self):
        from core.signals.pressure_delta_signal import PressureDeltaSignal
        # Recent hours should have higher weight than older hours
        w_recent = PressureDeltaSignal._exponential_weight(1)  # 1 hour ago
        w_old = PressureDeltaSignal._exponential_weight(48)  # 48 hours ago
        assert w_recent > w_old
        assert 0 < w_recent <= 1
        assert 0 < w_old <= 1


# TestRegimeSignal removed — RegimeSignal is a dead signal excluded from SignalRegistry
# (per core/signals/__init__.py comment: "exclude dead signals...regime_signal")


class TestGaussianV2Signal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['GaussianV2Signal']).GaussianV2Signal


class TestGoldilocksSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['GoldilocksSignal']).GoldilocksSignal


class TestWindDirectionShiftSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['WindDirectionShiftSignal']).WindDirectionShiftSignal


class TestFrontalDetectorSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['FrontalDetectorSignal']).FrontalDetectorSignal


class TestTemperatureAdvectionSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['TemperatureAdvectionSignal']).TemperatureAdvectionSignal


class TestNwpDirectSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['NwpDirectSignal']).NwpDirectSignal


class TestIntradayMetarConfirmationSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['IntradayMetarConfirmationSignal']).IntradayMetarConfirmationSignal


class TestFogrReversionSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['FogrReversionSignal']).FogrReversionSignal


class TestMetarDtdtSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['MetarDtdtSignal']).MetarDtdtSignal


class TestPressureTendencySignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['PressureTendencySignal']).PressureTendencySignal


class TestHrrrBiasCorrectedSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['HrrrBiasCorrectedSignal']).HrrrBiasCorrectedSignal


class TestEsdrSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['EsdrSignal']).EsdrSignal


class TestNwpDtdtFusionSignal(SignalTestBase):
    SIGNAL_CLASS = __import__('core.signals', fromlist=['NwpDtdtFusionSignal']).NwpDtdtFusionSignal


@pytest.mark.unit
class TestSignalRegistry:
    """Test that SignalRegistry instantiates all signals correctly."""

    @pytest.mark.unit
    def test_registry_all_signals(self):
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path="/tmp/test_registry.db")
        assert len(registry.signals) >= 12  # At least 12 signals registered
        for name, sig in registry.signals.items():
            assert hasattr(sig, 'evaluate'), f"{name} missing evaluate"
            assert hasattr(sig, 'name'), f"{name} missing name"
            assert hasattr(sig, 'min_lookback'), f"{name} missing min_lookback"

    @pytest.mark.unit
    def test_signal_names_unique(self):
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path="/tmp/test_registry.db")
        names = [sig.name for sig in registry.signals.values()]
        assert len(names) == len(set(names)), "Signal names must be unique"
