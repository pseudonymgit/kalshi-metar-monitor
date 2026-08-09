#!/usr/bin/env python3
# DEPRECATED — standalone Flask app. Use app.py Trading Dashboard blueprint instead.

"""
[DEPRECATION NOTICE - Phase C.1]

This module has been converted from a standalone Flask application to a pure
Python data module. The Flask app and CLI entry points have been removed.

Data functions (get_predictions, get_discrepancies, etc.) remain available as
module-level API functions. For the Flask-based dashboard, use the Trading
Dashboard blueprint registered in app.py (see core/trading_dashboard/).

The _self_test() function is retained for development verification.
"""

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 2. [2026-07-22 Phase C.1: Flask consolidation — removed standalone app + _main(); switched to sqlite_utils]
#

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template_string
from scipy import stats as sp_stats

from .sqlite_utils import get_sqlite_connection

# Try to import live market price fetcher for real market_prob values
try:
    from core.kalshi_price_fetcher import get_live_market_price as _dashboard_get_market_price
except ImportError:
    _dashboard_get_market_price = None

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
DEFAULT_MAPPING_PATH = str(REPO_ROOT / "data" / "station_mapping.json")

# ─── Data Structures ───────────────────────────────────────────────────────


@dataclass
class SignalBreakdown:
    """Individual signal contributing to a station prediction."""

    signal_name: str
    value: float
    weight: float
    contribution: float
    description: str


@dataclass
class StationPrediction:
    """Prediction payload for a single station."""

    station: str
    city_name: str
    predicted_temp_f: float
    std_dev: float
    z_score: float
    confidence: float
    market_prob: float
    model_prob: float
    discrepancy: float
    discrepancy_sigma: float
    signals: List[SignalBreakdown] = field(default_factory=list)
    last_updated: str = ""
    sample_size: int = 0


@dataclass
class DiscrepancyRow:
    """Single row in the discrepancy table."""

    station: str
    city_name: str
    model_prob: float
    market_prob: float
    discrepancy: float
    z_score: float
    flagged: bool


# ─── Inline HTML Template ──────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Weather Engine Dashboard</title>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #0f1117; color: #e0e0e0; }
        .header { background: #1a1d29; padding: 20px 40px; border-bottom: 2px solid #2a2d39; }
        .header h1 { font-size: 1.5rem; color: #4fc3f7; }
        .header .sub { font-size: 0.85rem; color: #888; margin-top: 4px; }
        .container { padding: 24px 40px; }
        .health-bar {
            display: flex; gap: 24px; margin-bottom: 24px;
            background: #1a1d29; padding: 16px 24px; border-radius: 8px;
        }
        .health-item { display: flex; flex-direction: column; }
        .health-label { font-size: 0.75rem; color: #888; text-transform: uppercase; }
        .health-value { font-size: 1.1rem; color: #4fc3f7; margin-top: 4px; }
        .health-value.warn { color: #ffb74d; }
        .health-value.error { color: #ef5350; }
        .health-value.ok { color: #66bb6a; }
        #chart { width: 100%; height: 600px; margin-bottom: 24px; }
        .discrepancy-table { width: 100%; border-collapse: collapse; margin-top: 16px; }
        .discrepancy-table th { text-align: left; padding: 10px 16px; background: #1a1d29;
            color: #888; font-size: 0.8rem; text-transform: uppercase; }
        .discrepancy-table td { padding: 10px 16px; border-bottom: 1px solid #2a2d39; }
        .discrepancy-table tr.flagged { background: rgba(239, 83, 80, 0.1); }
        .discrepancy-table tr.flagged td:first-child::before { content: "\u26a0 "; color: #ef5350; }
        .section-title { font-size: 1.1rem; color: #4fc3f7; margin: 24px 0 12px; }
        .refresh-note { font-size: 0.8rem; color: #666; margin-top: 8px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🌤️ Weather Engine Dashboard</h1>
        <div class="sub">Model PMF vs Market Odds — auto-refreshed on load</div>
    </div>
    <div class="container">
        <div class="health-bar" id="health-bar">Loading health...</div>
        <div class="section-title">Model Probability vs Market Odds by Station</div>
        <div id="chart"></div>
        <div class="section-title">Discrepancy Table (>2σ flagged)</div>
        <table class="discrepancy-table" id="disc-table">
            <thead>
                <tr><th>Station</th><th>City</th><th>Model Prob</th><th>Market Prob</th>
                    <th>Discrepancy</th><th>Z-Score</th><th>Status</th></tr>
            </thead>
            <tbody id="disc-body"></tbody>
        </table>
        <div class="refresh-note">Last rendered: {{ rendered_at }}</div>
    </div>
    <script>
        // Load health
        fetch('/health').then(r => r.json()).then(data => {
            const bar = document.getElementById('health-bar');
            const cls = data.db_connected ? 'ok' : 'error';
            bar.innerHTML = `
                <div class="health-item"><span class="health-label">DB Status</span>
                    <span class="health-value ${cls}">${data.db_connected ? 'Connected' : 'Disconnected'}</span></div>
                <div class="health-item"><span class="health-label">Signal Count</span>
                    <span class="health-value">${data.signal_count}</span></div>
                <div class="health-item"><span class="health-label">Last Update</span>
                    <span class="health-value">${data.last_update || 'N/A'}</span></div>
                <div class="health-item"><span class="health-label">Stations Tracked</span>
                    <span class="health-value">${data.stations_tracked}</span></div>
            `;
        });

        // Load discrepancy table
        fetch('/api/discrepancies').then(r => r.json()).then(data => {
            const tbody = document.getElementById('disc-body');
            tbody.innerHTML = data.map(row => `
                <tr class="${row.flagged ? 'flagged' : ''}">
                    <td>${row.station}</td><td>${row.city_name}</td>
                    <td>${row.model_prob.toFixed(4)}</td><td>${row.market_prob.toFixed(4)}</td>
                    <td>${row.discrepancy.toFixed(4)}</td>
                    <td>${row.z_score.toFixed(2)}</td>
                    <td>${row.flagged ? 'FLAGGED' : 'OK'}</td>
                </tr>
            `).join('');
        });

        // Build Plotly chart from discrepancies
        fetch('/api/discrepancies').then(r => r.json()).then(data => {
            const stations = data.map(d => d.station);
            const modelProbs = data.map(d => d.model_prob);
            const marketProbs = data.map(d => d.market_prob);
            const trace1 = {
                x: stations, y: modelProbs, name: 'Model PMF',
                type: 'bar', marker: { color: '#4fc3f7' }
            };
            const trace2 = {
                x: stations, y: marketProbs, name: 'Market Odds',
                type: 'bar', marker: { color: '#ffb74d' }
            };
            const layout = {
                barmode: 'group',
                title: 'Model PMF vs Market Odds by Station',
                paper_bgcolor: '#0f1117',
                plot_bgcolor: '#1a1d29',
                font: { color: '#e0e0e0', size: 12 },
                xaxis: { tickangle: -45 },
                yaxis: { title: 'Probability', range: [0, 1] },
                margin: { b: 120 }
            };
            Plotly.newPlot('chart', [trace1, trace2], layout, { responsive: true });
        });
    </script>
</body>
</html>"""


# ─── Market Probability Resolution ────────────────────────────────────────


def _resolve_market_prob(station: str, stats: dict) -> float:
    """
    Resolve market probability for a station using multiple sources.

    Resolution order:
      1. Live Kalshi API (via kalshi_price_fetcher)
      2. Alerts DB market_cache table
      3. Fallback: 0.5 (neutral)

    B-Mode R8 Cycle 4.3/4.1: Multi-source with circuit breaker awareness.
    """
    # Source 1: Live Kalshi API
    if _dashboard_get_market_price is not None:
        try:
            live_date = stats.get("latest_date")
            if live_date:
                live_price, _ = _dashboard_get_market_price(station, 'HIGH', live_date)
                if live_price is not None and 0.01 <= live_price <= 0.99:
                    return live_price
        except Exception:
            pass

    # Source 2: Alerts DB market_cache table
    try:
        alert_db = os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")
        if os.path.exists(alert_db):
            import sqlite3
            conn = sqlite3.connect(alert_db, timeout=1)
            try:
                row = conn.execute(
                    """SELECT cache_json FROM market_cache
                       WHERE station = ? ORDER BY updated_at DESC LIMIT 1""",
                    (station,)
                ).fetchone()
                if row:
                    cache = json.loads(row[0])
                    if isinstance(cache, dict):
                        yes_bid = cache.get("yes_bid_dollars") or cache.get("yes_bid")
                        yes_ask = cache.get("yes_ask_dollars") or cache.get("yes_ask")
                        last_price = cache.get("last_price_dollars") or cache.get("last_price")
                        # Prefer last_price, then midpoint
                        if last_price is not None:
                            price = float(last_price)
                            if 0 < price <= 1:
                                return price
                            if 1 < price <= 100:
                                return price / 100.0
                        if yes_bid is not None and yes_ask is not None:
                            bid = float(yes_bid) if float(yes_bid) <= 1 else float(yes_bid) / 100.0
                            ask = float(yes_ask) if float(yes_ask) <= 1 else float(yes_ask) / 100.0
                            if 0 < bid <= ask <= 1:
                                return (bid + ask) / 2.0
            finally:
                conn.close()
    except Exception:
        pass

    # Source 3: Fallback — neutral 0.5
    return 0.5


# ─── Dashboard Application ────────────────────────────────────────────────


class DashboardApp:
    """
    Legacy dashboard for the weather trading engine (DEPRECATED).

    NOTE: This class is kept for backward compatibility. New code should
    use the Trading Dashboard blueprint in app.py.

    Provides health checks, prediction APIs, a discrepancy table, and
    a Plotly-based HTML dashboard. All computations are deterministic
    (no AI/ML). Predictions use 30-day rolling mean + std from daily_stats.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, port: int = 5005):
        """
        Initialize the dashboard application.

        Args:
            db_path: Path to the METAR SQLite database.
            port: Port to serve the Flask app on.
        """
        self.db_path = db_path
        self.port = port
        self._mapping = self._load_station_mapping()
        self._app: Optional[Flask] = None
        self._logger = logging.getLogger(f"{__name__}.DashboardApp")

    # ─── Setup ────────────────────────────────────────────────────────────

    def _load_station_mapping(self) -> Dict[str, Dict[str, Any]]:
        """Load station → city mapping from JSON."""
        mapping_path = DEFAULT_MAPPING_PATH
        try:
            with open(mapping_path, "r") as f:
                data = json.load(f)
            return data.get("stations", {})
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            self._logger.warning("Could not load station mapping: %s", exc)
            return {}

    def _get_db_connection(self):
        """Get a SQLite connection with row factory."""
        conn = get_sqlite_connection(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_app(self) -> Flask:
        """Create and configure the Flask application with all routes."""
        app = Flask(__name__)
        self._app = app

        app.add_url_rule("/health", "health", self._health_endpoint, methods=["GET"])
        app.add_url_rule(
            "/api/predictions/<station>",
            "predictions",
            self._predictions_endpoint,
            methods=["GET"],
        )
        app.add_url_rule(
            "/api/discrepancies",
            "discrepancies",
            self._discrepancies_endpoint,
            methods=["GET"],
        )
        app.add_url_rule(
            "/dashboard",
            "dashboard",
            self._dashboard_endpoint,
            methods=["GET"],
        )
        app.add_url_rule("/", "root", lambda: self._dashboard_endpoint(), methods=["GET"])

        return app

    # ─── Core Analytics ────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """
        Check system health: DB connection, signal count, last update.

        Returns:
            Dict with db_connected, signal_count, last_update, stations_tracked.
        """
        result = {
            "db_connected": False,
            "signal_count": 0,
            "last_update": None,
            "stations_tracked": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # Check connection
            cursor.execute("SELECT 1")
            cursor.fetchone()
            result["db_connected"] = True

            # Count signals (daily_stats rows in last 30 days)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
                "%Y-%m-%d"
            )
            cursor.execute(
                "SELECT COUNT(*) FROM daily_stats WHERE date_utc >= ?", (cutoff,)
            )
            result["signal_count"] = cursor.fetchone()[0]

            # Last update
            cursor.execute("SELECT MAX(ingestion_timestamp_utc) FROM daily_stats")
            last = cursor.fetchone()[0]
            result["last_update"] = last

            # Stations tracked
            cursor.execute("SELECT COUNT(DISTINCT station) FROM daily_stats")
            result["stations_tracked"] = cursor.fetchone()[0]

            conn.close()
        except sqlite3.Error as exc:
            self._logger.error("Health check DB error: %s", exc)

        return result

    def _compute_station_stats(self, station: str, window_days: int = 30) -> Optional[Dict[str, Any]]:
        """
        Compute rolling statistics for a station over a window of days.

        Args:
            station: ICAO station code (e.g., 'KATL').
            window_days: Number of days for the rolling window.

        Returns:
            Dict with mean, std, sample_size, latest_temp, latest_date, or None.
        """
        try:
            conn = self._get_db_connection()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
                "%Y-%m-%d"
            )
            query = """
                SELECT date_utc, max_temp_f, min_temp_f, avg_temp_f
                FROM daily_stats
                WHERE station = ? AND date_utc >= ?
                ORDER BY date_utc DESC
            """
            df = pd.read_sql_query(query, conn, params=(station, cutoff))
            conn.close()

            if df.empty:
                return None

            # Use max_temp_f for HIGH market predictions
            temps = df["max_temp_f"].dropna()
            if temps.empty:
                return None

            mean_temp = float(temps.mean())
            std_temp = float(temps.std(ddof=1)) if len(temps) > 1 else 0.0
            latest_temp = float(temps.iloc[0])
            latest_date = str(df["date_utc"].iloc[0])

            return {
                "mean": mean_temp,
                "std": std_temp,
                "sample_size": len(temps),
                "latest_temp": latest_temp,
                "latest_date": latest_date,
            }
        except Exception as exc:
            self._logger.error("Stats computation failed for %s: %s", station, exc)
            return None

    def _compute_signals(self, station: str, stats: Dict[str, Any]) -> List[SignalBreakdown]:
        """
        Compute deterministic signal breakdown for a station.

        Signals are simple statistical features — no AI/ML:
          1. Mean reversion: distance from 30-day mean
          2. Volatility regime: std dev classification
          3. Momentum: latest vs previous day direction
        """
        signals: List[SignalBreakdown] = []
        mean = stats["mean"]
        std = stats["std"]
        latest = stats["latest_temp"]

        # Signal 1: Mean reversion
        reversion = abs(latest - mean)
        reversion_weight = 0.40
        reversion_contrib = reversion * reversion_weight
        signals.append(
            SignalBreakdown(
                signal_name="mean_reversion",
                value=round(reversion, 2),
                weight=reversion_weight,
                contribution=round(reversion_contrib, 4),
                description=f"|{latest:.1f} - {mean:.1f}| = {reversion:.2f}°F from 30d mean",
            )
        )

        # Signal 2: Volatility regime
        vol_level = "low" if std < 5 else ("moderate" if std < 10 else "high")
        vol_value = std
        vol_weight = 0.30
        vol_contrib = vol_value * vol_weight
        signals.append(
            SignalBreakdown(
                signal_name="volatility_regime",
                value=round(vol_value, 2),
                weight=vol_weight,
                contribution=round(vol_contrib, 4),
                description=f"30d σ = {std:.2f}°F ({vol_level} volatility)",
            )
        )

        # Signal 3: Momentum (latest vs mean direction)
        momentum = latest - mean
        mom_weight = 0.30
        mom_contrib = momentum * mom_weight
        signals.append(
            SignalBreakdown(
                signal_name="momentum",
                value=round(momentum, 2),
                weight=mom_weight,
                contribution=round(mom_contrib, 4),
                description=f"Latest {latest:.1f}°F vs mean {mean:.1f}°F → {'above' if momentum > 0 else 'below'} average",
            )
        )

        return signals

    def station_predictions(self, station: str) -> dict:
        """
        Get latest predictions for a station with signal breakdown.

        Computes a z-score based prediction from the 30-day window,
        converts to a model probability via the CDF, and compares
        against implicit market probability (0.5 default).

        Args:
            station: ICAO station code (e.g., 'KATL').

        Returns:
            Dict with prediction details and signal breakdown.
        """
        station = station.upper().strip()
        station_info = self._mapping.get(station, {})
        city_name = station_info.get("city_name", station)

        stats = self._compute_station_stats(station)
        if stats is None:
            return {
                "station": station,
                "city_name": city_name,
                "error": f"No data available for station {station}",
            }

        mean = stats["mean"]
        std = stats["std"]
        latest = stats["latest_temp"]

        # Z-score: how far latest temp is from mean, in std devs
        z = (latest - mean) / std if std > 0 else 0.0

        # Model probability: P(temp > mean) using normal CDF
        # If latest > mean, model thinks upward bias is more likely
        model_prob = float(sp_stats.norm.cdf(z)) if std > 0 else 0.5

        # Market probability: try live Kalshi price, fallback to cache, then 0.5
        # B-Mode R8 Cycle 4.3: Use stats latest_date instead of undefined 'date' variable
        # B-Mode R8 Cycle 4.x (FP 5.8): Multi-source market_prob with circuit breaker awareness
        market_prob = _resolve_market_prob(station, stats)

        # Discrepancy
        discrepancy = model_prob - market_prob

        # Z-score of discrepancy across all stations (computed in discrepancy_table)
        # For single-station endpoint, we compute a local sigma
        discrepancy_sigma = abs(discrepancy) / (std / abs(latest)) if std > 0 and latest != 0 else 0.0
        discrepancy_sigma = round(discrepancy_sigma, 4)

        # Confidence: based on sample size and std
        confidence = min(1.0, stats["sample_size"] / 30.0) * (1.0 / (1.0 + std / 20.0))

        # Compute signals
        signals = self._compute_signals(station, stats)

        prediction = StationPrediction(
            station=station,
            city_name=city_name,
            predicted_temp_f=round(latest, 2),
            std_dev=round(std, 2),
            z_score=round(z, 4),
            confidence=round(confidence, 4),
            market_prob=round(market_prob, 4),
            model_prob=round(model_prob, 4),
            discrepancy=round(discrepancy, 4),
            discrepancy_sigma=discrepancy_sigma,
            signals=signals,
            last_updated=stats["latest_date"],
            sample_size=stats["sample_size"],
        )

        return asdict(prediction)

    def discrepancy_table(self) -> list[dict]:
        """
        Build a table of model-vs-market odds discrepancies for all stations.

        Computes model probability for each station, compares against
        market probability (0.5 default), and flags rows where the
        z-score of the discrepancy exceeds 2σ.

        Returns:
            List of dicts with station, model_prob, market_prob, discrepancy,
            z_score, flagged.
        """
        rows: List[DiscrepancyRow] = []
        raw_discrepancies: List[float] = []

        for station in sorted(self._mapping.keys()):
            stats = self._compute_station_stats(station)
            if stats is None:
                continue

            mean = stats["mean"]
            std = stats["std"]
            latest = stats["latest_temp"]
            city = self._mapping[station].get("city_name", station)

            z = (latest - mean) / std if std > 0 else 0.0
            model_prob = float(sp_stats.norm.cdf(z)) if std > 0 else 0.5
            # B-Mode R8 Cycle 4.3: Use stats latest_date instead of undefined 'date' variable
            market_prob = _resolve_market_prob(station, stats)
            discrepancy = model_prob - market_prob
            raw_discrepancies.append(discrepancy)

            rows.append(
                DiscrepancyRow(
                    station=station,
                    city_name=city,
                    model_prob=round(model_prob, 4),
                    market_prob=round(market_prob, 4),
                    discrepancy=round(discrepancy, 4),
                    z_score=round(z, 4),
                    flagged=False,  # set after computing sigma
                )
            )

        # Compute z-scores of discrepancies across stations
        if len(raw_discrepancies) > 1:
            disc_array = np.array(raw_discrepancies)
            disc_mean = float(disc_array.mean())
            disc_std = float(disc_array.std(ddof=1))

            for row in rows:
                if disc_std > 0:
                    row.z_score = round((row.discrepancy - disc_mean) / disc_std, 4)
                    row.flagged = abs(row.z_score) > 2.0
                else:
                    row.z_score = 0.0
                    row.flagged = False

        return [asdict(r) for r in rows]

    def render_dashboard(self) -> str:
        """
        Render the full HTML dashboard page.

        Returns:
            HTML string with embedded Plotly chart and discrepancy table.
        """
        rendered_at = datetime.now(timezone.utc).isoformat()
        app = self._app or self.create_app()
        with app.app_context():
            return render_template_string(DASHBOARD_HTML, rendered_at=rendered_at)

    # ─── Flask Endpoint Wrappers ───────────────────────────────────────────

    def _health_endpoint(self):
        return jsonify(self.health_check())

    def _predictions_endpoint(self, station: str):
        return jsonify(self.station_predictions(station))

    def _discrepancies_endpoint(self):
        return jsonify(self.discrepancy_table())

    def _dashboard_endpoint(self):
        return self.render_dashboard()

    # ─── Run ──────────────────────────────────────────────────────────────

    def run(self):
        """Start the Flask development server (DEPRECATED)."""
        app = self.create_app()
        self._logger.info("Starting dashboard on port %d", self.port)
        app.run(host="0.0.0.0", port=self.port, debug=False)


# ─── Self-Tests ────────────────────────────────────────────────────────────


def _self_test():
    """Run deterministic self-tests for all dashboard components."""
    print("=" * 60)
    print("Dashboard MVP — Self-Test")
    print("=" * 60)

    dashboard = DashboardApp()

    # Test 1: Health check
    print("\n[1] Health check...")
    health = dashboard.health_check()
    assert health["db_connected"], "DB should be connected"
    assert health["stations_tracked"] > 0, "Should have stations tracked"
    print(f"    ✓ DB connected: {health['db_connected']}")
    print(f"    ✓ Signal count: {health['signal_count']}")
    print(f"    ✓ Stations tracked: {health['stations_tracked']}")
    print(f"    ✓ Last update: {health['last_update']}")

    # Test 2: Station predictions
    print("\n[2] Station predictions (KATL)...")
    preds = dashboard.station_predictions("KATL")
    assert "station" in preds, "Should have station key"
    assert preds["station"] == "KATL", "Station should be KATL"
    assert "signals" in preds, "Should have signal breakdown"
    assert len(preds["signals"]) == 3, "Should have 3 signals"
    assert 0.0 <= preds["model_prob"] <= 1.0, "Model prob in [0,1]"
    assert 0.0 <= preds["market_prob"] <= 1.0, "Market prob in [0,1]"
    print(f"    ✓ Station: {preds['station']} ({preds['city_name']})")
    print(f"    ✓ Predicted temp: {preds['predicted_temp_f']}°F")
    print(f"    ✓ Model prob: {preds['model_prob']}, Market prob: {preds['market_prob']}")
    print(f"    ✓ Signals: {len(preds['signals'])} (mean_reversion, volatility, momentum)")

    # Test 3: Invalid station
    print("\n[3] Invalid station (ZZZZ)...")
    invalid = dashboard.station_predictions("ZZZZ")
    assert "error" in invalid, "Should return error for unknown station"
    print(f"    ✓ Error returned: {invalid['error']}")

    # Test 4: Discrepancy table
    print("\n[4] Discrepancy table...")
    table = dashboard.discrepancy_table()
    assert isinstance(table, list), "Should return a list"
    assert len(table) > 0, "Should have at least one row"
    for row in table:
        assert "station" in row, "Row should have station"
        assert "model_prob" in row, "Row should have model_prob"
        assert "market_prob" in row, "Row should have market_prob"
        assert "z_score" in row, "Row should have z_score"
        assert "flagged" in row, "Row should have flagged"
    flagged_count = sum(1 for r in table if r["flagged"])
    print(f"    ✓ Total rows: {len(table)}")
    print(f"    ✓ Flagged (>2σ): {flagged_count}")

    # Test 5: HTML render
    print("\n[5] HTML dashboard render...")
    html = dashboard.render_dashboard()
    assert "<html" in html.lower(), "Should be valid HTML"
    assert "plotly" in html.lower(), "Should include Plotly"
    assert "discrepancy" in html.lower(), "Should have discrepancy section"
    print(f"    ✓ HTML length: {len(html)} chars")
    print(f"    ✓ Contains Plotly CDN script tag")
    print(f"    ✓ Contains discrepancy table")

    # Test 6: Flask app creation
    print("\n[6] Flask app creation...")
    app = dashboard.create_app()
    assert app is not None, "App should be created"
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/health" in rules, "Should have /health route"
    assert "/api/discrepancies" in rules, "Should have /api/discrepancies route"
    assert "/dashboard" in rules, "Should have /dashboard route"
    print(f"    ✓ Routes: {', '.join(sorted(rules))}")

    print("\n" + "=" * 60)
    print("All self-tests passed ✓")
    print("=" * 60)