#!/usr/bin/env python3
"""
P0b — Ingestion Parity Verification

Queries ingestion health for all stations and flags any station whose
last successful fetch time is more than 2× the median across all stations.

Usage:
    python3 scripts/check_ingestion_parity.py
    python3 scripts/check_ingestion_parity.py --station KNYC
    python3 scripts/check_ingestion_parity.py --json
    python3 scripts/check_ingestion_parity.py --app-url http://localhost:18889

References:
    - Gray Room Round 7 Expert 4, Section 2.2 (freshness thresholds)
    - B2 (unified monitoring dashboard), B13 (pre-trade integrity check)
    - B20 (graceful degradation state machine)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Try to import the health monitor directly (works when running from project root)
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.health_monitor import (
        check_all_stations_freshness,
        get_freshness_log_lines,
        STATE_NORMAL, STATE_WARN, STATE_DEGRADED, STATE_HALTED,
    )
    from core.data_processor import get_state
    HAVE_LOCAL_IMPORT = True
except ImportError:
    HAVE_LOCAL_IMPORT = False

# Try to import requests for HTTP mode
try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False


def query_via_http(app_url: str = "http://localhost:10000") -> Dict[str, Any]:
    """Query the ingestion-health endpoint via HTTP."""
    url = f"{app_url.rstrip('/')}/observability/ingestion-health"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def compute_parity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute ingestion parity from the /observability/ingestion-health payload.

    Flags any station whose last accepted observation age is more than
    2× the median age across all stations.
    """
    stations = payload.get("stations", [])
    if not stations:
        return {
            "ok": False,
            "error": "No stations in ingestion health payload",
            "stations_checked": 0,
            "parity_anomalies": [],
        }

    # Collect freshness lag seconds for stations with data
    lags = []
    station_lags: Dict[str, Optional[int]] = {}
    for s in stations:
        lag = s.get("freshness_lag_seconds")
        station_lags[s["station"]] = lag
        if lag is not None:
            lags.append(lag)

    if not lags:
        return {
            "ok": True,
            "stations_checked": len(stations),
            "parity_anomalies": [],
            "note": "No stations with valid freshness lag data",
            "median_lag_seconds": None,
        }

    # Compute median
    sorted_lags = sorted(lags)
    n = len(sorted_lags)
    if n % 2 == 1:
        median_lag = sorted_lags[n // 2]
    else:
        median_lag = (sorted_lags[n // 2 - 1] + sorted_lags[n // 2]) / 2.0

    threshold = median_lag * 2.0

    # Flag anomalies
    anomalies = []
    for s in stations:
        lag = s.get("freshness_lag_seconds")
        status = s.get("status", "unknown")
        station = s["station"]
        if lag is not None and lag > threshold and status != "healthy":
            anomalies.append({
                "station": station,
                "freshness_lag_seconds": lag,
                "median_lag_seconds": median_lag,
                "threshold_seconds": threshold,
                "ratio": round(lag / max(median_lag, 1), 2),
                "status": status,
                "status_reason": s.get("status_reason", ""),
                "latest_accepted_observation_utc": s.get("latest_accepted_observation_utc"),
            })

    # Compute stats
    healthy = sum(1 for s in stations if s.get("status") == "healthy")
    stale = sum(1 for s in stations if s.get("status") == "stale")

    return {
        "ok": True,
        "check_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "stations_checked": len(stations),
        "healthy_stations": healthy,
        "stale_stations": stale,
        "median_lag_seconds": round(median_lag, 1),
        "parity_threshold_2x_median_seconds": round(threshold, 1),
        "parity_anomalies": anomalies,
        "anomaly_count": len(anomalies),
        "scheduler_running": payload.get("scheduler_running", False),
        "last_poll_utc": payload.get("last_poll_utc"),
        "poll_lag_seconds": payload.get("poll_lag_seconds"),
    }


def run_local_check() -> Dict[str, Any]:
    """Run parity check using local imports (direct state access)."""
    state = get_state()
    freshness = check_all_stations_freshness(state=state)

    # Reformat for parity computation
    stations = []
    for s in freshness.get("stations", []):
        stations.append({
            "station": s["station"],
            "freshness_lag_seconds": s.get("age_seconds"),
            "status": "healthy" if s["state"] == STATE_NORMAL else "stale",
            "status_reason": s.get("threshold_triggered", ""),
            "latest_accepted_observation_utc": s.get("last_seen_iso"),
        })

    payload = {
        "stations": stations,
        "scheduler_running": False,
        "last_poll_utc": None,
        "poll_lag_seconds": None,
    }

    return compute_parity(payload)


def run_http_check(app_url: str = "http://localhost:10000") -> Dict[str, Any]:
    """Run parity check via HTTP."""
    if not HAVE_REQUESTS:
        return {"ok": False, "error": "requests library not available"}
    payload = query_via_http(app_url)
    return compute_parity(payload)


def format_report(parity: Dict[str, Any], verbose: bool = False) -> str:
    """Format parity check results as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append("P0b — Ingestion Parity Verification Report")
    lines.append("=" * 70)
    lines.append(f"Check timestamp: {parity.get('check_timestamp_utc', 'N/A')}")
    lines.append(f"Stations checked: {parity.get('stations_checked', 0)}")
    lines.append(f"Healthy: {parity.get('healthy_stations', 0)}")
    lines.append(f"Stale: {parity.get('stale_stations', 0)}")
    lines.append(f"Median lag: {parity.get('median_lag_seconds', 'N/A')}s")
    lines.append(f"Parity threshold (2× median): {parity.get('parity_threshold_2x_median_seconds', 'N/A')}s")
    lines.append(f"Anomalies found: {parity.get('anomaly_count', 0)}")

    if parity.get("parity_anomalies"):
        lines.append("")
        lines.append("PARITY ANOMALIES (stations >2× median lag):")
        for a in parity["parity_anomalies"]:
            lines.append(f"  {a['station']}: lag={a['freshness_lag_seconds']}s "
                         f"(ratio={a['ratio']}× median) status={a['status']}")
            if verbose:
                lines.append(f"    Reason: {a['status_reason']}")
                lines.append(f"    Last obs: {a.get('latest_accepted_observation_utc', 'N/A')}")

    if not parity.get("ok", True):
        lines.append(f"ERROR: {parity.get('error', 'Unknown error')}")

    lines.append("")
    lines.append("Scheduler running: %s" % parity.get("scheduler_running", "N/A"))
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P0b — Ingestion Parity Verification")
    parser.add_argument("--app-url", default="http://localhost:18889",
                        help="Flask app URL (default: http://localhost:18889)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--local", action="store_true",
                        help="Use local import instead of HTTP (app must be on same machine)")
    args = parser.parse_args()

    if args.local and HAVE_LOCAL_IMPORT:
        parity = run_local_check()
    elif HAVE_REQUESTS:
        parity = run_http_check(args.app_url)
    else:
        parity = run_local_check()

    if args.json:
        print(json.dumps(parity, indent=2, default=str))
    else:
        print(format_report(parity, verbose=args.verbose))

    # Return non-zero exit code if anomalies found
    if parity.get("anomaly_count", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()