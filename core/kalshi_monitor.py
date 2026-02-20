import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


_last_market_state = {}


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


def _kalshi_public_get(path):
    base_url = (
        os.getenv("KALSHI_PUBLIC_BASE_URL")
        or "https://api.elections.kalshi.com/trade-api/v2"
    ).rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    response = requests.get(f"{base_url}{normalized_path}", timeout=10)
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


def _format_change(prev, curr):
    return f"{prev} → {curr}"


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
                    "text": "Kalshi Public Monitor (Phase 2.1)",
                },
            }
        ],
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    return 200 <= response.status_code < 300


def check_public_market_changes(limit=5):
    markets_data = get_public_markets(limit=limit)
    markets = markets_data.get("markets", [])

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

        return {
            "markets_checked": len(markets),
            "changes_detected": 0,
            "alerts_sent": 0,
        }

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
            if _send_kalshi_market_alert(ticker, prev_state, curr_state):
                alerts_sent += 1

        _last_market_state[ticker] = curr_state

    return {
        "markets_checked": len(markets),
        "changes_detected": changes_detected,
        "alerts_sent": alerts_sent,
    }
