#!/usr/bin/env python3
"""
GS-C7 — Standard evaluation-artifact metrics block.

Every evaluation artifact should carry ECE, Brier, and the target-invariance
result together, so no downstream reader has to re-derive them (and no number
travels without its calibration/leak context).

Usage:
    from core.eval_metrics import artifact_metrics, brier, ece
    meta = artifact_metrics(preds, outcomes, invariance_passed=True, axis="strike_crossing")
"""
import math
from typing import List, Optional


def brier(preds: List[float], outcomes: List[int]) -> Optional[float]:
    if not preds:
        return None
    return sum((p - o) ** 2 for p, o in zip(preds, outcomes)) / len(preds)


def ece(preds: List[float], outcomes: List[int], bins: int = 10) -> Optional[float]:
    if not preds:
        return None
    n = len(preds)
    total = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [j for j, p in enumerate(preds) if (p >= lo and (p < hi or (i == bins - 1 and p <= hi)))]
        if not idx:
            continue
        conf = sum(preds[j] for j in idx) / len(idx)
        acc = sum(outcomes[j] for j in idx) / len(idx)
        total += len(idx) * abs(conf - acc)
    return total / n


def artifact_metrics(preds: List[float], outcomes: List[int],
                     invariance_passed: Optional[bool] = None,
                     axis: str = "strike_crossing") -> dict:
    """Standard metrics block to embed in every evaluation artifact."""
    return {
        "n": len(preds),
        "brier": round(brier(preds, outcomes), 4) if preds else None,
        "ece": round(ece(preds, outcomes), 4) if preds else None,
        "invariance_passed": invariance_passed,
        "label_axis": axis,  # NEVER ship a number without its axis
    }


if __name__ == "__main__":
    p = [0.9, 0.8, 0.6, 0.4, 0.55, 0.7]
    o = [1, 1, 1, 0, 1, 1]
    print(artifact_metrics(p, o, invariance_passed=True))
