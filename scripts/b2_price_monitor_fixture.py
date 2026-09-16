#!/usr/bin/env python3
"""Offline staging capture for the Q-B2 price provenance boundary.

This module deliberately accepts fixture rows only and writes only to the
caller-provided staging database. It never invokes the live collector, network,
or the canonical/live database path. Consumers should run the read-only B2 gate
against the resulting staging database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.z8_kalshi_market_cron import build_price_record


_SCHEMA = """
CREATE TABLE market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time TEXT NOT NULL,
    station TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    strike_temp REAL,
    strike_source_field TEXT,
    strike_provenance_status TEXT,
    market_type TEXT,
    yes_bid REAL,
    yes_ask REAL,
    mid_price REAL,
    price_unit TEXT,
    bid_source_field TEXT,
    ask_source_field TEXT,
    api_contract_version TEXT,
    provenance_status TEXT
)
"""


def capture_fixture_rows(
    staging_db: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    api_contract_version: str | None = None,
) -> int:
    """Capture validated fixture quotes into a caller-owned staging DB.

    The input is a synthetic fixture boundary, not a collector response. A
    missing version is intentionally passed through to ``build_price_record``
    and stored as UNKNOWN_UNVERIFIED/unverified; malformed quotes are skipped.
    """
    path = Path(staging_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted = 0
    with sqlite3.connect(path) as conn:
        conn.execute(_SCHEMA)
        for row in rows:
            prices = build_price_record(dict(row.get("market", row)), api_contract_version)
            if prices is None:
                continue
            conn.execute(
                """INSERT INTO market_snapshots
                (snapshot_time, station, series_ticker, market_ticker,
                 strike_temp, strike_source_field, strike_provenance_status,
                 market_type, yes_bid, yes_ask, mid_price, price_unit,
                 bid_source_field, ask_source_field, api_contract_version,
                 provenance_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["snapshot_time"], row["station"], row["series_ticker"],
                    row["market_ticker"], row.get("strike_temp"),
                    row.get("strike_source_field", "strike_price"),
                    row.get("strike_provenance_status", "verified"),
                    row.get("market_type", "binary"), prices["yes_bid"],
                    prices["yes_ask"], prices["mid_price"], prices["price_unit"],
                    prices["bid_source_field"], prices["ask_source_field"],
                    prices["api_contract_version"], prices["provenance_status"],
                ),
            )
            inserted += 1
    return inserted
