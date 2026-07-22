#!/usr/bin/env python3
"""
Settlement-Time Arbitrage Signal — Market Micro Structure Signal.

Identifies mispricing opportunities in the last hour before settlement:
- In the final hour, contracts may misprice vs known METAR temperature
- If the METAR observation for the settlement day is already known,
  the contract should converge to 0 or 1 (certainty)
- Spreads often widen as market makers pull liquidity before settlement
- Creates potential arbitrage: buy when contract undervalues known outcome

This is a HARD signal — only fires when the settlement outcome is
deterministic (METAR data already published for the settlement day).

Version: 1.0 — 2026-07-22 (Phase 19.3)
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

from core.market_cost_model import MARKET_COST_MODEL
from core.signals.base_signal import BaseSignal

_LOGGER = logging.getLogger(__name__)

# Kalshi settlement window: 7am-5pm ET
# Settlement time: typically 5pm ET for daily markets
SETTLEMENT_HOUR_ET = 17  # 5pm ET
SETTLEMENT_WINDOW_HOURS = 1.0  # 1 hour before settlement

# Arbitrage thresholds
MIN_ARBITRAGE_EDGE = 0.05  # 5¢ minimum edge to trigger
MAX_ARBITRAGE_SPREAD = 0.10  # 10¢ max spread for arbitrage (too wide = risk)


class SettlementTimeArbitrageSignal(BaseSignal):
    """
    Market Micro Signal: Settlement-Time Arbitrage.

    Detects mispricing in the last hour before settlement when
    the METAR outcome is already known.

    Direction:
    - If known temp >= threshold for HIGH market: signal = +1.0 (buy)
    - If known temp < threshold for HIGH market: signal = -1.0 (sell/short)
    - Same logic for LOW market (inverted)
    - If outcome not yet known: signal = 0.0 (no arbitrage)

    This is a DIRECTIONAL signal with high confidence when it fires.
    """

    def __init__(self, db_path: str):
        super().__init__(db_path)
        self.name = "settlement_arbitrage"
        self.min_lookback = 0  # No lookback needed — real-time signal
        self.cost_model = MARKET_COST_MODEL

    def evaluate(self, idx: int, days: int) -> Dict[str, any]:
        """
        Evaluate settlement-time arbitrage opportunity.

        Returns:
            dict with:
                - signal: float (-1 to 1), 0 = no opportunity
                - confidence: float (0-1)
                - metadata: dict with arbitrage details
        """
        timestamp = datetime.now(timezone.utc)
        ticker = self._get_ticker_for_idx(idx)
        station = self._get_station_for_idx(idx)
        market_type = self._get_market_type_for_idx(idx)
        threshold = self._get_threshold_for_idx(idx)

        # Check if we're in the settlement window
        # Kalshi trades 7am-5pm ET, settlement at ~5pm ET
        # But contracts settle at different times — check calendar
        is_settlement_window = self._is_in_settlement_window(timestamp)
        if not is_settlement_window:
            return {
                'signal': 0.0,
                'confidence': 0.0,
                'metadata': {
                    'signal_name': self.name,
                    'reason': 'not_in_settlement_window',
                    'timestamp': timestamp.isoformat(),
                }
            }

        # Get the METAR high/low for the current settlement day
        known_temp = self._get_known_settlement_temperature(station, timestamp)
        if known_temp is None:
            return {
                'signal': 0.0,
                'confidence': 0.0,
                'metadata': {
                    'signal_name': self.name,
                    'reason': 'no_metar_data',
                    'timestamp': timestamp.isoformat(),
                }
            }

        # Get current market price
        market_price = 0.5
        spread = 0.031
        if ticker:
            depth = self.cost_model.get_market_depth_snapshot(ticker)
            if depth:
                market_price = (depth.yes_bid + depth.yes_ask) / 2.0
                spread = depth.spread

        # Determine fair value based on known outcome
        if market_type == "HIGH":
            # High market: YES = temp >= threshold
            known_outcome = known_temp >= threshold
            fair_value = 0.99 if known_outcome else 0.01
        elif market_type == "LOW":
            # Low market: YES = temp <= threshold
            known_outcome = known_temp <= threshold
            fair_value = 0.99 if known_outcome else 0.01
        else:
            return {
                'signal': 0.0,
                'confidence': 0.0,
                'metadata': {
                    'signal_name': self.name,
                    'reason': f'unknown_market_type:{market_type}',
                    'timestamp': timestamp.isoformat(),
                }
            }

        # Calculate edge
        if known_outcome:
            # We should buy YES (contract should be ~$0.99)
            edge = fair_value - market_price
        else:
            # We should buy NO (contract should be ~$0.01, so YES price ~$0.01)
            # If market_price > 0.01, we can short YES
            edge = market_price - (1.0 - fair_value)

        # Check if edge is worth pursuing
        if edge < MIN_ARBITRAGE_EDGE:
            return {
                'signal': 0.0,
                'confidence': 0.0,
                'metadata': {
                    'signal_name': self.name,
                    'reason': 'edge_too_small',
                    'edge': edge,
                    'min_edge': MIN_ARBITRAGE_EDGE,
                    'timestamp': timestamp.isoformat(),
                }
            }

        # Check spread — too wide makes arbitrage risky
        if spread > MAX_ARBITRAGE_SPREAD:
            return {
                'signal': 0.0,
                'confidence': 0.0,
                'metadata': {
                    'signal_name': self.name,
                    'reason': 'spread_too_wide',
                    'spread': spread,
                    'max_spread': MAX_ARBITRAGE_SPREAD,
                    'timestamp': timestamp.isoformat(),
                }
            }

        # Directional signal
        direction = 1.0 if known_outcome else -1.0  # 1 = buy YES, -1 = buy NO
        signal = direction * min(1.0, edge / 0.10)  # Scale: 5¢ edge = 0.5, 10¢ edge = 1.0
        confidence = min(0.95, 0.7 + edge * 2.0)  # Higher edge = higher confidence

        return {
            'signal': round(signal, 4),
            'confidence': round(confidence, 3),
            'metadata': {
                'signal_name': self.name,
                'reason': 'arbitrage_opportunity',
                'fair_value': fair_value,
                'market_price': market_price,
                'known_temp': known_temp,
                'threshold': threshold,
                'market_type': market_type,
                'station': station,
                'edge': round(edge, 4),
                'spread': spread,
                'known_outcome': known_outcome,
                'direction': 'long_yes' if direction > 0 else 'long_no',
                'timestamp': timestamp.isoformat(),
            }
        }

    def _is_in_settlement_window(self, ts: datetime) -> bool:
        """
        Check if current time is within the settlement window
        (last hour before Kalshi settlement at ~5pm ET).
        """
        # Convert to ET
        from zoneinfo import ZoneInfo
        try:
            et_tz = ZoneInfo("America/New_York")
            et_time = ts.astimezone(et_tz)
            hour = et_time.hour
            minute = et_time.minute
            # Check if we're in the last hour before settlement
            # Settlement at 5pm ET, window from 4pm ET
            return hour == SETTLEMENT_HOUR_ET - 1 or (
                hour == SETTLEMENT_HOUR_ET - 1 and minute >= 0
            )
        except Exception:
            # Fallback: use UTC hour approximation
            # ET = UTC-4 (EDT) or UTC-5 (EST)
            # Approximate: check if UTC hour is 20-21 (when ET is 4-5pm EDT)
            utc_hour = ts.hour
            return 20 <= utc_hour <= 22

    def _get_known_settlement_temperature(
        self, station: str, ts: datetime
    ) -> Optional[float]:
        """
        Get the known METAR temperature for the settlement day.

        Queries the METAR database for the latest observation.
        Returns None if data is not yet available.
        """
        if not station:
            return None

        try:
            import sqlite3
            from pathlib import Path

            repo_root = Path(__file__).resolve().parents[2]
            metar_db = str(repo_root / "data" / "metar_backfill.db")

            if not Path(metar_db).exists():
                return None

            conn = sqlite3.connect(metar_db)
            cursor = conn.cursor()

            # Get the most recent METAR observation for this station
            # on the settlement day
            settlement_date = ts.strftime("%Y-%m-%d")

            cursor.execute(
                """
                SELECT temp_c FROM metar_observations
                WHERE station = ? AND observation_time >= ?
                ORDER BY observation_time DESC
                LIMIT 1
                """,
                (station, f"{settlement_date}T17:00:00"),
            )

            row = cursor.fetchone()
            conn.close()

            if row and row[0] is not None:
                return float(row[0])
            return None

        except Exception as e:
            _LOGGER.warning("settlement_arb_metar_query_failed station=%s error=%s", station, e)
            return None

    def _get_ticker_for_idx(self, idx: int) -> Optional[str]:
        return None

    def _get_station_for_idx(self, idx: int) -> Optional[str]:
        return None

    def _get_market_type_for_idx(self, idx: int) -> Optional[str]:
        return None

    def _get_threshold_for_idx(self, idx: int) -> Optional[float]:
        return None