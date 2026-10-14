"""
outcomes.py — G.8 T+1 outcome resolver (M2 calibration sprint).

Joins each persisted (station, date, signal) evaluation row from M1
(`additive_signal_results`, see persistence.py) to the realized OUTCOME of the
next trading day, carrying a date-tiered `provenance` tag on the outcome.

Realized outcome
----------------
For a signal evaluated on date D, the outcome is the realized settlement value
on the NEXT trading day D' = next_trading_day(D) (Kalshi HIGH/LOW markets settle
the next business day — see core/kalshi_calendar.py). We persist the realized
day-high/low temperature and, when a strike is available, the strict
strike-crossing label (core/settlement_labels.py; equality == loss).

  AXIS: every row is labelled `axis = 'strike_crossing'` (strike-crossing, NOT
  day-over-day). `outcome_yes` is NULL when no strike price is known.

Truth tiers (date-tiered per Dan's 2026-09-19 ruling; the tier is keyed off the
OUTCOME date D', not the evaluation date D):
  ghcn             -> D' <= 2026-09-12   (settlement_truth temp_source='ghcn',
                                          daily_stats fallback)
  kalshi_finalized -> D' <= 2026-09-16   (kalshi_settlements source_type=
                                          'finalized', settlement_truth fallback)
  weatherapi       -> D' >  2026-09-16   (weatherapi_archive.daily, settlement_
                                          truth fallback)

Guarantees
----------
  - Idempotent table creation; UPSERT on (station, date, signal), last-write-wins
    (same discipline as M1).
  - Missing outcome -> row written with resolved=0, outcome fields NULL. Nothing
    is ever fabricated.
  - Deterministic only — NO AI/ML. Source DBs are opened READ-ONLY; the only
    write is to the new `additive_signal_outcomes` table.
  - NO look-ahead: the outcome is strictly T+1 relative to the evaluated date.

Storage
-------
DB:    data/signal_cache.db
Table: additive_signal_outcomes  (new; does not touch additive_signal_results
                                   nor any other table in that DB).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union

from .paths import METAR_DB, WEATHERAPI_DB, KALSHI_SETTLEMENTS_DB, DATA_DIR
from .persistence import (
    provenance_for_date,
    load_results as load_signal_results,
    ensure_table as ensure_results_table,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME",
    "DEFAULT_DB_PATH",
    "AXIS",
    "ensure_table",
    "next_trading_day",
    "provenance_for_date",
    "resolve_rows",
    "resolve_all",
    "load_outcomes",
    "count_rows",
    "OUTCOME_COLUMNS",
]

TABLE_NAME = "additive_signal_outcomes"
DEFAULT_DB_PATH = DATA_DIR / "signal_cache.db"
AXIS = "strike_crossing"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    station        TEXT NOT NULL,
    date           TEXT NOT NULL,
    signal         TEXT NOT NULL,
    resolved       INTEGER NOT NULL DEFAULT 0,
    outcome_date   TEXT,
    market_type    TEXT NOT NULL DEFAULT 'HIGH',
    outcome_temp_f REAL,
    strike_price   REAL,
    strike_type    TEXT,
    outcome_yes    INTEGER,
    axis           TEXT NOT NULL DEFAULT '{AXIS}',
    provenance     TEXT NOT NULL,
    reason         TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (station, date, signal)
);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_station_date
    ON {TABLE_NAME} (station, date);
"""

OUTCOME_COLUMNS = [
    "station", "date", "signal", "resolved", "outcome_date", "market_type",
    "outcome_temp_f", "strike_price", "strike_type", "outcome_yes",
    "axis", "provenance", "reason", "updated_at",
]


# ─── date helpers ────────────────────────────────────────────────────────

def _to_date(d: Union[str, date_cls]) -> date_cls:
    if isinstance(d, date_cls):
        return d
    return date_cls.fromisoformat(str(d)[:10])


def next_trading_day(d: Union[str, date_cls]) -> str:
    """Next Kalshi trading day after `d` as YYYY-MM-DD (settlement is T+1)."""
    from core.kalshi_calendar import get_next_trading_day
    return get_next_trading_day(_to_date(d)).isoformat()


# ─── read-only source connections ────────────────────────────────────────

def _open_ro(path: Union[str, Path]) -> Optional[sqlite3.Connection]:
    """Open a source DB read-only. Returns None if the file does not exist."""
    p = Path(path)
    if not p.exists():
        return None
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


# ─── per-tier outcome lookups (return dict or None) ──────────────────────

def _mk_outcome(temp_f, market_type="HIGH", strike_price=None, strike_type=None):
    out = {
        "market_type": market_type or "HIGH",
        "outcome_temp_f": float(temp_f),
        "strike_price": float(strike_price) if strike_price is not None else None,
        "strike_type": strike_type,
        "outcome_yes": None,
    }
    if out["strike_price"] is not None:
        from core.settlement_labels import strike_crossing_yes
        try:
            # Resolve from the market's OWN strike_type when the row carries it
            # (greater/less/between); the market_type fallback is greater-tail
            # only and mislabels an "or below" (-less) market.
            out["outcome_yes"] = 1 if strike_crossing_yes(
                out["outcome_temp_f"], out["strike_price"], out["market_type"],
                strike_type=strike_type,
            ) else 0
        except ValueError:
            out["outcome_yes"] = None
    return out


def _truth_row_from_settlement_truth(conn, station, market_type, date, temp_source):
    row = conn.execute(
        """
        SELECT settlement_temp_f, strike_price, strike_type
        FROM settlement_truth
        WHERE station=? AND market_type=? AND local_trading_date=?
          AND temp_source=? AND trustworthy=1
        LIMIT 1
        """,
        (station, market_type, date, temp_source),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return _mk_outcome(row[0], market_type, row[1], row[2])


def _lookup_ghcn(metar_conn, weatherapi_conn, station, market_type, date):
    """GHCN tier (<= 2026-09-12)."""
    if metar_conn is not None:
        o = _truth_row_from_settlement_truth(
            metar_conn, station, market_type, date, "ghcn"
        )
        if o is not None:
            return o
        if _has_table(metar_conn, "daily_stats"):
            col = "max_temp_f" if market_type.upper() == "HIGH" else "min_temp_f"
            row = metar_conn.execute(
                f"SELECT {col} FROM daily_stats WHERE station=? AND date_local=? "
                f"AND {col} IS NOT NULL LIMIT 1",
                (station, date),
            ).fetchone()
            if row and row[0] is not None:
                return _mk_outcome(row[0], market_type)
    return None


def _lookup_kalshi_finalized(metar_conn, kalshi_conn, station, market_type, date):
    """Kalshi finalized tier (2026-09-13 .. 2026-09-16)."""
    if kalshi_conn is not None and _has_table(kalshi_conn, "kalshi_settlements"):
        like = "%HIGH%" if market_type.upper() == "HIGH" else "%LOW%"
        row = kalshi_conn.execute(
            """
            SELECT kalshi_temp, series FROM kalshi_settlements
            WHERE station=? AND target_date=? AND source_type='finalized'
              AND kalshi_temp IS NOT NULL AND series LIKE ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (station, date, like),
        ).fetchone()
        if row and row[0] is not None:
            mt = "LOW" if "LOW" in str(row[1]).upper() else "HIGH"
            return _mk_outcome(row[0], mt)
    if metar_conn is not None:
        return _truth_row_from_settlement_truth(
            metar_conn, station, market_type, date, "kalshi_finalized"
        )
    return None


def _lookup_weatherapi(metar_conn, weatherapi_conn, station, market_type, date):
    """WeatherAPI tier (> 2026-09-16)."""
    if weatherapi_conn is not None and _has_table(weatherapi_conn, "daily"):
        col = "max_temp_f" if market_type.upper() == "HIGH" else "min_temp_f"
        row = weatherapi_conn.execute(
            f"SELECT {col} FROM daily WHERE station=? AND date=? "
            f"AND {col} IS NOT NULL LIMIT 1",
            (station, date),
        ).fetchone()
        if row and row[0] is not None:
            return _mk_outcome(row[0], market_type)
    if metar_conn is not None:
        return _truth_row_from_settlement_truth(
            metar_conn, station, market_type, date, "weatherapi"
        )
    return None


def _lookup_outcome(
    metar_conn, weatherapi_conn, kalshi_conn, station, market_type, date, tier
):
    if tier == "ghcn":
        return _lookup_ghcn(metar_conn, weatherapi_conn, station, market_type, date)
    if tier == "kalshi_finalized":
        return _lookup_kalshi_finalized(
            metar_conn, kalshi_conn, station, market_type, date
        )
    if tier == "weatherapi":
        return _lookup_weatherapi(
            metar_conn, weatherapi_conn, station, market_type, date
        )
    return None


# ─── schema ──────────────────────────────────────────────────────────────

def ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the outcomes table + index. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


# ─── resolver ────────────────────────────────────────────────────────────

def resolve_rows(
    conn: sqlite3.Connection,
    signal_rows: Optional[Sequence[dict]] = None,
    metar_db: Union[str, Path, None] = METAR_DB,
    weatherapi_db: Union[str, Path, None] = WEATHERAPI_DB,
    kalshi_db: Union[str, Path, None] = KALSHI_SETTLEMENTS_DB,
    market_type: str = "HIGH",
    calendar: Callable[[Union[str, date_cls]], str] = next_trading_day,
    updated_at: Optional[str] = None,
) -> int:
    """
    Resolve T+1 outcomes for persisted signal rows and UPSERT the outcome table.

    Args:
        conn: open sqlite3 connection to signal_cache.db (caller owns commit).
        signal_rows: rows to resolve (dicts with at least station/date/signal).
            When None, loads every row from the M1 `additive_signal_results`
            table in the same connection.
        metar_db / weatherapi_db / kalshi_db: source DB paths; opened read-only.
        market_type: HIGH (default) or LOW.
        calendar: D -> next trading day (YYYY-MM-DD). Injectable for tests.
        updated_at: ISO timestamp override; defaults to now (UTC).

    Returns:
        Number of rows written (attempted UPSERTs; last-write-wins per key).
    """
    ensure_table(conn)
    if signal_rows is None:
        ensure_results_table(conn)
        signal_rows = load_signal_results(conn)

    rows = list(signal_rows)
    if not rows:
        return 0

    now = updated_at or datetime.now(timezone.utc).isoformat()

    metar_conn = _open_ro(metar_db) if metar_db else None
    weatherapi_conn = _open_ro(weatherapi_db) if weatherapi_db else None
    kalshi_conn = _open_ro(kalshi_db) if kalshi_db else None

    payload = []
    try:
        for r in rows:
            station = str(r["station"])
            date = str(r["date"])
            signal = str(r["signal"])

            outcome_date = str(calendar(date))
            tier = provenance_for_date(outcome_date)
            o = _lookup_outcome(
                metar_conn, weatherapi_conn, kalshi_conn,
                station, market_type, outcome_date, tier,
            )

            if o is None:
                payload.append((
                    station, date, signal, 0, outcome_date, market_type,
                    None, None, None, None, AXIS, tier,
                    "no_outcome_at_T1", now,
                ))
            else:
                payload.append((
                    station, date, signal, 1, outcome_date, o["market_type"],
                    o["outcome_temp_f"], o["strike_price"], o["strike_type"],
                    o["outcome_yes"], AXIS, tier, None, now,
                ))
    finally:
        for c in (metar_conn, weatherapi_conn, kalshi_conn):
            if c is not None:
                c.close()

    conn.executemany(
        f"""
        INSERT INTO {TABLE_NAME}
            (station, date, signal, resolved, outcome_date, market_type,
             outcome_temp_f, strike_price, strike_type, outcome_yes,
             axis, provenance, reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station, date, signal) DO UPDATE SET
            resolved       = excluded.resolved,
            outcome_date   = excluded.outcome_date,
            market_type    = excluded.market_type,
            outcome_temp_f = excluded.outcome_temp_f,
            strike_price   = excluded.strike_price,
            strike_type    = excluded.strike_type,
            outcome_yes    = excluded.outcome_yes,
            axis           = excluded.axis,
            provenance     = excluded.provenance,
            reason         = excluded.reason,
            updated_at     = excluded.updated_at
        """,
        payload,
    )
    return len(payload)


def resolve_all(
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
    **kwargs,
) -> int:
    """Convenience: open signal_cache.db, resolve all M1 rows, commit, close."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        n = resolve_rows(conn, **kwargs)
        conn.commit()
        return n
    finally:
        conn.close()


# ─── readers ─────────────────────────────────────────────────────────────

def load_outcomes(
    conn: sqlite3.Connection,
    station: Optional[str] = None,
    date: Optional[str] = None,
    signal: Optional[str] = None,
    resolved_only: bool = False,
) -> List[dict]:
    """Read outcome rows (optionally filtered) ordered by key."""
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
    if resolved_only:
        clauses.append("resolved = 1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(
        f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM {TABLE_NAME}{where} "
        f"ORDER BY station, date, signal",
        params,
    ).fetchall()

    out = []
    for r in rows:
        d = dict(zip(OUTCOME_COLUMNS, r))
        d["resolved"] = bool(d["resolved"])
        if d["outcome_yes"] is not None:
            d["outcome_yes"] = bool(d["outcome_yes"])
        out.append(d)
    return out


def count_rows(conn: sqlite3.Connection) -> int:
    """Total outcome rows."""
    ensure_table(conn)
    return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0])


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    n = resolve_all()
    print(f"resolved {n} outcome rows")
