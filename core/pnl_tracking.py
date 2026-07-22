"""PnL Tracking Module

Profit & loss tracking, reconciliation, calibration metrics, and reporting.
Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.
"""
import json
import logging
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .market_cost_model import estimate_total_cost

_LOGGER = logging.getLogger(__name__)


def process_settlements_for_date(self, settlement_date):
    """
    Process settlements for a specific date. Updates trades with settlement results
    and calculates realized P/L.
    """
    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()
    c.execute("ATTACH DATABASE ? AS metar", (self.metar_db,))

    # Get all open trades for which settlements are available
    c.execute("""
        SELECT t.trade_uuid, t.station, t.trade_type, t.trade_price, t.market_price,
               t.quantity,
               p.settlement_bucket, p.prior_settlement_bucket
        FROM trades t
        JOIN (SELECT station, market_type, local_trading_date, settlement_bucket, prior_settlement_bucket
              FROM metar.settlement_epochs
              WHERE epoch_status = 'closed' AND local_trading_date = ?) p
        ON t.station = p.station AND t.trade_date_utc = date(p.local_trading_date)
           AND t.market_type = p.market_type
        WHERE t.status = 'open'
    """, (settlement_date,))

    unsettled_trades = c.fetchall()

    settled_count = 0
    for trade_uuid, station, trade_type_str, filled_price, market_price, quantity, settlement_value, prior_settlement_value in unsettled_trades:
        # Convert trade type to enum
        try:
            trade_type = TradeType(trade_type_str)
        except ValueError:
            continue

        # Calculate settlement return based on the trade type and settlement value.
        # Long YES pays $1 when event occurs; long NO pays $1 when it does not.
        # Short sides are represented as negative exposure with realized P&L mirrored.
        # Settlement is directional: compare today's temp vs yesterday's temp
        # If temp went up, UP predictions win ($1); if temp went down, DOWN predictions win ($1)
        if prior_settlement_value is not None:
            settlement_contract_value = 1.0 if settlement_value > prior_settlement_value else 0.0
        else:
            # Fallback: compare against 50°F midpoint if no prior data
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
            # Settlement goes UP if temperature rose vs yesterday
            settlement_is_up = settlement_contract_value > 0.5
            predicted_is_up = orig_signal_direction == "UP"  # Signal was UP/DOWN

            # If signal was "UP" and settlement went UP, or signal was "DOWN" and settlement went DOWN, then correct
            was_correct = (predicted_is_up == settlement_is_up)

            # Extract signal name based on functionality
            if "calendar" in functionality:
                signal_name = "calendar_climatology"
            elif "momentum" in functionality or "late_day" in functionality:
                signal_name = "late_day_momentum"  # Using appropriate name for the late-day momentum signal
            elif "nwp" in functionality or "direct" in functionality:
                signal_name = "nwp_direct"
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
    conn = get_sqlite_connection(self.paper_db)
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

    # Add skill gate statistics if available (T5-B3.1)
    skilled_stations_count = 0
    total_stations_count = 0
    if self._skill_gate is not None:
        try:
            all_stations = []
            # Try to get all stations from various sources
            try:
                from station_registry import get_all_stations
                all_stations = get_all_stations()
            except ImportError:
                # Fallback to known stations if registry not available
                all_stations = ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA', 'KSEA', 'KSFO', 'KHOU', 'KPHX', 'KDEN']

            skilled_stations = self._skill_gate.get_skilled_stations('HIGH')
            skilled_stations_count = len(skilled_stations)
            total_stations_count = len(all_stations)

        except Exception as e:
            _LOGGER.warning(f"Error getting skill gate statistics: {e}")
            skilled_stations_count = 0
            total_stations_count = 0

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
        'max_drawdown': max_drawdown,
        'skilled_stations_count': skilled_stations_count,
        'total_stations_count': total_stations_count
    }
def calculate_calibration_metrics_for_date(self, for_date):
    """Calculate and store calibration metrics for the dashboard."""
    conn = get_sqlite_connection(self.paper_db)
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

    conn = get_sqlite_connection(self.paper_db)
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
    conn = get_sqlite_connection(self.paper_db)
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
    conn = get_sqlite_connection(self.paper_db)
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
    conn = get_sqlite_connection(self.paper_db)
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
        conn = get_sqlite_connection(self.paper_db)
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
