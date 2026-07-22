#!/usr/bin/env python3
"""
Test script for alert dispatcher — sends a test message via dispatch_current_alert.

Usage:
    PAPER_TRADING_INSTANCE=DEV python3 scripts/test_alert_dispatcher.py

Requires DISCORD_WEBHOOK_PROD / DISCORD_WEBHOOK_DEV / DISCORD_WEBHOOK_SBOX
to be set (via source scripts/load_webhooks.sh or .bashrc).
"""

import os
import sys
import json

# Ensure core/ is on sys.path
CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core")
if CORE_DIR not in sys.path:
    sys.path.insert(0, os.path.normpath(CORE_DIR))

from alert_dispatcher import dispatch_alert


def main():
    instance = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
    print(f"Testing alert dispatch for instance: {instance}")

    # Build a minimal test payload
    alert_data = {
        "station": "KATL",
        "market_type": "HIGH",
        "direction": "UP",
        "confidence": 0.72,
        "reason": "test_alert_dispatcher",
        "edge": 0.15,
        "balance": 10000.0,
        "position_size": 100.0,
        "lane": "regular",
        "schema_version": "2.1",
    }

    discord_payload = {
        "content": None,
        "embeds": [{
            "title": "🧪 Test Alert — Weather Engine",
            "description": "This is a test alert from the alert dispatcher.
If you see this, the webhook is working.",
            "color": 0x00ff00,
            "fields": [
                {"name": "Instance", "value": instance, "inline": True},
                {"name": "Station", "value": "KATL", "inline": True},
                {"name": "Direction", "value": "UP", "inline": True},
                {"name": "Confidence", "value": "0.72", "inline": True},
            ],
            "footer": {"text": "Weather Engine Test — Phase 16"},
            "timestamp": "2026-07-21T23:21:00Z"
        }]
    }

    result = dispatch_alert(alert_data, discord_payload, instance=instance)
    if result.get("success"):
        print(f"✓ Alert dispatched successfully: {result.get('status_code', 'N/A')}")
    else:
        print(f"✗ Alert dispatch failed: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
