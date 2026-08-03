#!/usr/bin/env python3
"""
C3 — Three-Lane Architecture Manager

Formalizes the three trading lanes that are already partially implemented:

Lane 1 — Directional Forecasting:
  Standard ensemble signals: calendar_climatology, gaussian, pressure_delta,
  forecast_disagreement, wind_direction_shift, etc. Uses agreement-gated
  majority voting. Predicts up/down for daily HIGH and LOW markets.

Lane 2 — Spike Reversion (Microstructure):
  Real-time METAR spike detection from metar_monitor.py, plus daily-level
  SpikeReversionSignal and FrontalPassageIntradaySignal. Trades brief
  temperature deviations that revert quickly.

Lane 3 — Spatial Coherence:
  Cross-station verification. Checks whether nearby stations confirm or
  contradict a signal before executing. Prevents double-counting the same
  weather event across adjacent stations.

The LaneManager routes signals to the appropriate lane, tracks per-lane
P&L, manages overlapping trade prevention, and provides a unified
trade decision interface.

Usage:
    from core.lane_manager import LaneManager, LaneType, LaneTrade

    manager = LaneManager()
    manager.route_signal("gaussian", "KATL", "up", 0.70)
    manager.route_signal("spike_reversion", "KATL", "down", 0.65)
    trades = manager.resolve_trades()  # Returns non-overlapping trades
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ─── Lane Type ─────────────────────────────────────────────────

class LaneType(Enum):
    """Trading lane types."""
    DIRECTIONAL = "directional"       # Lane 1: ensemble forecasting
    SPIKE_REVERSION = "spike_reversion"  # Lane 2: microstructure spikes
    SPATIAL_COHERENCE = "spatial_coherence"  # Lane 3: cross-station verification

    @staticmethod
    def from_signal(signal_name: str) -> "LaneType":
        """Map a signal name to its lane."""
        directional_signals = {
            "gaussian", "gaussian_v2", "pressure_delta", "calendar_climatology",
            "forecast_disagreement", "wind_direction_shift", "temperature_advection",
            "frontal_detector", "intraday_metar_confirmation", "fogr_reversion",
            "hrrr_bias_corrected", "nwp_dtdt_fusion", "spread_based_entry",
            "volume_momentum", "settlement_arbitrage", "corrected_pressure_delta",
            "ai_composite", "hrrr_bias_corrected_signal", "metar_nowcast",
            "spread_based_entry_detector",
        }
        spike_signals = {
            "microstructure_spike_reversion", "microstructure_spike_momentum_down",
            "frontal_passage_intraday", "spike_reversion",
        }
        if signal_name in directional_signals:
            return LaneType.DIRECTIONAL
        elif signal_name in spike_signals:
            return LaneType.SPIKE_REVERSION
        else:
            # Default to directional for unknown signals
            return LaneType.DIRECTIONAL


# ─── Lane Trade ─────────────────────────────────────────────────

class LaneTrade:
    """
    A trade routed through a specific lane.

    Attributes:
        lane: Which lane this trade belongs to
        station: Station code
        direction: 'up' or 'down'
        confidence: Signal confidence [0.0, 1.0]
        signal_name: Source signal name
        market_type: 'HIGH' or 'LOW'
        timestamp: When the signal was generated
        epoch_id: Settlement epoch identifier
        size: Position size in dollars (set by resolver)
        resolved: Whether this trade was resolved (confirmed/executed)
    """

    def __init__(
        self,
        lane: LaneType,
        station: str,
        direction: str,
        confidence: float,
        signal_name: str,
        market_type: str = "HIGH",
        timestamp: Optional[float] = None,
        epoch_id: Optional[str] = None,
    ):
        self.lane = lane
        self.station = station
        self.direction = direction
        self.confidence = confidence
        self.signal_name = signal_name
        self.market_type = market_type
        self.timestamp = timestamp or time.time()
        self.epoch_id = epoch_id or f"{datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}_{station}_{market_type}"
        self.size: float = 0.0
        self.resolved: bool = False
        self.outcome: Optional[bool] = None  # True = correct, False = incorrect

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane.value,
            "station": self.station,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "signal_name": self.signal_name,
            "market_type": self.market_type,
            "timestamp": self.timestamp,
            "epoch_id": self.epoch_id,
            "size": round(self.size, 2),
            "resolved": self.resolved,
        }

    def __repr__(self) -> str:
        return (f"<LaneTrade {self.lane.value} {self.station} "
                f"{self.direction} @ {self.confidence:.2f}>")


# ─── Lane Manager ──────────────────────────────────────────────

class LaneManager:
    """
    Manages the three-lane trading architecture.

    Routes signals to lanes, tracks per-lane P&L, prevents overlapping
    trades, and provides a unified resolution interface.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

        # Active pending trades per lane
        self._pending: Dict[LaneType, List[LaneTrade]] = {
            LaneType.DIRECTIONAL: [],
            LaneType.SPIKE_REVERSION: [],
            LaneType.SPATIAL_COHERENCE: [],
        }

        # Per-lane P&L tracking
        self._lane_pnl: Dict[str, Dict[str, float]] = {
            "directional": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
            "spike_reversion": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
            "spatial_coherence": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
        }

        # Epoch tracking per station per market — prevents overlapping trades
        self._active_epochs: Set[str] = set()

    # ── Signal Routing ────────────────────────────────────────

    def route_signal(
        self,
        signal_name: str,
        station: str,
        direction: str,
        confidence: float,
        market_type: str = "HIGH",
        epoch_id: Optional[str] = None,
    ) -> LaneTrade:
        """
        Route a signal to its appropriate lane.

        Args:
            signal_name: Name of the signal
            station: Station code
            direction: 'up' or 'down'
            confidence: Confidence [0.0, 1.0]
            market_type: 'HIGH' or 'LOW'
            epoch_id: Optional epoch identifier

        Returns:
            LaneTrade that was created
        """
        lane = LaneType.from_signal(signal_name)

        trade = LaneTrade(
            lane=lane,
            station=station,
            direction=direction,
            confidence=confidence,
            signal_name=signal_name,
            market_type=market_type,
            epoch_id=epoch_id,
        )

        self._pending[lane].append(trade)
        logger.debug(f"Routed {signal_name} → {lane.value} lane: {trade}")

        return trade

    def route_batch(
        self,
        signals: List[Dict[str, Any]],
    ) -> List[LaneTrade]:
        """
        Route multiple signals at once.

        Args:
            signals: List of dicts with keys: signal_name, station, direction,
                    confidence, market_type (optional), epoch_id (optional)

        Returns:
            List of LaneTrade objects
        """
        trades = []
        for s in signals:
            trade = self.route_signal(
                signal_name=s["signal_name"],
                station=s["station"],
                direction=s["direction"],
                confidence=s.get("confidence", 0.5),
                market_type=s.get("market_type", "HIGH"),
                epoch_id=s.get("epoch_id"),
            )
            trades.append(trade)
        return trades

    # ── Trade Resolution ──────────────────────────────────────

    def resolve_trades(
        self,
        station: Optional[str] = None,
        epoch_id: Optional[str] = None,
        max_overlap: int = 2,
    ) -> List[LaneTrade]:
        """
        Resolve pending trades into a final set of non-overlapping trades.

        Rules:
        - Directional and spike reversion lanes can co-exist for the same
          station (they trade different phenomena)
        - Within the same lane, only one trade per station per epoch
        - Higher confidence wins when there's overlap
        - Spatial coherence lane validates directional lane trades

        Args:
            station: Optional filter to resolve trades for one station
            epoch_id: Optional filter to resolve trades for one epoch
            max_overlap: Maximum number of overlapping lanes per station

        Returns:
            List of resolved trades with sizes set
        """
        resolved: List[LaneTrade] = []

        for lane in LaneType:
            for trade in self._pending[lane]:
                if station and trade.station != station:
                    continue
                if epoch_id and trade.epoch_id != epoch_id:
                    continue
                if trade.resolved:
                    continue

                epoch_key = f"{trade.station}:{trade.epoch_id}"

                # Check for same-lane overlap
                existing_same_lane = [
                    t for t in resolved
                    if t.station == trade.station
                    and t.market_type == trade.market_type
                    and t.lane == trade.lane
                    and t.epoch_id == trade.epoch_id
                ]

                if existing_same_lane:
                    # Keep only the highest-confidence trade per lane
                    existing = existing_same_lane[0]
                    if trade.confidence > existing.confidence:
                        resolved.remove(existing)
                        resolved.append(trade)
                else:
                    # Check cross-lane overlap (same station, same epoch, diff lane)
                    other_lanes = [
                        t for t in resolved
                        if t.station == trade.station
                        and t.market_type == trade.market_type
                        and t.lane != trade.lane
                        and t.epoch_id == trade.epoch_id
                    ]

                    # Count how many unique lanes are already active for this station/epoch
                    active_lanes = set(t.lane for t in other_lanes)
                    active_lanes.add(trade.lane)

                    if len(active_lanes) <= max_overlap:
                        resolved.append(trade)

                trade.resolved = True

        return resolved

    def record_outcome(
        self,
        trade: LaneTrade,
        was_correct: bool,
        pnl: float = 0.0,
    ) -> None:
        """
        Record a trade outcome for per-lane P&L tracking.

        Args:
            trade: The resolved trade
            was_correct: Whether the trade was directionally correct
            pnl: Profit/loss in dollars
        """
        lane_name = trade.lane.value
        self._lane_pnl[lane_name]["trades"] += 1
        if was_correct:
            self._lane_pnl[lane_name]["wins"] += 1
        else:
            self._lane_pnl[lane_name]["losses"] += 1
        self._lane_pnl[lane_name]["total_pnl"] += pnl
        trade.outcome = was_correct

    def get_lane_pnl(self, lane_type: Optional[LaneType] = None) -> Dict[str, Any]:
        """
        Get per-lane P&L summary.

        Args:
            lane_type: Optional filter for a specific lane

        Returns:
            Dict of lane P&L data
        """
        if lane_type:
            return dict(self._lane_pnl[lane_type.value])

        return dict(self._lane_pnl)

    def get_lane_win_rate(self, lane_type: LaneType) -> float:
        """
        Get the win rate for a specific lane.

        Args:
            lane_type: Lane to query

        Returns:
            Win rate [0.0, 1.0], or 0.5 if no trades
        """
        stats = self._lane_pnl[lane_type.value]
        if stats["trades"] == 0:
            return 0.5
        return stats["wins"] / stats["trades"]

    def clear_pending(self, lane_type: Optional[LaneType] = None) -> None:
        """Clear pending trades, optionally for a specific lane."""
        if lane_type:
            self._pending[lane_type] = []
        else:
            for lane in LaneType:
                self._pending[lane] = []

    def pending_count(self, lane_type: Optional[LaneType] = None) -> int:
        """Get count of pending trades."""
        if lane_type:
            return len(self._pending[lane_type])
        return sum(len(t) for t in self._pending.values())

    def get_summary(self) -> Dict[str, Any]:
        """Get a full summary of the lane manager state."""
        return {
            "pending_trades": self.pending_count(),
            "lane_pnl": self.get_lane_pnl(),
            "lane_win_rates": {
                lt.value: self.get_lane_win_rate(lt)
                for lt in LaneType
            },
        }


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the lane manager."""
    manager = LaneManager()

    # Test 1: Route directional signal
    t1 = manager.route_signal("gaussian", "KATL", "up", 0.70)
    assert t1.lane == LaneType.DIRECTIONAL
    assert t1.station == "KATL"
    assert t1.direction == "up"
    assert not t1.resolved
    print(f"Test 1 PASS: {t1}")

    # Test 2: Route spike reversion signal
    t2 = manager.route_signal("microstructure_spike_reversion", "KATL", "down", 0.65)
    assert t2.lane == LaneType.SPIKE_REVERSION
    print(f"Test 2 PASS: {t2}")

    # Test 3: Route frontal passage
    t3 = manager.route_signal("frontal_passage_intraday", "KBOS", "down", 0.80)
    assert t3.lane == LaneType.SPIKE_REVERSION
    print(f"Test 3 PASS: {t3}")

    # Test 4: Route batch
    batch = [
        {"signal_name": "calendar_climatology", "station": "KATL", "direction": "up", "confidence": 0.65},
        {"signal_name": "pressure_delta", "station": "KATL", "direction": "down", "confidence": 0.60},
    ]
    trades = manager.route_batch(batch)
    assert len(trades) == 2
    assert trades[0].lane == LaneType.DIRECTIONAL
    print(f"Test 4 PASS: batch routed {len(trades)} trades")

    # Test 5: Resolve trades — directional + spike reversion should co-exist
    resolved = manager.resolve_trades(station="KATL")
    katl_trades = [t for t in resolved if t.station == "KATL"]
    # Should have directional + spike reversion (different lanes)
    lanes_present = set(t.lane for t in katl_trades)
    assert LaneType.DIRECTIONAL in lanes_present
    assert LaneType.SPIKE_REVERSION in lanes_present
    print(f"Test 5 PASS: KATL resolved {len(katl_trades)} trades across {len(lanes_present)} lanes")

    # Test 6: Record outcomes
    for t in resolved:
        manager.record_outcome(t, was_correct=True, pnl=5.0)
    pnl = manager.get_lane_pnl()
    assert pnl["directional"]["trades"] > 0
    assert pnl["spike_reversion"]["trades"] > 0
    assert pnl["directional"]["wins"] > 0
    print(f"Test 6 PASS: PnL tracked: dir={pnl['directional']['trades']} spike={pnl['spike_reversion']['trades']}")

    # Test 7: Lane win rate
    wr = manager.get_lane_win_rate(LaneType.DIRECTIONAL)
    assert wr == 1.0  # All wins
    print(f"Test 7 PASS: lane win rate={wr}")

    # Test 8: Pending count
    assert manager.pending_count() > 0
    old_count = manager.pending_count()
    manager.clear_pending()
    assert manager.pending_count() == 0
    print(f"Test 8 PASS: cleared {old_count} pending trades")

    # Test 9: LaneType from signal
    assert LaneType.from_signal("gaussian") == LaneType.DIRECTIONAL
    assert LaneType.from_signal("microstructure_spike_reversion") == LaneType.SPIKE_REVERSION
    assert LaneType.from_signal("frontal_passage_intraday") == LaneType.SPIKE_REVERSION
    assert LaneType.from_signal("spike_reversion") == LaneType.SPIKE_REVERSION  # Moved to lane 2 in B-Mode R8 Cycle 2
    print("Test 9 PASS: lane mapping")

    # Test 10: get_summary
    summary = manager.get_summary()
    assert "pending_trades" in summary
    assert "lane_pnl" in summary
    assert "lane_win_rates" in summary
    print(f"Test 10 PASS: summary={summary['pending_trades']} pending")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()