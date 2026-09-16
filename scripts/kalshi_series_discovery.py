#!/usr/bin/env python3
"""Deterministic daily Kalshi weather-series discovery.

Writes a small JSON cache for the z8 collector.  The API response is the
source of truth; z8's static map remains a coverage fallback when discovery
is temporarily unavailable.
"""

import argparse
import datetime as dt
import json
import os
import tempfile

from core.kalshi_monitor import _discover_series_for_stations


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "data", "kalshi_series_discovery.json")
STATION_ALIASES = {"KDAL": "KDFW"}


def discover(output_path: str = DEFAULT_OUTPUT) -> dict:
    discovered = _discover_series_for_stations()
    if not discovered:
        raise RuntimeError("Kalshi discovery returned zero station mappings")
    normalized = {}
    aliases = {}
    for station, tickers in discovered.items():
        target = STATION_ALIASES.get(station.upper(), station.upper())
        if target != station.upper():
            aliases[station.upper()] = target
        normalized.setdefault(target, set()).update(tickers)
    from scripts.z8_kalshi_market_cron import STATION_SERIES
    baseline = set(STATION_SERIES)
    payload = {
        "discovered_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "kalshi_public_series_daily_temperature",
        "raw_station_count": len(discovered),
        "station_count": len(normalized),
        "aliases": aliases,
        "baseline_station_count": len(baseline),
        "baseline_missing_from_discovery": sorted(baseline - set(normalized)),
        "discovered_extra_stations": sorted(set(normalized) - baseline),
        "stations": {k: sorted(v) for k, v in sorted(normalized.items())},
    }
    parent = os.path.dirname(output_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kalshi_series_discovery.", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, output_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = discover(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
