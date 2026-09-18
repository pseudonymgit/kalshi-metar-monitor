#!/usr/bin/env python3
"""
Polymarket → Kalshi Cross-Platform Signal Feeder

Polls Polymarket WhaleWatch live data (data-api.polymarket.com/trades),
identifies weather-relevant consensus signals, maps them to Kalshi
station/bucket pairs, computes conviction multipliers per
FP-MULTI-SIGNAL-FUSION.md (Layer 3 — Bet Sizing), and stores results
in the polymarket_signals_feed table.

Entry point for the daily collector cron (scripts/polymarket_whale_collector.py).

Conviction multiplier formula (from FP-MULTI-SIGNAL-FUSION.md §5.2):

  If signal_direction matches ensemble_direction:
      conviction_multiplier = clamp(agreementScore / threshold, 1.0, 1.5)
  Else (contradicts):
      conviction_multiplier = max(0.2, 1 - 0.5 × anomaly_score)

Where:
  - agreementScore is the consensus agreement score (0.0-1.0)
  - threshold defaults to 0.6
  - anomaly_score is derived from (1.0 - agreementScore) as a proxy
    for cross-platform divergence

B-Mode compliant. No AI/ML.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DATA_API = "https://data-api.polymarket.com"
RECENT_TRADES_LIMIT = 300
CONSENSUS_MIN_WALLETS = 2
MIN_NOTIONAL = 500.0  # $500 minimum notional for weather signals
DEFAULT_CONVICTION_THRESHOLD = 0.6
MAX_RETRIES = 3
REQUEST_TIMEOUT_SEC = 30

# ── Station Mapping ──────────────────────────────────────────────────────

# City → ICAO station code mapping for Polymarket weather market parsing.
# Based on the Kalshi Tier 1-3 station roster.
STATION_MAP = {
    "new york": "KNYC",
    "nyc": "KNYC",
    "chicago": "KORD",
    "los angeles": "KLAX",
    "la": "KLAX",
    "dallas": "KDFW",
    "atlanta": "KATL",
    "boston": "KBOS",
    "philadelphia": "KPHL",
    "washington": "KDCA",
    "dc": "KDCA",
    "seattle": "KSEA",
    "miami": "KMIA",
    "denver": "KDEN",
    "phoenix": "KPHX",
    "houston": "KHOU",
    "san francisco": "KSFO",
    "sf": "KSFO",
    "las vegas": "KLAS",
    "minneapolis": "KMSP",
    "new orleans": "KMSY",
    "oklahoma city": "KOKC",
    "san antonio": "KSAT",
    "austin": "KAUS",
    "kansas city": "KMCI",
    "st louis": "KSTL",
    "portland": "KPDX",
    "sacramento": "KSAC",
    "orlando": "KMCO",
    "tampa": "KTPA",
    "charlotte": "KCLT",
    "nashville": "KBNA",
    "indianapolis": "KIND",
    "columbus": "KCMH",
    "milwaukee": "KMKE",
    "cincinnati": "KCVG",
    "raleigh": "KRDU",
    "salt lake city": "KSLC",
    "richmond": "KRIC",
    "hartford": "KHFD",
    "baltimore": "KBWI",
    "pittsburgh": "KPIT",
    "detroit": "KDTW",
    "san diego": "KSAN",
    "albuquerque": "KABQ",
    "omaha": "KOMA",
    "memphis": "KMEM",
    "louisville": "KSDF",
    "providence": "KPVD",
    "buffalo": "KBUF",
    "rochester": "KROC",
    "birmingham": "KBHM",
    "norfolk": "KORF",
    "albany": "KALB",
    "fresno": "KFAT",
    "tucson": "KTUS",
    "tulsa": "KTUL",
    "el paso": "KELP",
    "honolulu": "PHNL",
    "anchorage": "PANC",
    "fairbanks": "PAFA",
}

# Weather keywords to filter Polymarket markets
WEATHER_KEYWORDS = [
    "temperature", "temp", "high", "low", "heat", "fahrenheit",
    "celsius", "degree", "weather", "freeze", "snowfall",
    "precipitation", "rain", "storm", "hurricane", "wind",
]

# Direction keywords for parsing
UP_KEYWORDS = ["exceed", "above", "higher than", "greater than", "over", "at least", "high"]
DOWN_KEYWORDS = ["below", "lower than", "less than", "under", "low", "stay below", "remain below"]


# ── HTTP Fetch Helpers (std lib only, no requests) ────────────────────────

def _fetch_json(url: str, retries: int = MAX_RETRIES) -> Optional[list]:
    """Fetch JSON from URL with retry logic. Returns parsed JSON or None."""
    last_error = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"accept": "application/json"})
            with urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                logger.warning(f"Unexpected response type from {url}: {type(data)}")
                return None
        except HTTPError as e:
            last_error = e
            if e.code == 429:
                wait = min(2 ** attempt, 30)
                logger.info(f"Rate limited on {url}, retrying in {wait}s")
                time.sleep(wait)
            elif e.code >= 500:
                wait = min(2 ** attempt, 15)
                time.sleep(wait)
            else:
                break
        except (URLError, OSError, json.JSONDecodeError) as e:
            last_error = e
            time.sleep(min(2 ** attempt, 10))

    logger.error(f"Failed to fetch {url} after {retries} retries: {last_error}")
    return None


# ── Polymarket Title Parsing ─────────────────────────────────────────────

def parse_weather_market_title(title: str) -> Optional[Dict]:
    """
    Parse a Polymarket weather market title into Kalshi station, bucket, direction.

    Title patterns (typical):
        "Will the temperature in New York exceed 85°F on August 15?"
        "Will the temperature in NYC be below 32°F on January 10?"
        "Will the high temperature in Chicago be above 90°F on July 4?"
        "Will the temperature in Los Angeles reach at least 100°F on September 1?"

    Returns:
        dict with station, bucket (int), direction ('UP' or 'DOWN'), series ('HIGH')
        or None if not parseable as a weather market.
    """
    if not title:
        return None

    title_lower = title.lower()

    # Check if it's weather-related
    if not any(kw in title_lower for kw in WEATHER_KEYWORDS):
        return None

    # Extract city name: typically "in <City>" or "in <City> on"
    # Handles intermediate words like "stay", "remain", "be"
    city = None
    # Words that end a city name extraction
    city_end_words = r'\s+(?:on|exceed|above|below|be\s|stay|remain|reach|go|will|is|was|and)'
    city_match = re.search(
        r'\bin\s+([A-Za-z\s]+?)' + city_end_words, title
    )
    if city_match:
        city = city_match.group(1).strip().rstrip(',')

    if not city:
        # Try "the temperature in <City>" with end-word markers
        city_match = re.search(
            r'temperature\s+in\s+([A-Za-z\s]+?)' + city_end_words, title_lower
        )
        if city_match:
            city = city_match.group(1).strip().rstrip(',')

    if not city:
        return None

    # Map city to ICAO station code
    station = STATION_MAP.get(city.lower())
    if not station:
        return None

    # Extract temperature threshold
    temp_match = re.search(r'(\d+)\s*°?\s*[fF]', title)
    if not temp_match:
        temp_match = re.search(r'(\d+)\s*degrees?', title)
    if not temp_match:
        return None

    bucket = int(temp_match.group(1))

    # Determine direction
    direction = None
    # Check for "high temperature" or "low temperature" prefix
    if re.search(r'\blow\s+temperature\b', title_lower):
        direction = 'DOWN'
    elif re.search(r'\btemperature\s+.*\bbe\s+below\b', title_lower) or \
         re.search(r'\bstay\s+below\b', title_lower) or \
         re.search(r'\bremain\s+below\b', title_lower):
        direction = 'DOWN'
    elif re.search(r'\btemperature\s+.*\bbe\s+above\b', title_lower) or \
         re.search(r'\bexceed\b', title_lower) or \
         re.search(r'\bhigh\s+temperature\b', title_lower):
        direction = 'UP'

    if not direction:
        # Fall back to keyword match
        for kw in UP_KEYWORDS:
            if kw in title_lower:
                direction = 'UP'
                break
    if not direction:
        for kw in DOWN_KEYWORDS:
            if kw in title_lower:
                direction = 'DOWN'
                break

    if not direction:
        return None

    return {
        'station': station,
        'bucket': bucket,
        'direction': direction,
        'series': 'HIGH',
    }


def station_tier(station: str) -> int:
    """Get the liquidity tier for a station (1=T1, 2=T2, 3=T3, 0=unknown)."""
    T1 = {"KNYC", "KORD", "KLAX", "KDFW", "KATL", "KBOS", "KPHL", "KDCA"}
    T2 = {"KSEA", "KMIA", "KDEN", "KMDW", "KPHX", "KHOU"}
    T3 = {"KSFO", "KLAS", "KMSP", "KMSY", "KOKC", "KSAT", "KAUS"}
    if station in T1:
        return 1
    if station in T2:
        return 2
    if station in T3:
        return 3
    return 0


# ── Conviction Multiplier ─────────────────────────────────────────────────

def compute_conviction_multiplier(
    agreement_score: float,
    signal_direction: str,
    ensemble_direction: Optional[str] = None,
    threshold: float = DEFAULT_CONVICTION_THRESHOLD,
) -> float:
    """
    Compute the Layer 3 conviction multiplier per FP-MULTI-SIGNAL-FUSION.md.

    If signal_direction matches ensemble_direction:
        conviction_multiplier = clamp(agreementScore / threshold, 1.0, 1.5)
    Else (contradicts):
        conviction_multiplier = max(0.2, 1 - 0.5 × anomaly_score)
    where anomaly_score ≈ 1.0 - agreementScore (proxy for divergence).

    When no ensemble direction is known (default), assume agreement.
    """
    if ensemble_direction is None:
        ensemble_direction = signal_direction

    if signal_direction == ensemble_direction:
        mult = agreement_score / threshold
        return max(1.0, min(mult, 1.5))
    else:
        anomaly_score = 1.0 - agreement_score
        return max(0.2, 1.0 - 0.5 * anomaly_score)


def compute_agreement_score(whale_count: int, weighted_edge: float,
                            total_notional: float) -> float:
    """
    Compute agreement score (0.0-1.0) from consensus signal metrics.

    Combines whale count, weighted edge, and total notional into a
    normalized score. Higher = stronger consensus.
    """
    # Whale count (0-1 factor): 0 whales = 0, 5+ whales = 1.0
    whale_factor = min(whale_count / 5.0, 1.0)

    # Edge factor: abs edge normalized. 0.1 edge = 1.0
    edge_factor = min(abs(weighted_edge) / 0.10, 1.0)

    # Notional factor: $500 = 0.5, $5000+ = 1.0
    notional_factor = min(total_notional / 5000.0, 1.0)

    # Weighted average: whale presence dominates
    score = 0.5 * whale_factor + 0.3 * edge_factor + 0.2 * notional_factor
    return min(score, 1.0)


# ── Data Fetching ────────────────────────────────────────────────────────

def fetch_recent_trades() -> List[dict]:
    """
    Fetch recent Polymarket trades from the data API.

    Returns list of raw trade dicts (same schema as whalewatch-live.ts).
    """
    url = f"{DATA_API}/trades?limit={RECENT_TRADES_LIMIT}"
    data = _fetch_json(url)
    if data is None:
        return []
    return data


def fetch_markets(limit: int = 80, category: Optional[str] = None) -> List[dict]:
    """Fetch markets from Polymarket gamma API. Returns list of market dicts."""
    url = f"https://gamma-api.polymarket.com/markets?limit={limit}&closed=false&tag=weather&tag=climate"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else []


# ── Signal Processing ────────────────────────────────────────────────────

def extract_consensus_signals(trades: List[dict],
                              markets: List[dict]) -> List[dict]:
    """
    Extract consensus signals from Polymarket trades.

    Replicates the logic from whalewatch-live.ts getLiveWhaleIntelligence():
    1. Group trades by (marketSlug, side, outcome)
    2. Filter to groups with >= CONSENSUS_MIN_WALLETS
    3. Build consensus signal dicts with trader details

    Returns list of consensus signal dicts.
    """
    # Build market lookup by slug
    market_by_slug: Dict[str, dict] = {}
    for m in markets:
        slug = m.get('slug', '') or m.get('id', '')
        market_by_slug[slug] = m

    # Group trades by (marketSlug, side, outcome)
    groups: Dict[str, dict] = {}
    for raw in trades:
        slug = str(raw.get('slug', 'unknown-market'))
        side = str(raw.get('side', 'unknown'))
        outcome = str(raw.get('outcome', 'Unknown'))
        key = f"{slug}::{side}::{outcome}"

        if key not in groups:
            groups[key] = {
                'marketSlug': slug,
                'side': side,
                'outcome': outcome,
                'trades': [],
                'byWallet': {},
            }

        wallet = str(raw.get('proxyWallet', ''))
        size = float(raw.get('size', 0))
        price = float(raw.get('price', 0))
        notional = size * price
        display_name = raw.get('name') or raw.get('pseudonym') or wallet[:6] + '…' + wallet[-4:]

        trade = {
            'wallet': wallet,
            'displayName': display_name,
            'side': side,
            'outcome': outcome,
            'size': size,
            'price': price,
            'notional': notional,
            'timestamp': int(raw.get('timestamp', 0)),
        }

        groups[key]['trades'].append(trade)
        if wallet not in groups[key]['byWallet']:
            groups[key]['byWallet'][wallet] = []
        groups[key]['byWallet'][wallet].append(trade)

    # Build consensus signals
    market = market_by_slug.get('') or {}
    signals = []
    for key, group in groups.items():
        wallet_groups = group['byWallet']
        whale_count = len(wallet_groups)
        if whale_count < CONSENSUS_MIN_WALLETS:
            continue

        traders = []
        total_notional = 0.0
        for wallet, wallet_trades in sorted(
            wallet_groups.items(),
            key=lambda x: sum(t['notional'] for t in x[1]),
            reverse=True,
        ):
            wl_notional = sum(t['notional'] for t in wallet_trades)
            wl_last_seen = max(t['timestamp'] for t in wallet_trades)
            total_notional += wl_notional
            traders.append({
                'wallet': wallet,
                'label': wallet[:6] + '…' + wallet[-4:],
                'displayName': wallet_trades[0]['displayName'],
                'notional': round(wl_notional, 2),
                'lastSeen': wl_last_seen,
            })

        avg_price = sum(t['price'] for t in group['trades']) / max(len(group['trades']), 1)
        last_seen = max(t['timestamp'] for t in group['trades'])
        market_data = market_by_slug.get(group['marketSlug'], {})
        category = market_data.get('category', 'other')
        title = market_data.get('title', '') or market_data.get('question', '')
        hours_to_close = market_data.get('hoursToClose', 72)

        signal_id = f"{group['marketSlug']}-{group['side']}-{group['outcome']}"

        # Edge estimate: abs(market_price - avg_trade_price) / market_price
        market_price = market_data.get('yesPrice', avg_price)
        edge_estimate = abs(market_price - avg_price) / max(market_price, 0.001)

        opportunity_score = min(100, round(
            (market_data.get('opportunityScore', 50) or 50) * 0.55
            + whale_count * 9
            + min(total_notional / 2500, 18)
        ))

        signals.append({
            'id': signal_id,
            'marketId': market_data.get('id', group['marketSlug']),
            'marketSlug': group['marketSlug'],
            'category': category,
            'title': title,
            'side': group['side'],
            'outcome': group['outcome'],
            'whaleCount': whale_count,
            'totalNotional': round(total_notional, 2),
            'avgPrice': round(avg_price, 3),
            'lastSeen': last_seen,
            'hoursToClose': hours_to_close,
            'opportunityScore': opportunity_score,
            'agreementScore': compute_agreement_score(
                whale_count, edge_estimate, total_notional),
            'weightedEdge': round(edge_estimate, 4),
            'traders': traders,
        })

    return signals


def identify_weather_signals(signals: List[dict]) -> List[dict]:
    """
    Filter consensus signals to weather-relevant ones and map to Kalshi.

    For each signal, parse the market title to extract Kalshi station/bucket.
    Returns enriched signal dicts with Kalshi mapping and conviction multiplier.
    """
    weather_signals = []

    for signal in signals:
        title = signal.get('title', '')
        side = signal.get('side', 'yes')

        # Parse weather market title
        parsed = parse_weather_market_title(title)
        if parsed is None:
            continue

        # Map Polymarket side to signal direction
        # YES on "exceed 85°F" → UP, NO on "exceed 85°F" → DOWN
        if side == 'yes':
            signal_direction = 'UP' if parsed['direction'] == 'UP' else 'DOWN'
        else:
            signal_direction = 'DOWN' if parsed['direction'] == 'UP' else 'UP'

        total_notional = signal.get('totalNotional', 0)
        if total_notional < MIN_NOTIONAL:
            continue

        agreement_score = signal.get('agreementScore', 0.5)
        conviction = compute_conviction_multiplier(
            agreement_score, signal_direction)

        weather_signals.append({
            **signal,
            'kalshi_station': parsed['station'],
            'kalshi_bucket': parsed['bucket'],
            'kalshi_series': parsed['series'],
            'signal_direction': signal_direction,
            'conviction_multiplier': round(conviction, 3),
        })

    return weather_signals


# ── Main Feed Processing ─────────────────────────────────────────────────

def process_polymarket_feed(db_path: Optional[str] = None) -> dict:
    """
    Main feed processing entry point.

    1. Fetches trades and markets from Polymarket API
    2. Extracts consensus signals
    3. Identifies weather-relevant signals with Kalshi mapping
    4. Persists to DB
    5. Returns summary

    Args:
        db_path: Path to Polymarket WhaleWatch DB. Defaults to PM_WHALE_DB.

    Returns:
        dict with summary stats
    """
    from core.polymarket_whale_db import (
        init_db, store_consensus_signals,
        insert_signal_feed_entry, expire_stale_signals, get_stats
    )

    # Initialize DB
    if db_path is None:
        from core.polymarket_whale_db import PM_WHALE_DB
        db_path = PM_WHALE_DB
    conn = init_db(db_path=db_path)

    summary = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'trades_fetched': 0,
        'markets_fetched': 0,
        'consensus_signals': 0,
        'weather_signals_mapped': 0,
        'signals_persisted': 0,
        'errors': [],
    }

    try:
        # Step 1: Fetch data
        trades = fetch_recent_trades()
        summary['trades_fetched'] = len(trades)

        markets = fetch_markets()
        summary['markets_fetched'] = len(markets)

        if not trades:
            summary['errors'].append("No trades fetched from API")
            conn.close()
            return summary

        # Step 2: Extract consensus signals
        consensus = extract_consensus_signals(trades, markets)
        summary['consensus_signals'] = len(consensus)

        # Step 3: Persist consensus signals
        if consensus:
            store_consensus_signals(conn, consensus)

        # Step 4: Identify weather signals
        weather = identify_weather_signals(consensus)
        summary['weather_signals_mapped'] = len(weather)

        # Step 5: Persist to signals feed
        now_iso = datetime.now(timezone.utc).isoformat()
        for ws in weather:
            entry = {
                'signalId': ws.get('id', ''),
                'marketId': ws.get('marketId', ''),
                'timestamp': now_iso,
                'kalshi_station': ws['kalshi_station'],
                'kalshi_bucket': ws['kalshi_bucket'],
                'kalshi_series': ws.get('kalshi_series', 'HIGH'),
                'signal_direction': ws['signal_direction'],
                'conviction_multiplier': ws['conviction_multiplier'],
                'agreement_score': ws.get('agreementScore', 0),
                'whale_count': ws.get('whaleCount', 0),
                'total_notional': ws.get('totalNotional', 0),
                'status': 'PENDING',
                'ttl_hours': 24,
            }
            insert_signal_feed_entry(conn, entry)

        summary['signals_persisted'] = len(weather)

        # Step 6: Expire stale signals (older than their TTL)
        expired = expire_stale_signals(conn)
        summary['signals_expired'] = expired

        # Summary stats
        stats = get_stats(conn)
        summary['db_stats'] = stats

    except Exception as e:
        logger.exception("Error processing Polymarket feed")
        summary['errors'].append(str(e))
    finally:
        conn.close()

    return summary


# ── Integration Helper (for weather engine consumption) ──────────────────

def get_polymarket_conviction(station: str, bucket: int,
                              db_path: Optional[str] = None) -> float:
    """
    Get the current Polymarket conviction multiplier for a Kalshi market.

    Called by the weather engine at Layer 3 (Bet Sizing) to modulate
    the Kelly fraction based on Polymarket whale activity.

    Args:
        station: Kalshi station code (e.g., 'KNYC')
        bucket: Temperature bucket (e.g., 85)
        db_path: Optional DB path override

    Returns:
        conviction_multiplier (1.0 if no active signal)
    """
    from core.polymarket_whale_db import get_db, get_polymarket_conviction as db_get

    if db_path is None:
        from core.polymarket_whale_db import PM_WHALE_DB
        db_path = PM_WHALE_DB

    conn = get_db(db_path)
    try:
        return db_get(conn, station, bucket)
    finally:
        conn.close()


def get_all_active_convictions(db_path: Optional[str] = None) -> List[dict]:
    """Get all active Polymarket convictions for all Kalshi stations."""
    from core.polymarket_whale_db import get_db, get_active_signals

    if db_path is None:
        from core.polymarket_whale_db import PM_WHALE_DB
        db_path = PM_WHALE_DB

    conn = get_db(db_path)
    try:
        signals = get_active_signals(conn, min_whales=CONSENSUS_MIN_WALLETS)
        results = []
        for s in signals:
            results.append({
                'station': s['kalshi_station'],
                'bucket': s['kalshi_bucket'],
                'series': s.get('kalshi_series', 'HIGH'),
                'direction': s['signal_direction'],
                'multiplier': s['conviction_multiplier'],
                'agreement': s.get('agreementScore', 0),
                'whales': s.get('whale_count', 0),
            })
        return results
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # Quick test: parse sample titles
    test_titles = [
        "Will the temperature in New York exceed 85°F on August 15?",
        "Will the temperature in NYC be below 32°F on January 10?",
        "Will the high temperature in Chicago be above 90°F on July 4?",
    ]
    print("=== Title Parsing Test ===")
    for title in test_titles:
        result = parse_weather_market_title(title)
        if result:
            print(f"  '{title[:50]}...' → {result}")
        else:
            print(f"  '{title[:50]}...' → UNPARSEABLE")

    print("\n=== Conviction Multiplier Test ===")
    tests = [
        (0.85, 'UP', 'UP', "Match high agreement"),
        (0.45, 'UP', 'UP', "Match low agreement"),
        (0.75, 'UP', 'DOWN', "Contradiction high agreement"),
        (0.30, 'DOWN', 'UP', "Contradiction low agreement"),
    ]
    for score, sig_dir, ens_dir, label in tests:
        mult = compute_conviction_multiplier(score, sig_dir, ens_dir)
        print(f"  {label}: agree={score}, sig={sig_dir}, ens={ens_dir} → mult={mult:.3f}")

    print("\n=== Full Feed Test ===")
    result = process_polymarket_feed()
    print(json.dumps(result, indent=2, default=str))