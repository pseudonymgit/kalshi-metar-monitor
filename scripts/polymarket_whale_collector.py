#!/usr/bin/env python3
"""
Polymarket WhaleWatch — Daily Collection Cron Script

Runs as a daily cron to:
  1. Fetch Polymarket live trades + market data
  2. Extract consensus signals from whale activity
  3. Persist to data/polymarket_whale.db
  4. Identify weather-relevant signals mapped to Kalshi stations/buckets
  5. Compute conviction multipliers and store in signals feed
  6. Expire stale signals past their TTL
  7. Output a summary: N signals active, N with weather relevance, conviction range

Usage:
    python scripts/polymarket_whale_collector.py
    python scripts/polymarket_whale_collector.py --verbose
    python scripts/polymarket_whale_collector.py --db /path/to/custom.db

B-Mode compliant. No AI/ML.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.polymarket_whale_db import (
    init_db, get_db, PM_WHALE_DB, get_stats,
    expire_stale_signals,
)
from core.polymarket_kalshi_feeder import process_polymarket_feed

logger = logging.getLogger("polymarket_whale_collector")

SUMMARY_TEMPLATE = """
╔══════════════════════════════════════════════════════════╗
║  Polymarket WhaleWatch — Daily Collection Report        ║
╠══════════════════════════════════════════════════════════╣
║  Timestamp:     {timestamp:<36} ║
║──────────────────────────────────────────────────────────║
║  Trades Fetched:      {trades:<6}                          ║
║  Markets Fetched:     {markets:<6}                          ║
║  Consensus Signals:   {consensus:<6}                          ║
║  Weather Signals:     {weather:<6}                          ║
║  Signals Persisted:   {persisted:<6}                          ║
║  Signals Expired:     {expired:<6}                          ║
╠══════════════════════════════════════════════════════════╣
║  DB Stats:                                              ║
║    Total Traders:      {traders:<6}                          ║
║    Total Markets:      {markets_total:<6}                          ║
║    Weather Markets:    {weather_mkts:<6}                          ║
║    Active Feed Signals:{active:<6}                          ║
║    Weather Relevant:   {weather_rel:<6}                          ║
╠══════════════════════════════════════════════════════════╣
║  Errors: {errors:<43} ║
╚══════════════════════════════════════════════════════════╝
"""


def compute_conviction_range(conn) -> dict:
    """Compute conviction multiplier statistics from active signals."""
    row = conn.execute("""
        SELECT
            COUNT(*) as count,
            MIN(conviction_multiplier) as min_mult,
            MAX(conviction_multiplier) as max_mult,
            AVG(conviction_multiplier) as avg_mult
        FROM polymarket_signals_feed
        WHERE status='PENDING'
          AND kalshi_station IS NOT NULL
    """).fetchone()

    if row and row['count'] > 0:
        return {
            'count': row['count'],
            'min': round(row['min_mult'], 3),
            'max': round(row['max_mult'], 3),
            'avg': round(row['avg_mult'], 3),
        }
    return {'count': 0, 'min': 1.0, 'max': 1.0, 'avg': 1.0}


def list_weather_signals(conn) -> list:
    """List current weather-relevant active signals for the report."""
    rows = conn.execute("""
        SELECT f.kalshi_station, f.kalshi_bucket, f.kalshi_series,
               f.signal_direction, f.conviction_multiplier,
               f.whale_count, f.total_notional, m.title as market_title
        FROM polymarket_signals_feed f
        LEFT JOIN polymarket_markets m ON f.marketId = m.id
        WHERE f.status='PENDING'
          AND f.kalshi_station IS NOT NULL
          AND f.kalshi_bucket IS NOT NULL
        ORDER BY f.conviction_multiplier DESC, f.whale_count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket WhaleWatch — Daily Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                         # Normal run
    %(prog)s --verbose               # Verbose logging
    %(prog)s --dry-run               # Fetch and analyze, don't persist
    %(prog)s --db /tmp/test.db       # Custom DB path
        """
    )
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose logging')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress non-essential output')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Fetch and analyze but do not persist')
    parser.add_argument('--db', type=str, default=None,
                        help=f'Custom DB path (default: {PM_WHALE_DB})')
    parser.add_argument('--json', action='store_true',
                        help='Output summary as JSON instead of report')
    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else (
        logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    db_path = args.db or PM_WHALE_DB

    start_time = datetime.now(timezone.utc)

    # Phase 1: Run the feed processor
    logger.info("Starting Polymarket WhaleWatch collection...")

    if args.dry_run:
        # Dry run: fetch and analyze, persist to an in-memory temp DB
        import tempfile
        tmp_db = os.path.join(tempfile.gettempdir(),
                              f"pm_whale_dryrun_{int(start_time.timestamp())}.db")
        logger.info(f"Dry run — persisting to temporary DB: {tmp_db}")
        result = process_polymarket_feed(db_path=tmp_db)
        # Cleanup
        try:
            os.remove(tmp_db)
            if os.path.exists(tmp_db + "-wal"):
                os.remove(tmp_db + "-wal")
            if os.path.exists(tmp_db + "-shm"):
                os.remove(tmp_db + "-shm")
        except OSError:
            pass

        # For dry run, read DB stats from result
        stats = result.get('db_stats', {})
        conviction_info = {'count': 0, 'min': 1.0, 'max': 1.0, 'avg': 1.0}
        weather_signals_list = []
    else:
        result = process_polymarket_feed(db_path=db_path)

        # Phase 2: Read DB stats
        conn = get_db(db_path)
        try:
            stats = get_stats(conn)
            conviction_info = compute_conviction_range(conn)
            weather_signals_list = list_weather_signals(conn)
        finally:
            conn.close()

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    # Build summary
    summary = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'elapsed_seconds': round(elapsed, 1),
        'dry_run': args.dry_run,
        'trades_fetched': result.get('trades_fetched', 0),
        'markets_fetched': result.get('markets_fetched', 0),
        'consensus_signals': result.get('consensus_signals', 0),
        'weather_signals': result.get('weather_signals_mapped', 0),
        'signals_persisted': result.get('signals_persisted', 0),
        'signals_expired': result.get('signals_expired', 0),
        'traders_total': stats.get('trader_count', 0),
        'markets_total': stats.get('market_count', 0),
        'weather_markets': stats.get('weather_market_count', 0),
        'active_feed_signals': stats.get('active_signals', 0),
        'weather_relevant_signals': stats.get('weather_relevant_signals', 0),
        'conviction_range': conviction_info,
        'errors': result.get('errors', [])[:5],
        'weather_signals': weather_signals_list[:15],  # Top 15 for report
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        errors_str = "; ".join(summary['errors'][:2]) if summary['errors'] else "None"
        print(SUMMARY_TEMPLATE.format(
            timestamp=summary['timestamp'],
            trades=summary['trades_fetched'],
            markets=summary['markets_fetched'],
            consensus=summary['consensus_signals'],
            weather=summary['weather_signals'],
            persisted=summary['signals_persisted'],
            expired=summary['signals_expired'],
            traders=summary['traders_total'],
            markets_total=summary['markets_total'],
            weather_mkts=summary['weather_markets'],
            active=summary['active_feed_signals'],
            weather_rel=summary['weather_relevant_signals'],
            errors=errors_str[:43],
        ))

        # Print conviction range
        c = conviction_info
        print(f"\n  Conviction Range:  min={c['min']:.3f}  max={c['max']:.3f}  "
              f"avg={c['avg']:.3f}  ({c['count']} active)")

        # Print top weather signals
        if weather_signals_list:
            print(f"\n  Top Weather-Relevant Signals ({min(len(weather_signals_list), 15)} shown):")
            print(f"  {'Station':<8} {'Bucket':<7} {'Dir':<5} {'Mult':<7} "
                  f"{'Whales':<7} {'Notional':<10} Market")
            print(f"  {'─'*8} {'─'*7} {'─'*5} {'─'*7} {'─'*7} {'─'*10} {'─'*30}")
            for ws in weather_signals_list[:15]:
                title = ws.get('market_title', '') or ''
                print(f"  {ws['kalshi_station']:<8} {ws['kalshi_bucket']}°F   "
                      f"{ws['signal_direction']:<5} {ws['conviction_multiplier']:<7.3f} "
                      f"{ws['whale_count']:<7} ${ws['total_notional']:<8,.0f} "
                      f"{title[:30]}")

        print(f"\n  Completed in {elapsed:.1f}s")

    # Exit code: 0 = success, 1 = errors
    if summary['errors']:
        sys.exit(1)


if __name__ == '__main__':
    main()