import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


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
