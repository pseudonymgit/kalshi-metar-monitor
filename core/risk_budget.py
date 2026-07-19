#!/usr/bin/env python3
"""
Risk Budget Allocation — Edge 16, Phase 3 Risk Management

Implements a 3D allocation matrix (City × Market type × Signal type) for
distributing a fixed risk budget across concurrent positions.

Constraints:
  - Total budget: $250, $10/trade, max 25 concurrent positions
  - Max 20% per city ($50)
  - Max 30% per market type ($75)
  - Max 40% per signal type ($100)
  - Max 0.6 pairwise correlation between open positions

Deterministic math only — no AI/ML.

Version: Edge 16 — 2026-07-18
"""

import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

TOTAL_BUDGET_DEFAULT = 250.0
TRADE_SIZE = 10.0
MAX_CONCURRENT = 25

CITY_LIMIT_PCT = 0.20       # 20% per city
MARKET_LIMIT_PCT = 0.30     # 30% per market type
SIGNAL_LIMIT_PCT = 0.40     # 40% per signal type
CORRELATION_CAP = 0.60      # max pairwise correlation


@dataclass
class PositionRecord:
    """Tracks a single open position for budget-allocation purposes."""
    station: str
    market_type: str
    signal_type: str
    position_size: float
    opened_at: datetime = field(default_factory=datetime.utcnow)


class RiskBudgetAllocator:
    """
    Manages risk-budget allocation across a 3D matrix of
    City × Market type × Signal type.

    Each axis has an independent cap; total concurrent positions and
    total notional are also bounded.
    """

    def __init__(self, total_budget: float = TOTAL_BUDGET_DEFAULT):
        self.total_budget = total_budget
        self.trade_size = TRADE_SIZE
        self.max_concurrent = MAX_CONCURRENT

        self.city_limit = total_budget * CITY_LIMIT_PCT
        self.market_limit = total_budget * MARKET_LIMIT_PCT
        self.signal_limit = total_budget * SIGNAL_LIMIT_PCT

        # Active positions keyed by (station, market_type, signal_type)
        self._positions: Dict[Tuple[str, str, str], PositionRecord] = {}

        # Running tallies per axis
        self._city_usage: Dict[str, float] = {}
        self._market_usage: Dict[str, float] = {}
        self._signal_usage: Dict[str, float] = {}

        _LOGGER.info(
            "RiskBudgetAllocator initialised — budget=$%.0f, trade=$%.0f, "
            "max_concurrent=%d, city_cap=$%.0f, market_cap=$%.0f, "
            "signal_cap=$%.0f, corr_cap=%.2f",
            total_budget, TRADE_SIZE, MAX_CONCURRENT,
            self.city_limit, self.market_limit,
            self.signal_limit, CORRELATION_CAP,
        )

    # ─── Public API ────────────────────────────────────────────────────────

    def check_allocation(
        self,
        station: str,
        market_type: str,
        signal_type: str,
        position_size: float,
    ) -> Tuple[bool, str]:
        """
        Evaluate whether a new position fits within all budget constraints.

        Returns (allowed, reason).
        """
        size = abs(position_size)

        # 1. Concurrent count
        if len(self._positions) >= self.max_concurrent:
            return False, (
                f"Max concurrent positions ({self.max_concurrent}) reached"
            )

        # 2. Total notional
        current_total = sum(p.position_size for p in self._positions.values())
        if current_total + size > self.total_budget:
            return False, (
                f"Total budget ${self.total_budget:.0f} exceeded — "
                f"current=${current_total:.0f}, requested=${size:.0f}"
            )

        # 3. City cap
        city_current = self._city_usage.get(station, 0.0)
        if city_current + size > self.city_limit:
            return False, (
                f"City '{station}' cap ${self.city_limit:.0f} exceeded — "
                f"current=${city_current:.0f}, requested=${size:.0f}"
            )

        # 4. Market-type cap
        market_current = self._market_usage.get(market_type, 0.0)
        if market_current + size > self.market_limit:
            return False, (
                f"Market '{market_type}' cap ${self.market_limit:.0f} exceeded — "
                f"current=${market_current:.0f}, requested=${size:.0f}"
            )

        # 5. Signal-type cap
        signal_current = self._signal_usage.get(signal_type, 0.0)
        if signal_current + size > self.signal_limit:
            return False, (
                f"Signal '{signal_type}' cap ${self.signal_limit:.0f} exceeded — "
                f"current=${signal_current:.0f}, requested=${size:.0f}"
            )

        return True, "Allocation within all constraints"

    def record_position(
        self,
        station: str,
        market_type: str,
        signal_type: str,
        position_size: float,
    ) -> None:
        """
        Persist a position into the allocation matrix.
        Raises ValueError if constraints are violated (defensive).
        """
        allowed, reason = self.check_allocation(
            station, market_type, signal_type, position_size
        )
        if not allowed:
            raise ValueError(f"Allocation rejected: {reason}")

        key = (station, market_type, signal_type)
        size = abs(position_size)

        self._positions[key] = PositionRecord(
            station=station,
            market_type=market_type,
            signal_type=signal_type,
            position_size=size,
        )
        self._city_usage[station] = self._city_usage.get(station, 0.0) + size
        self._market_usage[market_type] = self._market_usage.get(market_type, 0.0) + size
        self._signal_usage[signal_type] = self._signal_usage.get(signal_type, 0.0) + size

        _LOGGER.info(
            "Position recorded — station=%s, market=%s, signal=%s, size=$%.2f",
            station, market_type, signal_type, size,
        )

    def remove_position(
        self,
        station: str,
        market_type: Optional[str] = None,
        signal_type: Optional[str] = None,
    ) -> int:
        """
        Remove one or more positions from the matrix.

        If market_type / signal_type are omitted, removes *all* positions
        for the given station.  Returns the number of positions removed.
        """
        removed = 0
        keys_to_delete: List[Tuple[str, str, str]] = []

        for key, pos in self._positions.items():
            if key[0] != station:
                continue
            if market_type is not None and key[1] != market_type:
                continue
            if signal_type is not None and key[2] != signal_type:
                continue
            keys_to_delete.append(key)

        for key in keys_to_delete:
            pos = self._positions.pop(key)
            size = pos.position_size
            self._city_usage[pos.station] -= size
            self._market_usage[pos.market_type] -= size
            self._signal_usage[pos.signal_type] -= size
            # Clean up zero entries
            if self._city_usage.get(pos.station, 0.0) <= 0:
                self._city_usage.pop(pos.station, None)
            if self._market_usage.get(pos.market_type, 0.0) <= 0:
                self._market_usage.pop(pos.market_type, None)
            if self._signal_usage.get(pos.signal_type, 0.0) <= 0:
                self._signal_usage.pop(pos.signal_type, None)
            removed += 1
            _LOGGER.info(
                "Position removed — station=%s, market=%s, signal=%s",
                pos.station, pos.market_type, pos.signal_type,
            )

        return removed

    def get_usage_report(self) -> dict:
        """
        Return a structured snapshot of current budget utilisation.
        """
        total_used = sum(p.position_size for p in self._positions.values())
        return {
            "total_budget": self.total_budget,
            "total_used": round(total_used, 2),
            "total_available": round(self.total_budget - total_used, 2),
            "concurrent_positions": len(self._positions),
            "max_concurrent": self.max_concurrent,
            "city_usage": {k: round(v, 2) for k, v in self._city_usage.items()},
            "market_usage": {k: round(v, 2) for k, v in self._market_usage.items()},
            "signal_usage": {k: round(v, 2) for k, v in self._signal_usage.items()},
            "city_limit": self.city_limit,
            "market_limit": self.market_limit,
            "signal_limit": self.signal_limit,
            "correlation_cap": CORRELATION_CAP,
        }

    def calculate_correlation_clamp(self, open_positions: list) -> float:
        """
        Given a list of correlation values between open positions,
        return the maximum allowed correlation for a new position.

        If any existing pairwise correlation exceeds CORRELATION_CAP,
        the clamp forces the new position's max correlation to 0
        (effectively blocking it).  Otherwise the clamp is CORRELATION_CAP.

        Args:
            open_positions: list of float correlation coefficients
                           (pairwise correlations of existing positions).

        Returns:
            float — maximum allowed correlation for a new entry.
        """
        if not open_positions:
            return CORRELATION_CAP

        max_existing = max(abs(c) for c in open_positions)
        if max_existing >= CORRELATION_CAP:
            _LOGGER.warning(
                "Correlation clamp triggered — max existing corr=%.3f ≥ cap=%.2f",
                max_existing, CORRELATION_CAP,
            )
            return 0.0  # Block new correlated entries

        # Remaining correlation headroom
        remaining = CORRELATION_CAP - max_existing
        return round(remaining, 4)


# ─── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rba = RiskBudgetAllocator()

    print("\n=== Risk Budget Allocator — Self-Test ===\n")

    # 1. Basic allocation
    ok, reason = rba.check_allocation("KATL", "temp_above", "momentum", 10.0)
    print(f"[1] check_allocation KATL/temp_above/momentum $10 → {ok}: {reason}")
    assert ok, "Should allow first allocation"

    rba.record_position("KATL", "temp_above", "momentum", 10.0)
    print(f"[1b] Position recorded.")

    # 2. City cap (max $50 → 5 trades)
    for i in range(4):
        rba.record_position("KATL", f"market_{i}", f"signal_{i}", 10.0)
    ok, reason = rba.check_allocation("KATL", "temp_below", "reversal", 10.0)
    print(f"[2] City cap (6th KATL) → {ok}: {reason}")
    assert not ok, "Should block 6th KATL position ($60 > $50 cap)"

    # 3. Market-type cap (max $75 → 7 positions total across stations)
    #    Already 1 temp_above from KATL, so only 6 more allowed ($70 + $10 = $80 > $75)
    for i in range(6):
        ok, reason = rba.check_allocation(f"station_{i}", "temp_above", f"sig_{i}", 10.0)
        if ok:
            rba.record_position(f"station_{i}", "temp_above", f"sig_{i}", 10.0)
    ok, reason = rba.check_allocation("station_99", "temp_above", "sig_99", 10.0)
    print(f"[3] Market cap (8th temp_above) → {ok}: {reason}")
    assert not ok, "Should block 8th temp_above ($80 > $75 cap)"

    # 4. Usage report
    report = rba.get_usage_report()
    print(f"\n[4] Usage report:\n{report}")
    assert report["concurrent_positions"] == 11
    assert report["total_used"] == 110.0

    # 5. Remove positions
    removed = rba.remove_position("KATL")
    print(f"\n[5] Removed {removed} KATL positions")
    assert removed == 5

    # 6. Correlation clamp — use fresh allocator to avoid market cap interference
    rba2 = RiskBudgetAllocator()
    clamp = rba2.calculate_correlation_clamp([0.3, 0.5, 0.1])
    print(f"[6] Correlation clamp (max existing 0.5) → {clamp}")
    assert clamp == round(CORRELATION_CAP - 0.5, 4)

    clamp_block = rba2.calculate_correlation_clamp([0.7])
    print(f"[6b] Correlation clamp (max existing 0.7 ≥ 0.6) → {clamp_block}")
    assert clamp_block == 0.0

    print("\n✅ All Risk Budget Allocator tests passed.\n")
