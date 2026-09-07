"""
integration.py — LOOP pipeline hook for additive signals.

Wires the additive stack into the 6-layer strike selector
(core/strike_selector.py). Layer 3 (signal-adjusted mean) currently applies
the LIOP directional offset via `directional_to_temp_offset()`. This module
adds the additive °F offsets ON TOP of that, with:

  1. Confidence gate — offsets below min confidence are dropped.
  2. Stack caps from G.7 co-occurrence verdicts — DOUBLE_COUNT pairs only
     contribute max(|a|, |b|), not the sum; the total stack is clamped.
  3. Global magnitude clamp — additive offsets may shift the mean by at
     most MAX_TOTAL_OFFSET (7°F) in aggregate.
  4. FULLY OPT-IN — nothing changes unless the caller passes
     additive_config. Default config keeps every signal disabled; live
     enablement is an explicit, auditable act after held-out validation.

Layer accounting: the result carries `additive_offset_f` and a per-signal
breakdown so layers remain debuggable (matches selector debug-fields style).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .base import AdditiveSignalResult
from . import SIGNAL_NAMES

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

MAX_TOTAL_OFFSET_F = 7.0       # Global magnitude clamp (aggregate |offset|)
DEFAULT_MIN_CONFIDENCE = 0.3   # Per-signal conviction floor

# Co-occurrence stack caps by verdict type (from G.7 analysis)
STACK_CAP_BY_VERDICT = {
    "DOUBLE_COUNT": 0.0,        # Only the max of the two offsets applies
    "PARTIAL_OVERLAP": 0.0,     # 50% of smaller offset contributed (TRAIN: conservative)
}

# Pair verdict store — populated by G.7 cooccurrence module
_pair_store: Dict[str, "PairVerdict"] = {}


@dataclass
class AdditiveStackResult:
    """Aggregate outcome of applying the additive stack to one (station, date)."""
    total_offset_f: float = 0.0
    applied: List[AdditiveSignalResult] = field(default_factory=list)
    dropped: List[Dict[str, str]] = field(default_factory=list)
    breakdown: Dict[str, float] = field(default_factory=dict)

    def to_debug(self) -> dict:
        return {
            "total_offset_f": round(self.total_offset_f, 2),
            "n_applied": len(self.applied),
            "applied": [r.signal for r in self.applied],
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
        }


def _lookup_pair(a: str, b: str) -> Optional["PairVerdict"]:
    """Pair verdict from the G.7 matrix if built; None otherwise."""
    key = tuple(sorted([a, b]))
    return _pair_store.get(key)


def math_clamp(v: float, lo: float, hi: float) -> float:
    """Clamp v to [lo, hi]."""
    return max(lo, min(hi, v))


def apply_additive_offsets(
    results: List[AdditiveSignalResult],
    enabled: Optional[List[str]] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_total: float = MAX_TOTAL_OFFSET_F,
) -> AdditiveStackResult:
    """
    Combine additive signal results into a single net offset.

    Args:
        results: outputs of evaluate_all() for one (station, date).
        enabled: names of signals allowed to contribute. None ->
                 ENABLED_BY_DEFAULT (empty — fully disabled).
        min_confidence: per-signal conviction floor.
        max_total: aggregate |offset| clamp.

    Returns:
        AdditiveStackResult with the net offset and full audit trail.
    """
    out = AdditiveStackResult()

    # Default: no signals enabled
    if enabled is None:
        from . import ENABLED_BY_DEFAULT
        allowed = set(ENABLED_BY_DEFAULT)
    else:
        allowed = set(enabled)

    candidates: List[AdditiveSignalResult] = []

    for r in results:
        if r.signal not in allowed:
            out.dropped.append({"signal": r.signal, "reason": "not_enabled"})
            continue
        if not r.fired:
            out.dropped.append({"signal": r.signal, "reason": "did_not_fire"})
            continue
        if r.confidence < min_confidence:
            out.dropped.append({
                "signal": r.signal,
                "reason": f"low_confidence ({r.confidence:.2f} < {min_confidence:.2f})",
            })
            continue
        candidates.append(r)

    if not candidates:
        return out

    # Apply G.7 co-occurrence caps for pair interactions
    applied_names: List[str] = []
    totals: Dict[str, float] = {}
    cap_hit: Dict[str, Dict[str, float]] = {}

    for r in candidates:
        total_before = sum(abs(totals.get(n, 0.0)) for n in totals)
        if total_before >= max_total:
            out.dropped.append({"signal": r.signal, "reason": "max_total_reached"})
            continue

        # Check co-occurrence caps with already-applied signals
        net_offset = r.offset_f
        for other in applied_names:
            pv = _lookup_pair(r.signal, other)
            if pv is not None and pv.verdict in STACK_CAP_BY_VERDICT:
                cap = STACK_CAP_BY_VERDICT[pv.verdict]
                combined = abs(net_offset) + abs(totals.get(other, 0.0))
                if abs(net_offset) > cap and abs(totals.get(other, 0.0)) > cap:
                    net_offset = math_clamp(net_offset, -cap, cap)

        if net_offset != r.offset_f:
            cap_hit[r.signal] = {"original": r.offset_f, "capped_to": net_offset}

        applied_names.append(r.signal)
        totals[r.signal] = net_offset
        out.applied.append(r)

    # Compute net total
    net = sum(totals.values())
    clamped = math_clamp(net, -max_total, max_total)

    if abs(clamped) != abs(net):
        out.breakdown["global_clamp_applied"] = True
        out.breakdown["pre_clamp"] = round(net, 2)

    out.total_offset_f = round(clamped, 2)
    out.breakdown["signals"] = {k: round(v, 2) for k, v in totals.items()}

    return out