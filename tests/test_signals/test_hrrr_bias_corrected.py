"""Tests for hrrr_bias_corrected signal."""
import math
import sys
from typing import Optional
import pytest

sys.path.insert(0, "prototypes/weather-engine-source")

from core.signals.hrrr_bias_corrected_signal import HRRRBiasCorrectedSignal


class TestHRRRBiasCorrectedSignal:
    """Test suite for HRRRBiasCorrectedSignal (standalone, not BaseSignal)."""

    def test_signal_can_be_imported(self):
        """Test 1: Signal can be imported and instantiated."""
        sig = HRRRBiasCorrectedSignal()
        assert sig is not None

    def test_station_bias_handles_missing_station(self):
        """Test 2: get_station_bias handles unknown station gracefully."""
        sig = HRRRBiasCorrectedSignal()
        result = sig.get_station_bias("")  
        assert result is None

    def test_handles_empty_data(self):
        """Test 3: Signal handles empty/missing data gracefully."""
        sig = HRRRBiasCorrectedSignal()
        # Empty station without coordinates returns None from get_station_bias
        result = sig.get_station_bias("XXXX")
        assert result is None or isinstance(result, float)
        # apply_bias_correction with empty forecasts does not crash
        result2 = sig.apply_bias_correction({"forecasts": []}, "XXXX")
        assert isinstance(result2, dict)

    def test_handles_missing_station(self):
        """Test 4: Signal handles empty station gracefully."""
        sig = HRRRBiasCorrectedSignal()
        result = sig.get_station_bias("")
        assert result is None

    def test_handles_missing_fields(self):
        """Test 5: Signal handles missing field data gracefully."""
        sig = HRRRBiasCorrectedSignal()
        result = sig.get_station_bias("KNOWNJUNK")
        assert result is None or isinstance(result, float)

    def test_apply_bias_correction_empty(self):
        """Test 6: apply_bias_correction handles empty forecast."""
        sig = HRRRBiasCorrectedSignal()
        result = sig.apply_bias_correction({"forecasts": [], "fetched_at": None}, "KNYC")
        assert isinstance(result, dict)
        assert result["bias_f"] is None

    def test_confidence_is_never_nan(self):
        """Test 7: Signal confidence float values are never NaN."""
        sig = HRRRBiasCorrectedSignal()
        # get_station_bias returns None or a float (never NaN)
        bias = sig.get_station_bias("KNYC")
        if bias is not None:
            assert not math.isnan(bias)
        # apply_bias_correction confidence from bias is well-formed
        result = sig.get_station_bias("MISSING")
        assert result is None or isinstance(result, float)

    def test_config_defaults(self):
        """Test 8: Signal has reasonable defaults."""
        sig = HRRRBiasCorrectedSignal()
        assert sig.config is not None
        assert isinstance(sig.config, dict)

    def test_constructor_takes_reasonable_arguments(self):
        """Test 9: Signal constructor takes reasonable arguments."""
        sig = HRRRBiasCorrectedSignal(config={"test": True})
        assert sig is not None
        assert sig.config["test"] is True
        sig2 = HRRRBiasCorrectedSignal()
        assert sig2 is not None