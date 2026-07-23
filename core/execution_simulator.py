#!/usr/bin/env python3
"""
Execution Simulator — Realistic fill simulation with Monte Carlo P&L ranges.

Provides a realistic execution model that:
- Widens spread for simulated fill (accounts for real market friction)
- Models market impact proportional to position_size / market_depth
- Runs Monte Carlo simulation: 1000 scenarios per trade
- Reports P&L percentiles (5th, 50th, 95th) instead of single point

Usage:
    from core.execution_simulator import ExecutionSimulator
    simulator = ExecutionSimulator()
    result = simulator.simulate_trade(ticker="KXHIGHKDEN-260722-85", ...)

Version: 1.0 — 2026-07-22 (Phase 19.2)
"""

import math
import random
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.market_cost_model import MARKET_COST_MODEL, MarketDepthSnapshot
from core.structured_logger import get_logger

_LOGGER = get_logger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────

DEFAULT_MONTE_CARLO_RUNS = 1000
NUM_MARKET_MAKERS = 5  # Assumed number of market makers for impact model

# Spread widening factor for simulated fills (accounts for adverse selection)
# Real fills tend to be worse than quoted spread
SPREAD_WIDENING_FACTOR = 1.3  # 30% wider than quoted spread for simulated fills

# Market impact model parameters
IMPACT_EXPONENT = 0.5          # Square-root impact model: impact ∝ sqrt(size/depth)
ALPHA_COST_FACTOR = 0.001      # Additional alpha cost per unit of volatility


@dataclass
class MonteCarloResult:
    """Results from a Monte Carlo simulation of trade execution."""
    mean_pnl: float
    median_pnl: float
    pnl_5th: float
    pnl_95th: float
    pnl_25th: float
    pnl_75th: float
    std_pnl: float
    sharpe_ratio: float
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    num_scenarios: int
    expected_fill_price: float
    fill_std: float
    full_fill_probability: float
    partial_fill_risk: str


@dataclass
class SimulationScenario:
    """One simulated execution scenario."""
    fill_price: float
    fill_fraction: float
    fill_cost: float
    gross_pnl: float
    net_pnl: float
    execution_quality: str  # "good", "average", "poor"


@dataclass
class TradeSimulationResult:
    """Complete simulation result for a single trade."""
    ticker: str
    direction: str  # "LONG" or "SHORT"
    requested_size: float
    signal_price: float
    market_price: float
    model_fair_value: float
    edge: float
    initial_spread: float
    expected_cost_per_contract: float
    monte_carlo: MonteCarloResult
    scenarios: List[SimulationScenario] = field(default_factory=list)
    execution_summary: str = ""


class ExecutionSimulator:
    """
    Realistic execution simulator with Monte Carlo P&L estimation.

    Simulates trade execution accounting for:
    - Real spread from Kalshi API (widened by SPREAD_WIDENING_FACTOR)
    - Market impact (square-root model)
    - Partial fill probability
    - Random fill price within the execution range
    """

    def __init__(self, num_mc_runs: int = DEFAULT_MONTE_CARLO_RUNS):
        self.num_mc_runs = num_mc_runs
        self.cost_model = MARKET_COST_MODEL

    def simulate_trade(
        self,
        ticker: str,
        direction: str,
        requested_size_contracts: float,
        signal_price: float,
        model_fair_value: float,
        market_price: Optional[float] = None,
        force_refresh_depth: bool = False,
    ) -> TradeSimulationResult:
        """
        Simulate a trade with Monte Carlo P&L estimation.

        Args:
            ticker: Full market ticker (e.g. 'KXHIGHKDEN-260722-85')
            direction: 'LONG' (buy) or 'SHORT' (sell)
            requested_size_contracts: Number of contracts to trade
            signal_price: Price at which signal was generated
            model_fair_value: Analytical fair value from model
            market_price: Current market mid price (auto-fetched if None)
            force_refresh_depth: Bypass depth cache

        Returns:
            TradeSimulationResult with P&L percentiles
        """
        direction = direction.upper()
        is_buy = direction == "LONG"

        # Fetch market depth
        depth = self.cost_model.get_market_depth_snapshot(ticker, force_refresh_depth)
        if depth is None:
            _LOGGER.warning("exec_sim_no_depth ticker=%s using fallback", ticker)
            # Create a synthetic depth with default spread
            spread = self.cost_model.spread
            depth = MarketDepthSnapshot(
                yes_bid=0.5 - spread / 2,
                yes_ask=0.5 + spread / 2,
                spread=spread,
                bid_volume=100,
                ask_volume=100,
                total_volume=200,
                last_price=0.5,
                implied_depth_score=0.3,
            )

        if market_price is None:
            market_price = (depth.yes_bid + depth.yes_ask) / 2.0

        initial_spread = depth.spread
        edge = model_fair_value - market_price if is_buy else market_price - model_fair_value

        # Run Monte Carlo scenarios
        scenarios = self._run_monte_carlo(
            depth=depth,
            is_buy=is_buy,
            requested_size=requested_size_contracts,
            market_price=market_price,
            model_fair_value=model_fair_value,
        )

        # Aggregate results
        mc_result = self._aggregate_scenarios(scenarios, requested_size_contracts)

        # Cost estimate
        cost_est = self.cost_model.estimate_total_cost(
            ticker=ticker,
            position_size_contracts=requested_size_contracts,
            is_buy=is_buy,
        )

        # Build summary
        summary = self._build_summary(mc_result, direction, edge, initial_spread)

        return TradeSimulationResult(
            ticker=ticker,
            direction=direction,
            requested_size=requested_size_contracts,
            signal_price=signal_price,
            market_price=market_price,
            model_fair_value=model_fair_value,
            edge=edge,
            initial_spread=initial_spread,
            expected_cost_per_contract=cost_est['total_cost_per_contract'],
            monte_carlo=mc_result,
            scenarios=scenarios,
            execution_summary=summary,
        )

    def _run_monte_carlo(
        self,
        depth: MarketDepthSnapshot,
        is_buy: bool,
        requested_size: float,
        market_price: float,
        model_fair_value: float,
    ) -> List[SimulationScenario]:
        """Run num_mc_runs simulation scenarios."""
        scenarios = []
        bid = depth.yes_bid
        ask = depth.yes_ask
        spread = depth.spread
        mid = (bid + ask) / 2.0
        est_depth = max(depth.total_volume, 1)

        for _ in range(self.num_mc_runs):
            # 1. Spread widening: simulated fills are worse than quoted
            effective_spread = spread * SPREAD_WIDENING_FACTOR
            half_eff_spread = effective_spread / 2.0

            # 2. Market impact: square-root model
            impact_ratio = math.sqrt(requested_size / max(est_depth, 1))
            if impact_ratio > 3.0:
                impact_ratio = 3.0  # Cap extreme impact

            market_impact = impact_ratio * half_eff_spread * 0.5

            # 3. Random fill price
            # For buys: fill between mid + noise and ask + impact
            # For sells: fill between bid - impact and mid - noise
            noise = random.gauss(0, half_eff_spread * 0.3)

            if is_buy:
                # Buy: pay between mid and ask (widened) + impact
                min_fill = mid + noise
                max_fill = ask + half_eff_spread + market_impact
                fill_price = random.uniform(min_fill, max_fill)
                fill_price = min(fill_price, 1.0)  # Cap at 1.0
            else:
                # Sell: receive between bid (widened) - impact and mid
                min_fill = bid - half_eff_spread - market_impact
                max_fill = mid + noise
                fill_price = random.uniform(min_fill, max_fill)
                fill_price = max(fill_price, 0.0)  # Floor at 0.0

            # 4. Partial fill probability
            impact_ratio_linear = requested_size / max(est_depth, 1)
            if impact_ratio_linear >= 0.5:
                fill_fraction = max(0.3, 1.0 / (1.0 + impact_ratio_linear))
                # Add randomness
                fill_fraction *= random.uniform(0.8, 1.2)
                fill_fraction = min(1.0, max(0.1, fill_fraction))
            else:
                fill_fraction = random.uniform(0.9, 1.0)

            # 5. Calculate P&L
            filled_size = requested_size * fill_fraction
            fill_cost = fill_price * filled_size

            if is_buy:
                gross_pnl = (model_fair_value - fill_price) * filled_size
            else:
                gross_pnl = (fill_price - model_fair_value) * filled_size

            # Net P&L after execution costs (spread + slippage already captured in fill)
            transaction_cost = effective_spread * filled_size * 0.5
            net_pnl = gross_pnl - transaction_cost

            # Execution quality classification
            if is_buy:
                slippage_from_mid = fill_price - mid
            else:
                slippage_from_mid = mid - fill_price

            if slippage_from_mid <= half_eff_spread * 0.5:
                quality = "good"
            elif slippage_from_mid <= half_eff_spread * 1.5:
                quality = "average"
            else:
                quality = "poor"

            scenarios.append(SimulationScenario(
                fill_price=round(fill_price, 4),
                fill_fraction=round(fill_fraction, 3),
                fill_cost=round(fill_cost, 4),
                gross_pnl=round(gross_pnl, 6),
                net_pnl=round(net_pnl, 6),
                execution_quality=quality,
            ))

        return scenarios

    def _aggregate_scenarios(
        self,
        scenarios: List[SimulationScenario],
        requested_size: float,
    ) -> MonteCarloResult:
        """Aggregate Monte Carlo scenarios into percentile statistics."""
        net_pnls = [s.net_pnl for s in scenarios]
        fill_prices = [s.fill_price for s in scenarios]
        fill_fractions = [s.fill_fraction for s in scenarios]

        sorted_pnls = sorted(net_pnls)
        n = len(sorted_pnls)

        if n == 0:
            return MonteCarloResult(
                mean_pnl=0.0, median_pnl=0.0, pnl_5th=0.0, pnl_95th=0.0,
                pnl_25th=0.0, pnl_75th=0.0, std_pnl=0.0, sharpe_ratio=0.0,
                win_rate=0.0, avg_win=0.0, avg_loss=0.0, max_drawdown=0.0,
                num_scenarios=0, expected_fill_price=0.0, fill_std=0.0,
                full_fill_probability=0.0, partial_fill_risk="unknown",
            )

        mean_pnl = sum(net_pnls) / n
        median_pnl = sorted_pnls[n // 2]
        pnl_5th = sorted_pnls[int(n * 0.05)]
        pnl_95th = sorted_pnls[int(n * 0.95)]
        pnl_25th = sorted_pnls[int(n * 0.25)]
        pnl_75th = sorted_pnls[int(n * 0.75)]

        variance = sum((p - mean_pnl) ** 2 for p in net_pnls) / n
        std_pnl = math.sqrt(variance)

        # Sharpe ratio (assumes risk-free rate = 0, uses P&L as return)
        sharpe_ratio = mean_pnl / max(std_pnl, 1e-8)

        # Win rate
        wins = [p for p in net_pnls if p > 0]
        losses = [p for p in net_pnls if p <= 0]
        win_rate = len(wins) / n
        avg_win = sum(wins) / max(len(wins), 1)
        avg_loss = sum(losses) / max(len(losses), 1)

        # Max drawdown (peak-to-trough in scenario P&Ls)
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in sorted_pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        avg_fill_price = sum(fill_prices) / n
        fill_variance = sum((f - avg_fill_price) ** 2 for f in fill_prices) / n
        fill_std = math.sqrt(fill_variance)

        full_fill_prob = sum(1 for f in fill_fractions if f >= 0.99) / n

        # Partial fill risk assessment
        partial_fill_rate = 1.0 - full_fill_prob
        if partial_fill_rate < 0.1:
            fill_risk = "low"
        elif partial_fill_rate < 0.3:
            fill_risk = "medium"
        else:
            fill_risk = "high"

        return MonteCarloResult(
            mean_pnl=round(mean_pnl, 6),
            median_pnl=round(median_pnl, 6),
            pnl_5th=round(pnl_5th, 6),
            pnl_95th=round(pnl_95th, 6),
            pnl_25th=round(pnl_25th, 6),
            pnl_75th=round(pnl_75th, 6),
            std_pnl=round(std_pnl, 6),
            sharpe_ratio=round(sharpe_ratio, 4),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 6),
            avg_loss=round(avg_loss, 6),
            max_drawdown=round(max_dd, 6),
            num_scenarios=n,
            expected_fill_price=round(avg_fill_price, 4),
            fill_std=round(fill_std, 4),
            full_fill_probability=round(full_fill_prob, 3),
            partial_fill_risk=fill_risk,
        )

    def _build_summary(
        self,
        mc: MonteCarloResult,
        direction: str,
        edge: float,
        spread: float,
    ) -> str:
        """Build a human-readable execution summary string."""
        edge_str = f"{edge * 100:.1f}¢" if abs(edge) < 1 else f"{edge:.4f}"
        spread_str = f"{spread * 100:.1f}¢"

        if mc.win_rate >= 0.65 and mc.sharpe_ratio >= 0.5:
            verdict = "FAVORABLE"
        elif mc.win_rate >= 0.45:
            verdict = "NEUTRAL"
        else:
            verdict = "UNFAVORABLE"

        return (
            f"{direction} | Edge {edge_str} | Spread {spread_str} | "
            f"Expected Fill: {mc.expected_fill_price:.4f} | "
            f"P&L 5th/50th/95th: {mc.pnl_5th:.4f}/{mc.median_pnl:.4f}/{mc.pnl_95th:.4f} | "
            f"Win Rate: {mc.win_rate:.1%} | Sharpe: {mc.sharpe_ratio:.2f} | "
            f"Full Fill: {mc.full_fill_probability:.0%} | "
            f"Fill Risk: {mc.partial_fill_risk} | "
            f"Verdict: {verdict}"
        )

    def summary_to_dict(self, result: TradeSimulationResult) -> Dict[str, any]:
        """Convert simulation result to a flat dict for logging/DB storage."""
        mc = result.monte_carlo
        return {
            'ticker': result.ticker,
            'direction': result.direction,
            'requested_size': result.requested_size,
            'signal_price': result.signal_price,
            'market_price': result.market_price,
            'model_fair_value': result.model_fair_value,
            'edge': result.edge,
            'initial_spread': result.initial_spread,
            'expected_cost_per_contract': result.expected_cost_per_contract,
            'mc_mean_pnl': mc.mean_pnl,
            'mc_median_pnl': mc.median_pnl,
            'mc_pnl_5th': mc.pnl_5th,
            'mc_pnl_95th': mc.pnl_95th,
            'mc_pnl_25th': mc.pnl_25th,
            'mc_pnl_75th': mc.pnl_75th,
            'mc_std_pnl': mc.std_pnl,
            'mc_sharpe': mc.sharpe_ratio,
            'mc_win_rate': mc.win_rate,
            'mc_full_fill_prob': mc.full_fill_probability,
            'mc_fill_risk': mc.partial_fill_risk,
            'execution_summary': result.execution_summary,
        }

    def is_trade_actionable(self, result: TradeSimulationResult) -> Tuple[bool, str]:
        """
        Determine if a trade should proceed based on Monte Carlo risk.

        Returns:
            Tuple of (actionable: bool, reason: str)
        """
        mc = result.monte_carlo

        # Deny if P&L 5th percentile is too negative (tail risk)
        if mc.pnl_5th < -0.05:  # 5¢+ loss at 5th percentile
            return False, f"Tail risk too high: P&L 5th={mc.pnl_5th:.4f}"

        # Deny if median P&L is negative
        if mc.median_pnl <= 0:
            return False, f"Median P&L non-positive: {mc.median_pnl:.4f}"

        # Deny if win rate is below 50%
        if mc.win_rate < 0.50:
            return False, f"Win rate below 50%: {mc.win_rate:.1%}"

        # Deny if partial fill risk is high
        if mc.partial_fill_risk == "high" and result.requested_size > 1:
            return False, f"High partial fill risk for size={result.requested_size}"

        # Deny if fill probability is too low
        if mc.full_fill_probability < 0.3 and result.requested_size > 1:
            return False, f"Full fill probability too low: {mc.full_fill_probability:.0%}"

        return True, "Trade is actionable within risk limits"


# Convenience singleton
EXECUTION_SIMULATOR = ExecutionSimulator()