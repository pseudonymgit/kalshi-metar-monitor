#!/usr/bin/env python3
"""
Phase 1 Build Configuration — Default Parameter Set

Wires the Phase 2 LHS sweep best config as the default production signal
pipeline parameters. The agreement gate, signal pipeline, and paper trading
loop all read from this module.

Phase 2 Best Config (from data/sweep/phase2_summary.json):
  decay_factor=0.802, min_agreement=4, min_conf=0.7962, sig_window=17,
  strike_offset=16.0, train_days=318, test_days=31

Best Metrics:
  73.03% directional accuracy, 0.54 Sharpe, 2,306 trades, 2.89 profit factor

Usage:
    from core.phase1_config import PHASE1_CONFIG
    n_req = PHASE1_CONFIG["min_agreement"]
    decay = PHASE1_CONFIG["decay_factor"]

Override via env vars:
    PHASE1_MIN_AGREE=5 python3 scripts/phase1_paper_trading_cron.py
"""

import os
from typing import Dict, Any

# ─── Phase 2 Best Config ─────────────────────────────────────────────────────

PHASE1_CONFIG: Dict[str, Any] = {
    # Agreement gate parameters
    "min_agreement": 4,           # N-of-M: at least N signals must agree
    "min_conf": 0.7962,           # Minimum confidence threshold
    "sig_window": 17,             # Signal window in hours/minutes

    # Decay and strike parameters
    "decay_factor": 0.802,        # Historical decay factor
    "strike_offset": 16.0,        # Bucket strike offset (°F)

    # Training / test split
    "train_days": 318,            # Training window (days)
    "test_days": 31,              # Test / forward window (days)

    # Position sizing
    "fee_rate": 0.0205,           # Kalshi round-trip fee (market_cost_model)
    "max_bankroll_pct": 0.08,     # 8% max per position (Kelly sizer cap)
    "fractional_kelly": 0.5,      # Half-Kelly for robustness
    "max_position_pct": 0.25,     # 25% max of balance (legacy cap)

    # Spread builder defaults
    "default_spread": "medium",   # Spread type: single, narrow, medium, wide
    "min_net_credit": 0.10,       # Minimum net credit to accept a spread

    # Paper trading DB
    "trade_db": "data/phase1_paper_trades.db",
    "log_file": "data/phase1_paper_trading.log",
}

# ─── Environment override support ────────────────────────────────────────────

_ENV_OVERRIDES: Dict[str, str] = {
    "PHASE1_MIN_AGREE": "min_agreement",
    "PHASE1_MIN_CONF": "min_conf",
    "PHASE1_SIG_WINDOW": "sig_window",
    "PHASE1_DECAY": "decay_factor",
    "PHASE1_STRIKE_OFFSET": "strike_offset",
    "PHASE1_TRAIN_DAYS": "train_days",
    "PHASE1_TEST_DAYS": "test_days",
    "PHASE1_FEE_RATE": "fee_rate",
    "PHASE1_FRACTIONAL_KELLY": "fractional_kelly",
}


def _apply_env_overrides() -> None:
    """Apply environment variable overrides to PHASE1_CONFIG."""
    for env_key, config_key in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                # Parse as float first, then int if no decimal
                parsed = float(val)
                if parsed == int(parsed) and config_key not in (
                    "fee_rate", "fractional_kelly", "decay_factor",
                    "min_conf", "strike_offset",
                ):
                    parsed = int(parsed)
                PHASE1_CONFIG[config_key] = parsed
            except (ValueError, TypeError):
                pass  # Silently ignore invalid env overrides


# Apply overrides at import time
_apply_env_overrides()


# ─── Aliases for backward compatibility ──────────────────────────────────────

def get_min_agreement() -> int:
    """Return the minimum number of signals that must agree."""
    return int(PHASE1_CONFIG["min_agreement"])


def get_min_confidence() -> float:
    """Return the minimum confidence threshold."""
    return float(PHASE1_CONFIG["min_conf"])


def get_decay_factor() -> float:
    """Return the decay factor for historical weighting."""
    return float(PHASE1_CONFIG["decay_factor"])


def get_strike_offset() -> float:
    """Return the strike offset (°F)."""
    return float(PHASE1_CONFIG["strike_offset"])


def get_fee_rate() -> float:
    """Return the round-trip fee rate."""
    return float(PHASE1_CONFIG["fee_rate"])


# ─── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Phase 1 Configuration ===")
    for key, val in PHASE1_CONFIG.items():
        print(f"  {key}: {val}")
    print(f"\n  min_agreement(): {get_min_agreement()}")
    print(f"  fee_rate(): {get_fee_rate()}")
    print(f"  decay_factor(): {get_decay_factor()}")
    print(f"  strike_offset(): {get_strike_offset()}")
    print("\n✅ Phase 1 config loaded.")