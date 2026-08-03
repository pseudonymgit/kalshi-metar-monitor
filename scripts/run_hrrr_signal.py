#!/usr/bin/env python3
"""
ADVANCE Signal Runner: HRRR Bias-Corrected (P1.x operational)

Fetches 3-day HRRR forecasts for all active stations via Open-Meteo,
applies rolling bias correction, and logs results for the pipeline.

Usage:
    python3 scripts/run_hrrr_signal.py [--stations KNYC,KLAX] [--output json]

B-Mode compliant. No AI/ML. Uses station_registry for coordinates.
"""

import argparse
import json
import logging
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is in path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.station_registry import get_all_stations, get_station_coordinates
from core.signals.hrrr_bias_corrected_signal import HRRRBiasCorrectedSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_hrrr_signal")


def collect_forecasts(stations: List[str], signal: HRRRBiasCorrectedSignal) -> Dict:
    """Fetch HRRR forecasts for all stations and store in DB."""
    results = {}
    for station in stations:
        lat, lon = get_station_coordinates(station)
        if lat is None or lon is None:
            logger.warning(f"No coordinates for {station}, skipping")
            results[station] = {"error": "no_coordinates"}
            continue

        try:
            extremes = signal.get_daily_extremes(station, lat, lon)
            if extremes.get("max_f") is not None:
                results[station] = {
                    "max_f": extremes["max_f"],
                    "min_f": extremes["min_f"],
                    "confidence": extremes["confidence"],
                    "bias_f": extremes.get("bias_f"),
                    "max_hour": extremes.get("max_hour"),
                    "min_hour": extremes.get("min_hour"),
                }
                logger.info(
                    f"{station}: HRRR max={extremes['max_f']}°F, "
                    f"min={extremes['min_f']}°F, confidence={extremes['confidence']:.3f}"
                )
            else:
                results[station] = {"error": "no_forecast"}
                logger.warning(f"{station}: No HRRR forecast returned")
        except Exception as e:
            results[station] = {"error": str(e)}
            logger.error(f"{station}: HRRR fetch failed: {e}")

    return results


def generate_trade_signals(results: Dict) -> List[Dict]:
    """
    Convert HRRR forecasts into trade signals for the pipeline.
    Produces directional signals when bias-corrected confidence is high.
    """
    signals = []
    for station, data in results.items():
        if "error" in data or data.get("confidence", 0) < 0.4:
            continue

        # Directional logic: if bias-corrected max differs significantly from
        # climatological 70°F baseline, generate a signal.
        # This will improve when GEFS comparison is added.
        max_f = data["max_f"]
        if max_f > 75:
            signals.append({
                "station": station,
                "direction": "up",
                "confidence": min(data["confidence"] + 0.05, 0.95),
                "reason": f"HRRR bias-corrected max {max_f}°F above 75°F threshold",
                "signal_name": "hrrr_bias_corrected",
            })
        elif max_f < 65:
            signals.append({
                "station": station,
                "direction": "down",
                "confidence": min(data["confidence"] + 0.05, 0.95),
                "reason": f"HRRR bias-corrected max {max_f}°F below 65°F threshold",
                "signal_name": "hrrr_bias_corrected",
            })

    return signals


def main():
    parser = argparse.ArgumentParser(description="HRRR Bias-Corrected Signal Runner")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated station list (default: all from registry)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path (default: stdout)")
    parser.add_argument("--collect-only", action="store_true",
                        help="Only collect forecasts, don't generate signals")
    args = parser.parse_args()

    # Determine stations
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
    else:
        stations = get_all_stations()

    logger.info(f"Running HRRR signal for {len(stations)} stations")

    # Initialize signal
    signal = HRRRBiasCorrectedSignal()

    # Collect forecasts
    results = collect_forecasts(stations, signal)

    # Generate signals
    signals = []
    if not args.collect_only:
        signals = generate_trade_signals(results)
        logger.info(f"Generated {len(signals)} trade signals from HRRR data")
        for s in signals:
            logger.info(f"  SIGNAL: {s['station']} → {s['direction']} "
                       f"(conf={s['confidence']:.3f}, reason={s['reason']})")

    # Build output
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stations_queried": len(stations),
        "stations_with_data": sum(1 for v in results.values() if "error" not in v),
        "signals_generated": len(signals),
        "forecasts": results,
        "trade_signals": signals,
    }

    # Write output
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