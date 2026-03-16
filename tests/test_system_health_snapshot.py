import unittest
from unittest.mock import patch

import app as app_module


class SystemHealthSnapshotTests(unittest.TestCase):
    @patch("app.datetime")
    def test_scheduler_not_running_blocks_ingestion_health(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            ingestion_snapshot={
                "scheduler_running": False,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {}},
            hydration_execution_snapshot={},
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["ingestion"]["status"], "BLOCKED")
        self.assertEqual(snapshot["ingestion"]["reason"], "scheduler_not_running")

    @patch("app.datetime")
    def test_hydration_cache_missing_blocks_hydration_health(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            station="KDEN",
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {"KDEN": {}}},
            hydration_execution_snapshot={
                "KDEN": {
                    "station": "KDEN",
                    "cache_written": False,
                    "evaluated_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["hydration"]["status"], "BLOCKED")
        self.assertEqual(snapshot["hydration"]["reason"], "hydration_cache_not_written")

    @patch("app.get_latest_station_market_evaluation_context", return_value={})
    @patch("app.datetime")
    def test_transitions_without_evaluation_block_evaluation_health(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            station="KDEN",
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {"KDEN": {}}},
            hydration_execution_snapshot={
                "KDEN": {
                    "station": "KDEN",
                    "cache_written": True,
                    "evaluated_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
            transitions=[{"station": "KDEN", "timestamp_utc": "2026-01-01T00:00:00+00:00"}],
            alerts=[],
        )

        self.assertEqual(snapshot["evaluation"]["status"], "BLOCKED")
        self.assertEqual(snapshot["evaluation"]["reason"], "evaluation_not_executed")

    @patch("app.datetime")
    def test_hydration_cache_valid_regression_degrades_health(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            station="KDEN",
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={
                "stations": {
                    "KDEN": {
                        "hydration_prerequisite": {
                            "cache_valid": False,
                            "series_discovered": True,
                        }
                    }
                }
            },
            hydration_execution_snapshot={
                "KDEN": {
                    "station": "KDEN",
                    "cache_written": True,
                    "evaluated_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["hydration"]["status"], "DEGRADED")
        self.assertEqual(snapshot["hydration"]["reason"], "ladder_cache_invalid")

    @patch("app.datetime")
    def test_hydration_series_discovered_regression_degrades_health(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            station="KDEN",
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={
                "stations": {
                    "KDEN": {
                        "hydration_prerequisite": {
                            "cache_valid": True,
                            "series_discovered": False,
                        }
                    }
                }
            },
            hydration_execution_snapshot={
                "KDEN": {
                    "station": "KDEN",
                    "cache_written": True,
                    "evaluated_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["hydration"]["status"], "DEGRADED")
        self.assertEqual(snapshot["hydration"]["reason"], "series_discovery_missing")

    @patch("app.datetime")
    def test_hydration_cache_not_written_precedence_over_degraded_signals(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            station="KDEN",
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={
                "stations": {
                    "KDEN": {
                        "hydration_prerequisite": {
                            "cache_valid": False,
                            "series_discovered": False,
                        }
                    }
                }
            },
            hydration_execution_snapshot={
                "KDEN": {
                    "station": "KDEN",
                    "cache_written": False,
                    "evaluated_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["hydration"]["status"], "BLOCKED")
        self.assertEqual(snapshot["hydration"]["reason"], "hydration_cache_not_written")

    @patch("app.datetime")
    def test_evaluation_suppression_breakdown_counts_reason_totals(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {}},
            hydration_execution_snapshot={},
            transitions=[
                {"alert_classification": "MARKET_SUPPRESSED", "suppression_reason": "directional_strike_rejected"},
                {"alert_classification": "MARKET_SUPPRESSED", "suppression_reason": "DIRECTIONAL_STRIKE_REJECTED"},
                {"alert_classification": "MARKET_SUPPRESSED", "suppression_reason": "expired_market"},
                {"alert_classification": "ALERT_SENT", "suppression_reason": "directional_strike_rejected"},
            ],
            alerts=[],
        )

        self.assertEqual(
            snapshot["evaluation"]["suppression_breakdown"],
            {
                "directional_strike_rejected": 2,
                "expired_market": 1,
            },
        )

    @patch("app.datetime")
    def test_evaluation_suppression_breakdown_uses_unknown_reason_bucket(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {}},
            hydration_execution_snapshot={},
            transitions=[
                {"alert_classification": "MARKET_SUPPRESSED"},
                {"alert_classification": "MARKET_SUPPRESSED", "suppression_reason": ""},
            ],
            alerts=[],
        )

        self.assertEqual(snapshot["evaluation"]["suppression_breakdown"], {"unknown_reason": 2})

    @patch("app.datetime")
    def test_evaluation_suppression_breakdown_empty_when_no_transitions(self, mock_datetime):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        snapshot = app_module.compute_system_health_snapshot(
            ingestion_snapshot={
                "scheduler_running": True,
                "last_poll_utc": "2026-01-01T00:00:00+00:00",
                "stale_after_seconds": 180,
                "stations": [{"station": "KDEN", "freshness_lag_seconds": 10}],
            },
            hydration_snapshot={"stations": {}},
            hydration_execution_snapshot={},
            transitions=[],
            alerts=[],
        )

        self.assertEqual(snapshot["evaluation"]["suppression_breakdown"], {})


if __name__ == "__main__":
    unittest.main()
