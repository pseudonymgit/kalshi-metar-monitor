#!/usr/bin/env python3
"""
run_whalewatch_collector.py — Run WhaleWatch order book collector.

Starts the OrderBookCollector to poll Kalshi order book snapshots across
all 20 stations × 2 series (HIGH/LOW).

Designed for:
  - background operation (nohup)
  - 1-hour test run
  - production continuous operation

Usage:
    python3 scripts/run_whalewatch_collector.py [--hours 1] [--continuous]

B-Mode compliant. No AI/ML.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
    ])
logger = logging.getLogger(__name__)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Run WhaleWatch order book collector')
    parser.add_argument('--hours', type=float, default=1.0,
                        help='Run duration in hours (default: 1.0)')
    parser.add_argument('--continuous', action='store_true',
                        help='Run indefinitely (ignores --hours)')
    parser.add_argument('--interval', type=int, default=60,
                        help='Poll interval in seconds (default: 60)')
    args = parser.parse_args()

    # Verify .env has Kalshi credentials
    env_path = os.path.join(REPO_ROOT, '.env')
    if not os.path.exists(env_path):
        logger.warning(".env file not found at %s", env_path)
    else:
        with open(env_path) as f:
            content = f.read()
        if 'KALSHI_KEY_ID' not in content or 'KALSHI_PRIVATE_KEY_PEM' not in content:
            logger.warning("Kalshi API credentials not found in .env")
        else:
            logger.info("Kalshi API credentials found")

    # Load Kalshi API key from .env
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('KALSHI_KEY_ID='):
                os.environ['KALSHI_KEY_ID'] = line.split('=', 1)[1]
            elif line.startswith('KALSHI_PRIVATE_KEY_PEM='):
                os.environ['KALSHI_PRIVATE_KEY_PEM'] = line.split('=', 1)[1]

    # Initialize DB
    from core.whale_watch_db import init_db
    from core.order_book_collector import poll_loop, get_current_temps_from_metar

    conn = init_db()

    logger.info("=" * 60)
    logger.info("WhaleWatch Collector starting")
    logger.info("Time: %s", datetime.now(timezone.utc).isoformat())
    if args.continuous:
        logger.info("Mode: CONTINUOUS (indefinite)")
    else:
        end_time = time.time() + args.hours * 3600
        logger.info("Mode: TIMED (%s hours until ~%s UTC)",
                    args.hours,
                    datetime.fromtimestamp(end_time, tz=timezone.utc).strftime('%H:%M:%S'))
    logger.info("=" * 60)

    # Get current METAR temperatures for strike-adjacent filtering
    current_temps = get_current_temps_from_metar()
    logger.info("Current METAR temps: %d stations with data", len(current_temps))

    try:
        if args.continuous:
            poll_loop(conn, interval=args.interval, current_temps=current_temps)
        else:
            iterations = int((args.hours * 3600) / args.interval) + 1
            poll_loop(conn, interval=args.interval,
                      iterations=iterations,
                      current_temps=current_temps)
    except KeyboardInterrupt:
        logger.info("WhaleWatch collector stopped by user")
    except Exception as e:
        logger.error("WhaleWatch collector error: %s", e, exc_info=True)
    finally:
        conn.close()
        logger.info("WhaleWatch collector finished")

    # Print summary
    from core.whale_watch_db import get_stats
    try:
        stats = get_stats()
        if stats:
            logger.info("Collection stats: %s", stats)
    except Exception:
        pass


if __name__ == '__main__':
    main()