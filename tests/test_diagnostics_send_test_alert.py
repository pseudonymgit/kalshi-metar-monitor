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
            "delivery_succeeded": True,
            "webhook_status_code": 204,
            "webhook_exception": None,
            "webhook_response_text": "ok",
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
        self.assertEqual(payload["delivery_succeeded"], True)
        self.assertEqual(payload["webhook_status_code"], 204)
        self.assertIsNone(payload["webhook_exception"])
        self.assertEqual(payload["webhook_response_text"], "ok")
        self.assertIsNone(payload["suppression_reason"])
        mock_run.assert_called_once_with(icao="KDEN", temp_f=49.2, deliver=True)

    @patch("app._run_simulate_ladder")
    def test_send_test_alert_includes_failed_delivery_details(self, mock_run):
        mock_run.return_value = {
            "ok": True,
            "alerts_generated": 1,
            "delivery_requested": True,
            "delivery_attempted": True,
            "delivery_succeeded": False,
            "webhook_status_code": 500,
            "webhook_exception": None,
            "webhook_response_text": "server error",
            "suppression_reason": None,
        }

        response = self.client.get("/debug/send-test-alert?temp=49.2")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["delivery_attempted"], True)
        self.assertEqual(payload["delivery_succeeded"], False)
        self.assertEqual(payload["webhook_status_code"], 500)
        self.assertIsNone(payload["webhook_exception"])
        self.assertEqual(payload["webhook_response_text"], "server error")

    @patch("app._run_simulate_ladder")
    def test_send_test_alert_no_alert_generated_has_null_delivery_details(self, mock_run):
        mock_run.return_value = {
            "ok": True,
            "alerts_generated": 0,
            "delivery_requested": True,
            "delivery_attempted": False,
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": None,
            "webhook_response_text": None,
            "suppression_reason": "NO_TRANSITION",
        }

        response = self.client.get("/debug/send-test-alert?temp=49.2")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["alerts_generated"], 0)
        self.assertEqual(payload["delivery_attempted"], False)
        self.assertEqual(payload["delivery_succeeded"], False)
        self.assertIsNone(payload["webhook_status_code"])
        self.assertIsNone(payload["webhook_exception"])
        self.assertIsNone(payload["webhook_response_text"])


if __name__ == "__main__":
    unittest.main()
