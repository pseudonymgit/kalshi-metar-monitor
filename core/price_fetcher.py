"""
Price fetcher Module

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


def _now_utc_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def _persist_rate_limit_entry(endpoint: str) -> None:
    """Record a rate limit entry (L1-T4)."""
    try:
        db_path = _alert_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Ensure schema exists
            _ensure_alert_schema()
            conn.execute(
                """
                INSERT INTO kalshi_rate_limit (endpoint, request_time)
                VALUES (?, ?)
                """,
                (endpoint, _now_utc_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("rate_limit_entry_persist_failed endpoint=%s error=%s", endpoint, e)
def _parse_retry_after(header_value: str) -> int:
    """Parse Retry-After header value to seconds (L1-T5).
    
    Supports:
    - Integer seconds: "120"
    - HTTP-date: "Tue, 11 Jun 2026 16:30:00 GMT"
    """
    if not header_value:
        return 60  # default fallback
    
    header_value = header_value.strip()
    
    try:
        # Try parsing as integer seconds
        return int(header_value)
    except (ValueError, TypeError):
        pass
    
    # Try parsing as HTTP-date
    try:
        # Parse HTTP-date format
        from email.utils import parsedate_to_datetime
        expires_dt = parsedate_to_datetime(header_value)
        now_dt = datetime.now(timezone.utc)
        delta = expires_dt - now_dt
        return max(1, int(delta.total_seconds()))
    except Exception:
        pass
    
    return 60  # default fallback
def _check_rate_limit(endpoint: str, max_requests: int = 10, window_seconds: int = 60) -> bool:
    """Check if rate limit is exceeded for an endpoint (L1-T4)."""
    try:
        db_path = _alert_db_path()
        _ensure_alert_schema()
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            # Count recent requests in the window
            now_iso = _now_utc_iso()
            rows = conn.execute(
                """
                SELECT COUNT(*) FROM kalshi_rate_limit
                WHERE endpoint = ?
                  AND request_time >= datetime(?, '-' || ? || ' seconds')
                """,
                (endpoint, now_iso, window_seconds),
            ).fetchone()
            count = rows[0] if rows else 0
            return count < max_requests
        finally:
            conn.close()
    except Exception as e:
        _LOGGER.warning("rate_limit_check_failed endpoint=%s error=%s", endpoint, e)
        return True  # allow by default on error
def _kalshi_public_get_with_rate_limit(path: str) -> dict:
    """Kalshi public GET with rate limiting and Retry-After handling (L1-T5)."""
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
    
    # Check rate limit before making request
    endpoint = normalized_path.split("?")[0]  # Use path without query for rate limit tracking
    if not _check_rate_limit(endpoint):
        _LOGGER.warning("rate_limit_exceeded endpoint=%s", endpoint)
        raise requests.HTTPError("Rate limit exceeded", response=type("Response", (object,), {
            "status_code": 429,
            "headers": {"Retry-After": "60"},
        })())
    
    # Record the request time
    _persist_rate_limit_entry(endpoint)
    
    response = _KALSHI_PUBLIC_SESSION.get(f"{base_url}{normalized_path}", timeout=10)
    response.raise_for_status()
    
    # Check for 429 with Retry-After
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        wait_seconds = _parse_retry_after(retry_after)
        _LOGGER.warning("rate_limit_429 endpoint=%s retry_after=%s", endpoint, wait_seconds)
        raise requests.HTTPError(
            f"Rate limit exceeded (Retry-After: {wait_seconds}s)",
            response=type("Response", (object,), {
                "status_code": 429,
                "headers": {"Retry-After": retry_after},
            })(),
        )
    
    return response.json()
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
    
    # Check for 429 with Retry-After handling
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        wait_seconds = _parse_retry_after(retry_after)
        _LOGGER.warning("rate_limit_429 endpoint=%s retry_after=%s", normalized_path, wait_seconds)
        raise requests.HTTPError(
            f"Rate limit exceeded (Retry-After: {wait_seconds}s)",
            response=type("Response", (object,), {
                "status_code": 429,
                "headers": {"Retry-After": retry_after},
            })(),
        )
    
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
