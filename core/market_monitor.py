"""
Market monitor Module

Extracted from kalshi_monitor.py during Phase 20.1 monolith decomposition.
"""



import base64
import copy
import contextvars
import json
import logging
import os
import re

# Layer 4: LOW market discovery regex
LOW_TICKER_PATTERN = re.compile(r"^LOW-\d{6}$")
import sqlite3
import threading
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

import re

# Layer 4: LOW market discovery regex
LOW_TICKER_PATTERN = re.compile(r"^LOW-\d{6}$")
from flask import has_request_context, request

from core.authoritative_state import immutable_public_state_snapshot
from core.station_time import parse_iso_utc, station_local_day_key, to_station_local
from core.metar_monitor import _now_utc_iso
from core.alert_schema import ALERT_SCHEMA_VERSION
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Cross-module imports (Phase 20 decomposition — originally shared via monolith namespace)
from .order_manager import _current_kalshi_execution_domain, _persist_market_cache
from .kalshi_price_fetcher import _kalshi_public_get

_last_market_state = {}
_last_composed_sent = {}
_last_market_check_summary = {}
_ladder_state = {}
_ladder_event_keys = {}
_LADDER_LOCK = threading.Lock()
_SERIES_LOCK = threading.Lock()
_PROXIMITY_LOCK = threading.Lock()
_SERIES_BY_STATION = {}
_SERIES_DISCOVERED = False
_SERIES_MARKETS_CACHE = {}
_SERIES_EVENTS_CACHE = {}
_HYDRATION_PREREQUISITE_STATE = {}
_SERIES_DISCOVERY_ATTEMPT_COUNT = 0
_LAST_SERIES_DISCOVERY_SUCCESS_UTC = None
_LAST_SERIES_DISCOVERY_ERROR = None
_DISCOVERED_WEATHER_MARKETS = []
_DISCOVERED_WEATHER_MARKETS_BY_STATION = {}
_MARKETS_CACHE_POPULATION_COUNT = 0
_LAST_HYDRATION_EXECUTION = {}
_hydration_queue = []
_hydration_backoff_until = {}
_last_hydration_request_ts = 0.0
_LAST_PROXIMITY_REGIME = {}
_PROXIMITY_RANK = {
    "FAR": 0,
    "APPROACHING": 1,
    "NEAR": 2,
    "CRITICAL": 3,
}

DIRECTIONAL_STRIKE_WINDOW_SIZE = 3

_EXPLICIT_SETTLEMENT_STATION_OVERRIDES = {
    "NYC": "KNYC",
    "CHI": "KMDW",
    "LAX": "KLAX",
    "DEN": "KDEN",
    "MIA": "KMIA",
    "AUS": "KAUS",
    "PHL": "KPHL",
    "PHIL": "KPHL",
}

_SETTLEMENT_ICAO_PATTERN = re.compile(r"\bK[A-Z]{3}\b")

_WEATHER_MARKET_TYPE_LABELS = {
    "HIGH_TEMP": "HIGH_TEMP",
    "LOW_TEMP": "LOW_TEMP",
    "PRECIP": "PRECIP",
}

# ─── Station city token map (compatibility shim) ───
# This is now derived from core.station_registry. Kept here for backward compatibility
# with app.py and other modules that import it directly.
# Do NOT hardcode station lists elsewhere — use station_registry.get_all_stations() instead.
try:
    from core.station_registry import get_station_mapping as _registry_get_mapping
    _STATION_CITY_TOKEN_MAP = {
        icao: info.get("kalshi_token", "")
        for icao, info in _registry_get_mapping().items()
    }
except Exception:
    # Fallback if station_registry import fails (shouldn't happen in normal operation)
    _STATION_CITY_TOKEN_MAP = {
        "KDEN": "DEN", "KLAX": "LAX", "KNYC": "NYC", "KPHL": "PHIL",
        "KMDW": "CHI", "KMIA": "MIA", "KAUS": "AUS",
    }
__all__ = ['get_default_config', 'map_market_to_station', 'discover_kalshi_weather_markets', 'resolve_settlement_station', 'ensure_series_discovery_loaded', 'get_series_discovery_cache_snapshot', 'get_series_surface_snapshot', 'get_cached_series_markets', 'get_station_hydration_cache_probe', 'ensure_ladder_hydration_prerequisite', 'get_hydration_prerequisite_state_snapshot', 'get_kalshi_connectivity_snapshot', 'get_last_hydration_execution_snapshot', 'enqueue_station_hydration', 'hydration_queue_snapshot', 'process_hydration_queue_worker', 'hydrate_station_ladder_snapshot', 'classify_proximity', 'build_structured_snapshot', 'build_structured_snapshot_from_cache', 'get_state', 'get_metrics']



def _extract_station_from_settlement_source_url(settlement_source: str | None) -> str | None:
    """Extract station ICAO from settlement source URL.
    
    Parses URLs like:
    - https://api.kalshi.com/trustful/settlement/NWS/issuedby=KNYC
    - https://api.kalshi.com/trustful/settlement/NWS/issuedby=KDEN
    
    Returns normalized ICAO (4-letter code) or None if not found.
    """
    if not settlement_source:
        return None
    
    # Look for issuedby= pattern in URL (3 or 4 letter station codes)
    match = re.search(r"issuedby=([A-Z]{3,4})", settlement_source)
    if match:
        return match.group(1).upper()
    
    return None
def _market_strike_value(market):
    if not isinstance(market, dict):
        return None

    direct = market.get("strike")
    if direct is not None:
        try:
            return float(direct)
        except Exception:
            return None

    floor = market.get("floor_strike")
    cap = market.get("cap_strike")

    if floor is not None:
        try:
            return float(floor)
        except Exception:
            pass

    if floor is not None and cap is not None:
        try:
            return (float(floor) + float(cap)) / 2.0
        except Exception:
            return None

    return None
def _directional_strike_window(markets, observed_value, direction):
    if observed_value is None:
        return list(markets)

    try:
        observed = float(observed_value)
    except Exception:
        return list(markets)

    window_size = max(int(os.getenv("DIRECTIONAL_STRIKE_WINDOW_SIZE", str(DIRECTIONAL_STRIKE_WINDOW_SIZE))), 1)
    ladder = []
    for market in markets:
        strike_value = _market_strike_value(market)
        if strike_value is None:
            continue
        ladder.append((strike_value, market))

    if not ladder:
        return []

    if direction == "HIGH":
        # Keep the directional side plus the nearest crossed/equal boundary for context;
        # this is not a generic ladder reducer.
        directional = [entry for entry in ladder if entry[0] > observed]
        crossed_or_equal = [entry for entry in ladder if entry[0] <= observed]
        if directional:
            directional.sort(key=lambda entry: entry[0])
            selected = directional[:window_size]
            if crossed_or_equal:
                selected.append(max(crossed_or_equal, key=lambda entry: entry[0]))
            return [entry[1] for entry in selected]
        return [max(ladder, key=lambda entry: entry[0])[1]]

    if direction == "LOW":
        directional = [entry for entry in ladder if entry[0] < observed]
        crossed_or_equal = [entry for entry in ladder if entry[0] >= observed]
        if directional:
            directional.sort(key=lambda entry: entry[0], reverse=True)
            selected = directional[:window_size]
            if crossed_or_equal:
                selected.append(min(crossed_or_equal, key=lambda entry: entry[0]))
            return [entry[1] for entry in selected]
        return [min(ladder, key=lambda entry: entry[0])[1]]

    return [entry[1] for entry in ladder]
def _observed_temperature_f(normalized_station):
    try:
        state_snapshot = immutable_public_state_snapshot()
        last = state_snapshot["last_obs"].get(normalized_station)
        if last and "temp_f" in last:
            return float(last["temp_f"])
    except Exception:
        pass
    return None
def _assemble_structured_snapshot_markets(filtered_markets, normalized_station, selected_types):
    # Keep live-fetch shaping and cache-snapshot shaping identical here; this shared
    # helper is an anti-drift seam and should remain the common path.
    markets = []

    for market in filtered_markets:
        ticker = market.get("ticker") or ""
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")

        if strike_type == "between" and floor is not None:
            strike = int(floor)
        elif strike_type == "less" and cap is not None:
            strike = int(cap)
        elif strike_type == "greater" and floor is not None:
            strike = int(floor)
        else:
            strike = _extract_strike_from_ticker(ticker)

        if strike is None:
            continue

        markets.append(
            {
                "ticker": ticker,
                "strike": strike,
                "strike_type": strike_type,
                "floor_strike": floor,
                "cap_strike": cap,
                "event_ticker": market.get("event_ticker"),
                "last_price": market.get("last_price"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
                "status": market.get("status"),
            }
        )

    markets.sort(key=lambda x: x["strike"])
    pre_directional_market_count = len(markets)
    observed_value = _observed_temperature_f(normalized_station)

    if observed_value is not None and len(selected_types) == 1:
        direction = next(iter(sorted(selected_types)))
        markets = _directional_strike_window(markets, observed_value, direction)

    return {
        "markets": markets,
        "observed_value": observed_value,
        "pre_directional_market_count": pre_directional_market_count,
    }
def get_default_config():
    return {
        "base_url": (os.getenv("KALSHI_BASE_URL") or "").strip(),
        "key_id": (os.getenv("KALSHI_KEY_ID") or "").strip(),
        "key_pem": os.getenv("KALSHI_PRIVATE_KEY_PEM") or "",
    }
def _classify_weather_market_type(market: dict) -> str | None:
    marker_strings = [
        str(market.get("ticker") or "").strip().upper(),
        str(market.get("title") or "").strip().upper(),
        str(market.get("subtitle") or "").strip().upper(),
        str(market.get("series_ticker") or "").strip().upper(),
        str(market.get("category") or "").strip().upper(),
    ]
    marker_blob = " ".join(token for token in marker_strings if token)

    if any(token in marker_blob for token in ("TMAX", "HIGH TEMP", "HIGHEST TEMPERATURE", "DAILY HIGH")):
        return _WEATHER_MARKET_TYPE_LABELS["HIGH_TEMP"]
    if any(token in marker_blob for token in ("TMIN", "LOW TEMP", "LOWEST TEMPERATURE", "DAILY LOW")):
        return _WEATHER_MARKET_TYPE_LABELS["LOW_TEMP"]
    if any(token in marker_blob for token in ("PRECIP", "RAIN", "RAINFALL")):
        return _WEATHER_MARKET_TYPE_LABELS["PRECIP"]
    return None
def _extract_station_from_settlement_market_metadata(market: dict) -> str | None:
    settlement_metadata = market.get("settlement_metadata")
    if isinstance(settlement_metadata, dict):
        for key in ("station", "icao", "station_code", "settlement_station"):
            value = settlement_metadata.get(key)
            normalized_value = str(value or "").strip().upper()
            if _SETTLEMENT_ICAO_PATTERN.fullmatch(normalized_value):
                return normalized_value

    for key in ("settlement_station", "station", "station_code", "icao"):
        value = market.get(key)
        normalized_value = str(value or "").strip().upper()
        if _SETTLEMENT_ICAO_PATTERN.fullmatch(normalized_value):
            return normalized_value

    settlement_text_sources = [
        market.get("settlement_rule"),
        market.get("settlement_rules"),
        market.get("rules_primary"),
        market.get("rules"),
    ]
    if isinstance(settlement_metadata, dict):
        settlement_text_sources.extend(
            settlement_metadata.get(key)
            for key in ("rule", "rules", "settlement_rule", "description", "details")
        )

    settlement_blob = " ".join(
        str(fragment).strip().upper()
        for fragment in settlement_text_sources
        if isinstance(fragment, str) and fragment.strip()
    )
    if not settlement_blob:
        return None

    match = _SETTLEMENT_ICAO_PATTERN.search(settlement_blob)
    if not match:
        return None
    return match.group(0)
def map_market_to_station(market: dict) -> str | None:
    """Extract station ICAO from market metadata using multiple sources.
    
    This function extracts station codes from:
    - settlement_metadata.station / station_code / icao
    - market.settlement_station / station / station_code / icao
    - ticker / series_ticker / settlement_source patterns
    - settlement rule text parsing
    
    Returns normalized ICAO (4-letter code) or None if not found.
    """
    # Primary: Try direct metadata fields first
    result = _extract_station_from_settlement_market_metadata(market)
    if result:
        return result
    
    # Secondary: Try ticker patterns (e.g., KXHIGHDEN, KXLOWLAX)
    ticker = str(market.get("ticker") or "").strip().upper()
    series_ticker = str(market.get("series_ticker") or "").strip().upper()
    settlement_source = str(market.get("settlement_source") or "").strip().upper()
    
    ticker_source = ticker or series_ticker or settlement_source
    if ticker_source:
        # Look for 4-letter ICAO pattern in ticker
        match = _SETTLEMENT_ICAO_PATTERN.search(ticker_source)
        if match:
            return match.group(0)
        
        # Try to extract station from ticker using the known patterns
        extracted = _extract_station_from_ticker(ticker_source)
        if extracted:
            return extracted
    
    return None
def _parse_market_expiration(market: dict):
    for key in (
        "expiration_time",
        "expiration",
        "close_time",
        "settlement_time",
        "settlement_date",
    ):
        parsed = parse_iso_utc(market.get(key))
        if parsed is not None:
            return parsed
    return None
def discover_kalshi_weather_markets(max_pages=5, page_limit=200):
    discovered_markets = []
    discovered_by_station = {}
    cursor = None

    for _ in range(max_pages):
        path = f"/markets?limit={int(page_limit)}&status=open"
        if cursor:
            path = f"{path}&cursor={cursor}"

        data = _kalshi_public_get(path)
        markets = data.get("markets") or []

        for market in markets:
            if str(market.get("status") or "").strip().upper() != "OPEN":
                continue

            market_type = _classify_weather_market_type(market)
            if market_type is None:
                continue

            station = _extract_station_from_settlement_market_metadata(market)
            if not station:
                continue

            market_symbol = str(market.get("ticker") or market.get("symbol") or "").strip().upper()
            if not market_symbol:
                continue

            normalized_market = {
                "market_symbol": market_symbol,
                "market_type": market_type,
                "station": station,
                "expiration": _parse_market_expiration(market),
                "active": True,
            }
            discovered_markets.append(normalized_market)
            discovered_by_station.setdefault(station, []).append(market_symbol)

        cursor = data.get("cursor")
        if not cursor:
            break

    with _SERIES_LOCK:
        global _DISCOVERED_WEATHER_MARKETS, _DISCOVERED_WEATHER_MARKETS_BY_STATION
        _DISCOVERED_WEATHER_MARKETS = list(discovered_markets)
        _DISCOVERED_WEATHER_MARKETS_BY_STATION = {
            station: sorted(set(symbols))
            for station, symbols in discovered_by_station.items()
        }
        # Persist discovered markets for restart survival
        for station, symbols in discovered_by_station.items():
            for symbol in symbols:
                _persist_market_cache(
                    f"discovered:{station}:{symbol}",
                    station,
                    {"symbol": symbol, "station": station, "discovered_at": datetime.now(timezone.utc).isoformat()},
                )

    return list(discovered_markets)
# Market discovery functions live in kalshi_monitor.py — this file is deprecated
# def build_market_derived_station_universe() removed — see kalshi_monitor.py
# def discover_market_derived_station_codes() removed — see kalshi_monitor.py
# def build_market_polling_station_universe() removed — see kalshi_monitor.py
# def get_discovered_weather_market_station_mapping() removed — see kalshi_monitor.py

def resolve_settlement_station(token: str) -> str | None:
    normalized_token = (token or "").strip().upper()
    if not normalized_token:
        return None

    # Primary: Try market-derived mapping first (dynamic discovery)
    # Build the mapping from market metadata
    try:
        from core.kalshi_monitor import _discover_series_for_stations
        discovered = _discover_series_for_stations()
        for station, city_tokens in discovered.items():
            if isinstance(city_tokens, list):
                for ct in city_tokens:
                    if ct == normalized_token:
                        return station
            elif city_tokens == normalized_token:
                return station
    except Exception:
        pass  # Fall back to extraction

    # Try to extract station from token
    extracted = _extract_station_from_ticker(token)
    if extracted:
        return extracted

    return _EXPLICIT_SETTLEMENT_STATION_OVERRIDES.get(normalized_token)
def _format_change(prev, curr):
    return f"{prev} → {curr}"
def _parse_target_market_types(raw_types):
    if not raw_types:
        return set()
    valid_tokens = {"HIGH", "LOW"}
    return {
        token
        for token in (part.strip().upper() for part in raw_types.split(","))
        if token in valid_tokens
    }
def _get_active_stations():
    raw = (os.getenv("KALSHI_ACTIVE_STATIONS") or "").strip()
    if not raw:
        return None
    return {
        token.strip().upper()
        for token in raw.split(",")
        if token.strip()
    }
def _station_local_kalshi_date_token(station, observation_time_utc: str | datetime | None = None):
    if isinstance(observation_time_utc, datetime):
        now_utc = observation_time_utc.astimezone(timezone.utc) if observation_time_utc.tzinfo else observation_time_utc.replace(tzinfo=timezone.utc)
    elif observation_time_utc is not None:
        now_utc = parse_iso_utc(str(observation_time_utc)) or datetime.now(timezone.utc)
    else:
        now_utc = datetime.now(timezone.utc)

    try:
        now_local = to_station_local(station, now_utc)
    except Exception:
        now_local = now_utc

    return now_local.strftime("%y%b%d").upper()
def _normalize_series_tickers(series_tickers):
    if not series_tickers:
        return []

    if isinstance(series_tickers, str):
        return [series_tickers]

    if isinstance(series_tickers, list):
        return [ticker for ticker in series_tickers if ticker]

    return []
def _configured_target_market_types():
    configured = _parse_target_market_types(os.getenv("KALSHI_TARGET_MARKET_TYPE"))
    return configured or {"HIGH"}
def _infer_series_market_type(*, ticker: str = "", title: str = "", source: str = ""):
    normalized_ticker = str(ticker or "").strip().upper()
    normalized_title = str(title or "").strip().upper()
    marker_blob = f"{normalized_ticker} {normalized_title}".strip()
    
    # Check source type first
    if source.upper() == "HOURLY":
        return "HOURLY"

    if any(token in marker_blob for token in ("KXHIGH", " HIGH", "HIGH ", "HIGHEST TEMPERATURE", "DAILY HIGH")):
        return "HIGH"
    if any(token in marker_blob for token in ("KXLOW", " LOW", "LOW ", "LOWEST TEMPERATURE", "DAILY LOW")):
        return "LOW"
    return None
def _series_tickers_for_market_types(series_tickers, market_types: set[str] | None = None):
    normalized_series_tickers = _normalize_series_tickers(series_tickers)
    selected_types = {
        token.strip().upper()
        for token in (market_types or set())
        if token and token.strip().upper() in {"HIGH", "LOW"}
    }
    if not selected_types:
        return normalized_series_tickers

    filtered = []
    for ticker in normalized_series_tickers:
        inferred_market_type = _infer_series_market_type(ticker=ticker)
        if inferred_market_type is None or inferred_market_type in selected_types:
            filtered.append(ticker)
    return filtered
def _discover_series_for_stations():
    """Discover weather markets and their settlement stations dynamically.
    
    Uses the /series API to get all daily temperature markets, then:
    1. Extracts station ICAOs from settlement_source URLs (issuedby=)
    2. Falls back to ticker pattern matching if URL parsing fails
    3. Builds bidirectional mapping: station → series_tickers
    
    Returns dict of station_code → list of series_tickers.
    """
    data = _kalshi_public_get("/series?tags=Daily%20temperature")
    series_items = data.get("series") or []
    
    if not series_items:
        return {}
    
    # Build station → series_tickers mapping from settlement sources
    discovered = {}
    
    for item in series_items:
        frequency = (item.get("frequency") or "").strip().lower()
        ticker = (item.get("ticker") or "").strip().upper()
        series_ticker = (item.get("series_ticker") or "").strip().upper()
        settlement_source = item.get("settlement_source", "")
        source_type = item.get("source", "")  # Could be "HOURLY" for NYC hourly markets
        
        # Infer market type from ticker and source
        market_type = _infer_series_market_type(ticker=ticker, source=source_type)
        if market_type not in {"HIGH", "LOW", "HOURLY"}:
            continue
        
        # Skip non-daily markets (for now, only include hourly if explicitly marked)
        if frequency != "daily" and market_type != "HOURLY":
            continue
        
        # Try to extract station from settlement_source URL
        station_from_url = _extract_station_from_settlement_source_url(settlement_source)
        if station_from_url:
            station_code = station_from_url
        else:
            # Fallback: try to extract from ticker (e.g., KXHIGHDEN, KXLOWLAX)
            station_code = _extract_station_from_ticker(ticker)
            if not station_code:
                continue
        
        # Normalize and add to discovered
        station_code = station_code.upper()
        if station_code not in discovered:
            discovered[station_code] = set()
        
        if series_ticker:
            discovered[station_code].add(series_ticker)
        if ticker:
            discovered[station_code].add(ticker)
    
    # Convert sets to lists for JSON serialization
    result = {}
    for station, tickers in discovered.items():
        result[station] = sorted(list(tickers))
    
    return result
def _extract_station_from_ticker(ticker: str) -> str | None:
    """Extract station ICAO from ticker pattern.
    
    Handles various ticker formats:
    - KXHIGHDEN, KXLOWLAX → DEN, LAX → KDEN, KLAX
    - KXHIGHTATL, KXLOWTDC → ATL, DCA → KATL, KDCA
    - KXHIGHNY, KXLOWTNYC → NYC → KNYC
    - KXHIGHOU, KXLOWTOKC → OKC → KOKC
    - KXHIGHTMIN, KXLOWTMIN → MSP → KMSP
    - KXHIGHTSEA → SEA → KSEA
    - KXHIGHTSFO → SFO → KSFO
    
    Returns normalized 4-letter ICAO or None.
    """
    if not ticker:
        return None
    
    # Remove KX prefix and trailing patterns
    # e.g., KXHIGHDEN → DEN, KXLOWTATL → TATL, KXHIGHNY → NY
    cleaned = ticker
    for prefix in ("KXHIGH", "KXLOW", "KXHIGHT", "KXLOWT"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    
    # Some patterns have a leading T (KXHIGHTDAL -> TDAL), strip it
    if cleaned and cleaned[0] == 'T':
        cleaned = cleaned[1:]
    
    # Common mappings (city token → ICAO)
    city_to_icao = {
        "ATL": "KATL",
        "AUS": "KAUS",
        "BOS": "KBOS",
        "DC": "KDCA",
        "DCA": "KDCA",
        "DEN": "KDEN",
        "DAL": "KDAL",  # Kalshi uses KXHIGHTDAL
        "DFW": "KDAL",  # DFW airport code → DAL ticker token
        "HOU": "KHOU",  # Houston Hobby
        "LAS": "KLAS",
        "LAX": "KLAX",
        "CHI": "KMDW",
        "CHICAGO": "KMDW",
        "MIA": "KMIA",
        "MIN": "KMSP",
        "MINNEAPOLIS": "KMSP",
        "NOLA": "KMSY",  # New Orleans (Louis Armstrong International)
        "NEW ORLEANS": "KMSY",
        "NY": "KNYC",
        "NYC": "KNYC",
        "OKC": "KOKC",
        "PHIL": "KPHL",
        "PHOENIX": "KPHX",
        "SATX": "KSAT",
        "SEA": "KSEA",
        "SFO": "KSFO",
        "PHL": "KPHL",
        "MIA": "KMIA",
        "MSY": "KMSY",
    }
    
    cleaned_upper = cleaned.upper()
    return city_to_icao.get(cleaned_upper)
def ensure_series_discovery_loaded():
    global _SERIES_DISCOVERED, _SERIES_BY_STATION
    global _SERIES_DISCOVERY_ATTEMPT_COUNT, _LAST_SERIES_DISCOVERY_SUCCESS_UTC, _LAST_SERIES_DISCOVERY_ERROR
    with _SERIES_LOCK:
        if _SERIES_DISCOVERED:
            return {
                station: list(series)
                for station, series in _SERIES_BY_STATION.items()
            }

        record_connectivity_state = True
        if _current_kalshi_execution_domain() != "prod":
            record_connectivity_state = False
        if has_request_context() and str(request.path or "").startswith("/observability/"):
            record_connectivity_state = False

        if record_connectivity_state:
            _SERIES_DISCOVERY_ATTEMPT_COUNT += 1
        # Layer 0 hydration is deferred to lazy-first-access to avoid
        # blocking the health check during Render's port scan window.
        # Market cache will be populated from SQLite on first market lookup.
        try:
            discovered = _discover_series_for_stations()
            if not discovered:
                raise RuntimeError("series discovery returned 0 station mappings")

            normalized_discovered = {}
            for station, value in discovered.items():
                if isinstance(value, str):
                    normalized = [value]
                elif isinstance(value, list):
                    normalized = [v for v in value if v]
                else:
                    normalized = _normalize_series_tickers(value)

                normalized_discovered[station] = normalized

            _SERIES_BY_STATION = normalized_discovered
            _SERIES_DISCOVERED = True
            if record_connectivity_state:
                _LAST_SERIES_DISCOVERY_SUCCESS_UTC = datetime.now(timezone.utc).isoformat()
                _LAST_SERIES_DISCOVERY_ERROR = None
            return {
                station: list(series)
                for station, series in _SERIES_BY_STATION.items()
            }
        except Exception as exc:
            if record_connectivity_state:
                _LAST_SERIES_DISCOVERY_ERROR = str(exc)
            raise
def get_series_discovery_cache_snapshot() -> dict:
    with _SERIES_LOCK:
        return {
            station: list(series)
            for station, series in _SERIES_BY_STATION.items()
        }
def get_series_surface_snapshot() -> dict:
    with _SERIES_LOCK:
        stations = []
        for station in sorted(_SERIES_BY_STATION.keys()):
            series_tickers = _normalize_series_tickers(_SERIES_BY_STATION.get(station))
            series_rows = []
            total_raw_market_count = 0

            for series_ticker in series_tickers:
                cache_entry = _SERIES_MARKETS_CACHE.get(series_ticker)
                hydrated = cache_entry is not None
                raw_market_count = len((cache_entry or {}).get("markets") or [])
                total_raw_market_count += raw_market_count
                series_rows.append(
                    {
                        "series_ticker": series_ticker,
                        "hydrated": hydrated,
                        "raw_market_count": raw_market_count,
                        "hydrated_at_utc": (cache_entry or {}).get("hydrated_at_utc"),
                        "station_local_day": (cache_entry or {}).get("station_local_day"),
                    }
                )

            stations.append(
                {
                    "station": station,
                    "series_tickers": list(series_tickers),
                    "series": series_rows,
                    "total_raw_market_count": total_raw_market_count,
                }
            )

        return {"generated_utc": datetime.now(timezone.utc).isoformat(), "stations": stations}
def get_cached_series_markets(series_ticker: str) -> dict | None:
    normalized_series_ticker = (series_ticker or "").strip().upper()
    if not normalized_series_ticker:
        return None

    with _SERIES_LOCK:
        cached_entry = _SERIES_MARKETS_CACHE.get(normalized_series_ticker)
        if cached_entry is None:
            return None
        return {
            **cached_entry,
            "markets": [dict(market) for market in (cached_entry.get("markets") or [])],
        }
def get_station_hydration_cache_probe(station: str) -> dict:
    normalized_station = (station or "").strip().upper()
    with _SERIES_LOCK:
        series_tickers = _series_tickers_for_market_types(
            _SERIES_BY_STATION.get(normalized_station),
            _configured_target_market_types(),
        )
        series_ticker = series_tickers[0] if series_tickers else None
        cache_entries = [_SERIES_MARKETS_CACHE.get(ticker) or {} for ticker in series_tickers]
        raw_market_count = sum(len(entry.get("markets") or []) for entry in cache_entries)
        return {
            "station": normalized_station,
            "series_ticker": series_ticker if len(series_tickers) <= 1 else list(series_tickers),
            "raw_market_count": raw_market_count,
            "cache_present": any(entry for entry in cache_entries),
            "markets_cached": raw_market_count > 0,
            "station_local_day": next((entry.get("station_local_day") for entry in cache_entries if entry.get("station_local_day")), None),
            "hydrated_at_utc": max((entry.get("hydrated_at_utc") for entry in cache_entries if entry.get("hydrated_at_utc")), default=None),
        }
def _station_local_previous_day(station: str, now_utc_iso: str) -> str:
    current_utc = parse_iso_utc(now_utc_iso)
    current_local = to_station_local(station, current_utc)
    previous_local = current_local - timedelta(days=1)
    previous_utc_iso = previous_local.astimezone(timezone.utc).isoformat()
    return station_local_day_key(station, previous_utc_iso)
def ensure_ladder_hydration_prerequisite(station: str) -> dict:
    normalized_station = (station or "").strip().upper()
    result = None

    if not normalized_station:
        return {"status": "cache_missing", "reason": "station_missing"}

    now_utc_iso = datetime.now(timezone.utc).isoformat()
    station_day = station_local_day_key(normalized_station, now_utc_iso)
    series_tickers = _series_tickers_for_market_types(
        ensure_series_discovery_loaded().get(normalized_station),
        _configured_target_market_types(),
    )
    series_ticker = series_tickers[0] if series_tickers else None
    series_discovered = bool(series_tickers)
    cache_entries = {
        ticker: get_cached_series_markets(ticker)
        for ticker in series_tickers
    }
    cached = cache_entries.get(series_ticker) if series_ticker else None
    markets_cached = any(bool((entry or {}).get("markets")) for entry in cache_entries.values())

    if not series_tickers:
        result = {"status": "cache_missing", "reason": "series_missing", "station_local_day": station_day}
        with _SERIES_LOCK:
            _HYDRATION_PREREQUISITE_STATE[normalized_station] = {
                "attempted": True,
                "cache_valid": False,
                "series_discovered": series_discovered,
                "markets_cached": markets_cached,
                "status": result.get("status"),
                "reason": result.get("reason"),
                "evaluated_at_utc": now_utc_iso,
            }
        return result

    missing_cache_series = [ticker for ticker in series_tickers if cache_entries.get(ticker) is None]
    if missing_cache_series:
        result = {"status": "cache_missing", "reason": "cache_missing", "series_ticker": series_ticker, "station_local_day": station_day}
        with _SERIES_LOCK:
            _HYDRATION_PREREQUISITE_STATE[normalized_station] = {
                "attempted": True,
                "cache_valid": False,
                "series_discovered": series_discovered,
                "markets_cached": markets_cached,
                "status": result.get("status"),
                "reason": result.get("reason"),
                "evaluated_at_utc": now_utc_iso,
            }
        return result

    stale_cache_entries = []
    for ticker in series_tickers:
        entry = cache_entries.get(ticker) or {}
        stale_cache_entries.append((ticker, entry.get("station_local_day"), entry.get("hydrated_at_utc")))

    rollover_grace = False
    yesterday_day = _station_local_previous_day(normalized_station, now_utc_iso)

    invalid_entry = None
    if stale_cache_entries:
        invalid_entry = next(
            (
                (ticker, cached_day, hydrated_at_utc)
                for ticker, cached_day, hydrated_at_utc in stale_cache_entries
                if cached_day != station_day and (yesterday_day is None or cached_day != yesterday_day)
            ),
            None,
        )
        if invalid_entry is None and all(cached_day == yesterday_day for _, cached_day, _ in stale_cache_entries):
            rollover_grace = True

    if invalid_entry is not None:
        _, cached_day, hydrated_at_utc = invalid_entry
        result = {
            "status": "cache_stale",
            "reason": "station_local_day_mismatch",
            "series_ticker": series_ticker if len(series_tickers) <= 1 else list(series_tickers),
            "station_local_day": station_day,
            "yesterday_station_local_day": yesterday_day,
            "cached_station_local_day": cached_day,
            "hydrated_at_utc": hydrated_at_utc,
        }
        with _SERIES_LOCK:
            _HYDRATION_PREREQUISITE_STATE[normalized_station] = {
                "attempted": True,
                "cache_valid": False,
                "series_discovered": series_discovered,
                "markets_cached": markets_cached,
                "status": result.get("status"),
                "reason": result.get("reason"),
                "evaluated_at_utc": now_utc_iso,
            }
        return result

    result = {
        "status": "cache_valid",
        "series_ticker": series_ticker,
        "station_local_day": station_day,
        "hydrated_at_utc": max(
            (entry.get("hydrated_at_utc") for entry in cache_entries.values() if (entry or {}).get("hydrated_at_utc")),
            default=None,
        ),
        "rollover_grace": rollover_grace,
    }
    with _SERIES_LOCK:
        _HYDRATION_PREREQUISITE_STATE[normalized_station] = {
            "attempted": True,
            "cache_valid": True,
            "series_discovered": series_discovered,
            "markets_cached": markets_cached,
            "status": result.get("status"),
            "reason": result.get("reason"),
            "evaluated_at_utc": now_utc_iso,
        }
    return result
def get_hydration_prerequisite_state_snapshot() -> dict:
    with _SERIES_LOCK:
        return {
            station: dict(state)
            for station, state in _HYDRATION_PREREQUISITE_STATE.items()
            if isinstance(station, str) and isinstance(state, dict)
        }
def get_kalshi_connectivity_snapshot() -> dict:
    with _SERIES_LOCK:
        return {
            "series_discovery_attempted": _SERIES_DISCOVERY_ATTEMPT_COUNT > 0,
            "last_series_discovery_success_utc": _LAST_SERIES_DISCOVERY_SUCCESS_UTC,
            "last_series_discovery_error": _LAST_SERIES_DISCOVERY_ERROR,
            "markets_cache_population_count": int(_MARKETS_CACHE_POPULATION_COUNT),
        }
def get_last_hydration_execution_snapshot() -> dict:
    with _SERIES_LOCK:
        return copy.deepcopy(_LAST_HYDRATION_EXECUTION)
def enqueue_station_hydration(station: str, reason: str = "cache_missing") -> bool:
    normalized_station = (station or "").strip().upper()
    if not normalized_station:
        return False

    with _SERIES_LOCK:
        if normalized_station in _hydration_queue:
            return False
        _hydration_queue.append(normalized_station)
        _hydration_queue.sort()

    _LOGGER.info("hydration_enqueued station=%s reason=%s", normalized_station, reason)
    return True
def hydration_queue_snapshot(*, reference_ts: float | None = None) -> dict:
    with _SERIES_LOCK:
        now_ts = float(reference_ts) if reference_ts is not None else time.time()
        backoff_until = dict(_hydration_backoff_until)
        backoff_stations = sorted(
            station for station, until_ts in backoff_until.items() if float(until_ts or 0.0) > now_ts
        )
        next_backoff_expiry = (min(backoff_until.values()) if backoff_until else None)
        return {
            "queue": list(_hydration_queue),
            "queue_depth": len(_hydration_queue),
            "queued_stations": list(_hydration_queue),
            "backoff_until": backoff_until,
            "backoff_stations": backoff_stations,
            "stations_in_backoff": len(backoff_stations),
            "next_backoff_expiry": (float(next_backoff_expiry) if next_backoff_expiry is not None else None),
            "last_hydration_request_ts": float(_last_hydration_request_ts or 0.0),
        }
def process_hydration_queue_worker(market_types: set[str] | None = None) -> dict:
    global _last_hydration_request_ts

    if _current_kalshi_execution_domain() != "production":
        return {"status": "skipped", "reason": "non_production_domain"}

    now_ts = time.time()
    with _SERIES_LOCK:
        if not _hydration_queue:
            return {"status": "idle", "reason": "queue_empty"}
        if now_ts - float(_last_hydration_request_ts or 0.0) < MIN_HYDRATION_INTERVAL_SECONDS:
            return {"status": "rate_limited", "reason": "interval_guard"}

        station = _hydration_queue[0]
        backoff_until_ts = float(_hydration_backoff_until.get(station) or 0.0)
        if now_ts < backoff_until_ts:
            return {
                "status": "backoff",
                "station": station,
                "retry_after": int(max(backoff_until_ts - now_ts, 0)),
            }

        _hydration_queue.pop(0)
        _last_hydration_request_ts = now_ts

    selected_types = {
        token.strip().upper()
        for token in (market_types or _configured_target_market_types())
        if token and token.strip().upper() in {"HIGH", "LOW"}
    }
    _LOGGER.info("hydration_worker station=%s", station)
    try:
        result = hydrate_station_ladder_snapshot(station=station, market_types=selected_types)
        with _SERIES_LOCK:
            _hydration_backoff_until.pop(station, None)
        _LOGGER.info("hydration_success station=%s", station)
        return {"status": "hydrated", "station": station, "result": result}
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if int(status_code or 0) == 429:
            retry_after = int(now_ts + HYDRATION_BACKOFF_SECONDS)
            with _SERIES_LOCK:
                _hydration_backoff_until[station] = float(retry_after)
                if station not in _hydration_queue:
                    _hydration_queue.append(station)
                    _hydration_queue.sort()
            _LOGGER.warning(
                "hydration_backoff station=%s retry_after=%s",
                station,
                retry_after,
            )
            return {"status": "backoff", "station": station, "retry_after": HYDRATION_BACKOFF_SECONDS}
        raise
def hydrate_station_ladder_snapshot(station: str, market_types: set[str]) -> dict:
    if _current_kalshi_execution_domain() != "production":
        raise RuntimeError("hydrate_station_ladder_snapshot requires production execution domain")

    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot(normalized_station, market_types)
    series_tickers = _series_tickers_for_market_types(
        ensure_series_discovery_loaded().get(normalized_station),
        market_types,
    )
    series_ticker = series_tickers[0] if series_tickers else None
    cache_entries = [get_cached_series_markets(ticker) for ticker in series_tickers]
    return {
        "station": normalized_station,
        "series_ticker": series_ticker,
        "status": "hydrated",
        "hydrated_at_utc": max((entry.get("hydrated_at_utc") for entry in cache_entries if (entry or {}).get("hydrated_at_utc")), default=None),
        "station_local_day": next((entry.get("station_local_day") for entry in cache_entries if (entry or {}).get("station_local_day")), None),
        "market_count": len(snapshot.get("markets") or []),
    }
def _build_weather_event_ticker(station: str, market_type: str, observation_time_utc: str | datetime | None = None):
    # Extract city token from station ICAO (e.g., KDEN → DEN, KNYC → NYC)
    city_token = _station_to_city_token(station)
    if not city_token:
        return None

    date_token = _station_local_kalshi_date_token(station, observation_time_utc=observation_time_utc)
    return f"KX{market_type}{city_token}-{date_token}"
def _station_to_city_token(station: str) -> str | None:
    """Convert station ICAO to city token for ticker construction.
    
    Handles special cases:
    - KNYC → NYC (Central Park, not JFK/LGA)
    - KMDW → CHI (Midway, not ORD)
    - KDCA → DC (Reagan National)
    
    Returns uppercase city token or None if unknown station.
    """
    if not station:
        return None
    
    # Strip K prefix and get 3-letter code
    code = station
    if code.startswith("K") and len(code) == 4:
        code = code[1:]
    
    # Known mappings (station code → Kalshi ticker city token)
    # Verified against actual Kalshi tickers from /series?tags=Daily temperature API
    token_map = {
        "ATL": "ATL",
        "AUS": "AUS",
        "BOS": "BOS",
        "DCA": "DC",       # KXHIGHTDC / KXLOWTDC
        "DEN": "DEN",
        "DFW": "DAL",      # Kalshi uses DAL in ticker (KXHIGHTDAL), settlement is DFW
        "DAL": "DAL",
        "HOU": "HOU",
        "LAS": "LV",       # KXHIGHTLV / KXLOWTLV
        "LAX": "LAX",
        "MDW": "CHI",      # KXHIGHCHI / KXLOWTCHI (Midway, not O'Hare)
        "MIA": "MIA",
        "MSP": "MIN",      # KXHIGHTMIN / KXLOWTMIN
        "MSY": "NOLA",     # KXHIGHTNOLA / KXLOWTNOLA (Louis Armstrong)
        "LGA": "NYC",
        "JFK": "NYC",
        "EWR": "NYC",
        "NYC": "NYC",      # Central Park ASOS (KNYC) — NOT an airport
        "OKC": "OKC",
        "PHL": "PHIL",     # KXHIGHPHIL / KXLOWTPHIL
        "PHX": "PHX",
        "SAT": "SATX",     # KXHIGHTSATX / KXLOWTSATX
        "SEA": "SEA",
        "SFO": "SFO",
    }
    
    return token_map.get(code.upper())
def _filter_structured_markets(markets, station, market_types, rejection_counts=None, observation_time_utc: str | datetime | None = None):
    rejection_counts = rejection_counts if isinstance(rejection_counts, dict) else None

    def _record_rejection(reason: str):
        if rejection_counts is None:
            return
        rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

    normalized_station = (station or "").strip().upper()
    city_token = _station_to_city_token(normalized_station)

    if not city_token:
        return []

    date_token = _station_local_kalshi_date_token(normalized_station, observation_time_utc=observation_time_utc)

    filtered = []

    for market in markets:
        ticker = (market.get("ticker") or "").upper()
        status = str(market.get("status") or "").upper()

        if status and status not in {"OPEN", "ACTIVE"}:
            _record_rejection("inactive_market")
            continue

        if city_token not in ticker:
            _record_rejection("city_token_mismatch")
            continue

        if date_token not in ticker:
            _record_rejection("date_mismatch")
            continue

        if market_types and not any(mt in ticker for mt in market_types):
            _record_rejection("market_type_mismatch")
            continue

        filtered.append(market)

    return filtered
def _extract_strike_from_ticker(ticker):
    if not ticker:
        return None
    match = re.search(r"B(\d+)$", ticker)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None
def classify_proximity(distance_f: float) -> str:
    if distance_f <= 0.25:
        return "CRITICAL"
    if distance_f <= 0.5:
        return "NEAR"
    if distance_f <= 0.8:
        return "APPROACHING"
    return "FAR"
def _select_event_ticker_for_series(
    events: list[dict],
    station: str,
    series_ticker: str,
    observation_time_utc: str | datetime | None = None,
) -> str | None:
    normalized_station = (station or "").strip().upper()
    normalized_series_ticker = (series_ticker or "").strip().upper()
    if not normalized_series_ticker:
        return None

    date_token = _station_local_kalshi_date_token(normalized_station, observation_time_utc=observation_time_utc)
    if not date_token:
        return None

    candidates: list[tuple[int, str]] = []
    for event in (events or []):
        event_ticker = str((event or {}).get("event_ticker") or "").strip().upper()
        if not event_ticker:
            continue
        if not event_ticker.startswith(f"{normalized_series_ticker}-"):
            continue
        if not event_ticker.endswith(f"-{date_token}"):
            continue

        score = 0
        status = str((event or {}).get("status") or "").strip().lower()
        if status in ("open", "active"):
            score += 1

        candidates.append((score, event_ticker))

    if not candidates:
        return None

    return sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]
def build_structured_snapshot(
    station: str,
    market_types: set,
    observation_time_utc: str | datetime | None = None,
):
    global _MARKETS_CACHE_POPULATION_COUNT
    normalized_station = (station or "").strip().upper()
    if isinstance(observation_time_utc, datetime):
        evaluated_at_utc = (
            observation_time_utc.astimezone(timezone.utc)
            if observation_time_utc.tzinfo
            else observation_time_utc.replace(tzinfo=timezone.utc)
        ).isoformat()
    elif observation_time_utc is not None:
        evaluated_at_utc = (parse_iso_utc(str(observation_time_utc)) or datetime.now(timezone.utc)).isoformat()
    else:
        evaluated_at_utc = datetime.now(timezone.utc).isoformat()
    series_by_station = ensure_series_discovery_loaded()
    discovered_series_tickers = series_by_station.get(normalized_station)
    series_tickers = _series_tickers_for_market_types(discovered_series_tickers, market_types)
    series_ticker = series_tickers[0] if series_tickers else None

    selected_types = {
        token.strip().upper()
        for token in (market_types or set())
        if token and token.strip().upper() in {"HIGH", "LOW"}
    }

    fetched_markets = []
    event_market_cache = {}
    cache_written = False
    for series_ticker_item in series_tickers:
        inferred_market_type = _infer_series_market_type(ticker=series_ticker_item)

        inferred_event_ticker = None
        if inferred_market_type:
            inferred_event_ticker = _build_weather_event_ticker(
                normalized_station,
                inferred_market_type,
                observation_time_utc=evaluated_at_utc,
            )

        series_markets = []
        direct_markets_resolved = False
        if inferred_event_ticker:
            try:
                if inferred_event_ticker in event_market_cache:
                    series_markets = list(event_market_cache[inferred_event_ticker])
                else:
                    inferred_data = _kalshi_public_get(f"/markets?event_ticker={inferred_event_ticker}&limit=100")
                    series_markets = inferred_data.get("markets") or []
                    event_market_cache[inferred_event_ticker] = list(series_markets)
                if series_markets:
                    direct_markets_resolved = True
                    _LOGGER.info(
                        "hydration_market_fetch station=%s event=%s markets=%s",
                        normalized_station,
                        inferred_event_ticker,
                        len(series_markets),
                    )
            except Exception:
                series_markets = []

        if direct_markets_resolved:
            fetched_markets.extend(series_markets)
            with _SERIES_LOCK:
                _SERIES_MARKETS_CACHE[series_ticker_item] = {
                    "markets": list(series_markets),
                    "hydrated_at_utc": evaluated_at_utc,
                    "station_local_day": station_local_day_key(normalized_station, evaluated_at_utc),
                }
                _MARKETS_CACHE_POPULATION_COUNT += 1
            # Persist market cache for restart survival
            _persist_market_cache(
                f"series:{normalized_station}:{series_ticker_item}",
                normalized_station,
                _SERIES_MARKETS_CACHE[series_ticker_item],
            )
            cache_written = True
            continue

        with _SERIES_LOCK:
            cached_events_entry = copy.deepcopy(_SERIES_EVENTS_CACHE.get(series_ticker_item))

        cache_reference_utc = parse_iso_utc(evaluated_at_utc)

        cached_events = None
        if cached_events_entry and cache_reference_utc is not None:
            cached_fetched_at = parse_iso_utc(cached_events_entry.get("fetched_at_utc"))
            if cached_fetched_at is not None:
                age_seconds = (cache_reference_utc - cached_fetched_at).total_seconds()
                if age_seconds < SERIES_EVENTS_CACHE_TTL_SECONDS:
                    cached_events = list(cached_events_entry.get("events") or [])

        if cached_events is not None:
            events_data = {"events": cached_events}
        else:
            events_data = _kalshi_public_get(f"/events?series_ticker={series_ticker_item}")
            with _SERIES_LOCK:
                _SERIES_EVENTS_CACHE[series_ticker_item] = {
                    "events": list(events_data.get("events") or []),
                    "fetched_at_utc": evaluated_at_utc,
                }
        event_ticker = _select_event_ticker_for_series(
            events=events_data.get("events") or [],
            station=normalized_station,
            series_ticker=series_ticker_item,
            observation_time_utc=evaluated_at_utc,
        )
        if event_ticker:
            if event_ticker in event_market_cache:
                series_markets = list(event_market_cache[event_ticker])
            else:
                data = _kalshi_public_get(f"/markets?event_ticker={event_ticker}&limit=100")
                series_markets = data.get("markets") or []
                event_market_cache[event_ticker] = list(series_markets)
            _LOGGER.info(
                "hydration_market_fetch station=%s event=%s markets=%s",
                normalized_station,
                event_ticker,
                len(series_markets),
            )
            fetched_markets.extend(series_markets)
        with _SERIES_LOCK:
            _SERIES_MARKETS_CACHE[series_ticker_item] = {
                "markets": list(series_markets),
                "hydrated_at_utc": evaluated_at_utc,
                "station_local_day": station_local_day_key(normalized_station, evaluated_at_utc),
            }
            _MARKETS_CACHE_POPULATION_COUNT += 1
        # Persist market cache for restart survival
        _persist_market_cache(
            f"series:{normalized_station}:{series_ticker_item}",
            normalized_station,
            _SERIES_MARKETS_CACHE[series_ticker_item],
        )
        cache_written = True

    rejection_counts = {}
    filtered_markets = _filter_structured_markets(
        fetched_markets,
        normalized_station,
        selected_types,
        rejection_counts,
        observation_time_utc=evaluated_at_utc,
    )

    with _SERIES_LOCK:
        _LAST_HYDRATION_EXECUTION[normalized_station] = {
            "evaluated_at_utc": evaluated_at_utc,
            "station": normalized_station,
            "series_ticker": series_ticker,
            "series_tickers": list(series_tickers),
            "raw_market_count": len(fetched_markets),
            "filtered_market_count": len(filtered_markets),
            "rejection_counts": dict(rejection_counts),
            "cache_written": cache_written,
        }

    structured_markets = _assemble_structured_snapshot_markets(
        filtered_markets,
        normalized_station,
        selected_types,
    )

    return {
        "station": normalized_station,
        "market_types": sorted(selected_types),
        "markets": structured_markets["markets"],
        "observed": {"current_temp_f": structured_markets["observed_value"]},
    }
def build_structured_snapshot_from_cache(
    station: str,
    market_types: set,
    observation_time_utc: str | datetime | None = None,
):
    normalized_station = (station or "").strip().upper()
    selected_types = {
        token.strip().upper()
        for token in (market_types or set())
        if token and token.strip().upper() in {"HIGH", "LOW"}
    }

    with _SERIES_LOCK:
        series_tickers = _series_tickers_for_market_types(_SERIES_BY_STATION.get(normalized_station), market_types)
        series_ticker = series_tickers[0] if series_tickers else None
        cached_markets = []
        for ticker in series_tickers:
            cached_markets.extend(list((_SERIES_MARKETS_CACHE.get(ticker) or {}).get("markets") or []))

    rejection_counts = {}
    filtered_markets = _filter_structured_markets(
        cached_markets,
        normalized_station,
        selected_types,
        rejection_counts,
        observation_time_utc=observation_time_utc,
    )

    structured_markets = _assemble_structured_snapshot_markets(
        filtered_markets,
        normalized_station,
        selected_types,
    )

    return {
        "station": normalized_station,
        "series_ticker": series_ticker,
        "series_tickers": list(series_tickers),
        "market_types": sorted(selected_types),
        "markets": structured_markets["markets"],
        "pre_directional_market_count": structured_markets["pre_directional_market_count"],
        "post_directional_market_count": len(structured_markets["markets"]),
        "empty_reason": (
            "cache_missing_or_empty"
            if len(cached_markets) == 0
            else "filtered_to_zero"
            if len(filtered_markets) == 0
            else "no_directional_ladder_match"
            if len(structured_markets["markets"]) == 0
            else None
        ),
        "raw_market_count": len(cached_markets),
        "filtered_market_count": len(filtered_markets),
        "rejection_counts": dict(rejection_counts),
        "cache_written": bool(cached_markets),
        "hydration_source": "cache_only",
        "observed": {"current_temp_f": structured_markets["observed_value"]},
    }
def _build_ladder_structure(markets):
    ladder = []

    for market in markets or []:
        strike_type = market.get("strike_type")
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")

        if strike_type == "between" and floor_strike is not None and cap_strike is not None:
            ladder.append(
                {
                    "kind": "between",
                    "low": int(floor_strike),
                    "high": int(cap_strike),
                }
            )
        elif strike_type == "less" and cap_strike is not None:
            ladder.append(
                {
                    "kind": "less",
                    "threshold": int(cap_strike),
                }
            )
        elif strike_type == "greater" and floor_strike is not None:
            ladder.append(
                {
                    "kind": "greater",
                    "threshold": int(floor_strike),
                }
            )

    def _sort_key(item):
        if item["kind"] == "between":
            return item["low"]
        return item["threshold"]

    ladder.sort(key=_sort_key)
    return ladder
def _determine_bucket(temp_f, ladder, market_type):
    if temp_f is None or not ladder:
        return (None, False)

    market_type = (market_type or "").upper()
    between_buckets = [item for item in ladder if item.get("kind") == "between"]

    if not between_buckets:
        return (None, False)

    if market_type == "HIGH":
        first_between = between_buckets[0]
        if temp_f < first_between["low"]:
            return (None, False)

        greater_rungs = [item for item in ladder if item.get("kind") == "greater"]
        if greater_rungs and temp_f > greater_rungs[-1]["threshold"]:
            return (len(between_buckets), True)

    elif market_type == "LOW":
        last_between = between_buckets[-1]
        if temp_f > last_between["high"]:
            return (None, False)

        less_rungs = [item for item in ladder if item.get("kind") == "less"]
        if less_rungs and temp_f < less_rungs[0]["threshold"]:
            return (-1, True)

    for idx, bucket in enumerate(between_buckets):
        if bucket["low"] <= temp_f < bucket["high"]:
            return (idx, False)

    return (None, False)
def get_state():
    cfg = get_default_config()
    auth_configured = bool(cfg["base_url"] and cfg["key_id"] and cfg["key_pem"])
    return {
        "base_url": cfg["base_url"],
        "auth_configured": auth_configured,
    }
def get_metrics():
    cfg = get_default_config()
    return {
        "base_url_configured": bool(cfg["base_url"]),
        "auth_configured": bool(cfg["base_url"] and cfg["key_id"] and cfg["key_pem"]),
    }
