#!/usr/bin/env python3
"""
luck_elimination.py — Luck Elimination Protocol

Provides statistical tools to separate genuine signal skill from luck in
the weather engine backtest framework.

Three key methods:
  1. Monte Carlo null distribution — shuffle settlement dates to destroy temporal
     alignment, then re-run signal evaluation to measure what accuracy a random
     (but equally frequent) signal achieves.
  2. Block bootstrap — resample trade sequences with block size = 5 to account
     for autocorrelation, producing reliable confidence intervals.
  3. Luck-adjusted accuracy — subtract the "luck floor" (50th percentile of the
     null distribution) from observed accuracy.
  4. P-value — probability that observed (or better) accuracy arises from luck.

Usage (from big_sweep.py):
    from core.luck_elimination import (
        null_distribution,
        bootstrap_ci,
        luck_adjusted_accuracy,
        p_value,
    )

    null = null_distribution(signal_obj, settlements, n_shuffles=1000)
    adj = luck_adjusted_accuracy(raw_accuracy, null)
    p = p_value(raw_accuracy, null)
    ci_lower, ci_upper = bootstrap_ci(trades, block_size=5)
"""

import copy
import logging
import random
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

_logger = logging.getLogger(__name__)

# ── Module-level references set from big_sweep.py ──────────────────
# These are wired once by big_sweep.py so the null-distribution evaluator
# can reuse the same STATIONS list, signal evaluation helpers, and
# settle data cache without circular imports.
_STATIONS: List[str] = []
_METAR_DB: str = ""
_safe_signal_evaluate_fn = None  # function(signal_obj, idx, days) -> (direction|None, confidence)
_compute_metrics_fn = None       # function(trades, min_trades) -> dict


def wire_dependencies(
    stations: List[str],
    metar_db: str,
    safe_signal_evaluate_fn,
    compute_metrics_fn,
) -> None:
    """Inject runtime dependencies from big_sweep.py.

    Must be called once before using any luck-elimination functions.
    """
    global _STATIONS, _METAR_DB, _safe_signal_evaluate_fn, _compute_metrics_fn
    _STATIONS = stations
    _METAR_DB = metar_db
    _safe_signal_evaluate_fn = safe_signal_evaluate_fn
    _compute_metrics_fn = compute_metrics_fn


# ══════════════════════════════════════════════════════════════════
# 1. MONTE CARLO NULL DISTRIBUTION
# ══════════════════════════════════════════════════════════════════

def _shuffle_dict_values(d):
    """Shuffle values across keys while preserving keys."""
    keys = list(d.keys())
    vals = list(d.values())
    random.shuffle(vals)
    return dict(zip(keys, vals))


def _shuffle_settlements_temporally(
    settlements: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    """Shuffle settlement temperature values across dates per station.

    Destroys temporal alignment between signal predictions and actual outcomes
    while preserving the frequency of up/down movements per station.

    Returns a new settlements dict with the same station/date keys but with
    temperature values permuted within each station.
    """
    shuffled = {}
    for station, date_temps in settlements.items():
        dates = list(date_temps.keys())
        temps = list(date_temps.values())
        random.shuffle(temps)
        shuffled[station] = dict(zip(dates, temps))
    return shuffled


def _evaluate_signal_on_full_settlements(
    signal_obj,
    signal_name: str,
    settlements: Dict[str, Dict[str, float]],
    config: dict,
    data_cache_get_metar_data,
) -> List[dict]:
    """Run a signal against a (possibly shuffled) settlements dict.

    Replicates the core evaluation loop from big_sweep.py's
    evaluate_signal_on_station using the wired-in evaluation function.

    Returns a list of trade dicts.
    """
    global _safe_signal_evaluate_fn
    if _safe_signal_evaluate_fn is None:
        raise RuntimeError("luck_elimination: wire_dependencies() not called")

    if signal_obj is None:
        return []

    all_trades = []
    for station in _STATIONS:
        if station not in settlements:
            continue
        days = data_cache_get_metar_data(station)
        if len(days) < 5:
            continue
        station_s = settlements.get(station, {})
        if not station_s:
            continue

        # Determine min lookback
        min_lb = 2
        if hasattr(signal_obj, 'min_lookback') and signal_obj.min_lookback is not None:
            try:
                min_lb = max(1, int(signal_obj.min_lookback))
            except (ValueError, TypeError):
                pass

        for idx in range(min_lb, len(days)):
            date_str = days[idx]['date']
            if date_str not in station_s:
                continue
            actual_temp = station_s[date_str]
            prev_date = days[idx - 1]['date']
            if prev_date not in station_s:
                continue
            prev_temp = station_s[prev_date]
            diff = actual_temp - prev_temp
            if diff == 0:
                continue
            actual_dir = 1 if diff > 0 else -1

            pd, cf = _safe_signal_evaluate_fn(signal_obj, idx, days)
            if pd is None:
                continue

            correct = (
                1 if (pd == 'up' and actual_dir == 1) or (pd == 'down' and actual_dir == -1)
                else 0
            )
            trade = {
                "station": station,
                "date": date_str,
                "predicted": 1 if pd == 'up' else -1,
                "actual": actual_dir,
                "confidence": cf,
                "correct": bool(correct),
                "brier_contrib": (cf - float(correct)) ** 2,
                "net_pnl": 0.0,  # not used in accuracy computation
            }
            all_trades.append(trade)

    return all_trades


def null_distribution(
    signal_obj,
    signal_name: str,
    settlements: Dict[str, Dict[str, float]],
    config: dict,
    data_cache_get_metar_data,
    n_shuffles: int = 1000,
    min_trades: int = 10,
) -> List[float]:
    """Generate a null distribution of accuracies via temporal shuffling.

    For each shuffle, temporal alignment of settlement values is destroyed
    (within each station), then the signal is re-evaluated. The resulting
    accuracy represents what a random signal with the same directional
    frequency would achieve.

    Args:
        signal_obj: The signal object (instance of BaseSignal or callable).
        signal_name: Canonical signal name (for logging).
        settlements: Dict[station][date] -> temperature.
        config: Sweep config dict (used for consistency with main loop).
        data_cache_get_metar_data: Function to load metar data for a station.
        n_shuffles: Number of Monte Carlo shuffles.
        min_trades: Minimum trades required for a shuffle to count.

    Returns:
        List of float accuracies (one per shuffle), length <= n_shuffles.
    """
    global _compute_metrics_fn
    if _compute_metrics_fn is None:
        raise RuntimeError("luck_elimination: wire_dependencies() not called")

    accuracies = []
    _logger.info(
        "  LuckElim: Computing null distribution for %s (%d shuffles)...",
        signal_name, n_shuffles,
    )

    for shuffle_idx in range(n_shuffles):
        shuffled_settlements = _shuffle_settlements_temporally(settlements)
        trades = _evaluate_signal_on_full_settlements(
            signal_obj, signal_name, shuffled_settlements, config, data_cache_get_metar_data,
        )

        if len(trades) < min_trades:
            continue

        metrics = _compute_metrics_fn(trades, min_trades)
        acc = metrics.get("accuracy", 0.0)
        accuracies.append(acc)

        if (shuffle_idx + 1) % 200 == 0:
            _logger.info("    Shuffle %d/%d completed (%.1f%% acc so far)",
                          shuffle_idx + 1, n_shuffles, np.mean(accuracies) * 100)

    if not accuracies:
        _logger.warning("  LuckElim: No valid shuffles for %s", signal_name)
        return [0.0]

    _logger.info(
        "  LuckElim: Null distribution for %s — mean=%.4f, median=%.4f, "
        "std=%.4f (n=%d)",
        signal_name, np.mean(accuracies), np.median(accuracies),
        np.std(accuracies), len(accuracies),
    )
    return accuracies


# ══════════════════════════════════════════════════════════════════
# 2. BLOCK BOOTSTRAP CONFIDENCE INTERVALS
# ══════════════════════════════════════════════════════════════════

def _extract_correct_array(trades: List[dict]) -> np.ndarray:
    """Extract boolean correct/incorrect array from trades, preserving order."""
    return np.array([1 if t.get("correct", False) else 0 for t in trades], dtype=float)


def bootstrap_ci(
    trades: List[dict],
    block_size: int = 5,
    n_boot: int = 2000,
    confidence: float = 0.95,
) -> Tuple[float, float]:
    """Compute confidence interval for accuracy using block bootstrap.

    Uses moving-block bootstrap to preserve autocorrelation structure in
    the trade sequence. Block size = 5 by default, which captures typical
    serial dependence in daily weather trades.

    Args:
        trades: List of trade dicts from signal evaluation (must have 'correct' key).
        block_size: Number of consecutive trades per block.
        n_boot: Number of bootstrap resamples.
        confidence: Confidence level (0.95 -> 95% CI).

    Returns:
        (lower_bound, upper_bound) for accuracy at the requested confidence level.
    """
    if len(trades) < block_size * 2:
        _logger.warning(
            "  Bootstrap: too few trades (%d) for block_size=%d — returning full range",
            len(trades), block_size,
        )
        return (0.0, 1.0)

    corrects = _extract_correct_array(trades)
    n = len(corrects)

    # Build list of blocks (overlapping)
    blocks = []
    for i in range(0, n - block_size + 1):
        blocks.append(corrects[i:i + block_size])
    n_blocks = len(blocks)

    if n_blocks == 0:
        return (0.0, 1.0)

    # Number of blocks needed per resample to cover n trades (with overlap)
    n_blocks_per_sample = int(np.ceil(n / block_size))

    boot_means = np.zeros(n_boot)
    rng = np.random.default_rng()

    for i in range(n_boot):
        indices = rng.integers(0, n_blocks, size=n_blocks_per_sample)
        sample = np.concatenate([blocks[j] for j in indices])[:n]
        boot_means[i] = np.mean(sample)

    alpha = 1.0 - confidence
    lower = float(np.percentile(boot_means, 100 * alpha / 2.0))
    upper = float(np.percentile(boot_means, 100 * (1.0 - alpha / 2.0)))

    _logger.info(
        "  Bootstrap: mean=%.4f, CI %.0f%%=[%.4f, %.4f] (blocks=%d, n_boot=%d)",
        np.mean(boot_means), confidence * 100, lower, upper, n_blocks, n_boot,
    )
    return (lower, upper)


# ══════════════════════════════════════════════════════════════════
# 3. LUCK-ADJUSTED ACCURACY
# ══════════════════════════════════════════════════════════════════

def luck_adjusted_accuracy(
    raw_accuracy: float,
    null_distribution: List[float],
    percentile: float = 50.0,
) -> float:
    """Subtract the luck floor from observed accuracy.

    The luck floor is defined as the given percentile of the null distribution
    (default: 50th percentile / median). Subtracting it gives the accuracy
    attributable to genuine signal skill.

    A negative result means the signal performed *worse* than the median
    random signal — a strong indicator of no skill.

    Args:
        raw_accuracy: Observed accuracy of the signal (e.g., 0.62).
        null_distribution: List of accuracies from shuffled runs.
        percentile: Percentile of null to use as luck floor (default 50 = median).

    Returns:
        luck_adjusted_accuracy (may be negative).
    """
    if not null_distribution:
        _logger.warning("  LuckAdj: empty null distribution, returning raw accuracy")
        return raw_accuracy

    luck_floor = float(np.percentile(null_distribution, percentile))
    adjusted = raw_accuracy - luck_floor

    _logger.info(
        "  LuckAdj: raw=%.4f, luck_floor=%.4f (p%.0f), adjusted=%.4f",
        raw_accuracy, luck_floor, percentile, adjusted,
    )
    return adjusted


# ══════════════════════════════════════════════════════════════════
# 4. P-VALUE
# ══════════════════════════════════════════════════════════════════

def p_value(
    observed_accuracy: float,
    null_distribution: List[float],
) -> float:
    """Compute the empirical p-value for the observed accuracy.

    p = (number of null samples >= observed accuracy) / (total null samples)

    This answers: "What fraction of random (shuffled) signals achieved
    accuracy at least as high as the observed one?"

    A low p-value (<0.05) suggests the observed result is unlikely to be
    due to luck alone.

    Args:
        observed_accuracy: The signal's observed accuracy.
        null_distribution: List of accuracies from shuffled runs.

    Returns:
        p-value in [0.0, 1.0].
    """
    if not null_distribution:
        _logger.warning("  PValue: empty null distribution, returning 1.0")
        return 1.0

    null_arr = np.array(null_distribution)
    count_extreme = int(np.sum(null_arr >= observed_accuracy))
    n_total = len(null_arr)
    p = count_extreme / n_total

    _logger.info(
        "  PValue: observed=%.4f, null_samples=%d, extreme=%d, p=%.4f",
        observed_accuracy, n_total, count_extreme, p,
    )
    return p


# ══════════════════════════════════════════════════════════════════
# 5. CONVENIENCE: FULL LUCK REPORT
# ══════════════════════════════════════════════════════════════════

def run_luck_elimination(
    signal_obj,
    signal_name: str,
    settlements: Dict[str, Dict[str, float]],
    config: dict,
    data_cache_get_metar_data,
    trades: List[dict],
    n_shuffles: int = 1000,
    block_size: int = 5,
    n_boot: int = 2000,
    confidence: float = 0.95,
    min_trades_null: int = 10,
) -> Optional[dict]:
    """Run the full Luck Elimination Protocol for one signal.

    Computes null distribution, luck-adjusted accuracy, p-value, and
    bootstrap confidence intervals in one call.

    Args:
        signal_obj: The signal object.
        signal_name: Canonical signal name.
        settlements: Dict[station][date] -> temperature.
        config: Sweep config dict.
        data_cache_get_metar_data: Function to load metar data for a station.
        trades: The trade list from the actual (non-shuffled) run.
        n_shuffles: Number of Monte Carlo shuffles for null distribution.
        block_size: Block size for bootstrap.
        n_boot: Number of bootstrap resamples.
        confidence: Confidence level for bootstrap CI.
        min_trades_null: Minimum trades per shuffle to count in null.

    Returns:
        dict with luck-elimination results, or None if insufficient trades.
    """
    global _compute_metrics_fn
    if _compute_metrics_fn is None:
        raise RuntimeError("luck_elimination: wire_dependencies() not called")

    if len(trades) < min_trades_null:
        _logger.info(
            "  LuckElim: skipping %s — only %d trades (need %d)",
            signal_name, len(trades), min_trades_null,
        )
        return None

    # Observed accuracy
    metrics = _compute_metrics_fn(trades, min_trades_null)
    raw_accuracy = metrics.get("accuracy", 0.0)

    # 1. Null distribution
    null = null_distribution(
        signal_obj, signal_name, settlements, config,
        data_cache_get_metar_data, n_shuffles=n_shuffles,
        min_trades=min_trades_null,
    )

    # 2. P-value
    p_val = p_value(raw_accuracy, null)

    # 3. Luck-adjusted accuracy
    adj = luck_adjusted_accuracy(raw_accuracy, null, percentile=50.0)

    # 4. Bootstrap CI
    ci_lower, ci_upper = bootstrap_ci(trades, block_size, n_boot, confidence)

    # 5. Null distribution summary stats
    null_arr = np.array(null)
    null_mean = float(np.mean(null_arr))
    null_median = float(np.median(null_arr))
    null_std = float(np.std(null_arr))
    null_q05 = float(np.percentile(null_arr, 5))
    null_q95 = float(np.percentile(null_arr, 95))

    # 6. Percentile rank of observed accuracy in null
    pct_rank = float(np.sum(null_arr <= raw_accuracy) / len(null_arr))

    result = {
        "signal_name": signal_name,
        "n_trades": len(trades),
        "raw_accuracy": raw_accuracy,
        "luck_adjusted_accuracy": adj,
        "p_value": p_val,
        "p_value_significant": p_val < 0.05,
        "p_value_highly_significant": p_val < 0.01,
        "percentile_in_null": pct_rank,
        "bootstrap_ci_lower": ci_lower,
        "bootstrap_ci_upper": ci_upper,
        "bootstrap_confidence": confidence,
        "bootstrap_block_size": block_size,
        "null_n_shuffles": len(null),
        "null_mean": null_mean,
        "null_median": null_median,
        "null_std": null_std,
        "null_q05": null_q05,
        "null_q95": null_q95,
        "null_distribution": null,
    }

    return result


def print_luck_report(result: dict, indent: str = "  ") -> None:
    """Pretty-print a luck-elimination result dict."""
    if result is None:
        print(f"{indent}Luck Elimination: SKIPPED (insufficient trades)")
        return

    sig = result["signal_name"]
    acc = result["raw_accuracy"] * 100
    adj = result["luck_adjusted_accuracy"] * 100
    p = result["p_value"]
    ci_l = result["bootstrap_ci_lower"] * 100
    ci_u = result["bootstrap_ci_upper"] * 100
    n_null = result["null_n_shuffles"]

    print(f"{indent}Luck Elimination — {sig}:")
    print(f"{indent}  Observed accuracy:     {acc:.2f}%")
    print(f"{indent}  Luck-adjusted accuracy: {adj:+.2f}%")
    print(f"{indent}  P-value:               {p:.4f} {'✓' if p < 0.05 else ''}")
    print(f"{indent}  Bootstrap 95% CI:       [{ci_l:.2f}%, {ci_u:.2f}%]")
    print(f"{indent}  Null distribution:      n={n_null}, "
          f"mean={result['null_mean']*100:.2f}%, "
          f"median={result['null_median']*100:.2f}%, "
          f"std={result['null_std']*100:.2f}%")
    print(f"{indent}  Percentile in null:     {result['percentile_in_null']*100:.1f}%")
    if p < 0.01:
        print(f"{indent}  ★ Highly significant (p<0.01)")
    elif p < 0.05:
        print(f"{indent}  ★ Significant (p<0.05)")
    else:
        print(f"{indent}    Not significant — results consistent with luck")