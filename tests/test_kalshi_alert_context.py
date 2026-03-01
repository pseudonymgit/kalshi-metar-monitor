import os
import sqlite3
import tempfile
import unittest

from core.kalshi_monitor import _derive_attention_phrase, _load_current_epoch_context


class KalshiAlertContextTests(unittest.TestCase):
    def test_load_current_epoch_context_prefers_open_epoch_within_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE settlement_epochs (
                    id INTEGER PRIMARY KEY,
                    station TEXT NOT NULL,
                    market_type TEXT,
                    local_trading_date TEXT NOT NULL,
                    settlement_bucket INTEGER NOT NULL,
                    prior_settlement_bucket INTEGER,
                    settlement_jump_magnitude INTEGER,
                    epoch_status TEXT NOT NULL,
                    reversion_occurred INTEGER NOT NULL DEFAULT 0,
                    first_reversion_timestamp_utc TEXT,
                    max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
                    terminal_state_reached INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "HIGH", "2026-01-01", 71, 70, 1, "closed", 0, None, 0.4, 0),
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "HIGH", "2026-01-01", 72, 71, 1, "open", 1, "2026-01-01T12:00:00Z", 0.8, 0),
            )
            conn.commit()
            conn.close()

            context = _load_current_epoch_context("KDEN", "HIGH", "2026-01-01T20:00:00Z")
            self.assertEqual(context["settlement_bucket"], 72)
            self.assertEqual(context["prior_settlement_bucket"], 71)
            self.assertEqual(context["settlement_jump_magnitude"], 1)
            self.assertEqual(context["epoch_status"], "open")
            self.assertTrue(context["reversion_occurred"])
            self.assertEqual(context["first_reversion_timestamp_utc"], "2026-01-01T12:00:00Z")
            self.assertEqual(context["max_excursion_above_settlement"], 0.8)
            self.assertFalse(context["terminal_state_reached"])

    def test_load_current_epoch_context_does_not_cross_market_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE settlement_epochs (
                    id INTEGER PRIMARY KEY,
                    station TEXT NOT NULL,
                    market_type TEXT,
                    local_trading_date TEXT NOT NULL,
                    settlement_bucket INTEGER NOT NULL,
                    prior_settlement_bucket INTEGER,
                    settlement_jump_magnitude INTEGER,
                    epoch_status TEXT NOT NULL,
                    reversion_occurred INTEGER NOT NULL DEFAULT 0,
                    first_reversion_timestamp_utc TEXT,
                    max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
                    terminal_state_reached INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "LOW", "2026-01-01", 58, 57, 1, "open", 0, None, 0.2, 0),
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "HIGH", "2026-01-01", 73, 72, 1, "open", 0, None, 0.3, 0),
            )
            conn.commit()
            conn.close()

            context = _load_current_epoch_context("KDEN", "HIGH", "2026-01-01T20:00:00Z")
            self.assertEqual(context["settlement_bucket"], 73)
            self.assertEqual(context["prior_settlement_bucket"], 72)

    def test_load_current_epoch_context_does_not_cross_local_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alerts.db")
            os.environ["ALERT_DB_PATH"] = db_path

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE settlement_epochs (
                    id INTEGER PRIMARY KEY,
                    station TEXT NOT NULL,
                    market_type TEXT,
                    local_trading_date TEXT NOT NULL,
                    settlement_bucket INTEGER NOT NULL,
                    prior_settlement_bucket INTEGER,
                    settlement_jump_magnitude INTEGER,
                    epoch_status TEXT NOT NULL,
                    reversion_occurred INTEGER NOT NULL DEFAULT 0,
                    first_reversion_timestamp_utc TEXT,
                    max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
                    terminal_state_reached INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "HIGH", "2025-12-31", 69, 68, 1, "open", 0, None, 0.1, 0),
            )
            conn.execute(
                """
                INSERT INTO settlement_epochs (
                    station, market_type, local_trading_date,
                    settlement_bucket, prior_settlement_bucket, settlement_jump_magnitude,
                    epoch_status, reversion_occurred, first_reversion_timestamp_utc,
                    max_excursion_above_settlement, terminal_state_reached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("KDEN", "HIGH", "2026-01-01", 72, 71, 1, "open", 0, None, 0.4, 0),
            )
            conn.commit()
            conn.close()

            context = _load_current_epoch_context("KDEN", "HIGH", "2026-01-01T20:00:00Z")
            self.assertEqual(context["settlement_bucket"], 72)
            self.assertEqual(context["prior_settlement_bucket"], 71)

    def test_attention_phrase_deterministic_priority(self):
        self.assertEqual(
            _derive_attention_phrase({"terminal_state_reached": True, "reversion_occurred": True}),
            "TERMINAL STATE",
        )
        self.assertEqual(
            _derive_attention_phrase({"terminal_state_reached": False, "reversion_occurred": True}),
            "REVERTED AFTER SETTLEMENT",
        )
        self.assertEqual(
            _derive_attention_phrase({
                "terminal_state_reached": False,
                "reversion_occurred": False,
                "settlement_bucket": 73,
                "prior_settlement_bucket": 72,
            }),
            "NEW SETTLEMENT / NO REVERSION YET",
        )
        self.assertEqual(
            _derive_attention_phrase({"epoch_status": "open"}),
            "OPEN EPOCH / ALERTABLE",
        )


if __name__ == "__main__":
    unittest.main()
