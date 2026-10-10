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

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.settlement_labels import strike_crossing_yes

__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_DDL",
    "CANONICAL_TEMP_SOURCE",
    "ensure_snapshot_table",
    "freeze_snapshot",
    "read_snapshot",
    "canonical_settle_yes",
    "canonical_outcome",
    "verify_regeneration_invariants",
    "ImmutableSettlementError",
]

SNAPSHOT_TABLE = "settlement_snapshot"
CANONICAL_TEMP_SOURCE = "kalshi_live_api"

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


def canonical_settle_yes(settlement_temp_f: float, strike_price: float,
                         market_type: str = "HIGH") -> int:
    """Canonical strike-crossing YES label (1/0). HIGH: temp > strike (strict)."""
    return 1 if strike_crossing_yes(settlement_temp_f, strike_price, market_type) else 0


def canonical_outcome(signal_direction: str, market_type: str, settlement_temp_f: float,
                      strike_price: float) -> str:
    """'correct'/'incorrect' from the STRICT strike-crossing axis.

    HIGH: YES (ITM) iff temp > strike; LOW: YES iff temp < strike. A trade whose
    signal_direction predicted the ITM side is 'correct'. Equality == not-ITM.
    """
    itm = canonical_settle_yes(settlement_temp_f, strike_price, market_type) == 1
    if (market_type or "HIGH").upper() == "HIGH":
        actual = "UP" if itm else "DOWN"
    else:  # LOW: ITM (temp < strike) means the DOWN side won
        actual = "DOWN" if itm else "UP"
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
