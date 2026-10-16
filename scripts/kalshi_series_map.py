"""Canonical Kalshi series ticker → station mapping (P0-B ladder backfill).

This module provides the same ticker resolution that the JSON-backed 
`kalshi_series_map.json` provides, exposed as an importable Python module
so scripts like `kalshi_strike_ladder_backfill.py` can `import kalshi_series_map`.

The authoritative data lives in `data/kalshi_series_map.json` — this module
is a thin wrapper that loads it at import time.
"""
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MAP_PATH = _REPO / "data" / "kalshi_series_map.json"

if not _MAP_PATH.exists():
    raise ImportError(f"kalshi_series_map.json not found at {_MAP_PATH}")

with open(_MAP_PATH) as _fh:
    _DATA = json.load(_fh)


def resolve_ticker(ticker: str) -> str:
    """Resolve a Kalshi market ticker to a canonical station ICAO."""
    norm = ticker.upper()
    for station, info in _DATA["stations"].items():
        for alias in info.get("aliases", []):
            if norm.startswith(alias):
                return station
        for alias in info.get("legacy_aliases", []):
            if norm.startswith(alias):
                return station
        for alias in info.get("low_aliases", []):
            if norm.startswith(alias):
                return station
    return _resolve_by_partial_match(ticker, _DATA)


def all_stations() -> list:
    return sorted(_DATA["stations"].keys())


def _resolve_by_partial_match(ticker: str, data: dict) -> str:
    """Fallback: try partial ticker suffix matching against known stations."""
    norm = ticker.upper()
    # Try known LOW ticker patterns that aren't in the alias map
    for station, info in data["stations"].items():
        for alias in info.get("low_aliases", []):
            if alias in norm:
                return station
        for alias in info.get("low_legacy_aliases", []):
            if alias in norm:
                return station
        for alias in info.get("aliases", []):
            if alias in norm:
                return station
    raise KeyError(f"Ticker {ticker} not found in kalshi_series_map")
