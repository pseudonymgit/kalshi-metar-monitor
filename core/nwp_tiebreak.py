#!/usr/bin/env python3
"""
Deterministic NWP ensemble tie-breaker (signal-level).

Problem
-------
When the 4 core NWP models (GFS, ECMWF, ICON, GEM) split 2-2 on direction,
the existing vote-count logic returns NEUTRAL (no signal). That drops the
station out of coverage for the day, even though the models collectively
still carry directional information.

Rule (chosen after evidence review — see WE-KSAT-TIEBREAK receipt)
------------------------------------------------------------------
  Magnitude-weighted directional lean.

  Each model's vote is its forecast DELTA (forecast[target] - forecast[prev]).
  Weights are inverse-MAE (better historical models get more weight). The
  tie direction = sign of the weighted sum of deltas.

  This uses the *size* of each model's disagreement, which vote-counting
  throws away. Empirically, on 2-2 days it beats every fixed "pick a model"
  rule (see receipt for numbers).

Determinism
-----------
  - No randomness.
  - If the weighted delta is exactly 0.0 (true dead heat), fall back to a
    fixed, documented model-priority order: GFS > ECMWF > ICON > GEM.
  - Same inputs -> same output, always.

Confidence honesty
------------------
  A tie is, by construction, the weakest-agreement case. The measured edge
  on 2-2 days is modest (~59%), so the tie-break confidence is capped LOW
  (TIEBREAK_MAX_CONF). We do NOT inflate confidence to force a direction.
  The caller keeps its own confidence for non-tie cases.
"""

from typing import Dict, List, Optional, Tuple

# Core models, and the deterministic fallback priority when the weighted
# delta is a perfect 0.0. Order = preference.
MODEL_PRIORITY: List[str] = ['gfs', 'ecmwf', 'icon', 'gem']

# Inverse-MAE weights (from multi_model_ensemble.MODEL_MAE:
# ecmwf 2.1, gfs 2.4, icon 2.6, gem 2.8). Computed lazily below.
_MODEL_MAE: Dict[str, float] = {
    'gfs': 2.4,
    'ecmwf': 2.1,
    'icon': 2.6,
    'gem': 2.8,
}

# Lowest confidence we will attach to a resolved tie. Kept low to avoid
# fabricating certainty: a 2-2 split is the weakest-agreement state.
TIEBREAK_MIN_CONF = 0.52
# Highest confidence we will attach to a resolved tie.
TIEBREAK_MAX_CONF = 0.58


def inverse_mae_weights(models: Optional[List[str]] = None) -> Dict[str, float]:
    """Return normalized inverse-MAE weights for the given (or core) models."""
    models = list(models) if models else list(MODEL_PRIORITY)
    raw = {m: 1.0 / (_MODEL_MAE.get(m, 3.0) + 0.1) for m in models}
    total = sum(raw.values())
    if total <= 0:
        n = len(models) or 1
        return {m: 1.0 / n for m in models}
    return {m: w / total for m, w in raw.items()}


def resolve_tie(deltas: Dict[str, Optional[float]]) -> Tuple[Optional[str], float, str]:
    """
    Resolve a directional tie using the magnitude-weighted lean.

    Args:
        deltas: per-model forecast deltas (target - prev). Models with
                None/missing are ignored.

    Returns:
        (direction, confidence, reason)
        direction: 'up' | 'down' | None (None only if no usable model)
        confidence: capped low (TIEBREAK_MIN_CONF..TIEBREAK_MAX_CONF)
        reason: short machine tag describing which branch fired
    """
    usable = {m: d for m, d in deltas.items()
              if m in MODEL_PRIORITY and d is not None}
    if not usable:
        return None, 0.0, 'no_usable_models'

    weights = inverse_mae_weights(list(usable.keys()))
    weighted = sum(weights[m] * usable[m] for m in usable)

    if weighted > 0:
        direction, reason = 'up', 'weighted_delta'
    elif weighted < 0:
        direction, reason = 'down', 'weighted_delta'
    else:
        # Perfect dead heat -> deterministic fixed priority.
        direction, reason = None, 'dead_heat'
        for m in MODEL_PRIORITY:
            d = usable.get(m)
            if d is not None and d != 0:
                direction = 'up' if d > 0 else 'down'
                reason = f'priority_fallback:{m}'
                break
        if direction is None:
            # Every usable delta is exactly 0 -> no directional information.
            return None, 0.0, 'all_zero_delta'

    # Confidence scales with how strongly the weighted lean points, but is
    # capped low because ties are the weakest case. We never claim >MAX.
    total_abs = sum(abs(d) for d in usable.values())
    if total_abs > 0:
        lean_ratio = abs(weighted) / total_abs   # in [0,1]
    else:
        lean_ratio = 0.0
    conf = TIEBREAK_MIN_CONF + (TIEBREAK_MAX_CONF - TIEBREAK_MIN_CONF) * lean_ratio
    return direction, conf, reason
