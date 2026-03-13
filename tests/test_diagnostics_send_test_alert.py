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

    @patch("app.get_station_hydration_cache_probe")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._run_simulate_ladder")
    def test_send_test_alert_defaults_and_response_contract(self, mock_run, mock_hydration_snapshot, mock_cache_probe):
        mock_hydration_snapshot.return_value = {"KDEN": {"status": "cache_valid"}}
        mock_cache_probe.return_value = {"cache_present": True, "raw_market_count": 2}
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

    @patch("app.get_station_hydration_cache_probe")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._run_simulate_ladder")
    def test_send_test_alert_includes_failed_delivery_details(self, mock_run, mock_hydration_snapshot, mock_cache_probe):
        mock_hydration_snapshot.return_value = {"KDEN": {"status": "cache_valid"}}
        mock_cache_probe.return_value = {"cache_present": True, "raw_market_count": 2}
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

    @patch("app.get_station_hydration_cache_probe")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._run_simulate_ladder")
    def test_send_test_alert_no_alert_generated_has_null_delivery_details(self, mock_run, mock_hydration_snapshot, mock_cache_probe):
        mock_hydration_snapshot.return_value = {"KDEN": {"status": "cache_valid"}}
        mock_cache_probe.return_value = {"cache_present": True, "raw_market_count": 2}
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

    @patch("app.get_station_hydration_cache_probe")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._run_simulate_ladder")
    def test_send_test_alert_preflight_blocks_delivery_when_hydration_unhealthy(
        self,
        mock_run,
        mock_hydration_snapshot,
        mock_cache_probe,
    ):
        mock_hydration_snapshot.return_value = {"KDEN": {"status": "cache_missing"}}
        mock_cache_probe.return_value = {"cache_present": False, "raw_market_count": 0}

        response = self.client.get("/debug/send-test-alert?temp=49.2&deliver=true")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["preflight_failed"], True)
        self.assertEqual(payload["preflight_reason"], "HYDRATION_CACHE_INVALID")
        self.assertEqual(payload["delivery_requested"], True)
        self.assertEqual(payload["delivery_attempted"], False)
        self.assertEqual(payload["alerts_generated"], 0)
        self.assertEqual(payload["hydration_state"]["cache_valid"], False)
        self.assertEqual(payload["hydration_state"]["markets_cached"], False)
        mock_run.assert_not_called()

    @patch("app.get_station_hydration_cache_probe")
    @patch("app.get_hydration_prerequisite_state_snapshot")
    @patch("app._run_simulate_ladder")
    def test_send_test_alert_preflight_allows_delivery_when_hydration_healthy(
        self,
        mock_run,
        mock_hydration_snapshot,
        mock_cache_probe,
    ):
        mock_hydration_snapshot.return_value = {"KDEN": {"status": "cache_valid"}}
        mock_cache_probe.return_value = {"cache_present": True, "raw_market_count": 3}
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

        response = self.client.get("/debug/send-test-alert?temp=49.2&deliver=true")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["delivery_attempted"], True)
        self.assertEqual(payload["delivery_succeeded"], True)
        mock_run.assert_called_once_with(icao="KDEN", temp_f=49.2, deliver=True)


if __name__ == "__main__":
    unittest.main()
