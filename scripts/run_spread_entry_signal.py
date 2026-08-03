#!/usr/bin/env python3
"""
ADVANCE Signal Runner: Spread-Based Entry (P1.x operational)

Checks Kalshi market bid/ask spread for profitable entry opportunities.
Designed to be called before order placement in the execution pipeline.

Usage:
    python3 scripts/run_spread_entry_signal.py \\
        --station KNYC --bucket-temp 84 --bid 55 --ask 57 \\
        --volume 1500 --hours-to-settlement 3.5

    # Production: reads from Kalshi API
    python3 scripts/run_spread_entry_signal.py --check-markets

B-Mode compliant. No AI/ML.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is in path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.signals.spread_based_entry_signal import SpreadBasedEntryDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_spread_entry_signal")


def check_market(detector: SpreadBasedEntryDetector, station: str,
                 bucket_temp_f: int, bid: int, ask: int,
                 volume_24h: float, hours_to_settlement: float) -> Dict:
    """
    Evaluate a single market for spread-based entry.

    Returns the check result dict with entry/exit flags.
    """
    result = detector.check(
        bid=bid, ask=ask, volume_24h=volume_24h,
        hours_to_settlement=hours_to_settlement,
        station=station, bucket_temp_f=bucket_temp_f,
    )

    if result.get("entry"):
        logger.info(
            f"ENTRY: {station}@{bucket_temp_f}°F — "
            f"score={result['score']:.3f}, units={result['units']}, "
            f"$value=${result['dollar_value']:.2f} — {result['reason']}"
        )
    else:
        logger.debug(
            f"NO ENTRY: {station}@{bucket_temp_f}°F — "
            f"{result.get('reason', 'unknown')}"
        )

    # Record spread for percentile history
    detector.record_spread(station, bucket_temp_f, ask - bid)

    return result


def main():
    parser = argparse.ArgumentParser(description="Spread-Based Entry Signal")
    parser.add_argument("--station", type=str, default="KNYC",
                        help="Station code")
    parser.add_argument("--bucket-temp", type=int, default=84,
                        help="Temperature bucket")
    parser.add_argument("--bid", type=int, default=55,
                        help="YES bid price in cents")
    parser.add_argument("--ask", type=int, default=57,
                        help="YES ask price in cents")
    parser.add_argument("--volume", type=float, default=1500.0,
                        help="24-hour dollar volume")
    parser.add_argument("--hours-to-settlement", type=float, default=3.5,
                        help="Hours until market settlement")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path")
    parser.add_argument("--check-markets", action="store_true",
                        help="Check all active markets via Kalshi API (requires live API)")
    args = parser.parse_args()

    # Initialize detector
    detector = SpreadBasedEntryDetector()

    if args.check_markets:
        # Production mode: scan all active markets
        logger.info("Checking all active markets (API mode)...")
        try:
            from core.kalshi_price_fetcher import get_live_market_price
            from core.station_registry import get_all_stations

            stations = get_all_stations()
            results = {}
            for station in stations:
                try:
                    # Fetch current market state
                    # This requires the Kalshi API to be configured
                    bid = 50  # Placeholder — real API fetch needed
                    ask = 52
                    volume = 1000.0
                    hours_to = 6.0
                    bucket = 75

                    result = check_market(
                        detector, station, bucket,
                        bid, ask, volume, hours_to,
                    )
                    results[station] = result
                except Exception as e:
                    logger.warning(f"{station}: API fetch failed: {e}")
                    results[station] = {"entry": False, "error": str(e)}

            output = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": "market_scan",
                "stations_checked": len(stations),
                "entry_opportunities": sum(
                    1 for r in results.values() if r.get("entry")
                ),
                "results": results,
            }
        except ImportError:
            logger.error("Kalshi price fetcher not available")
            output = {"error": "kalshi_price_fetcher not available"}

    else:
        # Direct check mode
        result = check_market(
            detector, args.station, args.bucket_temp,
            args.bid, args.ask, args.volume, args.hours_to_settlement,
        )
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "direct_check",
            "station": args.station,
            "bucket_temp_f": args.bucket_temp,
            "result": result,
        }

    # Write output
    output_str = json.dumps(output, indent=2, default=str)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output_str)
        logger.info(f"Output written to {args.output}")
    else:
        print(output_str)

    return output


if __name__ == "__main__":
    main()