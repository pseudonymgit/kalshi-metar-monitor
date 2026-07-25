#!/usr/bin/env python3
"""
P0c — Missing Alert Emissions Audit

Cross-references transition events (persisted in transition_events table)
against alert emissions (persisted in alerts table) for a given time window.

For each station, counts: transitions observed vs alerts emitted.
Outputs a station-by-station delta.

Usage:
    python3 scripts/audit_alert_emissions.py
    python3 scripts/audit_alert_emissions.py --hours 48
    python3 scripts/audit_alert_emissions.py --station KNYC
    python3 scripts/audit_alert_emissions.py --json
    python3 scripts/audit_alert_emissions.py --db-path core/alerts.db

References:
    - alert_path_truth tracking in _emit_alert() (data_processor.py:1461)
    - _annotate_transition_history_alert_path_truth() (metar_monitor.py:2887)
    - transition_events table (metar_monitor.py:2580)
    - alerts table (metar_monitor.py:2500)
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _default_db_path() -> str:
    """Find the default alert DB path."""
    # Try environment variable first
    env_path = os.environ.get("ALERT_DB_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    # Try common locations
    candidates = [
        os.path.join(PROJECT_ROOT, "core", "alerts.db"),
        os.path.join(PROJECT_ROOT, "data", "alerts.db"),
        os.path.join(os.getcwd(), "core", "alerts.db"),
        os.path.join(os.getcwd(), "data", "alerts.db"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]  # return first candidate even if doesn't exist


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Open SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def get_transition_stats(
    db_path: str,
    hours: int = 24,
    station_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get transition event statistics for the given time window.

    Returns:
        Dict with per-station transition counts and alert emission counts
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    if not os.path.exists(db_path):
        return {"ok": False, "error": f"Database not found: {db_path}"}

    conn = get_db_connection(db_path)

    try:
        # Check if tables exist
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        result = {
            "ok": True,
            "db_path": db_path,
            "audit_window_hours": hours,
            "cutoff_utc": cutoff,
            "check_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tables_available": tables,
            "per_station": {},
            "summary": {},
        }

        # --- Transition events ---
        if "transition_events" in tables:
            query = """
                SELECT station, COUNT(*) as transition_count
                FROM transition_events
                WHERE created_utc >= ?
            """
            params: List[Any] = [cutoff]
            if station_filter:
                query += " AND station = ?"
                params.append(station_filter.upper())
            query += " GROUP BY station ORDER BY station"

            transition_rows = conn.execute(query, tuple(params)).fetchall()
            transition_data = {r["station"]: r["transition_count"] for r in transition_rows}
        else:
            transition_data = {}

        # --- Alert emissions ---
        if "alerts" in tables:
            query = """
                SELECT station, COUNT(*) as alert_count
                FROM alerts
                WHERE created_utc >= ?
            """
            params = [cutoff]
            if station_filter:
                query += " AND station = ?"
                params.append(station_filter.upper())
            query += " GROUP BY station ORDER BY station"

            alert_rows = conn.execute(query, tuple(params)).fetchall()
            alert_data = {r["station"]: r["alert_count"] for r in alert_rows}
        else:
            alert_data = {}

        # --- Cross-reference ---
        all_stations = sorted(set(list(transition_data.keys()) + list(alert_data.keys())))

        per_station = {}
        total_transitions = 0
        total_alerts = 0
        total_missing_alerts = 0
        total_extra_alerts = 0
        stations_with_missing_alerts = 0
        stations_with_extra_alerts = 0
        stations_with_no_alerts = 0

        for station in all_stations:
            t_count = transition_data.get(station, 0)
            a_count = alert_data.get(station, 0)
            delta = a_count - t_count

            total_transitions += t_count
            total_alerts += a_count

            if t_count > 0 and a_count == 0:
                stations_with_no_alerts += 1

            if delta < 0:
                total_missing_alerts += abs(delta)
                stations_with_missing_alerts += 1
            elif delta > 0:
                total_extra_alerts += delta
                stations_with_extra_alerts += 1

            # Check alert_path_truth from transition_events metadata
            alert_path_truth_info = {}
            if "transition_events" in tables:
                truth_query = """
                    SELECT metadata_json FROM transition_events
                    WHERE station = ? AND created_utc >= ?
                    AND metadata_json LIKE '%alert_path_truth%'
                    ORDER BY id DESC
                    LIMIT 5
                """
                truth_rows = conn.execute(truth_query, (station, cutoff)).fetchall()
                if truth_rows:
                    truth_entries = []
                    for tr in truth_rows:
                        try:
                            meta = json.loads(tr["metadata_json"])
                            if "alert_path_truth" in meta:
                                truth_entries.append(meta["alert_path_truth"])
                        except Exception:
                            pass
                    alert_path_truth_info = {
                        "entries_with_truth": len(truth_rows),
                        "sample_truth": truth_entries[:3] if truth_entries else [],
                    }

            # Determine outcome classification
            if t_count == 0 and a_count == 0:
                outcome = "no_activity"
            elif t_count > 0 and a_count >= t_count:
                outcome = "all_emitted"
            elif t_count > 0 and a_count > 0 and a_count < t_count:
                outcome = "partial_emission"
            elif t_count > 0 and a_count == 0:
                outcome = "no_emission"
            else:
                outcome = "extra_alerts_without_transitions"

            per_station[station] = {
                "transitions": t_count,
                "alerts_emitted": a_count,
                "delta": delta,  # negative = missing alerts, positive = extra alerts
                "outcome": outcome,
                "alert_path_truth": alert_path_truth_info,
            }

        # # --- Alert path truth completeness ---
        # Check what fraction of transition events have alert_path_truth annotated
        truth_completeness = {}
        if "transition_events" in tables:
            total_truth_query = """
                SELECT COUNT(*) as total, 
                       SUM(CASE WHEN metadata_json LIKE '%alert_path_truth%' THEN 1 ELSE 0 END) as with_truth
                FROM transition_events
                WHERE created_utc >= ?
            """
            params = [cutoff]
            if station_filter:
                total_truth_query += " AND station = ?"
                params.append(station_filter.upper())
            truth_row = conn.execute(total_truth_query, tuple(params)).fetchone()
            if truth_row and truth_row["total"] > 0:
                truth_completeness = {
                    "total_transitions": truth_row["total"],
                    "with_alert_path_truth": truth_row["with_truth"],
                    "pct_complete": round(truth_row["with_truth"] / truth_row["total"] * 100, 1),
                }

        result["per_station"] = per_station
        result["summary"] = {
            "total_transitions": total_transitions,
            "total_alerts_emitted": total_alerts,
            "total_missing_alerts": total_missing_alerts,
            "total_extra_alerts": total_extra_alerts,
            "stations_with_activity": len(all_stations),
            "stations_with_missing_alerts": stations_with_missing_alerts,
            "stations_with_extra_alerts": stations_with_extra_alerts,
            "stations_with_no_alerts_despite_transitions": stations_with_no_alerts,
            "alert_path_truth_completeness": truth_completeness,
        }

        # Also get overall event counts by type
        if "transition_events" in tables:
            type_query = """
                SELECT transition_type, COUNT(*) as cnt
                FROM transition_events
                WHERE created_utc >= ?
            """
            params = [cutoff]
            if station_filter:
                type_query += " AND station = ?"
                params.append(station_filter.upper())
            type_query += " GROUP BY transition_type ORDER BY cnt DESC"
            type_rows = conn.execute(type_query, tuple(params)).fetchall()
            result["transition_type_breakdown"] = {r["transition_type"]: r["cnt"] for r in type_rows}

        if "alerts" in tables:
            alert_type_query = """
                SELECT alert_type, COUNT(*) as cnt
                FROM alerts
                WHERE created_utc >= ?
            """
            params = [cutoff]
            if station_filter:
                alert_type_query += " AND station = ?"
                params.append(station_filter.upper())
            alert_type_query += " GROUP BY alert_type ORDER BY cnt DESC"
            alert_type_rows = conn.execute(alert_type_query, tuple(params)).fetchall()
            result["alert_type_breakdown"] = {r["alert_type"]: r["cnt"] for r in alert_type_rows}

        return result

    finally:
        conn.close()


def format_report(data: Dict[str, Any], verbose: bool = False) -> str:
    """Format audit results as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append("P0c — Alert Emissions Audit Report")
    lines.append("=" * 70)
    lines.append(f"Database: {data.get('db_path', 'N/A')}")
    lines.append(f"Audit window: {data.get('audit_window_hours', 'N/A')} hours")
    lines.append(f"Check timestamp: {data.get('check_timestamp_utc', 'N/A')}")
    lines.append(f"Tables available: {data.get('tables_available', [])}")

    if not data.get("ok", False):
        lines.append(f"ERROR: {data.get('error', 'Unknown error')}")
        return "\n".join(lines)

    summary = data.get("summary", {})
    lines.append("")
    lines.append("--- Summary ---")
    lines.append(f"Total transitions: {summary.get('total_transitions', 0)}")
    lines.append(f"Total alerts emitted: {summary.get('total_alerts_emitted', 0)}")
    lines.append(f"Total missing alerts (delta): {summary.get('total_missing_alerts', 0)}")
    lines.append(f"Total extra alerts (delta): {summary.get('total_extra_alerts', 0)}")
    lines.append(f"Stations with activity: {summary.get('stations_with_activity', 0)}")
    lines.append(f"Stations with missing alerts: {summary.get('stations_with_missing_alerts', 0)}")
    lines.append(f"Stations with extra alerts: {summary.get('stations_with_extra_alerts', 0)}")
    lines.append(f"Stations with transitions but zero alerts: {summary.get('stations_with_no_alerts_despite_transitions', 0)}")

    # Alert path truth completeness
    truth_comp = summary.get("alert_path_truth_completeness", {})
    if truth_comp:
        lines.append(f"Alert path truth completeness: {truth_comp.get('pct_complete', 'N/A')}% "
                     f"({truth_comp.get('with_alert_path_truth', 0)}/{truth_comp.get('total_transitions', 0)})")

    # Transition type breakdown
    if data.get("transition_type_breakdown"):
        lines.append("")
        lines.append("Transition type breakdown:")
        for ttype, cnt in sorted(data["transition_type_breakdown"].items(), key=lambda x: -x[1]):
            lines.append(f"  {ttype}: {cnt}")

    # Alert type breakdown
    if data.get("alert_type_breakdown"):
        lines.append("")
        lines.append("Alert type breakdown:")
        for atype, cnt in sorted(data["alert_type_breakdown"].items(), key=lambda x: -x[1]):
            lines.append(f"  {atype}: {cnt}")

    # Per-station details
    per_station = data.get("per_station", {})
    if per_station:
        lines.append("")
        lines.append("--- Per-Station Breakdown ---")
        # Sort by delta (most missing first)
        sorted_stations = sorted(per_station.items(), key=lambda x: x[1].get("delta", 0))
        for station, info in sorted_stations:
            delta = info.get("delta", 0)
            outcome = info.get("outcome", "unknown")
            t_count = info.get("transitions", 0)
            a_count = info.get("alerts_emitted", 0)
            delta_str = f"+{delta}" if delta > 0 else str(delta)

            if delta != 0 or verbose:
                lines.append(f"  {station}: transitions={t_count} alerts={a_count} "
                             f"delta={delta_str} outcome={outcome}")

        # List stations with no emission despite transitions
        no_emission = [s for s, info in per_station.items() if info.get("outcome") == "no_emission"]
        if no_emission:
            lines.append("")
            lines.append(f"STATIONS WITH ZERO ALERTS DESPITE TRANSITIONS ({len(no_emission)}):")
            for s in no_emission:
                info = per_station[s]
                lines.append(f"  {s}: {info['transitions']} transitions, 0 alerts")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P0c — Alert Emissions Audit")
    parser.add_argument("--db-path", default=None, help="Path to alerts.db")
    parser.add_argument("--hours", type=int, default=24, help="Audit window in hours (default: 24)")
    parser.add_argument("--station", default=None, help="Filter to single station (e.g., KNYC)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    db_path = args.db_path or _default_db_path()
    result = get_transition_stats(
        db_path=db_path,
        hours=args.hours,
        station_filter=args.station,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result, verbose=args.verbose))

    # Return non-zero exit code if missing alerts found
    if result.get("ok", False) and result.get("summary", {}).get("total_missing_alerts", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()