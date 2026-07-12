#!/usr/bin/env python3
"""
Test alert sender for PROD / DEV / SBOX.

Usage:
    python scripts/test_alert.py --env dev
    python scripts/test_alert.py --env prod
    python scripts/test_alert.py --env sbox
"""
import argparse
import os
import requests
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description="Send test alert to a specific environment")
    parser.add_argument("--env", choices=["prod", "dev", "sbox"], required=True,
                        help="Which environment to test")
    args = parser.parse_args()

    env_map = {
        "prod": "DISCORD_WEBHOOK_PROD",
        "dev":  "DISCORD_WEBHOOK_DEV",
        "sbox": "DISCORD_WEBHOOK_SBOX",
    }

    var_name = env_map[args.env]
    webhook = os.getenv(var_name)

    if not webhook:
        print(f"ERROR: {var_name} is not set in the environment")
        print("Make sure you ran: source scripts/load_webhooks.sh")
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    content = f"[{args.env.upper()}] OpenClaw alert system test — webhook is live.\nTimestamp: {timestamp}"

    try:
        resp = requests.post(webhook, json={"content": content}, timeout=10)
        if resp.status_code == 204:
            print(f"✅ Test alert sent successfully to {args.env.upper()}")
        else:
            print(f"⚠️ Sent but got status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
