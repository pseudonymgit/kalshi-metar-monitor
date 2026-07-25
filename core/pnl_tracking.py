"""PnL Tracking Module

Profit & loss tracking, reconciliation, calibration metrics, and reporting.
Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.
"""
import json
import logging
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .market_cost_model import MARKET_COST_MODEL

# Default calibration report path
CALIBRATION_REPORT_PATH = str(Path(__file__).resolve().parent.parent / "reports" / "paper_trading_calibration.json")
__all__ = ['process_settlements_for_date', 'daily_reconciliation', 'calculate_calibration_metrics_for_date', 'get_current_balance', 'get_version_performance', 'generate_calibration_report', 'compute_sharpe', 'update_risk_metrics_on_trade', '_get_strike_price']


_LOGGER = logging.getLogger(__name__)


def process_settlements_for_date(self, settlement_date):
    """
    Process settlements for a specific date. Updates trades with settlement results
    and calculates realized P/L.

    Correct P&L calculation:
      - HIGH contract: $1.00 if settlement_temp > strike_price, else $0.00
      - LOW contract:  $1.00 if settlement_temp < strike_price, else $0.00
    """
    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()
    c.execute("ATTACH DATABASE ? AS metar", (self.metar_db,))

    # Get all open trades for which settlements are available
    c.execute("""
        SELECT t.trade_uuid, t.station, t.trade_type, t.trade_price, t.market_price,
               t.quantity, t.market_type, t.signal_direction,
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
    for row in unsettled_trades:
        trade_uuid, station, trade_type_str, filled_price, market_price, quantity, market_type, signal_direction, settlement_value, prior_settlement_value = row
        # Convert trade type to enum
        try:
            trade_type = TradeType(trade_type_str)
        except ValueError:
            continue

        # Get the strike price from Kalshi market metadata
        strike_price = _get_strike_price(station, market_type, settlement_date, self.paper_db, self.metar_db)

        # Calculate settlement payout based on actual temperature vs strike price.
        # Kalshi binary options:
        #   HIGH contract: pays $1.00 if settlement_temp > strike_price
        #   LOW contract:  pays $1.00 if settlement_temp < strike_price
        settlement_temp = settlement_value  # settlement_bucket = actual daily max/min temp in °F
        if strike_price is not None:
            if market_type == "HIGH":
                settlement_contract_value = 1.0 if settlement_temp > strike_price else 0.0
            elif market_type == "LOW":
                settlement_contract_value = 1.0 if settlement_temp < strike_price else 0.0
            else:
                _LOGGER.warning(
                    "Cannot settle trade %s: unknown market_type=%s",
                    trade_uuid, market_type
                )
                continue
        else:
            # No strike price available — cannot determine strike-based settlement.
            # Skipping is safer than falling back to direction-based or 50°F,
            # which produce systematically wrong P&L (see Phase A.1 spec).
            _LOGGER.warning(
                "Cannot settle trade %s: no strike_price available for "
                "station=%s market_type=%s date=%s. "
                "Trade left open for manual resolution.",
                trade_uuid, station, market_type, settlement_date
            )
            continue
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

        # B-Mode R8 Cycle 4.2: Determine settlement outcome direction
        # settlement_contract_value = 1.0 means temp breached strike (ITM)
        # For HIGH: temp breached strike = temp > strike = UP outcome
        # For LOW: temp breached strike = temp < strike = DOWN outcome
        contract_is_itm = settlement_contract_value > 0.5
        if market_type == "HIGH":
            actual_settlement_direction = "UP" if contract_is_itm else "DOWN"
        else:
            # LOW market: temp < strike = ITM = DOWN outcome
            actual_settlement_direction = "DOWN" if contract_is_itm else "UP"

        # Compare signal_direction to actual settlement direction
        predicted_correctly = (signal_direction == actual_settlement_direction)
        settlement_outcome = "correct" if predicted_correctly else "incorrect"

        # Update the trade with settlement data
        realized_pnl = profit
        c.execute("""
            UPDATE trades
            SET settled_value = ?, settlement_date_utc = ?,
                realized_pnl = ?, settlement_return_amount = ?,
                status = 'closed',
                settlement_confirmed = 1,
                settlement_outcome = ?
            WHERE trade_uuid = ?
        """, (settlement_value, settlement_date, profit, return_amount,
              settlement_outcome, trade_uuid))

        # Update corresponding position (reduce by quantity)
        trade_qty = qty

        # Find position this trade affects and reduce quantity
        c.execute("""
            UPDATE positions
            SET realized_pnl = realized_pnl + ?, quantity = quantity - ?,
                total_pnl = realized_pnl + ?
            WHERE trade_uuids LIKE ?
        """, (profit, trade_qty, profit, f'%{trade_uuid}%'))

        # Check if trade was correct: does signal direction match strike-based outcome?
        # Query the original trade for the signal direction and functionality
        c.execute("SELECT signal_direction, functionality, confidence_indicator, forecast_prob FROM trades WHERE trade_uuid = ?", (trade_uuid,))
        trade_row = c.fetchone()

        if trade_row and HAS_CALIBRATION_PIPELINE and self._calibrator:
            orig_signal_direction, functionality, raw_confidence, raw_forecast_prob = trade_row

            # Determine if the prediction was correct (strike-based)
            # settlement_contract_value = 1.0 means temp breached strike (ITM)
            # Signal "UP" predicted strike breach; "DOWN" predicted no breach.
            contract_is_itm = settlement_contract_value > 0.5
            predicted_breach = orig_signal_direction == "UP"  # Signal was UP/DOWN

            # For strike-based markets: correct if predicted breach matches actual breach
            was_correct = (predicted_breach == contract_is_itm)

            # Extract signal name based on functionality
            if "calendar" in functionality:
                signal_name = "calendar_climatology"
            elif "momentum" in functionality or "late_day" in functionality:
                signal_name = "late_day_momentum"  # Using appropriate name for the late-day momentum signal
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
    - BOTH paper P&L (unconfirmed) AND settlement-confirmed P&L tracked separately

    B-Mode R8 Cycle 4.1:
      - paper_pnl: includes unrealized MTM + unsettled positions (unconfirmed)
      - settlement_confirmed_pnl: only trades that have actually settled against Kalshi data
      - All existing P&L columns remain but are tagged as "PAPER P&L — unconfirmed"
    """
    if reconcile_date is None:
        reconcile_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Process any settlements for the reconcile date
    self.process_settlements_for_date(reconcile_date)

    # Mark all open positions to current market price
    mtm_result = self.mark_positions_to_market(reconcile_date)

    # Get opening balance from previous day
    prev_balance = self.get_current_balance(reconcile_date)

    # B-Mode R8 Cycle 4.1: Migrate settlement columns if not present
    self._migrate_settlement_columns(reconcile_date)

    # Count trades and other details for this date
    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()

    # Today's trades
    c.execute("""
        SELECT trade_cost, realized_pnl, COALESCE(quantity, 0) FROM trades
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

    # B-Mode R8 Cycle 4.1: Separate settlement-confirmed P&L from paper P&L
    # settlement_confirmed_pnl = only realized P&L from actually settled trades
    settlement_confirmed_pnl = sum(pnl for pnl in settled_pnls)
    # paper_pnl = unrealized MTM P&L from open positions (unconfirmed against settlement)
    paper_pnl = total_unrealized_pnl

    # Today's new trade count
    c.execute("SELECT COUNT(*) FROM trades WHERE trade_date_utc = ?", (reconcile_date,))
    total_new_trades = c.fetchone()[0]

    # Calculate P&L components and fees
    # t[0]=trade_cost, t[1]=realized_pnl, t[2]=quantity
    # Fee = quantity * round_trip_fraction per contract (from MARKET_COST_MODEL)
    from core.market_cost_model import ROUND_TRIP_FEE
    total_fees = sum(abs(t[2]) * ROUND_TRIP_FEE for t in today_all_trades)
    today_realized_pnl = sum(t[1] or 0 for t in today_all_trades)

    # Daily P&L = realized P&L (from settled trades today) + change in unrealized P&L
    # The unrealized P&L is already updated via mark_positions_to_market
    # ⚠️ PAPER P&L — unconfirmed against settlement data
    total_daily_pnl = today_realized_pnl + total_unrealized_pnl

    # Closing balance = opening + realized P&L + unrealized P&L - fees
    # ⚠️ Uses PAPER P&L — unconfirmed against settlement data
    closing_balance = prev_balance + today_realized_pnl + total_unrealized_pnl - total_fees

    # Track drawdown from peak - simplified implementation
    c.execute("""
        SELECT MAX(closing_balance)
        FROM daily_balances
        WHERE date_utc <= date(?, '-1 day')
    """, (reconcile_date,))
    peak = c.fetchone()[0] or self.initial_balance
    max_drawdown = min(0.0, closing_balance - peak)

    # B-Mode R8 Cycle 2.10: Add cost-adjusted P&L column
    # Ensure the column exists (migration for existing databases)
    try:
        c.execute("ALTER TABLE daily_balances ADD COLUMN cost_adjusted_pnl REAL DEFAULT NULL")
    except Exception:
        pass  # Column already exists

    # Cost-adjusted P&L = raw P&L minus total trading costs (fees + execution slippage)
    # ⚠️ Uses PAPER P&L — unconfirmed against settlement data
    cost_adjusted_pnl = total_daily_pnl - total_fees

    # B-Mode R8 Cycle 4.1: Add settlement-confirmed P&L columns
    try:
        c.execute("ALTER TABLE daily_balances ADD COLUMN paper_pnl REAL DEFAULT NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE daily_balances ADD COLUMN settlement_confirmed_pnl REAL DEFAULT NULL")
    except Exception:
        pass

    # Record daily reconciliation (INSERT OR REPLACE to allow re-runs)
    c.execute("""
        INSERT OR REPLACE INTO daily_balances (
            date_utc, opening_balance, closing_balance, pnl,
            trade_count, position_count, winning_trades, losing_trades,
            settled_trades, position_size_weight, total_fees_paid,
            max_drawdown_since_high, cost_adjusted_pnl,
            paper_pnl, settlement_confirmed_pnl
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        reconcile_date, prev_balance, closing_balance, total_daily_pnl,
        len(today_all_trades), open_positions, winning_trade_count,
        losing_trade_count, len(settled_records),
        sum(abs(t[0]) for t in today_all_trades) / max(len(today_all_trades), 1),
        total_fees, max_drawdown, cost_adjusted_pnl,
        paper_pnl, settlement_confirmed_pnl
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
        'cost_adjusted_pnl': cost_adjusted_pnl,  # B-Mode R8 Cycle 2.10
        'max_drawdown': max_drawdown,
        'skilled_stations_count': skilled_stations_count,
        'total_stations_count': total_stations_count
    }
def _migrate_settlement_columns(self, for_date=None):
    """
    B-Mode R8 Cycle 4.1/4.2: Ensure settlement confirmation columns exist
    in the trades and daily_balances tables.

    SAFE TO RE-RUN: uses IF NOT EXISTS / try-except for all ALTER TABLE calls.
    """
    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()

    # trades table: settlement_confirmed flag + settlement_outcome
    try:
        c.execute(
            "ALTER TABLE trades ADD COLUMN settlement_confirmed INTEGER DEFAULT 0"
        )
    except Exception:
        pass
    try:
        c.execute(
            "ALTER TABLE trades ADD COLUMN settlement_outcome TEXT DEFAULT NULL"
        )
    except Exception:
        pass

    # daily_balances table: paper_pnl + settlement_confirmed_pnl
    try:
        c.execute(
            "ALTER TABLE daily_balances ADD COLUMN paper_pnl REAL DEFAULT NULL"
        )
    except Exception:
        pass
    try:
        c.execute(
            "ALTER TABLE daily_balances ADD COLUMN settlement_confirmed_pnl REAL DEFAULT NULL"
        )
    except Exception:
        pass

    conn.commit()
    conn.close()
    _LOGGER.info("settlement_columns_migration: trades/settlement_confirmed, "
                 "trades/settlement_outcome, daily_balances/paper_pnl, "
                 "daily_balances/settlement_confirmed_pnl")


def compute_settlement_accuracy(self, for_date=None) -> dict:
    """
    B-Mode R8 Cycle 4.2: Calculate settlement-validated accuracy metric.

    settlement_accuracy = correct_settlement_predictions / total_settled_trades

    Compares predicted direction (signal_direction) against actual Kalshi
    settlement bucket outcome for every settled trade.

    Returns dict with:
      - settlement_accuracy: float 0-1
      - total_settled: int
      - correct_predictions: int
      - incorrect_predictions: int
      - accuracy_by_type: dict of accuracy per market_type (HIGH/LOW)
    """
    import statistics

    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()

    c.execute("""
        SELECT settlement_confirmed, settlement_outcome, market_type
        FROM trades
        WHERE settlement_confirmed = 1
          AND settlement_outcome IS NOT NULL
    """)

    records = c.fetchall()
    conn.close()

    total = len(records)
    if total == 0:
        return {
            "settlement_accuracy": 0.0,
            "total_settled": 0,
            "correct_predictions": 0,
            "incorrect_predictions": 0,
            "accuracy_by_type": {},
            "note": "No settlement-confirmed trades yet"
        }

    correct = sum(1 for _, outcome, _ in records if outcome == "correct")
    incorrect = total - correct

    # Per market type
    from collections import defaultdict
    by_type = defaultdict(lambda: {"correct": 0, "total": 0})
    for _, outcome, mtype in records:
        by_type[mtype]["total"] += 1
        if outcome == "correct":
            by_type[mtype]["correct"] += 1

    accuracy_by_type = {}
    for mtype, stats in by_type.items():
        accuracy_by_type[mtype] = {
            "accuracy": stats["correct"] / stats["total"] if stats["total"] > 0 else 0.0,
            "correct": stats["correct"],
            "total": stats["total"]
        }

    accuracy = correct / total if total > 0 else 0.0

    # Also compute paper accuracy for comparison (from same trades' pre-settlement P&L)
    # This is stored separately to show the paper-vs-settlement delta
    _LOGGER.info(
        "settlement_accuracy: %.4f (%d/%d correct) across %d trades",
        accuracy, correct, total, total
    )

    return {
        "settlement_accuracy": accuracy,
        "total_settled": total,
        "correct_predictions": correct,
        "incorrect_predictions": incorrect,
        "accuracy_by_type": accuracy_by_type
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
        return 0.0  # Default to 0.0 for insufficient data (B-Mode R8 Cycle 2.2)

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


def _get_strike_price(station: str, market_type: str, date: str, paper_db: str, metar_db: str) -> Optional[int]:
    """
    Look up the Kalshi market strike price for a station+market_type+date.

    Resolution order:
      1. Live Kalshi API (via kalshi_price_fetcher.get_live_market_price)
      2. market_cache table in the alerts DB
      3. Fallback: None (caller uses directional fallback)

    Returns the strike price in °F (integer), or None if unavailable.
    """
    # 1. Try live Kalshi API
    try:
        from .kalshi_price_fetcher import get_live_market_price
        price, meta = get_live_market_price(station, market_type, date)
        strike = meta.get("strike")
        if strike is not None:
            return int(strike)
        # Also try floor_strike/cap_strike
        strike_type = meta.get("strike_type")
        if strike_type == "greater":
            floor = meta.get("floor_strike")
            if floor is not None:
                return int(float(floor))
        elif strike_type == "less":
            cap = meta.get("cap_strike")
            if cap is not None:
                return int(float(cap))
    except Exception as e:
        _LOGGER.debug("strike_price_live_failed station=%s market=%s error=%s", station, market_type, e)

    # 2. Try market_cache table in the alerts DB
    try:
        alert_db = os.getenv("ALERT_DB_PATH", "/var/data/alerts.db")
        if os.path.exists(alert_db):
            conn = sqlite3.connect(alert_db, timeout=1)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            try:
                row = conn.execute(
                    """SELECT cache_json FROM market_cache
                       WHERE station = ? ORDER BY updated_at DESC LIMIT 1""",
                    (station,)
                ).fetchone()
                if row:
                    cache = json.loads(row[0])
                    # Look for the matching market_type in the cache
                    if isinstance(cache, dict):
                        strike = cache.get("strike")
                        if strike is not None:
                            return int(strike)
                        strike_type = cache.get("strike_type")
                        if strike_type == "greater":
                            floor = cache.get("floor_strike")
                            if floor is not None:
                                return int(float(floor))
                        elif strike_type == "less":
                            cap = cache.get("cap_strike")
                            if cap is not None:
                                return int(float(cap))
            finally:
                conn.close()
    except Exception as e:
        _LOGGER.debug("strike_price_cache_failed station=%s error=%s", station, e)

    # 3. Not available
    return None
