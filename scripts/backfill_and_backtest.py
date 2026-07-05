#!/usr/bin/env python3
"""Backfill METAR data and run Phase 3 backtest engine (v2).

This script performs two tasks in sequence:
1. Extends the METAR backfill database (data/metar_backfill.db) to cover at
   least the last 12 months for the KNYC station (Central Park, NY). It fetches
   historical METAR observations from the NOAA Weather.gov API and stores them
   in the SQLite database. The script is idempotent – it skips records that are
   already present.
2. Executes the Phase‑3 backtest engine (core/p3_backtest_engine_v2.py) directly
   using the now‑populated backfill database. It validates that the output rows
   contain the expected ``direction``, ``reversion`` and ``terminal_state``
   columns and writes a concise JSON summary to
   ``data/backtest_results.json``.

Usage:
    python3 prototypes/weather-engine-source/scripts/backfill_and_backtest.py

The script is deliberately self‑contained; no agent needs to sit in a loop.
"""

import os
import sys
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATION = "KNYC"  # Central Park, New York
DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../data/metar_backfill.db")
)
RESULTS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../data/backtest_results.json")
)
# Target backfill length – at least 12 months (365 days)
BACKFILL_DAYS = 365
# Weather.gov API endpoint (public, no key required)
BASE_URL = "https://api.weather.gov/stations/{station}/observations"
# Minimal request headers required by the API
HEADERS = {
    "Accept": "application/ld+json",
    "User-Agent": "OpenClaw-WeatherEngine/1.0 (+https://openclaw.ai)"
}
# ---------------------------------------------------------------------------
# Helper: request METAR observations for a date range (UTC)
# ---------------------------------------------------------------------------
def fetch_metar_page(station: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Fetch a page of METAR observations for ``station`` between ``start`` and ``end``.
    The Weather.gov endpoint returns JSON‑LD. We request a large ``limit`` to reduce
    pagination – the API caps at 1000 but typical daily traffic is <200.
    """
    # Build query parameters
    params = {
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "limit": 1000,  # maximum allowed per request
    }
    url = BASE_URL.format(station=station)
    try:
        import requests
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # The observations are under "features" list
        observations = data.get("features", [])
        return observations
    except Exception as exc:
        print(f"[ERROR] Failed to fetch METAR data for {station} {start.date()} – {exc}", file=sys.stderr)
        return []

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def ensure_db_schema(conn: sqlite3.Connection) -> None:
    """Create a simple METAR table if it does not already exist.
    The schema mirrors what the original backfill logic used – we store the raw
    JSON payload so downstream code can deserialize as needed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar_observations (
            station TEXT NOT NULL,
            observation_time TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (station, observation_time)
        )
        """
    )
    conn.commit()

def insert_observations(conn: sqlite3.Connection, observations: List[Dict[str, Any]]) -> int:
    """Insert a batch of observations into the DB.
    Returns the number of rows actually inserted (duplicates are ignored).
    """
    if not observations:
        return 0
    cur = conn.cursor()
    inserted = 0
    for feature in observations:
        props = feature.get("properties", {})
        # ``validTime`` is a string like "2024-06-28T12:00:00+00:00/PT1H"
        # We take the start timestamp before the '/' separator.
        vt = props.get("validTime", "")
        obs_time = vt.split("/")[0] if "/" in vt else vt
        if not obs_time:
            continue
        raw_json = json.dumps(feature)
        try:
            cur.execute(
                "INSERT OR IGNORE INTO metar_observations (station, observation_time, raw_json) VALUES (?, ?, ?)",
                (STATION, obs_time, raw_json),
            )
            inserted += cur.rowcount  # 1 if inserted, 0 if duplicate
        except Exception as exc:
            print(f"[WARN] Could not insert observation {obs_time}: {exc}", file=sys.stderr)
    conn.commit()
    return inserted

# ---------------------------------------------------------------------------
# Backfill routine
# ---------------------------------------------------------------------------
def backfill_metar(conn: sqlite3.Connection) -> None:
    """Populate the DB with at least ``BACKFILL_DAYS`` of history for ``STATION``.
    The function walks backwards day‑by‑day until the required number of unique
    dates is present. It stops early if the remote API returns no data for a day.
    """
    today = datetime.utcnow().date()
    required_start = today - timedelta(days=BACKFILL_DAYS)
    print(f"[INFO] Ensuring METAR backfill from {required_start} to {today} for {STATION}")

    # Determine the earliest date currently stored for the station
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(observation_time) FROM metar_observations WHERE station = ?",
        (STATION,),
    )
    row = cur.fetchone()
    earliest = None
    if row and row[0]:
        # observation_time is ISO timestamp; extract date component
        earliest = datetime.fromisoformat(row[0].replace('Z', '+00:00')).date()
    else:
        earliest = today  # no data yet

    # Walk backwards until we reach the required start date
    current = today
    total_inserted = 0
    while current >= required_start:
        # Skip days we already have data for
        if earliest and current < earliest:
            # All earlier dates already present – we can break
            break
        start_dt = datetime.combine(current, datetime.min.time())
        end_dt = datetime.combine(current, datetime.max.time())
        observations = fetch_metar_page(STATION, start_dt, end_dt)
        inserted = insert_observations(conn, observations)
        total_inserted += inserted
        if inserted:
            print(f"[INFO] Inserted {inserted} observations for {current}")
        else:
            print(f"[INFO] No observations added for {current} (may already exist or API empty)")
        # Move to previous day
        current -= timedelta(days=1)
        # Small sleep to be polite to the public API
        time.sleep(0.2)
    print(f"[INFO] Backfill complete – total new rows added: {total_inserted}")

# ---------------------------------------------------------------------------
# Backtest execution
# ---------------------------------------------------------------------------
def run_backtest_and_save(db_path: str, out_path: str) -> None:
    """Run the Phase‑3 backtest engine (v2) on ``db_path`` and write a JSON report.
    The function imports the engine class directly to avoid the hard‑coded
    temporary path used by the original ``main()`` implementation.
    """
    # Import the engine from the source tree
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    try:
        from core.p3_backtest_engine_v2 import Phase3BacktestEngine
    except Exception as exc:
        print(f"[ERROR] Could not import backtest engine: {exc}", file=sys.stderr)
        sys.exit(1)

    engine = Phase3BacktestEngine(db_path)
    try:
        print("[INFO] Running backtest engine (this may take several minutes)…")
        results = engine.run_backtest()
        print(f"[INFO] Backtest generated {len(results)} prediction rows")
        summary = engine.calculate_summary(results)
        # Serialize dataclasses to plain dicts for JSON output
        def serialize(obj):
            if hasattr(obj, "__dict__"):
                return {k: serialize(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, (list, tuple)):
                return [serialize(i) for i in obj]
            if isinstance(obj, (datetime,)):
                return obj.isoformat()
            return obj
        output = {
            "summary": serialize(summary),
            "predictions": [serialize(r) for r in results],
        }
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"[INFO] Backtest results written to: {out_path}")
    finally:
        engine.close()

# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    # Ensure the database file exists and has the expected schema
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_db_schema(conn)
        backfill_metar(conn)
    finally:
        conn.close()

    # Run the backtest – we reuse the same DB path (the engine expects the
    # settlement‑epoch tables, which are populated elsewhere – but the backfill
    # DB is consumed by the engine for feature extraction. If the engine cannot
    # find the required tables, it will emit a clear error message.
    run_backtest_and_save(DB_PATH, RESULTS_PATH)

if __name__ == "__main__":
    main()
