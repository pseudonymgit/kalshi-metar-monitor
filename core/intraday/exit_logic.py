"""
Exit Logic — Per-Position Exit Decisions for Intraday Pipeline (v1.0 — 2026-09-07)

Evaluates open positions against four exit criteria:
  1. Signal-flip reversal: close if fused signal direction opposes entry direction
  2. Auto-close at h-1: close all positions 1 hour before settlement
  3. Take-profit: close if market price hits 0.90 (BUY/YES) or 0.10 (SELL/NO)
  4. Stop-loss: close if market price moves >15pp against entry

Deterministic math only — no AI/ML.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────
TAKE_PROFIT_YES_PRICE = 0.90    # Take profit if YES contract hits 0.90
TAKE_PROFIT_NO_PRICE = 0.10     # Take profit if NO contract hits 0.10
STOP_LOSS_PP = 15.0             # Hard stop-loss: 15 percentage points adverse move
MIN_HOURS_BEFORE_SETTLEMENT = 1  # Auto-close threshold


@dataclass
class ExitSignal:
    """A single exit decision for an open position."""
    station: str
    reason: str               # "signal_flip", "auto_close_h1", "take_profit", "stop_loss"
    direction: str            # "UP" or "DOWN"
    entry_price: float
    current_price: float
    position_size: float
    estimated_pnl: float
    entry_hour: int
    current_hour: int


def _estimate_pnl(
    direction: str,
    entry_price: float,
    current_price: float,
    position_size: float,
) -> float:
    """Estimate PnL for a position given current market price."""
    direction_u = direction.upper()
    if direction_u in ("UP", "BUY", "YES"):
        return position_size * (current_price - entry_price)
    else:
        return position_size * (entry_price - current_price)


def _is_direction_up(direction: str) -> bool:
    """Return True if direction is UP/BUY/YES, False for DOWN/SELL/NO."""
    return direction.upper() in ("UP", "BUY", "YES")


def _is_fused_raised(fused_direction: str) -> bool:
    """Return True if fused direction indicates the asset goes up."""
    return fused_direction.upper() in ("UP", "HIGH", "YES", "1")


def evaluate_exits(
    open_positions: List[Dict],
    fused_results: Dict[str, object],
    live_prices: Dict[str, float],
    hour_before_settlement: int,
    current_hour: int = 0,
) -> List[ExitSignal]:
    """
    Evaluate all open positions against exit criteria.

    Checks are ordered by priority: auto-close (h-1) > take-profit > stop-loss > signal-flip.
    Each position closes on the first matching condition (continue keeps the loop moving).

    Args:
        open_positions: List of open position dicts, each with:
            - station: str (ICAO code)
            - direction: str ("UP" or "DOWN")
            - entry_price: float
            - position_size: float
            - entry_hour: int
        fused_results: Dict[station -> object] with attributes:
            - direction: str ("UP" or "DOWN")
            - mean_probability: float [0,1]
        live_prices: Dict[station -> float] current market prices
        hour_before_settlement: int hours until settlement
        current_hour: int current UTC hour (for logging)

    Returns:
        List of ExitSignal for positions that should be closed
    """
    exit_signals: List[ExitSignal] = []

    for position in open_positions:
        station = position.get("station", "")
        direction = position.get("direction", "UP")
        entry_price = position.get("entry_price", 0.5)
        position_size = position.get("position_size", 1.0)
        entry_hour = position.get("entry_hour", 0)

        current_price = live_prices.get(station, 0.5)
        entry_is_up = _is_direction_up(direction)

        # 1. Auto-close at h-1 — highest priority
        if hour_before_settlement <= MIN_HOURS_BEFORE_SETTLEMENT:
            pnl = _estimate_pnl(direction, entry_price, current_price, position_size)
            exit_signals.append(ExitSignal(
                station=station, reason="auto_close_h1",
                direction=direction, entry_price=entry_price,
                current_price=current_price, position_size=position_size,
                estimated_pnl=pnl, entry_hour=entry_hour,
                current_hour=current_hour,
            ))
            logger.info(
                "EXIT %s %s auto_close_h1 pnl=%.4f (h-before=%d)",
                station, direction, pnl, hour_before_settlement,
            )
            continue

        # 2. Take-profit
        if entry_is_up and current_price >= TAKE_PROFIT_YES_PRICE:
            pnl = _estimate_pnl(direction, entry_price, current_price, position_size)
            exit_signals.append(ExitSignal(
                station=station, reason="take_profit",
                direction=direction, entry_price=entry_price,
                current_price=current_price, position_size=position_size,
                estimated_pnl=pnl, entry_hour=entry_hour,
                current_hour=current_hour,
            ))
            logger.info(
                "EXIT %s %s take_profit pnl=%.4f (price=%.4f >= %.2f)",
                station, direction, pnl, current_price, TAKE_PROFIT_YES_PRICE,
            )
            continue

        if not entry_is_up and current_price <= TAKE_PROFIT_NO_PRICE:
            pnl = _estimate_pnl(direction, entry_price, current_price, position_size)
            exit_signals.append(ExitSignal(
                station=station, reason="take_profit",
                direction=direction, entry_price=entry_price,
                current_price=current_price, position_size=position_size,
                estimated_pnl=pnl, entry_hour=entry_hour,
                current_hour=current_hour,
            ))
            logger.info(
                "EXIT %s %s take_profit pnl=%.4f (price=%.4f <= %.2f)",
                station, direction, pnl, current_price, TAKE_PROFIT_NO_PRICE,
            )
            continue

        # 3. Stop-loss: >15pp adverse move
        if entry_is_up:
            adverse_move_pp = (entry_price - current_price) * 100.0
        else:
            adverse_move_pp = (current_price - entry_price) * 100.0

        if adverse_move_pp >= STOP_LOSS_PP:
            pnl = _estimate_pnl(direction, entry_price, current_price, position_size)
            exit_signals.append(ExitSignal(
                station=station, reason="stop_loss",
                direction=direction, entry_price=entry_price,
                current_price=current_price, position_size=position_size,
                estimated_pnl=pnl, entry_hour=entry_hour,
                current_hour=current_hour,
            ))
            logger.info(
                "EXIT %s %s stop_loss pnl=%.4f (adverse=%.1fpp)",
                station, direction, pnl, adverse_move_pp,
            )
            continue

        # 4. Signal-flip reversal
        if station in fused_results and fused_results[station] is not None:
            fused = fused_results[station]
            # Support both dict-style .get() and attribute-style access
            if hasattr(fused, "direction"):
                fused_dir = str(fused.direction).upper()
            elif isinstance(fused, dict):
                fused_dir = str(fused.get("direction", "")).upper()
            else:
                fused_dir = ""

            fused_is_up = _is_fused_raised(fused_dir)

            if entry_is_up != fused_is_up:
                pnl = _estimate_pnl(direction, entry_price, current_price, position_size)
                exit_signals.append(ExitSignal(
                    station=station, reason="signal_flip",
                    direction=direction, entry_price=entry_price,
                    current_price=current_price, position_size=position_size,
                    estimated_pnl=pnl, entry_hour=entry_hour,
                    current_hour=current_hour,
                ))
                current_dir_label = "UP" if fused_is_up else "DOWN"
                logger.info(
                    "EXIT %s %s signal_flip→%s pnl=%.4f",
                    station, direction, current_dir_label, pnl,
                )
                continue

    return exit_signals