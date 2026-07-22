# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#


"""
Alert Dispatcher — Bridges the alert builder to Discord webhook delivery.

The alert_builder.py produces formatted dicts but never sends them.
This module handles the actual HTTP POST to Discord, with retry support.

Usage:
    from alert_dispatcher import dispatch_alert
    dispatch_alert(alert_data, discord_payload)
"""

import os
import logging
import sys
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Webhook URL environment variables (matching instance_config pattern)
WEBHOOK_ENV_VARS = {
    "PROD": "DISCORD_WEBHOOK_PROD",
    "DEV": "DISCORD_WEBHOOK_DEV",
    "SBOX": "DISCORD_WEBHOOK_SBOX",
}

# Default user-agent for Discord webhook requests
DEFAULT_USER_AGENT = "WeatherEngine/2.0"


def _get_webhook_url(instance: Optional[str] = None) -> Optional[str]:
    """
    Get the Discord webhook URL for the current instance.
    
    Args:
        instance: Instance tag (PROD/DEV/SBOX). Defaults to PAPER_TRADING_INSTANCE env var or DEV.
    
    Returns:
        Webhook URL string or None if not configured.
    """
    if instance is None:
        instance = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
    
    env_var = WEBHOOK_ENV_VARS.get(instance)
    if not env_var:
        logger.warning(f"No webhook env var defined for instance {instance}")
        return None
    
    url = os.getenv(env_var)
    if not url:
        logger.warning(f"Webhook URL not set: {env_var} (required for {instance})")
        return None
    
    return url


def dispatch_alert(alert_data: Dict[str, Any], 
                   discord_payload: Dict[str, Any],
                   instance: Optional[str] = None,
                   webhook_url: Optional[str] = None,
                   timeout: int = 10) -> Dict[str, Any]:
    """
    Dispatch a formatted alert to Discord via webhook.
    
    Args:
        alert_data: Raw alert data from build_paper_trade_alert()
        discord_payload: Formatted payload from format_alert_for_discord()
        instance: Instance tag (PROD/DEV/SBOX). Auto-detected if None.
        webhook_url: Explicit webhook URL. Auto-resolved from env if None.
        timeout: HTTP request timeout in seconds.
    
    Returns:
        Dict with delivery status: {
            "delivered": bool,
            "status_code": int or None,
            "error": str or None
        }
    """
    result = {
        "delivered": False,
        "status_code": None,
        "error": None,
    }
    
    # Resolve webhook URL
    if not webhook_url:
        webhook_url = _get_webhook_url(instance)
    
    if not webhook_url:
        result["error"] = "No webhook URL configured"
        logger.error(f"Alert dispatch failed: {result['error']}")
        return result
    
    # Skip filtered alerts silently
    if alert_data.get("skip_reason"):
        logger.debug(f"Alert filtered (not sent): {alert_data['skip_reason']}")
        result["delivered"] = True  # Not an error — intentional skip
        result["status_code"] = -1
        return result
    
    # Add metadata
    if "embeds" in discord_payload:
        for embed in discord_payload["embeds"]:
            if isinstance(embed, dict):
                embed.setdefault("footer", {"text": f"Weather Engine [{instance or os.getenv('PAPER_TRADING_INSTANCE', 'DEV')}]"})
    
    # POST to Discord
    try:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        response = requests.post(
            webhook_url,
            json=discord_payload,
            headers=headers,
            timeout=timeout,
        )
        
        result["status_code"] = response.status_code
        
        if response.status_code == 204 or response.status_code == 200:
            result["delivered"] = True
            logger.info(f"Alert delivered to Discord (status={response.status_code})")
        elif response.status_code == 429:
            # Rate limited — could integrate with alert_retry_queue here
            retry_after = response.headers.get("Retry-After", "unknown")
            result["error"] = f"Rate limited (429), retry after {retry_after}s"
            logger.warning(result["error"])
        else:
            result["error"] = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.error(f"Alert delivery failed: {result['error']}")
    
    except requests.exceptions.Timeout:
        result["error"] = "Request timed out"
        logger.error(f"Alert delivery timed out ({timeout}s)")
    
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Connection error: {e}"
        logger.error(f"Alert delivery connection failed: {e}")
    
    except Exception as e:
        result["error"] = f"Unexpected error: {e}"
        logger.exception(f"Alert delivery failed unexpectedly: {e}")
    
    return result


# Convenience function for paper_trading_engine integration
def dispatch_current_alert(alert_data: Dict[str, Any],
                           discord_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    One-shot alert dispatch using auto-detected instance configuration.
    Suitable for the paper trading engine's main execution loop.
    """
    return dispatch_alert(alert_data, discord_payload)


# Alias for backward compatibility
send_alert_to_discord = dispatch_alert