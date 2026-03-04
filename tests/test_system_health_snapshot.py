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


if __name__ == "__main__":
    unittest.main()
