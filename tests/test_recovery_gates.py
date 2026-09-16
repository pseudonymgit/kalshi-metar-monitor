import os
import sqlite3
from datetime import datetime, timezone, timedelta

from core.calibration_pipeline import CalibrationPipeline
from scripts.z8_kalshi_market_cron import EXPECTED_STATIONS, validate_snapshot_receipt


def test_all_20_station_universe_is_explicit():
    assert len(EXPECTED_STATIONS) == 20
    assert {"KMDW", "KPHL", "KSAT"}.issubset(EXPECTED_STATIONS)


def test_z8_receipt_checks_freshness_and_coverage(tmp_path):
    db = tmp_path / "z8.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE market_snapshots (snapshot_time TEXT, station TEXT)")
        ts = datetime.now(timezone.utc).isoformat()
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?)", [(ts, s) for s in EXPECTED_STATIONS])
    receipt = validate_snapshot_receipt(str(db))
    assert receipt["ok"] is True
    assert receipt["row_count"] == 20
    assert receipt["missing_stations"] == []


def test_calibration_persistence_health_reports_write(tmp_path, monkeypatch):
    db = tmp_path / "calibration.db"
    monkeypatch.setenv("SIGNAL_CALIBRATION_DB", str(db))
    pipeline = CalibrationPipeline(["signal"], ["KNYC"])
    pipeline.update("signal", "KNYC", 0.7, True)
    health = pipeline.persistence_health()
    assert health["available"] is True
    assert health["writes"] == 1
    assert health["failures"] == 0
    assert health["last_success"]


def test_healthz_endpoint_is_registered_once(monkeypatch):
    monkeypatch.setenv("SKIP_WEATHER_COLLECTOR", "1")
    import app
    rules = [r for r in app.app.url_map.iter_rules() if r.rule == "/healthz"]
    assert len(rules) == 1
    response = app.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
