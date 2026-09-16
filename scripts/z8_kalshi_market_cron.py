#!/usr/bin/env python3
"""
Z.8 — Kalshi market snapshot collector.

Captures bid/ask/mid/volume/OI for all Kalshi weather markets
at each station, stores in data/kalshi_market_snapshots.db.

Usage:
    python3 scripts/z8_kalshi_market_cron.py              # run once
    python3 scripts/z8_kalshi_market_cron.py --once        # same
"""

import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time
import json
import math
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="[Z.8] %(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
LOGGER = logging.getLogger("z8")

# station -> list of possible HIGH series tickers to try
STATION_SERIES: dict[str, list[str]] = {
    "KATL": ["KXHIGHATL", "KXHIGHTATL"],
    "KAUS": ["KXHIGHAUS"],
    "KBOS": ["KXHIGHTBOS", "KXHIGHBOS"],
    "KDCA": ["KXHIGHDCA", "KXHIGHTDC"],
    "KDEN": ["KXHIGHDEN"],
    "KDFW": ["KXHIGHTDAL", "KXHIGHDFW"],
    "KHOU": ["KXHIGHTHOU", "KXHIGHHOU"],
    "KLAS": ["KXHIGHTLV", "KXHIGHLAS"],
    "KLAX": ["KXHIGHLAX"],
    "KMDW": ["KXHIGHCHI", "KXHIGHMDW"],
    "KMIA": ["KXHIGHMIA"],
    "KMSP": ["KXHIGHTMIN", "KXHIGHMSP"],
    "KMSY": ["KXHIGHTNOLA", "KXHIGHMSY"],
    "KNYC": ["KXHIGHNY0", "KXHIGHNY"],
    "KOKC": ["KXHIGHOKC", "KXHIGHTOKC"],
    "KPHL": ["KXHIGHPHIL"],
    "KPHX": ["KXHIGHTPHX", "KXHIGHPHX"],
    "KSAT": ["KXHIGHTSATX"],
    "KSEA": ["KXHIGHTSEA", "KXHIGHSEA"],
    "KSFO": ["KXHIGHTSFO", "KXHIGHSFO"],
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "kalshi_market_snapshots.db")
EXPECTED_STATIONS = frozenset(STATION_SERIES)
DISCOVERY_CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "kalshi_series_discovery.json")
STATION_ALIASES = {"KDAL": "KDFW"}

_LAST_REQUEST = 0.0

# Q-B2-PROD-PRICE-CONTRACT: the owner must provide the exact external API
# contract version before a row can be B2-eligible.  Monitor mode still needs
# to capture otherwise-valid quotes when that metadata is unavailable, so the
# absence is recorded explicitly rather than invented or treated as verified.
API_CONTRACT_VERSION_ENV = "Z8_API_CONTRACT_VERSION"
UNKNOWN_API_CONTRACT_VERSION = "UNKNOWN_UNVERIFIED"
OFFICIAL_API_CONTRACT_VERSION = "kalshi-predictions-rest-v2"
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKETS_PATH = "/markets"
PRICE_UNIT = "cents"
PRICE_BID_FIELD = "yes_bid"
PRICE_ASK_FIELD = "yes_ask"
VERIFIED_PROVENANCE_STATUS = "verified"
UNVERIFIED_PROVENANCE_STATUS = "unverified"


def require_api_contract_version(value: str | None = None, *, source_contract_established: bool = True) -> str:
    """Return the explicit override or the established official API surface.

    The default is valid only because this collector establishes the official
    base URL, markets endpoint, and unsuffixed cent fields below.  If that
    source/endpoint contract cannot be established, preserve the explicit
    unknown marker; B2 independently requires verified provenance.
    """
    version = value if value is not None else os.getenv(API_CONTRACT_VERSION_ENV)
    if not isinstance(version, str) or not version.strip():
        return OFFICIAL_API_CONTRACT_VERSION if source_contract_established else UNKNOWN_API_CONTRACT_VERSION
    return version.strip()


def build_price_record(market: dict[str, Any], api_contract_version: str | None = None,
                       *, source_contract_established: bool = True) -> dict[str, Any] | None:
    """Validate and normalize the approved unsuffixed-cents price contract.

    Only the paired unsuffixed fields are authoritative.  Dollar-suffixed
    fields, one-sided rows, mixed sources, and non-finite/out-of-range values
    are rejected rather than inferred or converted.
    """
    version = require_api_contract_version(api_contract_version,
                                           source_contract_established=source_contract_established)
    provenance_status = (
        VERIFIED_PROVENANCE_STATUS
        if version != UNKNOWN_API_CONTRACT_VERSION
        else UNVERIFIED_PROVENANCE_STATUS
    )
    # The approved contract is the paired, unsuffixed cents fields only.  Do
    # not silently combine them with dollar-suffixed alternatives.
    if any(field in market for field in ("yes_bid_dollars", "yes_ask_dollars")):
        return None
    if PRICE_BID_FIELD not in market or PRICE_ASK_FIELD not in market:
        return None
    if market.get(PRICE_BID_FIELD) is None or market.get(PRICE_ASK_FIELD) is None:
        return None
    try:
        bid = float(market[PRICE_BID_FIELD])
        ask = float(market[PRICE_ASK_FIELD])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask <= 100):
        return None
    return {
        "yes_bid": bid,
        "yes_ask": ask,
        "mid_price": (bid + ask) / 2,
        "price_unit": PRICE_UNIT,
        "bid_source_field": PRICE_BID_FIELD,
        "ask_source_field": PRICE_ASK_FIELD,
        "api_contract_version": version,
        "provenance_status": provenance_status,
    }


def load_discovered_series(path: str = DISCOVERY_CACHE_PATH) -> dict[str, list[str]]:
    """Load the latest daily discovery cache, returning an empty map if absent."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        stations = payload.get("stations") or {}
        result = {}
        for key, values in stations.items():
            station = STATION_ALIASES.get(str(key).upper(), str(key).upper())
            result.setdefault(station, set()).update(values)
        return {key: sorted(values) for key, values in result.items() if values}
    except (OSError, ValueError, TypeError):
        return {}


def effective_station_series(path: str = DISCOVERY_CACHE_PATH) -> dict[str, list[str]]:
    """Merge daily discovery over the static baseline without dropping coverage."""
    merged = {station: list(tickers) for station, tickers in STATION_SERIES.items()}
    for station, tickers in load_discovered_series(path).items():
        merged[station] = sorted(set(merged.get(station, []) + list(tickers)))
    return merged


def validate_snapshot_receipt(db_path: str = DB_PATH, max_age_seconds: int = 3900,
                              now: datetime.datetime | None = None) -> dict[str, Any]:
    """Fail-closed freshness/coverage/row-growth receipt for the latest snapshot."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT MAX(snapshot_time) FROM market_snapshots").fetchone()
        latest = row[0] if row else None
        if not latest:
            raise RuntimeError("z8 has no snapshots")
        latest_dt = datetime.datetime.fromisoformat(latest.replace("Z", "+00:00"))
        age = (now - latest_dt).total_seconds()
        stations = {r[0] for r in conn.execute("SELECT DISTINCT station FROM market_snapshots WHERE snapshot_time = ?", (latest,))}
        unknown_stations = sorted(stations - EXPECTED_STATIONS)
        rows = conn.execute("SELECT COUNT(*) FROM market_snapshots WHERE snapshot_time = ?", (latest,)).fetchone()[0]
        previous_ts = conn.execute(
            "SELECT MAX(snapshot_time) FROM market_snapshots WHERE snapshot_time < ?", (latest,)
        ).fetchone()[0]
        previous = conn.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE snapshot_time = ?", (previous_ts,)
        ).fetchone()[0] if previous_ts else 0
        total_rows = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        previous_total = total_rows - rows
    missing = sorted(EXPECTED_STATIONS - stations)
    return {"latest_snapshot": latest, "age_seconds": age, "fresh": age <= max_age_seconds,
            "stations": sorted(stations), "missing_stations": missing,
            "unknown_stations": unknown_stations,
            "exact_station_coverage": stations == EXPECTED_STATIONS,
            "row_count": rows, "prior_row_count": previous,
            "previous_snapshot": previous_ts,
            "total_row_count": total_rows,
            "previous_total_row_count": previous_total,
            "row_growth": rows > previous and rows > 0,
            "ok": age <= max_age_seconds and stations == EXPECTED_STATIONS and
            rows > 0 and rows > previous}


def _kalshi_get(path: str) -> dict[str, Any]:
    import requests
    global _LAST_REQUEST
    elapsed = time.time() - _LAST_REQUEST
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _LAST_REQUEST = time.time()
    resp = requests.get(f"{KALSHI_API_BASE}{path}", timeout=30)
    if resp.status_code == 429:
        LOGGER.warning("429, retrying after 5s")
        time.sleep(5)
        resp = requests.get(f"{KALSHI_API_BASE}{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            station TEXT NOT NULL,
            series_ticker TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            strike_temp REAL,
            market_type TEXT,
            yes_bid REAL,
            yes_ask REAL,
            mid_price REAL,
            volume_24h REAL,
            open_interest REAL,
            ltv REAL,
            price_unit TEXT,
            bid_source_field TEXT,
            ask_source_field TEXT,
            api_contract_version TEXT,
            provenance_status TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Additive-only migration: historical rows remain untouched and therefore
    # retain NULL provenance until independently re-ingested under the contract.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    for name in ("price_unit", "bid_source_field", "ask_source_field",
                 "api_contract_version", "provenance_status"):
        if name not in columns:
            conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {name} TEXT")


def main() -> int:
    api_contract_version = require_api_contract_version()
    t0 = time.time()
    snap_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        _ensure_schema(conn)
        total, ok, fail = 0, 0, 0
        for station, tickers in sorted(effective_station_series().items()):
            found = False
            series_ticker = tickers[0]  # default to first
            for t in tickers:
                try:
                    data = _kalshi_get(f"/markets?series_ticker={t}&limit=200")
                    markets = data.get("markets") or []
                    if markets:
                        found = True
                        series_ticker = t
                        break
                except Exception:
                    continue
            if not found:
                LOGGER.info("station=%s no active markets", station)
                continue
            try:
                data = _kalshi_get(f"/markets?series_ticker={series_ticker}&limit=200")
                markets = data.get("markets") or []
                inserted = 0
                for m in markets:
                    ticker = m.get("ticker", "")
                    strike = m.get("strike_price") or m.get("strike")
                    prices = build_price_record(m, api_contract_version)
                    if prices is None:
                        LOGGER.warning("station=%s ticker=%s rejected unproven price provenance", station, ticker)
                        continue
                    bid = prices["yes_bid"]
                    ask = prices["yes_ask"]
                    mid = prices["mid_price"]
                    vol = (m.get("volume_24h_fp") or m.get("volume_24h"))
                    oi = (m.get("open_interest_fp") or m.get("open_interest"))
                    if vol is not None:
                        vol = float(vol)
                    if oi is not None:
                        oi = float(oi)
                    ltv = m.get("last_traded_value")
                    if ltv is not None:
                        ltv = float(ltv)
                    mtype = m.get("type") or m.get("market_type")
                    if strike is not None:
                        strike = float(strike)
                    if not ticker:
                        continue
                    conn.execute("""
                        INSERT INTO market_snapshots
                            (snapshot_time, station, series_ticker, market_ticker,
                             strike_temp, market_type, yes_bid, yes_ask, mid_price,
                             volume_24h, open_interest, ltv, price_unit,
                             bid_source_field, ask_source_field,
                             api_contract_version, provenance_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (snap_ts, station, series_ticker, ticker,
                          strike, mtype, bid, ask, mid, vol, oi, ltv,
                          prices["price_unit"], prices["bid_source_field"],
                          prices["ask_source_field"], prices["api_contract_version"],
                          prices["provenance_status"]))
                    inserted += 1
                total += inserted
                ok += 1
                LOGGER.info("station=%s ticker=%s markets=%d", station, series_ticker, inserted)
            except Exception as e:
                fail += 1
                LOGGER.warning("station=%s FAIL: %s", station, e)
        conn.commit()
        LOGGER.info("DONE stations_ok=%d fail=%d total_markets=%d elapsed=%.1fs",
                    ok, fail, total, time.time() - t0)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    sys.exit(main())
