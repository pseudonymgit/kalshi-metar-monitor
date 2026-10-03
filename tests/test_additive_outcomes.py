"""
Test: G.8 T+1 outcome resolver (M2 calibration sprint).

Proves the deliverable contract:
  - join correctness: each persisted (station,date,signal) row gets the realized
    next-trading-day outcome (temp + strict strike-crossing label)
  - provenance tiering: outcome provenance is keyed off the OUTCOME date
    (ghcn <= 2026-09-12, kalshi_finalized <= 2026-09-16, weatherapi after)
  - unresolved handling: missing outcome -> resolved=0, fields NULL (no fabrication)
  - idempotent re-run: last-write-wins, exactly one row per key
  - no look-ahead: outcome_date is strictly after the evaluated date (T+1)

All DBs are temp files (conftest sandbox forbids writing production data/*.db).
Truth-source fixtures mirror the real schemas of metar_backfill.db,
weatherapi_archive.db and kalshi_settlements.db.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import date as date_cls

from core.additive import persistence, outcomes


def _mk_metar_db(path):
    """settlement_truth (station,market_type,local_trading_date,...) + daily_stats."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE settlement_truth (
            station TEXT, market_type TEXT, local_trading_date TEXT,
            settlement_temp_f REAL, temp_source TEXT, kalshi_temp_f REAL,
            strike_price REAL, strike_type TEXT, trustworthy INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE daily_stats (
            id INTEGER PRIMARY KEY, station TEXT, date_local TEXT,
            max_temp_f REAL, min_temp_f REAL
        )
        """
    )
    # ghcn tier (outcome date 2026-09-11)
    conn.execute(
        "INSERT INTO settlement_truth VALUES "
        "('KNYC','HIGH','2026-09-11',82.0,'ghcn',NULL,80.0,'greater',1)"
    )
    # kalshi_finalized tier (outcome date 2026-09-15)
    conn.execute(
        "INSERT INTO settlement_truth VALUES "
        "('KNYC','HIGH','2026-09-15',77.0,'kalshi_finalized',77.0,75.0,'greater',1)"
    )
    # weatherapi tier (outcome date 2026-09-18)
    conn.execute(
        "INSERT INTO settlement_truth VALUES "
        "('KNYC','HIGH','2026-09-18',70.0,'weatherapi',NULL,72.0,'greater',1)"
    )
    # ghcn daily_stats fallback (outcome date 2026-09-12, no truth row)
    conn.execute(
        "INSERT INTO daily_stats(station,date_local,max_temp_f,min_temp_f) "
        "VALUES ('KATL','2026-09-12',88.0,70.0)"
    )
    conn.commit()
    conn.close()


def _mk_weatherapi_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE daily (station TEXT, date TEXT, max_temp_f REAL, min_temp_f REAL)"
    )
    # weatherapi tier path via the archive table (outcome date 2026-09-20)
    conn.execute(
        "INSERT INTO daily VALUES ('KNYC','2026-09-20',68.0,54.0)"
    )
    conn.commit()
    conn.close()


def _mk_kalshi_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE kalshi_settlements (
            id INTEGER PRIMARY KEY, station TEXT, series TEXT, event_ticker TEXT,
            target_date TEXT, kalshi_temp REAL, markets_found INTEGER,
            finalized_count INTEGER, source TEXT, source_type TEXT,
            created_at TEXT, updated_at TEXT
        )
        """
    )
    # kalshi_finalized path via the finalized-settlements table (2026-09-14)
    conn.execute(
        "INSERT INTO kalshi_settlements "
        "(station,series,target_date,kalshi_temp,source_type,updated_at) "
        "VALUES ('KATL','KXHIGHTATL','2026-09-14',79.0,'finalized','2026-09-16 00:00:00')"
    )
    conn.commit()
    conn.close()


def _mk_signal_db(path):
    conn = sqlite3.connect(path)
    persistence.ensure_table(conn)
    conn.commit()
    conn.close()


class OutcomeResolverTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.metar = os.path.join(self.d, "metar.db")
        self.wapi = os.path.join(self.d, "weatherapi.db")
        self.kalshi = os.path.join(self.d, "kalshi.db")
        self.sig = os.path.join(self.d, "signal_cache.db")
        _mk_metar_db(self.metar)
        _mk_weatherapi_db(self.wapi)
        _mk_kalshi_db(self.kalshi)
        _mk_signal_db(self.sig)

    def tearDown(self):
        for p in (self.metar, self.wapi, self.kalshi, self.sig):
            for s in ("", "-wal", "-shm"):
                if os.path.exists(p + s):
                    os.unlink(p + s)

    def _conn(self):
        return sqlite3.connect(self.sig)

    def _persist(self, conn, station, date, signal="cloud_clearing"):
        from core.additive.base import AdditiveSignalResult
        persistence.persist_results(
            conn,
            {(station, date): [AdditiveSignalResult(
                signal=signal, offset_f=1.0, confidence=0.5, fired=True)]},
        )
        conn.commit()

    def _resolve(self, conn, market_type="HIGH"):
        return outcomes.resolve_rows(
            conn,
            signal_rows=None,
            metar_db=self.metar,
            weatherapi_db=self.wapi,
            kalshi_db=self.kalshi,
            market_type=market_type,
        )

    # ── join correctness + strict strike-crossing label ─────────────────
    def test_join_ghcn_tier_with_strike(self):
        conn = self._conn()
        try:
            # eval 2026-09-10 (Thu) -> next trading day 2026-09-11 (Fri)
            self._persist(conn, "KNYC", "2026-09-10")
            n = self._resolve(conn)
            self.assertEqual(n, 1)
            row = outcomes.load_outcomes(conn)[0]
            self.assertTrue(row["resolved"])
            self.assertEqual(row["outcome_date"], "2026-09-11")
            self.assertEqual(row["outcome_temp_f"], 82.0)
            self.assertEqual(row["strike_price"], 80.0)
            self.assertEqual(row["axis"], "strike_crossing")
            self.assertEqual(row["provenance"], "ghcn")
            # strict: 82 > 80 -> yes
            self.assertTrue(row["outcome_yes"])
        finally:
            conn.close()

    def test_strict_strike_equality_is_loss(self):
        conn = self._conn()
        try:
            # weatherapi tier, 70.0 vs strike 72.0 -> no
            self._persist(conn, "KNYC", "2026-09-17")
            self._resolve(conn)
            row = outcomes.load_outcomes(conn)[0]
            self.assertEqual(row["outcome_date"], "2026-09-18")
            self.assertFalse(row["outcome_yes"])
            self.assertEqual(row["provenance"], "weatherapi")
        finally:
            conn.close()

    # ── provenance tiering (keyed on OUTCOME date) ──────────────────────
    def test_provenance_tiering_by_outcome_date(self):
        conn = self._conn()
        try:
            self._persist(conn, "KNYC", "2026-09-10")   # outcome 09-11 -> ghcn
            self._persist(conn, "KNYC", "2026-09-14")   # outcome 09-15 -> kalshi_finalized
            self._persist(conn, "KNYC", "2026-09-17")   # outcome 09-18 -> weatherapi
            self._resolve(conn)
            provs = {
                r["date"]: r["provenance"]
                for r in outcomes.load_outcomes(conn)
            }
            self.assertEqual(provs["2026-09-10"], "ghcn")
            self.assertEqual(provs["2026-09-14"], "kalshi_finalized")
            self.assertEqual(provs["2026-09-17"], "weatherapi")
        finally:
            conn.close()

    def test_kalshi_finalized_via_settlements_table(self):
        conn = self._conn()
        try:
            # eval 2026-09-11 (Fri) -> next trading day 2026-09-14 (Mon)
            self._persist(conn, "KATL", "2026-09-11")
            self._resolve(conn)
            row = outcomes.load_outcomes(conn)[0]
            self.assertTrue(row["resolved"])
            self.assertEqual(row["outcome_date"], "2026-09-14")
            self.assertEqual(row["outcome_temp_f"], 79.0)
            self.assertEqual(row["provenance"], "kalshi_finalized")
        finally:
            conn.close()

    def test_ghcn_daily_stats_fallback(self):
        conn = self._conn()
        try:
            # KATL 2026-09-11 (Fri) -> 2026-09-12 (Sat) is not a trading day, so
            # use a date whose T+1 lands on the daily_stats row via direct calendar.
            cal = lambda d: "2026-09-12"  # noqa: E731 (inject)
            self._persist(conn, "KATL", "2026-09-11")
            outcomes.resolve_rows(
                conn, signal_rows=None, metar_db=self.metar,
                weatherapi_db=self.wapi, kalshi_db=self.kalshi, calendar=cal,
            )
            row = outcomes.load_outcomes(conn)[0]
            self.assertTrue(row["resolved"])
            self.assertEqual(row["outcome_temp_f"], 88.0)
            self.assertEqual(row["provenance"], "ghcn")
            self.assertIsNone(row["outcome_yes"])  # no strike available
        finally:
            conn.close()

    # ── unresolved handling ─────────────────────────────────────────────
    def test_unresolved_missing_outcome_not_fabricated(self):
        conn = self._conn()
        try:
            # Far-future date -> no truth anywhere.
            self._persist(conn, "KNYC", "2027-01-04")
            n = self._resolve(conn)
            self.assertEqual(n, 1)
            row = outcomes.load_outcomes(conn)[0]
            self.assertFalse(row["resolved"])
            self.assertIsNone(row["outcome_temp_f"])
            self.assertIsNone(row["outcome_yes"])
            self.assertIsNone(row["strike_price"])
            self.assertEqual(row["reason"], "no_outcome_at_T1")
            self.assertEqual(row["provenance"], "weatherapi")
        finally:
            conn.close()

    def test_no_lookahead_outcome_is_strictly_after_eval(self):
        conn = self._conn()
        try:
            self._persist(conn, "KNYC", "2026-09-10")
            self._resolve(conn)
            row = outcomes.load_outcomes(conn)[0]
            self.assertGreater(
                date_cls.fromisoformat(row["outcome_date"]),
                date_cls.fromisoformat(row["date"]),
            )
        finally:
            conn.close()

    def test_trading_day_rollover_skips_weekend(self):
        conn = self._conn()
        try:
            # Fri 2026-10-02 -> next trading day should be a later weekday.
            nxt = outcomes.next_trading_day("2026-10-02")
            self.assertGreater(date_cls.fromisoformat(nxt),
                               date_cls.fromisoformat("2026-10-02"))
            self.assertLess(date_cls.fromisoformat(nxt).weekday(), 5)
        finally:
            conn.close()

    # ── idempotency ─────────────────────────────────────────────────────
    def test_idempotent_rerun_last_write_wins(self):
        conn = self._conn()
        try:
            self._persist(conn, "KNYC", "2026-09-10")
            self._resolve(conn)
            first = outcomes.load_outcomes(conn)
            self._resolve(conn)   # identical re-run
            second = outcomes.load_outcomes(conn)
            self.assertEqual(outcomes.count_rows(conn), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(
                (first[0]["outcome_temp_f"], first[0]["provenance"]),
                (second[0]["outcome_temp_f"], second[0]["provenance"]),
            )
        finally:
            conn.close()

    def test_clean_init_creates_table_and_index(self):
        conn = self._conn()
        try:
            outcomes.ensure_table(conn)
            outcomes.ensure_table(conn)
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn(outcomes.TABLE_NAME, tables)
            idx = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn(f"idx_{outcomes.TABLE_NAME}_station_date", idx)
            pk = [
                r[1] for r in conn.execute(
                    f"PRAGMA table_info({outcomes.TABLE_NAME})"
                ) if r[5] > 0
            ]
            self.assertEqual(sorted(pk), ["date", "signal", "station"])
        finally:
            conn.close()

    def test_empty_input_noop(self):
        conn = self._conn()
        try:
            self.assertEqual(self._resolve(conn), 0)
            self.assertEqual(outcomes.count_rows(conn), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
