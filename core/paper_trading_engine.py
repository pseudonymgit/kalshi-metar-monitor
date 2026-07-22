#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 Phase 20.1: Monolith decomposition - extracted into signal_pipeline.py, trade_execution.py, pnl_tracking.py, settlement_processor.py]
# 2. [2026-07-21 Phase 4.4: Adaptive confidence thresholds - rolling 30d accuracy-based threshold adjustment]
# 3. [2026-07-21 Phase 4.7: Intraday METAR confirmation - same-day temperature trend confirmation signal]
# 4. [2026-07-21 Phase 4.5: Ensemble diversity score - penalize confidence when signals are redundant]
# 5. [2026-07-21 Phase 4.3: Dewpoint depression modulator - confidence boost/penalty based on humidity]
# 6. [2026-07-21 Phase 4.1: Spatial coherence gate - group 20 stations into 6 regions, confidence modulation based on regional consensus]
# 7. [2026-07-21 Fix ERA5 backfill: numpy array truthiness in process_netcdf_file]
# 8. [2026-07-21 Phase 8: Configure agreement threshold=3 (env var AGREEMENT_THRESHOLD), align with agreement_gate.py default]
# 9. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 10. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#
"""
PAPER TRADING ENGINE - Facade Module (v2.0)

This module now re-exports from the decomposed sub-modules:
  - signal_pipeline.py: Signal generation, probability estimation
  - trade_execution.py: Trade placement, position management, mark-to-market
  - pnl_tracking.py: P&L tracking, reconciliation, calibration metrics
  - settlement_processor.py: Settlement processing

PaperTrader class remains as the coordinator, delegating to sub-module functions.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from enum import Enum

from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .signal_pipeline import (
    _get_daily_metars, _get_settlement_data, _get_analytical_probability,
    _get_market_price, record_explicit_decision_output, generate_signals,
    is_recent_enough_for_late_day_analysis, _analyze_late_day_momentum_signals,
    calculate_temperature_trend, _get_prior_day_reversion,
    _get_calendar_climatology_direction, _get_prev_day_high_temperature,
)
from .trade_execution import (
    TradeType, MarketSide, place_paper_trade, _update_position_after_trade,
    _compute_round_trip_cost, _fetch_current_market_price,
    _get_cluster_exposure, _get_city_pair_exposure, mark_positions_to_market,
)
from .pnl_tracking import (
    process_settlements_for_date, daily_reconciliation,
    calculate_calibration_metrics_for_date, _calculate_simple_calibration_metrics,
    get_current_balance, get_version_performance, generate_calibration_report,
    compute_sharpe, _get_daily_trades, _get_hit_rate, update_risk_metrics_on_trade,
)
from .settlement_processor import SettlementProcessor

# Re-import risk controls
from .risk_controls import (
    RiskMetrics, RiskState, RiskConfig, DEFAULT_RISK_CONFIG,
    is_station_approved, get_approved_stations, evaluate_risk_state,
    risk_report, format_risk_alert, RiskManager, TradeResult,
)
from .alert_builder import (
    build_paper_trade_alert, format_alert_for_discord,
    OpportunityGrade, LaneType, PAPER_TRADE_ALERT_SCHEMA_VERSION,
    build_paper_trade_alert_dev,
)
from .alert_dispatcher import dispatch_current_alert

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_DB_PATH = str(REPO_ROOT / "data" / "paper_trading.db")
METAR_DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
CALIBRATION_REPORT_PATH = str(REPO_ROOT / "reports" / "paper_trading_calibration.json")

# Configuration flags
INSTANCE = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()
GOLDILOCKS_SEPARATE_LANE = True
_LOGGER = logging.getLogger(__name__)

RISK_CONFIG = DEFAULT_RISK_CONFIG

# Re-export signal pipeline functions
__all__ = [
    'PaperTrader', 'TradeType', 'MarketSide', 'daily_paper_run',
    'PAPER_DB_PATH', 'METAR_DB_PATH', 'CALIBRATION_REPORT_PATH',
    'RISK_CONFIG', 'INSTANCE', 'GOLDILOCKS_SEPARATE_LANE',
    # Delegated functions
    'generate_signals', 'place_paper_trade', 'mark_positions_to_market',
    'process_settlements_for_date', 'daily_reconciliation',
    'check_kill_switches', 'risk_report', 'compute_sharpe',
]


class PaperTrader:
    """Paper trading engine - coordinator that delegates to sub-module functions."""

    def __init__(self,
                 db_path: str = PAPER_DB_PATH,
                 initial_balance: float = 10000.0,
                 trade_version: str = "v2.0",
                 instance: str = INSTANCE,
                 risk_config: Optional[Dict[str, Any]] = None):
        self.db_path = db_path
        self.initial_balance = initial_balance
        self.trade_version = trade_version
        self.instance = instance
        self.risk_config = risk_config or RISK_CONFIG
        self.balance = initial_balance
        self._init_paper_db()
        self._init_metar_db_if_needed()
        self.settlement_processor = SettlementProcessor(db_path)
        _LOGGER.info(f"PaperTrader initialized: version={trade_version}, instance={instance}, db={db_path}")

    def _init_paper_db(self):
        """Initialize paper trading database schema."""
        conn = get_sqlite_connection(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_uuid TEXT UNIQUE NOT NULL,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    fill_price REAL,
                    trade_type TEXT NOT NULL,
                    trade_version TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_output_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    signal_direction TEXT,
                    analytical_probability REAL,
                    market_price REAL,
                    trade_version TEXT NOT NULL,
                    logged_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_balances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    balance_date TEXT NOT NULL UNIQUE,
                    balance REAL NOT NULL,
                    peak_balance REAL,
                    drawdown_pct REAL,
                    trade_version TEXT NOT NULL,
                    recorded_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_uuid TEXT UNIQUE NOT NULL,
                    station TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                current_price REAL,
                    entry_date TEXT NOT NULL,
                    is_open INTEGER DEFAULT 1,
                    trade_version TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calibration_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_date TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL,
                    trade_version TEXT NOT NULL,
                    recorded_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _init_metar_db_if_needed(self):
        """Initialize METAR database schema if not present."""
        from . import metar_monitor
        metar_monitor._ensure_alert_schema()

    def _current_datetime(self):
        return datetime.now(timezone.utc)

    def risk_report(self) -> dict:
        return risk_report(self.db_path, RISK_CONFIG)

    def format_risk_alert(self) -> str:
        return format_risk_alert()

    def build_paper_trade_alert(self, trade_result, station, market_type, direction, confidence, grade):
        return build_paper_trade_alert(
            trade_result=trade_result, station=station,
            market_type=market_type, direction=direction,
            confidence=confidence, grade=grade,
        )

    def build_paper_trade_alert_dev(self, trade_result, station, market_type, direction, confidence, grade):
        return build_paper_trade_alert_dev(
            trade_result=trade_result, station=station,
            market_type=market_type, direction=direction,
            confidence=confidence, grade=grade,
        )

    def check_kill_switches(self) -> Tuple[bool, List[str]]:
        """Check if any kill switches are active."""
        from .risk_controls import check_kill_switches as _check_kill_switches
        return _check_kill_switches(self.db_path, self.risk_config)

    def is_station_approved(self, station: str) -> bool:
        return is_station_approved(station)

    def get_approved_stations(self) -> List[str]:
        return get_approved_stations()
def daily_paper_run(run_date=None):
    """Execute paper trading for the given date."""
    if run_date is None:
        run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    print(f"PAPER TRADING RUN - {run_date}")
    print("=" * 70)

    # Increased initial balance for more realistic trading
    trader = PaperTrader(initial_balance=10000.0, fee_rate=0.0)  # 0.1% fee

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

    # Apply spatial coherence gate - enhance confidence based on regional agreement
    SPATIAL_COHERENCE_ENABLED = bool(int(os.getenv('SPATIAL_COHERENCE_ENABLED', '1')))  # Enabled by default, 0/1
    
    if SPATIAL_COHERENCE_ENABLED:
        print("Applying spatial coherence gate...")
        signals_with_conf = apply_spatial_coherence_gate(signals, run_date, trader.metar_db)
        
        # Count signal adjustments for logging
        conf_modified = 0
        for orig_sig, new_sig in zip(signals, signals_with_conf):
            if orig_sig[-1] != new_sig[-1]:  # Compare confidence values
                conf_modified += 1
        
        print(f"Applied spatial coherence to {len(signals_with_conf)} signals, modified confidence of {conf_modified} signals")
        signals = signals_with_conf
        
        # Apply dewpoint depression modulation after spatial coherence gate
        DEWPOINT_MODULATION_ENABLED = dewpoint_depression_modulation_enabled
        if DEWPOINT_MODULATION_ENABLED and dewpoint_depression_imported:
            print("Applying dewpoint depression modulation...")
            
            # Parse date string to datetime object
            try:
                run_datetime = datetime.strptime(run_date, '%Y-%m-%d')
            except ValueError:
                # Try ISO format if not basic format
                from datetime import datetime, timezone
                run_datetime = datetime.fromisoformat(run_date.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                
            modulated_signals = []
            dpd_modifications = 0
            for signal in signals:
                if len(signal) >= 5:
                    # Signal contains confidence value
                    station, market_type, signal_direction, reason, original_confidence = signal[0], signal[1], signal[2], signal[3], signal[4]
                    
                    # Apply dewpoint depression modulation
                    modulated_confidence = modulate_confidence(
                        station=station,
                        date=run_datetime,
                        metar_db_path=trader.metar_db,
                        confidence=original_confidence
                    )
                    
                    if abs(original_confidence - modulated_confidence) > 0.001:
                        dpd_modifications += 1
                    
                    modulated_signals.append((
                        station, market_type, signal_direction, reason, modulated_confidence
                    ))
                else:
                    # Backwards compatibility - append default confidence
                    modulated_signals.append((*signal, 0.5))
            
            signals = modulated_signals
            print(f"Applied dewpoint depression modulation to {len(signals_with_conf)} signals, modified confidence of {dpd_modifications} signals")
    else:
        print("Spatial coherence gate disabled")
        # Ensure signals have confidence values (default 0.5) for downstream compatibility
        signals = [(s[0], s[1], s[2], s[3], 0.5) if len(s) < 5 else s for s in signals]

    # Apply intraday METAR confirmation signal to adjust confidence based on same-day temperature trends
    # Only applies to signals where same-day METAR data is available (dates within last 24 hours)
    print("Applying intraday METAR confirmation...")
    intraday_confirmation_enabled = os.getenv('INTRADAY_CONFIRMATION_ENABLED', 'True').lower() == 'true'
    
    if intraday_confirmation_enabled:
        confirmed_signals = []
        intraday_confirmed = 0
        intraday_adjustments = 0
        
        for signal in signals:
            if len(signal) >= 5:
                # Extract signal components including original confidence
                station, market_type, signal_direction, reason, original_confidence = signal[0], signal[1], signal[2], signal[3], signal[4]
                
                # For intraday confirmation, predict direction: 'UP' if we expect higher temperature, 'DOWN' if lower
                predicted_direction = signal_direction.value  # MarketSide.UP.value='UP', MarketSide.DOWN.value='DOWN'
                
                # Get intraday confirmation
                confirmation_result = get_intraday_confirmation(
                    station=station,
                    date=run_date,
                    metar_db_path=trader.metar_db,
                    predicted_direction=predicted_direction,
                    base_confidence=original_confidence
                )
                
                if confirmation_result is not None:
                    # Apply intraday confirmation adjustment
                    intraday_confirmed += 1
                    new_confidence, confirmation_reason, details = confirmation_result
                    
                    if abs(new_confidence - original_confidence) > 0.001:  # Only count if changed meaningfully
                        intraday_adjustments += 1
                        
                    confirmed_signals.append((
                        station, market_type, signal_direction, reason, new_confidence
                    ))
                else:
                    # No intraday confirmation available for this signal, keep as is
                    confirmed_signals.append(signal)
            else:
                # Signal doesn't have confidence value, append as is
                confirmed_signals.append(signal)
        
        signals = confirmed_signals
        print(f"Applied intraday confirmation to {intraday_confirmed} signals, made meaningful confidence adjustments to {intraday_adjustments} signals")
    
    print(f"Generated {len(signals)} signals:")
    for i, sig in enumerate(signals):
        if len(sig) >= 5:  
            station, mtype, direction, reason, confidence = sig[0], sig[1], sig[2], sig[3], sig[4]
        else:
            station, mtype, direction, reason = sig
            confidence = 0.5  # Default
        print(f"  {i+1:2d}. {station} {mtype} {direction.value:>4s}: {reason} (conf: {confidence:.2f})")

    # Execute trades based on signals
    executed = 0
    skipped = 0
    trade_results = []

    for sig_item in signals:
        # Handle both 4-tuple and 5-tuple signals
        if len(sig_item) >= 5:
            station, market_type, signal_direction, reason, confidence = sig_item[0], sig_item[1], sig_item[2], sig_item[3], sig_item[4]
        else:
            station, market_type, signal_direction, reason = sig_item
            confidence = 0.5  # Default confidence
        
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

            # Dispatch to Discord (skip if alert was filtered by hard filters)
            dispatch_result = dispatch_current_alert(alert_data, discord_payload)
            if dispatch_result.get('error'):
                print(f"  [DISPATCH ERROR] {dispatch_result['error']}")

            # Print alert to console
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
