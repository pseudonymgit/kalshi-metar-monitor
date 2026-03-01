import os
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

import app as app_module
from core.settlement_epoch_logger import log_transition_for_settlement_epoch

app = app_module.app


class ObservabilityTraderDashboardTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app._build_market_coverage_rows")
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"], "poll_seconds": 60})
    @patch("app.get_state")
    @patch("app.datetime")
    def test_trader_dashboard_combines_station_rows(self, mock_datetime, mock_get_state, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 10, 12, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_get_state.return_value = {
            "stations": ["KDEN", "KLAX"],
            "last_seen_iso": {
                "KDEN": "2026-01-01T10:10:00+00:00",
                "KLAX": "2026-01-01T08:00:00+00:00",
            },
            "last_poll_utc": "2026-01-01T10:19:00+00:00",
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.1,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69, "market_type": "HIGH"},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )


            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transition_events (
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT,
                        station TEXT,
                        transition_type TEXT,
                        instant_bucket_before INTEGER,
                        instant_bucket_after INTEGER,
                        settlement_bucket INTEGER,
                        running_max REAL,
                        current_temp REAL,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO transition_events (
                        id,
                        created_utc,
                        station,
                        transition_type,
                        instant_bucket_before,
                        instant_bucket_after,
                        settlement_bucket,
                        running_max,
                        current_temp,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        101,
                        "2026-01-01T10:05:00Z",
                        "KDEN",
                        "instant_up",
                        69,
                        70,
                        70,
                        70.1,
                        70.1,
                        '{"market_evaluated": true, "alerts_sent": 1, "evaluation_outcome": "ALERT_SENT"}',
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO transition_events (
                        id,
                        created_utc,
                        station,
                        transition_type,
                        instant_bucket_before,
                        instant_bucket_after,
                        settlement_bucket,
                        running_max,
                        current_temp,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        102,
                        "2026-01-01T10:06:00Z",
                        "KDEN",
                        "instant_up",
                        70,
                        70,
                        70,
                        70.2,
                        70.2,
                        '{"note": "non_eval_event"}',
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            app_module._build_market_coverage_rows.return_value = {
                "station": None,
                "stations_evaluated": ["KDEN", "KLAX"],
                "rows": [
                    {
                        "station": "KDEN",
                        "market_type": "HIGH",
                        "alerting_possible": True,
                        "coverage_status": "alerting_possible_runtime_gated",
                        "coverage_reason": "eligible_but_runtime_transition_terminal_rate_limit_gates_apply",
                        "eligible_market_count_after_filters": 3,
                    },
                    {
                        "station": "KDEN",
                        "market_type": "LOW",
                        "alerting_possible": False,
                        "coverage_status": "not_covered",
                        "coverage_reason": "market_type_disabled_by_config",
                        "eligible_market_count_after_filters": 0,
                    },
                    {
                        "station": "KLAX",
                        "market_type": "HIGH",
                        "alerting_possible": False,
                        "coverage_status": "evaluation_only",
                        "coverage_reason": "webhook_missing",
                        "eligible_market_count_after_filters": 2,
                    },
                ],
            }

            response = self.client.get("/observability/trader-dashboard")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)

        rows = {row["station"]: row for row in payload["rows"]}

        expected_contract_fields = {
            "station",
            "ingestion_status",
            "ingestion_status_reason",
            "latest_accepted_observation_utc",
            "freshness_lag_seconds",
            "latest_poll_utc",
            "current_epoch_selection_source",
            "epoch_status",
            "local_trading_date",
            "settlement_bucket",
            "prior_settlement_bucket",
            "settlement_timestamp_utc",
            "reversion_occurred",
            "first_reversion_timestamp_utc",
            "max_excursion_above_settlement",
            "duration_at_or_above_settlement_seconds",
            "duration_strictly_above_settlement_seconds",
            "terminal_state_reached",
            "last_transition_timestamp_utc",
            "last_transition_temp_f",
            "epoch_count_today",
            "open_epoch_present",
            "closed_epoch_count_today",
            "reverted_epoch_count_today",
            "terminal_epoch_count_today",
            "high_alerting_possible",
            "low_alerting_possible",
            "high_coverage_status",
            "low_coverage_status",
            "high_coverage_reason",
            "low_coverage_reason",
            "high_eligible_market_count",
            "low_eligible_market_count",
            "latest_evaluation_timestamp_utc",
            "latest_market_evaluated",
            "latest_alerts_sent",
            "latest_evaluation_outcome",
            "latest_suppression_reason",
            "latest_transition_type",
            "latest_transition_event_id",
            "attention_status",
            "attention_reason",
        }

        self.assertTrue(expected_contract_fields.issubset(set(rows["KDEN"].keys())))
        self.assertTrue(expected_contract_fields.issubset(set(rows["KLAX"].keys())))

        self.assertEqual(rows["KDEN"]["ingestion_status"], "healthy")
        self.assertEqual(rows["KDEN"]["ingestion_status_reason"], "freshness_lag_within_threshold")
        self.assertEqual(rows["KDEN"]["epoch_status"], "open")
        self.assertEqual(rows["KDEN"]["settlement_bucket"], 70)
        self.assertEqual(rows["KDEN"]["epoch_count_today"], 1)
        self.assertEqual(rows["KDEN"]["open_epoch_present"], True)
        self.assertEqual(rows["KDEN"]["closed_epoch_count_today"], 0)
        self.assertEqual(rows["KDEN"]["reverted_epoch_count_today"], 0)
        self.assertEqual(rows["KDEN"]["terminal_epoch_count_today"], 0)
        self.assertEqual(rows["KDEN"]["high_alerting_possible"], True)
        self.assertEqual(rows["KDEN"]["low_alerting_possible"], False)
        self.assertEqual(rows["KDEN"]["high_coverage_status"], "alerting_possible_runtime_gated")
        self.assertEqual(rows["KDEN"]["low_coverage_status"], "not_covered")
        self.assertEqual(rows["KDEN"]["high_coverage_reason"], "eligible_but_runtime_transition_terminal_rate_limit_gates_apply")
        self.assertEqual(rows["KDEN"]["low_coverage_reason"], "market_type_disabled_by_config")
        self.assertEqual(rows["KDEN"]["high_eligible_market_count"], 3)
        self.assertEqual(rows["KDEN"]["low_eligible_market_count"], 0)
        self.assertEqual(rows["KDEN"]["attention_status"], "ready")
        self.assertEqual(rows["KDEN"]["attention_reason"], "alerting_possible_open_epoch")

        self.assertEqual(rows["KLAX"]["ingestion_status"], "stale")
        self.assertEqual(rows["KLAX"]["ingestion_status_reason"], "freshness_lag_exceeds_threshold")
        self.assertIsNone(rows["KLAX"]["epoch_status"])
        self.assertEqual(rows["KLAX"]["epoch_count_today"], 0)
        self.assertEqual(rows["KLAX"]["open_epoch_present"], False)
        self.assertEqual(rows["KLAX"]["closed_epoch_count_today"], 0)
        self.assertEqual(rows["KLAX"]["reverted_epoch_count_today"], 0)
        self.assertEqual(rows["KLAX"]["terminal_epoch_count_today"], 0)
        self.assertEqual(rows["KLAX"]["high_coverage_status"], "evaluation_only")
        self.assertIsNone(rows["KLAX"]["low_coverage_status"])
        self.assertEqual(rows["KLAX"]["high_eligible_market_count"], 2)
        self.assertIsNone(rows["KLAX"]["low_eligible_market_count"])
        self.assertEqual(rows["KDEN"]["latest_evaluation_timestamp_utc"], "2026-01-01T10:05:00Z")
        self.assertEqual(rows["KDEN"]["latest_market_evaluated"], True)
        self.assertEqual(rows["KDEN"]["latest_alerts_sent"], 1)
        self.assertEqual(rows["KDEN"]["latest_evaluation_outcome"], "ALERT_SENT")
        self.assertIsNone(rows["KDEN"]["latest_suppression_reason"])
        self.assertEqual(rows["KDEN"]["latest_transition_type"], "instant_up")
        self.assertEqual(rows["KDEN"]["latest_transition_event_id"], 101)

        self.assertIsNone(rows["KLAX"]["latest_evaluation_timestamp_utc"])
        self.assertIsNone(rows["KLAX"]["latest_market_evaluated"])
        self.assertIsNone(rows["KLAX"]["latest_alerts_sent"])
        self.assertIsNone(rows["KLAX"]["latest_evaluation_outcome"])
        self.assertIsNone(rows["KLAX"]["latest_suppression_reason"])
        self.assertIsNone(rows["KLAX"]["latest_transition_type"])
        self.assertIsNone(rows["KLAX"]["latest_transition_event_id"])
        self.assertEqual(rows["KLAX"]["attention_status"], "action_needed")
        self.assertEqual(rows["KLAX"]["attention_reason"], "stale_ingestion")


    def test_trader_dashboard_attention_priority_order(self):
        cases = [
            ({"ingestion_status": "stale"}, "action_needed", "stale_ingestion"),
            (
                {
                    "ingestion_status": "healthy",
                    "high_coverage_status": "not_covered",
                    "low_coverage_status": "not_covered",
                    "high_coverage_reason": "no_discovered_series",
                    "low_coverage_reason": "no_discovered_series",
                },
                "action_needed",
                "no_discovered_series",
            ),
            (
                {
                    "ingestion_status": "healthy",
                    "high_coverage_status": "not_covered",
                    "low_coverage_status": "not_covered",
                    "high_coverage_reason": "no_eligible_markets_after_filters",
                    "low_coverage_reason": "no_eligible_markets_after_filters",
                },
                "action_needed",
                "no_eligible_markets",
            ),
            (
                {
                    "ingestion_status": "healthy",
                    "terminal_state_reached": True,
                    "latest_evaluation_outcome": "SUPPRESSED_RUNTIME_GATED",
                },
                "watch",
                "terminal_state_reached",
            ),
            (
                {
                    "ingestion_status": "healthy",
                    "latest_evaluation_outcome": "SUPPRESSED_RUNTIME_GATED",
                    "high_coverage_status": "evaluation_only",
                    "high_coverage_reason": "webhook_missing",
                },
                "watch",
                "latest_evaluation_suppressed",
            ),
            (
                {
                    "ingestion_status": "healthy",
                    "high_coverage_status": "evaluation_only",
                    "high_coverage_reason": "webhook_missing",
                },
                "action_needed",
                "evaluation_only_webhook_missing",
            ),
            (
                {
                    "ingestion_status": "healthy",
                    "open_epoch_present": True,
                    "high_alerting_possible": True,
                },
                "ready",
                "alerting_possible_open_epoch",
            ),
            ({"ingestion_status": "healthy"}, "normal", "normal"),
        ]

        for row, expected_status, expected_reason in cases:
            with self.subTest(row=row):
                attention = app_module._derive_trader_dashboard_attention(row)
                self.assertEqual(attention["attention_status"], expected_status)
                self.assertEqual(attention["attention_reason"], expected_reason)


    @patch("app._build_market_coverage_rows", return_value={"station": "KDEN", "stations_evaluated": ["KDEN"], "rows": []})
    @patch("app.is_scheduler_running", return_value=True)
    @patch("app.get_default_config", return_value={"stations": ["KDEN", "KLAX"], "poll_seconds": 60})
    @patch(
        "app.get_state",
        return_value={
            "stations": ["KDEN", "KLAX"],
            "last_seen_iso": {"KDEN": "2026-01-01T10:10:00+00:00"},
            "last_poll_utc": "2026-01-01T10:19:00+00:00",
        },
    )
    @patch("app.datetime")
    def test_trader_dashboard_station_filter(self, mock_datetime, *_mocks):
        from datetime import datetime, timezone

        mock_datetime.now.return_value = datetime(2026, 1, 1, 10, 12, 0, tzinfo=timezone.utc)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        response = self.client.get("/observability/trader-dashboard?station=KDEN")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["station"], "KDEN")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rows"][0]["station"], "KDEN")


if __name__ == "__main__":
    unittest.main()
