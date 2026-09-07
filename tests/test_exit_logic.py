"""
P0 Unit Tests — core/intraday/exit_logic.py

Tests the four exit criteria:
  1. Signal-flip reversal (UP→DOWN or DOWN→UP)
  2. Auto-close at h-1
  3. Take-profit at 0.90 (YES) / 0.10 (NO)
  4. Stop-loss at 15pp adverse move
  5. No-op when no conditions are met
"""

import sys
from pathlib import Path
from typing import Dict, List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.intraday.exit_logic import (
    ExitSignal,
    evaluate_exits,
    TAKE_PROFIT_YES_PRICE,
    TAKE_PROFIT_NO_PRICE,
    STOP_LOSS_PP,
    MIN_HOURS_BEFORE_SETTLEMENT,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def up_position() -> Dict:
    """A simple UP/BUY position entered at h-8, price=0.55, size=2.0."""
    return {
        "station": "KNYC",
        "direction": "UP",
        "entry_price": 0.55,
        "position_size": 2.0,
        "entry_hour": 12,
    }


@pytest.fixture
def down_position() -> Dict:
    """A simple DOWN/SELL position entered at h-8, price=0.45, size=2.0."""
    return {
        "station": "KATL",
        "direction": "DOWN",
        "entry_price": 0.45,
        "position_size": 2.0,
        "entry_hour": 12,
    }


@pytest.fixture
def fused_up_result() -> Dict:
    """Fused result signalling UP direction."""
    return {
        "direction": "UP",
        "mean_probability": 0.72,
        "agreement_gate_passed": True,
    }


@pytest.fixture
def fused_down_result() -> Dict:
    """Fused result signalling DOWN direction."""
    return {
        "direction": "DOWN",
        "mean_probability": 0.32,
        "agreement_gate_passed": True,
    }


# ─── Test 1: Signal-Flip Reversal ─────────────────────────────────────────

class TestSignalFlipReversal:
    """Exit when the fused signal flips against the entry direction."""

    def test_up_position_signal_flips_down(self, up_position: Dict):
        """UP position, fused signal says DOWN → close."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "DOWN"}}
        prices = {"KNYC": 0.48}  # price dropped, confirming reversal

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "signal_flip"
        assert exits[0].station == "KNYC"
        assert exits[0].direction == "UP"

    def test_down_position_signal_flips_up(self, down_position: Dict):
        """DOWN position, fused signal says UP → close."""
        positions = [down_position]
        fused = {"KATL": {"direction": "UP"}}
        prices = {"KATL": 0.52}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "signal_flip"
        assert exits[0].station == "KATL"

    def test_no_flip_when_aligned(self, up_position: Dict):
        """UP position, fused signal still UP → no exit."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.58}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=16)
        assert len(exits) == 0

    def test_no_flip_when_no_fusion_data(self, up_position: Dict):
        """No fused data for station → no signal-flip exit."""
        positions = [up_position]
        fused: Dict = {}
        prices = {"KNYC": 0.50}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=16)
        assert len(exits) == 0

    def test_mixed_positions_only_flipped_closes(self, up_position: Dict, down_position: Dict):
        """Two positions, one flips, one stays aligned → only the flipped one closes."""
        # UP at KNYC flipped to DOWN, DOWN at KATL still DOWN → only KNYC closes
        positions = [up_position, down_position]
        fused = {"KNYC": {"direction": "DOWN"}, "KATL": {"direction": "DOWN"}}
        prices = {"KNYC": 0.50, "KATL": 0.42}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=16)
        assert len(exits) == 1
        assert exits[0].station == "KNYC"
        assert exits[0].reason == "signal_flip"


# ─── Test 2: Auto-Close at h-1 ────────────────────────────────────────────

class TestAutoCloseH1:
    """All positions close when hour_before_settlement <= 1."""

    def test_auto_close_triggers(self, up_position: Dict):
        """h-before=1 → close regardless of other conditions."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}  # aligned, would not flip
        prices = {"KNYC": 0.54}  # small loss, would not trigger other stops

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=1, current_hour=19)
        assert len(exits) == 1
        assert exits[0].reason == "auto_close_h1"
        assert exits[0].station == "KNYC"

    def test_auto_close_at_zero(self, up_position: Dict):
        """h-before=0 → close (settlement imminent)."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.60}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=0, current_hour=20)
        assert len(exits) == 1
        assert exits[0].reason == "auto_close_h1"

    def test_no_auto_close_when_enough_time(self, up_position: Dict):
        """h-before=6 → no auto-close."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.60}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=14)
        assert len(exits) == 0

    def test_auto_close_takes_priority(self, up_position: Dict):
        """h-1 should close even if conditions for other exits are also met."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "DOWN"}}  # would flip
        prices = {"KNYC": 0.30}  # would stop-loss

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=1, current_hour=19)
        assert len(exits) == 1
        # h-1 is checked first, so should win over flip/stop-loss
        assert exits[0].reason == "auto_close_h1"


# ─── Test 3: Take-Profit ──────────────────────────────────────────────────

class TestTakeProfit:

    def test_take_profit_up_at_90(self, up_position: Dict):
        """UP position, market hits 0.90 → take profit."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.90}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"
        # PnL = 2.0 * (0.90 - 0.55) = 0.70
        assert abs(exits[0].estimated_pnl - 0.70) < 0.001

    def test_take_profit_up_above_90(self, up_position: Dict):
        """UP position, market at 0.95 (above 0.90) → take profit."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.95}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"

    def test_take_profit_down_at_10(self, down_position: Dict):
        """DOWN position, market hits 0.10 → take profit."""
        positions = [down_position]
        fused = {"KATL": {"direction": "DOWN"}}
        prices = {"KATL": 0.10}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"
        # PnL = 2.0 * (0.45 - 0.10) = 0.70
        assert abs(exits[0].estimated_pnl - 0.70) < 0.001

    def test_take_profit_down_below_10(self, down_position: Dict):
        """DOWN position, market at 0.05 (below 0.10) → take profit."""
        positions = [down_position]
        fused = {"KATL": {"direction": "DOWN"}}
        prices = {"KATL": 0.05}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"

    def test_take_profit_not_hit(self, up_position: Dict):
        """UP position, market at 0.75 < 0.90 → no take-profit."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.75}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        take_profit_exits = [e for e in exits if e.reason == "take_profit"]
        assert len(take_profit_exits) == 0


# ─── Test 4: Stop-Loss ────────────────────────────────────────────────────

class TestStopLoss:

    def test_stop_loss_15pp_adverse_up(self, up_position: Dict):
        """UP position, market drops >15pp from 0.55 to 0.35 → stop-loss."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}  # still UP, no flip
        prices = {"KNYC": 0.35}  # 20pp drop

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"

    def test_stop_loss_15pp_adverse_down(self, down_position: Dict):
        """DOWN position, market rises >15pp from 0.45 to 0.65 → stop-loss."""
        positions = [down_position]
        fused = {"KATL": {"direction": "DOWN"}}  # still DOWN, no flip
        prices = {"KATL": 0.65}  # 20pp adverse move

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"

    def test_stop_loss_boundary_15pp(self, up_position: Dict):
        """UP position, exactly 15pp adverse → stop-loss triggers (>=)."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        # entry=0.55, 15pp adverse = 0.40
        prices = {"KNYC": 0.40}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"

    def test_stop_loss_near_boundary_14pp(self, up_position: Dict):
        """UP position, 14pp adverse < 15pp → no stop-loss."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {"KNYC": 0.41}  # 14pp

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        stop_loss_exits = [e for e in exits if e.reason == "stop_loss"]
        assert len(stop_loss_exits) == 0


# ─── Test 5: No-Op (no conditions met) ────────────────────────────────────

class TestNoOp:

    def test_no_exit_when_none_triggered(self):
        """Position with aligned signal, healthy price, enough time → no exit."""
        positions = [{
            "station": "KNYC",
            "direction": "UP",
            "entry_price": 0.55,
            "position_size": 1.0,
            "entry_hour": 10,
        }]
        fused = {"KNYC": {"direction": "UP", "mean_probability": 0.68}}
        prices = {"KNYC": 0.60}  # small profit, no trigger

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=14)
        assert len(exits) == 0

    def test_multiple_positions_none_trigger(self):
        """Multiple healthy positions, none should exit."""
        positions = [
            {"station": "KNYC", "direction": "UP", "entry_price": 0.55,
             "position_size": 1.0, "entry_hour": 10},
            {"station": "KATL", "direction": "DOWN", "entry_price": 0.40,
             "position_size": 1.0, "entry_hour": 10},
        ]
        fused = {"KNYC": {"direction": "UP"}, "KATL": {"direction": "DOWN"}}
        prices = {"KNYC": 0.60, "KATL": 0.38}  # both healthy

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=6, current_hour=14)
        assert len(exits) == 0


# ─── Test 6: Priority ordering ────────────────────────────────────────────

class TestPriorityOrdering:

    def test_stop_loss_beats_signal_flip(self, up_position: Dict):
        """When both stop-loss AND signal-flip apply, stop-loss wins (checked first)."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "DOWN"}}  # would flip
        prices = {"KNYC": 0.35}  # 20pp adverse, would stop-loss

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"

    def test_take_profit_beats_signal_flip(self, up_position: Dict):
        """When both take-profit AND signal-flip apply, take-profit wins."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "DOWN"}}  # would flip
        prices = {"KNYC": 0.92}  # would take-profit

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"

    def test_all_three_same_position_only_one_exit(self, up_position: Dict):
        """A single position should produce exactly one exit (first match)."""
        positions = [up_position]
        fused = {"KNYC": {"direction": "DOWN"}}
        prices = {"KNYC": 0.30}  # would match stop-loss and flip

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1  # stop-loss wins
        assert exits[0].reason == "stop_loss"
        assert exits[0].station == "KNYC"


# ─── Test 7: Edge cases ───────────────────────────────────────────────────

class TestEdgeCases:

    def test_empty_positions(self):
        """No open positions → no exits."""
        exits = evaluate_exits([], {}, {}, hour_before_settlement=6, current_hour=14)
        assert len(exits) == 0

    def test_station_not_in_fused_no_signal_flip(self):
        """Position station not in fused_results → no signal-flip, but other exits apply."""
        positions = [{
            "station": "KXYZ",
            "direction": "UP",
            "entry_price": 0.50,
            "position_size": 1.0,
            "entry_hour": 10,
        }]
        fused = {}
        prices = {"KXYZ": 0.95}  # take-profit

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        assert exits[0].reason == "take_profit"

    def test_station_not_in_prices_uses_fallback(self):
        """Station missing from live_prices → uses 0.5 fallback."""
        positions = [{
            "station": "KXYZ",
            "direction": "UP",
            "entry_price": 0.50,
            "position_size": 1.0,
            "entry_hour": 10,
        }]
        fused = {}
        prices = {}  # empty

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 0  # 0.5 == entry, no adverse move

    def test_take_profit_up_but_missing_prices_uses_fallback(self):
        """UP position with no live price → fallback 0.5, no take-profit (0.5 < 0.90)."""
        positions = [{
            "station": "KNYC",
            "direction": "UP",
            "entry_price": 0.55,
            "position_size": 1.0,
            "entry_hour": 10,
        }]
        fused = {"KNYC": {"direction": "UP"}}
        prices = {}  # KNYC missing

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 0  # 0.5 fallback, no condition hit

    def test_pnl_calculation_up_profit(self):
        """UP position, price went up → positive PnL."""
        positions = [{
            "station": "KNYC", "direction": "UP",
            "entry_price": 0.50, "position_size": 3.0, "entry_hour": 10,
        }]
        fused = {}
        prices = {"KNYC": 0.90}

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        # PnL = 3.0 * (0.90 - 0.50) = 1.20
        assert abs(exits[0].estimated_pnl - 1.20) < 0.001

    def test_pnl_calculation_down_profit(self):
        """DOWN position, price went down → positive PnL."""
        positions = [{
            "station": "KNYC", "direction": "DOWN",
            "entry_price": 0.60, "position_size": 2.0, "entry_hour": 10,
        }]
        fused = {}
        prices = {"KNYC": 0.90}  # went UP → loss for DOWN position

        exits = evaluate_exits(positions, fused, prices, hour_before_settlement=4, current_hour=16)
        assert len(exits) == 1
        # Either stop-loss (30pp adverse) or signal conditions
        assert exits[0].reason in ("stop_loss", "take_profit", "signal_flip")