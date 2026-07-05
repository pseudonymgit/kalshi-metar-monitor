#!/usr/bin/env python3
"""
Confidence-Weighted Position Sizing Module (v1.0 — 2026-07-05)

Implements confidence-weighted position sizing for all 8 signal types.
High-confidence signals → larger position size. Low-confidence → smaller.

Signal types (8 total):
  1. near_boundary_momentum_up
  2. near_boundary_momentum_down
  3. goldilocks_reversion_alert
  4. goldilocks_momentum_down
  5. reversion_after_settlement (transition)
  6. instant_up (transition)
  7. instant_down (transition)
  8. late_day_momentum_hourly

Sizing tiers:
  - HIGH confidence (≥0.70):   1.5x base size
  - MEDIUM confidence (0.50-0.69): 1.0x base size
  - LOW confidence (<0.50):    0.5x base size

All new sizing starts in DEV. Promote per PROMOTION-RULES.md.

Version: v1.0 2026-07-05
"""

import math
from typing import Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum


class ConfidenceTier(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class PositionSizingConfig:
    """Configuration for confidence-weighted position sizing."""
    base_size_usd: float = 100.0       # Base position size
    max_size_usd: float = 500.0        # Hard cap per trade
    min_size_usd: float = 25.0         # Minimum position size
    high_confidence_threshold: float = 0.70
    medium_confidence_threshold: float = 0.50
    high_multiplier: float = 1.5
    medium_multiplier: float = 1.0
    low_multiplier: float = 0.5
    # Per-signal-type overrides (optional)
    signal_overrides: Dict[str, Dict[str, float]] = None

    def __post_init__(self):
        if self.signal_overrides is None:
            self.signal_overrides = {}


# Default config for DEV instance
DEV_CONFIG = PositionSizingConfig(
    base_size_usd=50.0,     # Smaller in DEV
    max_size_usd=200.0,
    min_size_usd=10.0,
)

# Config for PROD instance (more conservative initially)
PROD_CONFIG = PositionSizingConfig(
    base_size_usd=100.0,
    max_size_usd=500.0,
    min_size_usd=25.0,
)

# Config for SBOX (sandbox — very small)
SBOX_CONFIG = PositionSizingConfig(
    base_size_usd=10.0,
    max_size_usd=50.0,
    min_size_usd=5.0,
)


def get_config_for_instance(instance: str) -> PositionSizingConfig:
    """Get position sizing config for a given instance."""
    instance = instance.upper().strip()
    if instance == "PROD":
        return PROD_CONFIG
    elif instance == "DEV":
        return DEV_CONFIG
    elif instance == "SBOX":
        return SBOX_CONFIG
    else:
        return DEV_CONFIG  # Default to DEV (safest)


def classify_confidence(confidence: float, config: PositionSizingConfig = None) -> ConfidenceTier:
    """Classify a confidence score into a tier."""
    if config is None:
        config = DEV_CONFIG
    
    if confidence >= config.high_confidence_threshold:
        return ConfidenceTier.HIGH
    elif confidence >= config.medium_confidence_threshold:
        return ConfidenceTier.MEDIUM
    else:
        return ConfidenceTier.LOW


def compute_position_size(
    signal_type: str,
    confidence: float,
    current_balance: float,
    config: PositionSizingConfig = None,
    market_price: float = 0.5,
) -> Tuple[float, ConfidenceTier, Dict[str, Any]]:
    """
    Compute confidence-weighted position size for a signal.
    
    Args:
        signal_type: One of the 8 signal types
        confidence: Confidence score 0.0-1.0
        current_balance: Current account balance in USD
        config: PositionSizingConfig (defaults to DEV)
        market_price: Current market price (0.0-1.0)
    
    Returns:
        Tuple of (position_size_usd, confidence_tier, metadata_dict)
    """
    if config is None:
        config = DEV_CONFIG
    
    # Classify confidence
    tier = classify_confidence(confidence, config)
    
    # Get multiplier for tier
    if tier == ConfidenceTier.HIGH:
        multiplier = config.high_multiplier
    elif tier == ConfidenceTier.MEDIUM:
        multiplier = config.medium_multiplier
    else:
        multiplier = config.low_multiplier
    
    # Check for per-signal override
    overrides = config.signal_overrides or {}
    signal_override = overrides.get(signal_type, {})
    if "multiplier" in signal_override:
        multiplier = signal_override["multiplier"]
    if "base_size" in signal_override:
        effective_base = signal_override["base_size"]
    else:
        effective_base = config.base_size_usd
    
    # Compute raw position size
    raw_size = effective_base * multiplier
    
    # Scale with balance (don't risk more than 10% of balance)
    balance_scaled = min(raw_size, current_balance * 0.10)
    
    # Scale with confidence more granularly (within tier)
    # This gives a smooth scaling within each tier
    if tier == ConfidenceTier.HIGH:
        # Scale from 1.0x to 1.5x within high tier
        tier_progress = (confidence - config.high_confidence_threshold) / (1.0 - config.high_confidence_threshold)
        tier_progress = max(0.0, min(1.0, tier_progress))
        granular_multiplier = 1.0 + tier_progress * 0.5
    elif tier == ConfidenceTier.MEDIUM:
        # Scale from 0.7x to 1.0x within medium tier
        tier_progress = (confidence - config.medium_confidence_threshold) / (config.high_confidence_threshold - config.medium_confidence_threshold)
        tier_progress = max(0.0, min(1.0, tier_progress))
        granular_multiplier = 0.7 + tier_progress * 0.3
    else:
        # Scale from 0.3x to 0.7x within low tier
        tier_progress = confidence / config.medium_confidence_threshold
        tier_progress = max(0.0, min(1.0, tier_progress))
        granular_multiplier = 0.3 + tier_progress * 0.4
    
    final_size = balance_scaled * granular_multiplier
    
    # Clamp to [min, max]
    final_size = max(config.min_size_usd, min(config.max_size_usd, final_size))
    
    # If balance is too low for even minimum size, return 0
    if current_balance < config.min_size_usd:
        final_size = 0.0
    
    metadata = {
        "signal_type": signal_type,
        "confidence": confidence,
        "confidence_tier": tier.value,
        "base_multiplier": multiplier,
        "granular_multiplier": granular_multiplier,
        "effective_base": effective_base,
        "balance_scaled": balance_scaled,
        "final_size": final_size,
        "config_instance": "DEV" if config is DEV_CONFIG else "PROD" if config is PROD_CONFIG else "SBOX" if config is SBOX_CONFIG else "CUSTOM",
    }
    
    return final_size, tier, metadata


# ─── Signal-specific confidence extractors ──────────────────────────────

def extract_confidence_from_signal_context(signal_context: Dict[str, Any]) -> float:
    """
    Extract confidence score from a signal context dict.
    Falls back to 0.5 if no confidence field is present.
    """
    # Direct confidence field
    confidence = signal_context.get("confidence")
    if confidence is not None:
        return float(confidence)
    
    # Infer from signal type
    signal_type = signal_context.get("signal_type", "")
    
    # For near_boundary_momentum signals, derive from momentum strength
    momentum = signal_context.get("momentum_f_per_sec")
    if momentum is not None:
        # Normalize momentum to 0-1 confidence
        return min(0.95, max(0.3, float(momentum) * 100))
    
    # For goldilocks signals with confidence_factors
    factors = signal_context.get("confidence_factors")
    if isinstance(factors, dict):
        is_daily_high = factors.get("is_daily_high", False)
        margin = float(factors.get("daily_high_margin", 0.0))
        obs_since_spike = int(factors.get("observations_since_spike", 0))
        day_fraction = float(factors.get("day_fraction_at_spike", 0.0))
        
        base = 0.4 if is_daily_high else 0.2
        bonus_margin = min(margin * 0.15, 0.2)
        bonus_obs = min(obs_since_spike * 0.02, 0.2)
        bonus_time = day_fraction * 0.2
        return min(0.9, base + bonus_margin + bonus_obs + bonus_time)
    
    # For transition signals without explicit confidence
    if signal_type in ("instant_up", "instant_down", "settlement_up"):
        return 0.6  # Default medium confidence for structural transitions
    
    if signal_type == "reversion_after_settlement":
        return 0.65  # Slightly higher — reversion has historical backing
    
    # For late_day_momentum_hourly
    if signal_type == "late_day_momentum_hourly":
        return 0.7  # Validated signal with known accuracy
    
    return 0.5  # Default neutral


# ─── CLI Test ───────────────────────────────────────────────────────────

def main():
    """Test position sizing across all 8 signal types."""
    import sys
    
    instance = sys.argv[1] if len(sys.argv) > 1 else "DEV"
    balance = float(sys.argv[2]) if len(sys.argv) > 2 else 10000.0
    
    config = get_config_for_instance(instance)
    
    print(f"\nPosition Sizing Test — Instance: {instance} — Balance: ${balance:,.2f}")
    print(f"Config: base=${config.base_size_usd}, max=${config.max_size_usd}, min=${config.min_size_usd}")
    print(f"Thresholds: HIGH≥{config.high_confidence_threshold}, MEDIUM≥{config.medium_confidence_threshold}")
    print("=" * 90)
    
    test_signals = [
        ("near_boundary_momentum_up", 0.85),
        ("near_boundary_momentum_up", 0.55),
        ("near_boundary_momentum_down", 0.75),
        ("near_boundary_momentum_down", 0.45),
        ("goldilocks_reversion_alert", 0.80),
        ("goldilocks_momentum_down", 0.65),
        ("reversion_after_settlement", 0.60),
        ("late_day_momentum_hourly", 0.70),
    ]
    
    print(f"{'Signal Type':<35} {'Conf':>5} {'Tier':<7} {'Size':>10} {'Mult':>6}")
    print("-" * 90)
    
    for signal_type, confidence in test_signals:
        size, tier, meta = compute_position_size(signal_type, confidence, balance, config)
        print(f"{signal_type:<35} {confidence:>5.2f} {tier.value:<7} ${size:>9.2f} {meta['granular_multiplier']:>5.2f}x")
    
    print()


if __name__ == "__main__":
    main()
