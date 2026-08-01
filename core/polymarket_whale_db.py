#!/usr/bin/env python3
"""
Polymarket WhaleWatch — SQLite Persistence Layer

Schema matching the TypeScript types from personas/bertram-gilfoyle/polymarket-whale-alpha.
Separate DB from the Kalshi WhaleWatch (whale_watch.db) to avoid schema coupling.

Tables:
  polymarket_traders           — Whale trader profiles and metrics
  polymarket_markets           — Market metadata (slug, category, prices)
  polymarket_positions         — Trader positions per market
  polymarket_consensus_signals — Aggregated consensus signals
  polymarket_signals_feed      — Cross-platform feed to Kalshi weather engine

B-Mode compliant. No AI/ML.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
PM_WHALE_DB = os.path.join(DATA_DIR, 'polymarket_whale.db')

SCHEMA_SQL = """
-- Polymarket whale trader profiles
CREATE TABLE IF NOT EXISTS polymarket_traders (
    id TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    displayName TEXT DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'sharp',
    primaryCategories TEXT DEFAULT '[]',
    hitRate REAL DEFAULT 0,
    pnl REAL DEFAULT 0,
    roi REAL DEFAULT 0,
    avgEdge REAL DEFAULT 0,
    avgPositionSize REAL DEFAULT 0,
    marketsTraded INTEGER DEFAULT 0,
    resolvedMarkets INTEGER DEFAULT 0,
    winCount INTEGER DEFAULT 0,
    lossCount INTEGER DEFAULT 0,
    recentForm REAL DEFAULT 0,
    consensusFollowRate REAL DEFAULT 0,
    copyScore REAL DEFAULT 0,
    convictionScore REAL DEFAULT 0,
    stabilityScore REAL DEFAULT 0,
    momentumScore REAL DEFAULT 0,
    agreementSignalScore REAL DEFAULT 0,
    label TEXT DEFAULT '',
    provenance TEXT DEFAULT 'live',
    lastActiveAt TEXT,
    notes TEXT DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Polymarket markets
CREATE TABLE IF NOT EXISTS polymarket_markets (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'open',
    platform TEXT DEFAULT 'polymarket',
    openAt TEXT,
    closeAt TEXT,
    resolveAt TEXT,
    daysToClose INTEGER DEFAULT 0,
    shortTerm INTEGER DEFAULT 0,
    volume REAL DEFAULT 0,
    liquidity REAL DEFAULT 0,
    yesPrice REAL DEFAULT 0,
    noPrice REAL DEFAULT 0,
    spread REAL DEFAULT 0,
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Polymarket whale positions
CREATE TABLE IF NOT EXISTS polymarket_positions (
    id TEXT PRIMARY KEY,
    traderId TEXT NOT NULL,
    marketId TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('yes','no')),
    size REAL DEFAULT 0,
    averagePrice REAL DEFAULT 0,
    currentPrice REAL DEFAULT 0,
    notional REAL DEFAULT 0,
    unrealizedPnl REAL DEFAULT 0,
    realizedPnl REAL DEFAULT 0,
    edgeEstimate REAL DEFAULT 0,
    conviction REAL DEFAULT 0,
    openedAt TEXT,
    updatedAt TEXT,
    closedAt TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);

-- Consensus signals (whale agreement on a market)
CREATE TABLE IF NOT EXISTS polymarket_consensus_signals (
    id TEXT PRIMARY KEY,
    marketId TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    title TEXT DEFAULT '',
    side TEXT NOT NULL CHECK(side IN ('yes','no')),
    agreementLevel TEXT NOT NULL DEFAULT 'weak',
    agreementScore REAL DEFAULT 0,
    copyOpportunityScore REAL DEFAULT 0,
    whaleCount INTEGER DEFAULT 0,
    weightedConviction REAL DEFAULT 0,
    weightedEdge REAL DEFAULT 0,
    liquidityScore REAL DEFAULT 0,
    timeToCloseHours REAL DEFAULT 0,
    rationale TEXT DEFAULT '',
    traders_json TEXT DEFAULT '[]',
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cross-platform feed: Polymarket signals → Kalshi weather engine
CREATE TABLE IF NOT EXISTS polymarket_signals_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signalId TEXT NOT NULL,
    marketId TEXT,
    timestamp TEXT NOT NULL,
    kalshi_station TEXT,
    kalshi_bucket INTEGER,
    kalshi_series TEXT DEFAULT 'HIGH',
    signal_direction TEXT NOT NULL DEFAULT 'UP',
    conviction_multiplier REAL DEFAULT 1.0,
    agreement_score REAL DEFAULT 0,
    whale_count INTEGER DEFAULT 0,
    total_notional REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','APPLIED','EXPIRED')),
    ttl_hours REAL DEFAULT 24,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_pm_traders_wallet ON polymarket_traders(wallet);
CREATE INDEX IF NOT EXISTS idx_pm_traders_tier ON polymarket_traders(tier);
CREATE INDEX IF NOT EXISTS idx_pm_markets_slug ON polymarket_markets(slug);
CREATE INDEX IF NOT EXISTS idx_pm_markets_cat ON polymarket_markets(category, status);
CREATE INDEX IF NOT EXISTS idx_pm_positions_trader ON polymarket_positions(traderId);
CREATE INDEX IF NOT EXISTS idx_pm_positions_market ON polymarket_positions(marketId);
CREATE INDEX IF NOT EXISTS idx_pm_consensus_market ON polymarket_consensus_signals(marketId);
CREATE INDEX IF NOT EXISTS idx_pm_consensus_cat ON polymarket_consensus_signals(category);
CREATE INDEX IF NOT EXISTS idx_pm_feed_station ON polymarket_signals_feed(kalshi_station, kalshi_bucket);
CREATE INDEX IF NOT EXISTS idx_pm_feed_status ON polymarket_signals_feed(status, timestamp);
CREATE INDEX IF NOT EXISTS idx_pm_feed_signal ON polymarket_signals_feed(signalId);
"""


def get_db(db_path: str = PM_WHALE_DB) -> sqlite3.Connection:
    """Get a connection to the Polymarket WhaleWatch DB with WAL mode."""
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None,
            db_path: str = PM_WHALE_DB) -> sqlite3.Connection:
    """Initialize schema. Returns connection."""
    if conn is None:
        conn = get_db(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ── Trader CRUD ──────────────────────────────────────────────────────────

def store_traders(conn: sqlite3.Connection, traders: List[dict]) -> int:
    """
    Upsert traders from live API data.

    Each trader dict should contain:
        id, wallet, displayName, tier, primaryCategories (list),
        metrics dict (hitRate, pnl, roi, avgEdge, avgPositionSize,
                     marketsTraded, resolvedMarkets, winCount, lossCount,
                     recentForm, consensusFollowRate),
        copyScore, convictionScore, stabilityScore, momentumScore,
        agreementSignalScore, label, provenance, lastActiveAt, notes

    Returns count of upserted rows.
    """
    count = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for t in traders:
        metrics = t.get('metrics', {})
        primary_cats = t.get('primaryCategories', [])
        notes_list = t.get('notes', [])

        conn.execute("""
            INSERT INTO polymarket_traders (
                id, wallet, displayName, tier, primaryCategories,
                hitRate, pnl, roi, avgEdge, avgPositionSize,
                marketsTraded, resolvedMarkets, winCount, lossCount,
                recentForm, consensusFollowRate,
                copyScore, convictionScore, stabilityScore, momentumScore,
                agreementSignalScore, label, provenance, lastActiveAt,
                notes, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                wallet=excluded.wallet,
                displayName=excluded.displayName,
                tier=excluded.tier,
                primaryCategories=excluded.primaryCategories,
                hitRate=excluded.hitRate,
                pnl=excluded.pnl,
                roi=excluded.roi,
                avgEdge=excluded.avgEdge,
                avgPositionSize=excluded.avgPositionSize,
                marketsTraded=excluded.marketsTraded,
                resolvedMarkets=excluded.resolvedMarkets,
                winCount=excluded.winCount,
                lossCount=excluded.lossCount,
                recentForm=excluded.recentForm,
                consensusFollowRate=excluded.consensusFollowRate,
                copyScore=excluded.copyScore,
                convictionScore=excluded.convictionScore,
                stabilityScore=excluded.stabilityScore,
                momentumScore=excluded.momentumScore,
                agreementSignalScore=excluded.agreementSignalScore,
                label=excluded.label,
                provenance=excluded.provenance,
                lastActiveAt=excluded.lastActiveAt,
                notes=excluded.notes,
                updated_at=excluded.updated_at
        """, (
            t.get('id', ''),
            t.get('wallet', ''),
            t.get('displayName', ''),
            t.get('tier', 'sharp'),
            json.dumps(primary_cats),
            metrics.get('hitRate', 0),
            metrics.get('pnl', 0),
            metrics.get('roi', 0),
            metrics.get('avgEdge', 0),
            metrics.get('avgPositionSize', 0),
            metrics.get('marketsTraded', 0),
            metrics.get('resolvedMarkets', 0),
            metrics.get('winCount', 0),
            metrics.get('lossCount', 0),
            metrics.get('recentForm', 0),
            metrics.get('consensusFollowRate', 0),
            t.get('copyScore', 0),
            t.get('convictionScore', 0),
            t.get('stabilityScore', 0),
            t.get('momentumScore', 0),
            t.get('agreementSignalScore', 0),
            t.get('label', ''),
            t.get('provenance', 'live'),
            t.get('lastActiveAt'),
            json.dumps(notes_list),
            now_iso,
        ))
        count += 1
    conn.commit()
    return count


def get_trader(conn: sqlite3.Connection, trader_id: str) -> Optional[dict]:
    """Get a single trader by ID."""
    row = conn.execute(
        "SELECT * FROM polymarket_traders WHERE id=?",
        (trader_id,)
    ).fetchone()
    return dict(row) if row else None


def get_traders_by_tier(conn: sqlite3.Connection,
                        tier: str = 'whale') -> List[dict]:
    """Get all traders of a given tier."""
    rows = conn.execute(
        "SELECT * FROM polymarket_traders WHERE tier=? ORDER BY copyScore DESC",
        (tier,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Market CRUD ──────────────────────────────────────────────────────────

def store_markets(conn: sqlite3.Connection, markets: List[dict]) -> int:
    """
    Upsert markets from live API data.

    Each market dict should contain:
        id, slug, title, category, status,
        openAt, closeAt, resolveAt,
        volume, liquidity, yesPrice, noPrice, spread,
        description, tags

    Returns count of upserted rows.
    """
    count = 0
    for m in markets:
        tags_list = m.get('tags', [])

        conn.execute("""
            INSERT INTO polymarket_markets (
                id, slug, title, category, status,
                openAt, closeAt, resolveAt,
                daysToClose, shortTerm,
                volume, liquidity, yesPrice, noPrice, spread,
                description, tags
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug,
                title=excluded.title,
                category=excluded.category,
                status=excluded.status,
                openAt=excluded.openAt,
                closeAt=excluded.closeAt,
                resolveAt=excluded.resolveAt,
                daysToClose=excluded.daysToClose,
                shortTerm=excluded.shortTerm,
                volume=excluded.volume,
                liquidity=excluded.liquidity,
                yesPrice=excluded.yesPrice,
                noPrice=excluded.noPrice,
                spread=excluded.spread,
                description=excluded.description,
                tags=excluded.tags,
                updated_at=datetime('now')
        """, (
            m.get('id', ''),
            m.get('slug', ''),
            m.get('title', ''),
            m.get('category', 'other'),
            m.get('status', 'open'),
            m.get('openAt'),
            m.get('closeAt'),
            m.get('resolveAt'),
            m.get('daysToClose', 0),
            1 if m.get('shortTerm') else 0,
            m.get('volume', 0),
            m.get('liquidity', 0),
            m.get('yesPrice', 0),
            m.get('noPrice', 0),
            m.get('spread', 0),
            m.get('description', ''),
            json.dumps(tags_list),
        ))
        count += 1
    conn.commit()
    return count


def get_markets_by_category(
    conn: sqlite3.Connection, category: str = 'weather'
) -> List[dict]:
    """Get markets by category."""
    rows = conn.execute(
        "SELECT * FROM polymarket_markets WHERE category=? AND status='open' ORDER BY volume DESC",
        (category,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Consensus Signal CRUD ────────────────────────────────────────────────

def store_consensus_signals(conn: sqlite3.Connection,
                            signals: List[dict]) -> int:
    """
    Upsert consensus signals.

    Each signal dict should contain:
        id, marketId, category, title, side, agreementLevel,
        agreementScore, copyOpportunityScore, whaleCount,
        weightedConviction, weightedEdge, liquidityScore,
        timeToCloseHours, rationale, traders (list)

    Returns count of upserted rows.
    """
    count = 0
    for s in signals:
        traders_json = json.dumps(s.get('traders', []))

        conn.execute("""
            INSERT INTO polymarket_consensus_signals (
                id, marketId, category, title, side,
                agreementLevel, agreementScore, copyOpportunityScore,
                whaleCount, weightedConviction, weightedEdge,
                liquidityScore, timeToCloseHours, rationale,
                traders_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                marketId=excluded.marketId,
                category=excluded.category,
                title=excluded.title,
                side=excluded.side,
                agreementLevel=excluded.agreementLevel,
                agreementScore=excluded.agreementScore,
                copyOpportunityScore=excluded.copyOpportunityScore,
                whaleCount=excluded.whaleCount,
                weightedConviction=excluded.weightedConviction,
                weightedEdge=excluded.weightedEdge,
                liquidityScore=excluded.liquidityScore,
                timeToCloseHours=excluded.timeToCloseHours,
                rationale=excluded.rationale,
                traders_json=excluded.traders_json,
                generated_at=datetime('now')
        """, (
            s.get('id', ''),
            s.get('marketId', ''),
            s.get('category', 'other'),
            s.get('title', ''),
            s.get('side', 'yes'),
            s.get('agreementLevel', 'weak'),
            s.get('agreementScore', 0),
            s.get('copyOpportunityScore', 0),
            s.get('whaleCount', 0),
            s.get('weightedConviction', 0),
            s.get('weightedEdge', 0),
            s.get('liquidityScore', 0),
            s.get('timeToCloseHours', 0),
            s.get('rationale', ''),
            traders_json,
        ))
        count += 1
    conn.commit()
    return count


def get_active_signals(conn: sqlite3.Connection,
                       station: Optional[str] = None,
                       min_whales: int = 2) -> List[dict]:
    """
    Get active (non-expired) consensus signals from the signals_feed table.

    Args:
        station: Optional Kalshi station filter (e.g., 'KNYC')
        min_whales: Minimum whale wallets for a signal to qualify

    Returns:
        List of active signal feed entries with full stats.
    """
    query = """
        SELECT f.*, c.agreementScore, c.whaleCount, c.weightedEdge,
               c.traders_json, m.title as market_title, m.slug as market_slug
        FROM polymarket_signals_feed f
        LEFT JOIN polymarket_consensus_signals c ON f.signalId = c.id
        LEFT JOIN polymarket_markets m ON f.marketId = m.id
        WHERE f.status = 'PENDING'
          AND f.whale_count >= ?
    """
    params: List[Any] = [min_whales]
    if station:
        query += " AND f.kalshi_station = ?"
        params.append(station)
    query += " ORDER BY f.conviction_multiplier DESC, f.whale_count DESC"

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_recent_signals(conn: sqlite3.Connection,
                       hours: int = 24) -> List[dict]:
    """Get signals created in the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT f.*, c.agreementScore, c.whaleCount, c.weightedEdge,
               c.traders_json, m.title as market_title
        FROM polymarket_signals_feed f
        LEFT JOIN polymarket_consensus_signals c ON f.signalId = c.id
        LEFT JOIN polymarket_markets m ON f.marketId = m.id
        WHERE f.created_at >= ?
        ORDER BY f.created_at DESC
    """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# ── Signal Feed CRUD ──────────────────────────────────────────────────────

def insert_signal_feed_entry(conn: sqlite3.Connection,
                             entry: dict) -> int:
    """
    Insert a new cross-platform signal feed entry.

    Args:
        entry: dict with keys:
            signalId, marketId (optional), timestamp,
            kalshi_station, kalshi_bucket, kalshi_series,
            signal_direction, conviction_multiplier,
            agreement_score, whale_count, total_notional,
            status ('PENDING'|'APPLIED'|'EXPIRED'), ttl_hours

    Returns: rowid
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("""
        INSERT INTO polymarket_signals_feed (
            signalId, marketId, timestamp,
            kalshi_station, kalshi_bucket, kalshi_series,
            signal_direction, conviction_multiplier,
            agreement_score, whale_count, total_notional,
            status, ttl_hours,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        entry.get('signalId', ''),
        entry.get('marketId'),
        entry.get('timestamp', now_iso),
        entry.get('kalshi_station'),
        entry.get('kalshi_bucket'),
        entry.get('kalshi_series', 'HIGH'),
        entry.get('signal_direction', 'UP'),
        entry.get('conviction_multiplier', 1.0),
        entry.get('agreement_score', 0),
        entry.get('whale_count', 0),
        entry.get('total_notional', 0),
        entry.get('status', 'PENDING'),
        entry.get('ttl_hours', 24),
        now_iso,
        now_iso,
    ))
    conn.commit()
    return cur.lastrowid or 0


def expire_stale_signals(conn: sqlite3.Connection) -> int:
    """Mark signals past their TTL as EXPIRED. Returns count expired."""
    cutoff = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("""
        UPDATE polymarket_signals_feed
        SET status='EXPIRED', updated_at=datetime('now')
        WHERE status='PENDING'
          AND datetime(created_at, '+' || ttl_hours || ' hours') < ?
    """, (cutoff,))
    conn.commit()
    return cur.rowcount


def get_polymarket_conviction(conn: sqlite3.Connection,
                              station: str,
                              bucket: int) -> float:
    """
    Get the current polymarket conviction multiplier for a Kalshi market.

    This is the Layer 3 integration point called by the weather engine.

    Args:
        station: Kalshi station code (e.g., 'KNYC')
        bucket: Temperature bucket (e.g., 85)

    Returns:
        conviction_multiplier (1.0 if no active signal)
    """
    row = conn.execute("""
        SELECT conviction_multiplier, agreement_score, whale_count
        FROM polymarket_signals_feed
        WHERE kalshi_station = ?
          AND kalshi_bucket = ?
          AND status = 'PENDING'
        ORDER BY conviction_multiplier DESC
        LIMIT 1
    """, (station, bucket)).fetchone()

    if row:
        return float(row['conviction_multiplier'])
    return 1.0


# ── Stats & Health ──────────────────────────────────────────────────────

def get_stats(conn: sqlite3.Connection) -> dict:
    """Get summary statistics for the Polymarket WhaleWatch DB."""
    stats = {}
    stats['trader_count'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_traders").fetchone()[0]
    stats['market_count'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_markets").fetchone()[0]
    stats['weather_market_count'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_markets WHERE category='weather'").fetchone()[0]
    stats['consensus_signals'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_consensus_signals").fetchone()[0]
    stats['signal_feed_entries'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_signals_feed").fetchone()[0]
    stats['active_signals'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_signals_feed WHERE status='PENDING'").fetchone()[0]
    stats['weather_relevant_signals'] = conn.execute(
        "SELECT COUNT(*) FROM polymarket_signals_feed "
        "WHERE status='PENDING' AND kalshi_station IS NOT NULL").fetchone()[0]
    stats['last_update'] = conn.execute(
        "SELECT MAX(updated_at) FROM polymarket_signals_feed").fetchone()[0]
    return stats


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    conn = init_db()
    print(f"Polymarket WhaleWatch DB initialized at {PM_WHALE_DB}")
    print(json.dumps(get_stats(conn), indent=2))
    conn.close()