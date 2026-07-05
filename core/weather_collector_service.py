"""
Weather Data Collector Service Module (v1.1 — 2026-07-05)

Tightened collection cadence per Dan's directive:
  - METAR: every 3 min during peak hours (12:00-24:00 UTC), every 5 min off-peak
  - Kalshi: every 5 min during trading windows (13:00-01:00 UTC), every 15 min off-hours
  - NWP: daily at 06:00 UTC

Runs on Render to provide 24/7 data for all instances (PROD/DEV/SBOX).
"""

import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import traceback
import sys
import subprocess

# ─── LOGGER SETUP ───────────────────────────────────────────────────────

logger = logging.getLogger("weather_collector")
logger.setLevel(logging.INFO)

REPO_ROOT = Path(__file__).resolve().parent.parent
(LOGS_DIR := REPO_ROOT / "logs").mkdir(exist_ok=True)
handler = logging.FileHandler(LOGS_DIR / "weather_collector.log")
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ─── PEAK HOUR DEFINITIONS ─────────────────────────────────────────────

# METAR peak: 12:00-24:00 UTC (covers US daylight hours when markets trade)
METAR_PEAK_START_UTC = 12
METAR_PEAK_END_UTC = 24

# Kalshi trading window: 13:00 UTC - 01:00 UTC next day (covers Kalshi market hours)
KALSHI_TRADE_START_UTC = 13
KALSHI_TRADE_END_UTC = 25  # 01:00 next day, expressed as 25 for comparison


def _is_metar_peak_hour() -> bool:
    """Check if current UTC hour is within METAR peak collection window."""
    hour = datetime.now(timezone.utc).hour
    return METAR_PEAK_START_UTC <= hour < METAR_PEAK_END_UTC


def _is_kalshi_trading_window() -> bool:
    """Check if current UTC time is within Kalshi trading window."""
    hour = datetime.now(timezone.utc).hour
    # Trading window spans 13:00-01:00 UTC (wraps midnight)
    if hour >= KALSHI_TRADE_START_UTC or hour < (KALSHI_TRADE_END_UTC - 24):
        return True
    return False


# ─── DYNAMIC INTERVAL CALCULATION ──────────────────────────────────────

def _metar_interval() -> int:
    """Return METAR collection interval in seconds based on current time."""
    if _is_metar_peak_hour():
        return 3 * 60   # 3 minutes during peak
    else:
        return 5 * 60   # 5 minutes off-peak


def _kalshi_interval() -> int:
    """Return Kalshi collection interval in seconds based on trading window."""
    if _is_kalshi_trading_window():
        return 5 * 60   # 5 minutes during trading
    else:
        return 15 * 60  # 15 minutes off-hours


# ─── COLLECTION FUNCTIONS ───────────────────────────────────────────────

def collect_metar_data():
    """Call the METAR collection script via subprocess."""
    logger.info("Starting METAR collection")
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "metar_collect_live.py")],
            capture_output=True,
            text=True,
            timeout=180,  # 3 minute timeout (must be < interval)
            cwd=str(REPO_ROOT)
        )
        if result.returncode == 0:
            logger.info("METAR collection completed successfully")
        else:
            logger.error(f"METAR collection failed (rc={result.returncode}): {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logger.error("METAR collection timed out after 3 minutes")
    except Exception as e:
        logger.error(f"METAR collection failed: {e}")
        logger.error(traceback.format_exc())


def collect_nwp_data():
    """Call the NWP collection script via subprocess."""
    logger.info("Starting NWP collection")
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "nwp_collect.py")],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT)
        )
        if result.returncode == 0:
            logger.info("NWP collection completed successfully")
        else:
            logger.error(f"NWP collection failed (rc={result.returncode}): {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logger.error("NWP collection timed out after 5 minutes")
    except Exception as e:
        logger.error(f"NWP collection failed: {e}")
        logger.error(traceback.format_exc())


def collect_kalshi_snapshots():
    """Capture Kalshi market state for all 20 stations."""
    logger.info("Starting Kalshi snapshot collection")
    try:
        sys.path.insert(0, str(REPO_ROOT / "core"))
        from kalshi_price_fetcher import get_live_market_price
        
        all_stations = [
            "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
            "KDFW", "KHOU", "KLAS", "KLAX", "KMDW",
            "KMIA", "KMSP", "KMSY", "KNYC", "KOKC",
            "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
        ]
        
        successful = 0
        for station in all_stations:
            try:
                high_price, _ = get_live_market_price(station, "HIGH")
                low_price, _ = get_live_market_price(station, "LOW")
                if high_price is not None:
                    successful += 1
            except Exception as station_error:
                logger.warning(f"Kalshi snapshot failed for {station}: {station_error}")
        
        logger.info(f"Kalshi snapshot completed: {successful}/{len(all_stations)} stations")
    except Exception as e:
        logger.error(f"Kalshi snapshot collection failed: {e}")
        logger.error(traceback.format_exc())


# ─── WEATHER COLLECTOR SERVICE CLASS ────────────────────────────────────

class WeatherCollectorService:
    """
    Background service with dynamic collection intervals:
    
    METAR:
      - Peak hours (12:00-24:00 UTC): every 3 minutes
      - Off-peak: every 5 minutes
    
    Kalshi:
      - Trading window (13:00-01:00 UTC): every 5 minutes
      - Off-hours: every 15 minutes
    
    NWP:
      - Daily at 06:00 UTC
    """
    
    def __init__(self):
        self.shutdown_event = threading.Event()
        self.last_runs = {
            "metar": 0,
            "nwp": 0,
            "kalshi": 0
        }
        self.nwp_daily_hour = 6
        self.collection_stats = {
            "metar_runs": 0,
            "nwp_runs": 0,
            "kalshi_runs": 0,
            "errors": 0
        }
        # Track current intervals for logging
        self._current_metar_interval = 0
        self._current_kalshi_interval = 0
    
    def should_run_nwp_now(self) -> bool:
        """Check if it's time to run the daily NWP collection."""
        now = datetime.now(timezone.utc)
        if now.hour == self.nwp_daily_hour and now.minute < 5:
            last_run_date = datetime.fromtimestamp(self.last_runs["nwp"], tz=timezone.utc).date()
            if last_run_date != now.date():
                return True
        return False
    
    def run_collection_cycle(self):
        """Execute collection jobs based on dynamic scheduling."""
        current_time = time.time()
        current_datetime = datetime.now(timezone.utc)
        
        # Calculate current intervals
        metar_interval = _metar_interval()
        kalshi_interval = _kalshi_interval()
        
        # Log interval changes
        if metar_interval != self._current_metar_interval:
            mode = "peak" if metar_interval == 180 else "off-peak"
            logger.info(f"METAR interval changed to {metar_interval}s ({mode})")
            self._current_metar_interval = metar_interval
        
        if kalshi_interval != self._current_kalshi_interval:
            mode = "trading" if kalshi_interval == 300 else "off-hours"
            logger.info(f"Kalshi interval changed to {kalshi_interval}s ({mode})")
            self._current_kalshi_interval = kalshi_interval
        
        # Run METAR
        if current_time - self.last_runs["metar"] >= metar_interval:
            collect_metar_data()
            self.last_runs["metar"] = time.time()
            self.collection_stats["metar_runs"] += 1
        
        # Run NWP (daily at 06:00 UTC)
        if self.should_run_nwp_now():
            collect_nwp_data()
            self.last_runs["nwp"] = time.time()
            self.collection_stats["nwp_runs"] += 1
        
        # Run Kalshi
        if current_time - self.last_runs["kalshi"] >= kalshi_interval:
            collect_kalshi_snapshots()
            self.last_runs["kalshi"] = time.time()
            self.collection_stats["kalshi_runs"] += 1
        
        logger.debug(f"Cycle at {current_datetime.isoformat()}")
    
    def start_background_service(self):
        """Start the perpetual collection service."""
        logger.info("=" * 60)
        logger.info("Weather Collector Service v1.1 Starting (Render)")
        logger.info(f"- METAR: 3min peak (12-24 UTC), 5min off-peak")
        logger.info(f"- NWP: daily at 06:00 UTC")
        logger.info(f"- Kalshi: 5min trading (13-01 UTC), 15min off-hours")
        logger.info("- Shared cache for PROD/DEV/SBOX")
        logger.info("=" * 60)
        
        # Initial collection run on startup
        logger.info("Performing initial collection cycle...")
        self.run_collection_cycle()
        
        # Main service loop — check every 30 seconds
        while not self.shutdown_event.is_set():
            try:
                self.run_collection_cycle()
                if self.shutdown_event.wait(timeout=30):
                    break
            except Exception as e:
                logger.error(f"Collection cycle error: {e}")
                logger.error(traceback.format_exc())
                self.collection_stats["errors"] += 1
                time.sleep(30)
        
        logger.info(f"Weather Collector Service stopped. Final stats: {self.collection_stats}")
    
    def stop(self):
        """Signal the background service to stop."""
        logger.info("Stopping Weather Collector Service...")
        self.shutdown_event.set()
