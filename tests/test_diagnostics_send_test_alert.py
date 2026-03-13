import unittest
from unittest.mock import patch

import app as app_module

app = app_module.app


class DiagnosticsPageTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_diagnostics_page_contains_vital_checks_and_test_alert_link(self):
        response = self.client.get("/diagnostics")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("/observability/hydration-prerequisite-runtime?station=KDEN", body)
        self.assertIn("/debug/send-test-alert?station=KDEN&temp=49.2&deliver=true", body)
        self.assertIn("/execution-domain", body)
        self.assertIn("/debug/alerts?limit=20", body)
        self.assertIn("/debug/state", body)


class DebugSendTestAlertEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._run_simulate_ladder")
    def test_send_test_alert_defaults_and_response_contract(self, mock_run):
        mock_run.return_value = {
            "ok": True,
            "alerts_generated": 1,
            "delivery_requested": True,
            "delivery_attempted": True,
            "suppression_reason": None,
        }

        response = self.client.get("/debug/send-test-alert?temp=49.2")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["temp"], 49.2)
        self.assertEqual(payload["deliver"], True)
        self.assertEqual(payload["alerts_generated"], 1)
        self.assertEqual(payload["delivery_requested"], True)
        self.assertEqual(payload["delivery_attempted"], True)
        self.assertIsNone(payload["suppression_reason"])
        mock_run.assert_called_once_with(icao="KDEN", temp_f=49.2, deliver=True)


if __name__ == "__main__":
    unittest.main()
