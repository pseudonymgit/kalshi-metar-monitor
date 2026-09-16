import sqlite3

import pytest

from scripts.z8_kalshi_market_cron import (
    KALSHI_API_BASE,
    KALSHI_MARKETS_PATH,
    OFFICIAL_API_CONTRACT_VERSION,
    UNKNOWN_API_CONTRACT_VERSION,
    _ensure_schema,
    build_price_record,
    require_api_contract_version,
)


def test_approved_unsuffixed_cents_pair_derives_same_unit_mid():
    row = build_price_record({"yes_bid": 31, "yes_ask": 37}, "kalshi-contract-test")
    assert row == {
        "yes_bid": 31.0,
        "yes_ask": 37.0,
        "mid_price": 34.0,
        "price_unit": "cents",
        "bid_source_field": "yes_bid",
        "ask_source_field": "yes_ask",
        "api_contract_version": "kalshi-contract-test",
        "provenance_status": "verified",
    }


def test_official_api_surface_is_the_default_contract():
    assert KALSHI_API_BASE == "https://api.elections.kalshi.com/trade-api/v2"
    assert KALSHI_MARKETS_PATH == "/markets"
    row = build_price_record({"yes_bid": 31, "yes_ask": 37})
    assert row["api_contract_version"] == OFFICIAL_API_CONTRACT_VERSION
    assert row["provenance_status"] == "verified"


@pytest.mark.parametrize(
    "market",
    [
        {"yes_bid_dollars": 0.31, "yes_ask_dollars": 0.37},
        {"yes_bid": 31, "yes_ask_dollars": 0.37},
        {"yes_bid": 31},
        {"yes_bid": 101, "yes_ask": 102},
    ],
)
def test_unapproved_missing_or_mixed_price_rows_are_quarantined(market):
    assert build_price_record(market, "kalshi-contract-test") is None


def test_unestablished_api_contract_remains_unknown_unverified(monkeypatch):
    monkeypatch.delenv("Z8_API_CONTRACT_VERSION", raising=False)
    assert require_api_contract_version(source_contract_established=False) == UNKNOWN_API_CONTRACT_VERSION
    assert build_price_record({"yes_bid": 31, "yes_ask": 37}, source_contract_established=False) == {
        "yes_bid": 31.0,
        "yes_ask": 37.0,
        "mid_price": 34.0,
        "price_unit": "cents",
        "bid_source_field": "yes_bid",
        "ask_source_field": "yes_ask",
        "api_contract_version": UNKNOWN_API_CONTRACT_VERSION,
        "provenance_status": "unverified",
    }


def test_unestablished_version_rows_remain_ineligible_for_b2():
    row = build_price_record({"yes_bid": 31, "yes_ask": 37}, None, source_contract_established=False)
    assert row["provenance_status"] == "unverified"
    assert row["api_contract_version"] == UNKNOWN_API_CONTRACT_VERSION


def test_dollar_suffixed_fields_are_rejected_even_with_unsuffixed_pair():
    assert build_price_record({"yes_bid": 31, "yes_ask": 37, "yes_bid_dollars": 0.31}, "kalshi-contract-test") is None


def test_schema_migration_is_additive_and_preserves_legacy_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, yes_bid REAL, yes_ask REAL, mid_price REAL)")
    conn.execute("INSERT INTO market_snapshots (yes_bid, yes_ask, mid_price) VALUES (31, 37, 34)")
    _ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    assert {"price_unit", "bid_source_field", "ask_source_field", "api_contract_version", "provenance_status"} <= columns
    assert conn.execute("SELECT yes_bid, yes_ask, mid_price FROM market_snapshots").fetchone() == (31.0, 37.0, 34.0)
    assert conn.execute("SELECT price_unit, provenance_status FROM market_snapshots").fetchone() == (None, None)
