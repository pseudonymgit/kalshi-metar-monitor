"""
Test: G.8 additive signal-result persistence writer (M1 calibration sprint).

Proves the deliverable contract:
  - idempotent table creation (clean init)
  - one row per (station, date, signal), dedup + last-write-wins
  - idempotent re-run leaves exactly one row per key with latest values
  - provenance is date-tiered per the 2026-09-19 doctrine
  - index on (station, date) exists

All work happens in a temp DB (conftest sandbox forbids writing data/*.db).
"""

import os
import sqlite3
import tempfile
import unittest

from core.additive.base import AdditiveSignalResult
from core.additive import persistence


def _res(signal, offset=0.0, confidence=0.0, fired=False, reason="", meta=None):
    return AdditiveSignalResult(
        signal=signal,
        offset_f=offset,
        confidence=confidence,
        fired=fired,
        reason=reason,
        meta=meta or {},
    )


class PersistenceWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ── clean init ──────────────────────────────────────────────────────
    def test_clean_init_creates_table_and_index(self):
        conn = self._conn()
        try:
            persistence.ensure_table(conn)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn(persistence.TABLE_NAME, tables)

            index = (
                f"idx_{persistence.TABLE_NAME}_station_date"
            )
            idx_rows = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn(index, idx_rows)

            # Primary key covers (station, date, signal)
            pk = [
                row[1]
                for row in conn.execute(
                    f"PRAGMA table_info({persistence.TABLE_NAME})"
                )
                if row[5] > 0
            ]
            self.assertEqual(sorted(pk), ["date", "signal", "station"])
        finally:
            conn.close()

    def test_ensure_table_idempotent(self):
        conn = self._conn()
        try:
            persistence.ensure_table(conn)
            persistence.ensure_table(conn)
            persistence.ensure_table(conn)
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name=?",
                (persistence.TABLE_NAME,),
            ).fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    # ── dedup + last-write-wins ─────────────────────────────────────────
    def test_dedup_last_write_wins(self):
        conn = self._conn()
        try:
            key = ("KNYC", "2026-10-01")
            n1 = persistence.persist_results(
                conn, {key: [_res("cloud_clearing", offset=1.5,
                                  confidence=0.4, fired=True,
                                  reason="first", meta={"v": 1})]}
            )
            conn.commit()
            self.assertEqual(n1, 1)

            # Re-evaluate the same key with different values.
            n2 = persistence.persist_results(
                conn, {key: [_res("cloud_clearing", offset=-2.0,
                                  confidence=0.8, fired=False,
                                  reason="second", meta={"v": 2})]}
            )
            conn.commit()
            self.assertEqual(n2, 1)

            rows = persistence.load_results(conn)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["offset_f"], -2.0)
            self.assertEqual(row["confidence"], 0.8)
            self.assertFalse(row["fired"])
            self.assertEqual(row["reason"], "second")
            self.assertEqual(row["meta"], {"v": 2})
        finally:
            conn.close()

    def test_one_row_per_station_date_signal(self):
        conn = self._conn()
        try:
            persistence.persist_results(conn, {
                ("KATL", "2026-10-01"): [
                    _res("cloud_clearing", fired=True),
                    _res("dryline_cooling", fired=False),
                ],
                ("KATL", "2026-10-02"): [
                    _res("cloud_clearing", fired=False),
                ],
            })
            conn.commit()
            self.assertEqual(persistence.count_rows(conn), 3)
            rows = conn.execute(
                f"SELECT signal, COUNT(*) FROM {persistence.TABLE_NAME} "
                "GROUP BY signal ORDER BY signal"
            ).fetchall()
            self.assertEqual(rows, [("cloud_clearing", 2), ("dryline_cooling", 1)])
        finally:
            conn.close()

    def test_idempotent_rerun_same_values(self):
        conn = self._conn()
        try:
            key = ("KSFO", "2026-10-02")
            results = {key: [_res("ramp_acceleration", offset=0.5,
                                  confidence=0.6, fired=True)]}
            persistence.persist_results(conn, results)
            conn.commit()
            first = persistence.load_results(conn)

            # Identical re-run — must not add rows, values unchanged.
            persistence.persist_results(conn, results)
            conn.commit()
            second = persistence.load_results(conn)

            self.assertEqual(len(second), 1)
            self.assertEqual(persistence.count_rows(conn), 1)
            self.assertEqual(
                (first[0]["offset_f"], first[0]["confidence"]),
                (second[0]["offset_f"], second[0]["confidence"]),
            )
        finally:
            conn.close()

    # ── provenance doctrine ─────────────────────────────────────────────
    def test_provenance_date_tiering(self):
        self.assertEqual(persistence.provenance_for_date("2026-09-12"), "ghcn")
        self.assertEqual(
            persistence.provenance_for_date("2026-09-13"), "kalshi_finalized"
        )
        self.assertEqual(
            persistence.provenance_for_date("2026-09-16"), "kalshi_finalized"
        )
        self.assertEqual(
            persistence.provenance_for_date("2026-09-17"), "weatherapi"
        )
        self.assertEqual(persistence.provenance_for_date("2025-01-01"), "ghcn")

    def test_every_row_carries_provenance(self):
        conn = self._conn()
        try:
            persistence.persist_results(conn, {
                ("KBOS", "2026-09-10"): [_res("cloud_clearing")],
                ("KBOS", "2026-09-15"): [_res("cloud_clearing")],
                ("KBOS", "2026-09-20"): [_res("cloud_clearing")],
            })
            conn.commit()
            provs = dict(conn.execute(
                f"SELECT date, provenance FROM {persistence.TABLE_NAME}"
            ).fetchall())
            self.assertEqual(provs["2026-09-10"], "ghcn")
            self.assertEqual(provs["2026-09-15"], "kalshi_finalized")
            self.assertEqual(provs["2026-09-20"], "weatherapi")
        finally:
            conn.close()

    def test_explicit_provenance_override(self):
        conn = self._conn()
        try:
            persistence.persist_results(
                conn, {("KMDW", "2026-10-01"): [_res("cloud_clearing")]},
                provenance="simulated",
            )
            conn.commit()
            row = persistence.load_results(conn)[0]
            self.assertEqual(row["provenance"], "simulated")
        finally:
            conn.close()

    def test_updated_at_and_meta_roundtrip(self):
        conn = self._conn()
        try:
            persistence.persist_results(
                conn,
                {("KMIA", "2026-10-01"): [
                    _res("gust_front_pressure", fired=True,
                         meta={"auc": 0.71, "tags": ["a", "b"]})
                ]},
                updated_at="2026-10-03T12:00:00+00:00",
            )
            conn.commit()
            row = persistence.load_results(conn)[0]
            self.assertEqual(row["updated_at"], "2026-10-03T12:00:00+00:00")
            self.assertEqual(row["meta"], {"auc": 0.71, "tags": ["a", "b"]})
        finally:
            conn.close()

    def test_empty_input_is_noop(self):
        conn = self._conn()
        try:
            n = persistence.persist_results(conn, {})
            conn.commit()
            self.assertEqual(n, 0)
            self.assertEqual(persistence.count_rows(conn), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
