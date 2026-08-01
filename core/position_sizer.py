#!/usr/bin/env python3
"""
Unified Position Sizer — Phase 1 Build

Merge of kelly_position_sizer.py (Edge 13, Phase 3 risk management) and
position_sizing.py (SH3 enhancement).  kelly_position_sizer.py is the primary
source; position_sizing.py's features are folded in where they add value.

Features:
- Kelly-criterion position sizing with adaptive confidence scaling
- Bayesian Beta-Binomial belief updates
- Fee-aware Kelly formula (0.0205 round-trip fee from market_cost_model)
- Fractional Kelly (half-Kelly default)
- Disagreement-based Kelly multiplier (B-Mode Cycle 6)
- Instance config factory (PROD, DEV, SBOX)
- 8% bankroll cap per position (kelly_position_sizer.py default)
- Trailing 10% max drawdown protection
- Rolling win rate tracking (30-day window)

Deterministic math only — no AI/ML.

Version: 2.1 — Phase 1 Build (2026-07-29)
"""

import logging
import math
from typing import Tuple, Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

MAX_BANKROLL_PCT = 0.08         # 8% cap per position (kelly_position_sizer.py)
MAX_DRAWDOWN_PCT = 0.10          # 10% trailing drawdown
MAX_POSITION_PCT = 0.25          # 25% max of balance (legacy SH3 cap)

# Round-trip fee = spread/2 + commission + slippage = 3.1¢/2 + 0¢ + 0.5¢ = 2.05¢
# Single source of truth from market_cost_model
try:
    from core.market_cost_model import ROUND_TRIP_FEE as DEFAULT_COST_FRACTION
except ImportError:
    import sys
    sys.path.insert(0, '.')
    from core.market_cost_model import ROUND_TRIP_FEE as DEFAULT_COST_FRACTION

# Adaptive scaling thresholds
CONFIDENCE_TIERS = [
    (0.80, 2.0),   # ≥ 80% → 2×
    (0.70, 1.5),   # 70–80% → 1.5×
    (0.60, 1.0),   # 60–70% → 1×
    (0.00, 0.5),   # < 60% → 0.5×
]

# Max contracts per trade (cap from R11 E2)
MAX_CONTRACTS = 500

# Entry price range limits (cap from R11 E2)
MIN_ENTRY_PRICE = 0.15
MAX_ENTRY_PRICE = 0.85

# Confidence classification thresholds
HIGH_CONF_THRESHOLD = 0.70
MEDIUM_CONF_THRESHOLD = 0.50


class ConfidenceTier:
    """Confidence tier classification."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ─── Bayesian Belief ────────────────────────────────────────────────────────

@dataclass
class BayesianBelief:
    """
    Beta-Binomial posterior for win-probability estimation.

    Prior: Beta(α=1, β=1) — uniform.
    After observing wins/losses: Beta(α + wins, β + losses).
    Posterior mean: α / (α + β).
    """
    alpha: float = 1.0
    beta: float = 1.0

    @property
    def posterior_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, wins: int, losses: int) -> None:
        self.alpha += max(0, wins)
        self.beta += max(0, losses)

    def confidence_interval(self, z: float = 1.96) -> Tuple[float, float]:
        """
        Approximate 95% CI using the Beta distribution's mean and variance.
        """
        total = self.alpha + self.beta
        if total <= 0:
            return 0.0, 1.0
        mean = self.alpha / total
        variance = (self.alpha * self.beta) / (total * total * (total + 1))
        std = math.sqrt(variance)
        return max(0.0, mean - z * std), min(1.0, mean + z * std)


# ─── Position Sizing Config ─────────────────────────────────────────────────

@dataclass
class PositionSizingConfig:
    """
    Unified configuration for position sizing.

    When use_kelly=True, enables fee-aware Kelly criterion sizing.
    When use_kelly=False, uses simple confidence-weighted sizing.
    """
    base_size_usd: float = 10.0
    max_size_usd: float = 500.0
    min_size_usd: float = 5.0
    max_position_fraction: float = MAX_POSITION_PCT
    high_confidence_threshold: float = HIGH_CONF_THRESHOLD
    medium_confidence_threshold: float = MEDIUM_CONF_THRESHOLD
    high_multiplier: float = 1.5
    medium_multiplier: float = 1.0
    low_multiplier: float = 0.5
    signal_overrides: Optional[Dict[str, Dict[str, float]]] = None
    # Kelly-specific fields
    use_kelly: bool = True
    fraction_kelly: float = 0.5        # 50% fractional Kelly
    fee_rate: float = DEFAULT_COST_FRACTION  # 0.0205
    window_days: int = 30

    def __post_init__(self):
        if self.signal_overrides is None:
            self.signal_overrides = {}
        # Ensure fee_rate is correct
        if self.fee_rate <= 0.0:
            _LOGGER.warning(
                "fee_rate=%.4f is invalid — overriding to %.4f",
                self.fee_rate, DEFAULT_COST_FRACTION,
            )
            self.fee_rate = DEFAULT_COST_FRACTION


# ─── Kelly Position Sizer (Primary) ────────────────────────────────────────

class PositionSizer:
    """
    Kelly-criterion position sizer with adaptive confidence scaling,
    Bayesian belief updates, bankroll protection, and rolling win rate.

    Merged from kelly_position_sizer.py (Edge 13) + position_sizing.py (SH3).
    """

    def __init__(
        self,
        bankroll: float = 10000.0,
        cost_fraction: Optional[float] = None,
        fee_rate: Optional[float] = None,
        fraction_kelly: float = 0.5,
        window_days: int = 30,
    ):
        self.bankroll = bankroll
        self._peak_bankroll = bankroll
        self._belief = BayesianBelief()
        self._adaptive_multiplier: float = 0.5
        self._current_drawdown: float = 0.0
        self._fraction_kelly = fraction_kelly
        self._window_days = window_days

        # Cost fraction: prefer explicit cost_fraction, then fee_rate, then default
        if cost_fraction is not None:
            self._cost_fraction = cost_fraction
        elif fee_rate is not None:
            self._cost_fraction = fee_rate
        else:
            self._cost_fraction = DEFAULT_COST_FRACTION

        # Ensure fee_rate is non-zero for fee-aware sizing
        if self._cost_fraction <= 0.0:
            _LOGGER.warning(
                "cost_fraction=%.4f is invalid — overriding to %.4f",
                self._cost_fraction, DEFAULT_COST_FRACTION,
            )
            self._cost_fraction = DEFAULT_COST_FRACTION

        # Rolling win rate tracking (from position_sizing.py)
        self.win_history: List[dict] = []

        _LOGGER.info(
            "PositionSizer initialised — bankroll=$%.2f, cap=%.0f%%, "
            "max_drawdown=%.0f%%, fee=%.4f, f_kelly=%.2f",
            bankroll, MAX_BANKROLL_PCT * 100, MAX_DRAWDOWN_PCT * 100,
            self._cost_fraction, self._fraction_kelly,
        )

    # ─── Public API ────────────────────────────────────────────────────────

    def set_adaptive_multiplier(self, confidence: float) -> float:
        """Set adaptive Kelly multiplier based on confidence (0–1)."""
        for threshold, multiplier in CONFIDENCE_TIERS:
            if confidence >= threshold:
                self._adaptive_multiplier = multiplier
                return multiplier
        self._adaptive_multiplier = 0.5
        return 0.5

    def update_belief(self, wins: int, losses: int) -> float:
        """Bayesian Beta-Binomial update. Returns posterior mean."""
        self._belief.update(wins, losses)
        return self._belief.posterior_mean

    def update_cost(self, cost_fraction: float) -> None:
        """Update the cost fraction (spread-based)."""
        self._cost_fraction = max(0.0, min(1.0, cost_fraction))

    def calculate_kelly_fraction(self, win_rate: float) -> float:
        """
        Calculate Kelly fraction.

        f* = (p - c) / (1 - c)

        where p is win probability and c is cost fraction.
        This is the standard Kelly for binary outcomes with asymmetric payoff:
        win → get (1-c) per contract, lose → forfeit full contract value.

        Returns raw Kelly fraction (before adaptive scaling and caps).
        """
        c = self._cost_fraction
        if c >= 1.0:
            return 0.0
        kelly = (win_rate - c) / (1.0 - c)
        return max(0.0, min(1.0, kelly))

    # Fee-aware Kelly formula removed — Fix 3: use standard binary Kelly
    # calculate_kelly_fraction() is the correct formula for binary options.
    # The fee-aware variant was derived from continuous-bet formulas and
    # produced incorrect sizing for binary contracts.

    def add_win_result(self, date_string: str, win: bool) -> None:
        """Add a trade result to rolling win rate history."""
        if isinstance(date_string, str):
            dt = datetime.strptime(date_string, '%Y-%m-%d')
        else:
            dt = date_string
        if dt < datetime.now() - timedelta(days=self._window_days + 1):
            return
        self.win_history.append({
            'date': date_string,
            'win': win,
            'timestamp': dt,
        })

    def get_rolling_win_rate(self) -> float:
        """Return 30-day rolling win rate or default 0.65."""
        cutoff = datetime.now() - timedelta(days=self._window_days)
        recent = [h for h in self.win_history if h['timestamp'] >= cutoff]
        if not recent:
            return 0.65
        wins = sum(1 for r in recent if r['win'])
        return wins / len(recent) if recent else 0.65

    def calculate_edge_from_win_rate(self, win_rate: float) -> float:
        """Calculate statistical edge = 2 * win_rate - 1."""
        return 2 * win_rate - 1

    def compute_position_size(
        self,
        confidence: float,
        win_rate: float,
        edge: float,
        use_kelly: bool = True,
    ) -> Tuple[float, Dict]:
        """
        Compute the final position size in dollars.

        Pipeline:
          1. If confidence <= 0 → return 0 position
          2. Calculate raw Kelly fraction
          3. Apply adaptive multiplier based on confidence
          4. Cap at 8% of bankroll
          5. Check trailing 10% drawdown — halve size if in drawdown

        Args:
            confidence: Signal confidence (0-1)
            win_rate: Estimated win probability (0-1)
            edge: Statistical edge (2*win_rate - 1)
            use_kelly: If True, use Kelly sizing; if False, use confidence-weighted

        Returns (position_size_dollars, details_dict).
        """
        details: Dict = {}

        # Zero confidence → no trade
        if confidence <= 0.0:
            details["kelly_fraction"] = 0.0
            details["kelly_type"] = "zero_confidence"
            details["adaptive_multiplier"] = 0.0
            details["adjusted_kelly"] = 0.0
            details["max_position"] = 0.0
            details["raw_position"] = 0.0
            details["drawdown_protection"] = "inactive"
            details["final_position"] = 0.0
            details["bankroll"] = round(self.bankroll, 2)
            details["confidence"] = 0.0
            details["win_rate"] = round(win_rate, 4)
            details["edge"] = round(edge, 4)
            details["posterior_mean"] = round(self._belief.posterior_mean, 4)
            details["fraction_kelly"] = self._fraction_kelly
            return 0.0, details

        # Edge-dependent tiered sizing (Oalkhadra R11 E2)
        # Replaces uniform fractional Kelly with edge-based tiers
        EDGE_TIERS = [
            (0.10, 0.75, "75% Kelly — strong edge"),    # edge > 0.10
            (0.06, 0.50, "50% Kelly — moderate edge"),   # edge 0.06-0.10
            (0.03, 0.25, "25% Kelly — weak edge"),       # edge 0.03-0.06
        ]
        effective_kelly_multiplier = 0.0
        edge_tier_label = "NO TRADE — edge < 0.03"
        for threshold, kelly_pct, label in EDGE_TIERS:
            if edge >= threshold:
                effective_kelly_multiplier = kelly_pct
                edge_tier_label = label
                break

        details["edge_multiplier"] = effective_kelly_multiplier
        details["edge_tier_label"] = edge_tier_label

        # NO TRADE if edge < 0.03
        if effective_kelly_multiplier <= 0.0:
            details["kelly_fraction"] = 0.0
            details["kelly_type"] = "edge_too_low"
            details["adaptive_multiplier"] = 0.0
            details["adjusted_kelly"] = 0.0
            details["max_position"] = 0.0
            details["raw_position"] = 0.0
            details["drawdown_protection"] = "inactive"
            details["final_position"] = 0.0
            details["bankroll"] = round(self.bankroll, 2)
            details["confidence"] = round(confidence, 4)
            details["win_rate"] = round(win_rate, 4)
            details["edge"] = round(edge, 4)
            details["posterior_mean"] = round(self._belief.posterior_mean, 4)
            details["fraction_kelly"] = self._fraction_kelly
            return 0.0, details

        if use_kelly:
            # 1. Raw Kelly (primary formula)
            kelly = self.calculate_kelly_fraction(win_rate)
            details["kelly_fraction"] = round(kelly, 6)
            details["kelly_type"] = "p-c/1-c"

            details["kelly_final"] = round(kelly, 6)
        else:
            # Simple confidence-weighted sizing
            kelly = confidence
            details["kelly_fraction"] = round(confidence, 6)
            details["kelly_type"] = "confidence_weighted"

        # 2. Adaptive multiplier (from confidence tiers)
        multiplier = self.set_adaptive_multiplier(confidence)
        details["adaptive_multiplier"] = multiplier

        # 3. Combine edge multiplier and adaptive multiplier
        combined_multiplier = effective_kelly_multiplier * multiplier
        details["combined_multiplier"] = round(combined_multiplier, 6)

        adjusted_kelly = kelly * combined_multiplier
        details["adjusted_kelly"] = round(adjusted_kelly, 6)

        # 4. Bankroll cap (8%)
        max_position = self.bankroll * MAX_BANKROLL_PCT
        raw_position = adjusted_kelly * self.bankroll
        position = min(raw_position, max_position)
        details["max_position"] = round(max_position, 2)
        details["raw_position"] = round(raw_position, 2)

        # 4. Drawdown protection
        self._update_drawdown()
        if self._current_drawdown >= MAX_DRAWDOWN_PCT:
            position *= 0.5
            details["drawdown_protection"] = "ACTIVE — position halved"
        else:
            details["drawdown_protection"] = "inactive"

        position = max(0.0, position)
        details["final_position"] = round(position, 2)
        details["bankroll"] = round(self.bankroll, 2)
        details["confidence"] = round(confidence, 4)
        details["win_rate"] = round(win_rate, 4)
        details["edge"] = round(edge, 4)
        details["posterior_mean"] = round(self._belief.posterior_mean, 4)
        details["fraction_kelly"] = self._fraction_kelly

        return position, details

    def update_bankroll(self, new_bankroll: float) -> None:
        """Update bankroll and trailing peak for drawdown tracking."""
        self.bankroll = new_bankroll
        if new_bankroll > self._peak_bankroll:
            self._peak_bankroll = new_bankroll

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _update_drawdown(self) -> None:
        if self._peak_bankroll <= 0:
            self._current_drawdown = 0.0
            return
        self._current_drawdown = (
            (self._peak_bankroll - self.bankroll) / self._peak_bankroll
        )

    def get_belief(self) -> BayesianBelief:
        return self._belief

    def get_drawdown(self) -> float:
        return self._current_drawdown

    @staticmethod
    def validate_entry_price(price: float) -> float:
        """Clamp entry price to [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE]. Returns clamped price."""
        return max(MIN_ENTRY_PRICE, min(MAX_ENTRY_PRICE, price))

    @staticmethod
    def cap_contracts(contracts: int) -> int:
        """Cap contract count at MAX_CONTRACTS."""
        return min(contracts, MAX_CONTRACTS)


# ─── Standalone functions (from position_sizing.py) ────────────────────────

def classify_confidence(
    confidence: float,
    config: Optional[PositionSizingConfig] = None,
) -> str:
    """Classify a confidence score into a tier."""
    if config is None:
        config = PositionSizingConfig()
    if confidence >= config.high_confidence_threshold:
        return ConfidenceTier.HIGH
    elif confidence >= config.medium_confidence_threshold:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


def get_config_for_instance(instance_name: str) -> PositionSizingConfig:
    """
    Factory: return position sizing config for a given instance.

    Args:
        instance_name: "PROD", "DEV", or "SBOX"

    Returns:
        PositionSizingConfig with instance-appropriate sizing parameters
    """
    name = instance_name.upper().strip()
    if name == "PROD":
        return PositionSizingConfig(
            base_size_usd=100.0, max_size_usd=500.0, min_size_usd=25.0,
            fee_rate=DEFAULT_COST_FRACTION, fraction_kelly=0.5,
            max_position_fraction=MAX_POSITION_PCT, window_days=30,
        )
    elif name == "DEV":
        return PositionSizingConfig(
            base_size_usd=50.0, max_size_usd=250.0, min_size_usd=10.0,
            fee_rate=DEFAULT_COST_FRACTION, fraction_kelly=0.5,
            max_position_fraction=MAX_POSITION_PCT, window_days=30,
        )
    elif name == "SBOX":
        return PositionSizingConfig(
            base_size_usd=10.0, max_size_usd=50.0, min_size_usd=5.0,
            fee_rate=DEFAULT_COST_FRACTION, fraction_kelly=0.5,
            max_position_fraction=MAX_POSITION_PCT, window_days=30,
        )
    raise ValueError(f"Unknown instance: {instance_name}. Expected PROD, DEV, or SBOX.")


def compute_disagreement(
    signal_directions: list,
    signal_confidences: Optional[list] = None,
) -> float:
    """
    Compute normalized vote variance (disagreement) across ensemble signals.

    Returns disagreement in [0.0, 1.0]: 0.0 = total agreement, 1.0 = total disagreement.
    """
    if not signal_directions or len(signal_directions) < 2:
        return 0.0
    if signal_confidences is None:
        signal_confidences = [1.0] * len(signal_directions)

    numeric_dirs = []
    weights = []
    for i, d in enumerate(signal_directions):
        if isinstance(d, (int, float)):
            numeric_dirs.append(1.0 if d > 0 else -1.0)
        elif isinstance(d, str):
            numeric_dirs.append(1.0 if d.lower() == 'up' else -1.0)
        else:
            continue
        weights.append(signal_confidences[i] if i < len(signal_confidences) else 1.0)

    if len(numeric_dirs) < 2:
        return 0.0

    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    weighted_mean = sum(
        d * w for d, w in zip(numeric_dirs, weights)
    ) / total_weight
    variance = sum(
        w * (d - weighted_mean) ** 2
        for d, w in zip(numeric_dirs, weights)
    ) / total_weight
    return min(1.0, variance)


def compute_kelly_multiplier_from_disagreement(
    signal_directions: list,
    signal_confidences: Optional[list] = None,
    base_multiplier: float = 1.0,
    min_multiplier: float = 0.1,
) -> float:
    """Compute Kelly position multiplier based on ensemble disagreement."""
    disagreement = compute_disagreement(signal_directions, signal_confidences)
    return base_multiplier * max(min_multiplier, 1.0 - disagreement)


# ─── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("\n=== Position Sizer — Phase 1 Self-Test ===\n")

    sizer = PositionSizer(bankroll=10000.0)

    # 1. Kelly fraction: f* = (p - c) / (1 - c)
    k = sizer.calculate_kelly_fraction(win_rate=0.60)
    print(f"[1] Kelly fraction (win_rate=0.60, cost={DEFAULT_COST_FRACTION}) → f*={k:.4f}")
    expected = (0.60 - DEFAULT_COST_FRACTION) / (1.0 - DEFAULT_COST_FRACTION)
    assert abs(k - expected) < 1e-6, f"Expected {expected}, got {k}"
    print(f"    Expected: {expected:.4f} ✓")

    # 2. Verify fee_rate is non-zero
    print(f"[2] Fee rate: {DEFAULT_COST_FRACTION:.4f} (should be 0.0205)")
    assert DEFAULT_COST_FRACTION > 0.0, f"Fee rate should be > 0, got {DEFAULT_COST_FRACTION}"
    assert abs(DEFAULT_COST_FRACTION - 0.0205) < 0.001, (
        f"Fee rate should be ~0.0205, got {DEFAULT_COST_FRACTION}"
    )
    print("    ✓ Fee rate is 0.0205")

    # 3. Adaptive multiplier tiers
    test_cases = [
        (0.50, 0.5, "conf=50% → 0.5×"),
        (0.60, 1.0, "conf=60% → 1.0×"),
        (0.70, 1.5, "conf=70% → 1.5×"),
        (0.80, 2.0, "conf=80% → 2.0×"),
    ]
    for conf, exp, label in test_cases:
        mult = sizer.set_adaptive_multiplier(conf)
        assert mult == exp, f"Expected {exp}, got {mult} for conf={conf}"
        print(f"[3] {label} → got {mult}× ✓")

    # 4. Position size at 65% confidence, 60% win rate
    size, details = sizer.compute_position_size(
        confidence=0.65, win_rate=0.60, edge=0.10
    )
    print(f"[4] Position at conf=65%, win_rate=60%: ${size:.2f}")
    print(f"    Details: kelly={details['kelly_fraction']}, "
          f"mult={details['adaptive_multiplier']}, "
          f"final={details['final_position']}")
    # Kelly ≈ (0.60 - 0.0205) / (1 - 0.0205) ≈ 0.5913, mult=1.0, cap=$800
    assert size == 800.0, f"Expected cap $800, got ${size}"

    # 5. (Fee-aware Kelly formula removed — Fix 3: use standard binary Kelly)

    # 5. Bayesian belief update
    posterior = sizer.update_belief(wins=30, losses=20)
    print(f"[5] After 30W/20L → posterior mean={posterior:.4f}")
    assert abs(posterior - 31.0/52.0) < 1e-6, f"Expected {31/52}, got {posterior}"

    # 6. Drawdown protection
    sizer.update_bankroll(8500.0)
    size3, details3 = sizer.compute_position_size(
        confidence=0.90, win_rate=0.60, edge=0.10
    )
    print(f"[6] Position in drawdown (15%): ${size3:.2f} — "
          f"{details3['drawdown_protection']}")
    assert "ACTIVE" in details3["drawdown_protection"]

    # 7. Disagreement computation
    disc = compute_disagreement(
        ['up', 'up', 'down'],
        [1.0, 0.8, 0.6],
    )
    print(f"[7] Disagreement (2 up, 1 down): {disc:.4f}")
    assert 0 < disc < 1.0, f"Expected moderate disagreement, got {disc}"

    # 8. Instance config
    prod_cfg = get_config_for_instance("PROD")
    print(f"[8] PROD config: fee_rate={prod_cfg.fee_rate}, "
          f"fraction_kelly={prod_cfg.fraction_kelly}")
    assert prod_cfg.fee_rate == DEFAULT_COST_FRACTION
    assert prod_cfg.fraction_kelly == 0.5

    # 9. Rolling win rate
    for i in range(20):
        sizer.add_win_result(
            (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d'),
            i % 3 != 2,
        )
    wr = sizer.get_rolling_win_rate()
    print(f"[9] Rolling win rate (20 recent trades): {wr:.4f}")
    assert 0.5 < wr < 1.0, f"Expected ~0.66, got {wr}"

    print("\n✅ All Position Sizer tests passed.\n")