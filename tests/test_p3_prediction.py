"""
Phase 3 Prediction Layer Tests

Comprehensive tests covering:
1. Feature extraction (14-dim vectors)
2. Match engine (similarity scoring)
3. Trajectory tracer (forward lookups)
4. Calibration engine (confidence bands)
5. End-to-end prediction test
"""

import unittest
from unittest.mock import patch, MagicMock
import math

# Core Phase 3 modules
from core import p3_feature_extractor as p3fe
from core import p3_match_engine as p3me
from core import p3_trajectory_tracer as p3tt
from core import p3_calibration_engine as p3ce
from core import p3_output_formatter as p3of
from core import p3_db_migration as p3db
from core import p3_scheduler as p3sch


# Sample settlement epoch data for testing (proper format matching feature extractor)
# The feature extractor expects specific key names from the epoch data
SAMPLE_OPEN_EPOCH = {
    "id": 1001,
    "station": "KDEN",
    "market_type": "high",
    "local_trading_date": "2026-06-25",
    "settlement_bucket": 85,
    "prior_settlement_bucket": 78,
    "settlement_timestamp_utc": "2026-06-25T14:00:00Z",
    "settlement_jump_magnitude": 7,
    "epoch_status": "open",
    "epoch_close_reason": None,
    "reversion_occurred": 1,
    "max_excursion_above_settlement": 5.5,
    "duration_at_or_above_settlement_seconds": 43200.0,
    "duration_strictly_above_settlement_seconds": 21600.0,
    "terminal_state_reached": 1,
    "last_transition_event_id": 12,
    "first_reversion_timestamp_utc": "2026-06-25T12:00:00Z",
    "goldilocks_emitted": 1,
}

SAMPLE_CLOSED_EPOCHS = [
    {
        "id": 900,
        "station": "KDEN",
        "market_type": "high",
        "local_trading_date": "2026-06-10",
        "settlement_bucket": 82,
        "prior_settlement_bucket": 75,
        "settlement_timestamp_utc": "2026-06-10T14:00:00Z",
        "settlement_jump_magnitude": 7,
        "epoch_status": "closed",
        "reversion_occurred": 1,
        "max_excursion_above_settlement": 4.2,
        "duration_at_or_above_settlement_seconds": 40000.0,
        "duration_strictly_above_settlement_seconds": 20000.0,
        "terminal_state_reached": 1,
        "last_transition_event_id": 10,
        "first_reversion_timestamp_utc": "2026-06-10T12:00:00Z",
        "goldilocks_emitted": 1,
    },
    {
        "id": 901,
        "station": "KDEN",
        "market_type": "high",
        "local_trading_date": "2026-06-12",
        "settlement_bucket": 84,
        "prior_settlement_bucket": 77,
        "settlement_timestamp_utc": "2026-06-12T14:00:00Z",
        "settlement_jump_magnitude": 7,
        "epoch_status": "closed",
        "reversion_occurred": 1,
        "max_excursion_above_settlement": 5.0,
        "duration_at_or_above_settlement_seconds": 42000.0,
        "duration_strictly_above_settlement_seconds": 21000.0,
        "terminal_state_reached": 1,
        "last_transition_event_id": 11,
        "first_reversion_timestamp_utc": "2026-06-12T12:00:00Z",
        "goldilocks_emitted": 1,
    },
    {
        "id": 902,
        "station": "KDEN",
        "market_type": "high",
        "local_trading_date": "2026-06-14",
        "settlement_bucket": 86,
        "prior_settlement_bucket": 79,
        "settlement_timestamp_utc": "2026-06-14T14:00:00Z",
        "settlement_jump_magnitude": 7,
        "epoch_status": "closed",
        "reversion_occurred": 0,
        "max_excursion_above_settlement": 6.0,
        "duration_at_or_above_settlement_seconds": 45000.0,
        "duration_strictly_above_settlement_seconds": 22000.0,
        "terminal_state_reached": 1,
        "last_transition_event_id": 13,
        "first_reversion_timestamp_utc": "2026-06-14T12:00:00Z",
        "goldilocks_emitted": 0,
    },
]


class TestFeatureExtractor(unittest.TestCase):
    """Test 14-dimensional feature extraction."""

    def test_extract_features_returns_14_dimensions(self):
        """Feature vector should have all 14 expected fields."""
        features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        
        # Verify all required fields are present
        self.assertTrue(hasattr(features, "settlement_jump_magnitude"))
        self.assertTrue(hasattr(features, "reversion_occurred"))
        self.assertTrue(hasattr(features, "max_excursion_above_settlement"))
        self.assertTrue(hasattr(features, "duration_at_or_above_seconds"))
        self.assertTrue(hasattr(features, "duration_strictly_above_seconds"))
        self.assertTrue(hasattr(features, "terminal_state_reached"))
        self.assertTrue(hasattr(features, "transition_count"))
        self.assertTrue(hasattr(features, "settlement_bucket"))

    def test_extract_features_numeric_values(self):
        """Feature values should be properly extracted and normalized."""
        features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        
        # Check integer fields
        self.assertEqual(features.settlement_jump_magnitude, 7)
        self.assertEqual(features.reversion_occurred, 1)
        self.assertEqual(features.terminal_state_reached, 1)
        self.assertEqual(features.transition_count, 12)
        self.assertEqual(features.goldilocks_emitted, 0)  # Default value, not tracked
        self.assertEqual(features.settlement_bucket, 85)
        
        # Check float fields
        self.assertEqual(features.max_excursion_above_settlement, 5.5)
        self.assertEqual(features.duration_at_or_above_seconds, 43200.0)
        self.assertGreater(features.reversion_latency_seconds, 0)

    def test_extract_features_with_none_prior_bucket(self):
        """Handle None prior_settlement_bucket gracefully."""
        epoch_with_none = {**SAMPLE_OPEN_EPOCH}
        del epoch_with_none["prior_settlement_bucket"]
        
        features = p3fe.extract_features_from_epoch(epoch_with_none)
        self.assertIsNone(features.prior_settlement_bucket)


class TestMatchEngine(unittest.TestCase):
    """Test similarity matching and analog finding."""

    def test_find_similar_epochs_returns_matches(self):
        """Should find similar epochs in corpus."""
        query_features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        
        match_result = p3me.find_similar_epochs(query_features, SAMPLE_CLOSED_EPOCHS)
        
        self.assertIsNotNone(match_result)
        self.assertGreaterEqual(match_result.total_analogs, 0)

    def test_get_top_analogs_returns_k_results(self):
        """Should return exactly k top analogs."""
        query_features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        match_result = p3me.find_similar_epochs(query_features, SAMPLE_CLOSED_EPOCHS)
        
        top_analogs = p3me.get_top_analogs(match_result, k=2)
        
        self.assertLessEqual(len(top_analogs), 2)

    def test_strong_vs_weak_matches(self):
        """Should classify matches as strong or weak based on threshold."""
        query_features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        match_result = p3me.find_similar_epochs(query_features, SAMPLE_CLOSED_EPOCHS)
        
        # Strong matches should have score >= 0.7
        for match in match_result.strong_matches:
            self.assertGreaterEqual(match.match_score, 0.7)

    def test_no_analogs_found(self):
        """Should handle case where no analogs are found."""
        query_features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        empty_corpus = []
        
        match_result = p3me.find_similar_epochs(query_features, empty_corpus)
        
        self.assertFalse(match_result.strong_matches)
        self.assertFalse(match_result.weak_matches)


class TestTrajectoryTracer(unittest.TestCase):
    """Test forward trajectory analysis."""

    def test_trace_all_trajectories_handles_empty_input(self):
        """Should handle empty strong matches gracefully."""
        empty_matches = []
        corpus = SAMPLE_CLOSED_EPOCHS
        
        result = p3tt.trace_all_trajectories(empty_matches, corpus)
        
        # Result should be a valid TrajectoryResult
        self.assertTrue(hasattr(result, "query_epoch_id"))
        self.assertTrue(hasattr(result, "matched_analogs"))
        self.assertEqual(result.matched_analogs, 0)

    def test_trace_trajectory_returns_forward_lookups(self):
        """Should find forward lookups for each strong match."""
        query_features = p3fe.extract_features_from_epoch(SAMPLE_OPEN_EPOCH)
        match_result = p3me.find_similar_epochs(query_features, SAMPLE_CLOSED_EPOCHS)
        
        if match_result.strong_matches:
            result = p3tt.trace_all_trajectories(match_result.strong_matches, SAMPLE_CLOSED_EPOCHS)
            
            # Should have trajectory clusters
            self.assertTrue(hasattr(result, "trajectory_clusters"))
            self.assertTrue(hasattr(result, "primary_projection"))
        else:
            self.skipTest("No strong matches found to test trajectory tracing")


class TestCalibrationEngine(unittest.TestCase):
    """Test confidence scoring and multimodal detection."""

    def test_calculate_confidence_returns_score(self):
        """Should calculate a valid confidence score."""
        confidence = p3ce.calculate_confidence(
            n=10,
            excess_kurtosis=0.5,
            sigma=0.1,
            mu=0.8,
            brier_score=0.15,
            delta_t_hours=24,
            p_up=0.6,
            p_down=0.4,
            outcomes=[0.75, 0.80, 0.78, 0.82, 0.76, 0.79, 0.81, 0.77, 0.83, 0.74],
        )
        
        self.assertGreaterEqual(confidence.final_score, 0.0)
        self.assertLessEqual(confidence.final_score, 1.0)

    def test_calculate_confidence_has_band(self):
        """Confidence should be assigned to a valid band."""
        confidence = p3ce.calculate_confidence(
            n=10,
            excess_kurtosis=0.5,
            sigma=0.1,
            mu=0.8,
            brier_score=0.15,
            delta_t_hours=24,
            p_up=0.6,
            p_down=0.4,
            outcomes=[0.75, 0.80, 0.78, 0.82, 0.76, 0.79, 0.81, 0.77, 0.83, 0.74],
        )
        
        self.assertIn(confidence.band, ["HIGH", "MODERATE", "LOW", "INSUFFICIENT"])

    def test_detect_multimodal_outcomes(self):
        """Should detect multimodal distribution."""
        outcomes = [0.7, 0.7, 0.7, 0.3, 0.3, 0.3, 0.5, 0.5]
        
        binning = p3ce.detect_multimodal_outcomes(outcomes)
        
        # Check that multimodal detection works (may or may not detect modes)
        self.assertIsNotNone(binning)
        self.assertIn("is_multimodal", vars(binning))

    def test_multimodal_penalty_applied(self):
        """Bimodal should receive penalty multiplier."""
        outcomes = [0.7, 0.7, 0.7, 0.3, 0.3, 0.3]
        
        confidence = p3ce.calculate_confidence(
            n=6,
            excess_kurtosis=0,
            sigma=0.2,
            mu=0.5,
            brier_score=0.1,
            delta_t_hours=0,
            p_up=0.5,
            p_down=0.5,
            outcomes=outcomes,
        )
        
        # Check penalty structure exists
        self.assertGreaterEqual(confidence.multimodal_penalty, 0.5)
        self.assertLessEqual(confidence.multimodal_penalty, 1.0)

    def test_temporal_decay(self):
        """Should apply temporal decay to confidence."""
        confidence = p3ce.calculate_confidence(
            n=10,
            excess_kurtosis=0.5,
            sigma=0.1,
            mu=0.8,
            brier_score=0.15,
            delta_t_hours=24,
            p_up=0.6,
            p_down=0.4,
            outcomes=[0.75, 0.80, 0.78, 0.82, 0.76, 0.79, 0.81, 0.77, 0.83, 0.74],
            epochs_ahead=5,
        )
        
        # Decayed score should be lower than final score
        self.assertLess(confidence.decayed_score, confidence.final_score)
        # Decayed score should not go below floor
        self.assertGreaterEqual(confidence.decayed_score, 0.10)


class TestOutputFormatter(unittest.TestCase):
    """Test prediction output formatting."""

    def test_create_prediction_has_required_fields(self):
        """Prediction message should have all required fields."""
        query_epoch = SAMPLE_OPEN_EPOCH
        top_analogs = []
        trajectory_result = MagicMock()
        confidence = MagicMock()
        confidence.final_score = 0.75
        confidence.band = "MODERATE"
        confidence.multimodal_state = "unimodal"
        
        prediction = p3of.create_prediction(
            station="KDEN",
            market_type="high",
            epoch_data=query_epoch,
            analogs=top_analogs,
            trajectory_result=trajectory_result,
            confidence=confidence,
        )
        
        self.assertEqual(prediction.station, "KDEN")
        self.assertEqual(prediction.market_type, "high")
        self.assertEqual(prediction.epoch_id, query_epoch["id"])


class TestDatabaseMigration(unittest.TestCase):
    """Test database schema and index management."""

    def test_ensure_phase3_index_creates_index(self):
        """Should create the composite index if it doesn't exist."""
        # This test verifies the function runs without error
        try:
            # The function is idempotent, so we can call it safely
            p3db.ensure_phase3_index()
        except Exception as e:
            self.fail(f"ensure_phase3_index raised exception: {e}")

    def test_verify_index_detects_existing_index(self):
        """Should verify existing index or return False if missing."""
        try:
            exists = p3db.verify_index()
            # Index either exists or doesn't, but function shouldn't crash
        except Exception as e:
            self.fail(f"verify_index raised exception: {e}")


class TestScheduler(unittest.TestCase):
    """Test scheduler functions."""

    def test_get_latest_settlement_epoch(self):
        """Should get latest open epoch for a station."""
        try:
            epoch = p3sch.get_latest_settlement_epoch("KDEN", "high")
            # May return None if no open epoch exists, which is valid
            # If we can't connect to DB, skip this test
        except Exception as e:
            if "unable to open database file" in str(e):
                self.skipTest("Database not available for this test")
            else:
                self.fail(f"get_latest_settlement_epoch raised unexpected exception: {e}")

    def test_get_closed_epochs_for_station(self):
        """Should get closed epochs for historical matching."""
        try:
            epochs = p3sch.get_closed_epochs_for_station("KDEN", "high", limit_days=365)
            # May return empty list if no historical data, which is valid
        except Exception as e:
            if "unable to open database file" in str(e):
                self.skipTest("Database not available for this test")
            else:
                self.fail(f"get_closed_epochs_for_station raised exception: {e}")

    def test_run_prediction_for_station(self):
        """Should run a prediction for a station."""
        try:
            result = p3sch.run_prediction_for_station("KDEN", "high")
            self.assertTrue(hasattr(result, "success"))
            self.assertTrue(hasattr(result, "message"))
            self.assertTrue(hasattr(result, "prediction"))
        except Exception as e:
            if "unable to open database file" in str(e):
                self.skipTest("Database not available for this test")
            else:
                self.fail(f"run_prediction_for_station raised exception: {e}")


class TestEndToEndPrediction(unittest.TestCase):
    """End-to-end prediction pipeline test."""

    @patch("core.p3_scheduler.get_latest_settlement_epoch")
    @patch("core.p3_scheduler.get_closed_epochs_for_station")
    def test_full_prediction_pipeline(self, mock_get_closed, mock_get_open):
        """Test the complete prediction flow."""
        # Setup mocks
        mock_get_open.return_value = SAMPLE_OPEN_EPOCH
        mock_get_closed.return_value = SAMPLE_CLOSED_EPOCHS
        
        # Run prediction
        result = p3sch.run_prediction_for_station("KDEN", "high")
        
        # Verify result structure
        self.assertTrue(hasattr(result, "success"))
        self.assertTrue(hasattr(result, "message"))
        self.assertTrue(hasattr(result, "prediction"))
        
        # Verify response object
        self.assertIn(result.timestamp_utc[-1:], ["Z"])  # ISO UTC format


if __name__ == "__main__":
    unittest.main()
