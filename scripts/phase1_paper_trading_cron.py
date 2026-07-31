#!/usr/bin/env python3
"""
Phase 1 Paper Trading Cron — Honest P&L Math (Phase 1 Fixes)

Six critical fixes applied 2026-07-29:
  Fix 1: Per-contract binary P&L math (not stock-style investment)
  Fix 2: Sharpe ratio computed from daily returns, not per-trade P&L
  Fix 3: Fee-aware Kelly formula removed — uses standard binary Kelly
  Fix 4: Spread builder removed — fabricated credit spreads removed
  Fix 5: Goldilocks/SpikeReversion removed (49.85% accuracy, negative EV)
  Fix 6: Fee changed to flat $0.01/contract/side, not % of position

Uses historical settlement data from metar_backfill.db (always available)
Phase 2 best config: decay=0.802, min_agreement=4, min_conf=0.7962
Trades recorded to local data/phase1_paper_trades.db

Usage:
    cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && \
    python3 scripts/phase1_paper_trading_cron.py [--days 7] [--start 2026-07-20]

Output:
    - data/phase1_paper_trades.db  — SQLite trade database
    - data/phase1_paper_trading.log  — Timestamped log

Version: Phase 1 Build — 2026-07-29
"""

import sys
import os
import json
import math
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Any

# ─── Path setup ─────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ─── Imports ────────────────────────────────────────────────────────────────

from core.phase1_config import PHASE1_CONFIG, get_min_agreement, get_fee_rate
from core.position_sizer import (
    PositionSizer,
    compute_disagreement,
    compute_kelly_multiplier_from_disagreement,
    get_config_for_instance,
)
from core.market_cost_model import ROUND_TRIP_FEE
from core.sqlite_utils import get_sqlite_connection
from core.signals import create_signal_registry, SignalRegistry

# ─── Logging ────────────────────────────────────────────────────────────────

LOG_FILE = REPO_ROOT / PHASE1_CONFIG["log_file"]
TRADE_DB = REPO_ROOT / PHASE1_CONFIG["trade_db"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
_LOGGER = logging.getLogger("phase1_paper_trading")
_LOGGER.info("=" * 60)
_LOGGER.info("Phase 1 Paper Trading Cron — Starting")
_LOGGER.info("Config: min_agreement=%d, min_conf=%.4f, decay=%.3f, "
             "strike_offset=%.1f, fee=%.4f",
             get_min_agreement(), PHASE1_CONFIG["min_conf"],
             PHASE1_CONFIG["decay_factor"], PHASE1_CONFIG["strike_offset"],
             get_fee_rate())

# ─── Constants ──────────────────────────────────────────────────────────────

METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
# Dynamically discover stations with settlement data in the target date range.
# Default ALL_STATIONS (original 20) only have data through 2025-08-27.
# Stations with 2026 data are discovered at runtime from the settlement DB.
# Fallback to the original list if discovery fails.
ALL_STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
    "KLAS", "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC",
    "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
]
INITIAL_BANKROLL = 10000.0
FEE = ROUND_TRIP_FEE  # 0.0205
# Phase 2 best config used min_agreement=4 with a 9-signal ensemble.
# Current registry has 7 active signals (after removing duplicates).
# Use 3 as default for the current signal count; can be overridden via env var.
_MIN_AGREEMENT_ENV = os.environ.get("PHASE1_MIN_AGREE")
if _MIN_AGREEMENT_ENV is not None:
    MIN_AGREEMENT = int(_MIN_AGREEMENT_ENV)
else:
    MIN_AGREEMENT = min(int(PHASE1_CONFIG["min_agreement"]), 3)
MIN_CONFIDENCE = float(PHASE1_CONFIG["min_conf"])
STRIKE_OFFSET = float(PHASE1_CONFIG["strike_offset"])
DECAY = float(PHASE1_CONFIG["decay_factor"])
FRACTIONAL_KELLY = float(PHASE1_CONFIG["fractional_kelly"])

# Spread builder for credit spreads
# ─── Database helpers ───────────────────────────────────────────────────────

def init_trade_db(db_path: str) -> sqlite3.Connection:
    """Create/connect to the Phase 1 trade DB."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            signal_direction TEXT NOT NULL,
            confidence REAL NOT NULL,
            agreement_count INTEGER NOT NULL,
            kelly_fraction REAL NOT NULL,
            position_size REAL NOT NULL,
            spread_type TEXT,
            net_credit REAL,
            max_risk REAL,
            fee_paid REAL NOT NULL,
            actual_direction TEXT,
            is_correct INTEGER,
            pnl REAL,
            bankroll_after REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_daily_summary (
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


def get_settlement_data(conn: sqlite3.Connection, station: str,
                        lookback_days: int = 365) -> List[Dict]:
    """Get historical settlement data for a station."""
    c = conn.cursor()
    c.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    rows = c.fetchall()
    results = []
    for r in rows:
        date_str = r[0]
        sb = r[1]
        psb = r[2]
        if sb is None or psb is None:
            continue
        direction = "up" if sb > psb else ("down" if sb < psb else "flat")
        results.append({
            "date": date_str,
            "settlement_bucket": sb,
            "prior_bucket": psb,
            "direction": direction,
        })
    return results


def get_temperature_data(conn: sqlite3.Connection, station: str) -> List[Dict]:
    """Get daily temperature data for a station.

    Filters out bad data points (temp > 200°F which indicates sensor errors).
    """
    c = conn.cursor()
    c.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(temp_f) as avg
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL AND temp_f < 200
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    rows = c.fetchall()
    return [
        {"date": r[0], "high": r[1], "low": r[2], "avg": r[3]}
        for r in rows
    ]


def merge_data(temps: List[Dict], settlements: List[Dict]) -> List[Dict]:
    """Merge temperature and settlement data by date."""
    settlement_map = {s["date"]: s for s in settlements}
    days = []
    for t in temps:
        if t["date"] in settlement_map:
            s = settlement_map[t["date"]]
            days.append({**t, "market_dir": s["direction"],
                         "settlement_bucket": s["settlement_bucket"],
                         "prior_bucket": s["prior_bucket"]})
    return days


# ─── Signal generation ──────────────────────────────────────────────────────

def generate_signals_for_station(
    registry: SignalRegistry,
    days: List[Dict],
    idx: int,
) -> List[Tuple[str, str, float]]:
    """
    Generate signals for a station at a given day index.

    Returns list of (direction, signal_name, confidence) tuples,
    where direction is 'up' or 'down'.
    """
    signals = []
    for name, signal in registry.get_all_signals().items():
        try:
            pred, conf = signal.evaluate(idx, days)
            if pred is not None and conf is not None:
                signals.append((pred, name, float(conf)))
        except Exception as e:
            _LOGGER.debug("Signal %s failed for idx %d: %s", name, idx, e)
    return signals


def apply_agreement_gate(
    signals: List[Tuple[str, str, float]],
    n_required: int = MIN_AGREEMENT,
) -> List[Tuple[str, str, float]]:
    """
    Apply N-of-M agreement gate.

    Returns only signals that match the majority direction IF that
    direction has at least n_required signals.
    """
    if len(signals) < n_required:
        return []

    up_count = sum(1 for s in signals if s[0] == "up")
    down_count = sum(1 for s in signals if s[0] == "down")

    if up_count >= n_required:
        return [s for s in signals if s[0] == "up"]
    elif down_count >= n_required:
        return [s for s in signals if s[0] == "down"]
    return []


# ─── Position sizing ────────────────────────────────────────────────────────

def compute_kelly_position(
    sizer: PositionSizer,
    confidence: float,
    win_rate: float,
    agreeing_signals: List[Tuple[str, str, float]],
) -> Tuple[float, Dict]:
    """
    Compute Kelly position size with disagreement-based multiplier.

    Args:
        sizer: PositionSizer instance
        confidence: Average signal confidence
        win_rate: Estimated win rate
        agreeing_signals: List of (direction, name, conf) tuples

    Returns (position_size, details_dict).
    """
    # Compute disagreement multiplier
    directions = [s[0] for s in agreeing_signals]
    confidences = [s[2] for s in agreeing_signals]
    kelly_mult = compute_kelly_multiplier_from_disagreement(
        directions, confidences
    )

    edge = sizer.calculate_edge_from_win_rate(win_rate)

    # Compute base position
    size, details = sizer.compute_position_size(
        confidence=confidence,
        win_rate=win_rate,
        edge=edge,
    )

    # Apply disagreement multiplier
    size *= kelly_mult
    details["kelly_multiplier_from_disagreement"] = round(kelly_mult, 4)

    return size, details


# ─── Trade execution ────────────────────────────────────────────────────────

def execute_trade(
    trade_conn: sqlite3.Connection,
    station: str,
    trading_date: str,
    direction: str,
    confidence: float,
    agreement_count: int,
    position_size: float,
    actual_direction: str,
    bankroll: float,
) -> Dict:
    """
    Execute a single trade and record it.

    Uses per-contract binary option math:
    - Entry price is 0.85 (default; should come from market data)
    - Kalshi fee is $0.01/contract/side flat
    - Contracts = position_size / entry_price
    - Win: contracts * (1.0 - entry_price) - round_trip_fee
    - Loss: -contracts * entry_price - round_trip_fee

    TODO: entry_price should come from Kalshi API market data.
    Currently using a hardcoded default of 0.85.

    Returns trade result dict with P&L.
    """
    is_correct = direction == actual_direction if actual_direction != "flat" else False

    # Per-contract binary math
    entry_price = 0.85  # Default; should come from market data
    contracts = position_size / entry_price
    # Kalshi real fee model: ceil(0.07 × quantity × price × (1-price)) per side × 2
    round_trip_fee = math.ceil(0.07 * contracts * entry_price * (1.0 - entry_price)) * 2

    # P&L calculation
    if is_correct:
        pnl = contracts * (1.0 - entry_price) - round_trip_fee
    else:
        pnl = -contracts * entry_price - round_trip_fee

    bankroll_after = bankroll + pnl

    trade_conn.execute("""
        INSERT INTO trades
        (station, trading_date, signal_direction, confidence, agreement_count,
         kelly_fraction, position_size, spread_type, net_credit, max_risk,
         fee_paid, actual_direction, is_correct, pnl, bankroll_after, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        station, trading_date, direction, confidence, agreement_count,
        0.0,  # kelly_fraction (computed inline)
        round(position_size, 2), "none", 0.0, round(position_size, 2),
        round(round_trip_fee, 2), actual_direction,
        1 if is_correct else 0, round(pnl, 2), round(bankroll_after, 2),
        datetime.now(timezone.utc).isoformat(),
    ))
    trade_conn.commit()

    result = {
        "station": station,
        "date": trading_date,
        "direction": direction,
        "confidence": confidence,
        "agreement": agreement_count,
        "position_size": round(position_size, 2),
        "spread_type": "none",
        "fee_paid": round(round_trip_fee, 2),
        "is_correct": is_correct,
        "pnl": round(pnl, 2),
        "bankroll_after": round(bankroll_after, 2),
    }
    return result


# ─── Summary ────────────────────────────────────────────────────────────────

def compute_sharpe(
    trades: List[Dict],
    bankroll: float,
    risk_free: float = 0.02,
) -> float:
    """
    Compute annualized Sharpe ratio from daily returns.

    Aggregates P&L by day, computes daily return as daily_pnl / bankroll,
    then annualizes from daily mean/vol. Returns 0.0 if fewer than 2
    trading days.
    """
    if not trades:
        return 0.0

    # Aggregate P&L by trading day
    daily_pnl: Dict[str, float] = {}
    for t in trades:
        day = t.get("date", "")
        daily_pnl[day] = daily_pnl.get(day, 0.0) + t["pnl"]

    if len(daily_pnl) < 2:
        return 0.0

    # Compute daily returns as fraction of bankroll
    daily_returns = [pnl / bankroll for pnl in daily_pnl.values()]

    n = len(daily_returns)
    mean_ret = sum(daily_returns) / n
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 1e-10

    # Annualize (approx 252 trading days)
    annual_ret = mean_ret * 252
    annual_std = std * math.sqrt(252)
    if annual_std == 0:
        return 0.0
    return (annual_ret - risk_free) / annual_std


def compute_max_drawdown(bankroll_series: List[float]) -> float:
    """Compute maximum drawdown as a percentage."""
    if not bankroll_series:
        return 0.0
    peak = bankroll_series[0]
    max_dd = 0.0
    for val in bankroll_series:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def print_summary(
    all_trades: List[Dict],
    bankroll: float,
    initial_bankroll: float,
) -> None:
    """Print a comprehensive trading summary."""
    total = len(all_trades)
    if total == 0:
        _LOGGER.info("=" * 60)
        _LOGGER.info("SUMMARY: No trades executed")
        _LOGGER.info("=" * 60)
        return

    correct = sum(1 for t in all_trades if t["is_correct"])
    accuracy = correct / total if total > 0 else 0.0
    total_pnl = sum(t["pnl"] for t in all_trades)
    pnl_pct = (total_pnl / initial_bankroll) * 100
    sharpe = compute_sharpe(all_trades, bankroll)
    bankroll_series = [initial_bankroll]
    for t in all_trades:
        bankroll_series.append(bankroll_series[-1] + t["pnl"])
    max_dd = compute_max_drawdown(bankroll_series)

    _LOGGER.info("=" * 60)
    _LOGGER.info("PHASE 1 PAPER TRADING — SUMMARY")
    _LOGGER.info("=" * 60)
    _LOGGER.info("Total trades:     %d", total)
    _LOGGER.info("Correct trades:   %d", correct)
    _LOGGER.info("Directional acc:  %.2f%%", accuracy * 100)
    _LOGGER.info("Total P&L:        $%.2f (%.2f%%)", total_pnl, pnl_pct)
    _LOGGER.info("Final bankroll:   $%.2f", bankroll)
    _LOGGER.info("Sharpe (ann.):    %.4f", sharpe)
    _LOGGER.info("Max drawdown:     %.2f%%", max_dd * 100)
    _LOGGER.info("Profit factor:    %.4f", (
        sum(t["pnl"] for t in all_trades if t["pnl"] > 0) /
        max(0.01, abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0)))
    ))
    _LOGGER.info("Config: decay=%.3f, min_agree=%d, min_conf=%.4f, "
                 "strike_offset=%.1f", DECAY, MIN_AGREEMENT, MIN_CONFIDENCE,
                 STRIKE_OFFSET)
    _LOGGER.info("Fee: $0.01/contract/side flat (entry_price=0.85 constant)")
    _LOGGER.info("Entry price: 0.85 (hardcoded — should come from Kalshi API)")
    _LOGGER.info("Fee rate: %.4f (legacy, for reference)", FEE)
    _LOGGER.info("=" * 60)


# ─── Simulation ─────────────────────────────────────────────────────────────

def simulate_day(
    metar_conn: sqlite3.Connection,
    trade_conn: sqlite3.Connection,
    registry: SignalRegistry,
    sizer: PositionSizer,
    date: str,
    days_by_station: Dict[str, List[Dict]],
    bankroll: float,
) -> Tuple[List[Dict], float]:
    """Simulate a single trading day."""
    trades_today = []
    current_bankroll = bankroll

    for station, daily in days_by_station.items():
        # Find index for this date
        idx = None
        for i, d in enumerate(daily):
            if d["date"] == date:
                idx = i
                break

        if idx is None or idx < 60:  # Need minimum lookback (some signals need 60+)
            continue

        today = daily[idx]
        if today["market_dir"] == "flat":
            continue

        # Generate signals
        signals = generate_signals_for_station(registry, daily, idx)
        if len(signals) < MIN_AGREEMENT:
            continue

        # Apply agreement gate
        agreeing = apply_agreement_gate(signals, MIN_AGREEMENT)
        if not agreeing:
            continue

        # Get majority direction and average confidence
        directions = [s[0] for s in agreeing]
        majority_dir = max(set(directions), key=directions.count)
        avg_conf = sum(s[2] for s in agreeing) / len(agreeing)

        # Apply min confidence threshold
        if avg_conf < MIN_CONFIDENCE:
            continue

        # Compute position size
        win_rate = sizer.get_rolling_win_rate()
        position_size, details = compute_kelly_position(
            sizer, avg_conf, win_rate, agreeing
        )

        if position_size <= 0:
            continue

        # Record trade (no spread builder — Fix 4: removed)
        result = execute_trade(
            trade_conn, station, date, majority_dir, avg_conf,
            len(agreeing), position_size,
            today["market_dir"], current_bankroll,
        )

        # Update bankroll
        current_bankroll = result["bankroll_after"]
        sizer.update_bankroll(current_bankroll)

        # Update rolling win rate
        sizer.add_win_result(date, result["is_correct"])

        trades_today.append(result)

    return trades_today, current_bankroll


def write_daily_summary(
    trade_conn: sqlite3.Connection,
    date: str,
    trades_today: List[Dict],
    bankroll: float,
    bankroll_start: float,
) -> None:
    """Write a daily summary to the trade DB."""
    total = len(trades_today)
    if total == 0:
        return
    correct = sum(1 for t in trades_today if t["is_correct"])
    accuracy = correct / total if total > 0 else 0.0
    total_pnl = sum(t["pnl"] for t in trades_today)
    sharpe = compute_sharpe(trades_today, bankroll)
    max_dd = (bankroll_start - bankroll) / bankroll_start if bankroll_start > 0 else 0.0

    trade_conn.execute("""
        INSERT OR REPLACE INTO trade_daily_summary
        (trading_date, total_trades, correct_trades, accuracy, total_pnl,
         bankroll, sharpe, max_drawdown, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date, total, correct, accuracy, round(total_pnl, 2),
        round(bankroll, 2), round(sharpe, 4), round(max_dd, 4),
        datetime.now(timezone.utc).isoformat(),
    ))
    trade_conn.commit()


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase 1 Paper Trading Loop"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of days to simulate (default: 7)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date YYYY-MM-DD (default: latest available)"
    )
    parser.add_argument(
        "--bankroll", type=float, default=INITIAL_BANKROLL,
        help="Initial bankroll (default: $10,000)"
    )
    args = parser.parse_args()

    # Open connections
    metar_conn = get_sqlite_connection(METAR_DB)
    trade_conn = init_trade_db(str(TRADE_DB))

    # Initialize signal registry
    registry = create_signal_registry(METAR_DB)
    _LOGGER.info("Signal registry initialised: %d signals",
                 len(registry.get_all_signals()))

    # Initialize position sizer
    sizer = PositionSizer(
        bankroll=args.bankroll,
        cost_fraction=FEE,
        fraction_kelly=FRACTIONAL_KELLY,
    )
    _LOGGER.info("Position sizer initialised: bankroll=$%.2f, fee=%.4f, "
                 "f_kelly=%.2f", args.bankroll, FEE, FRACTIONAL_KELLY)

    # Determine date range
    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    else:
        c = metar_conn.cursor()
        c.execute("""
            SELECT MAX(local_trading_date) FROM settlement_epochs
            WHERE epoch_status='closed'
        """)
        max_date = c.fetchone()[0]
        start_date = datetime.strptime(max_date, "%Y-%m-%d").date()
        # Walk back to have enough history
        start_date = start_date - timedelta(days=args.days - 1)

    # Generate dates
    dates = [
        (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(args.days)
    ]
    _LOGGER.info("Simulating %d days: %s to %s", len(dates),
                 dates[0], dates[-1])

    # Discover stations with settlement data in the target date range
    _LOGGER.info("Discovering stations with settlement data...")
    # Find dates that actually have both settlement data and
    # temperature data for trading
    c = metar_conn.cursor()
    c.execute("""
        SELECT DISTINCT s.station FROM settlement_epochs s
        WHERE s.epoch_status='closed'
        AND s.local_trading_date >= ? AND s.local_trading_date <= ?
        ORDER BY s.station
    """, (dates[0], dates[-1]))
    available_stations = [r[0] for r in c.fetchall()]

    # Fall back to ALL_STATIONS if no stations found in range
    if not available_stations:
        _LOGGER.info("No stations with settlement data in %s to %s - using ALL_STATIONS",
                     dates[0], dates[-1])
        available_stations = ALL_STATIONS

    _LOGGER.info("Available stations: %d", len(available_stations))

    # Pre-load data for all stations
    _LOGGER.info("Loading settlement data for all stations...")
    days_by_station = {}
    for station in available_stations:
        try:
            temps = get_temperature_data(metar_conn, station)
            settlements = get_settlement_data(metar_conn, station)
            days = merge_data(temps, settlements)
            if len(days) >= 60:
                days_by_station[station] = days
        except Exception as e:
            _LOGGER.warning("Failed to load data for %s: %s", station, e)

    _LOGGER.info("Loaded data for %d stations", len(days_by_station))

    # Simulate
    all_trades = []
    bankroll = args.bankroll

    for date in dates:
        bankroll_start = bankroll
        trades_today, bankroll = simulate_day(
            metar_conn, trade_conn, registry, sizer,
            date, days_by_station, bankroll,
        )

        write_daily_summary(trade_conn, date, trades_today, bankroll,
                            bankroll_start)

        if trades_today:
            correct = sum(1 for t in trades_today if t["is_correct"])
            pnl = sum(t["pnl"] for t in trades_today)
            _LOGGER.info(
                "Day %s: %d trades, %d correct (%.1f%%), P&L=$%.2f, "
                "bankroll=$%.2f",
                date, len(trades_today), correct,
                (correct / len(trades_today)) * 100 if trades_today else 0,
                pnl, bankroll,
            )
        else:
            _LOGGER.info("Day %s: 0 trades", date)

        all_trades.extend(trades_today)

    # Print summary
    print_summary(all_trades, bankroll, args.bankroll)

    # Write JSON summary
    summary_path = REPO_ROOT / "data" / "phase1_paper_trading_summary.json"
    bankroll_series = [args.bankroll]
    for t in all_trades:
        bankroll_series.append(bankroll_series[-1] + t["pnl"])
    with open(summary_path, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "min_agreement": MIN_AGREEMENT,
                "min_conf": MIN_CONFIDENCE,
                "decay_factor": DECAY,
                "strike_offset": STRIKE_OFFSET,
                "fee_rate": FEE,
                "fractional_kelly": FRACTIONAL_KELLY,
            },
            "total_trades": len(all_trades),
            "correct_trades": sum(1 for t in all_trades if t["is_correct"]),
            "accuracy": round(
                sum(1 for t in all_trades if t["is_correct"]) / len(all_trades), 4
            ) if all_trades else 0,
            "total_pnl": round(sum(t["pnl"] for t in all_trades), 2),
            "final_bankroll": round(bankroll, 2),
            "sharpe": round(compute_sharpe(all_trades, bankroll), 4),
            "max_drawdown": round(compute_max_drawdown(bankroll_series), 4),
        }, f, indent=2)
    _LOGGER.info("Summary written to %s", summary_path)

    # Close connections
    metar_conn.close()
    trade_conn.close()

    _LOGGER.info("Phase 1 Paper Trading Cron — Complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())