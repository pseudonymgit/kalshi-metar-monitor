#!/usr/bin/env python3
"""
Signal Configuration - Centralized thresholds and parameters for weather engine signals.

This module now re-exports configuration from core.instance_config to maintain
backward compatibility. New code should import from core.instance_config directly.
"""

# Re-export from canonical instance_config
from .instance_config import (
    DISAGREEMENT_THRESHOLD,
    ACTIVATION_THRESHOLD,
    ROUND_NUMBER_THRESHOLDS,
    ROUND_NUMBER_SINGLE_THRESHOLD,
    ROUND_NUMBER_AGGREGATE_THRESHOLD,
    DEFAULT_METAR_DB_PATH,
    DEFAULT_NWP_DB_PATH,
    DEFAULT_PAPER_TRADING_DB_PATH,
)

__all__ = [
    'DISAGREEMENT_THRESHOLD',
    'ACTIVATION_THRESHOLD',
    'ROUND_NUMBER_THRESHOLDS',
    'ROUND_NUMBER_SINGLE_THRESHOLD',
    'ROUND_NUMBER_AGGREGATE_THRESHOLD',
    'DEFAULT_METAR_DB_PATH',
    'DEFAULT_PAPER_TRADING_DB_PATH',
]