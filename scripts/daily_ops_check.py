#!/usr/bin/env python3
"""
B2 — Unified Monitoring Dashboard

Daily operations check that reports system health status:
  - Scheduler status via HTTP
  - Per-station freshness (ingestion status)
  - System health (ingestion, hydration, evaluation)
  - Outputs to stdout and optionally logs to a file

Usage:
    python3 scripts/daily_ops_check.py
    python3 scripts/daily_ops_check.py --app-url http://localhost:10000
    python3 scripts/daily_ops_check.py --log-file /var/log/weather-ops/daily_check.log
    python3 scripts/daily_ops_check.py --json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False


# ── Health endpoint paths ──────────────────────────────────────

INGESTION_HEALTH_PATH = "/observability/ingestion-health"
HYDRATION_HEALTH_PATH = "/observability/hydration-health"
EVALUATION_HEALTH_PATH = "/observability/evaluation-health"


def query_health_endpoint(app_url: str, path: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    """Query a health endpoint via HTTP GET. Returns None on failure."""
    if not HAVE_REQUESTS:
        return None
    url = f"{app_url.rstrip('/')}{path}"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def format_duration(seconds: Optional[float]) -> str:
    """Format seconds into human-readable duration."""
    if seconds is None:
        return "N/A"
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        return f"{hours}h {mins}m"
    else:
        days = int(seconds / 86400)
        hours = int((seconds % 86400) / 3600)
        return f"{days}d {hours}h"


def check_scheduler_status(app_url: str) -> Dict[str, Any]:
    """Check if the scheduler is running."""
    payload = query_health_endpoint(app_url, INGESTION_HEALTH_PATH)
    if payload is None:
        return {"status": "UNKNOWN", "error": "Cannot connect to health endpoint"}
    if "error" in payload:
        return {"status": "ERROR", "error": payload["error"]}

    scheduler_running = payload.get("scheduler_running", False)
    last_poll = payload.get("last_poll_utc", "N/A")
    poll_lag = payload.get("poll_lag_seconds")

    return {
        "status": "RUNNING" if scheduler_running else "STOPPED",
        "scheduler_running": scheduler_running,
        "last_poll_utc": last_poll,
        "poll_lag_seconds": poll_lag,
        "poll_lag_formatted": format_duration(poll_lag),
    }


def check_per_station_freshness(app_url: str) -> Dict[str, Any]:
    """Report per-station freshness from the ingestion health endpoint."""
    payload = query_health_endpoint(app_url, INGESTION_HEALTH_PATH)
    if payload is None:
        return {"status": "UNKNOWN", "error": "Cannot connect to health endpoint", "stations": []}
    if "error" in payload:
        return {"status": "ERROR", "error": payload["error"], "stations": []}

    stations = payload.get("stations", [])
    if not stations:
        return {
            "status": "NO_DATA",
            "stations_checked": 0,
            "stations": [],
            "note": "No station data in payload",
        }

    # Classify stations
    healthy = []
    stale = []
    for s in stations:
        station_info = {
            "station": s["station"],
            "status": s.get("status", "unknown"),
            "freshness_lag_seconds": s.get("freshness_lag_seconds"),
            "freshness_formatted": format_duration(s.get("freshness_lag_seconds")),
            "status_reason": s.get("status_reason", ""),
            "latest_accepted_observation_utc": s.get("latest_accepted_observation_utc"),
        }
        if s.get("status") == "healthy":
            healthy.append(station_info)
        else:
            stale.append(station_info)

    # Compute aggregate stats
    lags = [s.get("freshness_lag_seconds") for s in stations if s.get("freshness_lag_seconds") is not None]
    avg_lag = sum(lags) / len(lags) if lags else None
    max_lag = max(lags) if lags else None
    min_lag = min(lags) if lags else None

    return {
        "status": "OK",
        "stations_checked": len(stations),
        "healthy_count": len(healthy),
        "stale_count": len(stale),
        "avg_freshness_lag_seconds": avg_lag,
        "avg_freshness_formatted": format_duration(avg_lag),
        "max_freshness_lag_seconds": max_lag,
        "max_freshness_formatted": format_duration(max_lag),
        "min_freshness_lag_seconds": min_lag,
        "min_freshness_formatted": format_duration(min_lag),
        "healthy_stations": healthy,
        "stale_stations": stale,
    }


def check_system_health(app_url: str) -> Dict[str, Any]:
    """Check overall system health: ingestion, hydration, evaluation."""
    health = {
        "ingestion": {"status": "UNKNOWN"},
        "hydration": {"status": "UNKNOWN"},
        "evaluation": {"status": "UNKNOWN"},
    }

    # Ingestion health from ingestion endpoint
    ingestion = query_health_endpoint(app_url, INGESTION_HEALTH_PATH)
    if ingestion is not None and "error" not in ingestion:
        stations = ingestion.get("stations", [])
        healthy = sum(1 for s in stations if s.get("status") == "healthy")
        total = len(stations)
        scheduler = ingestion.get("scheduler_running", False)
        health["ingestion"] = {
            "status": "DEGRADED" if not scheduler else ("OK" if healthy == total else "DEGRADED"),
            "healthy_stations": healthy,
            "total_stations": total,
            "scheduler_running": scheduler,
            "stale_stations": total - healthy,
        }
    else:
        health["ingestion"] = {"status": "ERROR", "error": str(ingestion.get("error", "No response")) if ingestion else "No response"}

    # Hydration health
    hydration = query_health_endpoint(app_url, HYDRATION_HEALTH_PATH)
    if hydration is not None and "error" not in hydration:
        h_status = hydration.get("status", "unknown")
        health["hydration"] = {
            "status": "OK" if h_status == "healthy" else ("DEGRADED" if h_status == "degraded" else h_status.upper()),
            "details": hydration,
        }
    else:
        health["hydration"] = {"status": "ERROR", "error": str(hydration.get("error", "No response")) if hydration else "No response"}

    # Evaluation health
    evaluation = query_health_endpoint(app_url, EVALUATION_HEALTH_PATH)
    if evaluation is not None and "error" not in evaluation:
        e_status = evaluation.get("status", "unknown")
        health["evaluation"] = {
            "status": "OK" if e_status == "healthy" else ("DEGRADED" if e_status == "degraded" else e_status.upper()),
            "details": evaluation,
        }
    else:
        health["evaluation"] = {"status": "ERROR", "error": str(evaluation.get("error", "No response")) if evaluation else "No response"}

    return health


def generate_report(app_url: str, verbose: bool = False) -> Dict[str, Any]:
    """Generate a complete ops check report."""
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "app_url": app_url,
        "scheduler": check_scheduler_status(app_url),
        "freshness": check_per_station_freshness(app_url),
        "system_health": check_system_health(app_url),
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    """Format ops check report as human-readable text."""
    lines = []
    lines.append("=" * 72)
    lines.append("  WEATHER ENGINE — DAILY OPERATIONS CHECK")
    lines.append("=" * 72)
    lines.append(f"  Timestamp:  {report.get('timestamp_utc', 'N/A')}")
    lines.append(f"  App URL:    {report.get('app_url', 'N/A')}")
    lines.append("")

    # ── Scheduler Status ──
    scheduler = report.get("scheduler", {})
    sched_status = scheduler.get("status", "UNKNOWN")
    sched_icon = "✓" if sched_status == "RUNNING" else "✗"
    lines.append(f"  [{sched_icon}] Scheduler: {sched_status}")
    if verbose:
        lines.append(f"      Last poll: {scheduler.get('last_poll_utc', 'N/A')}")
        lines.append(f"      Poll lag:  {scheduler.get('poll_lag_formatted', 'N/A')}")
    if scheduler.get("error"):
        lines.append(f"      Error: {scheduler['error']}")
    lines.append("")

    # ── Per-Station Freshness ──
    freshness = report.get("freshness", {})
    f_status = freshness.get("status", "UNKNOWN")
    lines.append(f"  Freshness: {f_status}")
    lines.append(f"    Stations checked: {freshness.get('stations_checked', 0)}")
    lines.append(f"    Healthy: {freshness.get('healthy_count', 0)}  |  Stale: {freshness.get('stale_count', 0)}")

    if freshness.get("avg_freshness_lag_seconds") is not None:
        lines.append(f"    Avg freshness lag: {freshness.get('avg_freshness_formatted', 'N/A')}")
        lines.append(f"    Min lag: {freshness.get('min_freshness_formatted', 'N/A')}")
        lines.append(f"    Max lag: {freshness.get('max_freshness_formatted', 'N/A')}")

    if freshness.get("error"):
        lines.append(f"    Error: {freshness['error']}")

    # Stale stations detail
    stale = freshness.get("stale_stations", [])
    if stale:
        lines.append("")
        lines.append(f"  Stale stations ({len(stale)}):")
        for s in stale:
            lines.append(f"    {s['station']}: lag={s['freshness_formatted']}  reason={s['status_reason']}")
            if verbose and s.get("latest_accepted_observation_utc"):
                lines.append(f"      Last obs: {s['latest_accepted_observation_utc']}")

    if verbose and freshness.get("healthy_stations"):
        lines.append("")
        lines.append(f"  Healthy stations ({len(freshness['healthy_stations'])}):")
        for s in freshness["healthy_stations"]:
            lines.append(f"    {s['station']}: lag={s['freshness_formatted']}")

    lines.append("")

    # ── System Health ──
    system = report.get("system_health", {})
    lines.append("  System Health:")
    for component in ["ingestion", "hydration", "evaluation"]:
        comp = system.get(component, {})
        c_status = comp.get("status", "UNKNOWN")
        c_icon = "✓" if c_status == "OK" else "✗" if c_status == "ERROR" else "~"
        lines.append(f"    [{c_icon}] {component.capitalize()}: {c_status}")
        if verbose and comp.get("details"):
            lines.append(f"      Details: {json.dumps(comp.get('details'), default=str)[:200]}")
        if comp.get("error"):
            lines.append(f"      Error: {comp['error']}")
        if component == "ingestion" and comp.get("healthy_stations") is not None:
            lines.append(f"      {comp.get('healthy_stations')}/{comp.get('total_stations')} stations healthy")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="B2 — Unified Monitoring Dashboard")
    parser.add_argument("--app-url", default="http://localhost:10000",
                        help="Flask app URL (default: http://localhost:10000)")
    parser.add_argument("--log-file", default=None,
                        help="Optional log file path to append report")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of formatted text")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose output with all station details")
    args = parser.parse_args()

    if not HAVE_REQUESTS:
        print("ERROR: requests library is required. Install with: pip install requests")
        sys.exit(1)

    report = generate_report(args.app_url, verbose=args.verbose)

    if args.json:
        output = json.dumps(report, indent=2, default=str)
    else:
        output = format_report(report, verbose=args.verbose)

    print(output)

    # Log to file if requested
    if args.log_file:
        log_dir = os.path.dirname(args.log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(args.log_file, "a") as f:
            f.write(output)
            f.write("\n")

    # Exit with error code if any component is unhealthy
    system = report.get("system_health", {})
    for comp in ["ingestion", "hydration", "evaluation"]:
        if system.get(comp, {}).get("status") == "ERROR":
            sys.exit(1)

    # Check if scheduler is stopped
    if report.get("scheduler", {}).get("status") == "STOPPED":
        print("WARNING: Scheduler is not running.", file=sys.stderr)


if __name__ == "__main__":
    main()