#!/usr/bin/env python3
"""
P0e — Scheduler Execution Per Station

Verifies that each active station has been polled within its expected interval.
Extends the global is_scheduler_running() check with per-station granularity.

Usage:
    python3 scripts/check_scheduler_per_station.py
    python3 scripts/check_scheduler_per_station.py --json
    python3 scripts/check_scheduler_per_station.py --verbose
    python3 scripts/check_scheduler_per_station.py --interval 180  # expected 3min METAR polling
    python3 scripts/check_scheduler_per_station.py --app-url http://localhost:10000  # query live system

NOTE: The local (no --app-url) check calls is_scheduler_running() which checks
an in-process thread variable. This returns False when run standalone (outside the
Flask process). Use --app-url to query the live B-mode process directly.

References:
    - is_scheduler_running() in metar_monitor.py:5033
    - get_station_polling_status() in core/health_monitor.py
    - Gray Room Round 7 Expert 4, Section 2.2 (polling interval spec)
    - B20 (graceful degradation state machine)
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Add project root to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Try to import requests for HTTP mode
HAVE_REQUESTS = False
try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    pass


def query_via_http(app_url: str = "http://localhost:10000") -> Dict[str, Any]:
    """Query scheduler status from the live B-mode process via HTTP."""
    url = f"{app_url.rstrip('/')}/observability/ingestion-health"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": True,
            "check_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "scheduler_running": data.get("scheduler_running", False),
            "poll_lag_seconds": data.get("poll_lag_seconds"),
            "stale_after_seconds": data.get("stale_after_seconds"),
            "stations": data.get("stations", []),
            "system_health": data.get("system_health", {}),
            "source": "http",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "source": "http",
        }


def run_local_check(expected_poll_interval: int = 180) -> Dict[str, Any]:
    """Run per-station scheduler check using local imports."""
    try:
        from core.health_monitor import (
            get_all_stations_polling_status,
            get_station_polling_status,
        )
        from core.data_processor import get_state
        from core.metar_monitor import is_scheduler_running
    except ImportError as e:
        return {
            "ok": False,
            "error": f"Import failed: {e}",
            "suggestion": "Run from project root directory",
        }

    state = get_state()
    scheduler_running = is_scheduler_running()

    # Check all stations
    polling_result = get_all_stations_polling_status(
        state=state,
        expected_poll_interval_seconds=expected_poll_interval,
    )

    # Also check ACTIVE_STATIONS from p3_scheduler if available
    active_stations = []
    try:
        from core.p3_scheduler import ACTIVE_STATIONS
        active_stations = list(ACTIVE_STATIONS)
    except (ImportError, AttributeError):
        pass

    # Get ingestion runtime stations
    ingestion_runtime = state.get("ingestion_runtime", {})
    runtime_stations = sorted(ingestion_runtime.keys())

    return {
        "ok": True,
        "check_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scheduler_running": scheduler_running,
        "expected_poll_interval_seconds": expected_poll_interval,
        "active_stations_from_p3": active_stations,
        "stations_with_ingestion_runtime": runtime_stations,
        "stations_with_ingestion_runtime_count": len(runtime_stations),
        "polling_status": polling_result,
    }


def format_report(data: Dict[str, Any], verbose: bool = False) -> str:
    """Format scheduler check results as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append("P0e — Scheduler Execution Per Station Report")
    lines.append("=" * 70)
    lines.append(f"Check timestamp: {data.get('check_timestamp_utc', 'N/A')}")

    if not data.get("ok", False):
        lines.append(f"ERROR: {data.get('error', 'Unknown error')}")
        lines.append(f"  Suggestion: {data.get('suggestion', '')}")
        return "\n".join(lines)

    lines.append(f"Scheduler running (global): {data.get('scheduler_running', 'N/A')}")
    lines.append(f"Expected poll interval: {data.get('expected_poll_interval_seconds', 'N/A')}s")

    lines.append(f"Active stations (P3): {data.get('active_stations_from_p3', [])}")
    lines.append(f"Stations with ingestion runtime: {data.get('stations_with_ingestion_runtime_count', 0)}")

    polling = data.get("polling_status", {})
    summary = polling.get("summary", {})
    lines.append("")
    lines.append("Per-Station Polling Summary:")
    lines.append(f"  Healthy: {summary.get('healthy', 0)}")
    lines.append(f"  Stale/Delayed: {summary.get('stale_or_delayed', 0)}")
    lines.append(f"  Critical: {summary.get('critical', 0)}")
    lines.append(f"  Never Polled: {summary.get('never_polled', 0)}")
    lines.append(f"  Total checked: {polling.get('stations_checked', 0)}")

    # Per-station details
    stations = polling.get("stations", [])
    if stations:
        lines.append("")
        lines.append("Per-Station Details:")
        # Sort by state severity
        state_order = {"critical": 0, "stale": 1, "delayed": 2, "never_polled": 3, "unknown": 4, "healthy": 5}
        sorted_stations = sorted(stations, key=lambda s: state_order.get(s.get("state", "unknown"), 99))

        for s in sorted_stations:
            state = s.get("state", "unknown")
            poll_lag = s.get("poll_lag_seconds", "N/A")
            within = s.get("within_interval", False)
            obs_lag = s.get("observation_lag_seconds", "N/A")

            if state != "healthy" or verbose:
                lines.append(f"  {s['station']}: state={state} "
                             f"poll_lag={poll_lag}s within_interval={within} "
                             f"obs_lag={obs_lag}s")

        # List critical stations
        critical = [s for s in stations if s.get("state") == "critical"]
        if critical:
            lines.append("")
            lines.append(f"CRITICAL STATIONS ({len(critical)}):")
            for s in critical:
                lines.append(f"  {s['station']}: poll_lag={s.get('poll_lag_seconds', 'N/A')}s "
                             f"last_poll={s.get('last_poll_attempt_utc', 'never')}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P0e — Scheduler Execution Per Station")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--interval", type=int, default=180,
                        help="Expected poll interval in seconds (default: 180 = 3min)")
    parser.add_argument("--app-url", type=str, default=None,
                        help="HTTP URL of the live B-mode process (e.g. http://localhost:10000). "
                             "When set, queries scheduler status via HTTP instead of local in-process check.")
    args = parser.parse_args()

    if args.app_url:
        if not HAVE_REQUESTS:
            print("ERROR: requests library not available. Install with: pip install requests")
            sys.exit(1)
        result = query_via_http(app_url=args.app_url)
    else:
        result = run_local_check(expected_poll_interval=args.interval)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result, verbose=args.verbose))

    # Return non-zero exit code if critical stations found
    if not args.app_url:
        polling = result.get("polling_status", {})
        if polling.get("summary", {}).get("critical", 0) > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()