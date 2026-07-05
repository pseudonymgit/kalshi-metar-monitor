#!/usr/bin/env python3
"""
Unified Weather Data Collector — Render Service Edition (v1.0 — 2026-07-05)

Centralized collection service that runs on Render to provide 24/7 reliable data.
Handles METAR, NWP forecasts, and Kalshi snapshot collection for all 20 stations.

This script is designed to run as a background process in the Render production service,
ensuring 24/7 collection availability for all instances (PROD/DEV/SBOX) via shared cache.

All collection is pure deterministic Python — no AI/ML calls.
"""

import sys
import time
import threading
from datetime import datetime, timezone
import logging
import subprocess
from pathlib import Path

# Add core to path
current_dir = Path(__file__).parent
core_dir = current_dir.parent / 'core'
sys.path.insert(0, str(core_dir))

def collect_single_cycles():
    """Execute a single collection cycle for testing purposes."""
    # Import inside function to handle path correctly
    from weather_collector_service import collect_metar_data, collect_nwp_data, collect_kalshi_snapshots, logger

    logger.info("Running single collection cycle (test mode)")
    
    try:
        logger.info("Collecting METAR data...")
        collect_metar_data()
    except Exception as e:
        logger.error(f"METAR collection test failed: {e}")
        import traceback
        traceback.print_exc()
        
    try:    
        logger.info("Collecting NWP data...")
        collect_nwp_data()
    except Exception as e:
        logger.error(f"NWP collection test failed: {e}")
        import traceback
        traceback.print_exc()
        
    try:
        logger.info("Collecting Kalshi snapshots...")
        collect_kalshi_snapshots()
    except Exception as e:
        logger.error(f"Kalshi collection test failed: {e}")
        import traceback
        traceback.print_exc()
    
    logger.info("Single collection cycle completed")

def main(daemon_mode: bool = True):
    """
    Main entry point for the unified collector.
    
    Args:
        daemon_mode: If True, run continuously. If False, run once and exit.
    """
    # Setup logger 
    logger = logging.getLogger("weather_collector")
    logger.setLevel(logging.INFO)
    
    (logs_dir := Path(__file__).parent.parent / "logs").mkdir(exist_ok=True)
    handler = logging.FileHandler(logs_dir / "weather_collector.log")
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(handler)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if daemon_mode:
        # Start as an infinite background service
        from weather_collector_service import WeatherCollectorService
        
        collector_service = WeatherCollectorService()
        
        try:
            collector_service.start_background_service()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
            collector_service.stop()
            collector_service.shutdown_event.wait(1)  # Wait a moment for clean termination
        except Exception as e:
            logger.error(f"Fatal error in weather collector service: {e}")
            import traceback
            traceback.print_exc()
    else:
        # Run once for testing purposes
        collect_single_cycles()


if __name__ == "__main__":
    # Determine mode based on command line args
    # --test flag for single execution, everything else runs continuously
    daemon_mode = True
    if len(sys.argv) > 1:
        if sys.argv[1].lower() in ('--test', '--once', '-t'):
            daemon_mode = False
        elif sys.argv[1] in ('--help', '-h'):
            print("Usage:")
            print("  python3 collect_all.py          # Run continuously (daemon mode)")
            print("  python3 collect_all.py --test   # Run once and exit (for testing)")
            sys.exit(0)
    
    main(daemon_mode=daemon_mode)