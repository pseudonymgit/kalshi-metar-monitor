#!/usr/bin/env python3
"""
statistical_significance.py — WS7 inferential statistics module.

Implements the QC2-passed spec (WS7-INFERENTIAL-CORRECTION-SPEC-2026-08-15):

- Descriptive: 95% Clopper-Pearson exact CI (informational only)
- Inferential: p_vs_coinflip, p_vs_baseline via scipy.stats.binomtest
- Three-condition promotion gate:
    1. p_vs_coinflip < bonferroni_threshold
    2. p_vs_baseline < bonferroni_threshold
    3. estimate > baseline (baseline = max of honest trend B, trend C, persistence)
- Fixed predeclared Bonferroni families (no runtime changes):
    - 136 pairs:  alpha' = 0.05/136
    - 78 non-degenerate: alpha' = 0.05/78
    - Adapter 4 arms: alpha' = 0.0125
    - Beam 15 candidates: alpha' = 0.05/15
- Dummy Option B: excluded, no-control limitation documented.

No CI-based significance rule. CI is descriptive only.
"""

from typing import Dict, List, Optional, Tuple

from scipy.stats import beta, binomtest

# ── Fixed predeclared families (spec I2) ──────────────────────────────
FAMILIES = {
    "all_136_pairs": {"n_tests": 136, "alpha_overall": 0.05},
    "nondegenerate_78": {"n_tests": 78, "alpha_overall": 0.05},
    "adapter_4": {"n_tests": 4, "alpha_overall": 0.05},
    "beam_15": {"n_tests": 15, "alpha_overall": 0.05},
}


def bonferroni_alpha(family: str) -> float:
    """Return the Bonferroni-corrected alpha for a fixed predeclared family."""
    if family not in FAMILIES:
        raise ValueError(f"Unknown family: {family}. Fixed families: {list(FAMILIES)}")
    return FAMILIES[family]["alpha_overall"] / FAMILIES[family]["n_tests"]


def clopper_pearson_95_ci(correct: int, trades: int) -> Tuple[float, float]:
    """Exact 95% Clopper-Pearson CI (descriptive only — NOT for significance).

    Standard formula using beta distribution:
        lo = beta.ppf(alpha/2, correct, trades - correct + 1)
        hi = beta.ppf(1 - alpha/2, correct + 1, trades - correct)
    Handles edge cases correct=0 and correct=trades.
    """
    if trades <= 0:
        return (0.0, 0.0)
    alpha = 0.05
    lo = float(beta.ppf(alpha / 2, correct, trades - correct + 1)) if correct > 0 else 0.0
    hi = float(beta.ppf(1 - alpha / 2, correct + 1, trades - correct)) if correct < trades else 1.0
    return (round(lo, 6), round(hi, 6))


def p_vs_coinflip(correct: int, trades: int) -> float:
    """Binomial p-value against H0: p=0.5 (two-sided)."""
    if trades <= 0:
        return 1.0
    return float(binomtest(correct, trades, p=0.5).pvalue)


def p_vs_baseline(correct: int, trades: int, baseline: float) -> float:
    """Binomial p-value against H0: p=baseline (two-sided)."""
    if trades <= 0:
        return 1.0
    p0 = max(0.001, min(0.999, baseline))
    return float(binomtest(correct, trades, p=p0).pvalue)


def three_condition_gate(
    correct: int,
    trades: int,
    baseline: float,
    family: str = "all_136_pairs",
    accuracy: Optional[float] = None,
) -> Dict:
    """Apply the QC2 three-condition promotion gate.

    Conditions (ALL required):
      1. p_vs_coinflip < bonferroni_threshold
      2. p_vs_baseline < bonferroni_threshold
      3. estimate > baseline (baseline = max of honest B, C, persistence)

    Returns a dict with all inferential fields.
    """
    if trades <= 0:
        return {
            "trades": 0,
            "correct": 0,
            "accuracy": accuracy if accuracy is not None else 0.0,
            "clopper_pearson_95_ci": [0.0, 0.0],
            "p_vs_coinflip": 1.0,
            "p_vs_baseline": 1.0,
            "bonferroni_threshold": bonferroni_alpha(family),
            "significant": False,
            "gate": {"cond1_p_coin": False, "cond2_p_base": False, "cond3_est_gt_base": False},
            "dummy_pairs": "excluded, not executed",
        }

    est = accuracy if accuracy is not None else (correct / trades)
    alpha_f = bonferroni_alpha(family)
    p_coin = p_vs_coinflip(correct, trades)
    p_base = p_vs_baseline(correct, trades, baseline)
    ci = clopper_pearson_95_ci(correct, trades)

    cond1 = p_coin < alpha_f
    cond2 = p_base < alpha_f
    cond3 = est > baseline

    return {
        "trades": trades,
        "correct": correct,
        "accuracy": round(est, 6),
        "clopper_pearson_95_ci": [ci[0], ci[1]],
        "p_vs_coinflip": round(p_coin, 8),
        "p_vs_baseline": round(p_base, 8),
        "bonferroni_threshold": alpha_f,
        "baseline": baseline,
        "significant": bool(cond1 and cond2 and cond3),
        "gate": {"cond1_p_coin": cond1, "cond2_p_base": cond2, "cond3_est_gt_base": cond3},
        "family": family,
        "dummy_pairs": "excluded, not executed",
    }


def baseline_for_matched_population(
    trend_b: float, trend_c: float, persistence: float
) -> float:
    """Baseline = max of honest trend B, trend C, persistence (spec I1 cond 3)."""
    return max(trend_b, trend_c, persistence)


# ── Adapter family (spec B3, I2) ──────────────────────────────────────
ADAPTER_FAMILY_COMPARISONS = [
    ("T3", "T2"),   # adapter ON (top-3) vs baseline top-3
    ("T3a", "T2"),  # adapter only (no vote-share) vs baseline
    ("T3b", "T2"),  # neither modifier vs baseline
    ("T11", "T0"),  # full-ensemble adapter vs full-ensemble baseline
]
ADAPTER_FAMILY_ALPHA = bonferroni_alpha("adapter_4")  # 0.0125


def serialize_adapter_config(
    use_direction_weighted_adapter: bool,
    use_ds_conflict: bool = True,
    use_diversity_modifier: bool = True,
) -> Dict:
    """Serialize adapter config flags per arm (spec I6)."""
    return {
        "use_direction_weighted_adapter": bool(use_direction_weighted_adapter),
        "use_ds_conflict": bool(use_ds_conflict),
        "use_diversity_modifier": bool(use_diversity_modifier),
    }


def beam_selection_family(base_pair: List[str], candidates: int) -> Dict:
    """Serialize beam selection family metadata (spec I4)."""
    return {
        "base_pair": base_pair,
        "candidates": candidates,
        "correction": "Bonferroni",
        "alpha_prime": 0.05 / candidates,
        "alpha_overall": 0.05,
    }


def mcnemar_test(b: int, c: int) -> float:
    """McNemar's exact test for paired comparison of two arms (T3 vs T2).

    H0: T3 and T2 have equal accuracy on the matched population.
    b = discordant pairs where arm T3 is correct AND arm T2 is wrong
    c = discordant pairs where arm T3 is wrong AND arm T2 is correct
    n = b + c (total discordant pairs)

    If n == 0: p = 1.0 (no evidence of difference).
    Otherwise: two-sided binomial test with p=0.5 on min(b, c) of n.

    Args:
        b: discordant pairs (arm1 correct, arm2 wrong).
        c: discordant pairs (arm1 wrong, arm2 correct).

    Returns:
        Two-sided p-value.
    """
    from scipy.stats import binomtest

    assert b >= 0 and c >= 0, f"b and c must be non-negative, got b={b}, c={c}"
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    assert k <= n, f"k ({k}) cannot exceed n ({n})"
    return float(binomtest(k, n, p=0.5).pvalue)


def paired_comparison_fields(
    b: int, c: int, n_matched: int
) -> Dict:
    """Serialize the T3-vs-T2 paired comparison fields (spec I2).

    Args:
        b: discordant pairs where T3 correct AND T2 wrong.
        c: discordant pairs where T3 wrong AND T2 correct.
        n_matched: total matched (station, date) pairs where both arms predict.

    Returns:
        Dict with n_matched, b, c, p-value, direction.
    """
    p = mcnemar_test(b, c)
    if b > c:
        direction = "T3 better"
    elif c > b:
        direction = "T2 better"
    else:
        direction = "tie"
    return {
        "n_matched": int(n_matched),
        "b": int(b),
        "c": int(c),
        "t3_vs_t2_mcnemar_pvalue": round(p, 8),
        "t3_vs_t2_direction": direction,
    }


def integer_accuracy(trades: int, correct: int) -> float:
    """Compute accuracy from integer counts only (no float accumulation).

    Args:
        trades: integer total trades (sum of per-fold integer trades).
        correct: integer total correct (sum of per-fold integer correct).

    Returns:
        accuracy = correct / trades (float, rounded to 6 decimals).
    """
    assert trades >= 0, f"trades must be non-negative, got {trades}"
    assert 0 <= correct <= trades, f"correct {correct} outside [0, {trades}]"
    if trades == 0:
        return 0.0
    return round(correct / trades, 6)


def true_mcnemar_comparison(
    arm1_predictions: List[Dict],
    arm2_predictions: List[Dict],
    arm1_name: str = "T3",
    arm2_name: str = "T2",
) -> Dict:
    """Arm-agnostic true McNemar comparison from matched (station, date) predictions.

    b = discordant pairs where arm1 is correct AND arm2 is wrong
    c = discordant pairs where arm1 is wrong AND arm2 is correct

    Handles abstentions: if a (station, date) is present in one arm's predictions
    but not the other, it's counted as abstention asymmetry. Both arms must
    have a prediction for the pair to contribute to b/c.

    Exact test: binomtest(min(b, c), b + c, p=0.5)

    Args:
        arm1_predictions: list of prediction dicts with at minimum
            'station', 'date', 'pred_dir', 'correct', 'abstained' keys.
        arm2_predictions: same for the second arm.
        arm1_name: label for the first arm (default "T3").
        arm2_name: label for the second arm (default "T2").

    Returns:
        Dict with all matched-population comparison fields.
    """
    from scipy.stats import binomtest

    # Index by (station, date)
    arm1_map = {(p["station"], p["date"]): p for p in arm1_predictions}
    arm2_map = {(p["station"], p["date"]): p for p in arm2_predictions}

    common_keys = sorted(set(arm1_map.keys()) & set(arm2_map.keys()))

    b = 0  # arm1 correct, arm2 wrong
    c = 0  # arm1 wrong, arm2 correct
    n_both_correct = 0
    n_both_wrong = 0
    n_arm1_only = len(arm1_map) - len(common_keys)
    n_arm2_only = len(arm2_map) - len(common_keys)
    n_arm1_correct_arm2_abstained = 0
    n_arm2_correct_arm1_abstained = 0

    for key in common_keys:
        p1 = arm1_map[key]
        p2 = arm2_map[key]

        p1_pred = not p1.get("abstained", False) and p1.get("pred_dir") is not None
        p2_pred = not p2.get("abstained", False) and p2.get("pred_dir") is not None

        if p1_pred and p2_pred:
            if p1.get("correct") and p2.get("correct"):
                n_both_correct += 1
            elif not p1.get("correct") and not p2.get("correct"):
                n_both_wrong += 1
            elif p1.get("correct") and not p2.get("correct"):
                b += 1
            elif not p1.get("correct") and p2.get("correct"):
                c += 1
        elif p1_pred and not p2_pred:
            if p1.get("correct"):
                n_arm1_correct_arm2_abstained += 1
        elif p2_pred and not p1_pred:
            if p2.get("correct"):
                n_arm2_correct_arm1_abstained += 1

    n_discordant = b + c
    if n_discordant == 0:
        p_value = 1.0
    else:
        k = min(b, c)
        assert k <= n_discordant, f"k ({k}) cannot exceed n ({n_discordant})"
        p_value = float(binomtest(k, n_discordant, p=0.5).pvalue)

    if b > c:
        direction = f"{arm1_name} better"
    elif c > b:
        direction = f"{arm2_name} better"
    else:
        direction = "tie"

    return {
        "arm1_name": arm1_name,
        "arm2_name": arm2_name,
        "n_matched": len(common_keys),
        "n_both_predicted": b + c + n_both_correct + n_both_wrong,
        "b": b,
        "c": c,
        "n_both_correct": n_both_correct,
        "n_both_wrong": n_both_wrong,
        "n_arm1_only": n_arm1_only,
        "n_arm2_only": n_arm2_only,
        "n_arm1_correct_arm2_abstained": n_arm1_correct_arm2_abstained,
        "n_arm2_correct_arm1_abstained": n_arm2_correct_arm1_abstained,
        "p_value": round(p_value, 8),
        "direction": direction,
    }


def assert_adapter_alpha():
    """Assert adapter family alpha cross-reference (WS7 spec)."""
    from core.statistical_significance import ADAPTER_FAMILY_ALPHA
    assert abs(ADAPTER_FAMILY_ALPHA - 0.0125) < 1e-9, f"Adapter alpha mismatch: {ADAPTER_FAMILY_ALPHA}"


def assert_beam_alpha():
    """Assert beam family alpha cross-reference (WS7 spec)."""
    from core.statistical_significance import bonferroni_alpha, FAMILIES
    assert "beam_15" in FAMILIES, "Beam family missing"
    ba = bonferroni_alpha("beam_15")
    assert abs(ba - 0.05/15) < 1e-9, f"Beam alpha mismatch: {ba}"
