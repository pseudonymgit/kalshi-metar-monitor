#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Dewpoint Depression Modulator — Confidence Modifier Based on Cloud Cover

DPD = temperature - dewpoint

Cloud cover inference from DPD:
  - DPD > 15°F → clear skies, multiply confidence by 1.2 (cap at 1.0)
  - DPD < 5°F  → cloudy/overcast, multiply confidence by 0.8
  - 5°F ≤ DPD ≤ 15°F → no adjustment

Applied to ALL z-score signals' confidence values after computation.

No extra data needed — dewpoint already in METAR observations.

Usage:
    from core.dewpoint_modulator import modify_confidence_by_dewpoint
    adjusted_conf = modify_confidence_by_dewpoint(confidence, temp, dewpoint)
"""

import logging
from typing import Optional

_logger = logging.getLogger(__name__)

# ─── Thresholds ─────────────────────────────────────────────────────────────

CLEAR_SKY_DPD_THRESHOLD = 15.0   # °F: DPD above this = clear skies
CLOUDY_DPD_THRESHOLD = 5.0       # °F: DPD below this = cloudy/overcast
CLEAR_SKY_MULTIPLIER = 1.2       # Multiply confidence by this for clear skies
CLOUDY_MULTIPLIER = 0.8          # Multiply confidence by this for cloudy skies
MAX_CONFIDENCE = 1.0             # Hard cap after modulation


def modify_confidence_by_dewpoint(
    confidence: float,
    temperature: Optional[float],
    dewpoint: Optional[float],
) -> float:
    """
    Adjust signal confidence based on dewpoint depression (DPD).

    DPD = temperature - dewpoint

    - DPD > 15°F: clear skies, boost confidence (1.2x, capped at 1.0)
    - DPD < 5°F:  cloudy/overcast, reduce confidence (0.8x)
    - Otherwise: keep confidence unchanged

    Args:
        confidence: Raw signal confidence (0.0-1.0)
        temperature: Current temperature in °F
        dewpoint: Current dewpoint in °F

    Returns:
        Adjusted confidence (0.0-1.0)
    """
    if temperature is None or dewpoint is None:
        return confidence

    dpd = temperature - dewpoint

    if dpd > CLEAR_SKY_DPD_THRESHOLD:
        adjusted = min(confidence * CLEAR_SKY_MULTIPLIER, MAX_CONFIDENCE)
        _logger.debug(
            f"DewpointModulator: DPD={dpd:.1f}°F (clear skies), "
            f"confidence {confidence:.3f} -> {adjusted:.3f}"
        )
        return adjusted

    elif dpd < CLOUDY_DPD_THRESHOLD:
        adjusted = confidence * CLOUDY_MULTIPLIER
        _logger.debug(
            f"DewpointModulator: DPD={dpd:.1f}°F (cloudy/overcast), "
            f"confidence {confidence:.3f} -> {adjusted:.3f}"
        )
        return adjusted

    else:
        # 5°F ≤ DPD ≤ 15°F: no adjustment
        return confidence


def compute_dewpoint_depression(
    temperature: Optional[float],
    dewpoint: Optional[float],
) -> Optional[float]:
    """
    Compute dewpoint depression (DPD = temperature - dewpoint).

    Args:
        temperature: Temperature in °F
        dewpoint: Dewpoint in °F

    Returns:
        DPD in °F, or None if either value is missing
    """
    if temperature is None or dewpoint is None:
        return None
    return temperature - dewpoint


def describe_cloud_cover(
    temperature: Optional[float],
    dewpoint: Optional[float],
) -> str:
    """
    Describe cloud cover based on dewpoint depression.

    Args:
        temperature: Temperature in °F
        dewpoint: Dewpoint in °F

    Returns:
        Description string: 'clear_skies', 'cloudy_overcast', or 'unknown'
    """
    dpd = compute_dewpoint_depression(temperature, dewpoint)
    if dpd is None:
        return "unknown"
    if dpd > CLEAR_SKY_DPD_THRESHOLD:
        return "clear_skies"
    if dpd < CLOUDY_DPD_THRESHOLD:
        return "cloudy_overcast"
    return "mixed"