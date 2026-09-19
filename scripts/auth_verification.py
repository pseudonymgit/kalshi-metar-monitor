#!/usr/bin/env python3
"""
Kalshi/WeatherAPI auth verification cron.
Tests that API credentials are valid and alerts on failure.
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="[AUTH-CHECK] %(message)s")
LOGGER = logging.getLogger("auth_check")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _load_env() -> None:
    """Load .env into os.environ so getenv() calls see credentials.

    Prior to this fix, the cron ran the script in a fresh shell without the
    .env contents, so KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM / WEATHERAPI_KEY
    were unset and the script reported a false 'not set' failure every hour.
    Matches the collector convention (run_whalewatch_collector.py).
    """
    env_path = os.path.join(PROJECT_ROOT, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            # Do not overwrite already-set env vars; .env is the fallback source.
            os.environ.setdefault(key, value)


_load_env()

FAILURES: list[str] = []


def check_kalshi():
    """Verify Kalshi API authentication works."""
    try:
        import requests
        base = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
        key_id = os.getenv("KALSHI_KEY_ID")
        priv_key_pem = os.getenv("KALSHI_PRIVATE_KEY_PEM")
        if not key_id or not priv_key_pem:
            FAILURES.append("KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PEM not set")
            return
        # Authenticated endpoint test
        from core.kalshi_monitor import get_state
        state = get_state()
        if state and state.get("auth_configured"):
            LOGGER.info("Kalshi auth: OK (configured)")
        else:
            FAILURES.append("Kalshi auth reported as not configured")
    except Exception as e:
        FAILURES.append(f"Kalshi auth check failed: {e}")


def check_weatherapi():
    """Verify WeatherAPI key works."""
    try:
        import requests
        key = os.getenv("WEATHERAPI_KEY")
        if not key:
            FAILURES.append("WEATHERAPI_KEY not set")
            return
        # q must be a WeatherAPI-recognized location (city or lat,lon), NOT an
        # ICAO code. "KNYC" returned 400/1006 "No matching location found",
        # which was a false auth failure — the key itself is valid.
        resp = requests.get(
            f"https://api.weatherapi.com/v1/current.json?key={key}&q=New York",
            timeout=15
        )
        if resp.status_code == 200:
            LOGGER.info("WeatherAPI: OK (200)")
        elif resp.status_code == 403:
            FAILURES.append("WeatherAPI returned 403 — key may be invalid/expired")
        else:
            FAILURES.append(f"WeatherAPI returned {resp.status_code}")
    except Exception as e:
        FAILURES.append(f"WeatherAPI check failed: {e}")


if __name__ == "__main__":
    check_kalshi()
    check_weatherapi()
    if FAILURES:
        print("::warning file=auth-check.txt::" + " | ".join(FAILURES))
        for f in FAILURES:
            LOGGER.warning(f)
        sys.exit(1)
    else:
        LOGGER.info("All auth checks passed")
        sys.exit(0)