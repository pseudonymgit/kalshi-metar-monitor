#!/usr/bin/env python3
"""
Weather Engine DB Freshness Gate — standalone script.

Checks the MAX(date) of each core weather engine database against "today"
and flags any DB that is stale beyond a per-DB threshold. Designed to run
alongside snapshot_db.py in the weekly cron so stale databases can never
silently ride along as "healthy" again.

Usage:
    python3 scripts/check_db_freshness.py [--stale-days 14] [--json]

Exit code 0 = all fresh. Exit code 1 = at least one stale DB (printed).
B-Mode compliant. No AI/ML.
"""
import os
import sys
import json
import sqlite3
import argparse
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

# db file -> (table, date_column, max allowed staleness in days)
# Core data sources we care about for trading signals.
FRESHNESS_CHECKS = {
    "metar_backfill.db":    ("metar_observations", "date_utc", 3, False),
    "weatherapi_archive.db": ("hourly",            "date",     5, False),
    "nwp_forecasts.db":     ("nwp_forecasts",      "fetch_date", 3, False),
    "gefs_archive.db":      ("gefs_archive",       "target_date", 10, True),
    "tigge_archive.db":     ("tigge_archive",      "target_date", 10, True),
    "era5_archive.db":      ("era5_archive",       "target_date", 14, False),
    "iem_asos_1min.db":     ("observations",       "valid",    5, False),
    "kalshi_settlements.db":("kalshi_settlements", "target_date", 10, False),
    "forecast_disagreement_live.db": ("daily_forecast_snapshots", "collection_date", 5, False),
}

# Per-station freshness check for ensemble archives (flagged by True above)
PER_STATION_CHECKS = {
    "tigge_archive.db": ("tigge_archive", "target_date", "station", 10),
    "gefs_archive.db":  ("gefs_archive",  "target_date", "station", 10),
}


def max_date(db_path: Path, table: str, col: str):
    """Return the max value of a date-ish column, or None."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cur = conn.cursor()
        row = cur.execute(
            f'SELECT MAX("{col}") FROM "{table}" WHERE "{col}" IS NOT NULL AND "{col}" != ""'
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        return f"ERROR: {e}"


def parse_staleness(value: str, today: date):
    """Compute days stale from a raw DB value (may be date, datetime, timestamp)."""
    if value is None:
        return None  # empty table
    if isinstance(value, str) and value.startswith("ERROR:"):
        return value
    s = str(value)[:10]  # take YYYY-MM-DD
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        return (today - d).days
    except ValueError:
        return f"UNPARSEABLE: {value}"


def main():
    parser = argparse.ArgumentParser(description="Weather engine DB freshness gate")
    parser.add_argument("--stale-days", type=int, default=14,
                        help="Global max staleness fallback (default 14)")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    today = date.today()
    results = {}
    per_station_results = []

    for db_name, (table, col, threshold, _) in FRESHNESS_CHECKS.items():
        db_path = DATA_DIR / db_name
        if not db_path.exists():
            results[db_name] = {"status": "MISSING", "max_date": None, "stale_days": None}
            continue
        raw = max_date(db_path, table, col)
        if raw is None:
            results[db_name] = {"status": "EMPTY", "max_date": None, "stale_days": None}
            continue
        if isinstance(raw, str) and raw.startswith("ERROR:"):
            results[db_name] = {"status": "ERROR", "error": raw}
            continue
        stale = parse_staleness(raw, today)
        if isinstance(stale, str):  # unparseable
            results[db_name] = {"status": "UNPARSEABLE", "max_date": raw, "stale_days": stale}
            continue
        status = "OK" if stale <= threshold else "STALE"
        results[db_name] = {"status": status, "max_date": raw, "stale_days": stale, "threshold": threshold}

    # Per-station checks: max date grouped by station; flag any station stale
    # beyond the threshold even if the table-level max looks fresh.
    for db_name, (table, col, station_col, threshold) in PER_STATION_CHECKS.items():
        db_path = DATA_DIR / db_name
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            rows = conn.execute(
                f'SELECT "{station_col}", MAX("{col}") FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL AND "{col}" != "" GROUP BY "{station_col}"'
            ).fetchall()
            conn.close()
        except Exception as e:
            per_station_results.append({"db": db_name, "error": str(e)})
            continue
        for station, raw in rows:
            stale = parse_staleness(raw, today)
            if isinstance(stale, str):
                continue
            if stale > threshold:
                per_station_results.append({
                    "db": db_name, "station": station, "max_date": raw,
                    "stale_days": stale, "threshold": threshold,
                })

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(f"{'Database':<38} {'Status':<8} {'Max Date':<12} {'Stale(d)':<9} {'Thresh'}")
        print("-" * 80)
        for db_name, r in sorted(results.items()):
            st = r.get("status", "?")
            md = r.get("max_date", "-") or "-"
            sd = r.get("stale_days", "-")
            th = r.get("threshold", "-")
            print(f"{db_name:<38} {st:<8} {str(md):<12} {str(sd):<9} {th}")

    if per_station_results:
        print("\nPer-station stale members:")
        for r in per_station_results:
            if "station" in r:
                print(f"  {r['db']} / {r['station']}: max {r['max_date']} ({r['stale_days']}d stale, thresh {r['threshold']})")
            else:
                print(f"  {r['db']}: ERROR {r['error']}")

    stale_dbs = [n for n, r in results.items() if r.get("status") in ("STALE", "MISSING", "EMPTY", "ERROR")]
    if per_station_results:
        # Per-station staleness also fails the gate
        stale_dbs.append("per-station members")
    if stale_dbs:
        print(f"\n⚠️  STALE DATABASES: {', '.join(stale_dbs)}")
        sys.exit(1)
    print("\nAll databases fresh.")
    sys.exit(0)


if __name__ == "__main__":
    main()