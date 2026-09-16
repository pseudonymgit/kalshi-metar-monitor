#!/usr/bin/env python3
"""Daily deterministic Kalshi weather discovery + market snapshot refresh."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

from scripts.kalshi_series_discovery import discover
from scripts.z8_kalshi_market_cron import STATION_SERIES, effective_station_series, validate_snapshot_receipt


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE = os.path.join(PROJECT_ROOT, "data", "kalshi_series_discovery.json")
DEFAULT_RECEIPT = os.path.join(PROJECT_ROOT, "data", "kalshi_weather_refresh_receipt.json")


def run(output_cache: str = DEFAULT_CACHE, receipt_path: str = DEFAULT_RECEIPT) -> dict:
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    discovery = discover(output_cache)
    merged = effective_station_series(output_cache)
    missing = sorted(set(STATION_SERIES) - set(discovery["stations"]))
    command = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "z8_kalshi_market_cron.py"), "--once"]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"z8 failed ({completed.returncode}): {completed.stderr[-1000:]}")
    snapshot = validate_snapshot_receipt()
    receipt = {
        "started_at": started,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "kalshi_public_series_daily_temperature",
        "discovery": {
            "raw_station_count": discovery["raw_station_count"],
            "normalized_station_count": discovery["station_count"],
            "operational_baseline_count": len(STATION_SERIES),
            "merged_station_count": len(merged),
            "dynamic_stations": sorted(discovery["stations"]),
            "static_fallback_stations": missing,
            "aliases": discovery.get("aliases", {}),
        },
        "z8": snapshot,
        "ok": snapshot["ok"] and len(merged) >= len(STATION_SERIES),
    }
    os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
    tmp = receipt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, receipt_path)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    result = run(args.cache, args.receipt)
    raise SystemExit(0 if result["ok"] else 1)
