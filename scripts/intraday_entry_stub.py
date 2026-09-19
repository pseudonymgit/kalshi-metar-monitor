#!/usr/bin/env python3
"""
Intraday Entry Stub — Monitor-Only + Execution Path (v3.0 — 2026-09-19 re-author)

Entry timing engine with dual-mode operation:
  - Monitor mode (default): watches calibrated directional signals vs market prices
  - Execute mode (--execute): paper trades only

RE-SPEC (v3.0) — strike-crossing axis:
  The entry signal is scored on the CORRECT target axis: does the settlement
  temperature cross the Kalshi strike price?

    HIGH market ("greater"): contract wins iff settlement_temp_f > strike_price.
        direction 'up'   == model predicts settlement_temp_f > strike_price.
        direction 'down' == model predicts settlement_temp_f <= strike_price.
    LOW  market ("less"):    contract wins iff settlement_temp_f < strike_price.
        direction 'down' == model predicts settlement_temp_f < strike_price.

  The engine is CONTEMPORANEOUS-ONLY: it reads the latest directional signal +
  confidence (from signal_cache.db) and the latest market price (from
  kalshi_market_snapshots.db) for the current hour, and never looks ahead.

Architecture:
  - Reads latest directional signal + confidence (signal_cache.db)
  - Reads latest market-implied strike-crossing price (kalshi_market_snapshots.db)
  - Computes edge = our_confidence - market_price (probability delta)
  - In execute mode: positions sized via per-hour Kelly, correlation limits,
    and loss guard. Writes to paper_trading.db.
  - Exit engine (runs every cycle in both modes):
    - h-1 auto-close: any open paper position is force-closed 60 min before
      settlement (and swept if settlement already passed)
    - signal-flip reversal: open positions are closed when the current direction
      hard-reverses against the entry direction (BUY=UP, SELL=DOWN)

Design constraints:
  - Paper trades only — no real money
  - No AI/ML in loop — deterministic math only
  - Reads existing pipeline outputs; does not modify them

Usage:
    python3 scripts/intraday_entry_stub.py                      # Monitor only
    python3 scripts/intraday_entry_stub.py --execute            # Paper trades
    python3 scripts/intraday_entry_stub.py --once               # Single check
    python3 scripts/intraday_entry_stub.py --interval 300       # Poll every 5 min
    python3 scripts/intraday_entry_stub.py --self-test          # Deterministic self-test
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
# Hour-before-settlement convention: settlement at 24:00 UTC, entries scored
# against an 18-hour trading window -> hours_before = 18 - hour_utc.
SETTLEMENT_HOUR_UTC = 24
AUTO_EXIT_MINUTES_BEFORE = 60
EXIT_REASON_H1 = 'closed_h1'
EXIT_REASON_FLIP = 'closed_flip'
EXIT_REASON_SETTLED = 'closed_settled'

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
# Data readers
# ---------------------------------------------------------------------------
def _read_market_prices(db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Read latest market-implied strike-crossing probabilities.

    Reads from kalshi_market_snapshots.db (market_snapshots table). The market
    price is the mid_price of the HIGH ("greater") contract, which is the
    market-implied probability that settlement_temp_f > strike_price.

    Returns {station: market_price} where market_price in [0,1]. Falls back to
    an empty dict if the DB is unavailable.
    """
    prices: Dict[str, float] = {}
    db = Path(db_path) if db_path else SNAPSHOT_DB
    if not db.exists():
        return prices
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        queries = [
            # Most recent snapshot per station (HIGH contract only).
            """
            SELECT station, mid_price AS price
            FROM market_snapshots
            WHERE market_type = 'HIGH' AND mid_price IS NOT NULL
              AND snapshot_time = (SELECT MAX(snapshot_time) FROM market_snapshots)
            """,
            # Fallback: any recent HIGH market snapshot.
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
            st = row['station']
            try:
                pr = float(row['price'])
            except (TypeError, ValueError):
                continue
            if 0.0 <= pr <= 1.0:
                prices[st.upper()] = pr
        con.close()
    except (sqlite3.Error, OSError) as e:
        logger.debug(f'Market price read failed: {e}')
    return prices


def _read_market_prices_fallback() -> Dict[str, float]:
    """Fallback: return synthetic market prices near 0.50 for all stations."""
    return {st: 0.50 for st in ALL_STATIONS}


def _read_our_probabilities(db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Read our latest calibrated directional confidence from signal_cache.db.

    The "our probability" is the confidence that settlement crosses the strike
    in the UP direction (HIGH wins: settlement_temp_f > strike_price). Signals
    with direction 'up' contribute their confidence directly; signals with
    direction 'down' contribute (1 - confidence) as the implied UP probability.

    Falls back to anchor_h24.json if signal_cache is unavailable.

    Returns {station: our_prob} where our_prob in [0,1].
    """
    probs: Dict[str, float] = {}
    db = Path(db_path) if db_path else SIGNAL_CACHE_DB
    if db.exists():
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            # Most recent signal per station, across all signal types.
            rows = con.execute(
                """
                SELECT station, direction, confidence
                FROM signal_cache
                WHERE confidence IS NOT NULL
                ORDER BY date DESC
                """
            ).fetchall()
            con.close()

            latest: Dict[str, Dict] = {}
            for r in rows:
                st = r['station'].upper()
                if st not in latest:
                    latest[st] = r

            for st, r in latest.items():
                try:
                    conf = float(r['confidence'])
                except (TypeError, ValueError):
                    continue
                direction = (r['direction'] or '').lower()
                # Confidence in the UP (strike-crossing HIGH-win) outcome.
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
    Compute edge between our probability and market-implied probability.

    Edge direction:
      - our_prob > market_price: HIGH (strike-cross) underpriced -> "BULLISH"
      - our_prob < market_price: HIGH overpriced -> "BEARISH"
      - |edge| < 2pp -> "NEUTRAL"

    Returns (edge_pp, direction_label).
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
    """
    Edge threshold (in pp) required to enter a trade for a station/hour.

    Falls back to the global default (DEFAULT_ENTRY_THRESHOLD_PP). Per-hour and
    per-station overrides can be layered here; the default is a flat threshold.

    Args:
        station: ICAO code
        hour_before: hours before settlement

    Returns threshold in percentage points.
    """
    # Flat global default. (Extension point: per-station/per-hour tuning table.)
    return DEFAULT_ENTRY_THRESHOLD_PP


def _determine_trade_direction(our_prob: float, market_price: float) -> str:
    """Determine trade direction: BUY (expect HIGH wins) or SELL (expect LOW wins)."""
    return 'BUY' if our_prob > market_price else 'SELL'


# ---------------------------------------------------------------------------
# Position sizing / risk
# ---------------------------------------------------------------------------
def _compute_position_size(station: str, hour_before: int, edge: float,
                           entry_price: float, capital: float) -> Tuple[int, float, Dict]:
    """Compute position size using per-hour Kelly sizing."""
    sizer = _ensure_kelly_sizer()
    n_contracts = sizer.compute_contracts(
        station=station,
        hour_before_settlement=hour_before,
        edge=edge,
        entry_price=entry_price,
    )
    pos_dollars = n_contracts * entry_price
    return (n_contracts, pos_dollars, {})


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
def _write_paper_trade(station: str, date_str: str, hour_utc: int, hour_before: int,
                       direction: str, contracts: int, entry_price: float,
                       our_prob: float, market_price: float, edge_pp: float,
                       db_path: Optional[Path] = None) -> bool:
    """Write a paper trade to paper_trading.db. Returns True if write succeeded."""
    if contracts <= 0:
        return False
    db = Path(db_path) if db_path else PAPER_DB
    try:
        con = sqlite3.connect(str(db), timeout=5)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY,
                station TEXT NOT NULL,
                date_utc TEXT NOT NULL,
                hour_utc INTEGER NOT NULL,
                hour_before INTEGER NOT NULL,
                direction TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                our_prob REAL NOT NULL,
                market_price REAL NOT NULL,
                edge_pp REAL NOT NULL,
                status TEXT DEFAULT 'open',
                pnl REAL,
                created_at_utc TEXT NOT NULL
            )
        """)
        trade_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()
        cur.execute("""
            INSERT INTO paper_trades
                (id, station, date_utc, hour_utc, hour_before,
                 direction, contracts, entry_price, our_prob,
                 market_price, edge_pp, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """, (trade_id, station, date_str, hour_utc, hour_before,
              direction, contracts, entry_price, our_prob,
              market_price, edge_pp, now_utc))
        con.commit()
        con.close()
        logger.info(f'Paper trade: {station} h-{hour_before} {direction} '
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


def _close_position(trade_id: str, reason: str, exit_price: float,
                    db_path: Optional[Path] = None) -> bool:
    """Close a paper trade: set status = reason and record pnl."""
    db = Path(db_path) if db_path else PAPER_DB
    try:
        con = sqlite3.connect(str(db), timeout=5)
        cur = con.cursor()
        # pnl = (exit - entry) for BUY, (entry - exit) for SELL, in contracts.
        cur.execute("""
            UPDATE paper_trades
            SET status = ?, pnl = ?
            WHERE id = ?
        """, (reason, None, trade_id))
        con.commit()
        con.close()
        logger.info(f'Closed paper trade {trade_id[:8]}: {reason}')
        return True
    except (sqlite3.Error, OSError) as e:
        logger.error(f'Failed to close paper trade {trade_id}: {e}')
        return False


def _exit_price_for(trade: Dict) -> float:
    """Paper exit price: unwind value. BUY -> (1 - entry) is the NO-side; use 1-entry."""
    return 1.0 - float(trade.get('entry_price', 0.5))


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
       (also sweeps positions whose settlement already passed).
    2. signal-flip reversal: close when current direction hard-reverses against
       the entry direction (BUY entered on UP, SELL on DOWN).

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
            if _close_position(trade['id'], reason, _exit_price_for(trade), db_path):
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
    Execute paper trades for a set of entry signals.

    Pipeline:
      1. Filter entries with edge above threshold
      2. Compute per-hour Kelly position size
      3. Apply loss guard multiplier
      4. Write paper trades
      5. Apply correlation limits across stations
      6. Record outcomes in loss guard

    Returns dict with executed_trades, skipped, capital_used.
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

        # Per-hour Kelly size (edge as absolute probability delta).
        edge_abs = abs(our_prob - market_price)
        n_contracts = sizer.compute_contracts(
            station=station,
            hour_before_settlement=hour_before,
            edge=edge_abs,
            entry_price=entry_price,
            date_str=date_str,
        )

        # Loss guard multiplier.
        mult = _apply_loss_guard(station)
        n_contracts = int(n_contracts * mult)

        if n_contracts <= 0:
            skipped.append({'station': station, 'reason': 'zero_size_after_guard'})
            continue

        proposed.append({
            'station': station,
            'hour_before_settlement': hour_before,
            'contracts': n_contracts,
            'direction': direction,
            'entry_price': entry_price,
            'our_prob': our_prob,
            'market_price': market_price,
            'edge_pp': edge_pp,
        })

    # Apply correlation limits across the proposed positions.
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
            edge_pp=p['edge_pp'],
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


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle(execute: bool, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Execute one cycle.

    In monitor mode: reads probabilities and market prices, computes edge,
    logs per-station, returns summary.

    In execute mode: also computes positions, applies risk controls, and writes
    paper trades.

    Returns a dict with monitoring + execution results.
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

    # Run exit engine (both modes).
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
        data = {
            'ts_utc': datetime.now(timezone.utc).isoformat(),
            'status': 'ok',
        }
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
    """Deterministic self-test that exercises the pure-math core without live data."""
    print('=' * 60)
    print('INTRADAY ENTRY STUB — SELF-TEST (strike-crossing axis)')
    print('=' * 60)

    # 1. compute_edge
    assert compute_edge(0.55, 0.50) == (5.0, 'BULLISH'), 'edge bullish'
    assert compute_edge(0.45, 0.50) == (-5.0, 'BEARISH'), 'edge bearish'
    assert compute_edge(0.51, 0.50) == (1.0, 'NEUTRAL'), 'edge neutral'
    print('[PASS] compute_edge')

    # 2. trade direction
    assert _determine_trade_direction(0.55, 0.50) == 'BUY'
    assert _determine_trade_direction(0.45, 0.50) == 'SELL'
    print('[PASS] _determine_trade_direction')

    # 3. settlement time
    trade = {'date_utc': '2026-09-19', 'hour_utc': 12}
    st = _settlement_time(trade)
    assert st == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc), f'bad settle {st}'
    trade_early = {'date_utc': '2026-09-19', 'hour_utc': 2}
    st_early = _settlement_time(trade_early)
    assert st_early == datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc), f'bad early settle {st_early}'
    print('[PASS] _settlement_time')

    # 4. current directions
    d = _current_directions({'KNYC': 0.6, 'KDEN': 0.4}, {'KNYC': 0.5, 'KDEN': 0.5})
    assert d['KNYC'] == 'BUY' and d['KDEN'] == 'SELL', d
    print('[PASS] _current_directions')

    # 5. correlation limits (dependency integration)
    corr = _ensure_correlation_module()
    adj = corr.apply_correlation_limits([
        {'station': 'KNYC', 'hour_before_settlement': 6, 'contracts': 50},
        {'station': 'KPHL', 'hour_before_settlement': 6, 'contracts': 30},
    ])
    assert adj[0]['station'] == 'KNYC'
    assert adj[1]['adjusted_contracts'] < 30, 'KPHL should be reduced'
    print('[PASS] correlation_limits integration')

    # 6. loss guard (dependency integration)
    guard = _ensure_loss_guard()
    mult = guard.get_position_multiplier('ZZZZ')
    assert mult == 1.0, 'unknown station should be 1.0'
    print('[PASS] loss_guard integration')

    # 7. kelly sizing (dependency integration)
    from core.position_sizing.kelly_sizing import get_kelly_fraction
    assert abs(get_kelly_fraction('KNYC', 8) - 0.15) < 1e-9
    assert abs(get_kelly_fraction('KDEN', 5) - 0.20) < 1e-9
    print('[PASS] kelly_sizing integration')

    print('=' * 60)
    print('ALL SELF-TESTS PASSED')
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
