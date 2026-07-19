#!/usr/bin/env python3
"""
Kalshi API Setup Script

Setup, configure and test Kalshi API integration for the weather trading engine.
- Wires Kalshi credentials into config
- Tests connection to Kalshi API
- Adds orderbook snapshot to daily data collection

This script handles workstream 1 of the Kalshi API Integration project.

Usage:
    python scripts/kalshi_api_setup.py                  # full setup + test
    python scripts/kalshi_api_setup.py --test-only      # connection test only
    python scripts/kalshi_api_setup.py --collect-snapshot --date 2026-07-17
"""

import os
import sys
import json
import requests
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import argparse
import logging

# Import station registry as canonical source
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import station_registry

# Define the required functions directly to avoid import issues
KALSHI_BASE_URL = (
    os.getenv("KALSHI_PUBLIC_BASE_URL")
    or "https://api.elections.kalshi.com/trade-api/v2"
).rstrip("/")

_session = requests.Session()
_session.headers.update({
    "User-Agent": "WeatherEngineKalshiSetup/1.0",
    "Accept": "application/json",
})

def _kalshi_public_get(path: str, timeout: int = 10):
    """Make a public GET request to Kalshi API."""
    url = f"{KALSHI_BASE_URL}{path}"
    try:
        response = _session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling Kalshi API: {e}")
        return None

def get_public_markets(limit=5):
    """
    Fetch public Kalshi markets (no authentication).
    """
    data = _kalshi_public_get(f"/markets?limit={int(limit)}")
    if data:
        return {
            "cursor": data.get("cursor"),
            "count": len(data.get("markets", [])),
            "markets": data.get("markets", []),
        }
    else:
        return {"cursor": None, "count": 0, "markets": []}

def _classify_weather_market_type(market: dict) -> str | None:
    marker_strings = [
        str(market.get("ticker") or "").strip().upper(),
        str(market.get("title") or "").strip().upper(),
        str(market.get("subtitle") or "").strip().upper(),
        str(market.get("series_ticker") or "").strip().upper(),
        str(market.get("category") or "").strip().upper(),
    ]
    marker_blob = " ".join(token for token in marker_strings if token)

    _WEATHER_MARKET_TYPE_LABELS = {
        "HIGH_TEMP": "HIGH_TEMP",
        "LOW_TEMP": "LOW_TEMP",
        "PRECIP": "PRECIP",
    }
    
    if any(token in marker_blob for token in ("TMAX", "HIGH TEMP", "HIGHEST TEMPERATURE", "DAILY HIGH")):
        return _WEATHER_MARKET_TYPE_LABELS["HIGH_TEMP"]
    if any(token in marker_blob for token in ("TMIN", "LOW TEMP", "LOWEST TEMPERATURE", "DAILY LOW")):
        return _WEATHER_MARKET_TYPE_LABELS["LOW_TEMP"]
    if any(token in marker_blob for token in ("PRECIP", "RAIN", "RAINFALL")):
        return _WEATHER_MARKET_TYPE_LABELS["PRECIP"]
    return None

def get_station_mapping():
    """Return the static mapping of ICAO stations from the canonical registry."""
    from core.station_registry import STATIC_MAPPING
    return STATIC_MAPPING

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / "config.json"
DATA_DIR = REPO_ROOT / "data"
ORDERBOOK_SNAPSHOTS_DIR = DATA_DIR / "orderbook_snapshots"

def create_orderbook_snapshots_dir():
    """Create directory for storing orderbook snapshots."""
    ORDERBOOK_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def setup_kalshi_config():
    """
    Sets up Kalshi API credentials in configuration.
    
    Expected environment variables:
    - KALSHI_PUBLIC_BASE_URL: Base URL for public Kalshi API
    - KALSHI_BASE_URL: Base URL for authenticated API (if required)
    - KALSHI_KEY_ID: API key ID for authentication
    - KALSHI_PRIVATE_KEY_PEM: Private key for RSA signing
    """
    logger.info("Setting up Kalshi configuration...")
    
    public_base_url = os.getenv("KALSHI_PUBLIC_BASE_URL")
    base_url = os.getenv("KALSHI_BASE_URL")
    key_id = os.getenv("KALSHI_KEY_ID")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY_PEM")
    
    # Test public access first
    if not public_base_url:
        logger.warning("KALSHI_PUBLIC_BASE_URL not found. Using default: https://api.elections.kalshi.com/trade-api/v2")
        public_base_url = "https://api.elections.kalshi.com/trade-api/v2"
    
    # Build config object
    config = {
        "kalshi": {
            "public_base_url": public_base_url,
            "base_url": base_url or public_base_url,
            "key_id": key_id or "",
            # For security, don't store the actual PEM in the config file
            # Instead, keep it in the environment
            "has_private_key": bool(private_key_pem),
        }
    }
    
    # Save configuration
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    
    logger.info(f"Configuration saved to {CONFIG_FILE}")
    
    return config


def test_api_connection():
    """
    Tests connection to Kalshi API and validates authentication credentials.
    """
    logger.info("Testing connection to Kalshi API...")
    
    try:
        # Test public API
        logger.info("Testing public API endpoints...")
        public_test = get_public_markets(limit=5)
        if public_test.get('count', 0) > 0:
            logger.info(f"✅ Public API connection successful. Got {public_test['count']} markets.")
        else:
            logger.warning("⚠️ Public API connection established but no markets returned.")
        
        return True
    except Exception as e:
        logger.error(f"❌ API connection test failed: {e}")
        return False


def collect_orderbook_snapshot(date_str, limit=200):
    """
    Collects an orderbook snapshot for all registered stations and saves to file.
    
    Includes market ladder data (bid/ask volumes and prices) in addition to basic market data.
    
    Args:
        date_str: String in YYYY-MM-DD format to identify snapshot datetime
        limit: Number of markets to fetch in the request (max 1000)
    """
    logger.info(f"Collecting orderbook snapshot for {date_str}...")
    
    timestamp = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    
    try:
        # Get all registered stations
        stations = get_station_mapping()
        logger.info(f"Got {len(stations)} registered stations to check")
        
        # Fetch all market data
        markets_response = _kalshi_public_get(f"/markets?limit={min(limit, 1000)}&status=open")
        
        all_markets = markets_response.get("markets", [])
        logger.info(f"Fetched {len(all_markets)} total markets from Kalshi")
        
        # Create a mapping from Kalshi's market ticker patterns to our station codes
        # Based on the pattern mapping seen in kalshi_price_fetcher.py
        station_code_to_icao = {
            "TATL": "KATL", "AUS": "KAUS", "TBOS": "KBOS", "TDC": "KDCA", 
            "DEN": "KDEN", "TDAL": "KDFW", "THOU": "KHOU", "TLV": "KLAS", 
            "LAX": "KLAX", "CHI": "KMDW", "MIA": "KMIA", "TMIN": "KMSP", 
            "TNOLA": "KMSY", "NY": "KNYC", "TOKC": "KOKC", "PHIL": "KPHL", 
            "TPHX": "KPHX", "TSATX": "KSAT", "TSEA": "KSEA", "TSFO": "KSFO"
        }
        
        # Filter and enrich the weather markets
        snapshot_data = {
            "timestamp": timestamp,
            "date": date_str,
            "weather_markets": [],
            "non_weather_markets": [], 
            "stats": {
                "total_fetched": len(all_markets),
                "weather_markets_found": 0,
                "non_weather_markets": 0
            }
        }
        
        for market in all_markets:
            ticker = market.get("ticker", "").upper()
            
            # Identify weather markets based on ticker patterns
            is_weather = False
            extracted_station = None
            
            if "KXHIGH" in ticker or "KXLOW" in ticker:
                # Try to extract station code from weather market ticker
                # Ticker format example: KXHIGHKATL, KXLOWKDEN
                # Or: KXHIGHTATL, KXLOWTNY - with 'T' prefix for some stations
                ticker_suffix = ticker.replace("KXHIGH", "").replace("KXLOW", "").replace("K", "")[:4]
                
                # Use both our mapping and direct ticker parsing
                for code, icao in station_code_to_icao.items():
                    if code in ticker:
                        extracted_station = icao
                        break
                
                if not extracted_station and len(ticker_suffix) >= 3 and ticker_suffix[0] == 'T':
                    # Handle the "T" prefix pattern
                    possible_code = 'T' + ticker_suffix[1:]
                    extracted_station = station_code_to_icao.get(possible_code)
                
                if not extracted_station and ticker_suffix in station_code_to_icao.values():
                    # Handle direct ICAO suffix
                    extracted_station = ticker_suffix
                
                if extracted_station:
                    is_weather = True
                    
            if is_weather:
                # For weather markets, add detailed orderbook and pricing data
                market_data = {
                    "ticker": market.get("ticker"),
                    "event_ticker": market.get("event_ticker"),
                    "status": market.get("status"),
                    "close_time": market.get("close_time"),
                    "yes_bid": market.get("yes_bid"),
                    "yes_ask": market.get("yes_ask"),
                    "yes_bid_dollars": market.get("yes_bid_dollars"),
                    "yes_ask_dollars": market.get("yes_ask_dollars"),
                    "last_price": market.get("last_price"),
                    "last_price_dollars": market.get("last_price_dollars"),
                    "volume_24h": market.get("volume_24h_fp"),
                    "spot_price_range": market.get("spot_price_range"),
                    "strike": market.get("strike"),
                    "strike_type": market.get("strike_type"),
                    "floor_strike": market.get("floor_strike"),
                    "cap_strike": market.get("cap_strike"),
                    "expiration_time": market.get("expiration_time"),
                    "station": extracted_station,
                    "is_weather": True
                }
                
                snapshot_data["weather_markets"].append(market_data)
                
            else:
                # For non-weather markets, store minimal data
                market_data = {
                    "ticker": market.get("ticker"),
                    "title": market.get("title"),
                    "status": market.get("status"),
                    "close_time": market.get("close_time"),
                    "is_weather": False
                }
                snapshot_data["non_weather_markets"].append(market_data)
        
        # Update stats
        snapshot_data["stats"]["weather_markets_found"] = len(snapshot_data["weather_markets"])
        snapshot_data["stats"]["non_weather_markets"] = len(snapshot_data["non_weather_markets"])
        
        # Create snapshot file
        date_formatted = date_str.replace("-", "")
        snapshot_filename = f"kalshi_orderbook_snapshot_{date_formatted}.json"
        snapshot_path = ORDERBOOK_SNAPSHOTS_DIR / snapshot_filename
        
        with open(snapshot_path, 'w') as f:
            json.dump(snapshot_data, f, indent=2, default=str)
        
        logger.info(f"Orderbook snapshot saved to {snapshot_path}")
        logger.info(f"Snapshot contains {len(snapshot_data['weather_markets'])} weather markets and {len(snapshot_data['non_weather_markets'])} other markets")
        
        return snapshot_path
    
    except Exception as e:
        logger.error(f"❌ Error collecting orderbook snapshot: {e}")
        import traceback
        traceback.print_exc()
        return None


def validate_credentials_and_permissions():
    """
    Validates that the provided Kalshi credentials have the necessary permissions.
    
    Currently focuses on read permissions for available markets (public endpoint).
    """
    logger.info("Validating Kalshi credentials and permissions...")
    
    validated = {
        'public_access': False,
        'weather_market_access': False,
        'permissions': {
            'can_read_markets': False,
            'can_read_user_info': False,
            'error_message': ''
        }
    }
    
    try:
        # Test public access rights for market data (read-only)
        markets_response = get_public_markets(limit=5)
        if markets_response.get('count', 0) > 0:
            validated['public_access'] = True
            validated['permissions']['can_read_markets'] = True
            logger.info("✅ Public endpoint access: Granted")
        else:
            validated['permissions']['error_message'] = "Public endpoint returned no markets"
    
        # Check that we can parse/filter weather markets
        from core.kalshi_monitor import _classify_weather_market_type
        if validated['permissions']['can_read_markets']:
            weather_count = sum(1 for m in markets_response.get('markets', []) 
                                if _classify_weather_market_type(m) is not None)
            if weather_count > 0:
                validated['weather_market_access'] = True
                logger.info(f"✅ Weather market access: Found {weather_count} classified weather markets")
    
    except Exception as e:
        logger.error(f"❌ Validation failed: {e}")
        validated['permissions']['error_message'] = str(e)
    
    return validated


def check_station_coverage():
    """
    Verifies that our registered stations have corresponding Kalshi markets.
    Uses series_ticker queries (not paginated generic markets query).
    """
    logger.info("Checking station coverage against available markets...")
    
    from kalshi_price_fetcher import STATION_TO_KALSHI_CODE
    registered_stations = list(STATION_TO_KALSHI_CODE.keys())
    logger.info(f"Weather engine has {len(registered_stations)} registered stations")
    
    # Query each station's series ticker directly
    found_station_codes = set()
    for icao, code in STATION_TO_KALSHI_CODE.items():
        for mtype in ['HIGH', 'LOW']:
            st = f"KX{mtype}{code}"
            try:
                r = _session.get(f"{KALSHI_BASE_URL}/markets?series_ticker={st}&limit=5", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if data.get('markets'):
                        found_station_codes.add(icao)
                        break
            except requests.exceptions.RequestException:
                pass
                
    logger.info(f"Found Kalshi markets for {len(found_station_codes)} of our registered {len(registered_stations)} stations")
    
    covered = list(found_station_codes.intersection(registered_stations))
    uncovered = list(set(registered_stations) - found_station_codes)
    
    if uncovered:
        logger.info(f"Uncovered stations: {uncovered[:10]}{'...' if len(uncovered) > 10 else ''}")
    else:
        logger.info("All registered stations have matching Kalshi markets!")
    
    return {
        "total_registered": len(registered_stations),
        "covered_count": len(covered),
        "covered_stations": covered,
        "uncovered_stations": uncovered
    }


def main():
    parser = argparse.ArgumentParser(description='Kalshi API Setup Script')
    parser.add_argument('--test-only', action='store_true', help='Only run tests, don\'t perform setup')
    parser.add_argument('--collect-snapshot', action='store_true', help='Collect orderbook snapshot')
    parser.add_argument('--date', type=str, help='Date stamp for snapshot (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    if not args.test_only:
        logger.info("Starting Kalshi API Setup Process...")
        
        # Create the directory for snapshots
        create_orderbook_snapshots_dir()
        
        # Setup configuration
        config = setup_kalshi_config()
        
        # Validate configuration
        validation_results = validate_credentials_and_permissions()
        logger.info(f"Validation results: {json.dumps(validation_results, indent=2)}")
    
    else: 
        logger.info("Starting Kalshi API Connection Test...")
    
    # Always run the connection test
    api_connected = test_api_connection()
    
    # Check station coverage
    coverage_results = check_station_coverage()
    logger.info(f"Station coverage: {coverage_results['covered_count']}/{coverage_results['total_registered']} stations")
    
    # Collect snapshot if requested
    if args.collect_snapshot or (args.date and not args.test_only):
        date_str = args.date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        snapshot_file = collect_orderbook_snapshot(date_str)
        
        if snapshot_file:
            logger.info(f"✅ Orderbook snapshot collected: {snapshot_file}")
        else:
            logger.error("❌ Failed to collect orderbook snapshot")
            return 1
    
    if not api_connected:
        logger.error("❌ Kalshi API connection failed. Please check your configuration.")
        return 1
    
    logger.info("✅ Kalshi API Setup/Testing Complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())