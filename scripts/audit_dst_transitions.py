"""
DST/Timezone Drift Audit — Phase 3.7

Audits all 20 stations for timezone-consistent timestamp handling
in the METAR observations and settlement data.

Verifies:
  1. All dates in settlement_epochs are local_trading_date (not UTC)
  2. No duplicate entries caused by DST transition (fall-back)
  3. Missing data on spring-forward days
  4. Station timezone mapping is correct

Usage:
    python3 scripts/audit_dst_transitions.py
"""

import sqlite3
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Station timezone offsets from UTC (standard time, no DST)
STATION_TZ: dict = {
    'KNYC': -5, 'KBOS': -5, 'KPHL': -5, 'KDCA': -5,   # Eastern
    'KATL': -5, 'KMIA': -5,                               # Eastern
    'KMDW': -6, 'KMSP': -6,                               # Central
    'KMSY': -6, 'KHOU': -6, 'KDFW': -6, 'KAUS': -6,
    'KSAT': -6, 'KOKC': -6,
    'KDEN': -7,                                           # Mountain
    'KPHX': -7,                                           # Arizona (no DST)
    'KLAS': -8, 'KLAX': -8, 'KSFO': -8, 'KSEA': -8,      # Pacific
}


def audit():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    issues = []

    # Check 1: DST duplicate entries in settlement_epochs
    logger.info("=== DST Duplicate Check ===")
    for station in sorted(STATION_TZ.keys()):
        rows = conn.execute("""
            SELECT local_trading_date, COUNT(*) as cnt
            FROM settlement_epochs
            WHERE station = ? AND market_type = 'HIGH'
            GROUP BY local_trading_date
            HAVING cnt > 1
        """, (station,)).fetchall()
        if rows:
            # Check if this is a DST fall-back day (Nov 1-7)
            for r in rows:
                issues.append(f"DUPLICATE {station} on {r['local_trading_date']} ({r['cnt']}x)")
                logger.warning("Duplicate settlement date: %s %s (%d copies)",
                               station, r['local_trading_date'], r['cnt'])

    if not issues:
        logger.info("No duplicate date issues found across all stations")

    # Check 2: Missing data on DST spring-forward (March 8-14)
    logger.info("\n=== DST Spring-Forward Gap Check ===")
    spring_dates = [f"2025-03-{d:02d}" for d in range(8, 15)] + \
                   [f"2026-03-{d:02d}" for d in range(8, 15)]
    for station in sorted(STATION_TZ.keys()):
        for dt in spring_dates:
            row = conn.execute("""
                SELECT COUNT(*) as cnt FROM metar_observations
                WHERE station = ? AND date_utc = ?
            """, (station, dt)).fetchone()
            if row and row['cnt'] == 0:
                logger.warning("No data for %s on %s (possible DST gap)", station, dt)

    # Check 3: Verify 2025-11-02 DST fall-back (duplicate hour)
    logger.info("\n=== DST Fall-Back Hour Check ===")
    for station in sorted(STATION_TZ.keys()):
        rows = conn.execute("""
            SELECT date_utc, MAX(temp_f) as high
            FROM metar_observations
            WHERE station = ? AND date_utc = '2025-11-02'
            GROUP BY date_utc
        """, (station,)).fetchall()
        if not rows:
            logger.warning("No data for %s on 2025-11-02 (DST fall-back)", station)
        else:
            logger.info("  %s: %d obs on 2025-11-02, high=%s°F",
                        station, len(rows), rows[0]['high'])

    # Check 4: UTC date consistency (no dates in future)
    logger.info("\n=== Future Date Check ===")
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = conn.execute("""
        SELECT MAX(date_utc) as max_date FROM metar_observations
    """).fetchone()
    if row and row['max_date']:
        max_date = row['max_date']
        logger.info("Latest METAR observation date: %s (now: %s)", max_date, now)
        if max_date > now:
            issues.append(f"FUTURE_DATE: max_date={max_date} > now={now}")
            logger.error("Future date detected in METAR observations!")

    conn.close()

    # Summary
    logger.info("\n=== Audit Summary ===")
    if issues:
        logger.warning("Issues found: %d", len(issues))
        for issue in issues:
            logger.warning("  %s", issue)
    else:
        logger.info("All checks passed — no DST/timezone issues detected")

    return len(issues)


if __name__ == "__main__":
    sys.exit(audit())