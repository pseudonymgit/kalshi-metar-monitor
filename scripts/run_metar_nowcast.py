#!/usr/bin/env python3
"""
ADVANCE Signal Runner: Intraday METAR Nowcast (P1.x operational)

Compares live METAR observations against GEFS forecasted max/min to
generate intraday nowcast signals. Runs as a standalone check or wires
into the intraday trading loop.

Usage:
    python3 scripts/run_metar_nowcast.py [--stations KNYC,KLAX] [--output json]

B-Mode compliant. No AI/ML. Uses station_registry for station list.
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

from core.station_registry import get_all_stations
from core.signals.metar_nowcast_signal import MetarNowcastSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_metar_nowcast")


def evaluate_stations(stations: List[str], signal: MetarNowcastSignal,
                      bucket_temp: int = 75) -> List[Dict]:
    """
    Evaluate METAR nowcast for each station.

    Uses default GEFS forecasts (85/65 max/min) — in production these
    should come from the actual GEFS forecast DB.
    """
    results = []
    for station in stations:
        try:
            result = signal.evaluate(
                station=station,
                bucket_temp_f=bucket_temp,
                gefs_max_f=85.0,
                gefs_min_f=65.0,
            )
            entry = {
                "station": station,
                "signal_fired": result.get("signal", False),
                "confidence": result.get("confidence", 0.0),
                "direction": result.get("direction"),
                "metar_temp_f": result.get("metar_temp_f"),
                "metar_fresh": result.get("metar_fresh", False),
                "reason": result.get("reason", "unknown"),
            }
            results.append(entry)

            if result.get("signal"):
                logger.info(
                    f"{station}: NOWCAST signal={result['direction']} "
                    f"conf={result['confidence']:.3f} "
                    f"metar={result.get('metar_temp_f')}°F"
                )
            else:
                logger.debug(f"{station}: no signal — {result.get('reason')}")

        except Exception as e:
            results.append({
                "station": station,
                "error": str(e),
                "signal_fired": False,
            })
            logger.error(f"{station}: nowcast failed: {e}")

    return results


def generate_trade_signals(results: List[Dict]) -> List[Dict]:
    """Convert nowcast results into pipeline trade signals."""
    signals = []
    for r in results:
        if r.get("signal_fired") and r.get("confidence", 0) >= 0.4:
            direction = "up" if r["direction"] == "HIGH" else "down"
            signals.append({
                "station": r["station"],
                "direction": direction,
                "confidence": r["confidence"],
                "reason": r.get("reason", "METAR nowcast signal"),
                "signal_name": "metar_nowcast",
                "metar_temp_f": r.get("metar_temp_f"),
            })
    return signals


def main():
    parser = argparse.ArgumentParser(description="Intraday METAR Nowcast Runner")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated station list")
    parser.add_argument("--bucket-temp", type=int, default=75,
                        help="Temperature bucket to evaluate")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path")
    parser.add_argument("--signals-only", action="store_true",
                        help="Only output trade signals, not raw results")
    args = parser.parse_args()

    # Determine stations
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
    else:
        stations = get_all_stations()

    logger.info(f"Running METAR nowcast for {len(stations)} stations")

    # Initialize signal
    signal = MetarNowcastSignal()

    # Evaluate
    results = evaluate_stations(stations, signal, args.bucket_temp)
    signals = generate_trade_signals(results)

    logger.info(f"Generated {len(signals)} nowcast signals")
    for s in signals:
        logger.info(f"  SIGNAL: {s['station']} → {s['direction']} "
                   f"(conf={s['confidence']:.3f})")

    # Build output
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stations_evaluated": len(stations),
        "signals_fired": len([r for r in results if r.get("signal_fired")]),
        "trade_signals": len(signals),
        "results": results if not args.signals_only else None,
        "trade_signals_list": signals,
    }

    output_str = json.dumps(output, indent=2, default=str)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output_str)
        logger.info(f"Output written to {args.output}")
    else:
        print(output_str)

    return signals


if __name__ == "__main__":
    main()