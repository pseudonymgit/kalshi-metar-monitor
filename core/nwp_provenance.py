#!/usr/bin/env python3
"""
NWP run-provenance + as-of accessor (Sprint B / I4).

The NWP forecast tables (`data/nwp_forecasts.db`) historically stored only
`fetch_date` (a DATE) and `fetch_timestamp`, with no record of WHICH model run
(cycle) a row came from.  Without a run-initialisation instant, no backtest can
prove which model run was visible at decision time, so every read is
lookahead-by-construction — the same class of defect as the A6 provisional-truth
bug, but in the NWP layer.

This module closes that hole by adding two provenance columns and a **fail-closed
as-of accessor**:

  run_init_utc : the model cycle the row was produced from, as a full ISO-8601
                 UTC instant (e.g. GEFS 00Z -> '2026-10-11T00:00:00+00:00').
                 NULL for pre-migration rows (unknown vintage) — never fabricated
                 by default.
  fetched_at   : when we retrieved the row from upstream, ISO-8601 UTC.
                 Backfilled from the legacy `fetch_timestamp` column (identical
                 semantics; `fetch_timestamp` is retained for compatibility).

As-of semantics
---------------
Given a decision instant `t`, only rows whose model run was initialised at or
before `t` may be used.  The predicate is *fail closed*:

  * a row with NULL `run_init_utc` is EXCLUDED (unknown vintage is not evidence);
  * a row whose `run_init_utc` is after `t` is EXCLUDED (that is lookahead);
  * `strict=True` (the default) REQUIRES both `run_init_utc` and `fetched_at`
    to be present and <= `t`;
  * `strict=False` additionally admits legacy rows that carry only `fetched_at`
    (a documented, conservative fallback), still excluding both-NULL rows.

`require_provenance=True` on `query_asof` raises `ProvenanceMissingError` when the
provenance columns are absent entirely, rather than silently returning a narrower
(possibly empty) set.

Writers populate `run_init_utc` at write time via `run_init_utc_from_cycle` — the
model cycle the fetched data belongs to.  Deterministic, stdlib only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Column / table contract
# ---------------------------------------------------------------------------

NWP_TABLE = "nwp_forecasts"
RUN_INIT_COLUMN = "run_init_utc"
FETCHED_AT_COLUMN = "fetched_at"
LEGACY_FETCH_TS_COLUMN = "fetch_timestamp"

# Default model cycles for the global NWP models stored by nwp_collect.py
# (GFS / ECMWF / ICON / GEM) and the ensemble collectors.  A fetch made at
# 06:12Z is attributed to the 06Z cycle, etc.  Decline-hour flooring is
# conservative: the run's initialisation instant is never claimed to be LATER
# than it actually is at the granularity of the cycle.
DEFAULT_CYCLE_HOURS_UTC = (0, 6, 12, 18)


class ProvenanceMissingError(RuntimeError):
    """Raised when an as-of read is requested but provenance columns are absent.

    Fail closed: a caller that demands provenance must not receive a silently
    narrowed result set.
    """


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _to_utc(dt).isoformat()


def resolve_decision_instant(instant: Any = None) -> str:
    """Normalise a decision instant to an ISO-8601 UTC string.

    Accepts ``None`` (now), a ``datetime``, or an ISO-8601 string.  Naive
    datetimes are interpreted as UTC.  Lexicographic comparison of the returned
    zero-padded UTC ISO strings is chronological.
    """
    if instant is None:
        return _iso(datetime.now(timezone.utc))
    if isinstance(instant, datetime):
        return _iso(instant)
    if isinstance(instant, str):
        s = instant.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return _iso(datetime.fromisoformat(s))
        except ValueError:
            # Fall back to raw string (caller supplied an already-comparable
            # zero-padded ISO value, e.g. 'YYYY-MM-DDTHH:MM:SS+00:00').
            return instant
    raise TypeError(f"Unsupported decision instant: {instant!r}")


def run_init_utc_from_cycle(fetch_dt: Optional[datetime] = None,
                            cycle_hours: Sequence[int] = DEFAULT_CYCLE_HOURS_UTC) -> str:
    """Return the model-cycle initialisation instant for a fetch time.

    Floors `fetch_dt` (default: now UTC) to the most recent cycle hour.  Writers
    call this to stamp `run_init_utc` at insert time.
    """
    dt = _to_utc(fetch_dt)
    hours = sorted({int(h) % 24 for h in cycle_hours})
    chosen = hours[0]
    for h in hours:
        if dt.hour >= h:
            chosen = h
        else:
            break
    floored = dt.replace(hour=chosen, minute=0, second=0, microsecond=0)
    if chosen > dt.hour:
        # No cycle earlier than fetch hour in the same day -> previous day's last
        # cycle (only reachable for sparse cycle_hours sets).
        floored -= timedelta(days=1)
    return _iso(floored)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _table_columns(conn: sqlite3.Connection, table: str = NWP_TABLE) -> Dict[str, str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in cur.fetchall()}


def provenance_present(conn: sqlite3.Connection, table: str = NWP_TABLE) -> bool:
    """True when BOTH provenance columns exist on `table`."""
    cols = _table_columns(conn, table)
    return RUN_INIT_COLUMN in cols and FETCHED_AT_COLUMN in cols


def ensure_provenance_columns(conn: sqlite3.Connection, table: str = NWP_TABLE) -> Dict[str, str]:
    """Add the provenance columns when missing.  Idempotent; never rewrites rows.

    Returns a dict mapping column name -> 'added' or 'present'.  `ALTER TABLE
    ADD COLUMN` with no default is an O(1) schema change in SQLite, so this is
    safe on the 400MB live table.
    """
    cols = _table_columns(conn, table)
    status: Dict[str, str] = {}
    if RUN_INIT_COLUMN not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {RUN_INIT_COLUMN} TEXT")
        status[RUN_INIT_COLUMN] = "added"
    else:
        status[RUN_INIT_COLUMN] = "present"
    if FETCHED_AT_COLUMN not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {FETCHED_AT_COLUMN} TEXT")
        status[FETCHED_AT_COLUMN] = "added"
    else:
        status[FETCHED_AT_COLUMN] = "present"
    conn.commit()
    return status


def backfill_fetched_at(conn: sqlite3.Connection, table: str = NWP_TABLE) -> int:
    """Populate `fetched_at` from the legacy `fetch_timestamp` column.

    Only fills rows where `fetched_at IS NULL` (idempotent, never overwrites an
    explicit value).  Returns the number of rows updated.  `fetched_at` and
    `fetch_timestamp` are semantically identical (both = retrieval time).
    """
    cur = conn.execute(
        f"""UPDATE {table}
            SET {FETCHED_AT_COLUMN} = {LEGACY_FETCH_TS_COLUMN}
            WHERE {FETCHED_AT_COLUMN} IS NULL
              AND {LEGACY_FETCH_TS_COLUMN} IS NOT NULL"""
    )
    conn.commit()
    return cur.rowcount


def backfill_run_init_utc_derived(conn: sqlite3.Connection,
                                  table: str = NWP_TABLE,
                                  cycle_hours: Sequence[int] = DEFAULT_CYCLE_HOURS_UTC,
                                  dry_run: bool = False) -> int:
    """OPT-IN best-effort derivation of `run_init_utc` for legacy rows.

    Derives the run-init by flooring `fetched_at` to the most recent cycle hour.
    This is a *derived* vintage, not a recorded one: it can attribute a row to a
    cycle a few hours later than the true initialisation, which fails CLOSED for
    as-of reads (it can only hide data, never expose it early).  Left OFF by
    default so unattributed rows stay honestly NULL.

    Returns the number of rows updated (or that WOULD be updated when dry_run).
    """
    where = (f"{RUN_INIT_COLUMN} IS NULL AND {FETCHED_AT_COLUMN} IS NOT NULL")
    rows = conn.execute(
        f"SELECT id, {FETCHED_AT_COLUMN} FROM {table} WHERE {where}"
    ).fetchall()
    updates = []
    for rid, fetched in rows:
        try:
            ts = fetched[:-1] + "+00:00" if isinstance(fetched, str) and fetched.endswith("Z") else fetched
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        updates.append((run_init_utc_from_cycle(dt, cycle_hours), rid))
    if dry_run:
        return len(updates)
    if updates:
        conn.executemany(
            f"UPDATE {table} SET {RUN_INIT_COLUMN} = ? WHERE id = ?", updates
        )
        conn.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# As-of accessor
# ---------------------------------------------------------------------------

ASOF_PREDICATE_STRICT = (
    f" AND {RUN_INIT_COLUMN} IS NOT NULL AND {RUN_INIT_COLUMN} <= ?"
    f" AND {FETCHED_AT_COLUMN} IS NOT NULL AND {FETCHED_AT_COLUMN} <= ?"
)
# Legacy fallback: admit rows with only `fetched_at` (both-NULL still excluded
# because COALESCE yields NULL and NULL <= ? is NULL/false).
ASOF_PREDICATE_FALLBACK = (
    f" AND {FETCHED_AT_COLUMN} IS NOT NULL AND {FETCHED_AT_COLUMN} <= ?"
    f" AND COALESCE({RUN_INIT_COLUMN}, {FETCHED_AT_COLUMN}) <= ?"
)


def asof_clause(strict: bool = True) -> str:
    """Return the SQL as-of fragment (binds the decision instant TWICE).

    Append it to a WHERE clause; the caller binds the same decision instant for
    both placeholders.
    """
    return ASOF_PREDICATE_STRICT if strict else ASOF_PREDICATE_FALLBACK


def asof_params(decision_instant: Any = None, strict: bool = True) -> tuple:
    """Bind parameters matching `asof_clause` (decision instant, twice)."""
    bound = resolve_decision_instant(decision_instant)
    return (bound, bound)


def is_provenance_visible(run_init_utc: Optional[str],
                          fetched_at: Optional[str],
                          decision_instant: Any = None,
                          strict: bool = True) -> bool:
    """Pure-Python predicate: was this row visible at `decision_instant`?

    Fail closed — a row with missing provenance is never visible unless
    `strict=False` and it carries `fetched_at`.
    """
    bound = resolve_decision_instant(decision_instant)
    if fetched_at is None:
        return False
    if fetched_at > bound:
        return False
    if strict:
        if run_init_utc is None:
            return False
        if run_init_utc > bound:
            return False
        return True
    # Non-strict fallback: run_init unknown -> fall back to fetched_at.
    vintage = run_init_utc if run_init_utc is not None else fetched_at
    return vintage <= bound


def filter_rows_asof(rows: Iterable[Dict[str, Any]],
                     decision_instant: Any = None,
                     strict: bool = True) -> List[Dict[str, Any]]:
    """Filter an in-memory iterable of row dicts to the as-of-visible subset."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if is_provenance_visible(row.get(RUN_INIT_COLUMN),
                                 row.get(FETCHED_AT_COLUMN),
                                 decision_instant, strict=strict):
            out.append(row)
    return out


def query_asof(conn: sqlite3.Connection,
               decision_instant: Any = None,
               station: Optional[str] = None,
               target_date: Optional[str] = None,
               model: Optional[str] = None,
               variable: Optional[str] = None,
               strict: bool = True,
               require_provenance: bool = True,
               table: str = NWP_TABLE) -> List[Dict[str, Any]]:
    """Return only forecasts whose provenance is at-or-before `decision_instant`.

    Fail closed:
      * raises `ProvenanceMissingError` when `require_provenance=True` and the
        provenance columns do not exist (no silent narrowed result);
      * rows with missing/NULL provenance are excluded.
    """
    if not provenance_present(conn, table):
        if require_provenance:
            raise ProvenanceMissingError(
                f"{table} has no provenance columns ({RUN_INIT_COLUMN}, "
                f"{FETCHED_AT_COLUMN}); run ensure_provenance_columns() first. "
                f"Refusing an unattributed as-of read."
            )
        return []

    where = ["1=1"]
    params: List[Any] = []
    if station is not None:
        where.append("station = ?")
        params.append(station)
    if target_date is not None:
        where.append("target_date = ?")
        params.append(target_date)
    if model is not None:
        where.append("LOWER(model) = ?")
        params.append(model.lower())
    if variable is not None:
        where.append("variable = ?")
        params.append(variable)

    sql = (f"SELECT id, fetch_date, target_date, station, model, variable, value, "
           f"{LEGACY_FETCH_TS_COLUMN}, {RUN_INIT_COLUMN}, {FETCHED_AT_COLUMN} "
           f"FROM {table} WHERE " + " AND ".join(where) + asof_clause(strict))
    params.extend(asof_params(decision_instant, strict))

    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


__all__ = [
    "NWP_TABLE",
    "RUN_INIT_COLUMN",
    "FETCHED_AT_COLUMN",
    "LEGACY_FETCH_TS_COLUMN",
    "DEFAULT_CYCLE_HOURS_UTC",
    "ProvenanceMissingError",
    "resolve_decision_instant",
    "run_init_utc_from_cycle",
    "provenance_present",
    "ensure_provenance_columns",
    "backfill_fetched_at",
    "backfill_run_init_utc_derived",
    "asof_clause",
    "asof_params",
    "is_provenance_visible",
    "filter_rows_asof",
    "query_asof",
]
