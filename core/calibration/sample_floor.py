"""
B.3 — Sample Floor Rules

Smooth transition thresholds for the 4-tier hierarchical calibration.
No step-function jumps at boundaries — blend between adjacent tiers.

Provides:
  - resolve_floor(n) -> tier + blend weights (smooth, no hard cutoffs)
  - sigmoid_blend / linear_blend — smooth transition helpers
  - compute_confidence_floor(n, tier) -> confidence floor for position sizing

Re-authored 2026-09-19 (decompilation was corrupted; algorithm re-specified from
the module docstring + tier thresholds).

Deterministic math only. No AI/ML in loop.
"""

import math
from typing import Dict

TIER_THRESHOLDS = {
    'global_to_group': 5,
    'group_to_station': 20,
    'station_to_condition': 100,
}


def sigmoid_blend(n: float, threshold: float, steepness: float = 2.0) -> float:
    """
    Smooth blend weight using a sigmoid.

    At n = threshold: weight = 0.5. As n -> 0: weight -> 0 (parent).
    As n -> inf: weight -> 1 (child).
    """
    if n <= 0:
        return 0.0
    if threshold <= 0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-steepness * (n - threshold) / threshold))


def linear_blend(n: float, threshold_lo: float, threshold_hi: float) -> float:
    """Linear ramp blend between lo and hi thresholds, capped to [0, 1]."""
    if threshold_hi <= threshold_lo:
        return 1.0 if n >= threshold_lo else 0.0
    return max(0.0, min(1.0, (n - threshold_lo) / (threshold_hi - threshold_lo)))


def blend_estimates(parent: Dict, child: Dict, weight: float) -> Dict:
    """
    Blend parent and child estimate dicts by weight (weight=0 -> parent, 1 -> child).
    Blends the numeric fields ('bias', 'mae', 'rmse'); passes through 'n'.
    """
    w = max(0.0, min(1.0, weight))
    result = {}
    for key in ('bias', 'mae', 'rmse'):
        result[key] = parent.get(key, 0.0) * (1 - w) + child.get(key, 0.0) * w
    result['n'] = child.get('n', 0)
    result['weight'] = w
    return result


def resolve_floor(n: float) -> Dict:
    """
    Determine which tier a sample count falls into, WITHOUT step functions.

    Returns dict with:
      - tier: 'global' | 'group' | 'station' | 'condition'
      - blend_group_weight / blend_station_weight / blend_condition_weight (0-1)
      - blend_parent: parent tier name
    """
    n = float(n)
    g2g = TIER_THRESHOLDS['global_to_group']
    g2s = TIER_THRESHOLDS['group_to_station']
    s2c = TIER_THRESHOLDS['station_to_condition']

    # Blend weights via sigmoid transitions at each boundary.
    w_group = sigmoid_blend(n, g2g)          # 0..1, group-vs-global
    w_station = sigmoid_blend(n, g2s)        # 0..1, station-vs-group
    w_condition = sigmoid_blend(n, s2c)      # 0..1, condition-vs-station

    # Primary tier is the highest tier whose weight dominates.
    if w_condition >= 0.5:
        tier = 'condition'
        blend_parent = 'station'
    elif w_station >= 0.5:
        tier = 'station'
        blend_parent = 'group'
    elif w_group >= 0.5:
        tier = 'group'
        blend_parent = 'global'
    else:
        tier = 'global'
        blend_parent = 'global'

    return {
        'tier': tier,
        'blend_parent': blend_parent,
        'blend_group_weight': round(w_group, 4),
        'blend_station_weight': round(w_station, 4),
        'blend_condition_weight': round(w_condition, 4),
    }


def compute_confidence_floor(n_pairs: int, target_tier: str) -> float:
    """
    Confidence floor for a calibration estimate (lower bound on trust).

    Fewer pairs -> more conservative (higher floor, less trust). Returns [0, 1]
    where 1.0 = extremely confident.
    """
    if n_pairs <= 0:
        return 0.5
    base = math.sqrt(n_pairs) / (math.sqrt(n_pairs) + 2.0)
    if target_tier in ('condition', 'full_condition') and n_pairs >= 50:
        base = min(1.0, base + 0.1)
    elif target_tier == 'station' and n_pairs >= 20:
        base = min(1.0, base + 0.05)
    elif target_tier == 'global':
        base = max(base, 0.6)
    return min(1.0, max(0.0, base))


if __name__ == '__main__':
    print('B.3 — Sample Floor Rules Verification')
    print('=' * 60)
    print('\nSmooth transition around tier boundaries:')
    for n in (4, 5, 6, 7, 10, 15, 19, 20, 21, 50, 99, 100, 101, 150, 200):
        floor = resolve_floor(float(n))
        cf = compute_confidence_floor(n, floor['tier'])
        print(f"  n={n:>3}: tier={floor['tier']:<10} "
              f"group={floor['blend_group_weight']:.3f} "
              f"station={floor['blend_station_weight']:.3f} "
              f"condition={floor['blend_condition_weight']:.3f} "
              f"conf_floor={cf:.3f}")

    print('\nNo step-function verification:')
    f4, f5 = resolve_floor(4), resolve_floor(5)
    d = abs(f5['blend_group_weight'] - f4['blend_group_weight'])
    print(f"  g2g boundary (5): jump={d:.3f} {'NO' if d < 0.15 else 'YES'}")

    f19, f20 = resolve_floor(19), resolve_floor(20)
    d = abs(f20['blend_station_weight'] - f19['blend_station_weight'])
    print(f"  g2s boundary (20): jump={d:.3f} {'NO' if d < 0.15 else 'YES'}")

    f99, f100 = resolve_floor(99), resolve_floor(100)
    d = abs(f100['blend_condition_weight'] - f99['blend_condition_weight'])
    print(f"  s2c boundary (100): jump={d:.3f} {'NO' if d < 0.15 else 'YES'}")
    print('\nDone.')
