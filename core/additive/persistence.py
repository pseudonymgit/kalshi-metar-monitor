"""
persistence.py — G.8 signal-result persistence writer (M1 calibration sprint).

Durable per-(station, date, signal) store of additive signal evaluations so
downstream calibration (M2 outcome join, M4 receipts, M6 regression, M8 rerun)
can query evaluated signals instead of relying on transient in-memory lists.

Storage
-------
DB:    data/signal_cache.db   (the intraday-infra DB; its `signal_cache` table
                              already keys on (station, date, signal), so this
                              reuses the same convention and writer discipline).
Table: additive_signal_results  (new; does NOT touch the existing `signal_cache`
                              table or any other table in that DB).

Guarantees
----------
  - Idempotent table creation (CREATE TABLE IF NOT EXISTS) + (station, date) index.
  - UPSERT on (station, date, signal): dedup + last-write-wins via
    INSERT ... ON CONFLICT(station, date, signal) DO UPDATE.
  - Re-running the same (station, date) leaves exactly one row per key.
  - M4 fired-once latch (opt-in via fired_once_latch=True): because the key
    has no hour dimension, a quiet later cycle must not overwrite a productive
    earlier one. When enabled, an incoming fired=0 / confidence=0 row never
    downgrades an existing fired=1 / confidence>0 row for the same key — only
    reason/meta/updated_at are refreshed and the productive state is preserved.
  - provenance on every row (date-tiered per Dan's 2026-09-19 ruling).
  - Deterministic only — NO AI/ML. No look-ahead; this module never reads
    observations, it only persists already-computed evaluations.

Date-tiered provenance doctrine (Dan, 2026-09-19):
  GHCN        -> through 2026-09-12
  Kalshi      -> 2026-09-13 .. 2026-09-16 (finalized)
  WeatherAPI  -> after 2026-09-16 (current archive)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from .base import AdditiveSignalResult
from .paths import DATA_DIR

logger = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME",
    "DEFAULT_DB_PATH",
    "GHCN_PROVENANCE_CUTOFF",
    "KALSHI_PROVENANCE_CUTOFF",
    "ensure_table",
    "provenance_for_date",
    "persist_results",
    "persist_results_to_db",
    "load_results",
    "count_rows",
]

TABLE_NAME = "additive_signal_results"
DEFAULT_DB_PATH = DATA_DIR / "signal_cache.db"

GHCN_PROVENANCE_CUTOFF = "2026-09-12"
KALSHI_PROVENANCE_CUTOFF = "2026-09-16"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    station    TEXT NOT NULL,
    date       TEXT NOT NULL,
    signal     TEXT NOT NULL,
    offset_f   REAL,
    confidence REAL,
    fired      INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    meta       TEXT,
    provenance TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station, date, signal)
);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_station_date
    ON {TABLE_NAME} (station, date);
"""

_COLUMNS = [
    "station", "date", "signal", "offset_f", "confidence",
    "fired", "reason", "meta", "provenance", "updated_at",
]


def ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the results table + index. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


def provenance_for_date(date_str: str) -> str:
    """Map a YYYY-MM-DD date to its date-tiered provenance label."""
    if date_str <= GHCN_PROVENANCE_CUTOFF:
        return "ghcn"
    if date_str <= KALSHI_PROVENANCE_CUTOFF:
        return "kalshi_finalized"
    return "weatherapi"


def _meta_json(meta: Optional[dict]) -> str:
    try:
        return json.dumps(meta or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _existing_states(
    conn: sqlite3.Connection,
    keys: List[tuple],
) -> Dict[tuple, tuple]:
    """Return {(station, date, signal): (offset_f, confidence, fired)} for keys
    already present in the table. Missing keys are simply absent from the dict.

    Used by the M4 fired-once latch: a quiet evaluation cycle must not downgrade
    a productive one for the same (station, date, signal) key. The table has no
    hour dimension, so without this check a later zero-write overwrites it.
    """
    if not keys:
        return {}
    placeholders = ",".join(["(?,?,?)"] * len(keys))
    params = [v for key in keys for v in key]
    try:
        rows = conn.execute(
            f"SELECT station, date, signal, offset_f, confidence, fired "
            f"FROM {TABLE_NAME} "
            f"WHERE (station, date, signal) IN (VALUES {placeholders})",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        # Older SQLite without row-value IN (VALUES ...) support — fall back.
        rows = []
        for (st, dt, sg) in keys:
            rows.extend(conn.execute(
                f"SELECT station, date, signal, offset_f, confidence, fired "
                f"FROM {TABLE_NAME} WHERE station=? AND date=? AND signal=?",
                (st, dt, sg),
            ).fetchall())
    return {(r[0], r[1], r[2]): (r[3], r[4], int(r[5] or 0)) for r in rows}


def persist_results(
    conn: sqlite3.Connection,
    results_by_day: Dict[tuple, List[AdditiveSignalResult]],
    provenance: Optional[str] = None,
    updated_at: Optional[str] = None,
    fired_once_latch: bool = False,
) -> int:
    """
    UPSERT one row per evaluated (station, date, signal).

    Args:
        conn: open sqlite3 connection (caller owns transaction/commit).
        results_by_day: {(station, date): [AdditiveSignalResult, ...]} — the
            same shape produced by the additive stack. Date is 'YYYY-MM-DD'.
        provenance: explicit provenance label; when None, derived per-date from
            the date-tiered doctrine. Non-matching keys fall back to the
            date tier of their own date.
        updated_at: ISO timestamp override; defaults to now (UTC).
        fired_once_latch: M4 fired-once latch. When True, an incoming row may
            NOT downgrade an existing row for the same (station, date, signal)
            from fired=1 -> 0 or confidence>0 -> 0. On such a downgrade the
            durable productive state (offset_f, confidence, fired) is preserved
            and only reason/meta/updated_at (and provenance) are refreshed.
            Defaults to False to preserve unconditional last-write-wins for
            callers that need it; the intraday runner's M1 persist block opts in.

    Returns:
        Number of rows written (attempted UPSERTs; last-write-wins per key).
    """
    ensure_table(conn)
    now = updated_at or datetime.now(timezone.utc).isoformat()

    rows = []
    for (station, date), results in results_by_day.items():
        station = str(station)
        date = str(date)
        prov = provenance or provenance_for_date(date)
        for r in results:
            rows.append((
                station,
                date,
                str(r.signal),
                float(r.offset_f),
                float(r.confidence),
                1 if r.fired else 0,
                r.reason or "",
                _meta_json(r.meta),
                prov,
                now,
            ))

    if not rows:
        return 0

    # M4 fired-once latch: never let a quiet cycle downgrade a productive one.
    if fired_once_latch:
        existing = _existing_states(
            conn, [(r[0], r[1], r[2]) for r in rows]
        )
        for i, r in enumerate(rows):
            prev = existing.get((r[0], r[1], r[2]))
            if prev is None:
                continue
            prev_offset, prev_conf, prev_fired = prev
            cur_conf = float(r[4])
            cur_fired = int(r[5])
            downgrade = (prev_fired == 1 and cur_fired == 0) or (
                prev_conf is not None
                and float(prev_conf or 0.0) > 0.0
                and cur_conf <= 0.0
            )
            if downgrade:
                # Preserve the durable productive state; refresh only
                # reason / meta / updated_at / provenance.
                rows[i] = (
                    r[0], r[1], r[2],
                    prev_offset if prev_offset is not None else r[3],
                    prev_conf if prev_conf is not None else r[4],
                    prev_fired,
                    r[6], r[7], r[8], r[9],
                )

    conn.executemany(
        f"""
        INSERT INTO {TABLE_NAME}
            (station, date, signal, offset_f, confidence, fired, reason, meta,
             provenance, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station, date, signal) DO UPDATE SET
            offset_f   = excluded.offset_f,
            confidence = excluded.confidence,
            fired      = excluded.fired,
            reason     = excluded.reason,
            meta       = excluded.meta,
            provenance = excluded.provenance,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    return len(rows)


def persist_results_to_db(
    results_by_day: Dict[tuple, List[AdditiveSignalResult]],
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
    provenance: Optional[str] = None,
    fired_once_latch: bool = False,
) -> int:
    """Convenience wrapper: open `db_path`, UPSERT, commit, close."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        n = persist_results(
            conn, results_by_day, provenance=provenance,
            fired_once_latch=fired_once_latch,
        )
        conn.commit()
        return n
    finally:
        conn.close()


def load_results(
    conn: sqlite3.Connection,
    station: Optional[str] = None,
    date: Optional[str] = None,
    signal: Optional[str] = None,
) -> List[dict]:
    """Read persisted rows (optionally filtered) ordered by key."""
    ensure_table(conn)
    clauses, params = [], []
    if station:
        clauses.append("station = ?")
        params.append(station)
    if date:
        clauses.append("date = ?")
        params.append(date)
    if signal:
        clauses.append("signal = ?")
        params.append(signal)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE_NAME}{where} "
        f"ORDER BY station, date, signal",
        params,
    ).fetchall()

    out = []
    for r in rows:
        d = dict(zip(_COLUMNS, r))
        try:
            d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
        except (TypeError, ValueError):
            d["meta"] = {}
        d["fired"] = bool(d["fired"])
        out.append(d)
    return out


def count_rows(conn: sqlite3.Connection) -> int:
    """Total persisted result rows."""
    ensure_table(conn)
    return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0])
