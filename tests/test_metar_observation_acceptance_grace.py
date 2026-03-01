import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import metar_monitor


class MetarObservationAcceptanceGraceTests(unittest.TestCase):
    def setUp(self):
        with metar_monitor._STATE_LOCK:
            metar_monitor._STATE["last_seen_iso"].pop("KNYC", None)
            metar_monitor._STATE["last_obs"].pop("KNYC", None)

    @patch("core.metar_monitor._process_temperature_event", return_value=0)
    @patch("core.metar_monitor.set_latest_observation")
    def test_observation_inside_normal_window_accepted(self, mock_set_latest, _mock_process):
        ingested, _alerts = metar_monitor._ingest_obs(
            "KNYC",
            [{"obs_time": "2026-01-01T10:01:00+00:00", "temp_f": 70.0}],
            {"cache_file": "/tmp/unused"},
            allow_alert_delivery=False,
            persist_cache=False,
            window_start=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(ingested, 1)
        mock_set_latest.assert_called_once()

    @patch("core.metar_monitor._ALERT_LOGGER.debug")
    @patch("core.metar_monitor._process_temperature_event", return_value=0)
    @patch("core.metar_monitor.set_latest_observation")
    def test_observation_inside_grace_accepted(self, mock_set_latest, _mock_process, mock_debug):
        ingested, _alerts = metar_monitor._ingest_obs(
            "KNYC",
            [{"obs_time": "2026-01-01T09:50:00+00:00", "temp_f": 70.0}],
            {"cache_file": "/tmp/unused"},
            allow_alert_delivery=True,
            persist_cache=True,
            window_start=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(ingested, 1)
        mock_set_latest.assert_called_once()
        mock_debug.assert_called_once_with("accepted_with_grace station=KNYC lag_seconds=600")

    @patch("core.metar_monitor._process_temperature_event", return_value=0)
    @patch("core.metar_monitor.set_latest_observation")
    def test_observation_beyond_grace_rejected(self, mock_set_latest, _mock_process):
        ingested, _alerts = metar_monitor._ingest_obs(
            "KNYC",
            [{"obs_time": "2026-01-01T09:44:59+00:00", "temp_f": 70.0}],
            {"cache_file": "/tmp/unused"},
            allow_alert_delivery=False,
            persist_cache=False,
            window_start=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(ingested, 0)
        mock_set_latest.assert_not_called()

    @patch("core.metar_monitor._process_temperature_event", return_value=0)
    @patch("core.metar_monitor.set_latest_observation")
    def test_future_observation_rejected(self, mock_set_latest, _mock_process):
        ingested, _alerts = metar_monitor._ingest_obs(
            "KNYC",
            [{"obs_time": "2026-01-01T10:05:01+00:00", "temp_f": 70.0}],
            {"cache_file": "/tmp/unused"},
            allow_alert_delivery=False,
            persist_cache=False,
            window_start=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(ingested, 0)
        mock_set_latest.assert_not_called()

    @patch("core.metar_monitor._process_temperature_event", return_value=0)
    @patch("core.metar_monitor.set_latest_observation")
    def test_cross_day_observation_rejected(self, mock_set_latest, _mock_process):
        ingested, _alerts = metar_monitor._ingest_obs(
            "KNYC",
            [{"obs_time": "2026-01-02T04:50:00+00:00", "temp_f": 70.0}],
            {"cache_file": "/tmp/unused"},
            allow_alert_delivery=False,
            persist_cache=False,
            window_start=datetime(2026, 1, 2, 5, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 2, 5, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(ingested, 0)
        mock_set_latest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
