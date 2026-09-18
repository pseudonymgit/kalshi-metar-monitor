"""
consensus_scoring.py — Consensus Bonus Scoring + Jaccard Convergence Detection

Replaces the scalarized objective and convergence criteria used in
per_parameter_sweep.py with two independent modules:
  1) Consensus Bonus Scoring (threshold-based, non-compensatory)
  2) Jaccard Convergence Detection (set overlap on top-5 configs)

No modifications to per_parameter_sweep.py are needed. Import this module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Part 1: Consensus Bonus Scoring ──────────────────────────────────────────

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "accuracy": 0.51,
    "sharpe": 0.2,
    "total_pnl": -500.0,  # >= -500 (was > 0 — too tight, only ~30% passed)
    "brier": 0.28,        # lower is better
    "max_drawdown": 0.6,
}

BONUS_POOL = 0.5  # total bonus pool; scaled by n_passing / TOTAL_METRICS
BASE_SCORE = 0.5
TOTAL_METRICS = 5


# Per-metric comparison operators: '>' means strict greater than, '>=' non-strict.
# Lower-is-better metrics use '<' (strict) or '<=' (non-strict).
METRIC_OPERATORS: Dict[str, str] = {
    "accuracy": ">=",
    "sharpe": ">=",
    "total_pnl": ">",
    "brier": "<",
    "max_drawdown": "<=",
}


def _passes_threshold(metric_name: str, value: float, thresholds: Dict[str, float]) -> bool:
    """
    Determine whether a single metric value passes its threshold.

    Uses strict or non-strict comparison per metric as defined in
    METRIC_OPERATORS, matching the spec exactly:
      accuracy >= 0.57, sharpe >= 0.5, total_pnl > 0,
      brier < 0.25, max_drawdown <= 0.5.
    """
    threshold = thresholds.get(metric_name, 0.0)
    op = METRIC_OPERATORS.get(metric_name, ">=")
    if op == ">=":
        return value >= threshold
    elif op == ">":
        return value > threshold
    elif op == "<=":
        return value <= threshold
    elif op == "<":
        return value < threshold
    return value >= threshold  # fallback


def consensus_bonus_score(
    metrics: Dict[str, float],
    thresholds: Optional[Dict[str, float]] = None,
) -> float:
    """Return the non-compensatory count score for the five consensus metrics."""
    thresholds = thresholds or {
        "accuracy": 0.57, "sharpe": 0.5, "total_pnl": 0.0,
        "brier": 0.25, "max_drawdown": 0.5,
    }
    n_passing = sum(
        1 for name in thresholds
        if name in metrics and _passes_threshold(name, metrics[name], thresholds)
    )
    return BASE_SCORE + BONUS_POOL * (n_passing / TOTAL_METRICS)


def compute_pareto_front(
    configs: List[Dict[str, Any]],
    metrics_keys: List[str],
) -> List[Dict[str, Any]]:
    """
    Non-dominated sort returning the Pareto-optimal front (rank 0).

    Config A dominates config B iff A >= B on ALL metrics_keys and > B on at
    least one. Standard non-dominated sort (Deb et al., NSGA-II).

    Parameters
    ----------
    configs : list of dict
        Each dict must have an 'id' field and all keys in metrics_keys.
    metrics_keys : list of str
        Metric names used for dominance comparison.
        *Lower-is-better* metrics should be prefixed with 'neg_' or handled by
        the caller negating the value. This function assumes *higher is better*
        for all keys.

    Returns
    -------
    list of dict
        The subset of configs that form the Pareto front (rank 0).
        Order is preserved from the input.

    Example
    -------
    >>> configs = [
    ...     {"id": "A", "accuracy": 0.60, "sharpe": 1.0},
    ...     {"id": "B", "accuracy": 0.40, "sharpe": 0.5},
    ...     {"id": "C", "accuracy": 0.55, "sharpe": 0.9},
    ... ]
    >>> [c["id"] for c in compute_pareto_front(configs, ["accuracy", "sharpe"])]
    ['A', 'C']  # B dominated by both A and C
    """
    dominated = [False] * len(configs)

    for i in range(len(configs)):
        for j in range(len(configs)):
            if i == j:
                continue
            # Does config j dominate config i?
            dominates = True
            strictly_better = False
            for key in metrics_keys:
                vi = configs[i].get(key, float("-inf"))
                vj = configs[j].get(key, float("-inf"))
                if vj < vi:
                    dominates = False
                    break
                if vj > vi:
                    strictly_better = True
            if dominates and strictly_better:
                dominated[i] = True
                break  # i is dominated, move on

    return [c for i, c in enumerate(configs) if not dominated[i]]


def compare_scoring_methods(data_path: str, output_path: str) -> None:
    """
    Run both consensus bonus and a representative scalarised (weighted-sum)
    scoring method on the same data, then report which configs each method
    selects and why they differ.

    Reads JSON data from *data_path* (expected format: list of dicts, each
    with 'id' and metric keys). Writes a comparison report to *output_path*
    as JSON.

    The scalarised method used for comparison is:
        weighted_sum = 0.3*accuracy + 0.2*sharpe + 0.2*total_pnl_norm
                     + 0.15*(1-brier) + 0.15*(1-max_drawdown)

    Normalisation of total_pnl is min-max over the dataset so it falls in
    [0, 1], matching the other metrics.
    """
    with open(data_path, "r") as f:
        configs: List[Dict[str, Any]] = json.load(f)

    if not configs:
        logger.warning("Empty config list in %s; writing empty report.", data_path)
        Path(output_path).write_text(json.dumps({"error": "No configs found"}, indent=2))
        return

    # -- Consensus bonus ------------------------------------------------
    consensus_scores = []
    for cfg in configs:
        metrics = {k: cfg[k] for k in DEFAULT_THRESHOLDS if k in cfg}
        score = consensus_bonus_score(metrics)
        consensus_scores.append((cfg["id"], score))
    consensus_scores.sort(key=lambda x: x[1], reverse=True)
    top_consensus_id = consensus_scores[0][0]
    top_consensus_score = consensus_scores[0][1]

    # -- Pareto front ---------------------------------------------------
    pareto_keys = [k for k in DEFAULT_THRESHOLDS if k != "total_pnl"]
    # Negate lower-is-better metrics (brier, max_drawdown) before Pareto sort
    # since compute_pareto_front assumes all metrics are higher-is-better.
    lower_is_better = {"brier", "max_drawdown"}
    pareto_front = compute_pareto_front(
        [{"id": c["id"], **{k: (-c[k] if k in lower_is_better else c[k]) for k in pareto_keys if k in c}} for c in configs],
        pareto_keys,
    )
    pareto_ids = [c["id"] for c in pareto_front]

    # -- Weighted-sum (scalarised) --------------------------------------
    total_pnl_values = [cfg.get("total_pnl", 0) for cfg in configs]
    pnl_min, pnl_max = min(total_pnl_values), max(total_pnl_values)
    pnl_range = pnl_max - pnl_min if pnl_max > pnl_min else 1.0

    scalarised_scores = []
    for cfg in configs:
        acc = cfg.get("accuracy", 0)
        shrp = cfg.get("sharpe", 0)
        pnl_norm = (cfg.get("total_pnl", 0) - pnl_min) / pnl_range
        brier = cfg.get("brier", 1)
        dd = cfg.get("max_drawdown", 1)
        ws = 0.3 * acc + 0.2 * shrp + 0.2 * pnl_norm + 0.15 * (1 - brier) + 0.15 * (1 - dd)
        scalarised_scores.append((cfg["id"], ws))
    scalarised_scores.sort(key=lambda x: x[1], reverse=True)
    top_scalarised_id = scalarised_scores[0][0]
    top_scalarised_score = scalarised_scores[0][1]

    # -- Comparison report ----------------------------------------------
    report = {
        "consensus_bonus": {
            "top_config_id": top_consensus_id,
            "top_score": top_consensus_score,
            "all_scores": dict(consensus_scores),
        },
        "scalarised_weighted_sum": {
            "top_config_id": top_scalarised_id,
            "top_score": top_scalarised_score,
            "all_scores": dict(scalarised_scores),
        },
        "pareto_front_ids": pareto_ids,
        "discrepancy": top_consensus_id != top_scalarised_id,
        "analysis": "",
    }

    if top_consensus_id == top_scalarised_id:
        report["analysis"] = (
            f"Both methods agree on config '{top_consensus_id}' as the best. "
            "Consensus bonus enforces that all five metrics pass minimum thresholds "
            "before the count-based bonus applies; the weighted-sum allows one strong "
            "metric to compensate for a weak one. When both agree, the top config "
            "likely passes all thresholds AND has high raw values."
        )
    else:
        report["analysis"] = (
            f"Disagreement: consensus picks '{top_consensus_id}', "
            f"weighted-sum picks '{top_scalarised_id}'. "
            "Consensus bonus is non-compensatory: a config that excels on 3 metrics "
            "but fails 2 thresholds (e.g. high accuracy and sharpe but bad drawdown) "
            "will rank lower than a config that barely passes all 5. "
            "The weighted-sum may over-weight a single dominant metric. "
            "Review the per-metric breakdown to decide which aligns with trading goals."
        )

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info("Comparison report written to %s", output_path)


# ── Part 2: Jaccard Convergence Detection ────────────────────────────────────


def compute_jaccard_similarity(set_a: Set[Any], set_b: Set[Any]) -> float:
    """
    Jaccard similarity = |A ∩ B| / |A ∪ B|.

    Returns 0.0 if both sets are empty (convention: empty sets are identical).
    """
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 1.0
    intersection = set_a & set_b
    return len(intersection) / len(union)


def jaccard_convergence(
    top_configs_history: List[List[str]],
    window_size: int = 50,
    jaccard_threshold: float = 0.80,
    consecutive_windows: int = 3,
) -> Tuple[bool, float]:
    """
    Detect convergence by measuring Jaccard overlap of top-5 config sets
    across consecutive windows.

    Tracks the top-5 config IDs from each window. Computes Jaccard similarity
    between consecutive windows. If overlap >= *jaccard_threshold* for
    *consecutive_windows* windows in a row, declares convergence.

    Parameters
    ----------
    top_configs_history : list of list of str
        Each element is the list of top-5 config IDs for one evaluation window.
        Assumed sorted by recency (index 0 = oldest, -1 = latest).
    window_size : int
        Number of evaluations per window (used for logging context; default 50).
    jaccard_threshold : float
        Minimum Jaccard similarity to count as "converged window pair" (default 0.80).
    consecutive_windows : int
        Number of consecutive high-overlap window pairs required (default 3;
        means 4 windows / 3 pairs).

    Returns
    -------
    (is_converged, current_jaccard_value)
        is_converged : True if convergence criterion is met.
        current_jaccard_value : The Jaccard similarity of the most recent
            window pair (or 1.0 if fewer than 2 windows).

    Examples
    --------
    >>> jaccard_convergence(
    ...     [["a","b","c","d","e"], ["a","b","c","d","e"], ["a","b","c","d","e"]],
    ...     window_size=50, jaccard_threshold=0.80, consecutive_windows=3
    ... )
    (True, 1.0)  # 3 pairs (0-1, 1-2) of 1.0 >= threshold, but need 2 pairs → True

    >>> jaccard_convergence(
    ...     [["a","b","c","d","e"], ["f","g","h","i","j"]],
    ...     jaccard_threshold=0.80, consecutive_windows=3
    ... )
    (False, 0.0)  # 1 pair only, 0.0 < 0.80
    """
    if len(top_configs_history) < 2:
        return False, 1.0

    # Convert each window's config list to a set of top-5
    window_sets: List[Set[str]] = [
        set(ids[:5]) for ids in top_configs_history if len(ids) >= 1
    ]

    if len(window_sets) < 2:
        return False, 1.0

    # Compute Jaccard for consecutive window pairs
    jaccard_values: List[float] = []
    for i in range(1, len(window_sets)):
        sim = compute_jaccard_similarity(window_sets[i - 1], window_sets[i])
        jaccard_values.append(sim)

    current_jaccard = jaccard_values[-1] if jaccard_values else 1.0

    # Count trailing windows that meet the threshold
    consecutive_count = 0
    for sim in reversed(jaccard_values):
        if sim >= jaccard_threshold:
            consecutive_count += 1
        else:
            break

    is_converged = consecutive_count >= consecutive_windows
    return is_converged, current_jaccard


# ── Improvement Suggestion ───────────────────────────────────────────────────

# Accuracy could be improved by replacing the flat 0.57 threshold with a
# *dynamic* threshold derived from the climatological baseline. For example:
#   threshold_accuracy = max(0.57, climatological_accuracy + 0.02)
# This prevents a config that barely beats a naive baseline from scoring
# the same bonus as one that meaningfully outperforms in a high-skill regime.
# The climatological baseline can be fetched from the `climatology_pillar`
# module or computed on-the-fly from the last N days of data.
# Implementation is left as an exercise for when this module is integrated.