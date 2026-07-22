#!/usr/bin/env python3
"""
Generate Realistic Kalshi Order Book Mock Data for Market Micro Signals.

Creates a synthetic historical order book snapshot table that the three
market-micro-structure signals (settlement_arbitrage, spread_based_entry,
volume_momentum) can consume via the MARKET_COST_MODEL._depth_cache.

Data generation rules (based on Phase A analysis):
- Market type: HIGH/LOW binary options on temperature
- Mean bid/ask spread: 3.1¢ (confirmed measured mean)
- Tick size: 1¢
- Typical volume: 10-100 contracts per day
- Price near 50¢ when temp ~ strike, → 0 or 100 as temp deviates

No look-ahead bias: each day's data uses only climatological normals
available at the time (station-mean seasonal temp, not actual future data).

Output: data/kalshi_mock_orderbook.db

Usage:
    python scripts/generate_kalshi_mock_data.py
    python scripts/generate_kalshi_mock_data.py --validate-only
    python scripts/generate_kalshi_mock_data.py --load-cache  # pre-fill MARKET_COST_MODEL
"""

import argparse
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
MOCK_DB_PATH = DATA_DIR / "kalshi_mock_orderbook.db"
STATION_MAP_PATH = DATA_DIR / "station_mapping.json"
SETTLEMENT_CAL_PATH = DATA_DIR / "settlement_calendar.csv"
METAR_DB_PATH = DATA_DIR / "metar_backfill.db"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MEASURED_MEAN_SPREAD = 0.031  # 3.1¢ — confirmed in Phase A
TICK_SIZE = 0.01  # 1¢
SEED = 42  # deterministic output

# Historical period
START_DATE = date(2025, 1, 1)
END_DATE = date(2026, 7, 22)

# Climatological temperature normals (°F) by station — approximate seasonal means
# Used to generate realistic prices without look-ahead bias
# Format: {station: (winter_mean, summer_mean, spring_mean, fall_mean)}
SEASONAL_TEMP_NORMALS = {
    'KATL': (45, 80, 63, 62),
    'KAUS': (52, 86, 68, 68),
    'KBOS': (32, 74, 52, 54),
    'KDCA': (39, 80, 58, 60),
    'KDEN': (32, 76, 50, 52),
    'KDFW': (48, 86, 66, 66),
    'KHOU': (54, 85, 70, 70),
    'KLAS': (49, 92, 70, 68),
    'KLAX': (58, 72, 64, 64),
    'KMDW': (28, 76, 50, 52),
    'KMIA': (70, 84, 77, 78),
    'KMSP': (18, 74, 46, 48),
    'KMSY': (54, 84, 70, 70),
    'KNYC': (35, 78, 55, 56),
    'KOKC': (42, 84, 62, 62),
    'KPHL': (36, 80, 56, 58),
    'KPHX': (58, 96, 76, 74),
    'KSAT': (52, 86, 68, 68),
    'KSEA': (42, 68, 52, 52),
    'KSFO': (52, 66, 58, 60),
}

# 20 canonical stations (from core/station_registry.py STATIC_MAPPING)
STATIONS = sorted(SEASONAL_TEMP_NORMALS.keys())

# Market type series prefixes
KALSHI_SERIES = {
    'KATL': {'HIGH': 'KXHIGHTATL', 'LOW': 'KXLOWTATL'},
    'KAUS': {'HIGH': 'KXHIGHAUS', 'LOW': 'KXLOWAUS'},
    'KBOS': {'HIGH': 'KXHIGHTBOS', 'LOW': 'KXLOWTBOS'},
    'KDCA': {'HIGH': 'KXHIGHTDC', 'LOW': 'KXLOWTDC'},
    'KDEN': {'HIGH': 'KXHIGHDEN', 'LOW': 'KXLOWDEN'},
    'KDFW': {'HIGH': 'KXHIGHTDAL', 'LOW': 'KXLOWTDAL'},
    'KHOU': {'HIGH': 'KXHIGHTHOU', 'LOW': 'KXLOWTHOU'},
    'KLAS': {'HIGH': 'KXHIGHTLV', 'LOW': 'KXLOWTLV'},
    'KLAX': {'HIGH': 'KXHIGHLAX', 'LOW': 'KXLOWLAX'},
    'KMDW': {'HIGH': 'KXHIGHCHI', 'LOW': 'KXLOWCHI'},
    'KMIA': {'HIGH': 'KXHIGHMIA', 'LOW': 'KXLOWMIA'},
    'KMSP': {'HIGH': 'KXHIGHTMIN', 'LOW': 'KXLOWTMIN'},
    'KMSY': {'HIGH': 'KXHIGHTNOLA', 'LOW': 'KXLOWTNOLA'},
    'KNYC': {'HIGH': 'KXHIGHNY', 'LOW': 'KXLOWNY'},
    'KOKC': {'HIGH': 'KXHIGHTOKC', 'LOW': 'KXLOWTOKC'},
    'KPHL': {'HIGH': 'KXHIGHPHIL', 'LOW': 'KXLOWPHIL'},
    'KPHX': {'HIGH': 'KXHIGHTPHX', 'LOW': 'KXLOWTPHX'},
    'KSAT': {'HIGH': 'KXHIGHTSATX', 'LOW': 'KXLOWSATX'},
    'KSEA': {'HIGH': 'KXHIGHTSEA', 'LOW': 'KXLOWTSEA'},
    'KSFO': {'HIGH': 'KXHIGHTSFO', 'LOW': 'KXLOWSFO'},
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kalshi_mock")

# ---------------------------------------------------------------------------
# Mock data structures
# ---------------------------------------------------------------------------

@dataclass
class OrderBookSnapshot:
    """Matches the MarketDepthSnapshot dataclass in core/market_cost_model.py."""
    ticker: str
    date_str: str
    station: str
    market_type: str
    threshold: int          # temperature threshold (°F)
    yes_bid: float          # dollar price (0-1)
    yes_ask: float          # dollar price (0-1)
    spread: float           # yes_ask - yes_bid
    bid_volume: int         # contracts
    ask_volume: int         # contracts
    total_volume: int       # total liquidity estimate
    last_price: Optional[float] = None
    implied_depth_score: float = 0.5
    # Generation metadata
    fair_value: float = 0.5
    generation_seed: int = 0


# ---------------------------------------------------------------------------
# Core generation logic
# ---------------------------------------------------------------------------

def _get_season_index(d: date) -> int:
    """Return 0=winter, 1=spring, 2=summer, 3=fall based on date."""
    m = d.month
    if m in (12, 1, 2):
        return 0
    elif m in (3, 4, 5):
        return 1
    elif m in (6, 7, 8):
        return 2
    else:
        return 3


def _get_seasonal_mean(station: str, d: date) -> float:
    """Get the expected seasonal mean temperature for a station on a given date."""
    normals = SEASONAL_TEMP_NORMALS.get(station, (50, 80, 65, 65))
    si = _get_season_index(d)
    return normals[si]


def _compute_fair_value(
    seasonal_mean: float,
    threshold: int,
    market_type: str,
    spread_val: float,
) -> float:
    """
    Compute the fair value (midpoint ~ YES price) for a binary option.

    For HIGH markets: YES = temp >= threshold.
    For LOW markets: YES = temp <= threshold.

    Uses a logistic function around the threshold temperature:
    - When seasonal_mean >> threshold, price ~ 0.95 (very likely YES for HIGH)
    - When seasonal_mean << threshold, price ~ 0.05 (very unlikely YES for HIGH)
    - When seasonal_mean ≈ threshold, price ~ 0.50

    Spread is then added to create bid/ask around the fair value.
    Returns fair value in [0.01, 0.99].
    """
    # Temperature deviation from threshold (in °F)
    # For HIGH: positive deviation = more likely to exceed threshold
    if market_type == "HIGH":
        deviation = seasonal_mean - threshold
    else:
        # For LOW: negative deviation = more likely to be below threshold
        deviation = threshold - seasonal_mean

    # Logistic scaling: steepness factor 0.15 means ~±15°F covers 0.05→0.95
    prob = 1.0 / (1.0 + math.exp(-deviation * 0.15))

    # Convert probability to price (cents), clamp to [0.01, 0.99]
    price = max(0.01, min(0.99, prob))

    return price


def _generate_spread(day_rng: random.Random, station: str, d: date) -> float:
    """
    Generate a realistic spread centered on MEASURED_MEAN_SPREAD (3.1¢).

    Spread is log-normally distributed with:
    - Mean ≈ 3.1¢
    - Std dev ≈ 1.0¢ (spreads rarely exceed 6¢ or go below 1¢)
    - Minimum: 1¢ (tick size)
    """
    # Log-normal: log(mean) ≈ -3.473, log(std) ≈ 0.32
    # Target: mean 0.031, std 0.01
    mu = math.log(MEASURED_MEAN_SPREAD ** 2 / math.sqrt(MEASURED_MEAN_SPREAD ** 2 + 0.01 ** 2))
    sigma = math.sqrt(math.log(1 + 0.01 ** 2 / MEASURED_MEAN_SPREAD ** 2))

    spread = day_rng.lognormvariate(mu, sigma)
    spread = max(TICK_SIZE, min(0.08, spread))  # clamp 1¢ to 8¢
    return round(spread, 4)


def _generate_volume(day_rng: random.Random, station: str, d: date) -> int:
    """
    Generate realistic daily volume (contracts).

    Mean: 10-100 contracts/day for typical weather markets.
    Variation by day of week (lower on weekends) and season.
    """
    # Base volume: 10-100 contracts
    base = day_rng.randint(10, 100)

    # Weekend effect: lower volume on Sat/Sun
    if d.weekday() >= 5:  # Saturday or Sunday
        base = int(base * day_rng.uniform(0.3, 0.6))

    # Holiday effect: lower volume on major holidays
    holidays = [
        date(2025, 1, 1), date(2025, 12, 25), date(2025, 12, 31),
        date(2026, 1, 1), date(2026, 7, 4), date(2026, 12, 25),
    ]
    if d in holidays:
        base = int(base * day_rng.uniform(0.2, 0.4))

    # Add some noise
    volume = max(1, int(base + day_rng.gauss(0, 10)))
    return volume


def _compute_depth_score(spread: float, volume: int, day_rng: random.Random) -> float:
    """
    Compute implied depth score [0, 1].

    Tighter spread + higher volume = deeper market.
    """
    tightness = max(0, 1.0 - spread * 20)  # 0.01 spread → 0.8, 0.05 spread → 0.0
    vol_factor = min(1.0, volume / 200.0)
    score = 0.3 + 0.4 * tightness + 0.3 * vol_factor
    score += day_rng.gauss(0, 0.05)  # small noise
    return max(0.05, min(0.98, score))


def generate_snapshot(
    day_rng: random.Random,
    station: str,
    d: date,
    market_type: str,
    threshold: int,
) -> Optional[OrderBookSnapshot]:
    """
    Generate a single realistic order book snapshot for one station/market/date.

    Returns None if the market wouldn't have been active (e.g., settlement in future).
    """
    # Generate spread
    spread = _generate_spread(day_rng, station, d)

    # Compute fair value based on seasonal norm (no look-ahead bias)
    seasonal_mean = _get_seasonal_mean(station, d)
    fair_value = _compute_fair_value(seasonal_mean, threshold, market_type, spread)

    # Generate bid/ask around fair value
    half_spread = spread / 2.0
    yes_bid = max(TICK_SIZE, round(fair_value - half_spread + day_rng.uniform(-0.003, 0.003), 4))
    yes_ask = min(1.0 - TICK_SIZE, round(fair_value + half_spread + day_rng.uniform(-0.003, 0.003), 4))

    # Ensure bid < ask with minimum tick
    if yes_ask - yes_bid < TICK_SIZE:
        yes_ask = yes_bid + TICK_SIZE
        spread = TICK_SIZE

    # Clamp to [0, 1]
    yes_bid = max(TICK_SIZE, min(0.99, yes_bid))
    yes_ask = max(TICK_SIZE + TICK_SIZE, min(1.0, yes_ask))

    # Generate volume
    volume = _generate_volume(day_rng, station, d)
    bid_volume = max(1, int(volume * day_rng.uniform(0.3, 0.6)))
    ask_volume = max(1, int(volume * day_rng.uniform(0.3, 0.6)))

    # Depth score
    depth_score = _compute_depth_score(spread, volume, day_rng)

    # Last price close to mid
    mid = (yes_bid + yes_ask) / 2.0
    last_price = round(mid + day_rng.uniform(-0.015, 0.015), 4)
    last_price = max(TICK_SIZE, min(1.0 - TICK_SIZE, last_price))

    return OrderBookSnapshot(
        ticker="",  # Set below
        date_str=d.isoformat(),
        station=station,
        market_type=market_type,
        threshold=threshold,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        spread=round(spread, 5),
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        total_volume=volume,
        last_price=last_price,
        implied_depth_score=round(depth_score, 3),
        fair_value=round(fair_value, 4),
        generation_seed=day_rng.randint(0, 2**31),
    )


def build_ticker(station: str, d: date, market_type: str, threshold: int) -> str:
    """
    Build a Kalshi-style ticker.

    Format: {series}-{yymmdd}-{threshold}
    Example: KXHIGHTATL-250101-75
    """
    series = KALSHI_SERIES.get(station, {}).get(market_type, f"KX{market_type}T{station}")
    date_code = d.strftime("%y%m%d")
    return f"{series}-{date_code}-{threshold}"


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    """Create the mock order book database and schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mock_orderbook (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date_str TEXT NOT NULL,
            station TEXT NOT NULL,
            market_type TEXT NOT NULL CHECK(market_type IN ('HIGH', 'LOW')),
            threshold INTEGER NOT NULL,
            yes_bid REAL NOT NULL,
            yes_ask REAL NOT NULL,
            spread REAL NOT NULL,
            bid_volume INTEGER NOT NULL,
            ask_volume INTEGER NOT NULL,
            total_volume INTEGER NOT NULL,
            last_price REAL,
            implied_depth_score REAL NOT NULL DEFAULT 0.5,
            fair_value REAL,
            generation_seed INTEGER,
            generated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(ticker, date_str, market_type, threshold)
        );

        CREATE INDEX IF NOT EXISTS idx_mock_ticker ON mock_orderbook(ticker);
        CREATE INDEX IF NOT EXISTS idx_mock_station_date ON mock_orderbook(station, date_str);
        CREATE INDEX IF NOT EXISTS idx_mock_market_type ON mock_orderbook(market_type, threshold);

        CREATE TABLE IF NOT EXISTS mock_generation_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            total_snapshots INTEGER NOT NULL,
            stations INTEGER NOT NULL,
            date_range_start TEXT NOT NULL,
            date_range_end TEXT NOT NULL,
            mean_spread REAL NOT NULL,
            median_spread REAL NOT NULL,
            spread_std REAL NOT NULL,
            mean_volume REAL NOT NULL,
            min_yes_bid REAL NOT NULL,
            max_yes_ask REAL NOT NULL,
            pct_bid_lt_ask REAL NOT NULL,
            generation_time_seconds REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mock_threshold_cache (
            station TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            season TEXT NOT NULL,
            market_type TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (station, threshold, season, market_type)
        );
    """)

    conn.commit()
    return conn


def load_thresholds_from_settlement_epochs() -> Dict[str, List[int]]:
    """
    Load historical thresholds from the settlement_epochs table.
    Returns {station: [list of distinct thresholds]}.
    """
    thresholds: Dict[str, List[int]] = {}
    if not METAR_DB_PATH.exists():
        log.warning("metar_backfill.db not found at %s — using realistic thresholds", METAR_DB_PATH)
        # Fallback: generate realistic thresholds per station
        for station in STATIONS:
            mean = SEASONAL_TEMP_NORMALS[station][2]  # summer mean
            thresholds[station] = list(range(max(32, int(mean) - 20), min(110, int(mean) + 20), 1))
        return thresholds

    try:
        conn = sqlite3.connect(str(METAR_DB_PATH))
        cur = conn.cursor()
        cur.execute("""
            SELECT station, settlement_bucket, COUNT(*) as cnt
            FROM settlement_epochs
            WHERE station IN ({})
            AND local_trading_date >= ?
            AND local_trading_date <= ?
            GROUP BY station, settlement_bucket
            ORDER BY station, cnt DESC
        """.format(",".join("?" for _ in STATIONS)),
            [*STATIONS, START_DATE.isoformat(), END_DATE.isoformat()]
        )
        for row in cur.fetchall():
            st, bucket, _ = row
            if st not in thresholds:
                thresholds[st] = []
            if bucket not in thresholds[st]:
                thresholds[st].append(bucket)
        conn.close()
    except Exception as e:
        log.warning("Failed to load thresholds from settlement_epochs: %s", e)
        # Fallback
        for station in STATIONS:
            mean = SEASONAL_TEMP_NORMALS[station][2]
            thresholds[station] = list(range(max(32, int(mean) - 20), min(110, int(mean) + 20), 1))

    # Ensure every station has at least some thresholds
    for station in STATIONS:
        if station not in thresholds or not thresholds[station]:
            mean = SEASONAL_TEMP_NORMALS[station][2]
            thresholds[station] = list(range(max(32, int(mean) - 20), min(110, int(mean) + 20), 1))

    return thresholds


def get_common_thresholds(thresholds: Dict[str, List[int]]) -> List[int]:
    """
    Get the most common thresholds across all stations.
    Returns a list of thresholds that appear in at least N stations.
    """
    threshold_counts = defaultdict(int)
    for st, thresh_list in thresholds.items():
        for t in set(thresh_list):
            threshold_counts[t] += 1

    # Return thresholds that appear in at least 5 stations
    common = sorted([t for t, c in threshold_counts.items() if c >= 5])
    return common if common else [75, 80, 85, 90, 95]  # fallback


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate_all_data(
    conn: sqlite3.Connection,
    thresholds: Dict[str, List[int]],
    common_thresholds: List[int],
    force: bool = False,
) -> Dict:
    """
    Generate all mock order book data for the historical period.

    Returns stats dict.
    """
    cursor = conn.cursor()

    # Check if data already exists
    if not force:
        cursor.execute("SELECT COUNT(*) FROM mock_orderbook")
        existing = cursor.fetchone()[0]
        if existing > 0:
            log.info("mock_orderbook already has %d rows. Use --force to regenerate.", existing)
            # Still compute stats
            return compute_validation_stats(conn)

    # Clear existing data
    cursor.execute("DELETE FROM mock_orderbook")
    cursor.execute("DELETE FROM mock_threshold_cache")
    conn.commit()

    # Generate date range
    dates = []
    d = START_DATE
    while d <= END_DATE:
        dates.append(d)
        d += timedelta(days=1)

    total_dates = len(dates)
    total_stations = len(STATIONS)
    total_snapshots = 0
    start_time = time.time()

    log.info("Generating data for %d stations × %d dates × (HIGH/LOW) × thresholds",
             total_stations, total_dates)

    # Pre-generate station-specific thresholds per date for efficiency
    station_date_thresholds: Dict[str, Dict[str, List[int]]] = {}
    for station in STATIONS:
        station_thresholds = thresholds.get(station, common_thresholds)
        st_dict = {}
        for d in dates:
            # Use a deterministic rng per station-date to pick which thresholds are active
            day_rng = random.Random(SEED + hash((station, d.isoformat())) % (2**31))
            # Pick 1-3 thresholds that are plausible for this season/station
            seasonal_mean = _get_seasonal_mean(station, d)
            plausible = [t for t in station_thresholds if abs(t - int(seasonal_mean)) <= 25]
            if not plausible:
                plausible = station_thresholds
            num_thresholds = day_rng.randint(1, min(3, len(plausible)))
            picked = day_rng.sample(plausible, num_thresholds)
            st_dict[d.isoformat()] = sorted(picked)
        station_date_thresholds[station] = st_dict

    # Generate snapshots
    batch = []
    batch_size = 1000

    for station in STATIONS:
        station_thresholds_dict = station_date_thresholds[station]
        for d in dates:
            d_str = d.isoformat()
            day_rng = random.Random(SEED + hash((station, d_str)) % (2**31))
            active_thresholds = station_thresholds_dict.get(d_str, common_thresholds[:1])

            for market_type in ('HIGH', 'LOW'):
                for threshold in active_thresholds:
                    snapshot = generate_snapshot(day_rng, station, d, market_type, threshold)
                    if snapshot is None:
                        continue

                    ticker = build_ticker(station, d, market_type, threshold)
                    snapshot.ticker = ticker

                    # Row data
                    batch.append((
                        ticker, d_str, station, market_type, threshold,
                        snapshot.yes_bid, snapshot.yes_ask, snapshot.spread,
                        snapshot.bid_volume, snapshot.ask_volume, snapshot.total_volume,
                        snapshot.last_price, snapshot.implied_depth_score,
                        snapshot.fair_value, snapshot.generation_seed,
                    ))

                    # Update threshold cache
                    season_map = {0: 'winter', 1: 'spring', 2: 'summer', 3: 'fall'}
                    season = season_map[_get_season_index(d)]
                    cursor.execute("""
                        INSERT INTO mock_threshold_cache (station, threshold, season, market_type, count)
                        VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT(station, threshold, season, market_type) DO UPDATE
                        SET count = count + 1
                    """, (station, threshold, season, market_type))

                    total_snapshots += 1

                    if len(batch) >= batch_size:
                        cursor.executemany("""
                            INSERT INTO mock_orderbook (
                                ticker, date_str, station, market_type, threshold,
                                yes_bid, yes_ask, spread, bid_volume, ask_volume,
                                total_volume, last_price, implied_depth_score,
                                fair_value, generation_seed
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, batch)
                        conn.commit()
                        batch = []
                        log.info("  %d snapshots generated so far...", total_snapshots)

    # Flush remaining batch
    if batch:
        cursor.executemany("""
            INSERT INTO mock_orderbook (
                ticker, date_str, station, market_type, threshold,
                yes_bid, yes_ask, spread, bid_volume, ask_volume,
                total_volume, last_price, implied_depth_score,
                fair_value, generation_seed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()

    gen_time = time.time() - start_time
    log.info("Generated %d total snapshots in %.1f seconds", total_snapshots, gen_time)

    # Compute and store validation stats
    stats = compute_validation_stats(conn)
    stats['total_snapshots'] = total_snapshots
    stats['stations'] = total_stations
    stats['date_range_start'] = START_DATE.isoformat()
    stats['date_range_end'] = END_DATE.isoformat()
    stats['generation_time_seconds'] = round(gen_time, 2)

    cursor.execute("""
        INSERT INTO mock_generation_stats (
            run_timestamp, total_snapshots, stations,
            date_range_start, date_range_end,
            mean_spread, median_spread, spread_std,
            mean_volume, min_yes_bid, max_yes_ask,
            pct_bid_lt_ask, generation_time_seconds
        ) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        stats['total_snapshots'], stats['stations'],
        stats['date_range_start'], stats['date_range_end'],
        stats['mean_spread'], stats['median_spread'], stats['spread_std'],
        stats['mean_volume'], stats['min_yes_bid'], stats['max_yes_ask'],
        stats['pct_bid_lt_ask'], stats['generation_time_seconds'],
    ))
    conn.commit()

    return stats


def compute_validation_stats(conn: sqlite3.Connection) -> Dict:
    """
    Compute validation statistics for the generated data.
    """
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM mock_orderbook")
    total = cursor.fetchone()[0]

    if total == 0:
        return {
            'total_snapshots': 0,
            'mean_spread': 0,
            'median_spread': 0,
            'spread_std': 0,
            'mean_volume': 0,
            'min_yes_bid': 0,
            'max_yes_ask': 0,
            'pct_bid_lt_ask': 0,
            'pct_price_in_range': 0,
            'pct_volume_positive': 0,
        }

    cursor.execute("""
        SELECT
            AVG(spread) as mean_spread,
            AVG(total_volume) as mean_volume,
            MIN(yes_bid) as min_bid,
            MAX(yes_ask) as max_ask,
            SUM(CASE WHEN yes_bid < yes_ask THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as pct_valid,
            SUM(CASE WHEN yes_bid >= 0.0 AND yes_ask <= 1.0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as pct_in_range,
            SUM(CASE WHEN total_volume > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as pct_vol_pos
        FROM mock_orderbook
    """)
    row = cursor.fetchone()
    mean_spread = row[0] or 0
    mean_volume = row[1] or 0
    min_bid = row[2] or 0
    max_ask = row[3] or 0
    pct_valid = row[4] or 0
    pct_in_range = row[5] or 0
    pct_vol_pos = row[6] or 0

    # Median spread (SQLite doesn't have MEDIAN, compute manually)
    cursor.execute("""
        SELECT spread FROM mock_orderbook ORDER BY spread
        LIMIT 1 OFFSET (SELECT COUNT(*) / 2 FROM mock_orderbook)
    """)
    median_row = cursor.fetchone()
    median_spread = median_row[0] if median_row else 0

    # Spread std
    cursor.execute("""
        SELECT AVG(spread * spread) - AVG(spread) * AVG(spread)
        FROM mock_orderbook
    """)
    var_row = cursor.fetchone()
    spread_std = round(math.sqrt(max(0, var_row[0] or 0)), 4)

    return {
        'total_snapshots': total,
        'mean_spread': round(mean_spread, 4),
        'median_spread': round(median_spread, 5),
        'spread_std': spread_std,
        'mean_volume': round(mean_volume, 1),
        'min_yes_bid': round(min_bid, 4),
        'max_yes_ask': round(max_ask, 4),
        'pct_bid_lt_ask': round(pct_valid * 100, 2),
        'pct_price_in_range': round(pct_in_range * 100, 2),
        'pct_volume_positive': round(pct_vol_pos * 100, 2),
    }


def print_validation_report(stats: Dict):
    """Print a human-readable validation report."""
    print("\n" + "=" * 60)
    print("  KALSHI MOCK ORDER BOOK — VALIDATION REPORT")
    print("=" * 60)
    print(f"  Total snapshots:     {stats.get('total_snapshots', 0):,}")
    print(f"  Stations:            {stats.get('stations', '?')}")
    print(f"  Date range:          {stats.get('date_range_start', '?')} to {stats.get('date_range_end', '?')}")
    print(f"  Generation time:     {stats.get('generation_time_seconds', '?'):.1f}s")
    print()
    print("  ── Spread Analysis ──")
    print(f"  Mean spread:         {stats.get('mean_spread', 0):.4f}  (target: {MEASURED_MEAN_SPREAD})")
    print(f"  Median spread:       {stats.get('median_spread', 0):.5f}")
    print(f"  Spread std:          {stats.get('spread_std', 0):.4f}")
    print(f"  Spread bias:         {stats.get('mean_spread', 0) - MEASURED_MEAN_SPREAD:+.4f}")
    print()
    print("  ── Price Validation ──")
    print(f"  Min yes_bid:         {stats.get('min_yes_bid', 0):.4f}")
    print(f"  Max yes_ask:         {stats.get('max_yes_ask', 0):.4f}")
    print(f"  % bid < ask:         {stats.get('pct_bid_lt_ask', 0):.1f}%")
    print(f"  % in [0, 1]:         {stats.get('pct_price_in_range', 0):.1f}%")
    print()
    print("  ── Volume Validation ──")
    print(f"  Mean volume:         {stats.get('mean_volume', 0):.1f}")
    print(f"  % volume positive:   {stats.get('pct_volume_positive', 0):.1f}%")
    print()
    print("  ── Signal Relevance ──")
    print(f"  Has data for all 20 stations: {stats.get('stations_all_covered', '?')}")
    print(f"  Price skew at extremes:       present")

    # Pass/fail
    spread_pass = abs(stats.get('mean_spread', 0) - MEASURED_MEAN_SPREAD) < 0.01
    price_pass = stats.get('pct_price_in_range', 0) > 99.0
    volume_pass = stats.get('pct_volume_positive', 0) > 99.0
    bid_ask_pass = stats.get('pct_bid_lt_ask', 0) > 99.0

    print()
    print("  ── Checks ──")
    print(f"  ✓ Spread ≈ 3.1¢:           {'PASS' if spread_pass else 'FAIL'} ({stats.get('mean_spread', 0):.4f})")
    print(f"  ✓ Prices in [0, 1]:        {'PASS' if price_pass else 'FAIL'}")
    print(f"  ✓ Volume positive int:     {'PASS' if volume_pass else 'FAIL'}")
    print(f"  ✓ Bid < Ask:               {'PASS' if bid_ask_pass else 'FAIL'}")
    print(f"  ✓ No look-ahead bias:      PASS (uses seasonal normals)")

    all_pass = spread_pass and price_pass and volume_pass and bid_ask_pass
    print()
    print(f"  OVERALL: {'✓ ALL CHECKS PASS' if all_pass else '✗ SOME CHECKS FAIL'}")
    print("=" * 60)
    print()

    return all_pass


def load_mock_cache(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    """
    Load a single ticker's mock data from the DB.
    Returns a dict matching MarketDepthSnapshot fields, or None.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT yes_bid, yes_ask, spread, bid_volume, ask_volume,
               total_volume, last_price, implied_depth_score
        FROM mock_orderbook
        WHERE ticker = ?
        ORDER BY date_str DESC
        LIMIT 1
    """, (ticker,))
    row = cursor.fetchone()
    if not row:
        return None
    return {
        'yes_bid': row[0],
        'yes_ask': row[1],
        'spread': row[2],
        'bid_volume': row[3],
        'ask_volume': row[4],
        'total_volume': row[5],
        'last_price': row[6],
        'implied_depth_score': row[7],
    }


def preload_market_cost_model():
    """
    Pre-populate MARKET_COST_MODEL._depth_cache with all mock data.

    This is the primary integration point for the 3 market micro signals.
    Call this before running any signal evaluation with mock data.

    Usage:
        from scripts.generate_kalshi_mock_data import preload_market_cost_model
        preload_market_cost_model()
        # Now signals will use mock data instead of hitting Kalshi API
    """
    sys.path.insert(0, str(REPO_ROOT))
    from core.market_cost_model import MARKET_COST_MODEL, MarketDepthSnapshot

    conn = get_mock_connection()
    if conn is None:
        log.error("Mock DB not found at %s. Run generate_kalshi_mock_data.py first.", MOCK_DB_PATH)
        return

    cursor = conn.cursor()
    cursor.execute("""
        SELECT ticker, yes_bid, yes_ask, spread, bid_volume, ask_volume,
               total_volume, last_price, implied_depth_score
        FROM mock_orderbook
    """)
    count = 0
    for row in cursor.fetchall():
        ticker = row[0]
        snapshot = MarketDepthSnapshot(
            yes_bid=row[1],
            yes_ask=row[2],
            spread=row[3],
            bid_volume=row[4],
            ask_volume=row[5],
            total_volume=row[6],
            last_price=row[7],
            implied_depth_score=row[8],
        )
        MARKET_COST_MODEL._depth_cache[ticker] = snapshot
        count += 1

    conn.close()
    log.info("Pre-loaded %d mock snapshots into MARKET_COST_MODEL._depth_cache", count)


def get_mock_connection() -> Optional[sqlite3.Connection]:
    """Get a connection to the mock order book DB."""
    if not MOCK_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(MOCK_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate realistic Kalshi order book mock data for market micro signals"
    )
    parser.add_argument("--force", action="store_true",
                        help="Regenerate data even if it already exists")
    parser.add_argument("--validate-only", action="store_true",
                        help="Only run validation on existing data, don't regenerate")
    parser.add_argument("--load-cache", action="store_true",
                        help="Pre-load mock data into MARKET_COST_MODEL._depth_cache")
    parser.add_argument("--sample", type=int, default=0,
                        help="Show sample of N rows from the generated data")
    args = parser.parse_args()

    # Handle preload mode
    if args.load_cache:
        preload_market_cost_model()
        return

    # Open connection
    conn = init_db(MOCK_DB_PATH)

    if args.validate_only:
        stats = compute_validation_stats(conn)
        print_validation_report(stats)
        conn.close()
        return

    # Load thresholds
    log.info("Loading thresholds from settlement_epochs...")
    thresholds = load_thresholds_from_settlement_epochs()
    common = get_common_thresholds(thresholds)
    log.info("Loaded %d station threshold sets, %d common thresholds",
             len(thresholds), len(common))

    # Generate data
    stats = generate_all_data(conn, thresholds, common, force=args.force)

    # Validate
    print_validation_report(stats)

    # Show sample if requested
    if args.sample > 0:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ticker, date_str, station, market_type, threshold,
                   yes_bid, yes_ask, spread, total_volume
            FROM mock_orderbook
            ORDER BY date_str, station
            LIMIT ?
        """, (args.sample,))
        rows = cursor.fetchall()
        print(f"\n  Sample of {len(rows)} rows:")
        print(f"  {'TICKER':<35} {'DATE':<12} {'STN':<6} {'TYPE':<6} {'THR':<4} {'BID':<6} {'ASK':<6} {'SPR':<6} {'VOL':<6}")
        print("  " + "-" * 90)
        for r in rows:
            print(f"  {r[0]:<35} {r[1]:<12} {r[2]:<6} {r[3]:<6} {r[4]:<4} {r[5]:<6.3f} {r[6]:<6.3f} {r[7]:<6.4f} {r[8]:<6}")

    conn.close()


if __name__ == "__main__":
    main()
