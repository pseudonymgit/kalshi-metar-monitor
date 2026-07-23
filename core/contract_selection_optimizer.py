#!/usr/bin/env python3
"""
Contract Selection Optimizer — Picks the best risk-adjusted contract.

When multiple contracts are available for the same station/market-type
(e.g. D+1 and D+2), the optimizer selects the best risk-adjusted edge.

Selection criteria:
1. Prefer D+1 when spread is tight (≤ 3.1¢ baseline)
2. Prefer D+2 when D+1 spread is excessive (> 5¢) and D+2 spread is reasonable
3. Account for time decay: D+2 has more uncertainty, so edge must compensate
4. Penalize contracts with low liquidity / wide spreads
5. Report full ranking with rationale

Usage:
    from core.contract_selection_optimizer import ContractSelectionOptimizer
    optimizer = ContractSelectionOptimizer()
    best = optimizer.select_best_contract(candidates=[...])

Version: 1.0 — 2026-07-22 (Phase 19.4)
"""

import math
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from core.market_cost_model import MARKET_COST_MODEL, MarketDepthSnapshot

_LOGGER = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────

# Spread thresholds (fraction of $1)
BASELINE_SPREAD = 0.031          # 3.1¢ mean measured spread
TIGHT_SPREAD_THRESHOLD = 0.031   # ≤ 3.1¢ = tight
MODERATE_SPREAD_THRESHOLD = 0.05 # 5¢ = moderate
WIDE_SPREAD_THRESHOLD = 0.08     # 8¢ = wide

# D+2 correction factors
D2_SPREAD_PENALTY = 0.5          # Additional spread penalty for D+2 (uncertainty)
D2_EDGE_MULTIPLIER = 1.5         # D+2 edge must be 1.5x D+1 edge to be worth it
D2_TIME_DECAY_DAYS = 2           # Number of days until D+2 settlement

# Liquidity thresholds
MIN_LIQUIDITY_DEPTH = 0.2        # Minimum depth score for tradable contract
MIN_DAILY_VOLUME = 10            # Minimum daily volume (contracts)

# Scoring weights
WEIGHT_EDGE = 0.40               # Edge score weight
WEIGHT_SPREAD = 0.25             # Spread score weight
WEIGHT_LIQUIDITY = 0.20          # Liquidity score weight
WEIGHT_TIME_DECAY = 0.15         # Time decay / certainty weight


@dataclass
class ContractCandidate:
    """A contract candidate for selection."""
    ticker: str
    station: str
    market_type: str  # 'HIGH' or 'LOW'
    horizon: str      # 'D+1' or 'D+2'
    threshold: int    # Temperature threshold
    settlement_date: str  # YYYY-MM-DD
    model_fair_value: float  # Analytical fair value (0-1)
    predicted_edge: float    # |fair_value - market_price|
    depth: Optional[MarketDepthSnapshot] = None
    estimated_cost: float = 0.0
    liquidity_score: float = 0.5
    composite_score: float = 0.0
    rank: int = 0
    rationale: str = ""


@dataclass
class SelectionResult:
    """Result of contract selection optimization."""
    best_contract: Optional[ContractCandidate] = None
    all_ranked: List[ContractCandidate] = field(default_factory=list)
    passed_gates: List[ContractCandidate] = field(default_factory=list)
    failed_gates: List[Tuple[ContractCandidate, str]] = field(default_factory=list)


class ContractSelectionOptimizer:
    """
    Selects the best contract from a set of candidates based on
    risk-adjusted edge, spread, liquidity, and time horizon.

    Typical use case: given multiple contracts for the same station
    (e.g., KXHIGHKDEN with D+1 and D+2 settlement), which one offers
    the best risk-adjusted return?
    """

    def __init__(self):
        self.cost_model = MARKET_COST_MODEL

    def select_best_contract(
        self,
        candidates: List[ContractCandidate],
    ) -> SelectionResult:
        """
        Select the best contract from a list of candidates.

        Args:
            candidates: List of ContractCandidate objects with at minimum
                       ticker, horizon, model_fair_value, and predicted_edge.

        Returns:
            SelectionResult with best contract and full ranking.
        """
        if not candidates:
            return SelectionResult()

        # Enrich candidates with market data
        enriched = self._enrich_candidates(candidates)

        # Apply gates (minimum liquidity, maximum spread, etc.)
        passed, failed = self._apply_gates(enriched)

        if not passed:
            # No candidates passed gates — return best effort from enriched
            sorted_all = sorted(enriched, key=lambda c: c.composite_score, reverse=True)
            for i, c in enumerate(sorted_all):
                c.rank = i + 1
            return SelectionResult(
                best_contract=None,
                all_ranked=sorted_all,
                passed_gates=[],
                failed_gates=failed,
            )

        # Score and rank
        scored = self._score_candidates(passed)
        scored.sort(key=lambda c: c.composite_score, reverse=True)

        for i, c in enumerate(scored):
            c.rank = i + 1

        return SelectionResult(
            best_contract=scored[0],
            all_ranked=scored,
            passed_gates=scored,
            failed_gates=failed,
        )

    def select_for_station(
        self,
        station: str,
        market_type: str,
        threshold: int,
        d1_ticker: str,
        d2_ticker: str,
        d1_fair_value: float,
        d2_fair_value: float,
        d1_edge: float,
        d2_edge: float,
        d1_settlement: Optional[str] = None,
        d2_settlement: Optional[str] = None,
    ) -> SelectionResult:
        """
        Convenience: select between D+1 and D+2 contracts for one station.

        Args:
            station: ICAO code
            market_type: 'HIGH' or 'LOW'
            threshold: Temperature threshold
            d1_ticker: D+1 market ticker
            d2_ticker: D+2 market ticker
            d1_fair_value / d2_fair_value: Analytical fair values
            d1_edge / d2_edge: Predicted edges
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        candidates = [
            ContractCandidate(
                ticker=d1_ticker,
                station=station,
                market_type=market_type,
                horizon='D+1',
                threshold=threshold,
                settlement_date=d1_settlement or today,
                model_fair_value=d1_fair_value,
                predicted_edge=d1_edge,
            ),
            ContractCandidate(
                ticker=d2_ticker,
                station=station,
                market_type=market_type,
                horizon='D+2',
                threshold=threshold,
                settlement_date=d2_settlement or today,
                model_fair_value=d2_fair_value,
                predicted_edge=d2_edge,
            ),
        ]

        return self.select_best_contract(candidates)

    # ─── Internal methods ───────────────────────────────────────────────

    def _enrich_candidates(
        self,
        candidates: List[ContractCandidate],
    ) -> List[ContractCandidate]:
        """Enrich candidates with live market data and compute scores."""
        enriched = []

        for c in candidates:
            # Fetch market depth
            depth = self.cost_model.get_market_depth_snapshot(c.ticker)
            if depth:
                c.depth = depth
                spread = depth.spread

                # Estimate cost
                cost_est = self.cost_model.estimate_total_cost(
                    ticker=c.ticker,
                    position_size_contracts=1.0,
                    is_buy=True,
                )
                c.estimated_cost = cost_est['total_cost_per_contract']

                # Liquidity score
                c.liquidity_score = depth.implied_depth_score
            else:
                spread = BASELINE_SPREAD
                c.estimated_cost = BASELINE_SPREAD / 2.0 + 0.005
                c.liquidity_score = 0.3

            # Compute composite score (initial)
            c.composite_score = self._compute_composite_score(c)
            enriched.append(c)

        return enriched

    def _compute_composite_score(self, c: ContractCandidate) -> float:
        """
        Compute a composite score for a candidate on 0-1 scale.

        Higher = better risk-adjusted opportunity.
        Components:
        - Edge score: normalized predicted edge (0-1)
        - Spread score: inverse of spread (0-1, tight = good)
        - Liquidity score: depth score (0-1)
        - Time decay penalty: D+2 gets penalized
        """
        spread = c.depth.spread if c.depth else BASELINE_SPREAD

        # Edge score: normalize to 0-1 scale
        # Edge of 10¢ = 1.0, edge of 0¢ = 0.0
        edge_score = min(1.0, c.predicted_edge / 0.10)

        # Spread score: inverse — tighter is better
        # 1¢ spread = 1.0, 10¢ spread = 0.0
        spread_score = max(0.0, 1.0 - spread / 0.10)

        # Liquidity score: use depth directly
        liquidity_score = c.liquidity_score

        # Time decay score
        # D+1 = 1.0, D+2 = 0.6 (more uncertainty)
        if c.horizon == 'D+1':
            time_decay_score = 1.0
        elif c.horizon == 'D+2':
            # D+2 starts at 0.6, gets worse as settlement approaches
            time_decay_score = 0.6
        else:
            time_decay_score = 0.5

        # Composite
        composite = (
            WEIGHT_EDGE * edge_score +
            WEIGHT_SPREAD * spread_score +
            WEIGHT_LIQUIDITY * liquidity_score +
            WEIGHT_TIME_DECAY * time_decay_score
        )

        return round(composite, 4)

    def _apply_gates(
        self,
        candidates: List[ContractCandidate],
    ) -> Tuple[List[ContractCandidate], List[Tuple[ContractCandidate, str]]]:
        """Apply minimum quality gates to candidates."""
        passed = []
        failed = []

        for c in candidates:
            spread = c.depth.spread if c.depth else BASELINE_SPREAD

            # Gate 1: Minimum edge
            if c.predicted_edge <= 0:
                failed.append((c, f"Non-positive edge: {c.predicted_edge:.4f}"))
                continue

            # Gate 2: Maximum spread
            if spread > WIDE_SPREAD_THRESHOLD and c.horizon == 'D+1':
                failed.append((c, f"Spread too wide for D+1: {spread:.4f} > {WIDE_SPREAD_THRESHOLD}"))
                continue

            if spread > WIDE_SPREAD_THRESHOLD * 1.5 and c.horizon == 'D+2':
                failed.append((c, f"Spread too wide for D+2: {spread:.4f} > {WIDE_SPREAD_THRESHOLD * 1.5}"))
                continue

            # Gate 3: Minimum liquidity
            if c.liquidity_score < MIN_LIQUIDITY_DEPTH:
                failed.append((c, f"Liquidity too low: {c.liquidity_score:.3f} < {MIN_LIQUIDITY_DEPTH}"))
                continue

            # Gate 4: D+2 threshold check
            if c.horizon == 'D+2':
                # D+2 edge must be sufficient to compensate for extra uncertainty
                d1_candidates = [x for x in candidates if x.horizon == 'D+1']
                if d1_candidates:
                    best_d1 = max(d1_candidates, key=lambda x: x.predicted_edge)
                    if c.predicted_edge < best_d1.predicted_edge * D2_EDGE_MULTIPLIER:
                        # Not a hard fail — just add a note
                        c.rationale += (
                            f"D+2 edge ({c.predicted_edge:.4f}) < "
                            f"{D2_EDGE_MULTIPLIER}x D+1 edge ({best_d1.predicted_edge:.4f}); "
                            f"D+2 selected only if D+1 fails gates"
                        )

            passed.append(c)

        return passed, failed

    def _score_candidates(
        self,
        candidates: List[ContractCandidate],
    ) -> List[ContractCandidate]:
        """Score and rank candidates with rationale."""
        for c in candidates:
            spread = c.depth.spread if c.depth else BASELINE_SPREAD
            parts = []

            # Generate rationale
            spread_str = f"{spread * 100:.1f}¢"
            edge_str = f"{c.predicted_edge * 100:.1f}¢"

            if c.horizon == 'D+1':
                if spread <= TIGHT_SPREAD_THRESHOLD:
                    parts.append(f"D+1 spread {spread_str} is tight — favorable")
                elif spread <= MODERATE_SPREAD_THRESHOLD:
                    parts.append(f"D+1 spread {spread_str} is moderate — acceptable")
                else:
                    parts.append(f"D+1 spread {spread_str} is wide — only if D+2 is worse")
            else:  # D+2
                if spread <= TIGHT_SPREAD_THRESHOLD:
                    parts.append(f"D+2 spread {spread_str} is tight — consider despite time decay")
                elif spread <= MODERATE_SPREAD_THRESHOLD:
                    d2_spread_penalty = spread * (1 + D2_SPREAD_PENALTY)
                    parts.append(f"D+2 spread {spread_str} adjusted to {d2_spread_penalty * 100:.1f}¢ (with uncertainty penalty)")
                else:
                    parts.append(f"D+2 spread {spread_str} is wide — not recommended")

            parts.append(f"Edge {edge_str}")
            parts.append(f"Liquidity: {c.liquidity_score:.2f}")

            if c.liquidity_score < 0.4:
                parts.append("— low liquidity, size accordingly")

            c.rationale = " | ".join(parts)

        return candidates


# Convenience singleton
CONTRACT_SELECTOR = ContractSelectionOptimizer()