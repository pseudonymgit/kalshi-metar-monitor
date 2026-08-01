"""
Build Climatology Table — Phase 3.10

Pre-computes 30-year NOAA normal-based climatology per station per day-of-year.

Stores in a SQLite table for fast lookup during trading pipeline.

Usage:
    python3 scripts/build_climatology_table.py

Creates station_climatology table in metar_backfill.db with:
    - station, day_of_year (1-366)
    - mean_high, mean_low, mean_temp
    - std_high, std_low (for anomaly computation)
    - sample_size

B-Mode R8 Cycle 4.x — computed from 5+ years of METAR data.
"""

import sqlite3
import logging
import sys
import os
from datetime import datetime, timezone
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU',
    'KLAS', 'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC',
    'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO',
]


def build():
    logger.info("Building station climatology table from %s", DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")

    # Drop and re-create
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_climatology (
            station TEXT NOT NULL,
            day_of_year INTEGER NOT NULL,
            mean_high REAL,
            mean_low REAL,
            mean_temp REAL,
            std_high REAL,
            std_low REAL,
            sample_size INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (station, day_of_year)
        )
    """)

    for station in STATIONS:
        logger.info("Processing %s...", station)

        # Load daily aggregated data
        rows = conn.execute("""
            SELECT CAST(strftime('%j', date_utc) AS INTEGER) as doy,
                   MAX(temp_f) as high, MIN(temp_f) as low, AVG(temp_f) as mean
            FROM metar_observations
            WHERE station = ? AND temp_f IS NOT NULL
            GROUP BY date_utc
            ORDER BY doy ASC
        """, (station,)).fetchall()

        # Group by day_of_year
        by_doy: dict = defaultdict(lambda: {'highs': [], 'lows': []})
        for doy, high, low, mean in rows:
            if high is not None and low is not None:
                by_doy[doy]['highs'].append(high)
                by_doy[doy]['lows'].append(low)

        # Compute climatology per DOY
        for doy in range(1, 367):
            data = by_doy[doy]
            highs = data['highs']
            lows = data['lows']

            if len(highs) < 5:
                continue  # Insufficient data

            mean_high = sum(highs) / len(highs)
            mean_low = sum(lows) / len(lows)
            mean_temp = (mean_high + mean_low) / 2.0

            # Population std
            if len(highs) > 1:
                std_high = (sum((h - mean_high) ** 2 for h in highs) / len(highs)) ** 0.5
                std_low = (sum((l - mean_low) ** 2 for l in lows) / len(lows)) ** 0.5
            else:
                std_high = 0.0
                std_low = 0.0

            conn.execute("""
                INSERT OR REPLACE INTO station_climatology
                (station, day_of_year, mean_high, mean_low, mean_temp,
                 std_high, std_low, sample_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (station, doy, round(mean_high, 2), round(mean_low, 2),
                  round(mean_temp, 2), round(std_high, 2), round(std_low, 2),
                  len(highs)))

        conn.commit()

    # Verify
    count = conn.execute("SELECT COUNT(*) FROM station_climatology").fetchone()[0]
    stations_count = conn.execute("SELECT COUNT(DISTINCT station) FROM station_climatology").fetchone()[0]

    conn.close()
    logger.info("Climatology table built: %d rows across %d stations", count, stations_count)

    # Sample
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sample = conn.execute("""
        SELECT * FROM station_climatology
        WHERE station = 'KMSP' AND day_of_year IN (1, 100, 200, 300)
        ORDER BY day_of_year
    """).fetchall()
    conn.close()

    logger.info("Sample (KMSP):")
    for row in sample:
        logger.info("  DOY %3d: mean_high=%.1f°F mean_low=%.1f°F std_high=%.1f (n=%d)",
                    row['day_of_year'], row['mean_high'], row['mean_low'],
                    row['std_high'], row['sample_size'])


if __name__ == "__main__":
    build()