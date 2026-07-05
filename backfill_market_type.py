#!/usr/bin/env python3
"""
Backfill market_type for settlement epochs.

This script infers market_type from the alerts table and stores it in settlement_epochs.
"""

import sqlite3
import os
import re
from datetime import datetime

# Database path
ALERT_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source-backup-2026-06-17/alerts-prod.db"


def infer_market_type_from_event_ticker(event_ticker: str) -> str | None:
    """Infer market_type from event_ticker pattern."""
    if not event_ticker:
        return None
    
    ticker_upper = str(event_ticker).strip().upper()
    
    if any(token in ticker_upper for token in ("KXHIGH", " HIGH", "HIGHEST", "DAILY HIGH")):
        return "HIGH"
    if any(token in ticker_upper for token in ("KXLOW", " LOW", "LOWEST", "DAILY LOW")):
        return "LOW"
    if any(token in ticker_upper for token in ("HOURLY", "HOURLY-")):
        return "HOURLY"
    
    return None


def backfill_market_type(db_path: str) -> dict:
    """Backfill market_type for settlement epochs.
    
    Strategy:
    1. For epochs with a corresponding alert (by station and date), use the alert's market_type
    2. For remaining epochs, infer from settlement bucket patterns
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all alerts with market_type and event_ticker
    cursor.execute("""
        SELECT station, event_ticker, market_type, created_utc 
        FROM alerts 
        WHERE event_ticker IS NOT NULL 
        AND event_ticker != ''
        AND market_type IS NOT NULL
    """)
    alerts = cursor.fetchall()
    
    # Build a map of (station, date) -> market_type from alerts
    alert_map = {}
    for station, event_ticker, market_type, created_utc in alerts:
        # Try to extract date from event_ticker
        date_match = re.search(r'([A-Z]{3}\d{2})$', event_ticker)
        if date_match:
            ticker_date = date_match.group(1)
        else:
            try:
                dt = datetime.fromisoformat(created_utc.replace('Z', '+00:00'))
                ticker_date = dt.strftime('%d%b%y').upper()
            except:
                ticker_date = None
        
        if ticker_date:
            key = (station, ticker_date)
            alert_map[key] = market_type
    
    print(f"Built alert map with {len(alert_map)} entries")
    
    # Get all settlement epochs
    cursor.execute("""
        SELECT id, station, local_trading_date, settlement_bucket, 
               prior_settlement_bucket, epoch_status, last_transition_event_id
        FROM settlement_epochs
        WHERE market_type IS NULL
    """)
    epochs = cursor.fetchall()
    
    print(f"Found {len(epochs)} epochs without market_type")
    
    stats = {
        'backfilled': 0,
        'from_alerts': 0,
        'inferred': 0,
        'failed': 0,
    }
    
    for epoch_id, station, local_trading_date, settlement_bucket, prior_bucket, epoch_status, last_transition_id in epochs:
        market_type = None
        
        # Try to match by (station, date)
        try:
            dt = datetime.strptime(local_trading_date, '%Y-%m-%d')
            ticker_date = dt.strftime('%d%b%y').upper()
            key = (station, ticker_date)
            if key in alert_map:
                market_type = alert_map[key]
                stats['from_alerts'] += 1
        except:
            pass
        
        # If not found in alerts, infer from settlement bucket pattern
        if market_type is None:
            # Get the transition type for this epoch
            transition_type = None
            if last_transition_id:
                cursor.execute(
                    "SELECT transition_type FROM transition_events WHERE id = ?",
                    (last_transition_id,)
                )
                row = cursor.fetchone()
                if row:
                    transition_type = row[0]
            
            # Infer from settlement bucket direction
            if prior_bucket is not None:
                if settlement_bucket > prior_bucket:
                    market_type = "HIGH"
                elif settlement_bucket < prior_bucket:
                    market_type = "LOW"
                else:
                    market_type = "HIGH"  # default
            else:
                market_type = "HIGH"  # default
            
            stats['inferred'] += 1
        
        if market_type:
            cursor.execute(
                "UPDATE settlement_epochs SET market_type = ? WHERE id = ?",
                (market_type, epoch_id)
            )
            stats['backfilled'] += 1
        else:
            stats['failed'] += 1
    
    conn.commit()
    conn.close()
    
    return stats


def verify_backfill(db_path: str) -> dict:
    """Verify the backfill results."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM settlement_epochs")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM settlement_epochs WHERE market_type IS NOT NULL")
    with_type = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM settlement_epochs WHERE market_type IS NULL")
    null_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT market_type, COUNT(*) as count FROM settlement_epochs WHERE market_type IS NOT NULL GROUP BY market_type ORDER BY count DESC")
    by_type = cursor.fetchall()
    
    conn.close()
    
    return {
        'total': total,
        'with_type': with_type,
        'null_count': null_count,
        'by_type': by_type,
    }


def main():
    db_path = ALERT_DB_PATH
    
    print(f"Backfilling market_type for {db_path}")
    print("=" * 60)
    
    # First reset all market_type to NULL to start fresh
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE settlement_epochs SET market_type = NULL")
    conn.commit()
    conn.close()
    print("Reset all market_type to NULL")
    
    # Run backfill
    stats = backfill_market_type(db_path)
    
    print("\nBackfill stats:")
    print(f"  Backfilled: {stats['backfilled']}")
    print(f"    From alerts: {stats['from_alerts']}")
    print(f"    Inferred: {stats['inferred']}")
    print(f"    Failed: {stats['failed']}")
    
    # Verify
    print("\nVerifying backfill...")
    result = verify_backfill(db_path)
    
    print("\nVerification results:")
    print(f"  Total epochs: {result['total']}")
    print(f"  With market_type: {result['with_type']}")
    print(f"  Still NULL: {result['null_count']}")
    print("  By type:")
    for mt, count in result['by_type']:
        print(f"    {mt}: {count}")
    
    # Show sample epochs with market_type
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, station, market_type, local_trading_date, settlement_bucket 
        FROM settlement_epochs 
        WHERE market_type IS NOT NULL 
        LIMIT 5
    """)
    samples = cursor.fetchall()
    print("\nSample epochs with market_type:")
    for s in samples:
        print(f"  id={s[0]}, station={s[1]}, type={s[2]}, date={s[3]}, bucket={s[4]}")
    conn.close()


if __name__ == "__main__":
    main()
