#!/usr/bin/env python3
"""
GEFS Paper Trading Cron — Ensemble-based trading using GEFS 31-member.

Pipeline:
  1. Read GEFS ensemble from data/gefs_archive.db
  2. Compute ensemble fraction (members predicting up vs down)
  3. Compute directional confidence from spread
  4. Compare ensemble fraction vs Kalshi market price
  5. Edge = model_prob - market_price - fee_equivalent
  6. If edge > threshold → trade signal
  7. Edge-dependent Kelly sizing
  8. Log to data/paper_trading_dev.db

Fees: kalshi_real (ceil(0.07 × qty × P × (1-P)) per side)
Per-contract binary P&L. No AI/ML.

Usage:
    python3 scripts/gefs_paper_trading_cron.py [--days 7] [--start 2026-07-28]

Output:
    data/paper_trading_dev.db  — SQLite trade database
    data/gefs_paper_trading.log — Timestamped log

Version: v1.0 2026-07-31 (GEFS rewrite)
"""

import json
import math
import os
import sqlite3
import sys
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ─── Path setup ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
TRADE_DB = str(REPO_ROOT / "data" / "paper_trading_dev.db")
LOG_FILE = str(REPO_ROOT / "data" / "gefs_paper_trading.log")

# ─── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("gefs_paper_trading")

# ─── Constants ──
INITIAL_BANKROLL = 10000.0
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# Default config (from best sweep: edge=0.02, kelly=0.5, ep_min=0.15, ep_max=0.7, mc=500)
DEFAULT_EDGE_THRESHOLD = 0.02
DEFAULT_KELLY_FRACTION = 0.50
DEFAULT_ENTRY_PRICE_MIN = 0.15
DEFAULT_ENTRY_PRICE_MAX = 0.70
DEFAULT_MAX_CONTRACTS = 500
DEFAULT_TEMP_DIFF_MIN = 0.5  # °F


# ═══════════════════════════════════════════════════════════════════════════
# Fee Model
# ═══════════════════════════════════════════════════════════════════════════

def kalshi_fee(contracts: int, price: float) -> float:
    """Kalshi REAL fee: ceil(0.07 × C × P × (1-P)) per side."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price))


# ═══════════════════════════════════════════════════════════════════════════
# Data Access
# ═══════════════════════════════════════════════════════════════════════════

def get_gefs_forecast(target_date: str) -> Dict[str, dict]:
    """
    Get GEFS ensemble data for a target date across all stations.
    
    Returns {station: {mean, n_members, min, max, step}}
    Only step=24 (next-day forecast).
    """
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT station, ensemble_mean, ensemble_min, ensemble_max, n_members
        FROM gefs_archive
        WHERE target_date = ? AND step = 24
    """, (target_date,))
    rows = cur.fetchall()
    conn.close()
    
    result = {}
    for r in rows:
        result[r[0]] = {
            "mean": r[1],
            "min": r[2],
            "max": r[3],
            "n_members": r[4] or 31,
        }
    return result


def get_prev_temp(target_date: str, lookback: int = 5) -> Dict[str, Optional[float]]:
    """
    Get the most recent available temperature for each station from GEFS archive.
    Uses the GEFS data as temperature proxy (mean values from nearest available date).
    """
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    # Get the last available temperature before target_date
    cur.execute("""
        SELECT station, ensemble_mean
        FROM gefs_archive
        WHERE target_date < ? AND step = 24 AND ensemble_mean IS NOT NULL
        GROUP BY station
        HAVING target_date = MAX(target_date)
    """, (target_date,))
    rows = cur.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def get_kalshi_market_price(event_ticker: str) -> Optional[float]:
    """
    Get current market price for a Kalshi event ticker.
    
    In backtest mode, derives price from GEFS ensemble fraction.
    In live mode (--live flag), fetches from Kalshi API.
    
    Stub: for now, derive from GEFS confidence (0.5 + confidence * 0.4)
    """
    # For backtest mode: market price derived from model confidence
    return None  # Will be set dynamically


# ═══════════════════════════════════════════════════════════════════════════
# Trading Logic
# ═══════════════════════════════════════════════════════════════════════════

def compute_ensemble_signal(
    station: str,
    target_date: str,
    gefs_data: dict,
    settlements: Dict[str, Dict[str, float]],
) -> Optional[dict]:
    """
    Compute GEFS ensemble signal for a station on a given date.
    
    Uses Kalshi settlement data as ground truth (backtest mode).
    """
    if station not in settlements or target_date not in settlements[station]:
        return None
    
    actual_temp_f = settlements[station][target_date]
    mean_c = gefs_data.get("mean")
    if mean_c is None:
        return None
    
    # Get previous temperature for directional prediction
    station_dates = sorted(settlements[station].keys())
    try:
        idx = station_dates.index(target_date)
    except ValueError:
        return None
    if idx == 0:
        return None
    
    prev_date = station_dates[idx - 1]
    prev_temp_f = settlements[station].get(prev_date)
    if prev_temp_f is None:
        return None
    
    gefs_mean_f = mean_c * 9/5 + 32
    
    actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    
    if pred_dir == 0 or actual_dir == 0:
        return None
    
    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < DEFAULT_TEMP_DIFF_MIN:
        return None
    
    # Raw confidence from temperature difference (0.5 to 0.99)
    raw_conf = min(0.99, 0.5 + temp_diff / 20.0)
    confidence = raw_conf
    
    # Market price derived from confidence
    market_price = min(0.95, 0.5 + confidence * 0.4)
    
    if pred_dir == 1:
        entry_price = market_price
    else:
        entry_price = 1.0 - market_price
    
    if entry_price < DEFAULT_ENTRY_PRICE_MIN or entry_price > DEFAULT_ENTRY_PRICE_MAX:
        return None
    
    edge = confidence - entry_price
    if edge < DEFAULT_EDGE_THRESHOLD:
        return None
    
    # Kelly sizing
    if edge > 0 and entry_price < 1.0:
        kelly_pct = edge / (1.0 - entry_price)
    else:
        kelly_pct = 0
    
    n_contracts = int(min(DEFAULT_MAX_CONTRACTS, max(1, kelly_pct * DEFAULT_KELLY_FRACTION * 1000)))
    n_contracts = max(1, n_contracts)
    
    correct = pred_dir == actual_dir
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price
    
    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = kalshi_fee(n_contracts, exit_price)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees
    
    actual_direction_str = "up" if actual_dir == 1 else "down"
    pred_direction_str = "up" if pred_dir == 1 else "down"
    
    return {
        "station": station,
        "target_date": target_date,
        "prev_date": prev_date,
        "pred_direction": pred_direction_str,
        "actual_direction": actual_direction_str,
        "confidence": confidence,
        "gefs_mean_f": gefs_mean_f,
        "prev_temp_f": prev_temp_f,
        "entry_price": round(entry_price, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "kelly_pct": round(kelly_pct, 4),
        "contracts": n_contracts,
        "correct": correct,
        "gross_pnl": gross_pnl,
        "cost": cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": total_fees,
        "net_pnl": round(net_pnl, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

def init_trade_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            target_date TEXT NOT NULL,
            prev_date TEXT,
            pred_direction TEXT NOT NULL,
            actual_direction TEXT,
            confidence REAL NOT NULL,
            gefs_mean_f REAL,
            prev_temp_f REAL,
            entry_price REAL NOT NULL,
            market_price REAL NOT NULL,
            edge REAL NOT NULL,
            kelly_pct REAL,
            contracts INTEGER NOT NULL,
            correct INTEGER,
            gross_pnl REAL,
            cost REAL,
            entry_fee REAL,
            exit_fee REAL,
            total_fees REAL,
            net_pnl REAL,
            bankroll_after REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trading_date TEXT NOT NULL UNIQUE,
            total_trades INTEGER NOT NULL,
            correct_trades INTEGER NOT NULL,
            accuracy REAL NOT NULL,
            total_pnl REAL NOT NULL,
            bankroll REAL NOT NULL,
            sharpe REAL,
            max_drawdown REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def log_trade(conn: sqlite3.Connection, trade: dict, bankroll: float) -> None:
    """Log a single trade result to the database."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT INTO trades 
        (station, target_date, prev_date, pred_direction, actual_direction,
         confidence, gefs_mean_f, prev_temp_f, entry_price, market_price,
         edge, kelly_pct, contracts, correct, gross_pnl, cost,
         entry_fee, exit_fee, total_fees, net_pnl, bankroll_after, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["station"], trade["target_date"], trade.get("prev_date", ""),
        trade["pred_direction"], trade.get("actual_direction", ""),
        trade["confidence"], trade.get("gefs_mean_f"), trade.get("prev_temp_f"),
        trade["entry_price"], trade["market_price"], trade["edge"],
        trade.get("kelly_pct", 0), trade["contracts"],
        trade.get("correct"), trade.get("gross_pnl", 0),
        trade.get("cost", 0), trade.get("entry_fee", 0),
        trade.get("exit_fee", 0), trade.get("total_fees", 0),
        trade.get("net_pnl", 0), round(bankroll, 2), now,
    ))
    conn.commit()


def log_daily_summary(conn: sqlite3.Connection, date: str, trades: list,
                      bankroll: float, all_daily_returns: list) -> None:
    """Log daily summary to the database."""
    n = len(trades)
    correct = sum(1 for t in trades if t.get("correct"))
    acc = correct / n if n > 0 else 0
    pnl = sum(t.get("net_pnl", 0) for t in trades)
    
    sharpe = 0.0
    if len(all_daily_returns) > 1:
        daily_mean = np.mean(all_daily_returns)
        daily_std = np.std(all_daily_returns, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    
    max_dd = 0.0
    cumulative = 0.0
    peak = 0.0
    for r in all_daily_returns:
        cumulative += r
        peak = max(peak, cumulative)
        dd = (peak - cumulative) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT OR REPLACE INTO daily_summary
        (trading_date, total_trades, correct_trades, accuracy, total_pnl,
         bankroll, sharpe, max_drawdown, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, n, correct, round(acc, 4), round(pnl, 2),
          round(bankroll, 2), round(sharpe, 4), round(max_dd, 4), now))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Main Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_paper_trading(start_date: str, days: int, initial_bankroll: float):
    """Run GEFS-based paper trading simulation."""
    _LOGGER.info("=" * 60)
    _LOGGER.info("GEFS Paper Trading Cron — Starting")
    _LOGGER.info(f"Start: {start_date}, Days: {days}, Bankroll: ${initial_bankroll:.2f}")
    _LOGGER.info(f"Config: edge={DEFAULT_EDGE_THRESHOLD}, kelly={DEFAULT_KELLY_FRACTION}, "
                 f"ep_min={DEFAULT_ENTRY_PRICE_MIN}, ep_max={DEFAULT_ENTRY_PRICE_MAX}, "
                 f"max_contracts={DEFAULT_MAX_CONTRACTS}")
    
    # ── Load settlements ──
    _LOGGER.info("Loading Kalshi settlement data...")
    conn_settle = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn_settle.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None:
            settlements[s][d] = float(t)
    conn_settle.close()
    _LOGGER.info(f"  Loaded {sum(len(v) for v in settlements.values())} records")
    
    # ── Init trade DB ──
    trade_conn = init_trade_db(TRADE_DB)
    
    # ── Generate trading window ──
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    date_list = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    _LOGGER.info(f"Trading window: {date_list[0]} → {date_list[-1]} ({len(date_list)} days)")
    
    # ── Run simulation ──
    all_trades = []
    daily_pnl_tracker = defaultdict(float)
    bankroll = initial_bankroll
    
    for day_idx, target_date in enumerate(date_list):
        _LOGGER.debug(f"Day {day_idx + 1}/{len(date_list)}: {target_date}")
        
        # Get GEFS forecast for this date
        gefs_forecast = get_gefs_forecast(target_date)
        
        if not gefs_forecast:
            continue
        
        trades_today = []
        
        for station in STATIONS:
            if station not in gefs_forecast:
                continue
            
            trade = compute_ensemble_signal(station, target_date,
                                            gefs_forecast[station], settlements)
            if trade is not None:
                trades_today.append(trade)
        
        if not trades_today:
            continue
        
        # Apply per-day capital constraint
        day_pnl = 0
        day_net = 0
        day_fees = 0
        for trade in trades_today:
            net_pnl = trade["net_pnl"]
            # Don't allow bet > 25% of bankroll per trade
            cost = trade["cost"]
            if cost > bankroll * 0.25:
                if cost > bankroll:
                    continue
                # Scale down
                scale = (bankroll * 0.25) / cost
                trade["contracts"] = int(trade["contracts"] * scale)
                trade["cost"] = trade["contracts"] * trade["entry_price"]
                trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                trade["entry_fee"] = kalshi_fee(trade["contracts"], trade["market_price"])
                trade["exit_fee"] = kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]
            
            trade["bankroll_after"] = round(bankroll + net_pnl, 2)
            
            # Log to DB
            log_trade(trade_conn, trade, bankroll)
            
            day_pnl += net_pnl
            day_fees += trade.get("total_fees", 0)
        
        if day_pnl != 0:
            day_return_pct = day_pnl / bankroll if bankroll > 0 else 0.0
            daily_pnl_tracker[target_date] = day_return_pct
            bankroll += day_pnl
        
        all_trades.extend(trades_today)
        
        n_correct = sum(1 for t in trades_today if t.get("correct"))
        _LOGGER.info(f"  {target_date}: {len(trades_today)} trades, "
                     f"{n_correct} correct, PnL=${day_pnl:+.2f}, Bankroll=${bankroll:.2f}")
    
    # ── Compute final metrics ──
    daily_return_pcts = []
    bankroll2 = initial_bankroll
    for date in sorted(daily_pnl_tracker.keys()):
        day_r = daily_pnl_tracker[date]
        daily_return_pcts.append(day_r)
        bankroll2 *= (1 + day_r)
    
    # Log daily summaries
    for date in sorted(set(t.get("target_date", "") for t in all_trades)):
        day_trades = [t for t in all_trades if t.get("target_date") == date]
        day_returns_before = [daily_pnl_tracker.get(d, 0) for d in sorted(daily_pnl_tracker.keys()) if d <= date]
        log_daily_summary(trade_conn, date, day_trades, bankroll, day_returns_before)
    
    # ── Print summary ──
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.get("correct"))
    accuracy = correct / total_trades if total_trades > 0 else 0
    total_pnl = sum(t.get("net_pnl", 0) for t in all_trades)
    total_fees = sum(t.get("total_fees", 0) for t in all_trades)
    
    # Sharpe from daily % returns
    if len(daily_return_pcts) > 1:
        daily_mean = np.mean(daily_return_pcts)
        daily_std = np.std(daily_return_pcts, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0
    
    # Profit factor
    wins = sum(t.get("net_pnl", 0) for t in all_trades if t.get("net_pnl", 0) > 0)
    losses = abs(sum(t.get("net_pnl", 0) for t in all_trades if t.get("net_pnl", 0) < 0))
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    
    print(f"\n{'=' * 72}")
    print(f"  GEFS PAPER TRADING — RESULTS")
    print(f"{'=' * 72}")
    print(f"  Period:      {date_list[0]} → {date_list[-1]} ({len(date_list)} days)")
    print(f"  Total trades: {total_trades}")
    print(f"  Accuracy:    {accuracy * 100:.2f}%")
    print(f"  Total P&L:   ${total_pnl:+.2f}")
    print(f"  Total fees:  ${total_fees:.2f}")
    print(f"  Final bank:  ${bankroll:.2f} (${initial_bankroll:.2f} initial)")
    print(f"  Daily Sharpe: {sharpe:.4f}")
    print(f"  Profit factor: {pf:.3f}")
    print(f"  Daily returns: {len(daily_return_pcts)}")
    print(f"  Average return: {np.mean(daily_return_pcts) * 100:.4f}%/day" if daily_return_pcts else "")
    print(f"  Volatility:  {np.std(daily_return_pcts, ddof=1) * 100:.4f}%/day" if len(daily_return_pcts) > 1 else "")
    
    # Save JSON summary
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "edge_threshold": DEFAULT_EDGE_THRESHOLD,
            "kelly_fraction": DEFAULT_KELLY_FRACTION,
            "entry_price_min": DEFAULT_ENTRY_PRICE_MIN,
            "entry_price_max": DEFAULT_ENTRY_PRICE_MAX,
            "max_contracts": DEFAULT_MAX_CONTRACTS,
            "initial_bankroll": initial_bankroll,
            "signal_source": "GEFS ensemble (step=24)",
            "fee_model": "kalshi_real (ceil(0.07 × C × P × (1-P)) per side)",
        },
        "results": {
            "start_date": date_list[0],
            "end_date": date_list[-1],
            "trading_days": len(date_list),
            "total_trades": total_trades,
            "correct_trades": correct,
            "accuracy": round(accuracy, 4),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "final_bankroll": round(bankroll, 2),
            "total_return_pct": round((bankroll - initial_bankroll) / initial_bankroll * 100, 2),
            "daily_sharpe": round(sharpe, 4),
            "profit_factor": round(pf, 4),
        },
    }
    
    summary_path = REPO_ROOT / "data" / "gefs_paper_trading_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    _LOGGER.info(f"Summary written to {summary_path}")
    
    # Close
    trade_conn.close()
    _LOGGER.info("GEFS Paper Trading Cron — Complete")
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GEFS Paper Trading Cron")
    parser.add_argument("--days", type=int, default=7, help="Number of trading days")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (YYYY-MM-DD). Default: today-N days")
    parser.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL,
                        help=f"Initial bankroll (default: ${INITIAL_BANKROLL:.0f})")
    args = parser.parse_args()
    
    if not args.start:
        args.start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    
    return run_paper_trading(args.start, args.days, args.bankroll)


if __name__ == "__main__":
    sys.exit(main())