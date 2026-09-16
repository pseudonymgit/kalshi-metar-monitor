import csv
import sqlite3

from scripts.b2_input_schema_gate import inspect
from scripts.b2_price_monitor_fixture import capture_fixture_rows
from scripts.z8_kalshi_market_cron import UNKNOWN_API_CONTRACT_VERSION


def _row(day, market="M"):
    return {
        "snapshot_time": f"2026-09-{day:02d}T01:00:00+00:00",
        "station": "KATL",
        "series_ticker": "S",
        "market_ticker": market,
        "strike_temp": 72,
        "market": {"yes_bid": 31, "yes_ask": 37},
    }


def test_fixture_capture_stores_unknown_unsuffixed_cent_row_but_b2_rejects(tmp_path):
    db = tmp_path / "staging.db"
    assert capture_fixture_rows(db, [_row(1)]) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT yes_bid, yes_ask, mid_price, price_unit, bid_source_field, "
            "ask_source_field, api_contract_version, provenance_status "
            "FROM market_snapshots"
        ).fetchone()
    assert row == (31.0, 37.0, 34.0, "cents", "yes_bid", "yes_ask", UNKNOWN_API_CONTRACT_VERSION, "unverified")
    result = inspect(str(db), str(tmp_path / "missing.csv"), "dollars")
    assert result["eligible"] is False
    assert "validated_price_rows" in result["missing_inputs"]


def test_fixture_capture_explicit_version_is_verified_and_b2_price_rows_pass(tmp_path):
    db = tmp_path / "staging.db"
    rows = [_row(day, f"M{day}") for day in range(1, 73)]
    assert capture_fixture_rows(db, rows, api_contract_version="kalshi-contract-fixture-v1") == 72
    cal = tmp_path / "cal.csv"
    fields = ["snapshot_hour", "station", "series_ticker", "market_ticker", "market_type", "strike_temp", "strike_source_field", "strike_provenance_status", "calibrated_probability", "calibration_source", "calibration_version"]
    with cal.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(zip(fields, [row["snapshot_time"][:13], "KATL", "S", row["market_ticker"], "binary", 72, "strike_price", "verified", ".55", "fixture", "v1"])))
    with sqlite3.connect(db) as conn:
        statuses = conn.execute("SELECT DISTINCT api_contract_version, provenance_status FROM market_snapshots").fetchall()
    assert statuses == [("kalshi-contract-fixture-v1", "verified")]
    result = inspect(str(db), str(cal), "dollars")
    assert result["eligible"] is True
    assert result["snapshot"]["prices_valid"] is True
    assert result["join"]["exact_key_complete"] is True
