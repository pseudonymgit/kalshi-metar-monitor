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

    def test_epoch_segmentation_by_market_type(self):
        """L2-T2: Verify that HIGH and LOW epochs are stored separately and queries return correct market-type-specific epochs."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            # Log HIGH market epochs
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={
                    "obs_time": "2026-01-01T10:00:00Z",
                    "previous_settlement_bucket": 69,
                    "market_type": "HIGH",
                },
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="instant_up",
                settlement_bucket=70,
                current_temp=71.0,
                metadata={"obs_time": "2026-01-01T10:10:00Z", "market_type": "HIGH"},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:10:00Z",
            )

            # Log LOW market epochs
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=68,
                current_temp=68.2,
                metadata={
                    "obs_time": "2026-01-01T10:00:00Z",
                    "previous_settlement_bucket": 67,
                    "market_type": "LOW",
                },
                transition_event_id=3,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )

            # Log HIGH market second epoch
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=71,
                current_temp=71.1,
                metadata={"obs_time": "2026-01-01T10:30:00Z", "market_type": "HIGH"},
                transition_event_id=4,
                event_timestamp_utc="2026-01-01T10:30:00Z",
            )

            conn = sqlite3.connect(db_path)
            
            # Query HIGH market epochs
            high_epochs = conn.execute(
                "SELECT id, settlement_bucket, market_type FROM settlement_epochs WHERE market_type = 'HIGH' ORDER BY id"
            ).fetchall()
            
            # Query LOW market epochs
            low_epochs = conn.execute(
                "SELECT id, settlement_bucket, market_type FROM settlement_epochs WHERE market_type = 'LOW' ORDER BY id"
            ).fetchall()
            
            conn.close()

            # Verify HIGH epochs
            self.assertEqual(len(high_epochs), 2)
            self.assertEqual(high_epochs[0][1], 70)  # First HIGH epoch settlement bucket
            self.assertEqual(high_epochs[1][1], 71)  # Second HIGH epoch settlement bucket
            self.assertEqual(high_epochs[0][2], "HIGH")
            self.assertEqual(high_epochs[1][2], "HIGH")

            # Verify LOW epochs
            self.assertEqual(len(low_epochs), 1)
            self.assertEqual(low_epochs[0][1], 68)  # LOW epoch settlement bucket
            self.assertEqual(low_epochs[0][2], "LOW")

    def test_epoch_migration_with_null_market_type(self):
        """L2-T3: Verify backward compatibility - existing epochs with market_type IS NULL still queried (fallback)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            # Log epoch without market_type (backward compatibility - old data)
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "previous_settlement_bucket": 69},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )

            conn = sqlite3.connect(db_path)
            
            # Query epochs with market_type IS NULL
            null_epochs = conn.execute(
                "SELECT id, settlement_bucket, market_type FROM settlement_epochs WHERE market_type IS NULL ORDER BY id"
            ).fetchall()
            
            conn.close()

            # Verify null market_type epochs still work (backward compatibility)
            self.assertEqual(len(null_epochs), 1)
            self.assertEqual(null_epochs[0][1], 70)
            self.assertIsNone(null_epochs[0][2])

    def test_epoch_open_close_by_market_type(self):
        """Verify that epochs open/close independently by market_type."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            # Open HIGH epoch
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=70,
                current_temp=70.2,
                metadata={"obs_time": "2026-01-01T10:00:00Z", "market_type": "HIGH"},
                transition_event_id=1,
                event_timestamp_utc="2026-01-01T10:00:00Z",
            )

            # Open LOW epoch (should not close HIGH epoch)
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=68,
                current_temp=68.2,
                metadata={"obs_time": "2026-01-01T10:05:00Z", "market_type": "LOW"},
                transition_event_id=2,
                event_timestamp_utc="2026-01-01T10:05:00Z",
            )

            # Close HIGH epoch with next settlement up
            log_transition_for_settlement_epoch(
                station="KDEN",
                transition_type="settlement_up",
                settlement_bucket=71,
                current_temp=71.1,
                metadata={"obs_time": "2026-01-01T10:10:00Z", "market_type": "HIGH"},
                transition_event_id=3,
                event_timestamp_utc="2026-01-01T10:10:00Z",
            )

            conn = sqlite3.connect(db_path)
            
            # Query all epochs
            all_epochs = conn.execute(
                "SELECT id, market_type, epoch_status, epoch_close_reason FROM settlement_epochs ORDER BY id"
            ).fetchall()
            
            conn.close()

            # Verify: HIGH epoch 1 should be closed, HIGH epoch 2 should be open, LOW epoch should be open
            self.assertEqual(all_epochs[0][1], "HIGH")
            self.assertEqual(all_epochs[0][2], "closed")
            self.assertEqual(all_epochs[0][3], "next_settlement_up")
            
            self.assertEqual(all_epochs[1][1], "LOW")
            self.assertEqual(all_epochs[1][2], "open")
            self.assertIsNone(all_epochs[1][3])
            
            self.assertEqual(all_epochs[2][1], "HIGH")
            self.assertEqual(all_epochs[2][2], "open")
            self.assertIsNone(all_epochs[2][3])


if __name__ == "__main__":
    unittest.main()
