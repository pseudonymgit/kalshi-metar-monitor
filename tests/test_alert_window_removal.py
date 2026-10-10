import os
import tempfile
import unittest
from unittest.mock import patch

from core import metar_monitor


class AlertWindowRemovalTests(unittest.TestCase):
    def setUp(self):
        self.station = "KNYC"
        self.obs_time = "2026-01-01T03:10:00+00:00"
        # Hermetic DB: _process_temperature_event reaches settlement-epoch logging
        # which writes to ALERT_DB_PATH (defaults to /var/data, unwritable in tests).
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        os.environ["ALERT_DB_PATH"] = self.temp_db.name
        with metar_monitor._STATE_LOCK:
            metar_monitor._STATE["last_observed_integer"][self.station] = 69
            metar_monitor._STATE["running_daily_max"][self.station] = 69.2
            metar_monitor._STATE["last_settlement_bucket"][self.station] = 69
            metar_monitor._STATE["last_instant_bucket"][self.station] = 69
            metar_monitor._STATE["last_reset_date_local"][self.station] = metar_monitor.station_local_day_key(self.station, self.obs_time)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        os.environ.pop("ALERT_DB_PATH", None)

    @patch("core.kalshi_monitor._current_kalshi_execution_domain", return_value="production")
    @patch("core.metar_monitor._emit_alert")
    def test_alert_fires_without_time_window(self, mock_emit_alert, _domain):
        alerts = metar_monitor._process_temperature_event(
            icao=self.station,
            temp_f=70.1,
            obs_time=self.obs_time,
            cfg={"webhook": "https://example.com/webhook"},
            last_temp_f=69.2,
            allow_alert_delivery=True,
        )

        self.assertEqual(alerts, 1)
        mock_emit_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
