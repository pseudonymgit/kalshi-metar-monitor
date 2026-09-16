"""
P0 intraday core tests — kalshi_price_fetcher (Σ.1e).

Deterministic pure functions: series ticker construction and the thread-safe
price cache. No network calls are made (API functions are left untested here
to avoid external dependencies; they are exercised by integration tests).
"""

import pytest

from core import kalshi_price_fetcher as kpf


# ── Series ticker construction ───────────────────────────────

def test_series_ticker_high():
    ticker = kpf._series_ticker_for_station("KATL", market_type="HIGH")
    assert ticker is not None
    assert ticker.startswith("KXHIGH")


def test_series_ticker_low():
    ticker = kpf._series_ticker_for_station("KATL", market_type="LOW")
    assert ticker is not None
    assert ticker.startswith("KXLOW")


def test_series_ticker_normalizes_case_and_whitespace():
    a = kpf._series_ticker_for_station("katl", market_type="HIGH")
    b = kpf._series_ticker_for_station("  KATL  ", market_type="HIGH")
    assert a == b


def test_series_ticker_unknown_station_is_none():
    assert kpf._series_ticker_for_station("ZZZZ", market_type="HIGH") is None


# ── Price cache (thread-safe, TTL) ───────────────────────────

def test_cache_get_set_roundtrip():
    kpf.clear_price_cache()
    assert kpf._get_cached_price("KXHIGHKATL") is None
    kpf._set_cached_price("KXHIGHKATL", 0.42)
    assert kpf._get_cached_price("KXHIGHKATL") == 0.42


def test_cache_is_case_insensitive():
    kpf.clear_price_cache()
    kpf._set_cached_price("kxhighkatl", 0.42)
    assert kpf._get_cached_price("KXHIGHKATL") == 0.42


def test_cache_clear():
    kpf.clear_price_cache()
    kpf._set_cached_price("KXHIGHKATL", 0.42)
    kpf.clear_price_cache()
    assert kpf._get_cached_price("KXHIGHKATL") is None


def test_cache_stats_shape():
    kpf.clear_price_cache()
    kpf._set_cached_price("KXHIGHKATL", 0.42)
    stats = kpf.get_cache_stats()
    assert stats["entries"] == 1
    assert "KXHIGHKATL" in stats["tickers"]
    assert stats["ttl_seconds"] > 0


def test_cache_ttl_expiry(monkeypatch):
    import time
    real_time = time.time  # capture BEFORE patching to avoid recursion
    kpf.clear_price_cache()
    kpf._set_cached_price("KXHIGHKATL", 0.42)
    # Simulate time passing beyond TTL
    monkeypatch.setattr(time, "time", lambda: real_time() + kpf._PRICE_CACHE_TTL + 1.0)
    assert kpf._get_cached_price("KXHIGHKATL") is None
