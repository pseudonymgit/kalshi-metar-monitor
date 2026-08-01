#!/usr/bin/env python3
"""
WhaleWatch — Goldilocks + Ensemble Fusion

Integrates WhaleWatch anomaly signals into:
  1. Goldilocks Predictive Model (confidence boost +15-30%)
  2. Core GEFS Ensemble (modulation +0.5-2.0pp)

B-Mode compliant. No AI/ML.
"""

import json
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# === Goldilocks Fusion ===

def fuse_with_goldilocks(gpm_probability: float, whale_anomaly_strength: float) -> float:
    """
    Combine Goldilocks Predictive Model probability with WhaleWatch detection.

    GPM produces: P(Goldilocks | conditions) — probability of any spike today
    WhaleWatch produces: detected strength on a specific bucket

    Formula:
        combined = gpm_prob * (1.0 + 0.3 * whale_strength)
        Boost: +15-30% relative to GPM probability

    Args:
        gpm_probability: GPM probability (0.0-1.0)
        whale_anomaly_strength: WhaleWatch strength (0.0-1.0)

    Returns:
        Combined confidence (0.0-1.0)
    """
    if gpm_probability <= 0.0 or whale_anomaly_strength <= 0.0:
        return gpm_probability

    boost = 0.3 * whale_anomaly_strength
    return gpm_probability * (1.0 + boost)


def fuse_with_goldilocks_detailed(
    gpm_probability: float,
    whale_strength: float,
    whale_direction: str,
    bucket_temperature: int,
    current_temp_f: float,
) -> dict:
    """
    Full fusion with bucket resolution and trade decision.

    Returns dict with: combined_confidence, bucket, trade_recommendation, sizing_factor
    """
    if gpm_probability < 0.05 and whale_strength > 0.5:
        # GPM says no Goldilocks, but whale is active
        # Likely fundamental trading, not micro-spike
        trade_type = "fundamental"
        sizing_factor = 0.7
    elif gpm_probability >= 0.10 and whale_strength > 0.3:
        # Both confirm — highest confidence
        combined = gpm_probability * (1.0 + 0.3 * whale_strength)
        sizing_factor = 1.3
        trade_type = "goldilocks_confirmed"
    else:
        combined = gpm_probability
        sizing_factor = 1.0
        trade_type = "none"

    return {
        'combined_confidence': round(combined, 4),
        'bucket_temp_f': bucket_temperature,
        'current_temp_f': current_temp_f,
        'whale_strength': whale_strength,
        'sizing_factor': sizing_factor,
        'trade_type': trade_type,
    }


# === Ensemble Fusion ===

def modulate_ensemble_probability(
    base_probability: float,
    whale_anomaly_strength: float,
    whale_direction: str
) -> dict:
    """
    Apply WhaleWatch modulation to ensemble probability.

    Modulation: +0.5 to +2.0pp (percentage points), NOT ratio.

    Formula:
        modulation_pp = 0.5 + 1.5 * whale_strength
        Discount at extreme probabilities (<10% or >90%)
        Bonus +0.5pp when whale and ensemble agree

    Args:
        base_probability: Ensemble calibrated probability (0.0-1.0)
        whale_anomaly_strength: WhaleWatch strength (0.0-1.0)
        whale_direction: 'YES' or 'NO'

    Returns:
        dict with adjusted_probability, modulation_pp, edge_contribution
    """
    if whale_anomaly_strength <= 0:
        return {
            'adjusted_probability': base_probability,
            'modulation_pp': 0.0,
            'edge_contribution': 0.0,
        }

    # Base modulation
    modulation_pp = 0.5 + 1.5 * whale_anomaly_strength

    # Discount at extreme probabilities
    if base_probability > 0.90:
        modulation_pp *= 0.3
    elif base_probability < 0.10:
        modulation_pp *= 0.3

    # Bonus when whale and ensemble direction agree
    if (base_probability > 0.5 and whale_direction == "YES") or \
       (base_probability < 0.5 and whale_direction == "NO"):
        modulation_pp += 0.5

    # Cap
    modulation_pp = min(modulation_pp, 2.0)

    adjusted_probability = base_probability + (modulation_pp / 100.0)

    return {
        'adjusted_probability': round(adjusted_probability, 4),
        'modulation_pp': round(modulation_pp, 2),
        'edge_contribution': round(modulation_pp / 100.0, 4),
    }


def check_ensemble_conflict(
    ensemble_direction: str,
    whale_direction: str,
    ensemble_probability: float,
    market_price: float
) -> dict:
    """
    Check if ensemble and whale agree or conflict.

    Returns trade sizing adjustment and alert.
    """
    if whale_direction == ensemble_direction:
        return {
            'conflict': False,
            'sizing_adjustment': '+10%',
            'action': 'confirm',
        }
    elif whale_direction != ensemble_direction:
        return {
            'conflict': True,
            'sizing_adjustment': '-50%',
            'action': 'conflict',
        }
    else:
        return {
            'conflict': False,
            'sizing_adjustment': '0%',
            'action': 'neutral',
        }


if __name__ == '__main__':
    # Quick test
    print("Goldilocks fusion test:")
    r = fuse_with_goldilocks_detailed(0.20, 0.8, "YES", 84, 78)
    print(f"  GPM=20%, Whale=0.8 → {r['combined_confidence']*100:.1f}% confidence, "
          f"sizing={r['sizing_factor']}x, type={r['trade_type']}")

    print("\nEnsemble modulation test:")
    r = modulate_ensemble_probability(0.68, 0.8, "YES")
    print(f"  Base=68%, Whale=0.8 → {r['adjusted_probability']*100:.1f}% "
          f"(modulation: +{r['modulation_pp']}pp)")

    r = modulate_ensemble_probability(0.68, 0.4, "YES")
    print(f"  Base=68%, Whale=0.4 → {r['adjusted_probability']*100:.1f}% "
          f"(modulation: +{r['modulation_pp']}pp)")