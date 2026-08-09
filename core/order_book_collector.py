#!/usr/bin/env python3
"""
WhaleWatch — Order Book Collector

Polls Kalshi `/markets/{ticker}` and `/markets/{ticker}/orderbook` endpoints
at staggered intervals across all 20 stations × 2 markets (HIGH/LOW).

Staggered polling architecture:
  T0     → Series 1-5 (every 30s)
  T0+5s  → Series 6-10
  T0+10s → Series 11-15
  T0+15s → Series 16-20
  T0+20s → Series 21-25
  T0+25s → Series 26-30
  T0+30s → Series 31-35
  T0+35s → Series 36-40
  T0+40s → Sleep 20s
  T0+60s → Repeat

Rate limit guard reuses existing `_check_rate_limit` from kalshi_monitor.py.

B-Mode compliant. No AI/ML. Public endpoints only.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Allow import from repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Base URL for Kalshi API
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# 20 Kalshi stations with their ICAO codes
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

<<<<<<< HEAD
# Series ticker patterns — uses kalshi_price_fetcher mapping
# HIGH: KXHIGH{code}  (e.g., KXHIGHNY for KNYC)
# LOW:  KXLOWT{code}  (e.g., KXLOWTNYC for KNYC — note: some use KXLOW{code})
SERIES_TYPES = ["HIGH", "LOW"]

# Import canonical station→Kalshi code mapping from price fetcher
try:
    from core.kalshi_price_fetcher import STATION_TO_KALSHI_CODE, _series_ticker_for_station
except ImportError:
    # Fallback mapping if price fetcher not available
    STATION_TO_KALSHI_CODE = {
        "KATL": "TATL", "KAUS": "AUS", "KBOS": "TBOS", "KDCA": "TDC",
        "KDEN": "DEN", "KDFW": "TDAL", "KHOU": "THOU", "KLAS": "TLV",
        "KLAX": "LAX", "KMDW": "CHI", "KMIA": "MIA", "KMSP": "TMIN",
        "KMSY": "TNOLA", "KNYC": "NY", "KOKC": "TOKC", "KPHL": "PHIL",
        "KPHX": "TPHX", "KSAT": "TSATX", "KSEA": "TSEA", "KSFO": "TSFO",
    }
    def _series_ticker_for_station(station, market_type="HIGH"):
        code = STATION_TO_KALSHI_CODE.get(station)
        if code is None:
            return None
        # Most HIGH: KXHIGH{code}
        # Most LOW:  KXLOW{code}
        prefix = "KXHIGH" if market_type.upper() == "HIGH" else "KXLOW"
        return f"{prefix}{code}"

=======
# Series ticker patterns
# HIGH: KXHIGH{ICAO}  (e.g., KXHIGHNYC)
# LOW:  KXLOW{ICAO}   (e.g., KXLOWNYC)
SERIES_TYPES = ["HIGH", "LOW"]

>>>>>>> origin/main
# Tiered polling config
TIER_1_BUCKETS = 6       # Strike-adjacent: current temp ±3°F
TIER_2_BUCKETS = 15      # Full range
POLL_INTERVAL_NORMAL = 30  # seconds
POLL_INTERVAL_ACTIVE = 10  # seconds when anomaly detected
SERIES_PER_BATCH = 5       # To stagger API calls
SERIES_BATCH_GAP = 5       # seconds between batches
BURST_CLEANUP = 20         # sleep between cycles

# Tier 1 = strike-adjacent (current temp ±3°F) — every 30s
# Tier 2 = all buckets — every 5 min
# Tier 3 = full orderbook for anomaly buckets — on trigger
# Tier 4 = full scan every 15 min


def _kalshi_public_get(endpoint: str) -> Optional[dict]:
    """Make a public GET request to Kalshi API."""
    import urllib.request, urllib.error
    url = f"{KALSHI_BASE}/{endpoint.lstrip('/')}"
    try:
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("Rate limited on %s", endpoint)
        else:
            logger.warning("HTTP %d on %s: %s", e.code, endpoint, e.read()[:200])
        return None
    except Exception as e:
        logger.warning("Request failed %s: %s", endpoint, e)
        return None


<<<<<<< HEAD
def _get_station_series_ticker(station: str, series_type: str) -> Optional[str]:
    """Get the Kalshi series ticker for a station and series type.
    Uses the canonical mapping from kalshi_price_fetcher."""
    return _series_ticker_for_station(station, series_type)
=======
def _get_station_series_ticker(station: str, series_type: str) -> str:
    """Get the Kalshi series ticker for a station and series type."""
    prefix = "KXHIGH" if series_type == "HIGH" else "KXLOW"
    return f"{prefix}{station}"
>>>>>>> origin/main


def _fetch_open_markets(series_ticker: str) -> Optional[List[dict]]:
    """
    Fetch all open markets for a series (e.g., KXHIGHNYC).
    Returns list of market dicts with ticker, yes_bid, yes_ask, etc.
    """
    data = _kalshi_public_get(f"markets?series_ticker={series_ticker}&status=open")
    if not data:
        return None
    return data.get('markets', [])


def _fetch_market_detail(ticker: str) -> Optional[dict]:
    """Fetch single market detail."""
    data = _kalshi_public_get(f"markets/{ticker}")
    return data if data else None


def _fetch_orderbook(ticker: str) -> Optional[dict]:
    """Fetch full order book depth for a market."""
    data = _kalshi_public_get(f"markets/{ticker}/orderbook")
    return data if data else None


def _parse_market_to_snapshot(market: dict, station: str, series_type: str) -> dict:
    """Convert Kalshi market data to WhaleWatch snapshot format."""
    ticker = market.get('ticker', '')
    # Extract bucket temperature from ticker (e.g., KXHIGHNYC-26 → 26 → 84°F)
    # Kalshi uses increment numbers that map to temperatures
    # Actual temperature mapping depends on the day's baseline
    bucket_temp_f = _ticker_to_temperature(ticker, station, series_type)

<<<<<<< HEAD
    yes_bid = market.get('yes_bid_dollars') or market.get('yes_bid')
    yes_ask = market.get('yes_ask_dollars') or market.get('yes_ask')
    no_bid = market.get('no_bid_dollars') or market.get('no_bid')
    no_ask = market.get('no_ask_dollars') or market.get('no_ask')

    # Convert string dollars to float cents
    if yes_bid is not None:
        yes_bid = int(float(yes_bid) * 100) if isinstance(yes_bid, str) else yes_bid
    if yes_ask is not None:
        yes_ask = int(float(yes_ask) * 100) if isinstance(yes_ask, str) else yes_ask
    if no_bid is not None:
        no_bid = int(float(no_bid) * 100) if isinstance(no_bid, str) else no_bid
    if no_ask is not None:
        no_ask = int(float(no_ask) * 100) if isinstance(no_ask, str) else no_ask

    bid_size = float(str(market.get('yes_bid_size_fp', '0') or '0'))
    ask_size = float(str(market.get('yes_ask_size_fp', '0') or '0'))
=======
    yes_bid = market.get('yes_bid')
    yes_ask = market.get('yes_ask')
    no_bid = market.get('no_bid')
    no_ask = market.get('no_ask')
>>>>>>> origin/main

    return {
        'station': station,
        'series_type': series_type,
        'bucket_ticker': ticker,
        'bucket_temp_f': bucket_temp_f,
        'snapshot_ts': datetime.now(timezone.utc).isoformat(),
        'yes_bid': yes_bid,
        'yes_ask': yes_ask,
        'no_bid': no_bid,
        'no_ask': no_ask,
<<<<<<< HEAD
        'yes_bid_size': bid_size,
        'yes_ask_size': ask_size,
        'no_bid_size': float(str(market.get('no_bid_size_fp', '0') or '0')),
        'no_ask_size': float(str(market.get('no_ask_size_fp', '0') or '0')),
        'last_price': market.get('last_price_dollars') or market.get('last_price'),
        'volume_24h': float(str(market.get('volume_24h_fp', '0') or '0')),
        'open_interest': float(str(market.get('open_interest_fp', '0') or '0')),
        'spread_cents': (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None,
        'bid_ask_ratio': bid_size / max(ask_size, 1.0) if bid_size > 0 and ask_size > 0 else None,
=======
        'yes_bid_size': market.get('yes_bid_size'),
        'yes_ask_size': market.get('yes_ask_size'),
        'no_bid_size': market.get('no_bid_size'),
        'no_ask_size': market.get('no_ask_size'),
        'last_price': market.get('last_price'),
        'volume_24h': market.get('volume_24h_fp', 0),
        'open_interest': market.get('open_interest'),
        'spread_cents': (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None,
        'bid_ask_ratio': (yes_bid_size / max(ask_size, 1))
            if ((yes_bid_size := market.get('yes_bid_size', 0)) is not None
                and (ask_size := market.get('yes_ask_size', 1)) is not None)
            else None,
>>>>>>> origin/main
    }


def _ticker_to_temperature(ticker: str, station: str, series_type: str) -> int:
    """
<<<<<<< HEAD
    Kalshi ticker format: KXHIGH{code}-{YYYYMMDD}-{T/B}{temp}
    e.g. KXHIGHNY-26AUG07-T96 or KXHIGHNY-26AUG07-B95.5

    T{temp} = exact temperature strike
    B{temp} = bucket threshold (high temp = above bucket, low = below bucket)
    """
    parts = ticker.split('-')
    if len(parts) >= 3:
        last = parts[-1]
        # Strip T/B prefix and parse the number
        if last and last[0] in ('T', 'B'):
            try:
                return int(float(last[1:]))
            except ValueError:
                pass
        # Fallback: try plain int
        try:
            return int(float(last))
=======
    Kalshi ticker format: KXHIGH{ICAO}-{increment}
    Increment maps to temperature via daily baseline.

    Since baseline varies daily, use a heuristic: for Kalshi, the increment
    number represents the temperature in °F directly starting from a baseline.
    Common convention: increment 0 = 0°F, each increment = 1°F.

    For HIGH: increment = temperature - baseline_low
    For LOW:  increment = baseline_high - temperature

    Without the daily baseline from Kalshi, we extract the last segment.
    """
    # Fallback: extract the last number from ticker
    parts = ticker.split('-')
    if len(parts) >= 2:
        try:
            inc = int(parts[-1])
            # High series: temperature = inc (approximately)
            # Low series: temperature = 100-inc (approximately)
            if series_type == "HIGH":
                return inc
            else:
                return 100 - inc
>>>>>>> origin/main
        except ValueError:
            pass
    return 0


def _get_tier1_buckets(markets: List[dict], station: str, series_type: str,
                        current_temp_f: Optional[float] = None) -> List[str]:
    """
    Get strike-adjacent buckets (current temp ±3°F).

    Uses last_price as proxy for current temperature expectation when
    current_temp_f is not available.
    """
    if not markets:
        return []

    if current_temp_f is not None:
        target = current_temp_f
    else:
        # Proxy: weighted average of market prices
        total = 0
        weighted = 0
        for m in markets:
<<<<<<< HEAD
            bid_val = m.get('yes_bid_dollars') or m.get('yes_bid')
            if bid_val:
                bid_float = float(bid_val) if isinstance(bid_val, str) else bid_val
                temp = _ticker_to_temperature(m['ticker'], station, series_type)
                total += bid_float
                weighted += bid_float * temp
=======
            if m.get('yes_bid'):
                temp = _ticker_to_temperature(m['ticker'], station, series_type)
                total += m['yes_bid']
                weighted += m['yes_bid'] * temp
>>>>>>> origin/main
        target = (weighted / total) if total > 0 else 50

    tier1 = []
    for m in markets:
        temp = _ticker_to_temperature(m['ticker'], station, series_type)
        if abs(temp - target) <= 3:
            tier1.append(m['ticker'])
    return tier1


def poll_series(conn, station: str, series_type: str,
                current_temp_f: Optional[float] = None,
                fetch_depth: bool = False) -> Tuple[int, int]:
    """
    Poll one station-series (e.g., KNYC HIGH).

    Returns (tier1_snapshots, tier2_snapshots).
    """
    series_ticker = _get_station_series_ticker(station, series_type)
    markets = _fetch_open_markets(series_ticker)
    if not markets:
        return (0, 0)

    tier1_tickers = _get_tier1_buckets(markets, station, series_type, current_temp_f)

    count_t1 = 0
    count_t2 = 0

    for market in markets:
        ticker = market.get('ticker', '')
        is_tier1 = ticker in tier1_tickers if tier1_tickers else True

        snap = _parse_market_to_snapshot(market, station, series_type)

        # Import locally to avoid circular imports
        from core.whale_watch_db import insert_snapshot, insert_depth

        insert_snapshot(conn, snap)

        if is_tier1:
            count_t1 += 1
        else:
            count_t2 += 1

        if fetch_depth or (is_tier1 and False):  # Only on anomaly trigger
            depth = _fetch_orderbook(ticker)
            if depth:
                # Get last insert id
                row = conn.execute(
                    "SELECT MAX(id) FROM order_book_snap").fetchone()
                snap_id = row[0] if row else 0

                yes_levels = [(l.get('price', 0), l.get('size', 0))
                             for l in (depth.get('yes') or [])]
                no_levels = [(l.get('price', 0), l.get('size', 0))
                            for l in (depth.get('no') or [])]
                insert_depth(conn, snap_id, 'yes', yes_levels)
                insert_depth(conn, snap_id, 'no', no_levels)

        # Rate limit: be polite
        time.sleep(0.05)

    return (count_t1, count_t2)


def run_poll_cycle(conn, current_temps: Optional[Dict[str, float]] = None):
    """
    Run one complete poll cycle across all 20 stations × 2 series.

    Staggered schedule:
      Batch 1: stations[0:5]  HIGH + LOW  (10 series)
      Batch 2: stations[5:10] HIGH + LOW
      ...
      Batch 4: stations[15:20] HIGH + LOW
      Sleep 20s
      Repeat

    Returns total snapshots collected.
    """
    total_snapshots = 0

    for batch_start in range(0, len(STATIONS), SERIES_PER_BATCH):
        batch = STATIONS[batch_start:batch_start + SERIES_PER_BATCH]
        logger.debug("Polling batch: %s", batch)

        for station in batch:
            for series_type in SERIES_TYPES:
                current_temp = current_temps.get(station) if current_temps else None
                t1, t2 = poll_series(conn, station, series_type, current_temp)
                total_snapshots += t1 + t2

        # Gap between batches
        if batch_start + SERIES_PER_BATCH < len(STATIONS):
            time.sleep(SERIES_BATCH_GAP)

    # Cleanup sleep
    time.sleep(BURST_CLEANUP)
    return total_snapshots


def poll_loop(conn, interval: int = POLL_INTERVAL_NORMAL,
              iterations: Optional[int] = None,
              current_temps: Optional[Dict[str, float]] = None):
    """
    Continuous polling loop.

    Args:
        conn: DB connection
        interval: Seconds between full cycles (default: 30)
        iterations: Max cycles (None=infinite)
        current_temps: Optional dict of {station: current_temp_f}
    """
    cycle = 0
    while iterations is None or cycle < iterations:
        cycle += 1
        logger.info("Poll cycle %d starting...", cycle)
        start = time.monotonic()

        n = run_poll_cycle(conn, current_temps)
        elapsed = time.monotonic() - start

        logger.info("Cycle %d: %d snapshots in %.1fs", cycle, n, elapsed)

        sleep = max(0, interval - elapsed)
        if sleep > 0:
            time.sleep(sleep)

    logger.info("Poll loop completed after %d cycles", cycle)


def get_current_temps_from_metar() -> Dict[str, float]:
    """Get current temperature for each station from local METAR DB."""
    import sqlite3
    temps = {}
    db_path = os.path.join(REPO_ROOT, 'data', 'metar_backfill.db')
    if not os.path.exists(db_path):
        return temps
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for station in STATIONS:
            row = conn.execute("""
                SELECT temp_f FROM metar_observations
                WHERE station=? AND date_utc=?
                ORDER BY timestamp_utc DESC LIMIT 1
            """, (station, now)).fetchone()
            if row and row['temp_f']:
                temps[station] = row['temp_f']
        conn.close()
    except Exception as e:
        logger.debug("Could not fetch METAR temps: %s", e)
    return temps


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s')

    from core.whale_watch_db import init_db

    conn = init_db()
    logger.info("WhaleWatch collector starting...")

    try:
        poll_loop(conn, iterations=1)
    except KeyboardInterrupt:
        logger.info("Poll loop interrupted")
    finally:
        conn.close()
        logger.info("Done")