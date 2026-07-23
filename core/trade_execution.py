"""Trade Execution Module

Trade placement, execution, position management, and mark-to-market.
Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition.
"""
import json
import uuid
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from enum import Enum
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
from .market_cost_model import MARKET_COST_MODEL
from .station_registry import get_cluster_for_station as _get_cluster_for_station
from .kalshi_price_fetcher import get_live_market_price as _get_live_market_price
from core.structured_logger import get_logger
__all__ = ['TradeType', 'MarketSide', 'place_paper_trade', 'mark_positions_to_market']


_LOGGER = get_logger(__name__)


class TradeType(Enum):
    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"
    BUY_NO = "buy_no"
    SELL_NO = "sell_no"


class MarketSide(Enum):
    UP = "UP"
    DOWN = "DOWN"
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

    # T5-B3.1: Skill-based station gate - only trade on skilled stations
    if self._skill_gate is not None:
        if not self._skill_gate.is_station_skilled(station, market_type):
            return {
                'status': 'skipped',
                'reason': 'station_not_skilled',
                'market_price': None,
                'analytical_prob': None,
                'confidence': None,
                'metadata': {'station_skill_rejected': True, 'skill_check_applied': True}
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
            elif "nwp" in functionality or "direct" in functionality:
                signal_name = "nwp_direct"
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

    # Record decision output - this is P1.4 requirement

    # ─── PHASE 3 RISK MODULES: PRE-TRADE CHECKS ───────────────────────
    # 1. Stop-loss pre-trade check - check if the stop-loss conditions are triggered
    if self._stop_loss_monitor is not None:
        try:
            stopped, stop_reason, stop_details = self._stop_loss_monitor.check_stop_conditions()
            if stopped:
                return {
                    'status': 'skipped',
                    'reason': f'Stop-loss triggered: {stop_reason}',
                    'market_price': market_price,
                    'analytical_prob': analytical_prob,
                    'confidence': calibrated_confidence,
                    'metadata': {**metadata, 'stop_loss_triggered': True, 'stop_details': stop_details}
                }
            else:
                _LOGGER.info(f"Stop-loss conditions check: {stop_reason}")
        except Exception as e:
            # Log error but allow trade to proceed to maintain safety
            _LOGGER.error(f"Stop-loss check failed: {e}")

    # 2. Risk budget pre-allocation check
    if self._risk_budget is not None:
        try:
            allowed, budget_reason = self._risk_budget.check_allocation(
                station=station,
                market_type=market_type,
                signal_type=functionality,
                position_size=0  # Initially, we want to check if we can allocate for this trade
            )
            if not allowed:
                return {
                    'status': 'skipped',
                    'reason': f'Risk budget rejected: {budget_reason}',
                    'market_price': market_price,
                    'analytical_prob': analytical_prob,
                    'confidence': calibrated_confidence,
                    'metadata': {**metadata, 'risk_budget_rejected': True}
                }
            else:
                _LOGGER.info(f"Risk budget allocation check passed: {budget_reason}")
        except Exception as e:
            # Log error but allow trade to proceed
            _LOGGER.error(f"Risk budget check failed: {e}")

    # 3. Low liquidity pre-trade check - avoid markets that may have low volume conditions
    if self._low_liquidity_trap_filter is not None:
        try:
            trapped, trap_reason, trap_details = self._low_liquidity_trap_filter.analyze_market_liquidity(
                station=station,
                market_type=market_type,
                date=date,
                price_metadata=self._last_market_price_meta if hasattr(self, '_last_market_price_meta') and self._last_market_price_meta is not None else {}
            )
            if trapped:
                return {
                    'status': 'skipped',
                    'reason': f'Low liquidity trap detected: {trap_reason}',
                    'market_price': market_price,
                    'analytical_prob': analytical_prob,
                    'confidence': calibrated_confidence,
                    'metadata': {**metadata, 'low_liquidity_trap_triggered': True, 'trap_details': trap_details}
                }
            else:
                _LOGGER.info(f"Low liquidity check passed for {station}:{market_type}")
        except Exception as e:
            # Log error but allow trade to proceed - it's better to trade than lose due to check failure
            _LOGGER.error(f"Low liquidity trap check failed, proceeding with trade: {e}")

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

    # ─── Cost-Aware Trade Filter (Phase 3.5) ───────────────────────
    # Check if edge exceeds 1.5x round-trip cost.
    # Kalshi: spread=0.5¢, commission=0¢, slippage=0.5¢ → total 1.0¢ per contract
    round_trip_cost = self._compute_round_trip_cost(market_price, position_size_estimate=100.0)
    if fair_price_advantage >= 0:
        edge_ratio = fair_price_advantage / round_trip_cost if round_trip_cost > 0 else float('inf')
    else:
        edge_ratio = abs(fair_price_advantage) / round_trip_cost if round_trip_cost > 0 else float('inf')

    min_edge_ratio = 1.5  # Edge must be > 1.5x cost to pass
    if edge_ratio < min_edge_ratio:
        return {
            'status': 'skipped',
            'reason': f'Edge {abs(fair_price_advantage):.4f} insufficient vs cost {round_trip_cost:.4f} (ratio={edge_ratio:.2f}x < {min_edge_ratio:.1f}x)',
            'market_price': market_price,
            'analytical_prob': analytical_prob,
            'confidence': calibrated_confidence,
            'metadata': metadata,
            'round_trip_cost': round_trip_cost,
            'edge_ratio': edge_ratio,
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

    # ── RISK CONTROLS: Pre-trade check ────────────────────────
    # Check kill switches before sizing
    should_halt, kill_reasons = self.check_kill_switches()
    if should_halt:
        return {
            'status': 'skipped',
            'reason': f'Risk controls triggered: {"; ".join(kill_reasons)}',
            'market_price': market_price,
            'analytical_prob': analytical_prob,
            'confidence': calibrated_confidence,
            'metadata': {**metadata, 'risk_controls_halted': True, 'kill_reasons': kill_reasons},
        }

    # ── CONSOLIDATED POSITION SIZING PIPELINE ────────────────
    # Single pipeline: Kelly (primary) → fallback (if Kelly unavailable) → ladder (modifier)
    # Replaces 3 conflicting systems: Kelly sizer, confidence sizer, scaling ladder
    #
    # Rules:
    # 1. Kelly sizer is primary (corrected formula: f* = (p - c) / (1 - c))
    # 2. No min_size_usd floor - Kelly naturally produces 0 for no-edge trades
    # 3. Single cap: 8% of bankroll (from Kelly sizer)
    # 4. Scaling ladder is a downward modifier only (can reduce, never increase)
    # 5. Edge must be positive to size > 0

    current_balance = self.get_current_balance(date)
    sizing_meta = {}

    # Compute edge and win rate
    edge = abs(analytical_prob - market_price)
    win_rate = analytical_prob

    # ── Step 1: Primary sizing via Fee-Aware Kelly ──
    if self._kelly_sizer is not None and HAS_KELLY_SIZER:
        try:
            position_size, kelly_details = self._kelly_sizer.compute_position_size(
                win_rate=win_rate,
                confidence=confidence,
                total_capital=current_balance
            )
            sizing_meta['kelly_computed'] = True
            sizing_meta['kelly_details'] = kelly_details
            _LOGGER.info(f"Fee-aware Kelly position computed: ${position_size:.2f}")
        except Exception as e:
            _LOGGER.error(f"Fee-aware Kelly sizer failed, falling back: {e}")
            position_size = 0.0
            sizing_meta['kelly_fallback'] = True
    else:
        # Without Kelly sizer, use simple edge-based sizing (no min_size_usd floor)
        position_size = current_balance * edge * confidence * 0.08  # 8% bankroll cap
        sizing_meta['kelly_unavailable'] = True
        _LOGGER.warning("Kelly sizer unavailable, using fallback sizing")

    # ── Step 2: Apply scaling ladder as downward modifier ──
    if self._scaling_ladder is not None and position_size > 0:
        try:
            ladder_limit = self._scaling_ladder.get_position_limit()
            if position_size > ladder_limit:
                sizing_meta['scaling_ladder_applied'] = True
                sizing_meta['scaling_original_size'] = position_size
                sizing_meta['scaling_tier'] = self._scaling_ladder.get_current_tier()
                position_size = ladder_limit
                sizing_meta['scaling_adjusted_size'] = position_size
                _LOGGER.info(f"Position scaled down by ladder from ${sizing_meta['scaling_original_size']:.2f} to ${position_size:.2f}")
            else:
                sizing_meta['scaling_ladder_applied'] = False
        except Exception as e:
            _LOGGER.error(f"Scaling ladder adjustment failed: {e}")

    # ── Step 3: Record position in risk budget ──
    if self._risk_budget is not None and position_size > 0:
        try:
            self._risk_budget.record_position(
                station=station,
                market_type=market_type,
                signal_type=functionality,
                position_size=position_size
            )
            sizing_meta['risk_budget_recorded'] = True
            _LOGGER.info(f"Position recorded in risk budget: ${position_size:.2f}")
        except Exception as e:
            _LOGGER.error(f"Failed to record position in risk budget: {e}")

    # If position size is 0 or below, skip
    if position_size <= 0:
        return {
            'status': 'skipped',
            'reason': 'Position size is 0 (no edge or confidence too low)',
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
    conn = get_sqlite_connection(self.paper_db)
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

    # ─── PHASE 3 RISK MODULES: POST-TRADE RECORDING ───────────────────────
    # Record trade result with Stop Loss Monitor
    if self._stop_loss_monitor is not None:
        try:
            self._stop_loss_monitor.record_trade(
                pnl=net_cost,
                is_profitable=(net_cost > 0),
                date_str=date
            )
            _LOGGER.info(f"Trade result recorded with Stop Loss Monitor: PnL=${net_cost:.2f}, Profitable={net_cost>0}")
        except Exception as e:
            _LOGGER.error(f"Failed to record trade in Stop Loss Monitor: {e}")

    # Update scaling ladder for trade results
    if self._scaling_ladder is not None:
        try:
            self._scaling_ladder.record_trade_result(
                pnl=net_cost,
                is_profitable=(net_cost > 0)
            )
            tier, limit, reason = self._scaling_ladder.evaluate()
            _LOGGER.info(f"Scaling ladder updated: Tier={tier}, Limit=${limit}, Reason='{reason}'")
        except Exception as e:
            _LOGGER.error(f"Failed to update scaling ladder: {e}")

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
    conn = get_sqlite_connection(self.paper_db)
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
def _compute_round_trip_cost(self, market_price: float, position_size_estimate: float = 100.0) -> float:
    """
    Compute the estimated round-trip cost for a trade on Kalshi.

    Cost components (Kalshi):
      - Spread:   0.5¢ per contract ($0.005)
      - Commission: 0¢ (Kalshi has no commission on weather markets)
      - Slippage: 0.5¢ per contract ($0.005)
      - Total per contract: 1.0¢ ($0.01)

    Round-trip cost = (spread/2 + commission * 2 + slippage) * contract_count
    For a buy-and-hold-to-settlement, the cost is spread/2 + slippage
    (no commission, no second leg unless we close early).

    Args:
        market_price: Current market price (0.0-1.0)
        position_size_estimate: Estimated position size in USD for cost estimate

    Returns:
        Estimated round-trip cost as a fraction of notional (0.0-1.0 range)
    """
    # Kalshi cost structure per contract (from centralized model)
    from core.market_cost_model import MARKET_COST_MODEL
    spread_per_contract = MARKET_COST_MODEL.spread       # 3.1¢ measured mean
    commission_per_contract = MARKET_COST_MODEL.commission  # 0% commission
    slippage_per_contract = MARKET_COST_MODEL.slippage     # 0.5¢

    # Round-trip: buy + hold to settlement (one leg + slippage)
    cost_per_contract = MARKET_COST_MODEL.round_trip_fraction()

    # Estimate contract count for this position
    if market_price > 0.001:
        contract_count = position_size_estimate / market_price
    else:
        contract_count = position_size_estimate / 0.5  # fallback

    total_cost = cost_per_contract * contract_count

    # Return cost as fraction of position size (for edge comparison)
    if position_size_estimate > 0:
        return total_cost / position_size_estimate
    return cost_per_contract
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
    conn = get_sqlite_connection(self.paper_db)
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
    conn = get_sqlite_connection(self.paper_db)
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

    conn = get_sqlite_connection(self.paper_db)
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
