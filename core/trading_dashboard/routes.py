"""
Trading Dashboard Routes — Phase 17

Flask Blueprint with all trading dashboard API endpoints and page routes.

Endpoints:
  GET  /trading/                     — Main dashboard page
  GET  /trading/positions            — Positions panel page
  GET  /trading/api/pnl              — P&L summary (total, by city, time series)
  GET  /trading/api/positions        — Open positions with current prices
  GET  /trading/api/portfolio        — Total exposure, by cluster
  GET  /trading/api/alerts           — Last 50 journal entries, filterable
  GET  /trading/api/risk             — Risk state (OK/WARNING/KILL_SWITCH)
  GET  /trading/api/stats            — Signal accuracy, win rate, rolling Sharpe
  GET  /trading/api/stream           — SSE endpoint for real-time updates (30s push)
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Blueprint, Response, jsonify, render_template, request, stream_with_context,
)

from core.market_cost_model import MARKET_COST_MODEL

_LOGGER = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPER_DB_PATH = str(REPO_ROOT / "data" / "paper_trading.db")
JOURNAL_DB_PATH = str(REPO_ROOT / "data" / "trade_journal.db")

# ─── Blueprint ────────────────────────────────────────────────────────────

trading_bp = Blueprint(
    "trading_dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/trading/static",
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _get_paper_db():
    """Get a read-only connection to paper_trading.db."""
    conn = get_readonly_sqlite_connection(PAPER_DB_PATH, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _get_journal_db():
    """Get a read-only connection to trade_journal.db."""
    conn = get_readonly_sqlite_connection(JOURNAL_DB_PATH, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _db_exists(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ─── Page Routes ──────────────────────────────────────────────────────────

@trading_bp.route("/")
def index():
    """Main dashboard page."""
    return render_template("index.html")


@trading_bp.route("/positions")
def positions_page():
    """Positions panel page."""
    return render_template("positions.html")


# ─── API: P&L ─────────────────────────────────────────────────────────────

@trading_bp.route("/api/pnl")
def api_pnl():
    """
    P&L summary endpoint.

    Returns:
        total_pnl: Total realized P&L from daily_balances
        by_city: Dict of station -> pnl from trades
        daily_series: List of {date, pnl, cumulative_pnl} for line chart
        current_balance: Latest closing balance
    """
    if not _db_exists(PAPER_DB_PATH):
        return jsonify({
            "total_pnl": 0.0,
            "by_city": {},
            "daily_series": [],
            "current_balance": 10000.0,
            "initial_balance": 10000.0,
            "error": "paper_trading.db not found",
        })

    try:
        conn = _get_paper_db()

        # Current balance from latest daily_balances
        row = conn.execute("""
            SELECT closing_balance FROM daily_balances
            ORDER BY date_utc DESC LIMIT 1
        """).fetchone()
        current_balance = _safe_float(row["closing_balance"] if row else None, 10000.0)

        # Initial balance
        row = conn.execute("""
            SELECT opening_balance FROM daily_balances
            ORDER BY date_utc ASC LIMIT 1
        """).fetchone()
        initial_balance = _safe_float(row["opening_balance"] if row else None, 10000.0)

        # Daily P&L series
        daily_rows = conn.execute("""
            SELECT date_utc, pnl FROM daily_balances
            ORDER BY date_utc ASC
        """).fetchall()

        daily_series = []
        cumulative = 0.0
        for dr in daily_rows:
            pnl_val = _safe_float(dr["pnl"])
            cumulative += pnl_val
            daily_series.append({
                "date": dr["date_utc"],
                "pnl": round(pnl_val, 2),
                "cumulative_pnl": round(cumulative, 2),
            })

        total_pnl = round(cumulative, 2) if daily_series else 0.0

        # P&L by station from trades table
        by_city = {}
        try:
            city_rows = conn.execute("""
                SELECT station, SUM(realized_pnl) as pnl
                FROM trades
                WHERE status = 'closed' AND realized_pnl IS NOT NULL
                GROUP BY station
                ORDER BY pnl DESC
            """).fetchall()
            for cr in city_rows:
                by_city[cr["station"]] = round(_safe_float(cr["pnl"]), 2)
        except Exception:
            pass

        conn.close()

        return jsonify({
            "total_pnl": total_pnl,
            "by_city": by_city,
            "daily_series": daily_series,
            "current_balance": round(current_balance, 2),
            "initial_balance": round(initial_balance, 2),
        })

    except Exception as e:
        _LOGGER.error("P&L API error: %s", e)
        return jsonify({
            "total_pnl": 0.0,
            "by_city": {},
            "daily_series": [],
            "current_balance": 10000.0,
            "initial_balance": 10000.0,
            "error": str(e),
        })


# ─── API: Positions ───────────────────────────────────────────────────────

@trading_bp.route("/api/positions")
def api_positions():
    """
    Open positions endpoint.

    Returns:
        positions: List of open positions with entry price, current price, unrealized P&L
        open_count: Number of open positions
        total_unrealized_pnl: Sum of unrealized P&L
    """
    if not _db_exists(PAPER_DB_PATH):
        return jsonify({"positions": [], "open_count": 0, "total_unrealized_pnl": 0.0})

    try:
        conn = _get_paper_db()

        rows = conn.execute("""
            SELECT position_uuid, station, market_type, market_side,
                   average_cost, quantity, market_price,
                   mark_to_market_value, realized_pnl, unrealized_pnl,
                   total_pnl, opened_date_utc, last_updated_utc, status
            FROM positions
            WHERE status IN ('active', 'closed_partial')
            ORDER BY opened_date_utc DESC
        """).fetchall()

        positions = []
        for r in rows:
            positions.append({
                "position_uuid": r["position_uuid"],
                "station": r["station"],
                "market_type": r["market_type"],
                "market_side": r["market_side"],
                "average_cost": round(_safe_float(r["average_cost"]), 4),
                "quantity": r["quantity"],
                "market_price": round(_safe_float(r["market_price"]), 4),
                "mark_to_market_value": round(_safe_float(r["mark_to_market_value"]), 2),
                "realized_pnl": round(_safe_float(r["realized_pnl"]), 2),
                "unrealized_pnl": round(_safe_float(r["unrealized_pnl"]), 2),
                "total_pnl": round(_safe_float(r["total_pnl"]), 2),
                "opened_date_utc": r["opened_date_utc"],
                "last_updated_utc": r["last_updated_utc"],
                "status": r["status"],
            })

        total_unrealized = sum(p["unrealized_pnl"] for p in positions)

        conn.close()

        return jsonify({
            "positions": positions,
            "open_count": len(positions),
            "total_unrealized_pnl": round(total_unrealized, 2),
        })

    except Exception as e:
        _LOGGER.error("Positions API error: %s", e)
        return jsonify({"positions": [], "open_count": 0, "total_unrealized_pnl": 0.0, "error": str(e)})


# ─── API: Portfolio ───────────────────────────────────────────────────────

@trading_bp.route("/api/portfolio")
def api_portfolio():
    """
    Portfolio summary endpoint.

    Returns:
        total_exposure: Sum of mark_to_market_value on open positions
        by_cluster: Dict of cluster -> exposure
        by_station: Dict of station -> exposure
        spread_cost: Current spread cost from market_cost_model
    """
    if not _db_exists(PAPER_DB_PATH):
        return jsonify({
            "total_exposure": 0.0,
            "by_cluster": {},
            "by_station": {},
            "spread_cost": MARKET_COST_MODEL.spread,
        })

    try:
        conn = _get_paper_db()

        rows = conn.execute("""
            SELECT station, market_type, mark_to_market_value, quantity
            FROM positions
            WHERE status IN ('active', 'closed_partial')
        """).fetchall()

        total_exposure = 0.0
        by_station = {}
        for r in rows:
            val = abs(_safe_float(r["mark_to_market_value"]))
            total_exposure += val
            station = r["station"]
            by_station[station] = round(by_station.get(station, 0.0) + val, 2)

        # Resolve cluster mapping
        try:
            from core.station_registry import get_cluster_for_station
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
            by_cluster = {}
            for station, exposure in by_station.items():
                cluster = get_cluster_for_station(station) or "UNKNOWN"
                by_cluster[cluster] = round(by_cluster.get(cluster, 0.0) + exposure, 2)
        except ImportError:
            by_cluster = {}

        conn.close()

        return jsonify({
            "total_exposure": round(total_exposure, 2),
            "by_cluster": by_cluster,
            "by_station": by_station,
            "spread_cost": MARKET_COST_MODEL.spread,
        })

    except Exception as e:
        _LOGGER.error("Portfolio API error: %s", e)
        return jsonify({
            "total_exposure": 0.0,
            "by_cluster": {},
            "by_station": {},
            "spread_cost": MARKET_COST_MODEL.spread,
            "error": str(e),
        })


# ─── API: Alerts ──────────────────────────────────────────────────────────

@trading_bp.route("/api/alerts")
def api_alerts():
    """
    Alert feed endpoint.

    Query params:
        station: Optional station filter
        outcome: Optional outcome filter
        limit: Max results (default 50, max 200)

    Returns:
        alerts: List of journal entries, reverse-chronological
        color-coded outcome classes (green=executed, red=skipped, yellow=error)
    """
    if not _db_exists(JOURNAL_DB_PATH):
        return jsonify({"alerts": [], "count": 0})

    try:
        station = request.args.get("station", "").strip().upper() or None
        outcome = request.args.get("outcome", "").strip() or None
        try:
            limit = min(int(request.args.get("limit", "50")), 200)
        except (TypeError, ValueError):
            limit = 50

        conn = _get_journal_db()

        conditions = ["1=1"]
        params = []

        if station:
            conditions.append("station = ?")
            params.append(station)
        if outcome:
            conditions.append("outcome = ?")
            params.append(outcome)

        where = " AND ".join(conditions)
        rows = conn.execute(f"""
            SELECT id, timestamp_utc, station, market, direction,
                   signal_ids, confidence, edge, market_prob, lane,
                   outcome, alert_id, failure_mode, position_size,
                   trade_version, functionality, metadata_json
            FROM trade_journal
            WHERE {where}
            ORDER BY timestamp_utc DESC
            LIMIT ?
        """, (*params, limit)).fetchall()

        OUTCOME_COLORS = {
            "EXECUTED": "green",
            "SETTLED_WIN": "green",
            "SETTLED_LOSS": "red",
            "SKIPPED_EDGE": "red",
            "SKIPPED_COST": "red",
            "SKIPPED_CONFIDENCE": "red",
            "SKIPPED_FILTER": "red",
            "SKIPPED_COOLDOWN": "yellow",
            "SKIPPED_RISK": "yellow",
            "SKIPPED_STATION": "yellow",
            "SKIPPED_SKILL": "yellow",
            "SKIPPED_WINDOW": "yellow",
            "SKIPPED_CLUSTER": "yellow",
            "SKIPPED_CITY_PAIR": "yellow",
            "SKIPPED_SIZE_ZERO": "yellow",
            "ERROR": "yellow",
            "EXPIRED": "red",
        }

        alerts = []
        for r in rows:
            outcome_str = r["outcome"] or "UNKNOWN"
            alerts.append({
                "id": r["id"],
                "timestamp_utc": r["timestamp_utc"],
                "station": r["station"],
                "market": r["market"],
                "direction": r["direction"],
                "signal_ids": r["signal_ids"],
                "confidence": r["confidence"],
                "edge": r["edge"],
                "market_prob": r["market_prob"],
                "lane": r["lane"],
                "outcome": outcome_str,
                "outcome_color": OUTCOME_COLORS.get(outcome_str, "yellow"),
                "alert_id": r["alert_id"],
                "failure_mode": r["failure_mode"],
                "position_size": r["position_size"],
                "trade_version": r["trade_version"],
                "functionality": r["functionality"],
            })

        conn.close()

        return jsonify({
            "alerts": alerts,
            "count": len(alerts),
        })

    except Exception as e:
        _LOGGER.error("Alerts API error: %s", e)
        return jsonify({"alerts": [], "count": 0, "error": str(e)})


# ─── API: Risk ────────────────────────────────────────────────────────────

@trading_bp.route("/api/risk")
def api_risk():
    """
    Risk dashboard endpoint.

    Queries paper_trading.db for risk state, drawdown, daily loss, consecutive losses.

    Returns:
        risk_state: OK | WARNING | KILL_SWITCH
        drawdown_pct: Current drawdown percentage
        max_drawdown_pct: Max drawdown from peak
        daily_pnl: Today's P&L
        consecutive_losses: Current streak
        daily_loss_threshold: Max daily loss config
        max_consecutive_losses: Consecutive loss limit
        risk_config: Current risk control parameters
    """
    if not _db_exists(PAPER_DB_PATH):
        return jsonify({
            "risk_state": "OK",
            "drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "daily_loss_threshold": 500.0,
            "max_consecutive_losses": 3,
            "balance": 10000.0,
            "peak_balance": 10000.0,
        })

    try:
        conn = _get_paper_db()

        # Current balance
        row = conn.execute("""
            SELECT closing_balance FROM daily_balances
            ORDER BY date_utc DESC LIMIT 1
        """).fetchone()
        balance = _safe_float(row["closing_balance"] if row else None, 10000.0)

        # Peak balance
        row = conn.execute("""
            SELECT MAX(closing_balance) FROM daily_balances
        """).fetchone()
        peak = _safe_float(row[0] if row else None, balance)

        # Today's P&L
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT pnl FROM daily_balances WHERE date_utc = ?
        """, (today,)).fetchone()
        daily_pnl = _safe_float(row["pnl"] if row else None, 0.0)

        # Consecutive losses from trades
        row = conn.execute("""
            SELECT realized_pnl FROM trades
            WHERE status = 'closed'
            ORDER BY settlement_date_utc DESC
            LIMIT 10
        """).fetchall()
        consecutive_losses = 0
        for r in row:
            pnl = _safe_float(r["realized_pnl"])
            if pnl < 0:
                consecutive_losses += 1
            else:
                break

        # Total trades and metrics
        total_trades = conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0] or 0
        winning_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE realized_pnl > 0"
        ).fetchone()[0] or 0
        losing_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE realized_pnl < 0"
        ).fetchone()[0] or 0

        conn.close()

        # Calculate drawdown
        drawdown_pct = 0.0
        max_drawdown_pct = 0.0
        if peak > 0:
            drawdown_pct = (peak - balance) / peak * 100.0
            max_drawdown_pct = drawdown_pct  # simplified, could be tracked separately

        # Determine risk state
        risk_state = "OK"
        risk_reasons = []

        # Config thresholds
        daily_loss_threshold = 500.0
        max_consecutive_losses = 3
        drawdown_threshold = 15.0  # 15% max drawdown

        if abs(daily_pnl) >= daily_loss_threshold and daily_pnl < 0:
            risk_state = "KILL_SWITCH"
            risk_reasons.append(f"Daily loss ${abs(daily_pnl):.0f} exceeds ${daily_loss_threshold:.0f} threshold")
        elif consecutive_losses >= max_consecutive_losses:
            risk_state = "KILL_SWITCH"
            risk_reasons.append(f"Consecutive losses ({consecutive_losses}) >= {max_consecutive_losses}")
        elif drawdown_pct >= drawdown_threshold:
            risk_state = "WARNING"
            risk_reasons.append(f"Drawdown {drawdown_pct:.1f}% approaching {drawdown_threshold:.0f}% threshold")
        elif drawdown_pct >= drawdown_threshold * 0.7:
            risk_state = "WARNING"
            risk_reasons.append(f"Drawdown {drawdown_pct:.1f}% approaching threshold")

        return jsonify({
            "risk_state": risk_state,
            "risk_reasons": risk_reasons,
            "drawdown_pct": round(drawdown_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "daily_pnl": round(daily_pnl, 2),
            "consecutive_losses": consecutive_losses,
            "daily_loss_threshold": daily_loss_threshold,
            "max_consecutive_losses": max_consecutive_losses,
            "drawdown_threshold": drawdown_threshold,
            "balance": round(balance, 2),
            "peak_balance": round(peak, 2),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
        })

    except Exception as e:
        _LOGGER.error("Risk API error: %s", e)
        return jsonify({
            "risk_state": "OK",
            "drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "daily_loss_threshold": 500.0,
            "max_consecutive_losses": 3,
            "balance": 10000.0,
            "peak_balance": 10000.0,
            "error": str(e),
        })


# ─── API: Stats ───────────────────────────────────────────────────────────

@trading_bp.route("/api/stats")
def api_stats():
    """
    Performance analytics endpoint.

    Returns:
        signal_accuracy: Table of accuracy by signal type (last 30 days)
        win_rate_by_station: Win rate per station (bar chart data)
        rolling_sharpe: List of {trade_index, sharpe} for line chart (last 50 trades)
        summary: Aggregate stats
    """
    result = {
        "signal_accuracy": {},
        "win_rate_by_station": {},
        "rolling_sharpe": [],
        "summary": {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_edge": 0.0,
            "total_pnl": 0.0,
        },
    }

    # Try trade_journal for signal accuracy and win rates
    if _db_exists(JOURNAL_DB_PATH):
        try:
            conn = _get_journal_db()

            # Signal accuracy by functionality (last 30 days)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            rows = conn.execute("""
                SELECT
                    functionality,
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'EXECUTED' THEN 1 ELSE 0 END) as placed,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses
                FROM trade_journal
                WHERE outcome IN ('EXECUTED', 'SETTLED_WIN', 'SETTLED_LOSS', 'SKIPPED_EDGE', 'SKIPPED_FILTER')
                  AND timestamp_utc >= ?
                GROUP BY functionality
                HAVING total > 0
                ORDER BY total DESC
            """, (cutoff,)).fetchall()

            for r in rows:
                func = r["functionality"] or "unknown"
                total = r["total"] or 0
                placed = r["placed"] or 0
                wins = r["wins"] or 0
                losses = r["losses"] or 0
                accuracy = (wins / placed * 100.0) if placed > 0 else 0.0
                result["signal_accuracy"][func] = {
                    "total": total,
                    "placed": placed,
                    "wins": wins,
                    "losses": losses,
                    "accuracy_pct": round(accuracy, 1),
                }

            # Win rate by station
            rows = conn.execute("""
                SELECT
                    station,
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'EXECUTED' THEN 1 ELSE 0 END) as placed,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses
                FROM trade_journal
                WHERE outcome IN ('SETTLED_WIN', 'SETTLED_LOSS')
                GROUP BY station
                HAVING (wins + losses) > 0
                ORDER BY wins DESC
            """).fetchall()

            for r in rows:
                station = r["station"]
                wins = r["wins"] or 0
                losses = r["losses"] or 0
                total = wins + losses
                win_rate = (wins / total * 100.0) if total > 0 else 0.0
                result["win_rate_by_station"][station] = {
                    "wins": wins,
                    "losses": losses,
                    "total": total,
                    "win_rate_pct": round(win_rate, 1),
                }

            # Journal summary stats
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'EXECUTED' THEN 1 ELSE 0 END) as executed,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses,
                    AVG(CASE WHEN outcome = 'EXECUTED' AND edge IS NOT NULL THEN edge ELSE NULL END) as avg_edge
                FROM trade_journal
            """).fetchone()

            if row:
                total = row["total"] or 0
                wins = row["wins"] or 0
                losses = row["losses"] or 0
                settled = wins + losses
                result["summary"].update({
                    "total_trades": total,
                    "executed": row["executed"] or 0,
                    "settled": settled,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round((wins / settled * 100.0) if settled > 0 else 0.0, 1),
                    "avg_edge": round(_safe_float(row["avg_edge"]), 4),
                })

            conn.close()
        except Exception as e:
            _LOGGER.error("Journal stats error: %s", e)

    # Add paper_trading.db trade info
    if _db_exists(PAPER_DB_PATH):
        try:
            conn = _get_paper_db()

            # Get closed trades for rolling Sharpe
            rows = conn.execute("""
                SELECT realized_pnl, position_size_usd, settlement_date_utc
                FROM trades
                WHERE status = 'closed' AND realized_pnl IS NOT NULL
                ORDER BY settlement_date_utc DESC
                LIMIT 50
            """).fetchall()

            trades_list = []
            for r in rows:
                pnl = _safe_float(r["realized_pnl"])
                size = max(_safe_float(r["position_size_usd"]), 1.0)
                trades_list.append(pnl / size)

            trades_list.reverse()  # chronological order
            rolling_sharpe = []
            window = 10
            for i in range(min(window, len(trades_list)), len(trades_list) + 1):
                window_returns = trades_list[max(0, i - window):i]
                if len(window_returns) >= 2:
                    mean_r = sum(window_returns) / len(window_returns)
                    std_r = (sum((r - mean_r) ** 2 for r in window_returns) / (len(window_returns) - 1)) ** 0.5
                    sharpe = mean_r / std_r if std_r > 0 else 0.0
                else:
                    sharpe = 0.0
                rolling_sharpe.append({
                    "trade_index": i,
                    "sharpe": round(sharpe, 3),
                })

            result["rolling_sharpe"] = rolling_sharpe

            # Total P&L from paper DB
            row = conn.execute("""
                SELECT SUM(realized_pnl) FROM trades WHERE status = 'closed'
            """).fetchone()
            if row and row[0] is not None:
                result["summary"]["total_pnl"] = round(_safe_float(row[0]), 2)

            conn.close()
        except Exception as e:
            _LOGGER.error("Paper stats error: %s", e)

    return jsonify(result)


# ─── SSE Stream ───────────────────────────────────────────────────────────

@trading_bp.route("/api/stream")
def api_stream():
    """
    Server-Sent Events endpoint for real-time dashboard updates.

    Pushes a JSON blob every 30 seconds with all dashboard data.
    Clients reconnect automatically.
    """
    def generate():
        while True:
            try:
                payload = {}
                # Collect all data
                for endpoint, handler in [
                    ("pnl", api_pnl),
                    ("positions", api_positions),
                    ("portfolio", api_portfolio),
                    ("risk", api_risk),
                    ("stats", api_stats),
                ]:
                    try:
                        resp = handler()
                        data = resp.get_json() if hasattr(resp, "get_json") else {}
                        payload[endpoint] = data
                    except Exception as e:
                        _LOGGER.error("SSE %s error: %s", endpoint, e)
                        payload[endpoint] = {"error": str(e)}

                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                _LOGGER.error("SSE stream error: %s", e)
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            time.sleep(30)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )