#!/usr/bin/env python3
"""core/strike_axis.py — resolve a Kalshi market's OWN strike axis.

Why this exists (2026-10-14, external bug-hunt finding #3):
Kalshi daily temperature events publish TWO independent strike tails, and the
``-T##`` ticker suffix is used for BOTH:

  * ``strike_type='greater'`` — an "or above" tail; YES iff observed > floor.
  * ``strike_type='less'``    — an "or below" tail; YES iff observed <  cap.
  * ``strike_type='between'`` — a range;          YES iff floor <= observed <= cap.

The settlement engine historically assumed every ``-T##`` tail was a ``greater``
market under ``HIGH``. For the ~76 book rows whose tail is actually ``less``,
that assumption INVERTS the label (a HIGH "or below" contract is not "observed >
strike"). The ticker alone cannot tell the two apart; only ``strike_type`` (plus
floor/cap) can.

Resolution order (all read-only, no network):
  1. ``data/kalshi_strike_ladder.db`` by exact ticker (authoritative, 57k rows).
  2. ``data/kalshi_strike_ladder.db`` by (station, market_type, date, strike).
  3. ``None`` — callers must fail closed rather than assume an operator.

Both tails are STRICT: equality on a greater/less bound is a LOSS for YES.
Verified against Kalshi's live trade API + ``result`` field (1,093/1,093 tie rows
in the ladder are ``result='no'``).

B-Mode compliant. Deterministic. No AI/ML.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parents[1]

LADDER_DB = Path(os.environ.get(
    "KALSHI_STRIKE_LADDER_DB",
    str(REPO / "data" / "kalshi_strike_ladder.db")))

_TICKER_RE = re.compile(
    r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"(?P<kind>[TB])(?P<body>[0-9.]+)$")

_MONTHS = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
           "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}


def parse_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Parse a Kalshi temperature ticker into (series, date, kind, value).

    ``kind`` is 'T' (threshold tail) or 'B' (between range). NOTE: kind 'T' does
    NOT tell you whether the tail is greater or less — resolve that from the
    ladder (``resolve_strike_axis``); this parse is a fallback only.
    """
    if not ticker:
        return None
    m = _TICKER_RE.match(ticker.strip())
    if not m:
        return None
    return {
        "series": m.group("series"),
        "local_trading_date": f"20{m.group('yy')}-{_MONTHS[m.group('mon')]}-{m.group('dd')}",
        "kind": m.group("kind"),
        "value": float(m.group("body")),
        "ticker": ticker.strip(),
    }


def _connect(db: Path = LADDER_DB) -> Optional[sqlite3.Connection]:
    if not db or not os.path.exists(db):
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def resolve_strike_axis(ticker: str, *, station: Optional[str] = None,
                        market_type: Optional[str] = None,
                        local_trading_date: Optional[str] = None,
                        db: Path = LADDER_DB) -> Optional[Dict[str, Any]]:
    """Resolve the authoritative strike axis for a ticker. None when unknown.

    Returns ``{"strike_type", "strike_value", "floor_strike", "cap_strike",
    "settlement_value_dollars", "result", "ticker"}`` or None. Never raises.
    """
    conn = _connect(db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT strike_value, strike_type, floor_strike, cap_strike, "
            "settlement_value_dollars, result, ticker, station, local_trading_date "
            "FROM strike_ladder WHERE ticker=? LIMIT 1", (ticker,)).fetchone()
        if row is None:
            parsed = parse_ticker(ticker)
            if parsed and station and market_type and local_trading_date:
                row = conn.execute(
                    "SELECT strike_value, strike_type, floor_strike, cap_strike, "
                    "settlement_value_dollars, result, ticker, station, "
                    "local_trading_date FROM strike_ladder WHERE station=? AND "
                    "market_type=? AND local_trading_date=? AND strike_value=? LIMIT 1",
                    (station, market_type, local_trading_date, parsed["value"])).fetchone()
        if row is None:
            return None
        keys = ("strike_value", "strike_type", "floor_strike", "cap_strike",
                "settlement_value_dollars", "result", "ticker", "station",
                "local_trading_date")
        out = dict(zip(keys, row))
        return out
    except sqlite3.Error:
        return None
    finally:
        conn.close()


if __name__ == "__main__":
    for tk in ("KXHIGHCHI-26OCT09-T71", "KXHIGHCHI-26OCT09-T78",
               "KXHIGHTATL-26AUG18-T92", "KXHIGHTMIN-26OCT12-T81"):
        print(tk, resolve_strike_axis(tk))
