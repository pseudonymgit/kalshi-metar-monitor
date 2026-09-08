#!/usr/bin/env python3
"""
PnL Dashboard — reads paper_trading.db and computes P&L per station, per day, overall.

Reads from paper_trades (intraday) and trades (settled) tables.
Computes: total_pnl, win_rate, sharpe, max_drawdown, per_station breakdown.

Step 8 in the daemon cycle — wire after exit logic.
"""

import json
import logging
import math
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRADING_DB = Path(__file__).resolve().parent.parent / "data" / "paper_trading.db"


def _get_connection(db_path: str = None) -> sqlite3.Connection:
    """Get read-only SQLite connection."""
    path = db_path or str(TRADING_DB)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _compute_sharpe(pnl_series: List[float], risk_free_rate: float = 0.0) -> Optional[float]:
    """Compute annualized Sharpe ratio from a series of PnL values.

    Args:
        pnl_series: List of per-trade or per-day PnL values
        risk_free_rate: Risk-free rate (annualized, default 0%)

    Returns:
        Sharpe ratio, or None if insufficient data.
    """
    if len(pnl_series) < 2:
        return None
    mean_pnl = statistics.mean(pnl_series)
    std_pnl = statistics.stdev(pnl_series)
    if std_pnl < 1e-9:
        return 0.0
    # Assuming daily returns, scale by sqrt(252) for annualized
    sharpe = (mean_pnl - risk_free_rate) / std_pnl
    return sharpe


def _compute_max_drawdown(balance_series: List[Tuple[str, float]]) -> Tuple[float, str, str]:
    """Compute maximum drawdown from a series of (date, balance) tuples.

    Args:
        balance_series: List of (date, balance) tuples in chronological order

    Returns:
        (max_drawdown_pct, peak_date, trough_date)
    """
    if len(balance_series) < 2:
        return 0.0, "", ""

    peak = balance_series[0][1]
    peak_date = balance_series[0][0]
    max_dd = 0.0
    max_dd_peak_date = peak_date
    max_dd_trough_date = peak_date

    for date_str, balance in balance_series[1:]:
        if balance > peak:
            peak = balance
            peak_date = date_str
        dd = (peak - balance) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_peak_date = peak_date
            max_dd_trough_date = date_str

    return max_dd, max_dd_peak_date, max_dd_trough_date


def _get_paper_trade_pnl(conn: sqlite3.Connection) -> List[Dict]:
    """Get all intraday paper trades with PnL.

    Args:
        conn: Open connection to paper_trading.db

    Returns:
        List of trade dicts with station, date, pnl, direction, etc.
    """
    c = conn.cursor()
    c.execute("""
        SELECT station, date_utc, hour_utc, direction, contracts,
               entry_price, market_price, edge_pp, status, pnl
        FROM paper_trades
        ORDER BY created_at_utc
    """)
    trades = []
    for row in c.fetchall():
        trades.append({
            "station": row[0],
            "date_utc": row[1],
            "hour_utc": row[2],
            "direction": row[3],
            "contracts": row[4],
            "entry_price": row[5],
            "market_price": row[6],
            "edge_pp": row[7],
            "status": row[8],
            "pnl": row[9],
        })
    return trades


def _get_settled_trade_pnl(conn: sqlite3.Connection) -> List[Dict]:
    """Get settled trades with realized PnL.

    Args:
        conn: Open connection to paper_trading.db

    Returns:
        List of trade dicts with station, date, pnl, direction, etc.
    """
    c = conn.cursor()
    c.execute("""
        SELECT station, trade_date_utc, signal_direction, market_type,
               quantity, market_price, trade_price, trade_cost,
               realized_pnl, settlement_return_amount, status, settled_value
        FROM trades
        WHERE status != 'open'
        ORDER BY created_at_utc
    """)
    trades = []
    for row in c.fetchall():
        trades.append({
            "station": row[0],
            "date_utc": row[1],
            "signal_direction": row[2],
            "market_type": row[3],
            "quantity": row[4],
            "market_price": row[5],
            "trade_price": row[6],
            "trade_cost": row[7],
            "realized_pnl": row[8],
            "settlement_return": row[9],
            "status": row[10],
            "settled_value": row[11],
        })
    return trades


def _get_daily_balances(conn: sqlite3.Connection) -> List[Tuple[str, float]]:
    """Get daily balance history.

    Args:
        conn: Open connection to paper_trading.db

    Returns:
        List of (date_utc, closing_balance) tuples in chronological order.
    """
    c = conn.cursor()
    c.execute("""
        SELECT date_utc, closing_balance, pnl
        FROM daily_balances
        ORDER BY date_utc
    """)
    return [(row[0], row[1]) for row in c.fetchall()]


def run_pnl_dashboard(db_path: str = None) -> Dict:
    """Reads paper_trading.db, computes P&L per station/per day/overall.

    Args:
        db_path: Path to paper_trading.db (default: data/paper_trading.db)

    Returns:
        Dict with:
            total_pnl: float
            total_trades: int
            win_rate: float (0-1)
            sharpe: float or None
            max_drawdown_pct: float
            per_station: dict[str, dict]
            per_day: dict[str, dict]
    """
    dashboard = {
        "report_time": datetime.now(timezone.utc).isoformat(),
        "total_pnl": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "sharpe": None,
        "max_drawdown_pct": 0.0,
        "per_station": {},
        "per_day": {},
        "open_positions": 0,
    }

    try:
        conn = _get_connection(db_path)
    except Exception as e:
        logger.warning(f"Cannot open paper_trading.db at {db_path}: {e}")
        return dashboard

    # ─── Paper trades (intraday) ────────────────────────────────────
    paper_trades = _get_paper_trade_pnl(conn)

    # Compute open positions count from paper_trades
    open_paper = [t for t in paper_trades if t["status"] == "open"]
    dashboard["open_positions"] = len(open_paper)

    # Closed paper trades PnL
    closed_paper = [t for t in paper_trades if t["status"] == "closed" and t["pnl"] is not None]
    for t in closed_paper:
        pnl = float(t["pnl"])
        dashboard["total_pnl"] += pnl
        st = t["station"]
        if st not in dashboard["per_station"]:
            dashboard["per_station"][st] = {
                "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0
            }
        dashboard["per_station"][st]["trades"] += 1
        dashboard["per_station"][st]["pnl"] += pnl
        if pnl > 0:
            dashboard["per_station"][st]["wins"] += 1
        elif pnl < 0:
            dashboard["per_station"][st]["losses"] += 1

    # ─── Settled trades ─────────────────────────────────────────────
    settled_trades = _get_settled_trade_pnl(conn)
    for t in settled_trades:
        pnl = float(t["realized_pnl"] or 0.0)
        dashboard["total_pnl"] += pnl
        st = t["station"]
        if st not in dashboard["per_station"]:
            dashboard["per_station"][st] = {
                "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0
            }
        dashboard["per_station"][st]["trades"] += 1
        dashboard["per_station"][st]["pnl"] += pnl
        if pnl > 0:
            dashboard["per_station"][st]["wins"] += 1
        elif pnl < 0:
            dashboard["per_station"][st]["losses"] += 1

    # ─── Aggregate ──────────────────────────────────────────────────
    all_trades = closed_paper + [t for t in settled_trades]
    dashboard["total_trades"] = len(all_trades)
    dashboard["win_rate"] = _compute_win_rate(all_trades)

    # Per-day aggregation
    for t in all_trades:
        date_key = t["date_utc"][:10]
        pnl_val = float(t.get("pnl") or t.get("realized_pnl") or 0.0)
        if date_key not in dashboard["per_day"]:
            dashboard["per_day"][date_key] = {"pnl": 0.0, "trades": 0, "wins": 0}
        dashboard["per_day"][date_key]["pnl"] += pnl_val
        dashboard["per_day"][date_key]["trades"] += 1
        if pnl_val > 0:
            dashboard["per_day"][date_key]["wins"] += 1

    # Sharpe ratio from per-trade PnL series
    pnl_series = [float(t.get("pnl") or t.get("realized_pnl") or 0.0) for t in all_trades]
    dashboard["sharpe"] = _compute_sharpe(pnl_series)

    # Max drawdown from daily balances
    daily_balances = _get_daily_balances(conn)
    if daily_balances:
        dd_pct, peak_d, trough_d = _compute_max_drawdown(daily_balances)
        dashboard["max_drawdown_pct"] = dd_pct
        dashboard["max_drawdown_peak_date"] = peak_d
        dashboard["max_drawdown_trough_date"] = trough_d
        dashboard["current_balance"] = daily_balances[-1][1] if daily_balances else 0.0
        dashboard["peak_balance"] = max(b for _, b in daily_balances)

    conn.close()

    # Compute per-station win rates
    for st in dashboard["per_station"]:
        s = dashboard["per_station"][st]
        s["win_rate"] = s["wins"] / s["trades"] if s["trades"] > 0 else 0.0

    logger.info(
        f"PnL dashboard: total=${dashboard['total_pnl']:.2f} "
        f"trades={dashboard['total_trades']} "
        f"win_rate={dashboard['win_rate']:.1%} "
        f"sharpe={dashboard['sharpe'] or 'N/A'} "
        f"max_dd={dashboard['max_drawdown_pct']:.1%}"
    )
    return dashboard


def _compute_win_rate(trades: List[Dict]) -> float:
    """Compute win rate (fraction of trades with positive PnL)."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (float(t.get("pnl") or t.get("realized_pnl") or 0.0)) > 0)
    return wins / len(trades)


def persist_pnl_report(report: dict, path: str):
    """Writes PnL report to JSON file.

    Args:
        report: Dict from run_pnl_dashboard()
        path: Path to output JSON file
    """
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"PnL report written: {path} ({len(json.dumps(report, default=str))} bytes)")


# ── Standalone test ────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    report = run_pnl_dashboard()
    persist_pnl_report(report, str(Path(__file__).resolve().parent.parent / "reports" / "paper_pnl.json"))
    print(json.dumps(report, indent=2, default=str))