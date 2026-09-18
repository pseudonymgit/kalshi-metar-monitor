#!/usr/bin/env python3
"""
WS3/5 build acceptance tests — repaired signals.

Covers:
1. seasonal_regime: fallback label mapping + z-score primary path (station field)
2. eighty_two_member_ensemble: station-field fix fires on real data
3. temperature_trend: documented stub + directional exclusion
4. asos_microstructure: documented retirement from directional use
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals.dual_polarity_signal import SeasonalRegimeClassifier
from core.signals.eighty_two_member_ensemble_signal import EightyTwoMemberEnsembleSignal
from core.signals.temperature_trend_signal import TemperatureTrendSignal
from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES


class TestSeasonalRegimeFallback(unittest.TestCase):
    """seasonal_regime: fallback label mapping (R1 fix)."""

    def setUp(self):
        self.sig = SeasonalRegimeClassifier()

    def test_fallback_labels_map(self):
        """classify_regime() labels now map to directions."""
        self.assertEqual(self.sig._regime_to_direction("deep_winter"), "down")
        self.assertEqual(self.sig._regime_to_direction("transition_cold"), "down")
        self.assertEqual(self.sig._regime_to_direction("warm_season"), "up")
        self.assertEqual(self.sig._regime_to_direction("transition_warm"), "up")
        self.assertIsNone(self.sig._regime_to_direction("indeterminate"))

    def test_zscore_labels_unchanged(self):
        """z-score path labels still map correctly."""
        self.assertEqual(self.sig._regime_to_direction("extreme_cold"), "down")
        self.assertEqual(self.sig._regime_to_direction("cold"), "down")
        self.assertEqual(self.sig._regime_to_direction("neutral"), None)
        self.assertEqual(self.sig._regime_to_direction("hot"), "up")

    def test_evaluate_fallback_deep_winter(self):
        """No-station fallback: deep winter cold produces 'down'."""
        days = [
            {"date": "2026-01-01", "temp": 25.0},
            {"date": "2026-01-02", "temp": 20.0},
        ]
        result = self.sig.evaluate(1, days)
        self.assertEqual(result[0], "down")

    def test_evaluate_fallback_warm_season(self):
        """No-station fallback: summer warm produces 'up'."""
        days = [
            {"date": "2026-07-01", "temp": 95.0},
            {"date": "2026-07-02", "temp": 98.0},
        ]
        result = self.sig.evaluate(1, days)
        self.assertEqual(result[0], "up")


class TestSeasonalRegimePrimaryPath(unittest.TestCase):
    """seasonal_regime: primary z-score path works with station field."""

    def test_station_field_primary_path(self):
        """With station in days dict, primary path runs (climatology lookup)."""
        from scripts.ws5_common import load_daily_stats_batched

        md = load_daily_stats_batched(
            os.path.join(os.getcwd(), "data", "metar_backfill.db"), ["KMDW"]
        )["KMDW"]
        sig = SeasonalRegimeClassifier()
        fired = 0
        for i in range(1, min(50, len(md))):
            if sig.evaluate(i, md)[0] is not None:
                fired += 1
        # Primary path should fire on a meaningful fraction of days
        # (z-score falls outside neutral band on many days)
        self.assertGreater(fired, 5)


class TestEightyTwoMemberEnsemble(unittest.TestCase):
    """eighty_two_member_ensemble: station-field fix (WS3/5)."""

    def test_fires_with_station_field(self):
        """With station field in day dicts, signal fires on real data."""
        from scripts.ws5_common import load_daily_stats_batched

        md = load_daily_stats_batched(
            os.path.join(os.getcwd(), "data", "metar_backfill.db"), ["KMDW"]
        )["KMDW"]
        sig = EightyTwoMemberEnsembleSignal()
        fired = 0
        for i in range(1, min(50, len(md))):
            result = sig.evaluate(i, md)
            if result[0] is not None:
                fired += 1
        self.assertGreater(fired, 5)

    def test_registered(self):
        """Signal is in the registered names list."""
        self.assertIn("eighty_two_member_ensemble", REGISTERED_SIGNAL_NAMES)


class TestTemperatureTrendDisposition(unittest.TestCase):
    """temperature_trend: documented stub + directional exclusion."""

    def test_stub_behavior(self):
        """evaluate(idx, days) returns (None, 0.0) — documented design stub."""
        sig = TemperatureTrendSignal()
        days = [
            {"station": "KMDW", "date": "2026-01-01", "temp": 30.0},
            {"station": "KMDW", "date": "2026-01-02", "temp": 32.0},
        ]
        self.assertEqual(sig.evaluate(1, days), (None, 0.0))

    def test_registered_for_lane_use(self):
        """Signal remains registered (lane-only use, not directional)."""
        self.assertIn("temperature_trend", REGISTERED_SIGNAL_NAMES)

    def test_not_degenerate_but_directional_excluded(self):
        """temperature_trend is not marked degenerate; its directional
        exclusion is documented in the spec (B.2.4 RETIRE disposition),
        enforced by experiment signal subsets (S0-S10 exclude it)."""
        self.assertNotIn("temperature_trend", DEGENERATE_SIGNAL_NAMES)


class TestAsosMicrostructureDisposition(unittest.TestCase):
    """asos_microstructure: retirement from directional use."""

    def test_registered(self):
        """Still registered (lane use) — retirement is from directional
        experiment subsets, not from the registry."""
        self.assertIn("asos_microstructure", REGISTERED_SIGNAL_NAMES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
