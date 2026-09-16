import json

from scripts.kalshi_series_discovery import discover
from scripts.z8_kalshi_market_cron import effective_station_series, load_discovered_series


def test_discovery_cache_load_and_merge(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"stations": {"KNEW": ["KXHIGHNEW"], "KMDW": ["KXHIGHCHI"]}}))
    discovered = load_discovered_series(str(path))
    merged = effective_station_series(str(path))
    assert discovered["KNEW"] == ["KXHIGHNEW"]
    assert merged["KNEW"] == ["KXHIGHNEW"]
    assert "KXHIGHCHI" in merged["KMDW"]


def test_discovery_writes_atomic_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.kalshi_series_discovery._discover_series_for_stations", lambda: {"KDAL": ["KXHIGHTDAL"], "KNEW": ["KXHIGHNEW"]})
    payload = discover(str(tmp_path / "cache.json"))
    assert payload["station_count"] == 2
    assert payload["aliases"] == {"KDAL": "KDFW"}
    assert payload["stations"]["KDFW"] == ["KXHIGHTDAL"]
    assert json.loads((tmp_path / "cache.json").read_text())["stations"]["KNEW"] == ["KXHIGHNEW"]
