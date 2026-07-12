#!/usr/bin/env python3
"""Standalone alert test script (no Flask dependency)."""
import argparse
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("pip install requests (required for this script)")
    sys.exit(1)


def normalize_domain(d: str) -> str:
    d = (d or "production").strip().lower()
    if d in ("production", "prod"):
        return "prod"
    if d in ("sandbox", "sbox", "sbx"):
        return "sbox"
    if d in ("development", "dev"):
        return "dev"
    return d


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook", required=True, help="Discord webhook URL")
    parser.add_argument("--domain", default="dev", choices=["prod", "sbox", "dev"])
    parser.add_argument("--station", default="KNYC")
    args = parser.parse_args()

    domain = normalize_domain(args.domain)
    allowed = {"prod", "sbox", "dev"}
    if domain not in allowed:
        print(f"Domain {domain} not allowed. Allowed: {allowed}")
        sys.exit(1)

    payload = {
        "content": f"🧪 **{domain.upper()} Test Alert**",
        "embeds": [{
            "title": "Alert System Test",
            "description": f"Domain normalization test from {domain}",
            "color": 3066993,
            "fields": [
                {"name": "Station", "value": args.station, "inline": True},
                {"name": "Domain", "value": domain, "inline": True},
                {"name": "Timestamp (UTC)", "value": datetime.now(timezone.utc).isoformat(), "inline": True}
            ],
            "footer": {"text": "weather-engine-source • send_test_alert.py"}
        }]
    }

    try:
        resp = requests.post(args.webhook, json=payload, timeout=15)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text[:200] if resp.text else '(empty)'}")
        if resp.status_code == 204:
            print("✅ Alert delivered successfully")
        else:
            print("⚠️ Unexpected status code")
    except Exception as e:
        print(f"❌ Failed to send: {e}")


if __name__ == "__main__":
    main()
