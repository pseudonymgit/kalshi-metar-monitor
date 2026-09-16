import json

from scripts.daily_kalshi_weather_refresh import run


def test_daily_refresh_records_dynamic_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.daily_kalshi_weather_refresh.discover",
        lambda path: {
            "raw_station_count": 2,
            "station_count": 2,
            "stations": {"KATL": ["KXHIGHATL"], "KDFW": ["KXHIGHTDAL"]},
            "aliases": {"KDAL": "KDFW"},
        },
    )
    monkeypatch.setattr(
        "scripts.daily_kalshi_weather_refresh.effective_station_series",
        lambda path: {station: ["ticker"] for station in [
            "KATL", "KDFW", "KAUS", "KBOS", "KDCA", "KDEN", "KHOU", "KLAS", "KLAX",
            "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
        ]},
    )
    monkeypatch.setattr(
        "scripts.daily_kalshi_weather_refresh.subprocess.run",
        lambda *a, **k: type("Result", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(
        "scripts.daily_kalshi_weather_refresh.validate_snapshot_receipt",
        lambda: {"ok": True, "row_count": 20, "missing_stations": []},
    )
    receipt = run(str(tmp_path / "cache.json"), str(tmp_path / "receipt.json"))
    assert receipt["ok"] is True
    assert receipt["discovery"]["static_fallback_stations"]
    assert json.loads((tmp_path / "receipt.json").read_text())["ok"] is True
