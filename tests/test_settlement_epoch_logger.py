import os
import sqlite3
import tempfile
import unittest

from core.settlement_epoch_logger import log_transition_for_settlement_epoch


class SettlementEpochLoggerTests(unittest.TestCase):
    def test_opens_updates_and_closes_on_next_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="instant_up",
                settlement_bucket=70,
                current_temp=71.0,
                metadata={"obs_time": "2026-01-01T10:10:00Z"},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:10:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="reversion_after_settlement",
                settlement_bucket=70,
                current_temp=69.8,
                metadata={"obs_time": "2026-01-01T10:20:00Z"},
                transition_event_id=3,
                event_timestamp_utc="2026-01-01T10:20:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=71,
                current_temp=71.1,
                metadata={"obs_time": "2026-01-01T10:30:00Z", "previous_settlement_bucket": 70},
                transition_event_id=4,
                event_timestamp_utc="2026-01-01T10:30:00Z",
            )

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                """
                SELECT settlement_bucket,
                       prior_settlement_bucket,
                       settlement_jump_magnitude,
                       epoch_status,
                       epoch_close_reason,
                       reversion_occurred,
                       first_reversion_timestamp_utc,
                       max_excursion_above_settlement,
                       duration_at_or_above_settlement_seconds,
                       duration_strictly_above_settlement_seconds
                FROM settlement_epochs
                ORDER BY id
                """
            ).fetchall()
            conn.close()

            self.assertEqual(len(rows), 2)
            first = rows[0]
            second = rows[1]

            self.assertEqual(first[0], 70)
            self.assertEqual(first[1], 69)
            self.assertEqual(first[2], 1)
            self.assertEqual(first[3], "closed")
            self.assertEqual(first[4], "next_settlement_up")
            self.assertEqual(first[5], 1)
            self.assertEqual(first[6], "2026-01-01T10:20:00Z")
            self.assertAlmostEqual(first[7], 1.0)
            self.assertAlmostEqual(first[8], 1200.0)
            self.assertAlmostEqual(first[9], 1200.0)

            self.assertEqual(second[0], 71)
            self.assertEqual(second[1], 70)
            self.assertEqual(second[2], 1)
            self.assertEqual(second[3], "open")
            self.assertEqual(second[4], None)

    def test_closes_on_station_local_day_key_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=60,
                current_temp=60.0,
                metadata={"obs_time": "2026-02-01T06:55:00Z", "previous_settlement_bucket": 59},
                transition_event_id=1,
                event_timestamp_utc="2026-02-01T06:55:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="instant_up",
                settlement_bucket=60,
                current_temp=60.5,
                metadata={"obs_time": "2026-02-01T07:05:00Z"},
                transition_event_id=2,
                event_timestamp_utc="2026-02-01T07:05:00Z",
            )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT epoch_status, epoch_close_reason FROM settlement_epochs ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()

            self.assertEqual(row[0], "closed")
            self.assertEqual(row[1], "day_reset")

    def test_does_not_close_when_utc_day_changes_but_station_local_day_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            log_transition_for_settlement_epoch(
                station="KPHL",
                transition_type="settlement_up",
                settlement_bucket=60,
                current_temp=60.0,
                metadata={"obs_time": "2026-02-01T23:55:00Z", "previous_settlement_bucket": 59},
                transition_event_id=1,
                event_timestamp_utc="2026-02-01T23:55:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KPHL",
                transition_type="instant_up",
                settlement_bucket=60,
                current_temp=60.5,
                metadata={"obs_time": "2026-02-02T00:05:00Z"},
                transition_event_id=2,
                event_timestamp_utc="2026-02-02T00:05:00Z",
            )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT epoch_status, epoch_close_reason FROM settlement_epochs ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()

            self.assertEqual(row[0], "open")
            self.assertEqual(row[1], None)


if __name__ == "__main__":
    unittest.main()
