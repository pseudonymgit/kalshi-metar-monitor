#!/usr/bin/env python3
"""
PAPER TRADING ENGINE - Deterministic Implementation (v2.0)
Standalone script for paper trading, NO AI IN THE LOOP.

Core Features:
- Mandatory `trade_version` and `functionality` fields on every trade
- Deterministic-only (no AI/ML/LLM calls)
- Daily reconciliation (P&L, position counts, trade counts)
- Version-tagged analytics (so performance correlates to exact code state)
- Settlement logic with proper P&L calculation
- Decision output logging with "market implied vs analytical fair value"
- Calibration dashboard reporting hooks
- Simulated Kalshi/Polymarket API interface (no real trading)

⚠️ DETERMINISTIC BY DESIGN - No ML/AI in prediction/execution loop
  All signals must be based on deterministic calculations.
  ML/AI modeling stays in Phase 3 when risk layer is stable.

Version: v2.0 2026-07-03
"""

import sqlite3
import json
import uuid
import time
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import os
import statistics
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DB_PATH = str(REPO_ROOT / "data" / "paper_trading.db")
METAR_DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
CALIBRATION_REPORT_PATH = str(REPO_ROOT / "reports" / "paper_trading_calibration.json")

# Import the hourly late-day momentum signal
sys.path.insert(0, str(REPO_ROOT / "core"))
from late_day_momentum_hourly import late_day_momentum_hourly as _ldm_hourly_signal
from position_sizing import (
    compute_position_size as _compute_confidence_weighted_size,
    extract_confidence_from_signal_context as _extract_confidence,
    classify_confidence as _classify_confidence,
    ConfidenceTier as _ConfidenceTier,
    KellyPositionSizingConfig,
)
from kalshi_price_fetcher import (
    get_live_market_price as _get_live_market_price,
    clear_price_cache as _clear_price_cache,
    get_cache_stats as _get_cache_stats,
)
from station_time import is_within_entry_window as _is_within_entry_window
from station_registry import (
    get_all_stations as _get_all_stations,
    get_station_mapping as _get_station_mapping,
    validate_station_registry as _validate_station_registry,
    get_cluster_for_station as _get_cluster_for_station,
    CLUSTER_BUDGET_USD as _CLUSTER_BUDGET_USD,
    CITY_PAIR_CAP_USD as _CITY_PAIR_CAP_USD,
)
from risk_controls import (
    RiskMetrics,
    RiskState,
    RiskConfig,
    DEFAULT_RISK_CONFIG,
    is_station_approved,
    get_approved_stations,
    evaluate_risk_state,
    risk_report,
    format_risk_alert,
    RiskManager,
    RiskConfig,
    TradeResult
)
from alert_builder import (
    build_paper_trade_alert,
    format_alert_for_discord,
    OpportunityGrade,
    LaneType,
    PAPER_TRADE_ALERT_SCHEMA_VERSION,
    build_paper_trade_alert_dev,
)

# Attempt to import NWP Analog Signal - wrap in try/except to prevent crashes if sklearn/xgboost not installed
try:
    from core.signals.nwp_analog_signal import NwpAnalogSignal
    HAS_NWP_ANALOG = True
except ImportError:
    print("Warning: NWP Analog Signal modules not available. Install sklearn and xgboost for NWP features.")
    NwpAnalogSignal = None
    HAS_NWP_ANALOG = False

# Attempt to import CalibrationPipeline - wrap in try/except to prevent crashes if sklearn/scipy not installed
try:
    from core.calibration_pipeline import CalibrationPipeline
    HAS_CALIBRATION_PIPELINE = True
except ImportError:
    print("Warning: CalibrationPipeline modules not available. Install sklearn and scipy for calibration features.")
    CalibrationPipeline = None
    HAS_CALIBRATION_PIPELINE = False


# Instance environment variable (PROD/DEV/SBOX)
INSTANCE = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
_LOGGER = logging.getLogger(__name__)

# Alert builder integration - export alert builder functions
# Alert builder provides:
# - build_paper_trade_alert(): Build slim alert with S/A/B/C/D/F grade + Edge
# - compute_opportunity_grade(): Calculate grade from confidence, market_prob, Sharpe
# - classify_lane(): Classify signal into Regular/Sure_Thing/Goldilocks
# - format_alert_for_discord(): Format for Discord webhook

# Risk controls configuration (Marty's Phase 1 B1.5)
RISK_CONFIG = DEFAULT_RISK_CONFIG

# Trade types
class TradeType(Enum):
    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"
    BUY_NO = "buy_no"
    SELL_NO = "sell_no"


class MarketSide(Enum):
    UP = "UP"
    DOWN = "DOWN"


class PaperTrader:
    """
    Paper trading engine implementing deterministic-only signals with version tracking.
    All trade decisions and positions are logged with mandatory trade_version + functionality fields.

    Includes Marty's Phase 1 B1.5 risk guardrails:
    - Configurable max daily loss (default: $300)
    - Configurable max drawdown % (default: 10%)
    - Kill switch triggers: consecutive losses, correlation, signal conflict
    - Station gating: only approved stations (KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA)
    """

    def __init__(self,
                 paper_db=PAPER_DB_PATH,
                 metar_db=METAR_DB_PATH,
                 initial_balance=10000.0,
                 fee_rate=0.001):  # Very low fee for now, adjustable
        self.paper_db = paper_db
        self.metar_db = metar_db
        self.initial_balance = initial_balance
        self.position_size = 100  # Min position size in dollars
        self.max_position_value = 1000  # Max exposure per trade
        self.fee_rate = fee_rate  # Trading fee as proportion of fill

        # Marty's Phase 1 B1.5: Risk controls
        self._risk_config = RISK_CONFIG
        self._risk_metrics = RiskMetrics(
            daily_pnl=0.0,
            max_drawdown_pct=0.0,
            consecutive_losses=0,
            peak_balance=initial_balance,
            current_balance=initial_balance,
            risk_state=RiskState.OK,
        )
        # New comprehensive RiskManager for trade-by-trade monitoring
        self._risk_manager = RiskManager(RiskConfig())
        self._last_daily_settlement_date = None
        self._daily_trade_count = 0
        self._daily_loss_count = 0

        # Initialize databases
        self._init_paper_db()
        self._init_metar_db_if_needed()

        # Initialize signal instances
        if HAS_NWP_ANALOG:
            try:
                self._nwp_analog = NwpAnalogSignal()
            except Exception:
                print("Warning: Failed to initialize NWP Analog Signal instance")
                self._nwp_analog = None
        else:
            self._nwp_analog = None

        # Initialize calibration pipeline
        # Define the signal names and possible city codes for the calibration
        self.signal_names = ["calendar_climatology", "late_day_momentum", "nwp_analog", "late_day_analysis"]
        # Define common weather stations for initial available locations
        self.available_stations = ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA', 'KSEA', 'KSFO', 'KHOU', 'KPHX', 'KDEN']  # Common trade locations

        # Initialize calibration pipeline with common stations
        if HAS_CALIBRATION_PIPELINE:
            try:
                self._calibrator = CalibrationPipeline(self.signal_names, self.available_stations)
            except Exception as e:
                print(f"Warning: Failed to initialize CalibrationPipeline: {e}")
                self._calibrator = None
        else:
            self._calibrator = None

    def _init_paper_db(self):
        """Create paper trading database schema."""
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()
        c.execute("ATTACH DATABASE ? AS metar", (self.metar_db,))

        # Use IF NOT EXISTS to preserve data across runs - dropping tables
        # on every init destroys paper trading history.
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_uuid TEXT NOT NULL UNIQUE,
                trade_date_utc TEXT NOT NULL,
                station TEXT NOT NULL,
                market_type TEXT NOT NULL,  -- HIGH, LOW
                signal_direction TEXT NOT NULL,  -- UP or DOWN
                forecast_prob REAL,  -- Prediction confidence 0.0-1.0
                market_price REAL NOT NULL,  -- Market price when traded 0.0-1.0
                trade_type TEXT NOT NULL,  -- buy_yes, sell_yes, buy_no, sell_no
                quantity INTEGER NOT NULL, -- Number of binary contracts
                position_size_usd REAL NOT NULL,  -- Position value in USD
                trade_price REAL NOT NULL,  -- Price actually filled at 0.0-1.0
                trade_cost REAL NOT NULL,  -- Total cost (positive for debit, negative for credit)

                -- MANDATORY VERSION TRACKING FIELDS (per requirements)
                trade_version TEXT NOT NULL,  -- Version of prediction algorithm (v1.0_method1)
                functionality TEXT NOT NULL,  -- High-level functionality description

                -- Enhanced for settlement
                settled_value REAL,  -- Settlement value (0.0 or 1.0) when closed
                settlement_date_utc TEXT,  -- Date paid out
                realized_pnl REAL,  -- Realized profit/loss at settlement
                settlement_return_amount REAL,  -- Dollar amount from settlement event
                status TEXT DEFAULT 'open',  -- open, closed
                notes TEXT,

                -- Market analytics and calibration
                implied_prob REAL,    -- Market-implied probability (typically market_price)
                analytical_prob REAL,  -- Our calculated probability from forecast
                calibration_error REAL, -- abs(implied_prob - analytical_prob) for calibration tracking
                confidence_indicator REAL, -- Confidence score for reporting

                created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_date_station ON trades(trade_date_utc, station)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_version ON trades(trade_version, functionality)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_settle_date ON trades(settlement_date_utc, station)")

        # Decision Output Log - explicit recording of "market implied vs analytical fair value + confidence"
        c.execute("""
            CREATE TABLE IF NOT EXISTS decision_output_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_date_utc TEXT NOT NULL,
                station TEXT NOT NULL,
                market_type TEXT NOT NULL,  -- HIGH or LOW
                forecast_direction TEXT NOT NULL,  -- UP or DOWN
                market_implied_prob REAL NOT NULL,  -- From Kalshi/Polymarket API
                analytical_fair_value REAL NOT NULL, -- Our calculated fair value
                confidence_level REAL NOT NULL,      -- Confidence in our analysis (0.0-1.0)
                price_difference REAL,               -- Analytical - Implied
                recommendation TEXT,                 -- BUY_UP, BUY_DOWN, SELL_UP, SELL_DOWN, HOLD
                reasons TEXT,                        -- Comma-separated reasons for analysis
                divergence_detected BOOLEAN DEFAULT 0, -- Whether meaningful divergence detected
                trade_version TEXT NOT NULL,         -- For tracking evolution
                notes TEXT
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_station_date ON decision_output_log(station, decision_date_utc)")

        # Daily P&L reconciliation table
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_utc TEXT NOT NULL UNIQUE,
                opening_balance REAL NOT NULL,
                closing_balance REAL NOT NULL,
                pnl REAL NOT NULL,  -- Daily profit/loss
                trade_count INTEGER NOT NULL,
                position_count INTEGER NOT NULL,  -- Open positions at EOD
                winning_trades INTEGER NOT NULL,  -- Closed trades where pnl > 0
                losing_trades INTEGER NOT NULL,   -- Closed trades where pnl < 0
                settled_trades INTEGER NOT NULL,  -- Trades that actually settled
                position_size_weight REAL NOT NULL, -- Average position size for the day
                total_fees_paid REAL NOT NULL,     -- Fees paid during the day
                max_drawdown_since_high REAL NOT NULL,  -- Maximum drawdown from peak,

                created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Positions table
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_uuid TEXT NOT NULL UNIQUE,
                station TEXT NOT NULL,
                market_type TEXT NOT NULL,
                market_side TEXT NOT NULL,         -- UP or DOWN side of market
                trade_uuids TEXT DEFAULT '[]',     -- JSON array of trade UUIDs that make up this position
                average_cost REAL NOT NULL,        -- Average fill price
                quantity INTEGER NOT NULL,         -- Contract count (positive for long, negative for short)
                initial_value_usd REAL NOT NULL,   -- Dollar value of initial position
                market_price REAL NOT NULL,        -- Current market price
                mark_to_market_value REAL NOT NULL, -- Current market value
                realized_pnl REAL DEFAULT 0.0,     -- P&L realized upon partial/full closing
                unrealized_pnl REAL NOT NULL,     -- Unrealized profit/loss vs avg_cost
                total_pnl REAL NOT NULL,           -- Sum of realized + unrealized P/L

                -- Version tracking
                trade_versions TEXT DEFAULT '[]',  -- JSON array of trade versions in this position

                opened_date_utc TEXT NOT NULL,
                last_updated_utc TEXT NOT NULL,
                status TEXT DEFAULT 'active'      -- active, closed_partial, closed_complete
            )
        """)

        # Calibration metrics table for dashboard reporting
        c.execute("""
            CREATE TABLE IF NOT EXISTS calibration_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date_utc TEXT NOT NULL,
                brier_score REAL,
                expected_calibration_error REAL,
                avg_confidence REAL,
                total_trades INTEGER,
                total_resolved INTEGER,
                sample_completeness REAL,   -- Proportion of trades with settlements
                bucket_reliability_stats TEXT, -- JSON blob of reliability table
                pit_uniformity_score REAL,  -- How uniform is the PIT histogram
                notes TEXT
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_station_date ON decision_output_log(station, decision_date_utc)")

        # Daily P&L reconciliation table
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_utc TEXT NOT NULL UNIQUE,
                opening_balance REAL NOT NULL,
                closing_balance REAL NOT NULL,
                pnl REAL NOT NULL,  -- Daily profit/loss
                trade_count INTEGER NOT NULL,
                position_count INTEGER NOT NULL,  -- Open positions at EOD
                winning_trades INTEGER NOT NULL,  -- Closed trades where pnl > 0
                losing_trades INTEGER NOT NULL,   -- Closed trades where pnl < 0
                settled_trades INTEGER NOT NULL,  -- Trades that actually settled
                position_size_weight REAL NOT NULL, -- Average position size for the day
                total_fees_paid REAL NOT NULL,     -- Fees paid during the day
                max_drawdown_since_high REAL NOT NULL,  -- Maximum drawdown from peak,

                created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Positions table
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_uuid TEXT NOT NULL UNIQUE,
                station TEXT NOT NULL,
                market_type TEXT NOT NULL,
                market_side TEXT NOT NULL,         -- UP or DOWN side of market
                trade_uuids TEXT DEFAULT '[]',     -- JSON array of trade UUIDs that make up this position
                average_cost REAL NOT NULL,        -- Average fill price
                quantity INTEGER NOT NULL,         -- Contract count (positive for long, negative for short)
                initial_value_usd REAL NOT NULL,   -- Dollar value of initial position
                market_price REAL NOT NULL,        -- Current market price
                mark_to_market_value REAL NOT NULL, -- Current market value
                realized_pnl REAL DEFAULT 0.0,     -- P&L realized upon partial/full closing
                unrealized_pnl REAL NOT NULL,     -- Unrealized profit/loss vs avg_cost
                total_pnl REAL NOT NULL,           -- Sum of realized + unrealized P/L

                -- Version tracking
                trade_versions TEXT DEFAULT '[]',  -- JSON array of trade versions in this position

                opened_date_utc TEXT NOT NULL,
                last_updated_utc TEXT NOT NULL,
                status TEXT DEFAULT 'active'      -- active, closed_partial, closed_complete
            )
        """)

        # Calibration metrics table for dashboard reporting
        c.execute("""
            CREATE TABLE IF NOT EXISTS calibration_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date_utc TEXT NOT NULL,
                brier_score REAL,
                expected_calibration_error REAL,
                avg_confidence REAL,
                total_trades INTEGER,
                total_resolved INTEGER,
                sample_completeness REAL,   -- Proportion of trades with settlements
                bucket_reliability_stats TEXT, -- JSON blob of reliability table
                pit_uniformity_score REAL,  -- How uniform is the PIT histogram
                notes TEXT
            )
        """)

        conn.commit()
        conn.close()

    def _init_metar_db_if_needed(self):
        """Initialize metar DB if it's not complete."""
        # Just ensure the DB is accessible - don't modify existing schema
        try:
            conn = sqlite3.connect(self.metar_db)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM settlement_epochs LIMIT 1")
            conn.close()
        except Exception as e:
            print(f"WARNING: METAR database incomplete: {e}")

    def _current_datetime(self):
        """Get current datetime in UTC."""
        return datetime.now(timezone.utc).isoformat()

    def _get_daily_metars(self, target_date):
        """Get daily METAR data for a target date from metar DB."""
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        c.execute("""
            SELECT DISTINCT(station) FROM settlement_epochs
            WHERE local_trading_date = ?
            AND epoch_status = 'closed'
            ORDER BY station
        """, (target_date,))

        stations = [row[0] for row in c.fetchall()]
        conn.close()

        return stations

    def _get_settlement_data(self, station, trading_date):
        """
        Get settlement data for a given station and trading date.
        Returns settlement price (0.0-1.0) or None if not available
        """
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        c.execute("""
            SELECT settlement_bucket FROM settlement_epochs
            WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
        """, (station, trading_date))

        row = c.fetchone()
        conn.close()

        return row[0] / 100.0 if row and row[0] is not None else None

    def _get_analytical_probability(self, station, date, signal_direction):
        """
        DETERMINISTIC: Calculate analytical fair value based on historical data.

        This is the 'brain' of the deterministic trading - no ML/AI involved.
        Uses historical frequency, climatology, and recent patterns.

        Includes integration with climatology_pillar for enhanced analytics.

        Returns tuple: (probability estimate 0-1, confidence indicator, additional_metadata)
        """
        # Use historical data to calculate conditional probability
        # P(signal_direction | current conditions) based on historical frequency
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        # Get climatology - probability of temperature moving UP/DOWN on same date
        # based on historical observations from same calendar day
        target_month_day = date[5:10]  # Extract MM-DD from YYYY-MM-DD

        c.execute("""
            SELECT
                avg(CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END) as up_rate,
                count(*) as sample_size
            FROM settlement_epochs
            WHERE station = ?
            AND substr(local_trading_date, 6, 5) = ?
            AND epoch_status = 'closed'
            AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
        """, (station, target_month_day))

        climatology_row = c.fetchone()
        climatology_prob = climatology_row[0] if climatology_row and climatology_row[0] is not None else 0.5
        climatology_sample_size = climatology_row[1] if climatology_row else 0

        # Get recent trend (last 7 days)
        current_month_day = date[5:10]
        c.execute("""
            SELECT
                AVG(CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END) as trend_prob,
                COUNT(*) as trend_samples
            FROM settlement_epochs
            WHERE station = ?
            AND local_trading_date BETWEEN date(?, '-7 days') AND ?
            AND epoch_status = 'closed'
            AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
        """, (station, date, date))

        trend_row = c.fetchone()
        trend_prob = trend_row[0] if trend_row and trend_row[0] is not None else climatology_prob
        trend_samples = trend_row[1] if trend_row else 0

        # Get rolling window of last 30 days for station stability.
        # SQLite has no built-in STDDEV, so fetch the flags and compute volatility in Python.
        c.execute("""
            SELECT CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END as up_flag
            FROM settlement_epochs
            WHERE station = ?
            AND local_trading_date BETWEEN date(?, '-30 days') AND ?
            AND epoch_status = 'closed'
            AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
        """, (station, date, date))

        rolling_flags = [row[0] for row in c.fetchall() if row[0] is not None]
        rolling_prob = sum(rolling_flags) / len(rolling_flags) if rolling_flags else climatology_prob
        volatility = statistics.stdev(rolling_flags) if len(rolling_flags) > 1 else 0.2  # Default

        conn.close()

        # Combine all factors with weighted average
        total_weight = climatology_sample_size + trend_samples + 5  # 5 is arbitrary for rolling baseline
        combined_prob = (
            climatology_prob * climatology_sample_size +
            trend_prob * trend_samples +
            rolling_prob * 5
        ) / total_weight if total_weight > 0 else 0.5

        # Adjust based on signal direction
        if signal_direction == MarketSide.UP:
            prob = combined_prob
        elif signal_direction == MarketSide.DOWN:
            prob = 1.0 - combined_prob
        else:
            prob = 0.5

        # Confidence calculation based on sample sizes and stability
        confidence = min(0.95, max(0.3, 0.3 + min(0.65, (
            climatology_sample_size * 0.1 +  # More data = more confidence
            trend_samples * 0.2 +           # Recent data heavily weighted
            (1 - volatility) * 0.3          # Less volatility = more confidence
        ) / 10.0)))  # Normalize to reasonable range

        return prob, confidence, {
            'climat_prob': climatology_prob,
            'trend_prob': trend_prob,
            'rolling_prob': rolling_prob,
            'climat_samples': climatology_sample_size,
            'trend_samples': trend_samples,
            'volatility': volatility
        }

    def _get_market_price(self, station, date, market_type='HIGH'):
        """
        Get market price from live Kalshi API. Falls back to historical
        heuristic if API is unavailable.
        """
        # Try live Kalshi API first
        try:
            price, meta = _get_live_market_price(station, market_type, date)
            if price is not None and 0.01 <= price <= 0.99:
                # Store metadata for logging
                self._last_market_price_meta = meta
                return price
            # If price is at the boundary (0 or 1), market may be settled
            if price is not None and meta.get('fallback'):
                _LOGGER.info(
                    "market_price_fallback station=%s reason=%s",
                    station, meta.get('source')
                )
        except Exception as e:
            _LOGGER.warning(
                "market_price_live_failed station=%s error=%s", station, e
            )

        # Fallback: historical heuristic (for offline/backtest use)
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        c.execute("""
            SELECT date(min(local_trading_date)), AVG(prior_settlement_bucket),
                   AVG((settlement_bucket - prior_settlement_bucket) / 100.0) as avg_move
            FROM settlement_epochs
            WHERE station = ? AND market_type = ?
            AND local_trading_date BETWEEN date(?, '-14 days') AND ?
            AND epoch_status = 'closed'
        """, (station, market_type, date, date))

        row = c.fetchone()
        conn.close()

        if row and row[1] is not None:
            base_price = min(0.95, max(0.05, ((row[1] or 70.0) - 50) / 40.0))
            avg_move = row[2] or 0.05
            adjusted_price = base_price + avg_move
            return min(0.95, max(0.05, adjusted_price))
        else:
            return 0.5

    def record_explicit_decision_output(self, station, date, market_type, signal_direction,
                                      market_price, analytical_prob, confidence,
                                      reasons=None, trade_version="v2.0", notes=""):
        """
        Record explicit decision output: market implied probability vs analytical fair value + confidence.
        This addresses P1.4 requirement: "Explicit decision output: 'market implied probability vs analytical fair value + confidence'"
        """
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        # For HIGH type market:
        # - signal_direction: which way (UP/DOWN) we think it will move
        # - market_price: market's belief of UP probability
        # - analytical_prob: our calculated probability of UP direction
        market_implied_prob = market_price  # Market price is typically interpreted as implied probability
        # The analytical fair_value depends on how we match market direction expectation
        # Convert analytical_prob to match direction of interest
        if signal_direction == MarketSide.UP:
            analytical_fair_value = analytical_prob
        else:  # DOWN
            analytical_fair_value = 1.0 - analytical_prob

        # Calculate difference and determine recommendation
        price_diff = analytical_fair_value - market_implied_prob

        # Recommendation based on analysis: buy cheap asset / sell expensive one
        if abs(price_diff) > 0.05:  # 5% threshold for action
            if signal_direction == MarketSide.UP:
                if analytical_fair_value > market_implied_prob:
                    recommendation = "BUY_UP"  # Buy YES contract when we think UP prob is higher
                else:
                    recommendation = "SELL_UP"  # Sell YES contract when we think UP prob is lower
            else:  # signal_direction is DOWN
                if analytical_fair_value > 1.0 - market_implied_prob:
                    recommendation = "BUY_DOWN"  # Buy NO contract when we think DOWN prob is higher
                else:
                    recommendation = "SELL_DOWN"
        else:
            recommendation = "HOLD"

        # Check for meaningful divergence
        divergence = abs(price_diff) > 0.10  # If 10%+ divergence detected

        c.execute("""
            INSERT INTO decision_output_log
            (decision_date_utc, station, market_type, forecast_direction, market_implied_prob,
             analytical_fair_value, confidence_level, price_difference, recommendation,
             reasons, divergence_detected, trade_version, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date, station, market_type, signal_direction.value, market_implied_prob,
            analytical_fair_value, confidence, price_diff, recommendation,
            reasons or "", divergence, trade_version, notes or ""
        ))

        conn.commit()
        conn.close()

    def generate_signals(self, date):
        """
        DETERMINISTIC: Generate daily trade signals based on fixed algorithms.
        No ML/AI/LLM involvement.

        Signals include the hourly late_day_momentum signal (threshold=1.7)
        as a first-class participant alongside the existing deterministic signals.

        Returns list of (station, market_type, signal_direction, reason) tuples
        """
        signals = []

        # Determine if we have settlement data for this date
        available_stations = self._get_daily_metars(date)

        if not available_stations:
            print(f"INFO: No settlement data available for {date}, checking for live trading signals instead...")
            available_stations = [s[0] for s in
                [('KATL', 'Atlanta'), ('KBOS', 'Boston'), ('KLAX', 'Los Angeles'),
                 ('KJFK', 'New York'), ('KORD', 'Chicago'), ('KMIA', 'Miami'),
                 ('KSEA', 'Seattle'), ('KSFO', 'San Francisco'), ('KHOU', 'Houston'),
                 ('KPHX', 'Phoenix'), ('KDEN', 'Denver'), ('KATL', 'Atlanta')]]
            available_stations = list(set(available_stations[:6]))  # Use first 6

        # Open METAR DB connection for hourly late-day momentum signal
        metar_conn = sqlite3.connect(self.metar_db, timeout=10)

        for station in available_stations:
            # NOTE: reversion signal (Signal 1) REMOVED from ensemble per Phase 1 fix.
            # Reversion proved unreliable - it used stale fill_price for P&L and had
            # negative directional accuracy on out-of-sample data.

            # Signal 2: Calendar day pattern (climatology-based)
            climatology_direction = self._get_calendar_climatology_direction(station, date)
            if climatology_direction is not None and abs(climatology_direction) > 1.5:  # Meaningful trend over 1.5 points
                if climatology_direction > 0:
                    signals.append((station, "HIGH", MarketSide.UP, "calendar_trend_up"))
                else:
                    signals.append((station, "HIGH", MarketSide.DOWN, "calendar_trend_down"))

            # Signal 3: Hourly late-day momentum (first-class signal, threshold=1.7)
            ldm_direction, ldm_conf, ldm_prob = _ldm_hourly_signal(station, date, metar_conn)
            if ldm_direction is not None:
                market_side = MarketSide.UP if ldm_direction == "up" else MarketSide.DOWN
                signals.append((station, "HIGH", market_side, "late_day_momentum_hourly"))

            # Signal 4: NWP Analog (first-class signal)
            if HAS_NWP_ANALOG and self._nwp_analog:
                try:
                    result = self._nwp_analog.compute_signal(station, date)
                    if result and result.get('direction') is not None and result.get('confidence', 0) > 0:
                        direction = result['direction']
                        market_side = MarketSide.UP if direction == 1 else MarketSide.DOWN
                        signals.append((station, "HIGH", market_side, "nwp_analog"))
                except Exception as e:
                    _LOGGER.warning(f"Failed to compute NWP analog signal for {station} on {date}: {e}")

        metar_conn.close()

        # Also look for late-day METAR momentum patterns if date is today/tomorrow
        if self.is_recent_enough_for_late_day_analysis(date):
            signals.extend(self._analyze_late_day_momentum_signals(date))

        return signals

    def is_recent_enough_for_late_day_analysis(self, date_str):
        """Check if the analysis date is recent enough to have METAR data for analysis"""
        try:
            analysis_date = datetime.fromisoformat(date_str.replace('Z', '+00:00')) if 'Z' in date_str else \
                           datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            today = datetime.now(timezone.utc).date()
            target_date = analysis_date.date()
            # Check if target date is today or recent enough to have METAR data
            return (today - target_date).days <= 1   # Allow for today and yesterday
        except:
            return False

    def _analyze_late_day_momentum_signals(self, date):
        """Analyze late-day METAR patterns for same-day signals (P1.2 late-day plateau/slope)"""
        signals = []

        # This would involve checking late-day temperature patterns for plateaus/slopes
        # Simplified implementation for now
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        for station in ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA']:
            # Look for late-day temperature changes indicating potential momentum
            c.execute("""
                SELECT timestamp_utc, temp_f
                FROM metar_observations
                WHERE station = ?
                AND date_utc = ?
                AND CAST(strftime('%H', timestamp_utc) AS INTEGER) BETWEEN 17 AND 22
                AND temp_f IS NOT NULL
                ORDER BY timestamp_utc ASC
            """, (station, date))

            temp_readings = c.fetchall()
            if len(temp_readings) >= 3:
                temp_change_rate = self.calculate_temperature_trend(temp_readings)

                # If there's sustained late-day movement, suggest continuation (momentum)
                if temp_change_rate > 1.0:  # Rising rapidly
                    signals.append((station, "HIGH", MarketSide.UP, "late_day_upward_momentum"))
                elif temp_change_rate < -1.0:  # Dropping rapidly
                    signals.append((station, "HIGH", MarketSide.DOWN, "late_day_downward_momentum"))

        conn.close()
        return signals

    def calculate_temperature_trend(self, reading_pairs):
        """Simple linear trend calculation between late-day temperature measurements"""
        if len(reading_pairs) < 2:
            return 0

        # Extract temperature values
        temps = [r[1] for r in reading_pairs if r[1] is not None]
        if len(temps) < 2:
            return 0

        # Basic rate calculation over time period
        rate = (temps[-1] - temps[0]) / len(temps) if len(temps) > 0 else 0
        return rate

    def _get_prior_day_reversion(self, station, current_date):
        """Get temperature difference between yesterday's settlement and day before."""
        # This is a simplified reversion estimator. Keep the connection open
        # through both fallback queries.
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()
        try:
            # Get settlements for two consecutive days
            c.execute("""
                SELECT settlement_bucket, prior_settlement_bucket
                FROM settlement_epochs
                WHERE station = ?
                AND local_trading_date BETWEEN date(?, '-1 day') AND ?
                AND epoch_status = 'closed'
                ORDER BY local_trading_date DESC
            """, (station, current_date, current_date))

            rows = c.fetchall()
            if len(rows) >= 2:
                today_settled = rows[0][0]
                yesterday_settlement_prev = rows[1][0]
                if today_settled is not None and yesterday_settlement_prev is not None:
                    return today_settled - yesterday_settlement_prev

            # Alternative: just get prior-to-current move for today
            c.execute("""
                SELECT settlement_bucket, prior_settlement_bucket
                FROM settlement_epochs
                WHERE station = ? AND local_trading_date = ?
                AND epoch_status = 'closed'
            """, (station, current_date))

            row = c.fetchone()
            if row and row[0] is not None and row[1] is not None:
                return row[0] - row[1]  # Current minus prior

            return 0  # Neutral if no data available
        finally:
            conn.close()

    def _get_calendar_climatology_direction(self, station, date):
        """Get historical tendency for this station on this calendar day."""
        conn = sqlite3.connect(self.metar_db)
        c = conn.cursor()

        target_month_day = date[5:10]  # MM-DD

        c.execute("""
            SELECT
                AVG(settlement_bucket - prior_settlement_bucket) as avg_change
            FROM settlement_epochs
            WHERE station = ?
            AND substr(local_trading_date, 6, 5) = ?
            AND settlement_bucket IS NOT NULL
            AND prior_settlement_bucket IS NOT NULL
            AND epoch_status = 'closed'
        """, (station, target_month_day))

        result = c.fetchone()
        conn.close()

        return result[0] if result and result[0] is not None else None

    def place_paper_trade(self, station, market_type, signal_direction,
                         trade_version, functionality, date=None, notes=""):
        """
        Place a paper trade based on signal.

        All trades now include explicit market vs analytical probability recording
        Also records decision output as required by P1.4

        All trades must have:
        - trade_version: Version tag for the algorithm used (e.g., "v1.0_mean_regression")
        - functionality: Description of why this trade happened (e.g., "mean_reversion_daily_pattern")

        Marty's Phase 1 B1.5: Station gating - only trade approved stations.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # B1.5.2: Station approval gate - only trade approved stations
        if not self.is_station_approved(station):
            return {
                'status': 'skipped',
                'reason': f'Station {station} not in approved list: {self.get_approved_stations()}',
                'market_price': None,
                'analytical_prob': None,
                'confidence': None,
                'metadata': {'station_approval_rejected': True}
            }

        # R4-1.3: Settlement-window entry timing gate
        # Only enter trades within T-18h to T-2h before settlement
        # Same-day METARs are ~10x more predictive than prior-day observations
        within_window, window_reason = _is_within_entry_window(station, date)
        if not within_window:
            return {
                'status': 'skipped',
                'reason': f'Entry window: {window_reason}',
                'market_price': None,
                'analytical_prob': None,
                'confidence': None,
                'metadata': {'entry_window_rejection': window_reason}
            }

        # Get market price - in real world this comes from API
        market_price = self._get_market_price(station, date, market_type)

        # Calculate analytical probability using historical deterministics
        analytical_prob, confidence, metadata = self._get_analytical_probability(
            station, date, signal_direction
        )

        # If this is a late_day_momentum_hourly signal, override analytical
        # probability and confidence with the signal's own computed values.
        if functionality == "late_day_momentum_hourly":
            metar_conn = sqlite3.connect(self.metar_db, timeout=10)
            ldm_dir, ldm_conf, ldm_prob = _ldm_hourly_signal(station, date, metar_conn)
            metar_conn.close()
            if ldm_dir is not None:
                analytical_prob = ldm_prob
                confidence = max(confidence, ldm_conf)
                metadata['late_day_momentum_prob'] = ldm_prob
                metadata['late_day_momentum_confidence'] = ldm_conf
                metadata['source'] = 'late_day_momentum_hourly'
                metadata['rate_threshold'] = 1.7

        # Apply calibration to confidence if available
        calibrated_confidence = confidence  # Default to uncalibrated if no calibrator
        if HAS_CALIBRATION_PIPELINE and self._calibrator:
            try:
                # Ensure station is added to available stations for calibration purposes
                if station not in self._calibrator.city_codes:
                    # Need to recreate calibrator with new station included
                    new_cities = list(set(self._calibrator.city_codes + [station]))
                    temp_calibrator = CalibrationPipeline(self._calibrator.signal_names, new_cities)

                    # Copy over existing calibrator's learned data
                    temp_calibrator.calibrators = self._calibrator.calibrators
                    temp_calibrator.fallback_calibrators = self._calibrator.fallback_calibrators
                    temp_calibrator.global_calibrator = self._calibrator.global_calibrator
                    temp_calibrator.history = self._calibrator.history
                    temp_calibrator.refitted = self._calibrator.refitted

                    # Replace the old calibrator
                    self._calibrator = temp_calibrator

                # Determine signal name based on functionality
                if "calendar" in functionality:
                    signal_name = "calendar_climatology"
                elif "momentum" in functionality or "late_day" in functionality:
                    signal_name = "late_day_momentum"
                elif "nwp" in functionality or "analog" in functionality:
                    signal_name = "nwp_analog"
                elif "analysis" in functionality:
                    signal_name = "late_day_analysis"
                else:
                    signal_name = functionality

                # Apply calibration to the confidence
                calibrated_confidence = self._calibrator.calibrate(signal_name, station, confidence)

                # Refit the calibrator periodically to incorporate new data
                # This may happen multiple times as the same function gets called
                # We should be careful not to refit too frequently
            except Exception as e:
                print(f"Calibration failed for {station}, signal: {functionality}: {e}")
                # Fall back to original confidence
                calibrated_confidence = confidence
        else:
            # Use uncalibrated confidence if calibration is not available
            calibrated_confidence = confidence

    def check_kill_switches(self) -> Tuple[bool, List[str]]:
        """
        Check all kill switches and return whether we should stop trading.
        
        Returns:
            Tuple of (should_stop: bool, reasons: List[str])
        """
        reasons = []
        
        daily_loss_ok = self._risk_manager.check_daily_loss()
        if not daily_loss_ok:
            reasons.append("Daily loss limit exceeded")
        
        drawdown_ok = self._risk_manager.check_drawdown()
        if not drawdown_ok:
            reasons.append("Drawdown limit exceeded")
        
        consecutive_losses_ok = self._risk_manager.check_consecutive_losses()
        if not consecutive_losses_ok:
            reasons.append(f"Consecutive losses ({self._risk_manager.consecutive_losses}) exceeded limit")
        
        should_stop = len(reasons) > 0
        return should_stop, reasons

        # Record decision output - this is P1.4 requirement
        self.record_explicit_decision_output(
            station=station,
            date=date,
            market_type=market_type,
            signal_direction=signal_direction,
            market_price=market_price,
            analytical_prob=analytical_prob,
            confidence=calibrated_confidence,
            reasons=functionality,
            trade_version=trade_version,
            notes=notes
        )

        # Decide whether to take the trade based on price vs probability advantage
        fair_price_advantage = analytical_prob - market_price
        price_advantage_threshold = 0.08  # 8% edge requirement for HIGH type markets

        if abs(fair_price_advantage) < price_advantage_threshold:
            return {
                'status': 'skipped',
                'reason': f'Insufficient edge ({abs(fair_price_advantage):.1%} < {price_advantage_threshold:.1%})',
                'market_price': market_price,
                'analytical_prob': analytical_prob,
                'confidence': calibrated_confidence,
                'metadata': metadata
            }

        # Determine trade type based on signal direction vs price discrepancy
        if signal_direction == MarketSide.UP:
            # If we think UP is more likely and market undervalues this, BUY YES
            if analytical_prob > market_price:
                trade_type = TradeType.BUY_YES
            # If we think UP is more likely but market overvalues this, SELL NO
            else:
                trade_type = TradeType.SELL_NO
        elif signal_direction == MarketSide.DOWN:
            # If we think DOWN is more likely (UP is less likely) and market overvalues UP, SELL YES
            if analytical_prob < market_price:
                trade_type = TradeType.SELL_YES
            # If we think DOWN is less likely (UP is more likely) and market undervalues, BUY NO
            else:
                trade_type = TradeType.BUY_NO
        else:
            # Default handling - if confused, skip
            return {
                'status': 'skipped',
                'reason': 'Signal direction unclear',
                'market_price': market_price,
                'analytical_prob': analytical_prob,
                'confidence': confidence,
                'metadata': metadata
            }

        # Calculate position size using confidence-weighted sizing
        current_balance = self.get_current_balance(date)
        sizing_config = KellyPositionSizingConfig()

        # Extract confidence for sizing
        signal_context_for_sizing = {
            "signal_type": functionality,
            "confidence": confidence,
            "momentum_f_per_sec": metadata.get('late_day_momentum_confidence'),
        }
        extracted_confidence = _extract_confidence(signal_context_for_sizing)

        position_size, conf_tier, sizing_meta = _compute_confidence_weighted_size(
            signal_type=functionality,
            confidence=extracted_confidence,
            current_balance=current_balance,
            config=sizing_config,
            market_price=market_price,
        )

        # If position size is 0, skip
        if position_size <= 0:
            return {
                'status': 'skipped',
                'reason': f'Position size is 0 (balance too low or confidence too low)',
                'market_price': market_price,
                'analytical_prob': analytical_prob,
                'confidence': calibrated_confidence,
                'metadata': metadata,
                'sizing_metadata': sizing_meta,
            }

        # R4-1.6: Cluster budget caps + same-city pair hedging
        # Adjust position size based on cluster exposure and city pair net exposure
        cluster_name = _get_cluster_for_station(station)
        cluster_adjusted_size = position_size
        cluster_exposure = 0.0
        city_pair_exposure = 0.0

        if cluster_name:
            # Check current cluster exposure from open positions
            cluster_exposure = self._get_cluster_exposure(cluster_name, date)
            remaining_cluster_budget = _CLUSTER_BUDGET_USD - cluster_exposure
            if remaining_cluster_budget <= 0:
                return {
                    'status': 'skipped',
                    'reason': f'Cluster {cluster_name} budget exhausted (${cluster_exposure:.2f} >= ${_CLUSTER_BUDGET_USD:.2f})',
                    'market_price': market_price,
                    'analytical_prob': analytical_prob,
                    'confidence': calibrated_confidence,
                    'metadata': metadata,
                    'sizing_metadata': sizing_meta,
                    'cluster': cluster_name,
                    'cluster_exposure': cluster_exposure,
                }
            # Cap position size to remaining cluster budget
            cluster_adjusted_size = min(cluster_adjusted_size, remaining_cluster_budget)

        # Check same-city pair net exposure (HIGH + LOW for same station)
        city_pair_exposure = self._get_city_pair_exposure(station, date)
        remaining_city_budget = _CITY_PAIR_CAP_USD - city_pair_exposure
        if remaining_city_budget <= 0:
            return {
                'status': 'skipped',
                'reason': f'City pair {station} net exposure cap reached (${city_pair_exposure:.2f} >= ${_CITY_PAIR_CAP_USD:.2f})',
                'market_price': market_price,
                'analytical_prob': analytical_prob,
                'confidence': calibrated_confidence,
                'metadata': metadata,
                'sizing_metadata': sizing_meta,
                'station': station,
                'city_pair_exposure': city_pair_exposure,
            }
        # Cap position size to remaining city pair budget
        cluster_adjusted_size = min(cluster_adjusted_size, remaining_city_budget)

        # Apply the adjusted size
        position_size = cluster_adjusted_size
        sizing_meta['cluster'] = cluster_name
        sizing_meta['cluster_exposure_before'] = round(cluster_exposure, 2)
        sizing_meta['cluster_budget_cap'] = _CLUSTER_BUDGET_USD
        sizing_meta['city_pair_exposure_before'] = round(city_pair_exposure, 2)
        sizing_meta['city_pair_budget_cap'] = _CITY_PAIR_CAP_USD
        sizing_meta['adjusted_size'] = round(position_size, 2)

        confidence_factor = 0.5 + (confidence * 0.3)  # Legacy factor retained for compatibility

        # Fill the trade at market price (add fee impact)
        fill_price = market_price
        # Fetch current market price for mark-to-market immediately after fill
        # to avoid using stale fill_price for P&L calculations
        current_market_price = self._fetch_current_market_price(station, market_type, date)
        # Use current_market_price for quantity if it provides better sizing;
        # fall back to fill_price (which equals market_price) only when API fails
        effective_price = current_market_price if current_market_price and 0.01 <= current_market_price <= 0.99 else fill_price
        quantity = round(position_size / effective_price) if effective_price > 0.001 else 0

        fee_cost = abs(position_size * self.fee_rate)
        net_cost = position_size + fee_cost * (1 if trade_type in [TradeType.BUY_YES, TradeType.BUY_NO] else -1)

        # Record the trade in our log with enhanced analytics
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        trade_uuid = str(uuid.uuid4())

        c.execute("""
            INSERT INTO trades (
                trade_uuid, trade_date_utc, station, market_type, signal_direction,
                forecast_prob, market_price, trade_type, quantity, position_size_usd, trade_price, trade_cost,
                trade_version, functionality, notes,
                implied_prob, analytical_prob, confidence_indicator,
                calibration_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_uuid, date, station, market_type, signal_direction.value,
            analytical_prob, market_price, trade_type.value, quantity, position_size,
            fill_price, net_cost, trade_version, functionality, notes,
            market_price,  # Implied prob
            analytical_prob,  # Analytical prob
            confidence,  # Confidence indicator
            abs(market_price - analytical_prob)  # Calibration error
        ))

        conn.commit()
        trade_id = c.lastrowid
        conn.close()

        # Open/update position for this trade
        self._update_position_after_trade(trade_uuid, trade_type, fill_price, quantity,
                                        trade_version, functionality, date)

        # Log to decision output table
        self.record_explicit_decision_output(
            station=station,
            date=date,
            market_type=market_type,
            signal_direction=signal_direction,
            market_price=market_price,
            analytical_prob=analytical_prob,
            confidence=confidence,
            reasons=functionality,
            trade_version=trade_version,
            notes=f"Trade generated: {trade_type.value} with edge of {abs(fair_price_advantage):.2%}"
        )


        # Update risk metrics for successful trades
        self.update_risk_metrics_on_trade(net_cost, 'loss' if net_cost < 0 else 'win')

        # Update RiskManager with the new trade result
        from dataclasses import asdict
        trade_result = TradeResult(
            trade_id=trade_uuid,
            pnl=net_cost,  # Note: this is the immediate transaction cost, not finalized P&L
            is_profitable=net_cost > 0,
            trade_date=date
        )
        risk_state_after_execution = self._risk_manager.update_after_trade(trade_result)

        # Check if any risk control has been violated after this trade
        if not risk_state_after_execution.get('passed', True):
            print(f"Risk kill switch triggered for {station} by risk: {risk_state_after_execution.get('checks')} - skipping remaining trades")
            
            # Prepare failure reasons from failed checks
            kill_reasons = []
            checks = risk_state_after_execution.get('checks', {})
            if not checks.get('daily_loss_check', True):
                kill_reasons.append(f"Daily loss limit exceeded ({risk_state_after_execution.get('daily_loss_pct', 0):.1%})")
            if not checks.get('drawdown_check', True):
                kill_reasons.append(f"Drawdown limit exceeded ({risk_state_after_execution.get('drawdown_pct', 0):.1%})")
            if not checks.get('consecutive_losses_check', True):
                kill_reasons.append(f"Consecutive losses exceeded ({risk_state_after_execution.get('consecutive_losses', 0)})")
                
            return {
                'status': 'skipped',
                'reason': f'Risk kill switch activated: {"; ".join(kill_reasons)}',
                'market_price': market_price,
                'analytical_prob': analytical_prob,
                'confidence': calibrated_confidence,
                'metadata': metadata,
                'risk_killed': True,
                'risk_reasons': kill_reasons,
                'risk_state': risk_state_after_execution
            }

        # Compute Sharpe ratio for this trade result
        Sharpe = self.compute_sharpe()

        # Get hit rate from BSS matrix (historical directional accuracy)
        hit_rate, hit_rate_n = self._get_hit_rate(station, signal_direction.value)

        return {
            'status': 'executed',
            'trade_id': trade_id,
            'trade_uuid': trade_uuid,
            'trade_type': trade_type,
            'fill_price': fill_price,
            'quantity': quantity,
            'market_price': market_price,
            'analytical_prob': analytical_prob,
            'confidence': calibrated_confidence,
            'cost': net_cost,
            'fee_cost': fee_cost,
            'metadata': metadata,
            'sharpe': Sharpe,  # Sharpe ratio for alert grading
            'hit_rate': hit_rate,  # Directional accuracy from BSS matrix
            'hit_rate_n': hit_rate_n,  # Sample count for hit rate
            'risk_state': self._risk_metrics.risk_state,
            'risk_reasons': [],  # risk_report provides checks, not kill_switch_reasons
            'detailed_risk_state': risk_state_after_execution  # Detailed risk state from the risk manager
        }

    def _update_position_after_trade(self, trade_uuid, trade_type, fill_price, quantity,
                                   trade_version, functionality, date):
        """Update positions table after a trade execution.

        CRITICAL: Uses current_market_price (via Kalshi API) for unrealized P&L
        and mark-to-market valuation, NOT the fill_price.  The fill_price is ONLY
        used for cost basis (average_cost) calculation.  This ensures unrealized
        P&L always reflects current market reality, not stale execution prices.
        """
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        # Get the trade data to determine station/market_type
        c.execute("SELECT station, market_type, signal_direction FROM trades WHERE trade_uuid = ?", (trade_uuid,))
        row = c.fetchone()
        if not row:
            conn.close()
            return

        station, market_type, market_side = row
        # Use consistent position ID: station_marketType_direction
        position_uuid = f"POS_{station}_{market_type}_{market_side}"

        # Fetch current market price for mark-to-market (NOT fill_price)
        current_market_price = self._fetch_current_market_price(station, market_type, date)

        # Check if position already exists
        c.execute("""
            SELECT id, average_cost, quantity, initial_value_usd, market_price,
                   trade_uuids, trade_versions, realized_pnl
            FROM positions
            WHERE position_uuid = ? AND status = 'active'""",
                  (position_uuid,))
        pos_row = c.fetchone()

        if pos_row:
            # Update existing position
            _, avg_cost, old_qty, initial_val, _old_market_price, uuid_list, version_list, existing_realized_pnl = pos_row

            # Convert stored JSON lists to actual python lists
            uuids = set(json.loads(uuid_list or "[]"))
            versions = set(json.loads(version_list or "[]"))

            # Calculate new weighted average and quantities
            if trade_type in [TradeType.BUY_YES, TradeType.BUY_NO]:
                # Going long (adding to position)
                new_quantity = old_qty + quantity
                new_avg_cost = (avg_cost * old_qty + fill_price * quantity) / new_quantity if new_quantity != 0 else fill_price
                new_initial_value = initial_val + (fill_price * quantity)
            else:  # Selling
                # Going short (could be reducing or flipping position)
                new_quantity = old_qty - quantity
                new_initial_value = initial_val - (fill_price * quantity) if new_quantity != 0 else 0
                new_avg_cost = avg_cost if new_quantity != 0 else fill_price

            uuids.add(trade_uuid)
            versions.add(trade_version)
            realized_pnl = existing_realized_pnl or 0.0
        else:
            # Create new position
            new_quantity = quantity if trade_type in [TradeType.BUY_YES, TradeType.BUY_NO] else -quantity
            new_avg_cost = fill_price
            new_initial_value = fill_price * abs(new_quantity)
            initial_val = new_initial_value
            uuids = {trade_uuid}
            versions = {trade_version}
            realized_pnl = 0.0

        if new_quantity == 0:
            # Fully close position
            c.execute("""
                UPDATE positions
                SET status = 'closed_complete', last_updated_utc = ?, mark_to_market_value = 0,
                    unrealized_pnl = 0, total_pnl = total_pnl + realized_pnl,
                    market_price = ?
                WHERE position_uuid = ?
            """, (self._current_datetime(), current_market_price, position_uuid))
        else:
            # Mark-to-market using CURRENT market price, not fill_price
            # For a long position (new_quantity > 0):
            #   current_value = new_quantity * current_market_price
            #   cost_basis = new_quantity * new_avg_cost
            #   unrealized_pnl = current_value - cost_basis
            # For a short position (new_quantity < 0):
            #   current_value = abs(new_quantity) * (1 - current_market_price)  # value of short
            #   cost_basis = abs(new_quantity) * (1 - new_avg_cost)
            #   unrealized_pnl = cost_basis - current_value  # short profits when price drops
            #
            # Unified formula (works for both long and short):
            #   unrealized_pnl = new_quantity * (current_market_price - new_avg_cost)
            #   where new_quantity > 0 for long, < 0 for short
            current_value = new_quantity * current_market_price
            cost_basis = new_quantity * new_avg_cost
            unrealized_pnl = current_value - cost_basis
            total_pnl = realized_pnl + unrealized_pnl
            mark_to_market_value = current_value

            c.execute("""
                INSERT OR REPLACE INTO positions
                (position_uuid, station, market_type, market_side,
                 average_cost, quantity, initial_value_usd,
                 market_price, mark_to_market_value,
                 realized_pnl, unrealized_pnl, total_pnl,
                 trade_uuids, trade_versions,
                 opened_date_utc, last_updated_utc, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (
                position_uuid, station, market_type, market_side,
                new_avg_cost, new_quantity, new_initial_value,
                current_market_price, mark_to_market_value,
                realized_pnl,
                unrealized_pnl, total_pnl,
                json.dumps(list(uuids)), json.dumps(list(versions)),
                date, self._current_datetime()
            ))

        conn.commit()
        conn.close()

    def _fetch_current_market_price(self, station: str, market_type: str, date: str) -> float:
        """
        Fetch the current market price for mark-to-market valuation.

        Uses the Kalshi price fetcher with thread-safe caching.
        Falls back to the heuristic _get_market_price if the live API fails.
        """
        try:
            price, meta = _get_live_market_price(station, market_type, date)
            if price is not None and 0.01 <= price <= 0.99:
                return price
            # If price is at boundary (0 or 1), market may be settled - use fill price fallback
            if price is not None and meta.get('fallback'):
                _LOGGER.debug(
                    "mtm_price_fallback station=%s market=%s reason=%s",
                    station, market_type, meta.get('source')
                )
                # Fall through to heuristic
        except Exception as e:
            _LOGGER.warning(
                "mtm_price_live_failed station=%s market=%s error=%s", station, market_type, e
            )

        # Fallback: heuristic price (for offline/backtest use)
        return self._get_market_price(station, date, market_type)

    def _get_cluster_exposure(self, cluster_name: str, as_of_date: str) -> float:
        """R4-1.6: Get total USD exposure for a correlation cluster.

        Sums position_size_usd from all open trades for stations in the cluster.
        """
        from station_registry import get_cluster_stations
        cluster_stations = get_cluster_stations(cluster_name)
        if not cluster_stations:
            return 0.0

        placeholders = ','.join('?' * len(cluster_stations))
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()
        try:
            c.execute(f"""
                SELECT COALESCE(SUM(position_size_usd), 0.0)
                FROM trades
                WHERE station IN ({placeholders})
                  AND trade_date_utc = ?
                  AND status = 'open'
            """, (*cluster_stations, as_of_date))
            result = c.fetchone()
            return float(result[0]) if result else 0.0
        finally:
            conn.close()

    def _get_city_pair_exposure(self, station: str, as_of_date: str) -> float:
        """R4-1.6: Get net USD exposure for a city's HIGH+LOW pair.

        Sums position_size_usd from all open trades for this station
        (both HIGH and LOW markets) to check the city pair cap.
        """
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()
        try:
            c.execute("""
                SELECT COALESCE(SUM(position_size_usd), 0.0)
                FROM trades
                WHERE station = ?
                  AND trade_date_utc = ?
                  AND status = 'open'
            """, (station, as_of_date))
            result = c.fetchone()
            return float(result[0]) if result else 0.0
        finally:
            conn.close()

    def mark_positions_to_market(self, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Mark all open positions to current market price.

        This should be called during daily reconciliation and before any
        metric calculation (Sharpe, drawdown, etc.) to ensure unrealized P&L
        reflects current market reality, not stale fill prices.

        Returns summary statistics for observability.
        """
        if as_of_date is None:
            as_of_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        c.execute("""
            SELECT position_uuid, station, market_type, market_side,
                   average_cost, quantity, realized_pnl
            FROM positions
            WHERE status = 'active'
        """)

        positions = c.fetchall()
        updated_count = 0
        total_unrealized = 0.0
        total_realized = 0.0
        errors = []

        for pos_uuid, station, market_type, market_side, avg_cost, qty, realized in positions:
            try:
                current_price = self._fetch_current_market_price(station, market_type, as_of_date)

                # Unified unrealized P&L: qty * (current_price - avg_cost)
                # Works for both long (qty > 0) and short (qty < 0)
                current_value = qty * current_price
                cost_basis = qty * avg_cost
                unrealized_pnl = current_value - cost_basis
                total_pnl = (realized or 0) + unrealized_pnl

                c.execute("""
                    UPDATE positions
                    SET market_price = ?, mark_to_market_value = ?,
                        unrealized_pnl = ?, total_pnl = ?,
                        last_updated_utc = ?
                    WHERE position_uuid = ?
                """, (current_price, current_value, unrealized_pnl, total_pnl,
                      self._current_datetime(), pos_uuid))

                updated_count += 1
                total_unrealized += unrealized_pnl
                total_realized += (realized or 0)
            except Exception as e:
                errors.append({"position": pos_uuid, "station": station, "error": str(e)})
                _LOGGER.error("mtm_update_failed position=%s station=%s error=%s",
                             pos_uuid, station, e)

        conn.commit()
        conn.close()

        return {
            "positions_marked": updated_count,
            "total_unrealized_pnl": round(total_unrealized, 4),
            "total_realized_pnl": round(total_realized, 4),
            "total_pnl": round(total_realized + total_unrealized, 4),
            "errors": errors,
            "as_of_date": as_of_date,
        }

    def process_settlements_for_date(self, settlement_date):
        """
        Process settlements for a specific date. Updates trades with settlement results
        and calculates realized P/L.
        """
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()
        c.execute("ATTACH DATABASE ? AS metar", (self.metar_db,))

        # Get all open trades for which settlements are available
        c.execute("""
            SELECT t.trade_uuid, t.station, t.trade_type, t.trade_price, t.market_price,
                   t.quantity,
                   p.settlement_bucket
            FROM trades t
            JOIN (SELECT station, market_type, local_trading_date, settlement_bucket
                  FROM metar.settlement_epochs
                  WHERE epoch_status = 'closed' AND local_trading_date = ?) p
            ON t.station = p.station AND t.trade_date_utc = date(p.local_trading_date)
               AND t.market_type = p.market_type
            WHERE t.status = 'open'
        """, (settlement_date,))

        unsettled_trades = c.fetchall()

        settled_count = 0
        for trade_uuid, station, trade_type_str, filled_price, market_price, quantity, settlement_value in unsettled_trades:
            # Convert trade type to enum
            try:
                trade_type = TradeType(trade_type_str)
            except ValueError:
                continue

            # Calculate settlement return based on the trade type and settlement value.
            # Long YES pays $1 when event occurs; long NO pays $1 when it does not.
            # Short sides are represented as negative exposure with realized P&L mirrored.
            settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0
            qty = quantity or 0

            if trade_type == TradeType.BUY_YES:
                payout_per_contract = settlement_contract_value
                cost_per_contract = filled_price
                profit = (payout_per_contract - cost_per_contract) * qty
                return_amount = payout_per_contract * qty
            elif trade_type == TradeType.BUY_NO:
                payout_per_contract = 1.0 - settlement_contract_value
                cost_per_contract = 1.0 - filled_price
                profit = (payout_per_contract - cost_per_contract) * qty
                return_amount = payout_per_contract * qty
            elif trade_type == TradeType.SELL_YES:
                # Short YES: collect filled_price, owe event payout.
                profit = (filled_price - settlement_contract_value) * qty
                return_amount = profit
            elif trade_type == TradeType.SELL_NO:
                # Short NO: collect NO price, owe non-event payout.
                no_price = 1.0 - filled_price
                no_payout = 1.0 - settlement_contract_value
                profit = (no_price - no_payout) * qty
                return_amount = profit
            else:
                continue

            # Update the trade with settlement data
            realized_pnl = profit
            c.execute("""
                UPDATE trades
                SET settled_value = ?, settlement_date_utc = ?,
                    realized_pnl = ?, settlement_return_amount = ?,
                    status = 'closed'
                WHERE trade_uuid = ?
            """, (settlement_value/100.0, settlement_date, profit, return_amount, trade_uuid))

            # Update corresponding position (reduce by quantity)
            trade_qty = qty

            # Find position this trade affects and reduce quantity
            c.execute("""
                UPDATE positions
                SET realized_pnl = realized_pnl + ?, quantity = quantity - ?,
                    total_pnl = realized_pnl + ?
                WHERE trade_uuids LIKE ?
            """, (profit, trade_qty, profit, f'%{trade_uuid}%'))

            # Check if trade was directionally correct by comparing signal direction with actual settlement direction
            # Query the original trade for the signal direction and functionality
            c.execute("SELECT signal_direction, functionality, confidence_indicator, forecast_prob FROM trades WHERE trade_uuid = ?", (trade_uuid,))
            trade_row = c.fetchone()

            if trade_row and HAS_CALIBRATION_PIPELINE and self._calibrator:
                orig_signal_direction, functionality, raw_confidence, raw_forecast_prob = trade_row

                # Determine if the prediction was directionally correct
                settlement_is_up = settlement_contract_value > 0.5  # Settlement goes UP if > $0.50 (over 50F)
                predicted_is_up = orig_signal_direction == "UP"  # Signal was UP/DOWN

                # If signal was "UP" and settlement went UP, or signal was "DOWN" and settlement went DOWN, then correct
                was_correct = (predicted_is_up == settlement_is_up)

                # Extract signal name based on functionality
                if "calendar" in functionality:
                    signal_name = "calendar_climatology"
                elif "momentum" in functionality or "late_day" in functionality:
                    signal_name = "late_day_momentum"  # Using appropriate name for the late-day momentum signal
                elif "nwp" in functionality or "analog" in functionality:
                    signal_name = "nwp_analog"
                elif "analysis" in functionality:
                    signal_name = "late_day_analysis"
                else:
                    # Default to the functionality field as signal name
                    signal_name = functionality

                # Feed this result back to calibration pipeline
                try:
                    # Use raw_forecast_prob as primary confidence, fall back to raw_confidence if available
                    conf_to_use = raw_forecast_prob if raw_forecast_prob is not None else (raw_confidence if raw_confidence is not None else 0.5)
                    self._calibrator.update(signal_name, station, conf_to_use, was_correct)
                except Exception as e:
                    print(f"Error feeding trade to calibrator: {e}")

            # Update RiskManager with realized P&L after settlement
            settlement_trade_result = TradeResult(
                trade_id=trade_uuid,
                pnl=realized_pnl,
                is_profitable=realized_pnl > 0,
                trade_date=settlement_date
            )
            risk_state_after_settlement = self._risk_manager.update_after_trade(settlement_trade_result)

            settled_count += 1

        conn.commit()
        conn.close()
        print(f"Processed {settled_count} trade settlements for date {settlement_date}")

    def daily_reconciliation(self, reconcile_date=None):
        """
        Perform daily reconciliation of all positions and calculate metrics.

        Includes:
        - Settlement processing for the date
        - Mark-to-market of all open positions (using current Kalshi prices)
        - Both realized and unrealized P&L in daily P&L calculation
        """
        if reconcile_date is None:
            reconcile_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # Process any settlements for the reconcile date
        self.process_settlements_for_date(reconcile_date)

        # Mark all open positions to current market price
        mtm_result = self.mark_positions_to_market(reconcile_date)

        # Get opening balance from previous day
        prev_balance = self.get_current_balance(reconcile_date)

        # Count trades and other details for this date
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        # Today's trades
        c.execute("""
            SELECT trade_cost, realized_pnl FROM trades
            WHERE trade_date_utc = ?
            AND (status = 'open' OR status = 'closed')
        """, (reconcile_date,))
        today_all_trades = c.fetchall()

        # Open positions (positions opened before today but still active)
        c.execute("""
            SELECT COUNT(*) FROM positions
            WHERE DATE(opened_date_utc) < ?
            AND status = 'active'
        """, (reconcile_date,))
        open_positions = c.fetchone()[0]

        # Get total unrealized P&L from all open positions (after MTM update)
        c.execute("""
            SELECT COALESCE(SUM(unrealized_pnl), 0.0) FROM positions
            WHERE status = 'active'
        """)
        total_unrealized_pnl = c.fetchone()[0]

        # Settled trades for today (if any processing occurred)
        c.execute("""
            SELECT realized_pnl FROM trades
            WHERE settlement_date_utc = ? AND status = 'closed'
        """, (reconcile_date,))
        settled_records = c.fetchall()

        settled_pnls = [r[0] for r in settled_records if r[0] is not None]
        winning_trade_count = sum(1 for pnl in settled_pnls if pnl > 0)
        losing_trade_count = sum(1 for pnl in settled_pnls if pnl < 0)

        # Today's new trade count
        c.execute("SELECT COUNT(*) FROM trades WHERE trade_date_utc = ?", (reconcile_date,))
        total_new_trades = c.fetchone()[0]

        # Calculate P&L components and fees
        total_fees = sum(abs(t[0]) * self.fee_rate for t in today_all_trades)
        today_realized_pnl = sum(t[1] or 0 for t in today_all_trades)

        # Daily P&L = realized P&L (from settled trades today) + change in unrealized P&L
        # The unrealized P&L is already updated via mark_positions_to_market
        total_daily_pnl = today_realized_pnl + total_unrealized_pnl

        # Closing balance = opening + realized P&L + unrealized P&L - fees
        closing_balance = prev_balance + today_realized_pnl + total_unrealized_pnl - total_fees

        # Track drawdown from peak - simplified implementation
        c.execute("""
            SELECT MAX(closing_balance)
            FROM daily_balances
            WHERE date_utc <= date(?, '-1 day')
        """, (reconcile_date,))
        peak = c.fetchone()[0] or self.initial_balance
        max_drawdown = min(0.0, closing_balance - peak)

        # Record daily reconciliation (INSERT OR REPLACE to allow re-runs)
        c.execute("""
            INSERT OR REPLACE INTO daily_balances (
                date_utc, opening_balance, closing_balance, pnl,
                trade_count, position_count, winning_trades, losing_trades,
                settled_trades, position_size_weight, total_fees_paid,
                max_drawdown_since_high
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            reconcile_date, prev_balance, closing_balance, total_daily_pnl,
            len(today_all_trades), open_positions, winning_trade_count,
            losing_trade_count, len(settled_records),
            sum(abs(t[0]) for t in today_all_trades) / max(len(today_all_trades), 1),
            total_fees, max_drawdown
        ))

        conn.commit()
        conn.close()

        # Calculate and store calibration metrics regularly
        self.calculate_calibration_metrics_for_date(reconcile_date)

        return {
            'date': reconcile_date,
            'opening_balance': prev_balance,
            'closing_balance': closing_balance,
            'daily_pnl': total_daily_pnl,
            'trade_count': len(today_all_trades),
            'open_positions': open_positions,
            'winning_trades': winning_trade_count,
            'losing_trades': losing_trade_count,
            'settled_trades': len(settled_records),
            'new_trades_today': total_new_trades,
            'total_fees': total_fees,
            'max_drawdown': max_drawdown
        }

    def calculate_calibration_metrics_for_date(self, for_date):
        """Calculate and store calibration metrics for the dashboard."""
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        # Get resolved trades for calibration calculation
        c.execute("""
            SELECT implied_prob, analytical_prob, confidence_indicator
            FROM trades
            WHERE settlement_date_utc <= ? AND status = 'closed'
            AND implied_prob IS NOT NULL AND analytical_prob IS NOT NULL
        """, (for_date,))

        resolved_trades = c.fetchall()

        if len(resolved_trades) >= 20:  # Require minimum number for meaningful metrics
            implieds = [t[0] for t in resolved_trades]
            analytics = [t[1] for t in resolved_trades]
            confidences = [t[2] for t in resolved_trades if t[2] is not None]

            # Calculate Brier score (proper implementation would need actual results)
            # For now, using placeholder approach until settlement data is fully processed
            brier_score, ece, avg_conf = self._calculate_simple_calibration_metrics(
                [t[1] for t in resolved_trades],  # our probabilities
                []  # would be actual outcomes
            )

            # Store in calibration table for dashboard
            c.execute("""
                INSERT INTO calibration_metrics
                (report_date_utc, brier_score, expected_calibration_error,
                 avg_confidence, total_trades, total_resolved)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (for_date, brier_score, ece,
                  statistics.mean(confidences) if confidences else 0.5,
                  len(resolved_trades), len(resolved_trades)))

            conn.commit()

        conn.close()

    def _calculate_simple_calibration_metrics(self, probabilities, actual_outcomes):
        """Simple version of calibration calculations when outcomes are available."""
        # This is a stub; implementation requires actual resolved trade outcomes
        return 0.20, 0.02, 0.7  # placeholder values

    def get_current_balance(self, for_date=None):
        """Get current account balance as of given date."""
        if for_date is None:
            for_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        c.execute("""
            SELECT closing_balance FROM daily_balances
            WHERE date_utc < ?
            ORDER BY date_utc DESC LIMIT 1
        """, (for_date,))

        result = c.fetchone()
        conn.close()

        return result[0] if result else self.initial_balance

    def get_version_performance(self, trade_version):
        """Query performance by specific trade version."""
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        c.execute("""
            SELECT
                COUNT(*) as trade_count,
                AVG(ABS(realized_pnl)) as avg_pnl,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(realized_pnl) as total_pnl
            FROM trades
            WHERE trade_version = ? AND status = 'closed'
        """, (trade_version,))

        result = c.fetchone()
        conn.close()

        return {
            'trade_count': result[0],
            'avg_pnl': result[1],
            'wins': result[2],
            'losses': result[3],
            'total_pnl': result[4],
            'win_rate': result[2]/(result[2]+result[3]) if (result[2]+result[3]) > 0 else 0
        }

    def generate_calibration_report(self, save_path=CALIBRATION_REPORT_PATH):
        """Generate a JSON report of calibration metrics for the dashboard."""
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        # Get latest calibration metrics
        c.execute("""
            SELECT brier_score, expected_calibration_error, avg_confidence,
                   total_trades, total_resolved
            FROM calibration_metrics
            ORDER BY report_date_utc DESC
            LIMIT 1
        """)

        row = c.fetchone()
        conn.close()

        if row:
            # Prepare base report
            report = {
                'generated_at': self._current_datetime(),
                'latest_metrics': {
                    'brier_score': row[0],
                    'expected_calibration_error': row[1],
                    'average_confidence': row[2],
                    'total_trades_analyzed': row[3],
                    'resolved_trades': row[4]
                }
            }

            # Add calibration pipeline metrics if available
            if HAS_CALIBRATION_PIPELINE and self._calibrator:
                try:
                    # Refit calibrators to get current stats
                    self._calibrator.refit()

                    # Count various types of calibrators
                    per_signal_per_city_count = len(self._calibrator.calibrators)
                    per_signal_global_count = len(self._calibrator.fallback_calibrators)
                    global_count = 1 if self._calibrator.global_calibrator is not None else 0

                    calibration_info = {
                        'calibration_pipeline_enabled': True,
                        'calibrators_fitted': {
                            'per_signal_per_city': per_signal_per_city_count,
                            'per_signal_global': per_signal_global_count,
                            'global': global_count,
                        },
                        'total_signal_city_combinations': len(self._calibrator.signal_names) * len(self._calibrator.city_codes),
                        'signals_covered': self._calibrator.signal_names,
                        'cities_covered': list(self._calibrator.city_codes),
                        'data_points_total': sum(len(hist) for hist in self._calibrator.history.values()),
                        'data_distribution': {str(k): len(v) for k, v in self._calibrator.history.items()}
                    }

                    report['calibration_pipeline_metrics'] = calibration_info

                except Exception as e:
                    print(f"Error generating calibration pipeline report: {e}")
                    report['calibration_pipeline_metrics'] = {
                        'calibration_pipeline_enabled': True,
                        'error_generating_report': str(e)
                    }
            else:
                report['calibration_pipeline_metrics'] = {
                    'calibration_pipeline_enabled': False
                }

            # Write to file
            report_dir = os.path.dirname(save_path)
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)

            with open(save_path, 'w') as f:
                json.dump(report, f, indent=2)

            print(f"Calibration report generated: {save_path}")
            return report
        else:
            print("No calibration data available yet")
            return {}

    # ─── Marty's Phase 1 B1.5: Risk Report ──────────────────────────────

    def risk_report(self) -> dict:
        """
        Generate a risk report with current exposure, daily P&L, and kill switch status.

        Returns a dict suitable for logging, alerting, or dashboard display.
        This is the main public interface for risk status checking.
        """
        return risk_report(self._risk_metrics, self._risk_config)

    def format_risk_alert(self) -> str:
        """Format risk metrics as a human-readable alert string."""
        return format_risk_alert(self._risk_metrics)

    def build_paper_trade_alert(self, trade_result: Dict[str, Any], station: str,
                                market_type: str, direction: str) -> Dict[str, Any]:
        """
        Build a slim paper-trade alert with S/A/B/C/D/F Opportunity Grade + Edge.

        Uses the alert_builder module to:
        - Compute Opportunity Grade (S/A/B/C/D/F) based on trade confidence, market probability, and Sharpe
        - Calculate Edge (Trade Conf - Market prob)
        - Classify lane (Regular, Sure_Thing, Goldilocks)
        - Build slim Discord message format with real Kalshi URLs
        - Include hit rate from historical BSS matrix
        """
        hit_rate = trade_result.get('hit_rate')
        hit_rate_n = trade_result.get('hit_rate_n')
        return build_paper_trade_alert(trade_result, station, market_type, direction, INSTANCE, hit_rate, hit_rate_n)

    def build_paper_trade_alert_dev(self, trade_result: Dict[str, Any], station: str,
                                   market_type: str, direction: str) -> Dict[str, Any]:
        """
        DEV variant: Build paper-trade alert with Enhanced Opportunity Grade.
        """
        hit_rate = trade_result.get('hit_rate')
        hit_rate_n = trade_result.get('hit_rate_n')
        return build_paper_trade_alert_dev(trade_result, station, market_type, direction, INSTANCE, hit_rate, hit_rate_n)

    def compute_sharpe(self, trades: List[Dict[str, Any]] = None) -> float:
        """
        Compute Sharpe ratio from historical trades.

        Args:
            trades: Optional list of trade results with pnl fields. If None, uses daily trades.

        Returns:
            Sharpe ratio (0 if insufficient data)
        """
        if trades is None:
            # Get trades from today
            trades = self._get_daily_trades()

        if len(trades) < 2:
            return 1.0  # Default to 1.0 for insufficient data

        # Calculate returns
        returns = [t.get('pnl', 0) / max(abs(t.get('position_size_usd', 100)), 1) for t in trades]

        # Calculate mean return and standard deviation
        mean_return = statistics.mean(returns)
        std_return = statistics.stdev(returns) if len(returns) > 1 else 0.01

        # Calculate Sharpe ratio (assuming risk-free rate = 0)
        if std_return > 0:
            sharpe = mean_return / std_return
        else:
            sharpe = float('inf') if mean_return > 0 else 0.0

        return sharpe

    def _get_daily_trades(self) -> List[Dict[str, Any]]:
        """Get trades from today for Sharpe calculation."""
        conn = sqlite3.connect(self.paper_db)
        c = conn.cursor()

        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        c.execute("""
            SELECT trade_uuid, position_size_usd, trade_cost as pnl, status
            FROM trades
            WHERE trade_date_utc = ?
        """, (today,))

        trades = []
        for row in c.fetchall():
            trades.append({
                'trade_uuid': row[0],
                'position_size_usd': row[1],
                'pnl': row[2],
                'status': row[3]
            })

        conn.close()
        return trades

    def _get_hit_rate(self, station: str, direction: str) -> Tuple[float, int]:
        """
        Get historical hit rate for a station+direction combination.

        Looks up the directional accuracy from the per-station BSS matrix / B7 backtests.

        Args:
            station: Station ICAO code
            direction: 'UP' or 'DOWN'

        Returns:
            Tuple of (hit_rate as 0.0-1.0, sample count)
        """
        # Default: 50% hit rate (neutral) with minimal confidence
        default_hit_rate = 0.50
        default_n = 0

        try:
            conn = sqlite3.connect(self.paper_db)
            c = conn.cursor()

            # Look up hit rate from historical directional accuracy table
            c.execute("""
                SELECT hit_rate, sample_count
                FROM station_hit_rates
                WHERE station = ? AND direction = ?
            """, (station, direction))

            row = c.fetchone()
            conn.close()

            if row:
                return row[0], row[1]

            return default_hit_rate, default_n

        except sqlite3.Error:
            # Table doesn't exist yet, return default
            return default_hit_rate, default_n

    def check_kill_switches(self) -> Tuple[bool, List[str]]:
        """
        Evaluate all kill switch conditions.

        Returns (should_halt, list_of_reasons).
        """
        reasons = []

        # Check max daily loss
        max_daily_loss_dollars = self._risk_config.max_daily_loss_percent * self._risk_config.initial_capital
        if max_daily_loss_dollars > 0 and self._risk_metrics.daily_pnl < -max_daily_loss_dollars:
            reasons.append(f"Daily loss (${abs(self._risk_metrics.daily_pnl):.2f}) exceeds limit (${max_daily_loss_dollars:.2f})")

        # Check max drawdown
        if self._risk_config.max_drawdown_pct and self._risk_metrics.max_drawdown_pct >= self._risk_config.max_drawdown_pct:
            reasons.append(f"Drawdown ({self._risk_metrics.max_drawdown_pct:.1%}) exceeds limit ({self._risk_config.max_drawdown_pct:.1%})")

        # Check consecutive losses (from risk_metrics)
        if self._risk_metrics.consecutive_losses >= self._risk_config.max_consecutive_losses:
            reasons.append(f"Consecutive losses ({self._risk_metrics.consecutive_losses}) >= limit ({self._risk_config.max_consecutive_losses})")

        return len(reasons) > 0, reasons

    def update_risk_metrics_on_trade(self, trade_pnl: float, trade_result: str):
        """
        Update risk metrics after a trade.

        Args:
            trade_pnl: Profit/loss from this trade (positive = profit, negative = loss)
            trade_result: 'win' or 'loss' for tracking consecutive losses
        """
        self._risk_metrics.daily_pnl += trade_pnl

        # Track daily loss count for consecutive loss tracking
        if trade_pnl < 0:
            self._risk_metrics.consecutive_losses += 1
        else:
            self._risk_metrics.consecutive_losses = 0

        # Track peak balance and drawdown
        if self._risk_metrics.current_balance > self._risk_metrics.peak_balance:
            self._risk_metrics.peak_balance = self._risk_metrics.current_balance

        drawdown_pct = 0.0
        if self._risk_metrics.peak_balance > 0:
            drawdown_pct = (self._risk_metrics.peak_balance - self._risk_metrics.current_balance) / self._risk_metrics.peak_balance * 100

        if drawdown_pct > self._risk_metrics.max_drawdown_pct:
            self._risk_metrics.max_drawdown_pct = drawdown_pct

                # Re-evaluate risk state
        risk_state = evaluate_risk_state(self._risk_manager)
        self._risk_metrics.risk_state = risk_state

    def is_station_approved(self, station: str) -> bool:
        """Check if a station is in the approved list (B1.5.2)."""
        approved_stations = self.get_approved_stations()
        if not approved_stations:
            return True  # If no specific stations are approved, all are allowed
        return is_station_approved(station, approved_stations)

    def get_approved_stations(self) -> List[str]:
        """Return list of approved station codes."""
        import station_registry
        return station_registry.get_all_stations()
        return get_all_stations()


def daily_paper_run(run_date=None):
    """Execute paper trading for the given date."""
    if run_date is None:
        run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    print(f"PAPER TRADING RUN - {run_date}")
    print("=" * 70)

    # Increased initial balance for more realistic trading
    trader = PaperTrader(initial_balance=10000.0, fee_rate=0.001)  # 0.1% fee

    # Marty's Phase 1 B1.5: Check kill switches before trading
    should_halt, kill_reasons = trader.check_kill_switches()
    if should_halt:
        print("\n=== RISK GUARDRAILS KILL SWITCH TRIGGERED ===")
        print(trader.format_risk_alert())
        print("\nTrading halted. Resolve issues before resuming.")
        return {
            'status': 'halted',
            'reasons': kill_reasons,
            'date': run_date
        }

    # Generate signals for all stations with available data
    print(f"Generating signals for {run_date}...")
    signals = trader.generate_signals(run_date)

    print(f"Generated {len(signals)} signals:")
    for i, (station, mtype, direction, reason) in enumerate(signals):
        print(f"  {i+1:2d}. {station} {mtype} {direction.value:>4s}: {reason}")

    # Execute trades based on signals
    executed = 0
    skipped = 0
    trade_results = []

    for station, market_type, signal_direction, reason in signals:
        # Version-tag late_day_momentum_hourly trades distinctly
        if reason == "late_day_momentum_hourly":
            trade_ver = "v2.1_ldm_hourly"
        else:
            trade_ver = "v2.0_paper_trade"

        result = trader.place_paper_trade(
            station=station,
            market_type=market_type,
            signal_direction=signal_direction,
            trade_version=trade_ver,
            functionality=reason,
            date=run_date,
            notes=f"Generated from signal: {reason}"
        )

        if result['status'] == 'executed':
            # Build alert with S/A/B/C/D/F Opportunity Grade + Edge
            alert_data = trader.build_paper_trade_alert(
                trade_result=result,
                station=station,
                market_type=market_type,
                direction=signal_direction,
            )

            # Format for Discord
            discord_payload = format_alert_for_discord(alert_data)

            # Print alert to console (skip if alert was filtered by hard filters)
            if alert_data.get('skip_reason'):
                print(f"  [FILTERED] {station}:{market_type} {result['trade_type'].value.upper()} - {alert_data['skip_reason']}")
                skipped += 1
            else:
                print(f"  [ALERT] {station}:{market_type} {result['trade_type'].value.upper()} ")
                print(f"         Grade: {alert_data['grade']} | Lane: {alert_data['lane_label']}")
                print(f"         Edge: {alert_data['edge_pct']} | Sharpe: {alert_data['Sharpe']:.1f}")
                print(f"         Conf: {alert_data['trade_confidence']:.0%} | Market prob: {alert_data['market_prob']:.2%}")
                print(f"         Market URL: {alert_data['market_url']}")
                print(f"         Amount: ${result['cost']:.2f}")

            # Store alert data in result for tracking
            result['alert_data'] = alert_data
            executed += 1
            trade_results.append(result)
        else:
            print(f"  [SKIP] {station}:{signal_direction.value} {result['reason']}")
            skipped += 1

    print(f"\nTrade execution: {executed} executed, {skipped} skipped")

    # Perform daily reconciliation (includes settlement processing if available)
    print(f"\nDaily Reconciliation for {run_date}...")
    reconciled = trader.daily_reconciliation(run_date)

    print("Reconciliation Summary:")
    print(f"  Trades executed:    {reconciled['new_trades_today']}")
    print(f"  Open positions:     {reconciled['open_positions']}")
    print(f"  Settled today:      {reconciled['settled_trades']}")
    print(f"  Win/Loss:           {reconciled['winning_trades']}/{reconciled['losing_trades']}")
    print(f"  Fees paid:          ${reconciled['total_fees']:.2f}")
    print(f"  Daily P&L:          ${reconciled['daily_pnl']:.2f}")
    print(f"  Max drawdown:       ${reconciled['max_drawdown']:.2f}")

    print(f"\nAccount Balance:")
    print(f"  Opening:            ${reconciled['opening_balance']:.2f}")
    print(f"  Closing:            ${reconciled['closing_balance']:.2f}")
    print(f"  Daily Change:       ${reconciled['daily_pnl']:.2f}")

    # Update risk metrics with reconciliation results
    trader._risk_metrics.current_balance = reconciled['closing_balance']
    trader._risk_metrics.daily_pnl = reconciled['daily_pnl']
    trader._risk_metrics.max_drawdown_pct = (reconciled['opening_balance'] - reconciled['closing_balance']) / reconciled['opening_balance'] * 100 if reconciled['opening_balance'] > 0 else 0

    # Re-evaluate risk state after reconciliation
    trader._risk_metrics.risk_state = evaluate_risk_state(trader._risk_manager)

    print(f"\n=== RISK GUARDRAILS STATUS ===")
    risk_report = trader.risk_report()
    print(f"  Risk State: {risk_report['risk_state'].upper()}")
    print(f"  Current Balance: ${risk_report['current_balance_usd']:,.2f}")
    print(f"  Daily P&L: ${risk_report['daily_pnl_usd']:,.2f}")
    print(f"  Max Drawdown: {risk_report['max_drawdown_pct']:.1%}")
    print(f"  Consecutive Losses: {risk_report['consecutive_losses']}")
    if risk_report['kill_switch_reasons']:
        print("  Kill Switch Triggers:")
        for reason in risk_report['kill_switch_reasons']:
            print(f"    - {reason}")
    else:
        print("  Kill Switch Status: OK (no triggers)")

    # Generate enhanced calibration report for P1.5
    print(f"\nGenerating calibration report...")
    calibration_metrics = trader.generate_calibration_report()

    print("\nRun complete.")


if __name__ == "__main__":
    daily_paper_run()
