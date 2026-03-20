import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from core.metar_monitor import get_persisted_transition_history

app = app_module.app


class PersistedTransitionHistoryTests(unittest.TestCase):
    def _create_transition_events_table(self, conn):
        conn.execute(
            """
            CREATE TABLE transition_events (
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

    def test_returns_persisted_rows_in_descending_order_with_metadata_promotion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "alerts.db")
            conn = sqlite3.connect(db_path)
            try:
                self._create_transition_events_table(conn)
                conn.execute(
                    """
                    INSERT INTO transition_events (
                        id, created_utc, station, transition_type,
                        instant_bucket_before, instant_bucket_after,
                        settlement_bucket, running_max, current_temp, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "2026-03-20T09:59:00+00:00",
                        "KDEN",
                        "instant_up",
                        68,
                        69,
                        69,
                        69.1,
                        69.1,
                        json.dumps({"obs_time": "2026-03-20T09:58:00+00:00"}, sort_keys=True),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO transition_events (
                        id, created_utc, station, transition_type,
                        instant_bucket_before, instant_bucket_after,
                        settlement_bucket, running_max, current_temp, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        "2026-03-20T10:00:00+00:00",
                        "KDEN",
                        "settlement_up",
                        69,
                        70,
                        70,
                        70.3,
                        70.3,
                        json.dumps(
                            {
                                "obs_time": "2026-03-20T10:00:00+00:00",
                                "alerts_sent": 1,
                                "evaluation_outcome": "ALERT_SENT",
                                "suppression_reason": "",
                                "market_eligibility_runtime": {"eligible_markets_count": 2},
                                "alert_path_truth": {"blocking_stage": "delivered"},
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.dict(os.environ, {"ALERT_DB_PATH": db_path}, clear=False):
                rows = get_persisted_transition_history(station="kden", limit=10)

        self.assertEqual([row["transition_event_id"] for row in rows], [2, 1])
        latest = rows[0]
        self.assertEqual(latest["station"], "KDEN")
        self.assertEqual(latest["timestamp_utc"], "2026-03-20T10:00:00+00:00")
        self.assertEqual(latest["alerts_sent"], 1)
        self.assertEqual(latest["evaluation_outcome"], "ALERT_SENT")
        self.assertEqual(latest["suppression_reason"], "")
        self.assertEqual(latest["market_eligibility_runtime"], {"eligible_markets_count": 2})
        self.assertEqual(latest["alert_path_truth"], {"blocking_stage": "delivered"})
        self.assertEqual(latest["metadata"]["obs_time"], "2026-03-20T10:00:00+00:00")

    @patch("core.metar_monitor.station_local_day_key")
    def test_filters_by_station_and_day(self, mock_station_local_day_key):
        mock_station_local_day_key.side_effect = lambda station, ts: {
            "2026-03-19T23:50:00+00:00": "2026-03-19",
            "2026-03-20T00:05:00+00:00": "2026-03-20",
            "2026-03-20T01:00:00+00:00": "2026-03-20",
        }[ts]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "alerts.db")
            conn = sqlite3.connect(db_path)
            try:
                self._create_transition_events_table(conn)
                rows = [
                    (
                        1,
                        "2026-03-19T23:51:00+00:00",
                        "KDEN",
                        "instant_up",
                        60,
                        61,
                        61,
                        61.0,
                        61.0,
                        json.dumps({"obs_time": "2026-03-19T23:50:00+00:00"}, sort_keys=True),
                    ),
                    (
                        2,
                        "2026-03-20T00:06:00+00:00",
                        "KDEN",
                        "instant_up",
                        61,
                        62,
                        62,
                        62.0,
                        62.0,
                        json.dumps({"obs_time": "2026-03-20T00:05:00+00:00"}, sort_keys=True),
                    ),
                    (
                        3,
                        "2026-03-20T01:01:00+00:00",
                        "KLAX",
                        "instant_up",
                        62,
                        63,
                        63,
                        63.0,
                        63.0,
                        json.dumps({"obs_time": "2026-03-20T01:00:00+00:00"}, sort_keys=True),
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO transition_events (
                        id, created_utc, station, transition_type,
                        instant_bucket_before, instant_bucket_after,
                        settlement_bucket, running_max, current_temp, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

            with patch.dict(os.environ, {"ALERT_DB_PATH": db_path}, clear=False):
                filtered = get_persisted_transition_history(station="KDEN", day="2026-03-20", limit=10)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["station"], "KDEN")
        self.assertEqual(filtered[0]["transition_event_id"], 2)


class ObservabilityTransitionsEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    @patch("app.get_transition_history")
    def test_live_endpoint_preserves_response_envelope_shape(self, mock_get_transition_history):
        mock_get_transition_history.return_value = [
            {
                "station": "KDEN",
                "transition_type": "settlement_up",
                "timestamp_utc": "2026-03-20T10:00:00+00:00",
                "transition_event_id": 77,
                "metadata": {"obs_time": "2026-03-20T10:00:00+00:00"},
            }
        ]

        response = self.client.get("/observability/transitions?station=kden&limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload, {
            "ok": True,
            "count": 1,
            "transitions": mock_get_transition_history.return_value,
        })
        mock_get_transition_history.assert_called_once_with(
            station="KDEN",
            limit=5,
        )

    @patch("app.get_persisted_transition_history")
    def test_persisted_endpoint_preserves_response_envelope_shape(self, mock_get_persisted_transition_history):
        mock_get_persisted_transition_history.return_value = [
            {
                "station": "KDEN",
                "transition_type": "settlement_up",
                "timestamp_utc": "2026-03-20T10:00:00+00:00",
                "transition_event_id": 77,
                "metadata": {"obs_time": "2026-03-20T10:00:00+00:00"},
            }
        ]

        response = self.client.get("/observability/transitions/persisted?station=kden&day=2026-03-20&limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload, {
            "ok": True,
            "count": 1,
            "transitions": mock_get_persisted_transition_history.return_value,
        })
        mock_get_persisted_transition_history.assert_called_once_with(
            station="KDEN",
            day="2026-03-20",
            limit=5,
        )
