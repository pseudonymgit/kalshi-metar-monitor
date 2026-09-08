"""Tests for cross_model_divergence signal."""
import math
import sys
from typing import Optional
import pytest

sys.path.insert(0, "prototypes/weather-engine-source")

from core.signals.cross_model_divergence_signal import CrossModelDivergenceSignal

def _make_days(n=30, start="2024-01-01"):
    """Create a list of n daily weather dicts for testing."""
    from datetime import datetime, timedelta
    base = datetime.strptime(start, "%Y-%m-%d")
    days = []
    for i in range(n):
        d = base + timedelta(days=i)
        days.append({
            "date": d.strftime("%Y-%m-%d"),
            "high": 70.0 + (i % 10) * 2.0,
            "low": 50.0 + (i % 8) * 1.5,
            "temp": 60.0 + (i % 6) * 2.0,
            "dewpoint": 45.0 + (i % 5),
            "wind_dir": 180,
            "wind_speed": 5 + (i % 3),
            "pressure": 1015.0 + (i % 4),
            "station": "KNYC",
        })
    return days


class TestCrossModelDivergenceSignal:
    """Test suite for CrossModelDivergenceSignal."""

    def test_signal_can_be_imported(self):
        """Test 1: Signal can be imported from its module."""
        sig = CrossModelDivergenceSignal()
        assert sig is not None
        assert sig.name == "cross_model_divergence"

    def test_evaluate_returns_correct_type(self):
        """Test 2: Signal returns correct type from evaluate()."""
        sig = CrossModelDivergenceSignal()
        days = _make_days(35)
        result = sig.evaluate(5, days)
        assert isinstance(result, tuple)
        assert len(result) == 2
        direction, confidence = result
        assert direction is None or direction in ("up", "down")
        assert isinstance(confidence, float)
        assert 0.0 <= confidence <= 1.0

    @pytest.mark.parametrize("idx,daylist", [
        (0, []),
        (0, [dict()]),
        (-1, [dict(), dict()]),
    ])
    def test_handles_empty_data(self, idx, daylist):
        """Test 3: Signal handles empty/invalid data gracefully."""
        sig = CrossModelDivergenceSignal()
        result = sig.evaluate(idx, daylist)
        assert result is not None
        assert isinstance(result, tuple)
        assert len(result) == 2
        direction, confidence = result
        assert direction is None or direction in ("up", "down")

    def test_handles_missing_station(self):
        """Test 4: Signal handles missing station gracefully."""
        sig = CrossModelDivergenceSignal()
        result = sig.evaluate_for_station("", "")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_handles_missing_fields(self):
        """Test 5: Signal handles missing fields gracefully."""
        sig = CrossModelDivergenceSignal()
        days = [{"date": "2024-01-01"}, {"date": "2024-01-02"}]
        result = sig.evaluate(1, days)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_evaluate_for_station_returns_valid_structure(self):
        """Test 6: Signal evaluate_for_station returns valid structure."""
        sig = CrossModelDivergenceSignal()
        result = sig.evaluate_for_station("KNYC", "2024-01-15")
        assert isinstance(result, tuple)
        assert len(result) == 2
        direction, confidence = result
        assert direction is None or direction in ("up", "down")
        assert 0.0 <= confidence <= 1.0

    def test_confidence_is_never_nan(self):
        """Test 7: Signal confidence is never NaN."""
        sig = CrossModelDivergenceSignal()
        days = _make_days(35)
        result = sig.evaluate(10, days)
        _, confidence = result
        assert not math.isnan(confidence)

    def test_min_lookback_is_consistent(self):
        """Test 8: Signal min_lookback is consistent."""
        sig = CrossModelDivergenceSignal()
        mlb = sig.min_lookback
        assert isinstance(mlb, int)
        assert mlb >= 0

    def test_constructor_takes_reasonable_arguments(self):
        """Test 9: Signal constructor takes reasonable arguments."""
        sig = CrossModelDivergenceSignal(db_path=None)
        assert sig is not None
        sig2 = CrossModelDivergenceSignal()
        assert sig2 is not None
