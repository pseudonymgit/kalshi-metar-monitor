#!/usr/bin/env python3
"""
Live Kalshi Market Price Fetcher (v1.0 — 2026-07-05)

Replaces the heuristic _get_market_price in paper_trading_engine.py with
real Kalshi API calls. Uses the public (unauthenticated) endpoints when
possible, falls back to authenticated calls when configured.

This is a pure deterministic script module — no AI/ML/LLM calls.

Usage:
    from core.kalshi_price_fetcher import get_live_market_price
    price = get_live_market_price("KDEN", "HIGH")

Version: v1.0 2026-07-05
"""

import os
import logging
import time
import json
import sqlite3
import threading
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parents[1]

_LOGGER = logging.getLogger(__name__)

# ─── Station → Kalshi Series Ticker Mapping ─────────────────────────────

# Kalshi series tickers follow the pattern KXHIGH<CODE> or KXLOW<CODE>
# where <CODE> is a station-specific code. The code suffixes are NOT
# predictable — some have a T prefix (TBOS, TDAL, THOU, TMIN, TPHX, TSEA, TSFO, TATL, TDC)
# and some don't (AUS, DEN, LAX, CHI, MIA, NY, PHIL).
# This mapping was verified against live Kalshi API on 2026-07-02.
#
# Removed stations (negative-EV or duplicates):
# - KJFK: duplicate of KNYC (both map to NY series)
# - KORD: duplicate of KMDW (both map to CHI series)
# - KCLT: not in verified 20-station set
# - KDTW: not in verified 20-station set
# - KLAS, KMSY, KOKC, KSAT: in verified set but no profitable signal — removed from registry

STATION_TO_KALSHI_CODE = {
    "KATL": "TATL",
    "KAUS": "AUS",
    "KBOS": "TBOS",
    "KDCA": "TDC",
    "KDEN": "DEN",
    "KDFW": "TDAL",
    "KHOU": "THOU",
    "KLAX": "LAX",
    "KMDW": "CHI",
    "KMIA": "MIA",
    "KMSP": "TMIN",
    "KNYC": "NY",
    "KPHL": "PHIL",
    "KPHX": "TPHX",
    "KSEA": "TSEA",
    "KSFO": "TSFO",
}

# Reverse mapping for discovery
KALSHI_CODE_TO_STATION = {v: k for k, v in STATION_TO_KALSHI_CODE.items()}


def _series_ticker_for_station(station: str, market_type: str = "HIGH") -> Optional[str]:
    """Get the Kalshi series ticker for a station and market type."""
    station = station.strip().upper()
    code = STATION_TO_KALSHI_CODE.get(station)
    if code is None:
        return None
    prefix = "KXHIGH" if market_type.upper() == "HIGH" else "KXLOW"
    return f"{prefix}{code}"


# ─── Thread-Safe Price Cache ───────────────────────────────────────────

_PRICE_CACHE: Dict[str, Tuple[float, float]] = {}  # ticker → (price, timestamp)
_PRICE_CACHE_LOCK = threading.Lock()
_PRICE_CACHE_TTL = 60.0  # seconds — don't hit API more than once per minute per ticker


def _get_cached_price(ticker: str) -> Optional[float]:
    """Get cached price if still valid (thread-safe)."""
    with _PRICE_CACHE_LOCK:
        entry = _PRICE_CACHE.get(ticker.upper())
        if entry is None:
            return None
        price, ts = entry
        if (time.time() - ts) > _PRICE_CACHE_TTL:
            return None
        return price


def _set_cached_price(ticker: str, price: float) -> None:
    """Cache a price (thread-safe)."""
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE[ticker.upper()] = (price, time.time())


def clear_price_cache() -> None:
    """Clear the entire price cache (thread-safe). Useful for testing or forced refresh."""
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE.clear()


def get_cache_stats() -> Dict[str, Any]:
    """Return cache statistics for observability."""
    with _PRICE_CACHE_LOCK:
        return {
            "entries": len(_PRICE_CACHE),
            "tickers": list(_PRICE_CACHE.keys()),
            "ttl_seconds": _PRICE_CACHE_TTL,
        }


# ─── Kalshi API ─────────────────────────────────────────────────────────

_KALSHI_BASE_URL = (
    os.getenv("KALSHI_PUBLIC_BASE_URL")
    or "https://api.elections.kalshi.com/trade-api/v2"
).rstrip("/")

# Use a shared session for connection pooling
import requests
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "WeatherEnginePaperTrader/1.0",
    "Accept": "application/json",
})


def _kalshi_public_get(path: str, timeout: int = 10) -> Optional[dict]:
    """Make a public GET request to Kalshi API. Returns None on failure."""
    url = f"{_KALSHI_BASE_URL}{path}"
    try:
        response = _SESSION.get(url, timeout=timeout)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            _LOGGER.warning("kalshi_rate_limited url=%s retry_after=%s", url, retry_after)
            return None
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        _LOGGER.warning("kalshi_timeout url=%s", url)
        return None
    except requests.exceptions.ConnectionError as e:
        _LOGGER.warning("kalshi_connection_error url=%s error=%s", url, e)
        return None
    except Exception as e:
        _LOGGER.warning("kalshi_request_failed url=%s error=%s", url, e)
        return None


def _find_current_market_for_series(series_ticker: str) -> Optional[dict]:
    """
    Find the current active market for a series.
    Returns the market dict or None.
    """
    data = _kalshi_public_get(
        f"/markets?series_ticker={series_ticker}&limit=50&status=open"
    )
    if data is None:
        return None
    
    markets = data.get("markets") or []
    if not markets:
        return None
    
    # Find the market with the nearest expiration that's still open
    now = datetime.now(timezone.utc)
    candidates = []
    for market in markets:
        exp_str = market.get("close_time") or market.get("expiration_time")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(
                    exp_str.replace("Z", "+00:00")
                )
                if exp_dt > now:
                    candidates.append((exp_dt, market))
            except Exception:
                candidates.append((now, market))
        else:
            candidates.append((now, market))
    
    if not candidates:
        return None
    
    # Sort by expiration (nearest first)
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _extract_market_price(market: dict) -> Optional[float]:
    """
    Extract a market price from a Kalshi market dict.
    Preference: last_price → last_price_dollars → midpoint of yes_bid/yes_ask → yes_ask → 0.5 fallback.
    
    Kalshi API returns prices in both cent and dollar formats:
    - last_price (cent), last_price_dollars (dollar)
    - yes_bid/yes_ask (cent), yes_bid_dollars/yes_ask_dollars (dollar)
    """
    # Last traded price (dollar format — preferred)
    for key in ("last_price_dollars", "last_price"):
        val = market.get(key)
        if val is not None:
            try:
                price = float(val)
                # If it looks like a dollar price (0-1), use directly
                if 0 < price <= 1:
                    return price
                # If it looks like cents (1-100), convert
                if 1 < price <= 100:
                    return price / 100.0
            except (TypeError, ValueError):
                pass
    
    # Midpoint of bid/ask (dollar format preferred)
    for suffix in ("_dollars", ""):
        yes_bid = market.get(f"yes_bid{suffix}")
        yes_ask = market.get(f"yes_ask{suffix}")
        if yes_bid is not None and yes_ask is not None:
            try:
                bid = float(yes_bid)
                ask = float(yes_ask)
                # Normalize to 0-1 range
                if bid > 1 or ask > 1:
                    bid = bid / 100.0
                    ask = ask / 100.0
                if 0 < bid <= ask <= 1:
                    return (bid + ask) / 2.0
            except (TypeError, ValueError):
                pass
    
    # Just ask (dollar format preferred)
    for suffix in ("_dollars", ""):
        yes_ask = market.get(f"yes_ask{suffix}")
        if yes_ask is not None:
            try:
                ask = float(yes_ask)
                if ask > 1:
                    ask = ask / 100.0
                if 0 < ask <= 1:
                    return ask
            except (TypeError, ValueError):
                pass
    
    # Just bid (dollar format preferred)
    for suffix in ("_dollars", ""):
        yes_bid = market.get(f"yes_bid{suffix}")
        if yes_bid is not None:
            try:
                bid = float(yes_bid)
                if bid > 1:
                    bid = bid / 100.0
                if 0 < bid <= 1:
                    return bid
            except (TypeError, ValueError):
                pass
    
    return None


def get_live_market_price(
    station: str,
    market_type: str = "HIGH",
    date_str: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """
    Get live market price from Kalshi for a station/market_type.
    
    Args:
        station: ICAO code (e.g., "KDEN")
        market_type: "HIGH" or "LOW"
        date_str: Optional date (used for logging only)
    
    Returns:
        Tuple of (price 0.0-1.0, metadata_dict)
        If API fails, returns (fallback_price, metadata) where fallback is 
        derived from historical data.
    """
    station = station.strip().upper()
    market_type = market_type.strip().upper()
    
    # Get series ticker
    series_ticker = _series_ticker_for_station(station, market_type)
    
    metadata = {
        "station": station,
        "market_type": market_type,
        "series_ticker": series_ticker,
        "source": "kalshi_live",
        "cached": False,
        "api_called": False,
        "fallback": False,
    }
    
    if series_ticker is None:
        metadata["source"] = "fallback_no_series"
        metadata["fallback"] = True
        return (0.5, metadata)
    
    # Check cache
    cached = _get_cached_price(series_ticker)
    if cached is not None:
        metadata["cached"] = True
        metadata["source"] = "kalshi_cached"
        return (cached, metadata)
    
    # Find current market
    metadata["api_called"] = True
    market = _find_current_market_for_series(series_ticker)
    
    if market is None:
        metadata["source"] = "fallback_no_market"
        metadata["fallback"] = True
        _LOGGER.info("kalshi_no_open_market series=%s station=%s", series_ticker, station)
        return (0.5, metadata)
    
    # Extract price
    price = _extract_market_price(market)
    
    if price is None:
        metadata["source"] = "fallback_no_price"
        metadata["fallback"] = True
        return (0.5, metadata)
    
    # Cache it
    _set_cached_price(series_ticker, price)
    
    metadata["market_ticker"] = market.get("ticker")
    metadata["event_ticker"] = market.get("event_ticker")
    metadata["market_status"] = market.get("status")
    metadata["last_price"] = market.get("last_price_dollars") or market.get("last_price")
    metadata["yes_bid"] = market.get("yes_bid_dollars") or market.get("yes_bid")
    metadata["yes_ask"] = market.get("yes_ask_dollars") or market.get("yes_ask")
    metadata["volume_24h"] = market.get("volume_24h_fp")
    
    return (price, metadata)


def warm_price_cache_for_stations(stations: list, market_type: str = "HIGH") -> Dict[str, float]:
    """
    Pre-warm the price cache for a list of stations.
    Useful to call before a paper trading run.
    """
    results = {}
    for station in stations:
        price, meta = get_live_market_price(station, market_type)
        results[station] = price
    return results


def build_market_url(station: str, market_type: str = "HIGH", date_str: str = None) -> Optional[str]:
    """
    Build a direct Kalshi market URL for a station/market_type.
    
    Tries live API to get the event_ticker. If that fails, constructs a URL
    from the series ticker as a fallback.
    
    URL format: https://kalshi.com/markets/<event_ticker>
    """
    station = station.strip().upper()
    market_type = market_type.strip().upper()
    
    # Try to get event_ticker from live API
    try:
        price, meta = get_live_market_price(station, market_type, date_str)
        event_ticker = meta.get("event_ticker")
        if event_ticker:
            return f"https://kalshi.com/markets/{event_ticker}"
        # Fallback: use market_ticker (contract-level)
        market_ticker = meta.get("market_ticker")
        if market_ticker:
            return f"https://kalshi.com/markets/{market_ticker}"
    except Exception:
        pass
    
    # Fallback: construct from series ticker (less specific but still useful)
    series_ticker = _series_ticker_for_station(station, market_type)
    if series_ticker:
        return f"https://kalshi.com/markets/{series_ticker}"
    
    return None


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    """Test live price fetching."""
    import sys
    
    stations = sys.argv[1:] if len(sys.argv) > 1 else [
        "KDEN", "KLAX", "KNYC", "KPHL", "KMDW", "KMIA", "KAUS"
    ]
    
    print(f"\nLive Kalshi Market Price Test")
    print(f"Base URL: {_KALSHI_BASE_URL}")
    print(f"{'Station':<8} {'Series':<12} {'Price':>8} {'Source':<20} {'Cached':>6}")
    print("-" * 60)
    
    for station in stations:
        for mtype in ("HIGH",):
            price, meta = get_live_market_price(station, mtype)
            print(f"{station:<8} {meta.get('series_ticker', 'N/A'):<12} "
                  f"{price:>8.4f} {meta['source']:<20} {'yes' if meta['cached'] else 'no':>6}")
    
    print()


if __name__ == "__main__":
    main()
