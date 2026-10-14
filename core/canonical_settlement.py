#!/usr/bin/env python3
"""
canonical_settlement.py — Canonical settlement truth + epoch-regeneration
immutability invariant.

Ruling: `.meta/continuity/weather-engine/settlement-truth-reconciliation-2026-10-10.md`.

Canonical settlement truth for a station-day is the Kalshi live API
`expiration_value` (cross-checked against GHCN). The booked `trades.settled_value`
was written by an unreliable settle-time read and is NOT canonical. This module
is the single source of truth for:

  1. the canonical strike-crossing label / outcome (HIGH: temp > strike),
  2. a **frozen settlement snapshot** — the canonical value is written once,
     timestamped with an as-of anchor (`settled_at_utc`), and can never change
     when the epoch table is regenerated (write-once + append-only triggers),
  3. the invariant check that fails CLOSED when a regeneration would move a
     frozen settlement value.

Invariant (room-adopted): *a settlement value cannot change when the epoch table
is regenerated.* `verify_regeneration_invariants()` is the enforcement point.

B-Mode compliant. Deterministic. No AI/ML. Does not mutate historical P&L.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.settlement_labels import strike_crossing_yes

__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_DDL",
    "CANONICAL_TEMP_SOURCE",
    "CANONICAL_TEMP_SOURCES",
    "is_canonical_temp_source",
    "ensure_snapshot_table",
    "freeze_snapshot",
    "read_snapshot",
    "load_canonical_snapshots",
    "resolve_canonical_settlement",
    "resolve_finalized_settlement",
    "resolve_settlement_truth",
    "KALSHI_SETTLEMENTS_DB",
    "FINALIZED_SOURCE",
    "assert_canonical_immutable",
    "canonical_settle_yes",
    "canonical_outcome",
    "outcome_from_market_result",
    "verify_regeneration_invariants",
    "ImmutableSettlementError",
]

SNAPSHOT_TABLE = "settlement_snapshot"
CANONICAL_TEMP_SOURCE = "kalshi_live_api"

# Step-2 source label: a Kalshi *finalized* settlement row (source_type='finalized')
# in `data/kalshi_settlements.db`. Same repo-relative default as
# `core.lane_goldilocks.DEFAULT_SETTLEMENTS_DB`; overridable for tests/ops.
FINALIZED_SOURCE = "kalshi_finalized"
KALSHI_SETTLEMENTS_DB = os.environ.get("KALSHI_SETTLEMENTS_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "kalshi_settlements.db")

# The ONLY `temp_source` values that are canonical settlement truth. Kalshi's
# live API `expiration_value` and its finalized settlement record both encode the
# official daily high. Every other value a snapshot row can carry (the backfill's
# `weatherapi` blend, `fallback_cross_check`, or any unknown/NULL provenance) is a
# PROVISIONAL proxy that must never be booked as a final settlement. Single-sourced
# here so the settle read path and any other consumer share one definition.
CANONICAL_TEMP_SOURCES = frozenset({CANONICAL_TEMP_SOURCE, "kalshi_finalized"})

# Frozen, append-only snapshot of the canonical settlement per station/market/day.
# PK is (station, market_type, local_trading_date). Triggers below make rows
# immutable so a later epoch regeneration (or any other writer) cannot move the
# value.
SNAPSHOT_DDL = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
    station            TEXT NOT NULL,
    market_type        TEXT NOT NULL,
    local_trading_date TEXT NOT NULL,
    settlement_temp_f  REAL NOT NULL,
    temp_source        TEXT NOT NULL,
    strike_price       REAL,
    strike_type        TEXT,
    settle_yes         INTEGER,
    settled_at_utc     TEXT,          -- as-of anchor (market finalization ts)
    frozen_at_utc      TEXT NOT NULL, -- when the snapshot was frozen
    PRIMARY KEY (station, market_type, local_trading_date)
);
CREATE TRIGGER IF NOT EXISTS trg_{SNAPSHOT_TABLE}_no_update
    BEFORE UPDATE ON {SNAPSHOT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{SNAPSHOT_TABLE} is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_{SNAPSHOT_TABLE}_no_delete
    BEFORE DELETE ON {SNAPSHOT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{SNAPSHOT_TABLE} is immutable'); END;
"""


class ImmutableSettlementError(RuntimeError):
    """Raised when a write or a regeneration would move a frozen settlement value."""


def ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the frozen snapshot table + immutability triggers."""
    conn.executescript(SNAPSHOT_DDL)
    conn.commit()


def is_canonical_temp_source(temp_source: Any) -> bool:
    """True iff `temp_source` marks canonical settlement truth.

    Canonical == `kalshi_live_api` (Kalshi live API expiration_value) or
    `kalshi_finalized` (Kalshi finalized settlement record). Anything else —
    including the `weatherapi` provisional blend, `fallback_cross_check`, an
    empty string, or None — is NOT final settlement truth and callers must fail
    closed. See `CANONICAL_TEMP_SOURCES`.
    """
    return temp_source in CANONICAL_TEMP_SOURCES


def canonical_settle_yes(settlement_temp_f: float, strike_price: float,
                         market_type: str = "HIGH",
                         strike_type: Optional[str] = None,
                         floor_strike: Optional[float] = None,
                         cap_strike: Optional[float] = None) -> int:
    """Canonical YES label (1/0), resolved from the market's OWN strike_type.

    Kalshi daily temperature markets offer BOTH an "or above" tail
    (``strike_type='greater'``: YES iff temp > floor) and an "or below" tail
    (``strike_type='less'``: YES iff temp < cap) for the same event; the `-T##`
    ticker is ambiguous between them. Pass ``strike_type`` and the label uses the
    market's own operator. When ``strike_type`` is omitted the legacy
    market_type fallback (HIGH => greater, LOW => less) is used — correct only
    for the greater-tail case. Both tails are STRICT (equality == not-ITM).
    """
    return 1 if strike_crossing_yes(
        settlement_temp_f, strike_price, market_type,
        strike_type, floor_strike, cap_strike) else 0


def canonical_outcome(signal_direction: str, market_type: str, settlement_temp_f: float,
                      strike_price: float, strike_type: Optional[str] = None,
                      floor_strike: Optional[float] = None,
                      cap_strike: Optional[float] = None) -> str:
    """'correct'/'incorrect' from the STRICT strike-crossing axis.

    ITM is resolved from the market's own ``strike_type`` when supplied
    (greater: temp > floor; less: temp < cap; between: floor<=temp<=cap), else
    from the legacy market_type fallback (HIGH: temp > strike; LOW: temp <
    strike). The HIGH/LOW mapping of ITM->signal token is preserved exactly as
    before: for a HIGH market 'UP' is the YES/ITM leg; for a LOW market 'DOWN'
    is. Equality == not-ITM.
    """
    itm = canonical_settle_yes(settlement_temp_f, strike_price, market_type,
                               strike_type, floor_strike, cap_strike) == 1
    if (market_type or "HIGH").upper() == "HIGH":
        actual = "UP" if itm else "DOWN"
    else:  # LOW: ITM (temp < strike) means the DOWN side won
        actual = "DOWN" if itm else "UP"
    return "correct" if str(signal_direction).upper() == actual else "incorrect"


def outcome_from_market_result(signal_direction: str, kalshi_result: str) -> str:
    """'correct'/'incorrect' from Kalshi's OWN settled ``result`` ('yes'/'no').

    The maximally honest label: it consumes the market's recorded settlement
    outcome instead of re-deriving it from temperature vs an assumed operator.
    ``signal_direction`` 'UP' == we took the YES leg; 'DOWN' == the NO leg.
    """
    r = str(kalshi_result).lower()
    if r not in ("yes", "no"):
        raise ValueError(f"kalshi_result must be yes|no, got {kalshi_result!r}")
    actual = "UP" if r == "yes" else "DOWN"
    return "correct" if str(signal_direction).upper() == actual else "incorrect"


def read_snapshot(conn: sqlite3.Connection, station: str, market_type: str,
                  local_trading_date: str) -> Optional[Dict[str, Any]]:
    """Read one frozen snapshot row (read-only). None when not frozen."""
    row = conn.execute(
        f"""SELECT station, market_type, local_trading_date, settlement_temp_f,
                   temp_source, strike_price, strike_type, settle_yes,
                   settled_at_utc, frozen_at_utc
              FROM {SNAPSHOT_TABLE}
             WHERE station=? AND market_type=? AND local_trading_date=?""",
        (station, market_type, local_trading_date),
    ).fetchone()
    if not row:
        return None
    keys = ("station", "market_type", "local_trading_date", "settlement_temp_f",
            "temp_source", "strike_price", "strike_type", "settle_yes",
            "settled_at_utc", "frozen_at_utc")
    return dict(zip(keys, row))


def freeze_snapshot(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]], *,
                    frozen_at_utc: str, conflict_raises: bool = True
                    ) -> Tuple[int, int, List[str]]:
    """Write canonical settlement values into the frozen snapshot. Write-once.

    `rows` items require: station, market_type, local_trading_date,
    settlement_temp_f (and optionally temp_source, strike_price, strike_type,
    settled_at_utc). `settle_yes` is derived when strike_price is present.

    Existing rows whose value is byte-identical are SKIPPED (idempotent). An
    existing row whose settlement value differs raises ImmutableSettlementError
    (fail closed) — a frozen settlement value must never move.
    """
    ensure_snapshot_table(conn)
    inserted = skipped = 0
    conflicts: List[str] = []
    for r in rows:
        key = (r["station"], r["market_type"], r["local_trading_date"])
        temp = r["settlement_temp_f"]
        existing = read_snapshot(conn, *key)
        if existing is not None:
            if existing["settlement_temp_f"] == temp:
                skipped += 1
                continue
            msg = (f"{key[0]}/{key[1]}/{key[2]} frozen={existing['settlement_temp_f']} "
                   f"!= new={temp}")
            conflicts.append(msg)
            if conflict_raises:
                raise ImmutableSettlementError(
                    "refusing to move a frozen settlement value: " + msg)
            continue
        strike = r.get("strike_price")
        settle_yes = r.get("settle_yes")
        if settle_yes is None and strike is not None:
            settle_yes = canonical_settle_yes(temp, strike, r["market_type"])
        conn.execute(
            f"""INSERT INTO {SNAPSHOT_TABLE}
                (station, market_type, local_trading_date, settlement_temp_f,
                 temp_source, strike_price, strike_type, settle_yes,
                 settled_at_utc, frozen_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r["station"], r["market_type"], r["local_trading_date"], temp,
             r.get("temp_source", CANONICAL_TEMP_SOURCE),
             strike, r.get("strike_type"), settle_yes,
             r.get("settled_at_utc"), frozen_at_utc),
        )
        inserted += 1
    conn.commit()
    return inserted, skipped, conflicts


def load_canonical_snapshots(metar_db: str) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Read the frozen canonical snapshot read-only; {} when the table is absent.

    Forward settle path: the writer must read canonical truth from the frozen
    `settlement_snapshot` (Kalshi live API / GHCN), NOT from the raw/epoch METAR
    extreme. Returns {(station, market_type, local_trading_date):
    {settlement_temp_f, temp_source}}, INCLUDING ONLY rows whose `temp_source` is
    canonical (`CANONICAL_TEMP_SOURCES`). A row from any other source is a
    provisional proxy and is omitted so the caller fails closed. Never raises on
    a missing/empty db — a missing snapshot is a *gap*, reported by the caller,
    not a crash.
    """
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    if not metar_db or not os.path.exists(metar_db):
        return out
    try:
        ro = sqlite3.connect(f"file:{metar_db}?mode=ro", uri=True)
        try:
            rows = ro.execute(
                f"SELECT station, market_type, local_trading_date, "
                f"settlement_temp_f, temp_source FROM {SNAPSHOT_TABLE}"
            ).fetchall()
        finally:
            ro.close()
    except sqlite3.Error:
        return out
    for st, mt, d, temp, src in rows:
        if temp is None:
            continue
        # Fail closed (2026-10-10): a row whose temp_source is not canonical is a
        # provisional proxy (e.g. the backfill's weatherapi blend), NOT final
        # settlement truth. Exclude it so the settle writer leaves the trade OPEN
        # instead of booking a value that will not self-heal when Kalshi finalizes.
        if not is_canonical_temp_source(src):
            continue
        out[(st, mt, d)] = {"settlement_temp_f": float(temp), "temp_source": src}
    return out


def resolve_canonical_settlement(conn: sqlite3.Connection, station: str,
                                 market_type: str, local_trading_date: str
                                 ) -> Optional[Dict[str, Any]]:
    """Canonical settlement for one station/market/day, or None when unfrozen or
    non-canonical.

    A thin, explicit read over the frozen snapshot so the settle writer has a
    single named entry point: canonical truth exists iff a snapshot row exists
    AND its `temp_source` is canonical. A row from a non-canonical source
    (weatherapi/provisional/fallback/unknown) returns None — the settle writer
    then fails closed and leaves the trade OPEN rather than booking a proxy.
    """
    snap = read_snapshot(conn, station, market_type, local_trading_date)
    if snap is None:
        return None
    if not is_canonical_temp_source(snap["temp_source"]):
        return None
    return {"settlement_temp_f": float(snap["settlement_temp_f"]),
            "temp_source": snap["temp_source"]}


def resolve_finalized_settlement(station: str, market_type: str,
                                 local_trading_date: str, *,
                                 kalshi_db: Optional[str] = None
                                 ) -> Optional[Dict[str, Any]]:
    """A4 promotion, step 2 of the settle resolution order.

    Canonical truth for a station-day may exist in a finalized form before it is
    frozen into `settlement_snapshot` (e.g. Oct 9, which the backfill froze from
    the `weatherapi` provisional source write-once). This reads the finalized
    Kalshi settlement row (`source_type='finalized'`) from
    `data/kalshi_settlements.db` and returns it labeled `kalshi_finalized`.

    HIGH only: the table carries the KXHIGH series; a LOW request returns None so
    the caller fails closed. Read-only; never raises on a missing/empty/locked db
    (a missing finalized row is a *gap* to fail closed on, not a crash).
    """
    if str(market_type).upper() != "HIGH":
        return None
    db = kalshi_db or KALSHI_SETTLEMENTS_DB
    if not db or not os.path.exists(db):
        return None
    try:
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = ro.execute(
                "SELECT kalshi_temp FROM kalshi_settlements WHERE station=? "
                "AND target_date=? AND source_type='finalized' "
                "AND kalshi_temp IS NOT NULL ORDER BY id DESC LIMIT 1",
                (station, str(local_trading_date)[:10]),
            ).fetchone()
        finally:
            ro.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return {"settlement_temp_f": float(row[0]), "temp_source": FINALIZED_SOURCE}


def resolve_settlement_truth(conn: sqlite3.Connection, station: str,
                             market_type: str, local_trading_date: str, *,
                             canonical_map: Optional[Dict] = None,
                             kalshi_db: Optional[str] = None
                             ) -> Tuple[Optional[Dict[str, Any]], str]:
    """A4 promotion — the single settle resolution order, fail-closed.

      1. canonical frozen snapshot row (`kalshi_live_api`/`kalshi_finalized`);
      2. finalized `kalshi_settlements` row (station/day, `source_type='finalized'`);
      3. fail closed (`None`) — NEVER book a provisional/snapshot proxy.

    Returns `(record, reason)` where `record` is None on failure and `reason`
    names which step resolved (or why the caller must leave the trade OPEN).

    `canonical_map` is a pre-loaded `load_canonical_snapshots()` result (the
    settle writer always supplies one). When it is given, step 1 is answered
    from it — a map that already contains canonical sources only. When it is
    None, `conn` is read as the snapshot DB (a real snapshot connection, not the
    trade connection) so a caller with a raw snapshot handle still resolves.
    """
    key = (station, market_type, local_trading_date)
    snap = None
    if canonical_map is not None:
        snap = canonical_map.get(key)
        if snap is not None and is_canonical_temp_source(snap.get("temp_source")):
            return snap, "canonical_snapshot"
    elif conn is not None:
        try:
            snap = resolve_canonical_settlement(conn, station, market_type,
                                                local_trading_date)
        except sqlite3.Error:
            snap = None
        if snap is not None:
            return snap, "canonical_snapshot"
    fin = resolve_finalized_settlement(station, market_type, local_trading_date,
                                       kalshi_db=kalshi_db)
    if fin is not None:
        return fin, "finalized_settlements_row"
    return None, "no_canonical_truth"


def assert_canonical_immutable(existing_value: Any, fetched_value: Any,
                              key: Any) -> None:
    """Fail loud when a canonical value would move.

    Invariant: once a canonical settlement value exists for a station/day, a
    later fetch must not move it. Both values may be None (nothing to compare).
    Raises ImmutableSettlementError on a real move.
    """
    if existing_value is None or fetched_value is None:
        return
    if float(existing_value) != float(fetched_value):
        raise ImmutableSettlementError(
            f"canonical settlement moved for {key}: "
            f"existing={existing_value} fetched={fetched_value}")


def verify_regeneration_invariants(conn: sqlite3.Connection,
                                   regenerated: Iterable[Dict[str, Any]]
                                   ) -> List[str]:
    """Enforce: a settlement value cannot change when the epoch table regenerates.

    `regenerated` is the candidate set a regeneration would write, each item
    carrying station/market_type/local_trading_date + settlement value under one
    of the keys `settlement_temp_f`/`settlement_bucket`/`epoch_bucket`. Returns a
    list of human-readable violations (empty == invariant holds). Callers must
    FAIL CLOSED on any non-empty result.
    """
    violations: List[str] = []
    for r in regenerated:
        val = r.get("settlement_temp_f")
        if val is None:
            val = r.get("settlement_bucket")
        if val is None:
            val = r.get("epoch_bucket")
        if val is None:
            continue
        frozen = read_snapshot(conn, r["station"], r["market_type"],
                               r["local_trading_date"])
        if frozen is None:
            continue  # nothing frozen -> nothing to protect
        if float(frozen["settlement_temp_f"]) != float(val):
            violations.append(
                f"{r['station']}/{r['market_type']}/{r['local_trading_date']}: "
                f"regeneration would move frozen {frozen['settlement_temp_f']} -> {val}")
    return violations
