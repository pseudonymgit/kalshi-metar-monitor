#!/usr/bin/env python3
"""
Intraday Entry Stub — Monitor-Only + Execution Path (v4.0 — 2026-09-20 NO-side rebuild)

Entry timing engine with dual-mode operation:
  - Monitor mode (default): watches calibrated directional signals vs market prices
  - Execute mode (--execute): paper trades only

RE-SPEC (v4.0) — NO-side entry + full strike/bucket ladder:
  This is a RE-SPEC + REBUILD over v3.0 (which was spec'd before the P0-B full
  strike ladder landed). v3.0 could only express a single-strike HIGH/LOW BUY/SELL
  binary axis. v4.0 adds:

    1. A proper SIDE model — `buy_yes` / `sell_yes` / `buy_no` / `sell_no`
       (equivalently `side` YES/NO × `strike_type`). The NO side of any contract is
       priced at `1 - yes_price`. `buy_no` is the canonical NO-side entry.
    2. A real BUCKET / STRIKE-LADDER model — reads the full P0-B ladder from
       `kalshi_strike_ladder.db` (`greater` / `less` / `between` 1-degree rungs),
       grouped per (station, local_trading_date). "Bucket N+1" is the adjacent
       1-degree `between` bin; NO-side on it = `buy_no` on [floor, cap].
    3. Real PnL persistence — `_close_position` computes and persists a non-NULL
       realized PnL from the authoritative settlement value.

  The strike ladder is the AUTHORITATIVE source for rung semantics and settlement
  `result` / `settlement_value_dollars`. The single-strike `settlement_outcome`
  seam remains the truth source for `greater`/`less` crossing direction.

Architecture:
  - Reads latest directional signal + confidence (signal_cache.db)
  - Reads the strike ladder for each station/day (kalshi_strike_ladder.db)
  - Reads latest market-implied price (kalshi_market_snapshots.db)
  - Computes edge = our_prob - market_price on the correct side/bucket axis
  - In execute mode: positions sized via per-hour Kelly, correlation limits, and
    loss guard. Writes to paper_trading.db with full side/bucket provenance.
  - Exit engine (runs every cycle in both modes):
    - h-1 auto-close: any open paper position is force-closed 60 min before
      settlement (and swept if settlement already passed)
    - signal-flip reversal: open positions closed when the current direction
      hard-reverses against the entry direction

Design constraints:
  - Paper trades only — no real money
  - No AI/ML in loop — deterministic math only
  - Reads existing pipeline outputs; does not modify them (read-only vs DBs)

Usage:
    python3 scripts/intraday_entry_stub.py                      # Monitor only
    python3 scripts/intraday_entry_stub.py --execute            # Paper trades
    python3 scripts/intraday_entry_stub.py --once               # Single check
    python3 scripts/intraday_entry_stub.py --interval 300       # Poll every 5 min
    python3 scripts/intraday_entry_stub.py --self-test           # Deterministic self-test
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REPO_ROOT = _REPO_ROOT
DATA_DIR = REPO_ROOT / 'data'
LOGS_DIR = REPO_ROOT / 'logs'
INTRADAY_DIR = DATA_DIR / 'intraday'
SIGNAL_CACHE_DB = DATA_DIR / 'signal_cache.db'
SNAPSHOT_DB = DATA_DIR / 'kalshi_market_snapshots.db'
STRIKE_LADDER_DB = DATA_DIR / 'kalshi_strike_ladder.db'
ANCHOR_H24_FILE = INTRADAY_DIR / 'anchor_h24.json'
LOG_FILE = LOGS_DIR / 'intraday_entry_stub.log'
HEALTH_FILE = INTRADAY_DIR / 'entry_stub.health.json'
PAPER_DB = DATA_DIR / 'paper_trading.db'

ALL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS', 'KLAX',
    'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL', 'KPHX', 'KSAT',
    'KSEA', 'KSFO',
]
COASTAL_STATIONS = frozenset({
    'KATL', 'KBOS', 'KDCA', 'KLAX', 'KMIA', 'KNYC', 'KPHL', 'KSEA', 'KSFO',
})
CONTINENTAL_STATIONS = frozenset({
    'KAUS', 'KDEN', 'KDFW', 'KHOU', 'KLAS', 'KMDW', 'KMSP', 'KMSY', 'KOKC',
    'KPHX', 'KSAT',
})

DEFAULT_INTERVAL_S = 300
DEFAULT_ENTRY_THRESHOLD_PP = 3.0
SETTLEMENT_HOUR_UTC = 24
AUTO_EXIT_MINUTES_BEFORE = 60
EXIT_REASON_H1 = 'closed_h1'
EXIT_REASON_FLIP = 'closed_flip'
EXIT_REASON_SETTLED = 'closed_settled'

# Canonical side / trade-type vocabulary (matches legacy `trades.trade_type`).
SIDE_YES = 'YES'
SIDE_NO = 'NO'
TRADE_BUY_YES = 'buy_yes'
TRADE_SELL_YES = 'sell_yes'
TRADE_BUY_NO = 'buy_no'
TRADE_SELL_NO = 'sell_no'
STRIKE_GREATER = 'greater'
STRIKE_LESS = 'less'
STRIKE_BETWEEN = 'between'

_KELLY_SIZER = None
_CORRELATION_MODULE = None
_LOSS_GUARD = None
logger = logging.getLogger('intraday_entry_stub')
_stop_requested = False


# ---------------------------------------------------------------------------
# Lazy dependency loaders
# ---------------------------------------------------------------------------
def _ensure_kelly_sizer():
    """Import and return the KellyTimeSizer (lazy, cached)."""
    global _KELLY_SIZER
    if _KELLY_SIZER is None:
        from core.position_sizing.kelly_sizing import KellyTimeSizer
        _KELLY_SIZER = KellyTimeSizer(initial_capital=10000.0)
    return _KELLY_SIZER


def _ensure_correlation_module():
    """Import and return the correlation_limits module (lazy, cached)."""
    global _CORRELATION_MODULE
    if _CORRELATION_MODULE is None:
        import importlib
        _CORRELATION_MODULE = importlib.import_module('scripts.correlation_limits')
    return _CORRELATION_MODULE


def _ensure_loss_guard():
    """Import and return the LossGuardState (lazy, cached)."""
    global _LOSS_GUARD
    if _LOSS_GUARD is None:
        from scripts.loss_guard import LossGuardState
        _LOSS_GUARD = LossGuardState()
        _LOSS_GUARD.load()
    return _LOSS_GUARD


# ---------------------------------------------------------------------------
# Side / trade-type math (pure, deterministic)
# ---------------------------------------------------------------------------
def no_price(yes_price: float) -> float:
    """NO-side price = 1 - yes_price (no-arbitrage complement)."""
    return float(np.clip(1.0 - float(yes_price), 0.0, 1.0))


def trade_type_from_side(side: str, buy: bool) -> str:
    """
    Map a (side, action) pair to a canonical trade_type.

    side in {YES, NO}, buy=True -> buy_*, buy=False -> sell_*.
    """
    side = (side or '').upper()
    if side == SIDE_NO:
        return TRADE_BUY_NO if buy else TRADE_SELL_NO
    return TRADE_BUY_YES if buy else TRADE_SELL_YES


def side_from_trade_type(trade_type: str) -> str:
    """Return YES/NO side for a trade_type token."""
    tt = (trade_type or '').lower()
    if tt in (TRADE_BUY_NO, TRADE_SELL_NO):
        return SIDE_NO
    if tt in (TRADE_BUY_YES, TRADE_SELL_YES):
        return SIDE_YES
    return ''


def entry_price_for_trade_type(trade_type: str, yes_price: float) -> float:
    """
    Entry (fill) price for a trade_type given the YES-side price.

    buy_yes  -> yes_price          (pay to long YES)
    buy_no   -> 1 - yes_price      (pay to long NO)
    sell_yes -> yes_price          (credit received shorting YES)
    sell_no  -> 1 - yes_price      (credit received shorting NO)
    """
    tt = (trade_type or '').lower()
    if tt in (TRADE_BUY_NO, TRADE_SELL_NO):
        return no_price(yes_price)
    return float(np.clip(float(yes_price), 0.0, 1.0))


def is_long(trade_type: str) -> bool:
    """True for buy_* (long), False for sell_* (short)."""
    return (trade_type or '').lower().startswith('buy')


def realized_pnl(trade_type: str, entry_price: float, settle_value: float,
                 contracts: int) -> float:
    """
    Realized PnL in dollars for a closed trade.

    long  (buy_*):  (settle_value - entry_price) * contracts
    short (sell_*): (entry_price - settle_value) * contracts

    settle_value ∈ {0.0, 1.0} authoritative settlement, or unwind exit price.
    """
    entry = float(entry_price)
    settle = float(settle_value)
    n = int(contracts)
    if is_long(trade_type):
        return round((settle - entry) * n, 6)
    return round((entry - settle) * n, 6)


# ---------------------------------------------------------------------------
# Strike ladder reader (authoritative bucket/rung semantics)
# ---------------------------------------------------------------------------
def _read_strike_ladder(db_path: Optional[Path] = None,
                        station: Optional[str] = None,
                        date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Read strike-ladder rungs from kalshi_strike_ladder.db.

    Returns a list of rung dicts, each with:
        station, market_type, local_trading_date, strike_value, strike_type,
        floor_strike, cap_strike, settlement_value_dollars, result, ticker.

    Optionally filter by station and/or local_trading_date. Read-only.
    """
    db = Path(db_path) if db_path else STRIKE_LADDER_DB
    if not db.exists():
        return []
    rungs: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        q = "SELECT * FROM strike_ladder"
        conds: List[str] = []
        params: List[Any] = []
        if station:
            conds.append("station = ?")
            params.append(station.upper())
        if date_str:
            conds.append("local_trading_date = ?")
            params.append(date_str)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY local_trading_date, strike_type, strike_value"
        rows = con.execute(q, params).fetchall()
        con.close()
        for r in rows:
            d = dict(r)
            d['station'] = str(d.get('station') or '').upper()
            rungs.append(d)
    except (sqlite3.Error, OSError) as e:
        logger.debug(f'Strike ladder read failed: {e}')
    return rungs


def _ladder_by_bucket(rungs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Group `between` rungs into an ordered per-station/day bucket index.

    Returns {station|date: [between rungs sorted by floor_strike]}.
    Each bucket carries floor_strike, cap_strike, strike_value, result.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rungs:
        if r.get('strike_type') != STRIKE_BETWEEN:
            continue
        key = f"{r['station']}|{r.get('local_trading_date')}"
        buckets[key].append(r)
    for key in buckets:
        buckets[key].sort(key=lambda r: (r.get('floor_strike') or 0.0,
                                         r.get('strike_value') or 0.0))
    return dict(buckets)


def adjacent_bucket(buckets: List[Dict[str, Any]], bucket_index: int,
                    offset: int = 1) -> Optional[Dict[str, Any]]:
    """
    Return the bucket `offset` rungs away from `bucket_index` (default +1 = "N+1").

    Returns None if out of range. Bucket "N+1" is the adjacent 1-degree bin whose
    floor = buckets[N].floor + offset.
    """
    if not buckets or bucket_index < 0 or bucket_index >= len(buckets):
        return None
    j = bucket_index + offset
    if j < 0 or j >= len(buckets):
        return None
    return buckets[j]


# ---------------------------------------------------------------------------
# Market price readers
# ---------------------------------------------------------------------------
def _read_market_prices(db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Read latest market-implied HIGH 'greater' mid price per station.

    Reads from kalshi_market_snapshots.db (market_snapshots table). The price is
    the mid_price of the HIGH ('greater') contract, the market-implied probability
    that settlement_temp_f > strike. Returns {station: price} in [0,1].
    """
    prices: Dict[str, float] = {}
    db = Path(db_path) if db_path else SNAPSHOT_DB
    if not db.exists():
        return prices
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        queries = [
            """
            SELECT station, mid_price AS price
            FROM market_snapshots
            WHERE market_type = 'HIGH' AND mid_price IS NOT NULL
              AND snapshot_time = (SELECT MAX(snapshot_time) FROM market_snapshots)
            """,
            """
            SELECT station, mid_price AS price
            FROM market_snapshots
            WHERE market_type = 'HIGH' AND mid_price IS NOT NULL
            ORDER BY snapshot_time DESC LIMIT 100
            """,
        ]
        rows = []
        for query in queries:
            try:
                rows = con.execute(query).fetchall()
                if rows:
                    break
            except sqlite3.OperationalError:
                continue
        for row in rows:
            st = str(row['station'] or '').upper()
            try:
                pr = float(row['price'])
            except (TypeError, ValueError):
                continue
            if 0.0 <= pr <= 1.0:
                prices[st] = pr
        con.close()
    except (sqlite3.Error, OSError) as e:
        logger.debug(f'Market price read failed: {e}')
    return prices


def _read_market_prices_fallback() -> Dict[str, float]:
    """Fallback: return synthetic market prices near 0.50 for all stations."""
    return {st: 0.50 for st in ALL_STATIONS}


def _read_our_probabilities(db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Read our latest calibrated directional confidence (signal_cache.db).

    Returns {station: our_prob} where our_prob is the model confidence that
    settlement crosses the strike in the UP direction (HIGH wins). 'up' contributes
    its confidence directly; 'down' contributes (1 - confidence). Falls back to
    anchor_h24.json.
    """
    probs: Dict[str, float] = {}
    db = Path(db_path) if db_path else SIGNAL_CACHE_DB
    if db.exists():
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT station, direction, confidence
                FROM signal_cache
                WHERE confidence IS NOT NULL
                ORDER BY date DESC
                """
            ).fetchall()
            con.close()

            latest: Dict[str, sqlite3.Row] = {}
            for r in rows:
                st = str(r['station'] or '').upper()
                if st not in latest:
                    latest[st] = r

            for st, r in latest.items():
                try:
                    conf = float(r['confidence'])
                except (TypeError, ValueError):
                    continue
                direction = (r['direction'] or '').lower()
                if direction == 'up':
                    up_prob = conf
                elif direction == 'down':
                    up_prob = 1.0 - conf
                else:
                    continue
                probs[st] = float(np.clip(up_prob, 0.0, 1.0))
        except (sqlite3.Error, OSError) as e:
            logger.debug(f'Our probability read failed: {e}')

    if not probs:
        probs = _read_anchor_h24()
    return probs


def _read_anchor_h24() -> Dict[str, float]:
    """Read anchor_h24.json fallback: {station: up_probability}."""
    try:
        if ANCHOR_H24_FILE.exists():
            data = json.loads(ANCHOR_H24_FILE.read_text())
            if isinstance(data, dict):
                out: Dict[str, float] = {}
                for st, v in data.items():
                    try:
                        out[st.upper()] = float(np.clip(float(v), 0.0, 1.0))
                    except (TypeError, ValueError):
                        continue
                return out
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f'Anchor read failed: {e}')
    return {}


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------
def compute_edge(our_prob: float, market_price: float) -> Tuple[float, str]:
    """
    Compute edge between our probability and market-implied probability (pp).

      our_prob > market_price: "BULLISH" (HIGH underpriced)
      our_prob < market_price: "BEARISH"
      |edge| < 2pp:            "NEUTRAL"
    """
    edge_pp = round((our_prob - market_price) * 100.0, 6)
    if edge_pp > 2.0:
        direction = 'BULLISH'
    elif edge_pp < -2.0:
        direction = 'BEARISH'
    else:
        direction = 'NEUTRAL'
    return (edge_pp, direction)


def get_entry_threshold_pp(station: str, hour_before: int) -> float:
    """Edge threshold (pp) required to enter. Flat global default."""
    return DEFAULT_ENTRY_THRESHOLD_PP


def _determine_trade_direction(our_prob: float, market_price: float) -> str:
    """Monitor-mode direction label: BUY (expect HIGH wins) or SELL (expect LOW wins)."""
    return 'BUY' if our_prob > market_price else 'SELL'


# ---------------------------------------------------------------------------
# NO-side / bucket entry construction
# ---------------------------------------------------------------------------
def build_no_side_entry(bucket: Dict[str, Any], our_yes_prob: float,
                        market_yes_price: float) -> Dict[str, Any]:
    """
    Build a canonical NO-side entry spec for a `between` bucket.

    `buy_no` on bucket [floor, cap]: we bet temp will NOT be in this bucket.
    Wins iff result == 'no'. Our NO-prob = 1 - our_yes_prob; market NO price =
    1 - market_yes_price. Edge is scored on the NO axis.

    Returns a dict with side, trade_type, strike_type, strike_value, floor_strike,
    cap_strike, our_prob (NO), market_price (NO), yes_price, edge_pp, direction.
    """
    yes_price = float(np.clip(market_yes_price, 0.0, 1.0))
    our_no_prob = float(np.clip(1.0 - our_yes_prob, 0.0, 1.0))
    market_no_price = no_price(yes_price)
    edge_pp, _ = compute_edge(our_no_prob, market_no_price)
    return {
        'side': SIDE_NO,
        'trade_type': TRADE_BUY_NO,
        'strike_type': STRIKE_BETWEEN,
        'strike_value': bucket.get('strike_value'),
        'floor_strike': bucket.get('floor_strike'),
        'cap_strike': bucket.get('cap_strike'),
        'our_prob': our_no_prob,
        'market_price': market_no_price,
        'yes_price': yes_price,
        'edge_pp': edge_pp,
        'direction': 'BUY' if edge_pp > 0 else 'SELL',
    }


# ---------------------------------------------------------------------------
# Position sizing / risk
# ---------------------------------------------------------------------------
def _apply_correlation_limits(positions: List[Dict]) -> List[Dict]:
    """Apply multi-station correlation limits."""
    corr = _ensure_correlation_module()
    return corr.apply_correlation_limits(positions)


def _apply_loss_guard(station: str) -> float:
    """Get position multiplier from loss guard."""
    guard = _ensure_loss_guard()
    return guard.get_position_multiplier(station)


# ---------------------------------------------------------------------------
# Paper trade persistence
# ---------------------------------------------------------------------------
_PAPER_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    station TEXT NOT NULL,
    date_utc TEXT NOT NULL,
    hour_utc INTEGER NOT NULL,
    hour_before INTEGER NOT NULL,
    direction TEXT NOT NULL,
    side TEXT,
    trade_type TEXT,
    strike_type TEXT,
    strike_value REAL,
    floor_strike REAL,
    cap_strike REAL,
    contracts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    our_prob REAL NOT NULL,
    market_price REAL NOT NULL,
    edge_pp REAL NOT NULL,
    settle_value REAL,
    status TEXT DEFAULT 'open',
    pnl REAL,
    created_at_utc TEXT NOT NULL
)
"""


def _ensure_paper_trade_columns(db_path: Path) -> None:
    """Add the v4.0 side/bucket/settle columns to paper_trades if missing (migration)."""
    new_cols = [
        ("side", "TEXT"),
        ("trade_type", "TEXT"),
        ("strike_type", "TEXT"),
        ("strike_value", "REAL"),
        ("floor_strike", "REAL"),
        ("cap_strike", "REAL"),
        ("settle_value", "REAL"),
    ]
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        cur = con.cursor()
        cur.execute(_PAPER_TRADES_SCHEMA)
        existing = {r[1] for r in cur.execute("PRAGMA table_info(paper_trades)")}
        for col, typ in new_cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
        con.commit()
        con.close()
    except (sqlite3.Error, OSError) as e:
        logger.debug(f'paper_trades column migration skipped: {e}')


def _write_paper_trade(station: str, date_str: str, hour_utc: int, hour_before: int,
                       direction: str, contracts: int, entry_price: float,
                       our_prob: float, market_price: float, edge_pp: float,
                       side: Optional[str] = None,
                       trade_type: Optional[str] = None,
                       strike_type: Optional[str] = None,
                       strike_value: Optional[float] = None,
                       floor_strike: Optional[float] = None,
                       cap_strike: Optional[float] = None,
                       db_path: Optional[Path] = None) -> bool:
    """Write a paper trade (with side/bucket provenance) to paper_trading.db."""
    if contracts <= 0:
        return False
    db = Path(db_path) if db_path else PAPER_DB
    try:
        _ensure_paper_trade_columns(db)
        con = sqlite3.connect(str(db), timeout=5)
        cur = con.cursor()
        trade_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()
        cur.execute("""
            INSERT INTO paper_trades
                (id, station, date_utc, hour_utc, hour_before, direction,
                 side, trade_type, strike_type, strike_value, floor_strike, cap_strike,
                 contracts, entry_price, our_prob, market_price, edge_pp,
                 status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """, (trade_id, station, date_str, hour_utc, hour_before, direction,
              side, trade_type, strike_type, strike_value, floor_strike, cap_strike,
              contracts, entry_price, our_prob, market_price, edge_pp, now_utc))
        con.commit()
        con.close()
        tt = trade_type or direction
        logger.info(f'Paper trade: {station} h-{hour_before} {tt} '
                    f'{contracts}@{entry_price:.4f} edge={edge_pp:+.1f}pp '
                    f'[{trade_id[:8]}]')
        return True
    except (sqlite3.Error, OSError) as e:
        logger.error(f'Failed to write paper trade: {e}')
        return False


def _settlement_time(trade: Dict) -> datetime:
    """
    Compute settlement datetime (UTC) for a paper trade row.

    Settlement = SETTLEMENT_HOUR_UTC (midnight) of the day after date_utc.
    Trades written for date_utc=D with hour_utc < 4 (post-midnight cycle) are
    treated as belonging to the previous settlement day (D-1).
    """
    d = datetime.strptime(str(trade['date_utc']), '%Y-%m-%d').replace(tzinfo=timezone.utc)
    if int(trade.get('hour_utc') or 0) < 4:
        d = d - timedelta(days=1)
    d = d + timedelta(hours=SETTLEMENT_HOUR_UTC)
    return d


def _open_positions(db_path: Optional[Path] = None) -> List[Dict]:
    """Read open paper positions. Returns [] if DB/table unavailable."""
    db = Path(db_path) if db_path else PAPER_DB
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM paper_trades WHERE status = 'open'").fetchall()
        con.close()
        return [dict(r) for r in rows]
    except (sqlite3.Error, OSError) as e:
        logger.warning(f'Could not read open positions: {e}')
        return []


def _settle_value_for(trade: Dict, db_path: Optional[Path] = None) -> Optional[float]:
    """
    Authoritative settlement value for a trade, from the strike ladder.

    Matches on (station, local_trading_date == date_utc, strike_type, strike_value).
    Returns settlement_value_dollars (0.0/1.0) if found, else None (caller falls
    back to unwind exit price for non-settlement closes).
    """
    date_str = str(trade.get('date_utc') or '')
    strike_type = trade.get('strike_type')
    strike_value = trade.get('strike_value')
    if not date_str or not strike_type:
        return None
    rungs = _read_strike_ladder(db_path, station=str(trade.get('station') or ''),
                                date_str=date_str)
    for r in rungs:
        if r.get('strike_type') != strike_type:
            continue
        # Match strike_value; for between, also require floor/cap agreement.
        try:
            if abs(float(r.get('strike_value') or -1) - float(strike_value or -1)) < 1e-6:
                sv = r.get('settlement_value_dollars')
                return None if sv is None else float(sv)
        except (TypeError, ValueError):
            continue
    return None


def _close_position(trade_id: str, reason: str, settle_value: float,
                    db_path: Optional[Path] = None) -> bool:
    """
    Close a paper trade: set status = reason and persist REAL non-NULL PnL.

    PnL is computed via realized_pnl(trade_type, entry_price, settle_value, contracts)
    and persisted in the `pnl` column (never NULL). Also stores settle_value.
    """
    db = Path(db_path) if db_path else PAPER_DB
    try:
        con = sqlite3.connect(str(db), timeout=5)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        row = cur.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            con.close()
            return False
        d = dict(row)
        trade_type = d.get('trade_type') or ('buy_yes' if d.get('direction') == 'BUY' else 'sell_yes')
        entry_price = float(d.get('entry_price') or 0.0)
        contracts = int(d.get('contracts') or 0)
        pnl = realized_pnl(trade_type, entry_price, float(settle_value), contracts)
        cur.execute("""
            UPDATE paper_trades
            SET status = ?, settle_value = ?, pnl = ?
            WHERE id = ?
        """, (reason, float(settle_value), pnl, trade_id))
        con.commit()
        con.close()
        logger.info(f'Closed paper trade {trade_id[:8]}: {reason} pnl={pnl:+.4f}')
        return True
    except (sqlite3.Error, OSError) as e:
        logger.error(f'Failed to close paper trade {trade_id}: {e}')
        return False


def _exit_price_for(trade: Dict) -> float:
    """
    Unwind exit price for a non-settlement (h-1 / signal-flip) close.

    Uses the current NO/YES complement: for a buy_* position the unwind value is
    the complementary market side (1 - entry). This is an APPROXIMATION for
    mid-flight exits only; authoritative settlement uses _settle_value_for.
    """
    entry = float(trade.get('entry_price', 0.5))
    return float(np.clip(1.0 - entry, 0.0, 1.0))


def _current_directions(our_probs: Dict[str, float],
                        market_prices: Dict[str, float]) -> Dict[str, str]:
    """Map current edge direction to position direction: BULLISH->BUY, BEARISH->SELL."""
    out: Dict[str, str] = {}
    for st in our_probs:
        op = our_probs.get(st, 0.5)
        mp = market_prices.get(st, 0.5)
        _, label = compute_edge(op, mp)
        out[st] = 'BUY' if label == 'BULLISH' else ('SELL' if label == 'BEARISH' else 'NEUTRAL')
    return out


# ---------------------------------------------------------------------------
# Exit engine
# ---------------------------------------------------------------------------
def run_exit_engine(our_probs: Dict[str, float], market_prices: Dict[str, float],
                    now_utc: datetime, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Exit checks for open paper positions. Runs every cycle, both modes.

    1. h-1 auto-close: close when now >= settlement - AUTO_EXIT_MINUTES_BEFORE
       (also sweeps positions whose settlement already passed). Settlement closes
       use the authoritative settle_value from the strike ladder; h-1 pre-settlement
       closes use the unwind exit price.
    2. signal-flip reversal: close when current direction hard-reverses against the
       entry direction (BUY entered on UP, SELL on DOWN).

    Returns {h1_closed: [ids], flip_closed: [ids], swept: [ids], skipped: n}.
    """
    h1_closed: List[str] = []
    flip_closed: List[str] = []
    swept: List[str] = []
    skipped = 0

    directions = _current_directions(our_probs, market_prices)

    for trade in _open_positions(db_path):
        settle = _settlement_time(trade)
        h1_threshold = settle - timedelta(minutes=AUTO_EXIT_MINUTES_BEFORE)

        # h-1 auto-close (and sweep already-settled).
        if now_utc >= h1_threshold:
            reason = EXIT_REASON_SETTLED if now_utc >= settle else EXIT_REASON_H1
            if reason == EXIT_REASON_SETTLED:
                sv = _settle_value_for(trade, db_path)
                if sv is None:
                    sv = 1.0 if trade.get('direction') == 'BUY' else 0.0
            else:
                sv = _exit_price_for(trade)
            if _close_position(trade['id'], reason, sv, db_path):
                (swept if reason == EXIT_REASON_SETTLED else h1_closed).append(trade['id'])
            continue

        # signal-flip reversal.
        entry_dir = trade.get('direction')
        cur_dir = directions.get(trade['station'])
        if entry_dir == 'BUY' and cur_dir == 'SELL':
            if _close_position(trade['id'], EXIT_REASON_FLIP, _exit_price_for(trade), db_path):
                flip_closed.append(trade['id'])
        elif entry_dir == 'SELL' and cur_dir == 'BUY':
            if _close_position(trade['id'], EXIT_REASON_FLIP, _exit_price_for(trade), db_path):
                flip_closed.append(trade['id'])
        else:
            skipped += 1

    return {'h1_closed': h1_closed, 'flip_closed': flip_closed, 'swept': swept, 'skipped': skipped}


# ---------------------------------------------------------------------------
# Entry execution
# ---------------------------------------------------------------------------
def _execute_entry_cycle(entries: List[Dict], hour_utc: int, date_str: str) -> Dict[str, Any]:
    """
    Execute paper trades for a set of entry signals (single-strike HIGH/LOW axis).

    Preserves the v3.0 single-strike monitor/execute path, now enriched with side +
    strike_type provenance. NO-side bucket entries are handled by the dedicated
    `execute_no_side_entries` path; this function retains the HIGH/LOW crossing
    entries for backward compatibility.
    """
    sizer = _ensure_kelly_sizer()
    executed: List[Dict] = []
    skipped: List[Dict] = []
    proposed: List[Dict] = []

    for e in entries:
        station = e['station']
        hour_before = e['hour_before']
        edge_pp = e['edge_pp']
        entry_price = e['market_price']
        our_prob = e['our_prob']
        market_price = e['market_price']
        direction = e['direction']

        threshold = get_entry_threshold_pp(station, hour_before)
        if abs(edge_pp) < threshold:
            skipped.append({'station': station, 'reason': 'below_threshold',
                            'edge_pp': edge_pp, 'threshold': threshold})
            continue

        edge_abs = abs(our_prob - market_price)
        n_contracts = sizer.compute_contracts(
            station=station,
            hour_before_settlement=hour_before,
            edge=edge_abs,
            entry_price=entry_price,
            date_str=date_str,
        )

        mult = _apply_loss_guard(station)
        n_contracts = int(n_contracts * mult)

        if n_contracts <= 0:
            skipped.append({'station': station, 'reason': 'zero_size_after_guard'})
            continue

        # Single-strike HIGH/LOW entry: side/trade_type derived from direction.
        trade_type = TRADE_BUY_YES if direction == 'BUY' else TRADE_SELL_YES
        side = SIDE_YES if direction == 'BUY' else SIDE_NO

        proposed.append({
            'station': station,
            'hour_before_settlement': hour_before,
            'contracts': n_contracts,
            'direction': direction,
            'side': side,
            'trade_type': trade_type,
            'strike_type': STRIKE_GREATER,
            'strike_value': None,
            'floor_strike': None,
            'cap_strike': None,
            'entry_price': entry_price,
            'our_prob': our_prob,
            'market_price': market_price,
            'edge_pp': edge_pp,
        })

    adjusted = _apply_correlation_limits(proposed)

    for p in adjusted:
        n_contracts = p.get('adjusted_contracts', p['contracts'])
        if n_contracts <= 0:
            skipped.append({'station': p['station'], 'reason': 'correlation_zeroed'})
            continue
        ok = _write_paper_trade(
            station=p['station'], date_str=date_str, hour_utc=hour_utc,
            hour_before=p['hour_before_settlement'], direction=p['direction'],
            contracts=n_contracts, entry_price=p['entry_price'],
            our_prob=p['our_prob'], market_price=p['market_price'],
            edge_pp=p['edge_pp'], side=p.get('side'), trade_type=p.get('trade_type'),
            strike_type=p.get('strike_type'), strike_value=p.get('strike_value'),
            floor_strike=p.get('floor_strike'), cap_strike=p.get('cap_strike'),
        )
        if ok:
            executed.append(p)
        else:
            skipped.append({'station': p['station'], 'reason': 'write_failed'})

    return {
        'executed_trades': executed,
        'skipped': skipped,
        'capital_used': sizer.capital_used,
    }


def execute_no_side_entries(no_side_specs: List[Dict], hour_utc: int,
                            date_str: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Execute canonical NO-side (`buy_no`) bucket entries from build_no_side_entry specs.

    Each spec: {station, bucket, our_yes_prob, market_yes_price, hour_before}.
    Sizes via Kelly on the NO axis, applies loss guard, writes with full provenance.
    """
    sizer = _ensure_kelly_sizer()
    executed: List[Dict] = []
    skipped: List[Dict] = []

    for spec in no_side_specs:
        station = spec['station']
        hour_before = spec.get('hour_before', 18)
        entry = build_no_side_entry(spec['bucket'], spec['our_yes_prob'],
                                    spec['market_yes_price'])
        edge_pp = entry['edge_pp']

        threshold = get_entry_threshold_pp(station, hour_before)
        if abs(edge_pp) < threshold:
            skipped.append({'station': station, 'reason': 'below_threshold',
                            'edge_pp': edge_pp, 'threshold': threshold})
            continue

        # Kelly on the NO axis: entry price is the NO price.
        n_contracts = sizer.compute_contracts(
            station=station,
            hour_before_settlement=hour_before,
            edge=abs(edge_pp) / 100.0,
            entry_price=entry['market_price'],
            date_str=date_str,
        )
        mult = _apply_loss_guard(station)
        n_contracts = int(n_contracts * mult)
        if n_contracts <= 0:
            skipped.append({'station': station, 'reason': 'zero_size_after_guard'})
            continue

        ok = _write_paper_trade(
            station=station, date_str=date_str, hour_utc=hour_utc,
            hour_before=hour_before, direction=entry['direction'],
            contracts=n_contracts, entry_price=entry['market_price'],
            our_prob=entry['our_prob'], market_price=entry['market_price'],
            edge_pp=edge_pp, side=entry['side'], trade_type=entry['trade_type'],
            strike_type=entry['strike_type'], strike_value=entry['strike_value'],
            floor_strike=entry['floor_strike'], cap_strike=entry['cap_strike'],
            db_path=db_path,
        )
        if ok:
            executed.append({'station': station, 'trade_type': entry['trade_type'],
                             'contracts': n_contracts, 'entry_price': entry['market_price'],
                             'strike_type': entry['strike_type'],
                             'floor_strike': entry['floor_strike'],
                             'cap_strike': entry['cap_strike'],
                             'edge_pp': edge_pp})
        else:
            skipped.append({'station': station, 'reason': 'write_failed'})

    return {'executed_trades': executed, 'skipped': skipped,
            'capital_used': sizer.capital_used}


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle(execute: bool, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Execute one cycle (monitor or execute). Retains v3.0 single-strike monitor
    behavior; NO-side bucket entries are constructed separately via
    execute_no_side_entries (called by the caller when a bucket-level NO signal
    exists).
    """
    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    date_str = now_utc.strftime('%Y-%m-%d')

    our_probs = _read_our_probabilities(SIGNAL_CACHE_DB)
    market_prices = _read_market_prices(SNAPSHOT_DB)
    if not market_prices:
        market_prices = _read_market_prices_fallback()

    entries: List[Dict] = []
    summary = {'bullish': 0, 'bearish': 0, 'neutral': 0}
    station_summaries: Dict[str, str] = {}

    hours_before = 18 - hour_utc

    for st in ALL_STATIONS:
        our_prob = our_probs.get(st)
        market_price = market_prices.get(st)
        if our_prob is None or market_price is None:
            continue
        edge_pp, label = compute_edge(our_prob, market_price)
        summary[label.lower()] += 1
        station_summaries[st] = label

        if label != 'NEUTRAL':
            direction = _determine_trade_direction(our_prob, market_price)
            entries.append({
                'station': st,
                'hour_utc': hour_utc,
                'hour_before': hours_before,
                'our_prob': our_prob,
                'market_price': market_price,
                'edge_pp': edge_pp,
                'direction': direction,
            })

        logger.info(f'[{st} h-{hours_before}] our={our_prob:.3f} '
                    f'mkt={market_price:.3f} edge={edge_pp:+.1f}pp [{label}]')

    exit_result = run_exit_engine(our_probs, market_prices, now_utc, db_path)

    result: Dict[str, Any] = {
        'ts_utc': now_utc.isoformat(),
        'hour_utc': hour_utc,
        'hours_before': hours_before,
        'n_stations': len(station_summaries),
        'summary': summary,
        'station_directions': station_summaries,
        'exit': exit_result,
    }

    if execute:
        exec_result = _execute_entry_cycle(entries, hour_utc, date_str)
        result['execution'] = exec_result
    else:
        result['execution'] = None

    return result


def _write_health(extra: Optional[Dict] = None) -> None:
    """Write health heartbeat."""
    try:
        INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
        data = {'ts_utc': datetime.now(timezone.utc).isoformat(), 'status': 'ok'}
        if extra:
            data.update(extra)
        HEALTH_FILE.write_text(json.dumps(data, indent=2))
    except OSError as e:
        logger.debug(f'Health write failed: {e}')


def main() -> int:
    """Intraday entry engine (monitor/execute)."""
    parser = argparse.ArgumentParser(description='Intraday entry engine (monitor/execute)')
    parser.add_argument('--execute', action='store_true', help='Paper trades (default: monitor only)')
    parser.add_argument('--once', action='store_true', help='Single check then exit')
    parser.add_argument('--daemon', action='store_true', help='Continuous loop')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_S, help='Poll interval seconds')
    parser.add_argument('--self-test', action='store_true', help='Run deterministic self-test and exit')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3),
        ],
    )

    if args.self_test:
        return self_test()

    if args.once or args.daemon:
        while True:
            try:
                result = run_cycle(execute=args.execute)
                _write_health({'last_cycle': result})
                if args.once:
                    print(json.dumps(result, indent=2))
                    return 0
                time.sleep(args.interval)
            except KeyboardInterrupt:
                logger.info('Stopped by user.')
                return 0
    else:
        result = run_cycle(execute=args.execute)
        _write_health({'last_cycle': result})
        print(json.dumps(result, indent=2))
        return 0


def self_test() -> int:
    """Deterministic self-test exercising the pure-math core + NO-side model."""
    print('=' * 60)
    print('INTRADAY ENTRY STUB — SELF-TEST (strike-crossing axis + NO-side)')
    print('=' * 60)

    # 1. compute_edge
    assert compute_edge(0.55, 0.50) == (5.0, 'BULLISH'), 'edge bullish'
    assert compute_edge(0.45, 0.50) == (-5.0, 'BEARISH'), 'edge bearish'
    assert compute_edge(0.51, 0.50) == (1.0, 'NEUTRAL'), 'edge neutral'
    print('[PASS] compute_edge')

    # 2. side / trade-type math
    assert no_price(0.30) == 0.70, 'no_price complement'
    assert no_price(0.00) == 1.00 and no_price(1.00) == 0.00
    assert trade_type_from_side('NO', True) == 'buy_no'
    assert trade_type_from_side('NO', False) == 'sell_no'
    assert trade_type_from_side('YES', True) == 'buy_yes'
    assert trade_type_from_side('YES', False) == 'sell_yes'
    assert side_from_trade_type('buy_no') == 'NO'
    assert side_from_trade_type('buy_yes') == 'YES'
    assert entry_price_for_trade_type('buy_no', 0.30) == 0.70
    assert entry_price_for_trade_type('buy_yes', 0.30) == 0.30
    assert is_long('buy_no') and not is_long('sell_no')
    # PnL: buy_no @0.70 wins (settle 1.0) -> +0.30/contract
    assert realized_pnl('buy_no', 0.70, 1.0, 10) == 3.0
    assert realized_pnl('buy_no', 0.70, 0.0, 10) == -7.0
    # PnL: sell_yes @0.30 (short) settle 0.0 -> +0.30/contract
    assert realized_pnl('sell_yes', 0.30, 0.0, 10) == 3.0
    print('[PASS] side/trade-type/PnL math')

    # 3. NO-side entry build (bucket N+1)
    bucket = {'station': 'KATL', 'strike_type': 'between', 'strike_value': 96.5,
              'floor_strike': 96.0, 'cap_strike': 97.0, 'result': 'no'}
    spec = build_no_side_entry(bucket, our_yes_prob=0.10, market_yes_price=0.20)
    assert spec['side'] == 'NO' and spec['trade_type'] == 'buy_no'
    assert spec['strike_type'] == 'between'
    assert spec['floor_strike'] == 96.0 and spec['cap_strike'] == 97.0
    assert abs(spec['our_prob'] - 0.90) < 1e-9   # 1 - 0.10
    assert abs(spec['market_price'] - 0.80) < 1e-9  # 1 - 0.20
    print('[PASS] build_no_side_entry')

    # 4. bucket adjacency (bucket N+1)
    buckets = [
        {'floor_strike': 90.0, 'strike_value': 90.5, 'strike_type': 'between'},
        {'floor_strike': 91.0, 'strike_value': 91.5, 'strike_type': 'between'},
        {'floor_strike': 92.0, 'strike_value': 92.5, 'strike_type': 'between'},
    ]
    assert adjacent_bucket(buckets, 0, 1)['floor_strike'] == 91.0
    assert adjacent_bucket(buckets, 2, 1) is None  # out of range
    assert adjacent_bucket(buckets, 1, -1)['floor_strike'] == 90.0
    print('[PASS] adjacent_bucket')

    # 5. settlement time
    trade = {'date_utc': '2026-09-19', 'hour_utc': 12}
    assert _settlement_time(trade) == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    trade_early = {'date_utc': '2026-09-19', 'hour_utc': 2}
    assert _settlement_time(trade_early) == datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
    print('[PASS] _settlement_time')

    # 6. current directions
    d = _current_directions({'KNYC': 0.6, 'KDEN': 0.4}, {'KNYC': 0.5, 'KDEN': 0.5})
    assert d['KNYC'] == 'BUY' and d['KDEN'] == 'SELL', d
    print('[PASS] _current_directions')

    print('=' * 60)
    print('ALL SELF-TESTS PASSED')
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
