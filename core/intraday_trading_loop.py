#!/usr/bin/env python3
"""
Intraday Trading Loop — Phase 18

Separate intraday trading mode that runs hourly via cron. Evaluates daily
contracts at multiple points during the day, refines predictions with
more recent METAR data, and manages entry/exit using sequential triggers.

Architecture (D1):
- Stage A: METAR-based signals (FOGR, dT/dt, Pressure Tendency)
- Stage B: HRRR bias-corrected (if HRRR data available)
- Stage C: NWP+METAR fusion (confirmation/contradiction)
- Stage D: Entry/Exit rules

Sequential Trigger Architecture (D1):
- Not majority-vote ensemble — signals fire in sequence (A→B→C→D)
- Each stage must confirm before advancing
- Stage 1: Enter at signal fire when ensemble spread < 2.5°C IQR
- Stage 2: Confirmation from first 2 METAR obs showing trajectory alignment
- Window: 6h to fill Stage 2 or cancel

Dual Exit (D2):
- Time decay: Reduce position at T-6h, T-3h, close at T-1h
- Confidence decay: Close 50% if spread widens >40% from entry, 100% if >75%
- Hard stop: -40% of initial margin
- Profit take: 50% at +8¢ with >4h remaining

Spread-Based Entry Avoidance (D3):
- If current Kalshi spread is widening → avoid or increase edge requirement
- Edge requirement = 2x spread

Usage:
    python3 -m core.intraday_trading_loop --mode check
    python3 -m core.intraday_trading_loop --mode trade
"""

import argparse
import logging
import sqlite3
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict, Any

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.station_registry import get_all_stations
from core.market_cost_model import MARKET_COST_MODEL
from core.risk_controls import check_kill_switches, RiskState, DEFAULT_RISK_CONFIG
from core.fee_aware_kelly_position_sizing import KellyPositionSizer, KellySizingConfig

logger = logging.getLogger(__name__)

# DB paths
METAR_DB = PROJECT_ROOT / "data" / "metar_backfill.db"
NWP_DB = PROJECT_ROOT / "data" / "nwp_forecasts.db"

# Intraday signal imports
from core.signals.fogr_reversion_signal import FogrReversionSignal
from core.signals.hrrr_bias_corrected_signal import HRRRBiasCorrectedSignal
from core.signals.metar_nowcast_signal import MetarNowcastSignal
from core.signals.spread_based_entry_signal import SpreadBasedEntryDetector
from core.signals.nwp_dtdt_fusion_signal import NwpDtdtFusionSignal

# ── Configuration ──────────────────────────────────────────────

# Entry thresholds (Stage 1)
MAX_ENSEMBLE_SPREAD_IQR_C = 2.5  # 2.5°C IQR max for entry
STAGE1_WINDOW_HOURS = 6          # 6h to fill Stage 2 or cancel

# Exit thresholds (D2)
PROFIT_TAKE_PCT = 0.50           # Take 50% profit at threshold
PROFIT_TAKE_CENTS = 8            # 8¢ profit
PROFIT_TAKE_MIN_HOURS = 4        # Must have >4h remaining
HARD_STOP_LOSS_PCT = 0.40        # -40% of initial margin
TIME_DECAY_T6_HOURS = 6          # T-6h: reduce position
TIME_DECAY_T3_HOURS = 3          # T-3h: reduce further
TIME_DECAY_CLOSE_HOURS = 1       # T-1h: close regardless

# Spread avoidance (D3)
SPREAD_WIDENING_THRESHOLD = 0.40  # 40% spread widening triggers confidence decay
SPREAD_WIDENING_FULL_CLOSE = 0.75 # 75% spread widening = full close
EDGE_SPREAD_MULTIPLIER = 2.0      # Edge requirement = 2x spread

# Stage sequence
STAGE_A_SIGNALS = ['fogr_reversion', 'metar_nowcast']
STAGE_B_SIGNALS = ['hrrr_bias_corrected']
STAGE_C_SIGNALS = ['nwp_dtdt_fusion']


class IntradayState:
    """
    Tracks state for an intraday trading position.
    """
    def __init__(self, station: str, date: str, market_type: str):
        self.station = station
        self.date = date
        self.market_type = market_type
        self.stage = 0                # 0=idle, 1=entered, 2=confirmed
        self.entry_time = None
        self.entry_spread = None
        self.entry_price = None
        self.position_size = 0
        self.current_confidence = 0.0
        self.current_direction = None
        self.stage_a_fired = False
        self.stage_b_fired = False
        self.stage_c_fired = False
        self.metar_confirmations = 0  # Stage 2: METAR obs showing trajectory alignment

    def to_dict(self) -> dict:
        return {
            'station': self.station,
            'date': self.date,
            'market_type': self.market_type,
            'stage': self.stage,
            'entry_time': self.entry_time,
            'entry_spread': self.entry_spread,
            'entry_price': self.entry_price,
            'position_size': self.position_size,
            'current_confidence': self.current_confidence,
            'current_direction': self.current_direction,
            'stage_a_fired': self.stage_a_fired,
            'stage_b_fired': self.stage_b_fired,
            'stage_c_fired': self.stage_c_fired,
            'metar_confirmations': self.metar_confirmations,
        }


class IntradayTradingLoop:
    """
    Intraday trading loop that evaluates daily contracts at multiple points
    during the day, refining predictions with more recent METAR data.

    Sequential trigger architecture (A→B→C→D):
    - Stage A: METAR-based signals (FOGR, dT/dt, Pressure Tendency)
    - Stage B: HRRR bias-corrected signal
    - Stage C: NWP+METAR fusion signal
    - Stage D: Entry/Exit rules
    """

    def __init__(self, metar_db_path: str = None, nwp_db_path: str = None):
        self.metar_db_path = str(metar_db_path or METAR_DB)
        self.nwp_db_path = str(nwp_db_path or NWP_DB)

        # Initialize signals
        self.fogr = FogrReversionSignal(db_path=self.metar_db_path)
        self.hrrr = HRRRBiasCorrectedSignal(db_path=self.metar_db_path)
        self.metar_nowcast = MetarNowcastSignal()
        self.spread_entry = SpreadBasedEntryDetector()
        self.fusion = NwpDtdtFusionSignal(db_path=self.metar_db_path, nwp_db_path=self.nwp_db_path)

        # Risk controls
        self.risk_state = RiskState(config=DEFAULT_RISK_CONFIG)
        self.kelly_sizer = KellyPositionSizer()

        # Active positions
        self.positions: Dict[str, IntradayState] = {}

        # Market cost model
        self.cost_model = MARKET_COST_MODEL

    def get_key(self, station: str, date: str, market_type: str) -> str:
        return f"{station}|{date}|{market_type}"

    # ── Stage A: METAR-Based Signals ─────────────────────────

    def evaluate_stage_a(self, station: str, date: str, market_type: str) -> Tuple[Optional[str], float]:
        """
        Evaluate Stage A signals (METAR-based).

        Returns:
            (direction, max_confidence) — the strongest signal from Stage A
        """
        signals = []

        # A1: FOGR Reversion
        if market_type == 'HIGH':
            d, c = self.fogr.evaluate_for_station(station, date, market_type=market_type)
            if d is not None:
                signals.append(('fogr', d, c))

        # A2: METAR Nowcast (ADVANCE signal)
        try:
            result = self.metar_nowcast.evaluate(
                station=station,
                bucket_temp_f=75,  # Default — use actual bucket in production
                gefs_max_f=85.0,
                gefs_min_f=65.0,
            )
            if result.get('signal'):
                d = 'up' if result['direction'] == 'HIGH' else 'down'
                c = result['confidence']
                signals.append(('metar_nowcast', d, c))
        except Exception as e:
            logger.debug(f"METAR nowcast Stage A failed for {station}: {e}")

        if not signals:
            return None, 0.0

        # Stage A fires if at least 2 signals agree on direction
        up_signals = [s for s in signals if s[1] == 'up']
        down_signals = [s for s in signals if s[1] == 'down']

        if len(up_signals) >= 2:
            # Majority UP
            max_conf = max(s[2] for s in up_signals)
            return 'up', max_conf
        elif len(down_signals) >= 2:
            # Majority DOWN
            max_conf = max(s[2] for s in down_signals)
            return 'down', max_conf
        elif len(signals) == 1:
            # Single signal — use it but with reduced confidence
            return signals[0][1], signals[0][2] * 0.7

        # Mixed signals — no clear Stage A trigger
        return None, 0.0

    # ── Stage B: HRRR Bias-Corrected ──────────────────────────

    def evaluate_stage_b(self, station: str, date: str, market_type: str) -> Tuple[Optional[str], float]:
        """
        Evaluate Stage B signal (HRRR bias-corrected).

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        return self.hrrr.evaluate_for_station(station, date, market_type=market_type)

    # ── Stage C: NWP + METAR Fusion ──────────────────────────

    def evaluate_stage_c(self, station: str, date: str, market_type: str) -> Tuple[Optional[str], float]:
        """
        Evaluate Stage C signal (NWP + METAR fusion).

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        return self.fusion.evaluate_for_station(station, date, market_type=market_type)

    # ── Stage D1: Entry/Sequential Trigger ────────────────────

    def check_ensemble_spread(self, station: str, date: str) -> Optional[float]:
        """
        Check ensemble spread (IQR) from NWP data.

        Returns:
            IQR in °C, or None if insufficient data
        """
        if not os.path.exists(self.nwp_db_path):
            return None

        try:
            conn = sqlite3.connect(self.nwp_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            cur = conn.cursor()

            # Get ensemble member values for the target date
            field = 'temperature_2m_max'
            cur.execute("""
                SELECT value
                FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND model = 'ensemble'
                  AND variable = ? AND member_index IS NOT NULL
            """, (station, date, field))
            values = [r[0] for r in cur.fetchall() if r[0] is not None]

            conn.close()

            if len(values) < 5:
                return None

            sorted_vals = sorted(values)
            q1 = sorted_vals[len(sorted_vals) // 4]
            q3 = sorted_vals[3 * len(sorted_vals) // 4]
            return q3 - q1

        except Exception:
            return None

    def check_spread_condition(self, station: str, date: str) -> Tuple[bool, float]:
        """
        Check if ensemble spread is acceptable for entry (D1).

        Returns:
            (pass, spread_iqr_c)
        """
        spread = self.check_ensemble_spread(station, date)
        if spread is None:
            # No ensemble data — allow entry with warning
            return True, 0.0
        return spread < MAX_ENSEMBLE_SPREAD_IQR_C, spread

    # ── Stage D2: Dual Exit ──────────────────────────────────

    def check_time_decay(self, position: IntradayState, current_time: datetime,
                         settlement_time: datetime) -> Tuple[float, str]:
        """
        Check time-based position reduction.

        Args:
            position: Current position state
            current_time: Current UTC time
            settlement_time: Settlement time (typically 21:00 UTC = 5pm ET)

        Returns:
            (reduction_factor, reason)
        """
        hours_remaining = (settlement_time - current_time).total_seconds() / 3600.0

        if hours_remaining <= TIME_DECAY_CLOSE_HOURS:
            return 1.0, "time_decay_close"
        elif hours_remaining <= TIME_DECAY_T3_HOURS:
            return 0.75, "time_decay_t3"
        elif hours_remaining <= TIME_DECAY_T6_HOURS:
            return 0.50, "time_decay_t6"
        return 0.0, "no_time_decay"

    def check_confidence_decay(self, position: IntradayState,
                                current_spread: float) -> Tuple[float, str]:
        """
        Check confidence-based position reduction based on spread widening.

        Args:
            position: Current position state
            current_spread: Current ensemble spread

        Returns:
            (reduction_factor, reason)
        """
        if position.entry_spread is None or current_spread is None or position.entry_spread <= 0:
            return 0.0, "no_spread_data"

        spread_change = (current_spread - position.entry_spread) / position.entry_spread

        if spread_change >= SPREAD_WIDENING_FULL_CLOSE:
            return 1.0, "confidence_decay_full"
        elif spread_change >= SPREAD_WIDENING_THRESHOLD:
            return 0.5, "confidence_decay_half"
        return 0.0, "no_confidence_decay"

    def check_profit_take(self, position: IntradayState, current_price: float,
                          current_time: datetime, settlement_time: datetime) -> Tuple[float, str]:
        """
        Check profit-taking conditions.

        Args:
            position: Current position state
            current_price: Current market price
            current_time: Current UTC time
            settlement_time: Settlement time

        Returns:
            (reduction_factor, reason)
        """
        if position.entry_price is None or current_price is None:
            return 0.0, "no_price_data"

        hours_remaining = (settlement_time - current_time).total_seconds() / 3600.0

        # Profit calculation: for UP contracts, profit = current_price - entry_price
        if position.current_direction == 'up':
            profit_cents = (current_price - position.entry_price) * 100
        else:
            profit_cents = (position.entry_price - current_price) * 100

        if profit_cents >= PROFIT_TAKE_CENTS and hours_remaining > PROFIT_TAKE_MIN_HOURS:
            return PROFIT_TAKE_PCT, "profit_take"

        return 0.0, "no_profit_take"

    def check_hard_stop(self, position: IntradayState, current_price: float) -> Tuple[bool, str]:
        """
        Check hard stop-loss condition.

        Args:
            position: Current position state
            current_price: Current market price

        Returns:
            (triggered, reason)
        """
        if position.entry_price is None or current_price is None:
            return False, "no_price_data"

        # Loss calculation
        if position.current_direction == 'up':
            loss_pct = (position.entry_price - current_price) / position.entry_price
        else:
            loss_pct = (current_price - position.entry_price) / position.entry_price

        if loss_pct >= HARD_STOP_LOSS_PCT:
            return True, f"hard_stop_loss_{loss_pct:.1%}"

        return False, "no_stop"

    # ── Stage D3: Spread-Based Entry Avoidance ────────────────

    def check_spread_entry_avoidance(self, current_spread: float,
                                      historical_spread: float = None,
                                      station: str = None,
                                      bucket_temp_f: int = 75) -> Tuple[bool, float]:
        """
        Check if current market spread is widening and adjust edge requirement.

        Uses the ADVANCE SpreadBasedEntryDetector for statistical spread analysis
        (percentile-based) when station is provided, falling back to simple
        threshold comparison.

        Args:
            current_spread: Current Kalshi market spread
            historical_spread: Historical average spread for this market
            station: Station code (required for ADVANCE signal analysis)
            bucket_temp_f: Temperature bucket

        Returns:
            (avoid_entry, edge_requirement)
        """
        edge_requirement = current_spread * EDGE_SPREAD_MULTIPLIER

        # Use ADVANCE Spread-Based Entry Detector for percentile analysis
        if station is not None and hasattr(self, 'spread_entry'):
            try:
                pctile = self.spread_entry.get_spread_percentile(
                    station, bucket_temp_f, current_spread
                )
                # If spread is in the 80th+ percentile (very wide), avoid
                if pctile > 0.80:
                    edge_requirement = max(edge_requirement, current_spread * 3.0)
                    return True, edge_requirement
                # If spread is below 50th percentile (compressed), reduce edge
                if pctile < 0.50:
                    edge_requirement = max(edge_requirement, current_spread * 1.5)
                    return False, edge_requirement
            except Exception:
                pass

        # Fallback: simple historical comparison
        if historical_spread is not None and current_spread > historical_spread:
            edge_requirement = max(edge_requirement, current_spread * 3.0)
            return True, edge_requirement

        return False, edge_requirement

    # ── Main Evaluation Loop ─────────────────────────────────

    def evaluate_intraday(self, station: str, date: str, market_type: str,
                          current_time: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Full intraday evaluation pipeline.

        Args:
            station: Station code
            date: ISO date string
            market_type: 'HIGH' or 'LOW'
            current_time: Current UTC time (defaults to now)

        Returns:
            Dict with evaluation results
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)

        key = self.get_key(station, date, market_type)
        position = self.positions.get(key, IntradayState(station, date, market_type))

        result = {
            'station': station,
            'date': date,
            'market_type': market_type,
            'time': current_time.isoformat(),
            'stage_a': None,
            'stage_b': None,
            'stage_c': None,
            'entry_decision': None,
            'exit_decision': None,
            'position': position.to_dict(),
        }

        # Stage A: METAR-based signals
        stage_a_dir, stage_a_conf = self.evaluate_stage_a(station, date, market_type)
        result['stage_a'] = {'direction': stage_a_dir, 'confidence': stage_a_conf}

        if stage_a_dir is None:
            result['entry_decision'] = {'action': 'hold', 'reason': 'stage_a_no_signal'}
            self.positions[key] = position
            return result

        position.stage_a_fired = True

        # Stage B: HRRR (if available)
        stage_b_dir, stage_b_conf = self.evaluate_stage_b(station, date, market_type)
        result['stage_b'] = {'direction': stage_b_dir, 'confidence': stage_b_conf}

        if stage_b_dir is not None:
            position.stage_b_fired = True

        # Stage C: NWP + METAR Fusion
        stage_c_dir, stage_c_conf = self.evaluate_stage_c(station, date, market_type)
        result['stage_c'] = {'direction': stage_c_dir, 'confidence': stage_c_conf}

        if stage_c_dir is not None:
            position.stage_c_fired = True

        # Determine overall direction from the sequential pipeline
        # Stage A is the primary trigger; Stage C may override in contradiction
        overall_direction = stage_a_dir
        overall_confidence = stage_a_conf

        if stage_c_dir is not None:
            if stage_c_dir == stage_a_dir:
                # Confirmation — boost confidence
                overall_confidence = min(0.95, stage_a_conf + stage_c_conf * 0.2)
            else:
                # Contradiction — reduce confidence
                overall_confidence = stage_a_conf * 0.5
                if stage_c_conf > 0.7 and stage_a_conf < 0.4:
                    # Strong contradiction — flip direction
                    overall_direction = stage_c_dir
                    overall_confidence = stage_c_conf * 0.7

        position.current_direction = overall_direction
        position.current_confidence = overall_confidence

        # Stage D1: Entry conditions
        is_entry_eligible = False
        if position.stage == 0:
            spread_pass, iqr = self.check_spread_condition(station, date)

            # Stage D3: Spread-Based Entry Avoidance check
            avoid_entry, edge_req = self.check_spread_entry_avoidance(
                current_spread=iqr if iqr else 0.0,
                station=station,
                bucket_temp_f=75,
            )

            min_confidence = 0.3
            is_entry_eligible = (
                overall_direction is not None
                and overall_confidence >= min_confidence
                and spread_pass
                and not avoid_entry
            )

            if is_entry_eligible:
                position.stage = 1
                position.entry_time = current_time.isoformat()
                position.entry_spread = iqr
                position.entry_price = 0.5  # Will be updated with real price
                result['entry_decision'] = {
                    'action': 'enter',
                    'direction': overall_direction,
                    'confidence': overall_confidence,
                    'spread_iqr_c': iqr,
                    'edge_requirement': edge_req,
                    'reason': 'sequential_triggers_met' if not avoid_entry else 'spread_too_wide',
                }
            else:
                result['entry_decision'] = {
                    'action': 'hold',
                    'reason': 'entry_conditions_not_met',
                    'spread_check': spread_pass,
                    'spread_avoid_entry': avoid_entry,
                    'confidence': overall_confidence,
                    'min_confidence': min_confidence,
                }

        # Stage D2: Exit checks (if position is open)
        if position.stage >= 1:
            # Settlement time: 21:00 UTC = 5pm ET
            settlement_time = datetime.strptime(date, '%Y-%m-%d').replace(
                hour=21, tzinfo=timezone.utc
            )

            # Time decay
            reduction, reason = self.check_time_decay(position, current_time, settlement_time)
            if reduction > 0:
                result['exit_decision'] = {
                    'action': 'close' if reduction >= 1.0 else 'reduce',
                    'reduction': reduction,
                    'reason': reason,
                }
                if reduction >= 1.0:
                    position.stage = 0
                    del self.positions[key]
                else:
                    self.positions[key] = position
                return result

        # Stage 2: Confirmation from METAR observations
        if position.stage == 1:
            # Check if we have 2 METAR observations showing trajectory alignment
            try:
                conn = sqlite3.connect(self.metar_db_path)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                cur = conn.cursor()
                cur.execute("""
                    SELECT temp_f, timestamp_utc
                    FROM metar_observations
                    WHERE station = ? AND date_utc = ? AND temp_f IS NOT NULL
                    ORDER BY timestamp_utc ASC
                    LIMIT 5
                """, (station, date))
                obs = cur.fetchall()
                conn.close()

                # Check if trajectory is aligned with direction
                if len(obs) >= 2:
                    temps = [o[0] for o in obs if o[0] is not None]
                    if len(temps) >= 2:
                        rising = all(temps[i] < temps[i+1] for i in range(len(temps)-1))
                        falling = all(temps[i] > temps[i+1] for i in range(len(temps)-1))

                        if (overall_direction == 'up' and rising) or \
                           (overall_direction == 'down' and falling):
                            position.metar_confirmations += 1
                            if position.metar_confirmations >= 2:
                                position.stage = 2
                                result['entry_decision'] = {
                                    'action': 'confirm',
                                    'direction': overall_direction,
                                    'confidence': overall_confidence,
                                    'reason': 'stage2_metar_confirmation',
                                }
            except Exception:
                pass

        self.positions[key] = position
        return result

    # ── Batch Evaluation ──────────────────────────────────────

    def evaluate_all_stations(self, date: str, market_type: str = 'HIGH',
                               current_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        Evaluate intraday signals for all stations.

        Args:
            date: ISO date string
            market_type: 'HIGH' or 'LOW'
            current_time: Current UTC time

        Returns:
            List of evaluation results
        """
        stations = get_all_stations()
        results = []

        for station in stations:
            try:
                result = self.evaluate_intraday(station, date, market_type, current_time)
                results.append(result)
            except Exception as e:
                logger.error(f"Error evaluating {station}: {e}")
                results.append({
                    'station': station,
                    'date': date,
                    'error': str(e),
                })

        return results

    # ── Risk Control Check ────────────────────────────────────

    def pre_trade_safety_check(self) -> Tuple[bool, str]:
        """
        Check kill switches before any trading.

        Returns:
            (pass, reason)
        """
        try:
            kill_switches = check_kill_switches(self.risk_state)
            if not kill_switches[0]:
                return False, f"Kill switch active: {kill_switches[1]}"
            return True, "all_clear"
        except Exception as e:
            return False, f"risk_check_error: {e}"

    # ── CLI ───────────────────────────────────────────────────

    def run_once(self, mode: str = 'check', date: str = None,
                 market_type: str = 'HIGH'):
        """
        Run the intraday trading loop once.

        Args:
            mode: 'check' (evaluate only) or 'trade' (execute trades)
            date: Target date (defaults to today)
            market_type: 'HIGH' or 'LOW'
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        current_time = datetime.now(timezone.utc)
        print(f"Intraday Trading Loop — {current_time.isoformat()}")
        print(f"Mode: {mode}, Date: {date}, Market: {market_type}")
        print()

        # Pre-trade safety check
        safe, reason = self.pre_trade_safety_check()
        if not safe:
            print(f"⛔ Safety check FAILED: {reason}")
            return

        print(f"✅ Safety check passed: {reason}")
        print()

        # Evaluate all stations
        results = self.evaluate_all_stations(date, market_type, current_time)

        # Summarize
        entries = [r for r in results if r.get('entry_decision', {}).get('action') == 'enter']
        confirms = [r for r in results if r.get('entry_decision', {}).get('action') == 'confirm']
        exits = [r for r in results if r.get('exit_decision') is not None]
        holds = [r for r in results if r.get('entry_decision', {}).get('action') == 'hold']

        print(f"Results: {len(results)} stations")
        print(f"  Entries: {len(entries)}")
        print(f"  Confirms: {len(confirms)}")
        print(f"  Exits: {len(exits)}")
        print(f"  Holds: {len(holds)}")
        print()

        if entries:
            print("=== Entry Signals ===")
            for r in entries[:10]:
                ed = r['entry_decision']
                print(f"  {r['station']} {r['market_type']}: "
                      f"{ed.get('direction')} (conf={ed.get('confidence', 0):.2f}) — {ed.get('reason')}")

        if mode == 'trade' and entries:
            print("\n⚠️ Trade mode selected — would execute trades here")
            print(f"  Using Kelly sizer for position sizing")
            print(f"  Market cost: {self.cost_model.round_trip_fraction():.3f} per contract")

        # Print active positions
        if self.positions:
            print(f"\nActive positions: {len(self.positions)}")
            for key, pos in self.positions.items():
                station, p_date, m_type = key.split('|')
                print(f"  {station} {m_type} {p_date}: stage={pos.stage} "
                      f"dir={pos.current_direction} conf={pos.current_confidence:.2f}")

        print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")


def main():
    parser = argparse.ArgumentParser(description="Intraday Trading Loop")
    parser.add_argument('--mode', choices=['check', 'trade'], default='check',
                        help='Check signals or execute trades')
    parser.add_argument('--date', type=str, default=None,
                        help='Target date (YYYY-MM-DD, default: today)')
    parser.add_argument('--market', choices=['HIGH', 'LOW'], default='HIGH',
                        help='Market type')
    parser.add_argument('--metar-db', type=str, default=None,
                        help='METAR database path')
    parser.add_argument('--nwp-db', type=str, default=None,
                        help='NWP database path')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    loop = IntradayTradingLoop(metar_db_path=args.metar_db, nwp_db_path=args.nwp_db)
    loop.run_once(mode=args.mode, date=args.date, market_type=args.market)


if __name__ == "__main__":
    main()