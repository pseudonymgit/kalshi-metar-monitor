import unittest
from unittest.mock import Mock, patch

from core.metar_monitor import _fetch_range_nws


class NWSFetchDiagnosticLoggingTests(unittest.TestCase):
    @patch("core.metar_monitor.requests.get")
    @patch("core.metar_monitor._current_kalshi_execution_domain", create=True, return_value="production")
    def test_logs_request_and_result_diagnostics(self, _mock_domain, mock_get):
        response = Mock()
        response.status_code = 200
        response.headers = {"Date": "Mon, 03 Mar 2026 01:02:03 GMT"}
        response.json.return_value = {
            "features": [
                {
                    "properties": {
                        "temperature": {"value": 10.0},
                        "timestamp": "2026-03-03T01:00:00+00:00",
                    }
                }
            ]
        }
        response.raise_for_status = Mock()
        mock_get.return_value = response

        cfg = {"http_from": "ops@example.com", "http_agent": "KalshiMetarMonitor/1.1 (+ops@example.com)"}
        with self.assertLogs("core.metar_monitor", level="INFO") as logs:
            _fetch_range_nws("KNYC", "2026-03-03T00:00:00Z", "2026-03-03T01:00:00Z", cfg)

        emitted = "\n".join(logs.output)
        self.assertIn("NWS_FETCH_DIAGNOSTIC", emitted)
        self.assertIn("station=KNYC", emitted)
        self.assertIn(
            "url=https://api.weather.gov/stations/KNYC/observations?limit=200",
            emitted,
        )
        self.assertIn("NWS_FETCH_RESULT", emitted)
        self.assertIn("feature_count=1", emitted)


if __name__ == "__main__":
    unittest.main()
