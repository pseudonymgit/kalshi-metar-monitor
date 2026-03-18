"""
Kalshi Monitor

This module owns market discovery, ladder hydration, and deterministic market
transition interpretation for the METAR runtime.

Responsibilities
- Discover and cache Kalshi market ladders per station/day.
- Build structured snapshots for market eligibility evaluation.
- Convert ladder transitions into alert-ready context for the alert layer.

This module MUST NOT
- Ingest raw METAR observations directly.
- Emit transition events (owned by transition_emitter).
- Mutate observability state.
"""

import base64
import copy
import contextvars
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import has_request_context, request

from core.authoritative_state import immutable_public_state_snapshot
from core.station_time import parse_iso_utc, station_local_day_key, to_station_local
from core.alert_schema import ALERT_SCHEMA_VERSION
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Optional reuse of Phase 1 timezone helper
try:
    from core.metar_monitor import _to_local
except Exception:
    _to_local = None

try:
    from core.metar_monitor import get_state as get_metar_state
except Exception:
    get_metar_state = None
    
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

_STATION_CITY_TOKEN_MAP = {
    "KDEN": "DEN",
    "KLAX": "LAX",
    "KNYC": "NY",
    "KPHL": "PHIL",
    "KMDW": "CHI",
    "KMIA": "MIA",
    "KAUS": "AUS",
}

_KALSHI_EXECUTION_DOMAIN = contextvars.ContextVar("kalshi_execution_domain", default="production")
# Replay/diagnostics safety: these domains are hard-blocked from live
# Kalshi calls to preserve deterministic replay and read-only tooling.
_FORBIDDEN_KALSHI_DOMAINS = frozenset({"observability", "diagnostics", "audit", "replay"})
_KALSHI_PUBLIC_SESSION = requests.Session()
_KALSHI_PUBLIC_SESSION.trust_env = False
_LOGGER = logging.getLogger(__name__)
logger = _LOGGER
MIN_HYDRATION_INTERVAL_SECONDS = 1
HYDRATION_BACKOFF_SECONDS = 120
DIRECTIONAL_STRIKE_WINDOW_SIZE = 3
SERIES_EVENTS_CACHE_TTL_SECONDS = 60


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
        directional = [entry for entry in ladder if entry[0] > observed]
        if directional:
            directional.sort(key=lambda entry: entry[0])
            return [entry[1] for entry in directional[:window_size]]
        return [max(ladder, key=lambda entry: entry[0])[1]]

    if direction == "LOW":
        directional = [entry for entry in ladder if entry[0] < observed]
        if directional:
            directional.sort(key=lambda entry: entry[0], reverse=True)
            return [entry[1] for entry in directional[:window_size]]
        return [min(ladder, key=lambda entry: entry[0])[1]]

    return [entry[1] for entry in ladder]

class kalshi_execution_domain:
    def __init__(self, domain: str):
        self._domain = (domain or "production").strip().lower() or "production"
        self._token = None

    def __enter__(self):
        self._token = _KALSHI_EXECUTION_DOMAIN.set(self._domain)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _KALSHI_EXECUTION_DOMAIN.reset(self._token)


def _current_kalshi_execution_domain() -> str:
    return str(_KALSHI_EXECUTION_DOMAIN.get() or "production").strip().lower() or "production"


def set_kalshi_execution_domain(domain: str):
    normalized = (domain or "production").strip().lower() or "production"
    return _KALSHI_EXECUTION_DOMAIN.set(normalized)


def reset_kalshi_execution_domain(token) -> None:
    _KALSHI_EXECUTION_DOMAIN.reset(token)


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


def _alert_db_path() -> str:
    return os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")


def _load_current_epoch_context(station: str, market_type: str, obs_time_utc: str):
    normalized_station = (station or "").strip().upper()
    normalized_market_type = (market_type or "").strip().upper() or None
    local_trading_date = station_local_day_key(normalized_station, obs_time_utc)
    if not normalized_station or local_trading_date == "unknown":
        return {}

    try:
        conn = sqlite3.connect(f"file:{_alert_db_path()}?mode=ro", uri=True, timeout=1)
    except Exception:
        return {}

    try:
        row = conn.execute(
            """
            SELECT settlement_bucket,
                   prior_settlement_bucket,
                   settlement_jump_magnitude,
                   epoch_status,
                   reversion_occurred,
                   first_reversion_timestamp_utc,
                   max_excursion_above_settlement,
                   terminal_state_reached
            FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND local_trading_date = ?
            ORDER BY CASE WHEN epoch_status = 'open' THEN 0 ELSE 1 END,
                     id DESC
            LIMIT 1
            """,
            (normalized_station, normalized_market_type, normalized_market_type, local_trading_date),
        ).fetchone()
        if not row:
            return {}

        return {
            "settlement_bucket": row[0],
            "prior_settlement_bucket": row[1],
            "settlement_jump_magnitude": row[2],
            "epoch_status": row[3],
            "reversion_occurred": bool(row[4]),
            "first_reversion_timestamp_utc": row[5],
            "max_excursion_above_settlement": row[6],
            "terminal_state_reached": bool(row[7]),
        }
    except Exception:
        return {}
    finally:
        conn.close()


def _derive_attention_phrase(epoch_context):
    if bool(epoch_context.get("terminal_state_reached")):
        return "TERMINAL STATE"
    if bool(epoch_context.get("reversion_occurred")):
        return "REVERTED AFTER SETTLEMENT"

    settlement_bucket = epoch_context.get("settlement_bucket")
    prior_settlement_bucket = epoch_context.get("prior_settlement_bucket")
    if (
        isinstance(settlement_bucket, int)
        and isinstance(prior_settlement_bucket, int)
        and settlement_bucket > prior_settlement_bucket
    ):
        return "NEW SETTLEMENT / NO REVERSION YET"

    if str(epoch_context.get("epoch_status") or "").lower() == "open":
        return "OPEN EPOCH / ALERTABLE"

    return "EPOCH CONTEXT AVAILABLE"


def get_default_config():
    return {
        "base_url": (os.getenv("KALSHI_BASE_URL") or "").strip(),
        "key_id": (os.getenv("KALSHI_KEY_ID") or "").strip(),
        "key_pem": os.getenv("KALSHI_PRIVATE_KEY_PEM") or "",
    }


def _sign_request_rsa(private_key_pem, timestamp, method, path, body=""):
    message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    signature = private_key.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _kalshi_get(path):
    cfg = get_default_config()
    base_url = cfg["base_url"].rstrip("/")
    key_id = cfg["key_id"]
    key_pem = cfg["key_pem"]

    if not base_url:
        raise ValueError("KALSHI_BASE_URL is not configured")
    if "/trade-api/v2" in path:
        raise ValueError("path must not include /trade-api/v2")
    if not key_id or not key_pem:
        raise ValueError("Kalshi auth is not configured")

    normalized_path = path if path.startswith("/") else f"/{path}"
    timestamp = str(int(time.time() * 1000))
    signature = _sign_request_rsa(
        key_pem,
        timestamp,
        "GET",
        normalized_path,
        "",
    )

    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }

    response = requests.get(
        f"{base_url}{normalized_path}",
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


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


# Architectural boundary: this is the only public API call path and it
# enforces execution-domain guards before any network side effect occurs.
def _kalshi_public_get(path):
    execution_domain = _current_kalshi_execution_domain()
    normalized_path = path if path.startswith("/") else f"/{path}"

    if execution_domain in _FORBIDDEN_KALSHI_DOMAINS:
        request_path = str(request.path or "") if has_request_context() else None
        _LOGGER.warning(
            "kalshi_public_get_blocked domain=%s path=%s request_path=%s",
            execution_domain,
            normalized_path,
            request_path,
        )
        raise RuntimeError(f"Live Kalshi call attempted in forbidden execution domain '{execution_domain}'")
    if has_request_context() and str(request.path or "").startswith("/observability/"):
        _LOGGER.warning(
            "kalshi_public_get_blocked domain=%s path=%s request_path=%s",
            execution_domain,
            normalized_path,
            str(request.path or ""),
        )
        raise RuntimeError("Live Kalshi call attempted in observability path")

    base_url = (
        os.getenv("KALSHI_PUBLIC_BASE_URL")
        or "https://api.elections.kalshi.com/trade-api/v2"
    ).rstrip("/")
    response = _KALSHI_PUBLIC_SESSION.get(f"{base_url}{normalized_path}", timeout=10)
    response.raise_for_status()
    return response.json()


def get_public_markets(limit=5):
    """
    Fetch public Kalshi markets (no authentication).
    """
    data = _kalshi_public_get(f"/markets?limit={int(limit)}")
    return {
        "cursor": data.get("cursor"),
        "count": len(data.get("markets", [])),
        "markets": data.get("markets", []),
    }


def _get_all_public_markets(max_pages=5, page_limit=200):
    markets = []
    cursor = None

    for _ in range(max_pages):
        path = f"/markets?limit={int(page_limit)}"
        if cursor:
            path = f"{path}&cursor={cursor}"

        data = _kalshi_public_get(f"/markets?series_ticker={series_ticker}&limit=200")

        _LOGGER.warning(
            "kalshi_probe station=%s series=%s response_keys=%s",
            normalized_station,
            series_ticker,
            list(data.keys()) if isinstance(data, dict) else type(data),
        )

        markets = data.get("markets") or []

        _LOGGER.warning(
            "kalshi_probe_market_count station=%s markets=%s",
            normalized_station,
            len(markets),
        )
        
        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


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

    return list(discovered_markets)


def build_market_derived_station_universe(max_pages=5, page_limit=200):
    city_tokens = set()
    cursor = None

    for _ in range(max_pages):
        path = f"/markets?limit={int(page_limit)}&status=open"
        if cursor:
            path = f"{path}&cursor={cursor}"

        data = _kalshi_public_get(path)
        markets = data.get("markets") or []

        for market in markets:
            if str(market.get("status") or "").upper() != "OPEN":
                continue

            ticker = str(market.get("ticker") or "").strip().upper()
            prefix = None
            if ticker.startswith("KXHIGH"):
                prefix = "KXHIGH"
            elif ticker.startswith("KXLOW"):
                prefix = "KXLOW"

            if not prefix:
                continue

            remainder = ticker[len(prefix):]
            city_token, _, _ = remainder.partition("-")
            city_token = city_token.strip().upper()
            if city_token:
                city_tokens.add(city_token)

        cursor = data.get("cursor")
        if not cursor:
            break

    return sorted(city_tokens)


def resolve_settlement_station(token: str) -> str | None:
    normalized_token = (token or "").strip().upper()
    if not normalized_token:
        return None

    for station, city_token in _STATION_CITY_TOKEN_MAP.items():
        if (city_token or "").strip().upper() == normalized_token:
            return station

    return _EXPLICIT_SETTLEMENT_STATION_OVERRIDES.get(normalized_token)


def build_market_polling_station_universe(max_pages=5, page_limit=200):
    tokens = build_market_derived_station_universe(max_pages=max_pages, page_limit=page_limit)
    stations = set()

    for token in tokens:
        try:
            station = resolve_settlement_station(token)
        except Exception:
            continue
        if station:
            stations.add(station)

    return sorted(stations)


def get_discovered_weather_market_station_mapping() -> dict:
    with _SERIES_LOCK:
        return {
            station: list(markets)
            for station, markets in _DISCOVERED_WEATHER_MARKETS_BY_STATION.items()
        }


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

    if _to_local:
        try:
            now_local = _to_local(station, now_utc)
        except Exception:
            now_local = now_utc
    else:
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


def _discover_series_for_stations():
    data = _kalshi_public_get("/series?tags=Daily%20temperature")
    series_items = data.get("series") or []
    configured_stations = set((_get_active_stations() or set()))
    configured_stations.update(_STATION_CITY_TOKEN_MAP.keys())

    reverse_city_token_map = {
        city_token: station
        for station, city_token in _STATION_CITY_TOKEN_MAP.items()
    }

    for item in series_items:
        frequency = (item.get("frequency") or "").strip().lower()
        title = (item.get("title") or "").strip().lower()
        ticker = (item.get("ticker") or "").strip().upper()

        if frequency != "daily":
            continue
        if "highest" not in title:
            continue
        if not ticker.startswith("KXHIGH"):
            continue

        discovered_token = ticker[len("KXHIGH"):]
        discovered_station = reverse_city_token_map.get(discovered_token)
        if discovered_station:
            configured_stations.add(discovered_station)

    discovered = {}

    for station in sorted(configured_stations):
        station_code = (station or "").strip().upper()
        city_token = _STATION_CITY_TOKEN_MAP.get(station_code, "")

        candidates = []
        for item in series_items:
            frequency = (item.get("frequency") or "").strip().lower()
            title = (item.get("title") or "").strip()
            ticker = (item.get("ticker") or "").strip().upper()

            if frequency != "daily":
                continue
            if "highest" not in title.lower():
                continue
            if not ticker:
                continue

            if station_code in ticker or (city_token and city_token in ticker):
                score = 0
                if city_token:
                    if ticker == f"KXHIGH{city_token}":
                        score = 5
                    elif ticker == f"KX{city_token}HIGH":
                        score = 4
                    elif ticker.startswith("KXHIGH") and city_token in ticker:
                        score = 3
                    elif "HIGH" in ticker and city_token in ticker:
                        score = 2
                    else:
                        score = 1
                candidates.append((score, ticker))

        if candidates:
            ranked_candidates = sorted(candidates, key=lambda candidate: (-candidate[0], candidate[1]))
            discovered[station_code] = _normalize_series_tickers([candidate[1] for candidate in ranked_candidates])

    return discovered


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
        if _current_kalshi_execution_domain() != "production":
            record_connectivity_state = False
        if has_request_context() and str(request.path or "").startswith("/observability/"):
            record_connectivity_state = False

        if record_connectivity_state:
            _SERIES_DISCOVERY_ATTEMPT_COUNT += 1
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
        series_tickers = _normalize_series_tickers(_SERIES_BY_STATION.get(normalized_station))
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
    series_tickers = ensure_series_discovery_loaded().get(normalized_station) or []
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

    selected_types = set(market_types or {"HIGH"})
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


# Replay safety rule: hydration is production-only because it performs
# live market reads; replay must use persisted deterministic history.
def hydrate_station_ladder_snapshot(station: str, market_types: set[str]) -> dict:
    if _current_kalshi_execution_domain() != "production":
        raise RuntimeError("hydrate_station_ladder_snapshot requires production execution domain")

    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot(normalized_station, market_types)
    series_tickers = ensure_series_discovery_loaded().get(normalized_station) or []
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
    city_token = _STATION_CITY_TOKEN_MAP.get(station)
    if not city_token:
        return None

    date_token = _station_local_kalshi_date_token(station, observation_time_utc=observation_time_utc)
    return f"KX{market_type}{city_token}-{date_token}"


def _filter_structured_markets(markets, station, market_types, rejection_counts=None, observation_time_utc: str | datetime | None = None):
    rejection_counts = rejection_counts if isinstance(rejection_counts, dict) else None

    def _record_rejection(reason: str):
        if rejection_counts is None:
            return
        rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

    normalized_station = (station or "").strip().upper()
    city_token = _STATION_CITY_TOKEN_MAP.get(normalized_station)

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

    candidates: list[tuple[int, str]] = []
    for event in (events or []):
        event_ticker = str((event or {}).get("event_ticker") or "").strip().upper()
        if not event_ticker:
            continue

        score = 0
        if event_ticker.startswith(f"{normalized_series_ticker}-"):
            score += 4
        if date_token and event_ticker.endswith(f"-{date_token}"):
            score += 2

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
    if isinstance(discovered_series_tickers, str):
        series_tickers = [discovered_series_tickers]
    elif isinstance(discovered_series_tickers, (list, tuple, set)):
        series_tickers = [ticker for ticker in discovered_series_tickers if ticker]
    else:
        series_tickers = []
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
        inferred_market_type = None
        if series_ticker_item.startswith("KXHIGH"):
            inferred_market_type = "HIGH"
        elif series_ticker_item.startswith("KXLOW"):
            inferred_market_type = "LOW"

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
            cached_event_markets = event_market_cache.get(event_ticker)
            if cached_event_markets:
                series_markets = list(cached_event_markets)
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
            "raw_market_count": len(fetched_markets),
            "filtered_market_count": len(filtered_markets),
            "rejection_counts": dict(rejection_counts),
            "cache_written": cache_written,
        }

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
    observed_value = None
    if get_metar_state:
        try:
            observed_value = (
                get_metar_state().get("last_obs", {})
                .get(normalized_station, {})
                .get("temp_f")
            )
        except Exception:
            pass
    if observed_value is None:
        try:
            state_snapshot = immutable_public_state_snapshot()
            last = state_snapshot["last_obs"].get(normalized_station)
            if last and "temp_f" in last:
                observed_value = float(last["temp_f"])
        except Exception:
            pass

    if observed_value is not None and len(selected_types) == 1:
        direction = next(iter(sorted(selected_types)))
        markets = _directional_strike_window(markets, observed_value, direction)

    return {
        "station": normalized_station,
        "market_types": sorted(selected_types),
        "markets": markets,
        "observed": {"current_temp_f": observed_value},
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
        series_tickers = _normalize_series_tickers(_SERIES_BY_STATION.get(normalized_station))
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

    markets = []
    pre_directional_market_count = 0
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
    observed_value = None
    if get_metar_state:
        try:
            observed_value = (
                get_metar_state().get("last_obs", {})
                .get(normalized_station, {})
                .get("temp_f")
            )
        except Exception:
            pass
    if observed_value is None:
        try:
            state_snapshot = immutable_public_state_snapshot()
            last = state_snapshot["last_obs"].get(normalized_station)
            if last and "temp_f" in last:
                observed_value = float(last["temp_f"])
        except Exception:
            pass

    if observed_value is not None and len(selected_types) == 1:
        direction = next(iter(sorted(selected_types)))
        markets = _directional_strike_window(markets, observed_value, direction)

    return {
        "station": normalized_station,
        "series_ticker": series_ticker,
        "market_types": sorted(selected_types),
        "markets": markets,
        "pre_directional_market_count": pre_directional_market_count,
        "post_directional_market_count": len(markets),
        "empty_reason": (
            "cache_missing_or_empty"
            if len(cached_markets) == 0
            else "filtered_to_zero"
            if len(filtered_markets) == 0
            else "no_directional_ladder_match"
            if len(markets) == 0
            else None
        ),
        "raw_market_count": len(cached_markets),
        "filtered_market_count": len(filtered_markets),
        "rejection_counts": dict(rejection_counts),
        "cache_written": bool(cached_markets),
        "hydration_source": "cache_only",
        "observed": {"current_temp_f": observed_value},
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


# Alert eligibility gate: ladder state transition determines whether a
# market is eligible for alerting (entry/bucket/final) or suppressed.
def process_ladder_transition(station, market_type, snapshot, current_temp):
    markets = (snapshot or {}).get("markets") or []
    if not markets:
        return {
            "should_alert": False,
            "reason": None,
            "outcome_hint": "NO_ELIGIBLE_MARKET",
        }

    event_ticker = markets[0].get("event_ticker")
    if not event_ticker:
        return {
            "should_alert": False,
            "reason": None,
            "outcome_hint": "NO_ELIGIBLE_MARKET",
        }

    ladder = _build_ladder_structure(markets)
    bucket_index, final_now = _determine_bucket(current_temp, ladder, market_type)

    normalized_station = (station or "").strip().upper()
    normalized_market_type = (market_type or "").strip().upper()
    with _LADDER_LOCK:
        prev_event_state_key = _ladder_event_keys.get((normalized_station, normalized_market_type))
        state_key = f"{normalized_station}_{normalized_market_type}_{event_ticker}"

        if prev_event_state_key and prev_event_state_key != state_key:
            _ladder_state.pop(prev_event_state_key, None)

        _ladder_event_keys[(normalized_station, normalized_market_type)] = state_key

        state = _ladder_state.get(
            state_key,
            {"inside": False, "bucket_index": None, "final_hit": False},
        )

        should_alert = False
        reason = None
        terminal_state_blocked = bool(final_now and state.get("final_hit"))
        prior_bucket_index = state.get("bucket_index")

        if not state["inside"] and bucket_index is not None:
            should_alert = True
            reason = "entry"
        elif state["inside"] and bucket_index is not None and bucket_index != state.get("bucket_index"):
            should_alert = True
            reason = "bucket"

        if final_now and not state.get("final_hit"):
            should_alert = True
            reason = "final"

        if bucket_index is None:
            state["inside"] = False
            state["bucket_index"] = None
            state["final_hit"] = False
        else:
            state["inside"] = True
            state["bucket_index"] = bucket_index
            if final_now:
                state["final_hit"] = True

        _ladder_state[state_key] = state

    direction = None
    if should_alert:
        if prior_bucket_index is not None and bucket_index is not None and bucket_index != prior_bucket_index:
            direction = "UP" if bucket_index > prior_bucket_index else "DOWN"
        else:
            direction = "UP"

    return {
        "should_alert": should_alert,
        "reason": reason,
        "bucket_index": bucket_index,
        "direction": direction,
        "terminal_state_blocked": terminal_state_blocked,
        "outcome_hint": "TERMINAL_STATE" if terminal_state_blocked else None,
    }


def _send_kalshi_market_alert(ticker, prev_state, curr_state):
    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return False

    fields = [{"name": "Ticker", "value": str(ticker)}]

    prev_price = None if not prev_state else prev_state.get("last_price")
    prev_status = None if not prev_state else prev_state.get("status")
    curr_price = curr_state.get("last_price")
    curr_status = curr_state.get("status")

    if prev_state is None or prev_price != curr_price:
        fields.append(
            {
                "name": "Last Price",
                "value": _format_change(prev_price, curr_price),
            }
        )

    if prev_state is None or prev_status != curr_status:
        fields.append(
            {
                "name": "Status",
                "value": _format_change(prev_status, curr_status),
            }
        )

    payload = {
        "content": None,
        "embeds": [
            {
                "title": "Kalshi Market Update",
                "fields": fields,
                "footer": {
                    "text": "Kalshi Monitor (Public Mode)",
                },
            }
        ],
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    return 200 <= response.status_code < 300


def send_composed_weather_market_alert(
    station: str,
    market_types: set,
    transition_reason: str = None,
    prev_temp_f=None,
    now_temp_f=None,
    delta_f=None,
    obs_time_utc=None,
):
    normalized_station = (station or "").strip().upper()
    snapshot = build_structured_snapshot_from_cache(
        normalized_station,
        market_types,
        observation_time_utc=obs_time_utc,
    )
    markets = snapshot.get("markets", [])
    current_temp_f = (snapshot.get("observed") or {}).get("current_temp_f")
    market_types_list = snapshot.get("market_types", [])

    if not markets:
        enqueue_station_hydration(normalized_station, reason="alert_send_cache_missing")
        return {"ok": False, "reason": "no_markets"}

    webhook_url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return {
            "ok": False,
            "reason": "missing_webhook",
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": None,
            "webhook_response_text": None,
        }

    def _to_price(value, fallback):
        chosen = value if value is not None else fallback
        if chosen is None:
            return "N/A"
        try:
            return str(int(round(float(chosen))))
        except (TypeError, ValueError):
            return "N/A"

    def _sort_key(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        if strike_type == "less":
            return float("-inf")
        if strike_type == "greater":
            return float("inf")
        if floor is None:
            return float("inf")
        return float(floor)

    sorted_markets = sorted(markets, key=_sort_key)

    current_index = None
    for idx, market in enumerate(sorted_markets):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        if current_temp_f is None:
            continue
        if strike_type == "less" and cap is not None and current_temp_f <= float(cap):
            current_index = idx
            break
        if (
            strike_type == "between"
            and floor is not None
            and cap is not None
            and float(floor) <= current_temp_f < float(cap)
        ):
            current_index = idx
            break
        if strike_type == "greater" and floor is not None and current_temp_f >= float(floor):
            current_index = idx
            break

    if current_index is None and sorted_markets:
        current_index = 0 if current_temp_f is None else len(sorted_markets) - 1

    market_type = market_types_list[0] if market_types_list else ""
    event_ticker = (sorted_markets[0] if sorted_markets else {}).get("event_ticker") or "N/A"

    previous_bucket_index = None
    previous_temp_f = None
    previous_context = getattr(send_composed_weather_market_alert, "_prev_context", {})
    context_key = f"{normalized_station}_{market_type}_{event_ticker}"
    if isinstance(previous_context, dict):
        prior = previous_context.get(context_key) or {}
        previous_bucket_index = prior.get("bucket_index")
        previous_temp_f = prior.get("temp_f")

    reason_lower = (transition_reason or "").lower()
    if reason_lower == "up":
        direction_up = True
    elif reason_lower == "down":
        direction_up = False
    elif (
        previous_bucket_index is not None
        and current_index is not None
        and previous_bucket_index != current_index
    ):
        direction_up = current_index > previous_bucket_index
    elif (
        previous_temp_f is not None
        and current_temp_f is not None
        and float(current_temp_f) != float(previous_temp_f)
    ):
        direction_up = float(current_temp_f) > float(previous_temp_f)
    else:
        direction_up = True

    direction_icon = "⬆️" if direction_up else "⬇️"

    def _label_for_market(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        if strike_type == "less" and cap is not None:
            return f"{int(float(cap))} or below"
        if strike_type == "greater" and floor is not None:
            return f"{int(float(floor))} or higher"
        if strike_type == "between" and floor is not None and cap is not None:
            return f"{int(float(floor))}–{int(float(cap))}"
        strike = market.get("strike")
        return str(int(float(strike))) if strike is not None else "N/A"

    row_labels = [_label_for_market(market) for market in sorted_markets]
    label_width = max((len(label) for label in row_labels), default=0)

    ladder_rows = []
    for idx, market in enumerate(sorted_markets):
        yes_price = _to_price(market.get("yes_bid"), market.get("yes_ask"))
        no_price = _to_price(market.get("no_bid"), market.get("no_ask"))
        label = row_labels[idx].ljust(label_width)
        prefix = "▶   " if idx == current_index else "    "
        suffix = "  ← CURRENT" if idx == current_index else ""
        ladder_rows.append(
            f"{prefix}{label}  YES {yes_price}¢  NO {no_price}¢{suffix}"
        )

    current_market = sorted_markets[current_index] if sorted_markets and current_index is not None else None
    current_label = _label_for_market(current_market) if current_market else "N/A"

    strike_type_for_title = (current_market or {}).get("strike_type")
    if strike_type_for_title == "less":
        title_emoji = "🧊"
    elif strike_type_for_title == "greater":
        title_emoji = "🔥"
    else:
        title_emoji = "🌡️"

    distance_info = "MAX REACHED"

    def _ordered_bounds(market):
        strike_type = market.get("strike_type")
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        low = float("-inf") if strike_type == "less" else (
            float(floor) if floor is not None else float("-inf")
        )
        high = float("inf") if strike_type == "greater" else (
            float(cap) if cap is not None else float("inf")
        )
        return (low, high)

    ordered_markets = sorted(
        sorted_markets,
        key=lambda m: (_ordered_bounds(m)[0], _ordered_bounds(m)[1]),
    )

    ordered_index = None
    if current_market is not None:
        current_ticker = current_market.get("ticker")
        for idx, market in enumerate(ordered_markets):
            if market.get("ticker") == current_ticker:
                ordered_index = idx
                break

    if current_market is not None and current_temp_f is not None and ordered_index is not None:
        if direction_up:
            if ordered_index < len(ordered_markets) - 1:
                next_market = ordered_markets[ordered_index + 1]
                boundary = next_market.get("floor_strike")
                if boundary is not None:
                    distance = round(float(boundary) - float(current_temp_f), 1)
                    distance_info = f"{distance:.1f}°F"
            else:
                distance_info = "MAX REACHED"
        else:
            if ordered_index > 0:
                next_market = ordered_markets[ordered_index - 1]
                boundary = next_market.get("cap_strike")
                if boundary is not None:
                    distance = round(float(current_temp_f) - float(boundary), 1)
                    distance_info = f"{distance:.1f}°F"
            else:
                distance_info = "MIN REACHED"

    local_time_display = "N/A"
    local_dt = None
    if _to_local:
        try:
            local_dt = _to_local(normalized_station, datetime.now(timezone.utc))
            local_time_display = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            local_time_display = "N/A"
            local_dt = None

    temp_display = "N/A" if current_temp_f is None else f"{float(current_temp_f):.1f}"
    prev_display = "N/A" if prev_temp_f is None else f"{float(prev_temp_f):.1f}"
    now_display = "N/A" if now_temp_f is None else f"{float(now_temp_f):.1f}"
    delta_display = "N/A" if delta_f is None else f"{float(delta_f):+.1f}"

    epoch_context = _load_current_epoch_context(
        normalized_station,
        market_type,
        obs_time_utc,
    )
    epoch_context["previous_relevant_bucket"] = previous_bucket_index
    epoch_context["current_relevant_bucket"] = current_index

    station_local_timestamp = None
    obs_dt = parse_iso_utc(obs_time_utc)
    if obs_dt is not None:
        try:
            station_local_timestamp = to_station_local(normalized_station, obs_dt).isoformat()
        except Exception:
            station_local_timestamp = None
    epoch_context["station_local_timestamp"] = station_local_timestamp

    attention_phrase = _derive_attention_phrase(epoch_context)

    hydration_snapshot = get_last_hydration_execution_snapshot().get(normalized_station, {})
    hydration_status = "READY" if hydration_snapshot.get("cache_written") else "BLOCKED"
    hydration_evaluated_at = parse_iso_utc(hydration_snapshot.get("evaluated_at_utc"))
    ladder_cache_age_seconds = None
    if hydration_evaluated_at is not None:
        reference_dt = obs_dt if obs_dt is not None else datetime.now(timezone.utc)
        ladder_cache_age_seconds = max(
            0,
            int((reference_dt - hydration_evaluated_at).total_seconds()),
        )
    markets_considered_count = int(hydration_snapshot.get("raw_market_count") or len(markets))
    eligible_markets_count = len(markets)
    rejected_markets_count = max(markets_considered_count - eligible_markets_count, 0)
    rejection_counts = hydration_snapshot.get("rejection_counts") or {}
    rejection_breakdown = {
        "directional_strike_rejected": max(int(hydration_snapshot.get("filtered_market_count") or 0) - eligible_markets_count, 0),
        "wrong_series": int(rejection_counts.get("city_token_mismatch") or 0) + int(rejection_counts.get("market_type_mismatch") or 0),
        "expired_market": int(rejection_counts.get("inactive_market") or 0),
        "settlement_mismatch": int(rejection_counts.get("date_mismatch") or 0),
        "unknown_reason": max(
            rejected_markets_count
            - (
                max(int(hydration_snapshot.get("filtered_market_count") or 0) - eligible_markets_count, 0)
                + int(rejection_counts.get("city_token_mismatch") or 0)
                + int(rejection_counts.get("market_type_mismatch") or 0)
                + int(rejection_counts.get("inactive_market") or 0)
                + int(rejection_counts.get("date_mismatch") or 0)
            ),
            0,
        ),
    }
    transition_type = (transition_reason or "").strip().lower() or None
    instant_bucket_before = previous_bucket_index
    instant_bucket_after = current_index
    settlement_bucket = epoch_context.get("settlement_bucket")
    running_max = epoch_context.get("running_max")
    timestamp_utc = obs_time_utc
    temperature_f = now_temp_f
    reason_token = (transition_reason or "crossed").strip().lower() or "crossed"
    decision = "FIRED"
    reason = transition_reason or "ladder_transition"
    alert_type = "ladder_transition"
    direction = "UP" if direction_up else "DOWN"
    event_ticker_value = event_ticker
    summary = (
        f"{normalized_station} {transition_type or reason_token} detected; "
        f"{eligible_markets_count} eligible markets; "
        f"alert fired ({reason})"
    )

    header = f"{title_emoji} {normalized_station} {market_type or 'WEATHER'} — Ladder Cross {direction_icon}"
    ladder_block = "\n".join(ladder_rows)
    content = (
        f"{header}\n"
        f"Structure: {attention_phrase}\n"
        f"Prev: {prev_display}°F\n"
        f"Now: {now_display}°F\n"
        f"Δ: {delta_display}°F\n"
        f"{temp_display}°F  →  Entered {current_label}\n"
        f"Local time: {local_time_display}\n\n"
        f"Epoch: S={epoch_context.get('settlement_bucket')}"
        f" P={epoch_context.get('prior_settlement_bucket')}"
        f" J={epoch_context.get('settlement_jump_magnitude')}"
        f" Status={epoch_context.get('epoch_status')}"
        f" Rev={epoch_context.get('reversion_occurred')}"
        f" RevAt={epoch_context.get('first_reversion_timestamp_utc')}"
        f" Exc={epoch_context.get('max_excursion_above_settlement')}"
        f" Terminal={epoch_context.get('terminal_state_reached')}\n"
        f"Relevant bucket: prev={epoch_context.get('previous_relevant_bucket')} curr={epoch_context.get('current_relevant_bucket')}\n"
        f"Station local obs: {epoch_context.get('station_local_timestamp')}\n\n"
        f"Event: {event_ticker}\n"
        f"https://kalshi.com/markets/{event_ticker}\n\n"
        "LADDER\n"
        "────────────────────────────────\n"
        f"{ladder_block}\n"
        "────────────────────────────────\n\n"
        f"Next rung: {distance_info}"
    )

    market_open = bool((current_market or {}).get("open", True))
    market_expired = bool((current_market or {}).get("expired", False))
    market_range = {
        "strike_type": (current_market or {}).get("strike_type"),
        "floor_strike": (current_market or {}).get("floor_strike"),
        "cap_strike": (current_market or {}).get("cap_strike"),
        "label": current_label,
    }
    hydration_state = {
        "status": hydration_status,
        "series_discovered": bool(hydration_snapshot.get("series_ticker")),
    }
    payload = {
        "content": content,
        "embeds": [],
        "schema_version": ALERT_SCHEMA_VERSION,
        "alert_schema_version": ALERT_SCHEMA_VERSION,
        "timestamp_utc": timestamp_utc,
        "station": normalized_station,
        "classification": "MARKET_ELIGIBLE",
        "alert_summary": {
            "station": normalized_station,
            "transition_type": transition_type,
            "settlement_bucket": settlement_bucket,
            "market_symbol": event_ticker_value,
            "alert_classification": "MARKET_ELIGIBLE",
        },
        "transition_correlation": {
            "transition_event_id": None,
            "timestamp_utc": obs_time_utc,
            "instant_bucket_before": instant_bucket_before,
            "instant_bucket_after": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
            "running_max": running_max,
        },
        "market_evaluation": {
            "market_symbol": event_ticker_value,
            "market_range": market_range,
            "market_open": market_open,
            "market_expired": market_expired,
            "eligibility_result": "ELIGIBLE",
        },
        "suppression_context": {
            "suppression_reason": "",
            "settlement_mismatch": False,
            "expired_market": market_expired,
            "hydration_blocked": not bool(hydration_snapshot.get("cache_written")),
            "execution_domain_blocked": _current_kalshi_execution_domain() in _FORBIDDEN_KALSHI_DOMAINS,
        },
        "diagnostic_metadata": {
            "alert_schema_version": ALERT_SCHEMA_VERSION,
            "execution_domain": _current_kalshi_execution_domain(),
            "hydration_state": hydration_state,
            "ladder_cache_age_seconds": ladder_cache_age_seconds,
            "evaluation_timestamp": timestamp_utc,
        },
        "alert_classification": "MARKET_ELIGIBLE",
        "summary": {
            "headline": summary,
            "transition": transition_type,
            "temp_f": temperature_f,
            "instant_bucket": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
        },
        "transition_context": {
            "transition_type": transition_type,
            "instant_before": instant_bucket_before,
            "instant_after": instant_bucket_after,
            "settlement_bucket": settlement_bucket,
            "running_max": running_max,
            "obs_time": obs_time_utc,
        },
        "market_context": {
            "series_ticker": (sorted_markets[0] if sorted_markets else {}).get("series_ticker"),
            "event_ticker": event_ticker_value,
            "market_type": market_type,
            "strike": (current_market or {}).get("strike"),
            "proximity_regime": classify_proximity(abs(float((current_market or {}).get("strike") or 0) - float(temperature_f))) if temperature_f is not None and (current_market or {}).get("strike") is not None else None,
            "hydrated": bool(hydration_snapshot.get("cache_written")),
        },
        "eligibility_evaluation": {
            "markets_considered": markets_considered_count,
            "eligible_markets": eligible_markets_count,
            "rejected_markets": rejected_markets_count,
            "rejection_breakdown": rejection_breakdown,
        },
        "suppression": {
            "suppressed": False,
            "reason": "",
            "reason_category": "NO_TRANSITION",
        },
        "execution_context": {
            "execution_domain": _current_kalshi_execution_domain(),
            "hydration_state": {
                **hydration_state,
                "ladder_cache_age_seconds": ladder_cache_age_seconds,
            },
            "scheduler_poll_count": None,
            "station_local_timestamp": station_local_timestamp or (local_dt.isoformat() if local_dt else None),
        },
        "alert_context": {
            "attention_phrase": attention_phrase,
            **epoch_context,
        },
        "alert_decision": {
            "decision": decision,
            "reason": reason,
            "alert_type": alert_type,
            "bucket_index": current_index,
            "direction": direction,
            "event_ticker": event_ticker_value,
        },
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        return {
            "ok": False,
            "reason": "webhook_exception",
            "delivery_succeeded": False,
            "webhook_status_code": None,
            "webhook_exception": str(e),
            "webhook_response_text": None,
        }
    webhook_response_text = str(getattr(response, "text", "") or "")[:200] or None
    if not (200 <= response.status_code < 300):
        return {
            "ok": False,
            "reason": "webhook_failed",
            "delivery_succeeded": False,
            "webhook_status_code": int(response.status_code),
            "webhook_exception": None,
            "webhook_response_text": webhook_response_text,
        }

    key = f"{normalized_station}_{','.join(sorted(snapshot.get('market_types', [])))}"
    _last_composed_sent[key] = datetime.utcnow().isoformat() + "Z"

    if not isinstance(previous_context, dict):
        previous_context = {}
    previous_context[context_key] = {
        "bucket_index": current_index,
        "temp_f": current_temp_f,
    }
    send_composed_weather_market_alert._prev_context = previous_context

    return {
        "ok": True,
        "delivery_succeeded": True,
        "webhook_status_code": int(response.status_code),
        "webhook_exception": None,
        "webhook_response_text": webhook_response_text,
        "markets_included": len(markets),
        "observed": current_temp_f,
        "event_ticker": event_ticker,
        "bucket_index": current_index,
        "attention_phrase": attention_phrase,
        "alert_context": {
            "attention_phrase": attention_phrase,
            **epoch_context,
        },
    }


def check_public_market_changes(limit=5):
    global _last_market_check_summary

    markets_data = get_public_markets(limit=limit)
    markets = markets_data.get("markets", [])
    target_station = (os.getenv("KALSHI_TARGET_STATION") or "").strip().upper()
    target_market_types = _parse_target_market_types(
        os.getenv("KALSHI_TARGET_MARKET_TYPE")
    )

    if target_station:
        markets = _filter_structured_markets(
            markets,
            target_station,
            target_market_types,
        )

    raw_allowlist = (os.getenv("KALSHI_ALERT_TICKERS") or "").strip()
    alert_allowlist = None
    if raw_allowlist:
        alert_allowlist = {
            ticker.strip()
            for ticker in raw_allowlist.split(",")
            if ticker.strip()
        }

    if not _last_market_state:
        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue

            _last_market_state[ticker] = {
                "last_price": market.get("last_price"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
                "status": market.get("status"),
            }

        summary = {
            "markets_checked": len(markets),
            "changes_detected": 0,
            "alerts_sent": 0,
        }
        _last_market_check_summary = summary
        return summary

    changes_detected = 0
    alerts_sent = 0

    for market in markets:
        ticker = market.get("ticker")
        if not ticker:
            continue

        curr_state = {
            "last_price": market.get("last_price"),
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "no_bid": market.get("no_bid"),
            "no_ask": market.get("no_ask"),
            "status": market.get("status"),
        }

        prev_state = _last_market_state.get(ticker)
        should_alert = (
            prev_state is None
            or prev_state.get("last_price") != curr_state.get("last_price")
            or prev_state.get("status") != curr_state.get("status")
        )

        if should_alert:
            changes_detected += 1
            if (
                alert_allowlist is None or ticker in alert_allowlist
            ) and _send_kalshi_market_alert(ticker, prev_state, curr_state):
                alerts_sent += 1

        _last_market_state[ticker] = curr_state

    summary = {
        "markets_checked": len(markets),
        "changes_detected": changes_detected,
        "alerts_sent": alerts_sent,
    }
    _last_market_check_summary = summary
    return summary


def get_ladder_state_snapshot():
    """
    Read-only snapshot of in-memory ladder state.

    Returns:
        {
            "ladder_state": {"<normalized_key>": state_dict},
            "ladder_event_keys": {"<station_market_type>": "<normalized_key>"},
            "total_state_keys": int
        }
    """

    def _normalize_key(raw_key):
        if isinstance(raw_key, tuple) and len(raw_key) == 2:
            station, market_type = raw_key
            return f"{station}_{market_type}"
        return raw_key

    with _LADDER_LOCK:
        ladder_state_copy = {
            _normalize_key(state_key): dict(state_value)
            for state_key, state_value in _ladder_state.items()
        }
        ladder_event_keys_copy = {
            f"{station}_{market_type}": _normalize_key(state_key)
            for (station, market_type), state_key in _ladder_event_keys.items()
        }

    return {
        "ladder_state": ladder_state_copy,
        "ladder_event_keys": ladder_event_keys_copy,
        "total_state_keys": len(ladder_state_copy),
    }
