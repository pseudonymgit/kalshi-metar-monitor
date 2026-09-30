#!/usr/bin/env python3
"""
data_freshness_monitor.py — deterministic per-source freshness monitor for the
Weather Engine.

Purpose: catch a stalled collection at 1-2 missed cycles instead of after days
of silence. Standalone, NO AI/ML, NO network calls. Reads only local SQLite DBs.

Design:
  - A registry declares, per source, its expected cadence (hours) and how many
    extra cycles of slack to tolerate before alerting.
  - For each source we compute the age (in hours) of the newest data point and
    compare against (cadence_hours * (1 + slack_cycles)).
  - Sources that share one DB but have independent producers (e.g. the gefs_ens
    model inside nwp_forecasts.db) are declared as separate entries with a
    `model_filter`, so a single stalled model is caught even when the DB as a
    whole looks fresh.
  - Ensemble archives get an additional per-station check: a station that stops
    flowing is flagged even if the table-level max is fresh.

Exit codes:
  0 = all sources fresh
  1 = at least one source STALE / MISSING / EMPTY / ERROR
  2 = monitor misconfiguration (bad registry)

Usage:
    python3 scripts/data_freshness_monitor.py
    python3 scripts/data_freshness_monitor.py --json
    python3 scripts/data_freshness_monitor.py --now 2026-09-24T22:00:00+00:00
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
# Each entry:
#   key        : stable identifier
#   db         : filename under data/
#   table      : table to query
#   date_col   : column holding the date/datetime of the data point
#   cadence_h  : expected hours between successful collections
#   slack      : extra missed cycles tolerated before alert (0,1,2,...)
#   model      : optional model filter (column `model` == value)
#   per_station: optional (station_col) -> also flag individual stalled stations
#   note       : human description of the producing cron
#
# Alert threshold (hours) = cadence_h * (1 + slack).
SOURCES = [
    dict(key="metar", db="metar_backfill.db", table="metar_observations",
         date_col="date_utc", cadence_h=24, slack=1,
         note="METAR daily observations"),
    dict(key="weatherapi", db="weatherapi_archive.db", table="hourly",
         date_col="date", cadence_h=24, slack=1,
         note="WE-weatherapi-daily-refresh (06:00 UTC)"),
    dict(key="nwp_core", db="nwp_forecasts.db", table="nwp_forecasts",
         date_col="fetch_timestamp", cadence_h=6, slack=1,
         note="NWP Forecast Collection (0,6,12,18 UTC) — gfs/ecmwf/icon/gem"),
    dict(key="nwp_gefs_ens", db="nwp_forecasts.db", table="nwp_forecasts",
         date_col="fetch_timestamp", cadence_h=24, slack=1, model="gefs_ens",
         note="GEFS ensemble rows in nwp_forecasts.db (model=gefs_ens)"),
    dict(key="gefs_archive", db="gefs_archive.db", table="gefs_archive",
         date_col="target_date", cadence_h=24, slack=1, per_station="station",
         note="WE-GEFS-daily-backfill / populate_gefs_archive.py"),
    dict(key="tigge_archive", db="tigge_archive.db", table="tigge_archive",
         date_col="target_date", cadence_h=24, slack=1, per_station="station",
         note="ecmwf-tigge-daily-update (07:00 UTC)"),
    dict(key="iem_asos_1min", db="iem_asos_1min.db", table="observations",
         date_col="valid", cadence_h=1, slack=2,
         note="z5-asos-1min-cron (hourly :40) / WE-IEM-ASOS-daily-backfill"),
    dict(key="kalshi_settlements", db="kalshi_settlements.db",
         table="kalshi_settlements", date_col="target_date",
         cadence_h=24, slack=1,
         note="kalshi-settlement-upkeep (08:00 UTC)"),
    dict(key="forecast_disagreement", db="forecast_disagreement_live.db",
         table="daily_forecast_snapshots", date_col="collection_date",
         cadence_h=24, slack=1,
         note="Forecast Disagreement Daily Collection (07:00 ET)"),
    dict(key="era5_archive", db="era5_archive.db", table="era5_archive",
         date_col="target_date", cadence_h=168, slack=1, per_station="station",
         note="era5 archive backfill (weekly cadence)"),
    # R/T (2026-09-29): GHCN was NOT monitored at all — the exact source Dan
    # flagged as 5 days stale with no alert. Added here (the registry the hourly
    # WE-data-freshness-monitor cron actually runs). Daily producer; t+1 lag is
    # normal, so cadence 24h + slack 1 = 48h alert threshold.
    dict(key="ghcn_settlements", db="ghcn_settlements.db", table="ghcn_settlements",
         date_col="date", cadence_h=24, slack=1, per_station="station",
         note="WE-GHCN-daily-backfill (06:30 UTC)"),
    # Kalshi market snapshots (live order book; not the settlement table).
    dict(key="kalshi_market_snapshots", db="kalshi_market_snapshots.db",
         table="market_snapshots", date_col="snapshot_time", cadence_h=1, slack=2,
         note="kalshi live market snapshots"),
]


def _connect(db_path: Path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)


def _max_raw(db_path: Path, table: str, col: str, model: str | None):
    """Newest raw value of date_col (optionally filtered by model), or error."""
    where = f'"{col}" IS NOT NULL AND "{col}" != ""'
    params = []
    if model:
        where += ' AND "model" = ?'
        params.append(model)
    sql = f'SELECT MAX("{col}") FROM "{table}" WHERE {where}'
    try:
        conn = _connect(db_path)
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def _parse_ts(value):
    """Best-effort parse of a DB date/datetime string into aware datetime."""
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("ERROR:"):
        return value
    s = str(value).strip().replace("Z", "+00:00")
    # try full datetime first
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.strptime(s[: len(fmt) + 2], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # last resort: leading YYYY-MM-DD
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return f"UNPARSEABLE: {value}"


def check_source(src: dict, now: datetime, data_dir: Path = DATA_DIR) -> dict:
    db_path = data_dir / src["db"]
    threshold_h = src["cadence_h"] * (1 + src["slack"])
    out = {
        "key": src["key"],
        "db": src["db"],
        "cadence_h": src["cadence_h"],
        "slack": src["slack"],
        "threshold_h": threshold_h,
        "note": src.get("note", ""),
    }
    if src.get("model"):
        out["model"] = src["model"]
    if not db_path.exists():
        out.update(status="MISSING", last=None, age_h=None)
        return out

    raw = _max_raw(db_path, src["table"], src["date_col"], src.get("model"))
    if raw is None:
        out.update(status="EMPTY", last=None, age_h=None)
        return out
    if isinstance(raw, str) and raw.startswith("ERROR:"):
        out.update(status="ERROR", last=None, age_h=None, error=raw)
        return out

    dt = _parse_ts(raw)
    if isinstance(dt, str):  # unparseable
        out.update(status="UNPARSEABLE", last=raw, age_h=None, error=dt)
        return out

    age_h = (now - dt).total_seconds() / 3600.0
    out["last"] = raw
    out["age_h"] = round(age_h, 1)
    out["status"] = "STALE" if age_h > threshold_h else "OK"
    return out


def check_per_station(src: dict, now: datetime, data_dir: Path = DATA_DIR) -> list[dict]:
    """Flag individual stations in an ensemble archive that stopped flowing."""
    station_col = src.get("per_station")
    if not station_col:
        return []
    db_path = data_dir / src["db"]
    if not db_path.exists():
        return []
    threshold_h = src["cadence_h"] * (1 + src["slack"])
    rows = []
    try:
        conn = _connect(db_path)
        recs = conn.execute(
            f'SELECT "{station_col}", MAX("{src["date_col"]}") FROM "{src["table"]}" '
            f'WHERE "{src["date_col"]}" IS NOT NULL AND "{src["date_col"]}" != "" '
            f'GROUP BY "{station_col}"'
        ).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        return [{"db": src["db"], "error": str(e)}]

    for station, raw in recs:
        dt = _parse_ts(raw)
        if isinstance(dt, str):
            continue
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h > threshold_h:
            rows.append({
                "db": src["db"], "station": station, "last": raw,
                "age_h": round(age_h, 1), "threshold_h": threshold_h,
            })
    return rows


def main():
    ap = argparse.ArgumentParser(description="Weather Engine data freshness monitor")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    ap.add_argument("--now", help="override 'now' (ISO-8601 UTC) for testing")
    ap.add_argument("--data-dir", help="override data dir (for frozen-fixture tests)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve() if args.data_dir else DATA_DIR

    if args.now:
        now = _parse_ts(args.now)
        if isinstance(now, str):
            print(f"bad --now: {args.now}", file=sys.stderr)
            return 2
    else:
        now = datetime.now(timezone.utc)

    results = [check_source(s, now, data_dir) for s in SOURCES]
    per_station = []
    for s in SOURCES:
        per_station.extend(check_per_station(s, now, data_dir))

    stale = [r for r in results if r["status"] != "OK"]
    report = {
        "checked_at": now.isoformat(),
        "sources": results,
        "per_station_stale": per_station,
        "stale_count": len(stale),
        "healthy": not stale and not per_station,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"Data Freshness Monitor — {now.isoformat()}")
        print("=" * 92)
        print(f"{'SOURCE':<22} {'STATUS':<9} {'LAST':<21} {'AGE(h)':<8} {'THRESH':<7} NOTE")
        print("-" * 92)
        for r in results:
            print(f"{r['key']:<22} {r['status']:<9} {str(r.get('last') or '-'):<21} "
                  f"{str(r.get('age_h') if r.get('age_h') is not None else '-'):<8} "
                  f"{r['threshold_h']:<7} {r.get('note','')}")
        if per_station:
            print("\nPer-station stale members:")
            for r in per_station:
                if "station" in r:
                    print(f"  {r['db']} / {r['station']}: last {r['last']} "
                          f"({r['age_h']}h stale, thresh {r['threshold_h']}h)")
                else:
                    print(f"  {r['db']}: ERROR {r['error']}")
        if report["healthy"]:
            print("\nAll sources fresh.")
        else:
            names = ", ".join(r["key"] for r in stale) or "(none)"
            print(f"\n\u26a0\ufe0f  STALE SOURCES: {names}")
            if per_station:
                print(f"\u26a0\ufe0f  STALE STATIONS: {len(per_station)}")

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
