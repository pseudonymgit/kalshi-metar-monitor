import unittest
from unittest.mock import patch

import app as app_module


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def get_json(self, silent=False):
        return self.payload


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, path, query_string=None):
        self.calls.append((path, query_string or {}))
        return FakeResponse(self.payloads[path])


class PipelineTruthEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app_module.app.test_client()

    def payloads(self, hydration="cache_valid", eligible=1, alerts=1, transitions=1):
        return {
            "/observability/transition-runtime": {
                "transitions_seen_today": transitions,
                "last_transition_timestamp": "2026-01-01T00:00:00Z",
            },
            "/observability/hydration-prerequisite-runtime": {"hydration_state": {"status": hydration}},
            "/observability/market-eligibility-runtime": {"eligible_markets_count": eligible},
            "/integrity/alert_pipeline": {"hydration_stall_signal": {"alerts_sent_today": alerts}},
        }

    def test_missing_station_returns_http_400(self):
        self.assertEqual(self.client.get("/observability/pipeline-truth").status_code, 400)

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_lowercase_station_normalized_to_uppercase(self, mock_tc, _):
        fc = FakeClient(self.payloads())
        mock_tc.return_value = fc
        payload = self.client.get("/observability/pipeline-truth?station=kden").get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertTrue(all(q.get("station") == "KDEN" for _, q in fc.calls))

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    def test_unknown_station_returns_deterministic_json_with_required_fields(self, _):
        payload = self.client.get("/observability/pipeline-truth?station=xxxx").get_json()
        self.assertEqual(payload["pipeline_status"], "unknown_station")
        self.assertEqual(set(payload.keys()), {
            "station", "pipeline_status", "blocking_stage", "reason", "transitions_seen_today",
            "eligible_markets_count", "alerts_sent_today", "hydration_status", "last_transition_timestamp",
        })

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_hydration_classification(self, mock_tc, _):
        mock_tc.return_value = FakeClient(self.payloads(hydration="cache_stale"))
        payload = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        self.assertEqual(payload["blocking_stage"], "HYDRATION")

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_market_evaluation_classification(self, mock_tc, _):
        mock_tc.return_value = FakeClient(self.payloads(eligible=0))
        payload = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        self.assertEqual(payload["blocking_stage"], "MARKET_EVALUATION")

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_alert_emission_classification(self, mock_tc, _):
        mock_tc.return_value = FakeClient(self.payloads(alerts=0, transitions=1))
        payload = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        self.assertEqual(payload["blocking_stage"], "ALERT_EMISSION")

    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_identical_input_produces_identical_output(self, mock_tc, _):
        mock_tc.return_value = FakeClient(self.payloads())
        first = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        second = self.client.get("/observability/pipeline-truth?station=KDEN").get_json()
        self.assertEqual(first, second)

    @patch("app.stop_scheduler", side_effect=AssertionError("no scheduler mutation"))
    @patch("app.start_scheduler", side_effect=AssertionError("no scheduler mutation"))
    @patch("app._send_alert", side_effect=AssertionError("no alert mutation"))
    @patch("app.enqueue_station_hydration", side_effect=AssertionError("no hydration mutation"))
    @patch("app._canonical_live_station_universe", return_value={"stations": ["KDEN"]})
    @patch("app.app.test_client")
    def test_endpoint_does_not_mutate_runtime_state(self, mock_tc, *_):
        mock_tc.return_value = FakeClient(self.payloads())
        self.assertEqual(self.client.get("/observability/pipeline-truth?station=KDEN").status_code, 200)


if __name__ == "__main__":
    unittest.main()
