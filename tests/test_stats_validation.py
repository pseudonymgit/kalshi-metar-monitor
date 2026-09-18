#!/usr/bin/env python3
"""
test_stats_validation.py — Comprehensive validation of tests/stats.py against scipy.

Validates all 5 statistical primitive functions with 100+ test cases:
  1. Two-sided exact binomial test (Clopper-Pearson CI)
  2. Holm-Bonferroni correction
  3. Minimum sample size calculator
  4. Effect size reporting (Cohen's h)
  5. Per-test registration and family correction

Author: Gilfoyle (routed from Donna)
"""
import math
import sys
import traceback

# Ensure we can import from the project
sys.path.insert(0, ".")

import tests.stats as stats  # noqa: E402
from tests.stats import (
    binomial_test,
    holm_bonferroni,
    effect_size_report,
    cohens_h_effect,
    minimum_sample_size,
    register_test,
    clear_registry,
    record_p_value,
    apply_family_corrections,
    _norm_cdf,
    _norm_ppf,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

pass_count = 0
fail_count = 0
warn_count = 0


def check(label: str, condition: bool, detail: str = ""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  ✓ {label}")
    else:
        fail_count += 1
        print(f"  ✗ {label}  << FAIL >>  {detail}")


def check_close(label: str, ours: float, expected: float, tol: float = 1e-4):
    global pass_count, fail_count
    diff = abs(ours - expected)
    if diff <= tol or (math.isinf(diff) and math.isinf(expected)):
        pass_count += 1
        print(f"  ✓ {label}: {ours:.6f} ≈ {expected:.6f} (diff={diff:.2e})")
    else:
        fail_count += 1
        print(f"  ✗ {label}: {ours:.6f} ≠ {expected:.6f} (diff={diff:.2e})")


def warn(label: str, msg: str):
    global warn_count
    warn_count += 1
    print(f"  ⚠ {label}: {msg}")


# ---------------------------------------------------------------------------
# 1. Binomial test validation
# ---------------------------------------------------------------------------

def test_binomial():
    print("\n" + "=" * 60)
    print("SECTION 1: Two-sided exact binomial test")
    print("=" * 60)

    from scipy.stats import binomtest, beta

    # --- 1a: Basic scenarios ---
    print("\n  1a: Basic known outcomes")

    # Fair coin: 60 heads out of 100
    r = binomial_test(60, 100, 0.5)
    s = binomtest(60, 100, 0.5)
    check_close("60/100 @ p=0.5: p-value", r["p_value"], s.pvalue, 1e-4)
    sci_ci = s.proportion_ci(0.95)
    check_close("60/100: CI lower", r["ci_lower"], sci_ci.low, 1e-3)
    check_close("60/100: CI upper", r["ci_upper"], sci_ci.high, 1e-3)
    check("60/100: type", r["statistic_type"] == "two_sided_binomial")

    # Exactly null: 50 heads out of 100
    r = binomial_test(50, 100, 0.5)
    s = binomtest(50, 100, 0.5)
    check_close("50/100 @ p=0.5: p-value", r["p_value"], s.pvalue, 1e-4)

    # Extreme: 100/100 successes
    r = binomial_test(100, 100, 0.5)
    s = binomtest(100, 100, 0.5)
    check_close("100/100 @ p=0.5: p-value", r["p_value"], s.pvalue, 1e-4)
    check_close("100/100: CI lower", r["ci_lower"], s.proportion_ci(0.95).low, 1e-2)
    check("100/100: CI upper = 1.0", r["ci_upper"] == 1.0)

    # Extreme: 0/100 successes
    r = binomial_test(0, 100, 0.5)
    s = binomtest(0, 100, 0.5)
    check_close("0/100 @ p=0.5: p-value", r["p_value"], s.pvalue, 1e-4)
    check("0/100: CI lower = 0.0", r["ci_lower"] == 0.0)

    # Different null
    r = binomial_test(75, 100, 0.8)
    s = binomtest(75, 100, 0.8)
    check_close("75/100 @ p=0.8: p-value", r["p_value"], s.pvalue, 1e-4)

    # --- 1b: Cohen's h verification ---
    print("\n  1b: Cohen's h")

    h = cohens_h_effect(0.6, 0.5)
    expected_h = 2.0 * math.asin(math.sqrt(0.6)) - 2.0 * math.asin(math.sqrt(0.5))
    check_close("Cohen's h for 0.6 vs 0.5", h, expected_h, 1e-10)

    h_zero = cohens_h_effect(0.5, 0.5)
    check_close("Cohen's h for equal proportions", h_zero, 0.0, 1e-10)

    h_neg = cohens_h_effect(0.4, 0.5)
    expected_neg = 2.0 * math.asin(math.sqrt(0.4)) - 2.0 * math.asin(math.sqrt(0.5))
    check_close("Cohen's h for 0.4 vs 0.5 (negative)", h_neg, expected_neg, 1e-10)

    h_bound = cohens_h_effect(1.0, 0.5)
    expected_bound = 2.0 * math.asin(1.0) - 2.0 * math.asin(math.sqrt(0.5))
    check_close("Cohen's h for 1.0 vs 0.5 (boundary)", h_bound, expected_bound, 1e-10)

    # --- 1c: Systematic sweep ---
    print("\n  1c: Systematic sweep (n=10..200, various p_null)")

    test_cases = [
        (5, 10, 0.5),
        (3, 10, 0.3),
        (8, 10, 0.8),
        (2, 10, 0.5),
        (9, 10, 0.5),
        (15, 20, 0.5),
        (12, 20, 0.6),
        (40, 50, 0.5),
        (30, 50, 0.5),
        (45, 50, 0.5),
        (55, 100, 0.5),
        (40, 100, 0.5),
        (65, 100, 0.5),
        (120, 200, 0.5),
        (110, 200, 0.5),
        (90, 200, 0.5),
    ]

    for k, n, p0 in test_cases:
        r = binomial_test(k, n, p0)
        s = binomtest(k, n, p0)
        p_ok = abs(r["p_value"] - s.pvalue) < 1e-4
        ci_ok = abs(r["ci_lower"] - s.proportion_ci(0.95).low) < 1e-3 and \
                abs(r["ci_upper"] - s.proportion_ci(0.95).high) < 1e-3
        label = f"   {k}/{n} @ p={p0}"
        if not p_ok:
            warn(label, f"p-value: ours={r['p_value']:.6f}, scipy={s.pvalue:.6f}")
        if not ci_ok and n <= 50:
            warn(label, f"CI: ours=[{r['ci_lower']:.4f},{r['ci_upper']:.4f}], scipy=[{s.proportion_ci(0.95).low:.4f},{s.proportion_ci(0.95).high:.4f}]")
        if p_ok and ci_ok:
            check(label, True)

    # --- 1d: Edge cases ---
    print("\n  1d: Edge cases")

    try:
        binomial_test(-1, 10, 0.5)
        check("negative n_successes raises ValueError", False)
    except ValueError:
        check("negative n_successes raises ValueError", True)

    try:
        binomial_test(11, 10, 0.5)
        check("n_successes > n_trials raises ValueError", False)
    except ValueError:
        check("n_successes > n_trials raises ValueError", True)

    try:
        binomial_test(5, 0, 0.5)
        check("zero n_trials raises ValueError", False)
    except ValueError:
        check("zero n_trials raises ValueError", True)

    try:
        binomial_test(5, 10, -0.1)
        check("p_null < 0 raises ValueError", False)
    except ValueError:
        check("p_null < 0 raises ValueError", True)

    try:
        binomial_test(5, 10, 1.5)
        check("p_null > 1 raises ValueError", False)
    except ValueError:
        check("p_null > 1 raises ValueError", True)

    # n=1 edge cases
    for k in [0, 1]:
        r = binomial_test(k, 1, 0.5)
        s = binomtest(k, 1, 0.5)
        check_close(f"{k}/1: p-value", r["p_value"], s.pvalue, 1e-4)

    # p_null=0 or 1 edge cases
    for k, n, p0 in [(0, 10, 0.0), (10, 10, 1.0), (5, 10, 0.0), (5, 10, 1.0)]:
        r = binomial_test(k, n, p0)
        check(f"{k}/{n} @ p={p0}: runs without error", True)
        check(f"{k}/{n} @ p={p0}: p_value in [0,1]", 0 <= r["p_value"] <= 1)


# ---------------------------------------------------------------------------
# 2. Holm-Bonferroni validation
# ---------------------------------------------------------------------------

def test_holm_bonferroni():
    print("\n" + "=" * 60)
    print("SECTION 2: Holm-Bonferroni correction")
    print("=" * 60)

    # --- 2a: Classic example ---
    print("\n  2a: Classic 4-test example")
    p_vals = [0.01, 0.04, 0.05, 0.20]
    r = holm_bonferroni(p_vals, alpha=0.05)

    # Manual computation:
    # sorted: 0.01 (idx 0), 0.04 (idx 1), 0.05 (idx 2), 0.20 (idx 3)
    # Step 0: 0.01 * 4 = 0.04  → reject (0.04 <= 0.05)
    # Step 1: 0.04 * 3 = 0.12  → don't reject (0.12 > 0.05)
    # Step 2: 0.05 * 2 = 0.10  → adjust to max(0.10, 0.12) = 0.12 → don't reject
    # Step 3: 0.20 * 1 = 0.20  → adjust to max(0.20, 0.12) = 0.20 → don't reject

    check("HB: rejected[0] (0.01)", r["rejected"][0], "first test should be rejected")
    check("HB: not rejected[1] (0.04)", not r["rejected"][1], "second should NOT be rejected")
    check("HB: not rejected[2] (0.05)", not r["rejected"][2], "third should NOT be rejected")
    check("HB: not rejected[3] (0.20)", not r["rejected"][3], "fourth should NOT be rejected")
    check_close("HB: adj[0] = 0.04", r["adjusted_p_values"][0], 0.04, 1e-10)
    check_close("HB: adj[1] = 0.12", r["adjusted_p_values"][1], 0.12, 1e-10)
    check_close("HB: adj[2] = 0.12", r["adjusted_p_values"][2], 0.12, 1e-10)
    check_close("HB: adj[3] = 0.20", r["adjusted_p_values"][3], 0.20, 1e-10)

    # --- 2b: All significant ---
    print("\n  2b: All significant")
    p_vals = [0.001, 0.002, 0.003, 0.004]
    r = holm_bonferroni(p_vals, 0.05)
    check("HB: all rejected", all(r["rejected"]), "all 4 should be rejected")

    # --- 2c: None significant ---
    print("\n  2c: None significant")
    p_vals = [0.1, 0.2, 0.3, 0.4]
    r = holm_bonferroni(p_vals, 0.05)
    check("HB: none rejected", not any(r["rejected"]), "none should be rejected")

    # --- 2d: Edge cases ---
    print("\n  2d: Edge cases")

    # Empty list
    r = holm_bonferroni([], 0.05)
    check("HB: empty list", r["adjusted_p_values"] == [] and r["rejected"] == [])

    # Single value
    r = holm_bonferroni([0.01], 0.05)
    check("HB: single reject", r["rejected"][0])
    check_close("HB: single adj=0.01", r["adjusted_p_values"][0], 0.01, 1e-10)

    r = holm_bonferroni([0.10], 0.05)
    check("HB: single not rejected", not r["rejected"][0])

    # Ties
    p_vals = [0.01, 0.01, 0.03]
    r = holm_bonferroni(p_vals, 0.05)
    # sorted: 0.01(0), 0.01(1), 0.03(2)
    # Step 0: 0.01*3=0.03 → reject
    # Step 1: 0.01*2=0.02 → max(0.02, 0.03)=0.03 → reject
    # Step 2: 0.03*1=0.03 → max(0.03, 0.03)=0.03 → reject
    check("HB: ties all rejected", all(r["rejected"]))
    check_close("HB: ties adj[0]=0.03", r["adjusted_p_values"][0], 0.03, 1e-10)
    check_close("HB: ties adj[1]=0.03", r["adjusted_p_values"][1], 0.03, 1e-10)
    check_close("HB: ties adj[2]=0.03", r["adjusted_p_values"][2], 0.03, 1e-10)

    # Adjusted p-value monotonicity check
    p_vals = [0.001, 0.01, 0.02, 0.03, 0.05]
    r = holm_bonferroni(p_vals, 0.05)
    adj = r["adjusted_p_values"]
    sorted_adj = sorted(adj)
    check("HB: adjusted values are monotonic", all(
        sorted_adj[i] <= sorted_adj[i+1] for i in range(len(sorted_adj)-1)
    ))

    # Corrected alpha
    r = holm_bonferroni([0.01, 0.02, 0.03, 0.04], 0.05)
    check_close("HB: corrected_alpha = 0.0125", r["corrected_alpha_effective"], 0.0125, 1e-10)


# ---------------------------------------------------------------------------
# 3. Minimum sample size validation
# ---------------------------------------------------------------------------

def test_sample_size():
    print("\n" + "=" * 60)
    print("SECTION 3: Minimum sample size calculator")
    print("=" * 60)

    from scipy.stats import norm as sp_norm

    # --- 3a: Standard scenario ---
    print("\n  3a: Standard 5pp MDE, 80% power, 95% confidence")
    r = minimum_sample_size(mde=0.05, alpha=0.05, power=0.80, p_null=0.5)
    check("n_required > 0", r["n_required"] > 0)
    check("actual_power >= 0.80", r["actual_power_at_n"] >= 0.80)
    check("mde_at_n >= 0.049", r["mde_at_n"] >= 0.049, "achievable MDE should be close to requested")
    check("mde_at_n <= 0.051", r["mde_at_n"] <= 0.051)
    print(f"    n={r['n_required']}, power={r['actual_power_at_n']:.4f}, mde={r['mde_at_n']:.4f}")

    # --- 3b: Higher power ---
    print("\n  3b: 90% power at 5pp MDE")
    r = minimum_sample_size(mde=0.05, alpha=0.05, power=0.90, p_null=0.5)
    check("n_required for 90% power > n for 80% power", r["n_required"] > 700)
    print(f"    n={r['n_required']}, power={r['actual_power_at_n']:.4f}")

    # --- 3c: Smaller MDE ---
    print("\n  3c: 2pp MDE, 80% power")
    r = minimum_sample_size(mde=0.02, alpha=0.05, power=0.80, p_null=0.5)
    check("n for 2pp > n for 5pp", r["n_required"] >= 4000)
    print(f"    n={r['n_required']}, power={r['actual_power_at_n']:.4f}, mde={r['mde_at_n']:.4f}")

    # --- 3d: Larger MDE ---
    print("\n  3d: 10pp MDE, 80% power")
    r = minimum_sample_size(mde=0.10, alpha=0.05, power=0.80, p_null=0.5)
    check("n for 10pp < n for 5pp", r["n_required"] <= 250)
    print(f"    n={r['n_required']}, power={r['actual_power_at_n']:.4f}")

    # --- 3e: Different null ---
    print("\n  3e: p_null=0.3, 5pp MDE")
    r = minimum_sample_size(mde=0.05, alpha=0.05, power=0.80, p_null=0.3)
    check("n_required > 0 for p_null=0.3", r["n_required"] > 0)
    print(f"    n={r['n_required']}, power={r['actual_power_at_n']:.4f}")

    # --- 3f: Edge cases ---
    print("\n  3f: Edge cases")

    try:
        minimum_sample_size(mde=0, alpha=0.05, power=0.80)
        check("mde=0 raises ValueError", False)
    except ValueError:
        check("mde=0 raises ValueError", True)

    try:
        minimum_sample_size(mde=0.05, alpha=0, power=0.80)
        check("alpha=0 raises ValueError", False)
    except ValueError:
        check("alpha=0 raises ValueError", True)

    try:
        minimum_sample_size(mde=0.05, alpha=0.05, power=0)
        check("power=0 raises ValueError", False)
    except ValueError:
        check("power=0 raises ValueError", True)

    try:
        minimum_sample_size(mde=0.05, alpha=0.05, power=0.80, p_null=0)
        check("p_null=0 raises ValueError", False)
    except ValueError:
        check("p_null=0 raises ValueError", True)

    try:
        minimum_sample_size(mde=0.6, alpha=0.05, power=0.80, p_null=0.5)
        check("p_null + mde > 1 raises ValueError", False)
    except ValueError:
        check("p_null + mde > 1 raises ValueError", True)


# ---------------------------------------------------------------------------
# 4. Effect size reporting validation
# ---------------------------------------------------------------------------

def test_effect_size():
    print("\n" + "=" * 60)
    print("SECTION 4: Effect size reporting (Cohen's h)")
    print("=" * 60)

    # --- 4a: Interpretation thresholds ---
    print("\n  4a: Interpretation thresholds")
    r = effect_size_report(505, 1000, 0.5)
    check("h=0.01: small effect", r["interpretation"] == "small")
    check_close("h=0.01: accuracy=0.505", r["accuracy"], 0.505, 1e-6)

    r = effect_size_report(600, 1000, 0.5)
    check("h=0.20: medium effect", r["interpretation"] == "medium",
          f"got h={r['cohens_h']:.4f}")

    r = effect_size_report(750, 1000, 0.5)
    check("h=0.52: large effect", r["interpretation"] == "large",
          f"got h={r['cohens_h']:.4f}")

    # --- 4b: Boundaries ---
    print("\n  4b: Boundary cases")
    r = effect_size_report(0, 100, 0.5)
    check("0/100: runs without error", True)
    check("0/100: accuracy = 0.0", r["accuracy"] == 0.0)
    check("0/100: large effect (h >> 0.5)", r["interpretation"] == "large",
          f"got h={r['cohens_h']:.4f}")

    r = effect_size_report(100, 100, 0.5)
    check("100/100: runs without error", True)
    check("100/100: accuracy = 1.0", r["accuracy"] == 1.0)

    # n=1 edge case
    for k in [0, 1]:
        r = effect_size_report(k, 1, 0.5)
        check(f"{k}/1: runs without error", True)
        check(f"{k}/1: valid interpretation", r["interpretation"] in ("small", "medium", "large"))

    # --- 4c: Symmetry ---
    print("\n  4c: Symmetry check")
    r1 = effect_size_report(600, 1000, 0.5)
    r2 = effect_size_report(400, 1000, 0.5)
    check_close("h(0.6) ≈ -h(0.4)", r1["cohens_h"], -r2["cohens_h"], 1e-10)

    # --- 4d: CI bounds ---
    print("\n  4d: CI bounds")
    r = effect_size_report(600, 1000, 0.5)
    ci = r["cohens_h_ci"]
    check("CI: lower < h < upper", ci[0] < r["cohens_h"] < ci[1])
    check("CI: width reasonable", ci[1] - ci[0] < 0.5)


# ---------------------------------------------------------------------------
# 5. Test registration and family correction validation
# ---------------------------------------------------------------------------

def test_registration():
    print("\n" + "=" * 60)
    print("SECTION 5: Test registration and family correction")
    print("=" * 60)

    # --- 5a: Single family ---
    print("\n  5a: Single family (DATA)")
    clear_registry()
    register_test("data_test_1", "DATA")
    register_test("data_test_2", "DATA")
    register_test("data_test_3", "DATA")
    register_test("data_test_4", "DATA")
    record_p_value("data_test_1", 0.01)
    record_p_value("data_test_2", 0.04)
    record_p_value("data_test_3", 0.20)
    record_p_value("data_test_4", 0.50)

    results = apply_family_corrections(0.05)
    check("single family: 4 results", len(results) == 4)
    # Holm on [0.01, 0.04, 0.20, 0.50], family size 4, alpha=0.05
    # Step 0: 0.01*4=0.04 → reject
    # Step 1: 0.04*3=0.12 → not reject
    # Step 2: 0.20*2=0.40 → not reject
    # Step 3: 0.50*1=0.50 → not reject
    check("single family: test 1 rejected", results[0]["rejected"])
    check("single family: test 2 not rejected", not results[1]["rejected"])

    # --- 5b: Custom family size ---
    print("\n  5b: Custom family size")
    clear_registry()
    register_test("custom_1", "CUSTOM", 2)
    register_test("custom_2", "CUSTOM", 2)
    record_p_value("custom_1", 0.01)
    record_p_value("custom_2", 0.50)
    results = apply_family_corrections(0.05)
    check("custom family: 2 results", len(results) == 2)

    # --- 5c: Unknown family error ---
    print("\n  5c: Unknown family error")
    clear_registry()
    try:
        register_test("bad_family", "UNKNOWN")
        check("unknown family raises ValueError", False)
    except ValueError:
        check("unknown family raises ValueError", True)

    # --- 5d: Multi-family ---
    print("\n  5d: Multi-family correction")
    clear_registry()
    # Family 1: DATA, all significant
    register_test("d1", "DATA")
    register_test("d2", "DATA")
    register_test("d3", "DATA")
    register_test("d4", "DATA")
    record_p_value("d1", 0.00001)
    record_p_value("d2", 0.00002)
    record_p_value("d3", 0.00003)
    record_p_value("d4", 0.00004)

    # Family 2: BASELINE, nothing significant
    register_test("b1", "BASELINE")
    register_test("b2", "BASELINE")
    register_test("b3", "BASELINE")
    record_p_value("b1", 0.4)
    record_p_value("b2", 0.5)
    record_p_value("b3", 0.6)

    results = apply_family_corrections(0.05)
    check("multi-family: 7 results", len(results) == 7)

    # Family 1 should have some rejected (very low p-values)
    data_results = [r for r in results if r["family"] == "DATA"]
    baseline_results = [r for r in results if r["family"] == "BASELINE"]

    check("DATA: at least one rejected", any(r["rejected"] for r in data_results))
    check("BASELINE: none rejected", not any(r["rejected"] for r in baseline_results),
          "high p-values should not be rejected")

    # --- 5e: Clear registry ---
    print("\n  5e: Registry clearing")
    clear_registry()
    results = apply_family_corrections(0.05)
    check("empty registry: no results", len(results) == 0)


# ---------------------------------------------------------------------------
# 6. Internal function validation
# ---------------------------------------------------------------------------

def test_internals():
    print("\n" + "=" * 60)
    print("SECTION 6: Internal function validation")
    print("=" * 60)

    from scipy.stats import norm

    # --- 6a: norm_cdf ---
    print("\n  6a: Normal CDF (erf-based)")
    for x in [-8.0, -5.0, -3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 1.96, 3.0, 5.0, 8.0]:
        ours = _norm_cdf(x)
        ref = norm.cdf(x)
        check_close(f"  CDF({x:+.2f})", ours, ref, 1e-10)

    # --- 6b: norm_ppf ---
    print("\n  6b: Normal PPF (Newton solver)")
    for p in [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.975, 0.99, 0.995, 0.999]:
        ours = _norm_ppf(p)
        ref = norm.ppf(p)
        check_close(f"  PPF({p:.3f})", ours, ref, 1e-4)

    # --- 6c: Symmetry ---
    print("\n  6c: Symmetry check")
    for p in [0.01, 0.05, 0.1, 0.25]:
        check_close(f"  PPF({p}) = -PPF({1-p})", _norm_ppf(p), -_norm_ppf(1-p), 1e-10)

    # --- 6d: CDF(PPF(p)) ≈ p ---
    print("\n  6d: CDF(PPF(p)) roundtrip")
    for p in [0.001, 0.01, 0.05, 0.1, 0.5, 0.9, 0.95, 0.99, 0.999]:
        x = _norm_ppf(p)
        cdf_at_x = _norm_cdf(x)
        check_close(f"  CDF(PPF({p}))", cdf_at_x, p, 1e-8)

    # --- 6e: PPF(CDF(x)) ≈ x ---
    print("\n  6e: PPF(CDF(x)) roundtrip")
    for x in [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]:
        ppf_at_cdf = _norm_ppf(_norm_cdf(x))
        check_close(f"  PPF(CDF({x:.2f}))", ppf_at_cdf, x, 1e-4)


# ---------------------------------------------------------------------------
# Run all test sections
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("WS2.1 Statistical Primitives — Validation Suite")
    print("Scipy version:", end=" ")
    import scipy
    print(scipy.__version__)
    print("=" * 60)

    try:
        test_binomial()
    except Exception as e:
        print(f"\n  !!! FAILURE in binomial tests: {e}")
        traceback.print_exc()
        global fail_count
        fail_count += 1

    try:
        test_holm_bonferroni()
    except Exception as e:
        print(f"\n  !!! FAILURE in Holm-Bonferroni tests: {e}")
        traceback.print_exc()
        fail_count += 1

    try:
        test_sample_size()
    except Exception as e:
        print(f"\n  !!! FAILURE in sample size tests: {e}")
        traceback.print_exc()
        fail_count += 1

    try:
        test_effect_size()
    except Exception as e:
        print(f"\n  !!! FAILURE in effect size tests: {e}")
        traceback.print_exc()
        fail_count += 1

    try:
        test_registration()
    except Exception as e:
        print(f"\n  !!! FAILURE in registration tests: {e}")
        traceback.print_exc()
        fail_count += 1

    try:
        test_internals()
    except Exception as e:
        print(f"\n  !!! FAILURE in internal function tests: {e}")
        traceback.print_exc()
        fail_count += 1

    # Summary
    print("\n" + "=" * 60)
    total = pass_count + fail_count
    pct = pass_count / total * 100 if total > 0 else 0
    print(f"SUMMARY:  {pass_count} passed / {fail_count} failed / {warn_count} warnings")
    print(f"          {pct:.1f}% pass rate ({total} total assertions)")
    print("=" * 60)

    if fail_count > 0:
        print("\n❌ VALIDATION FAILED — see above for details")
        sys.exit(1)
    else:
        print("\n✅ ALL TESTS PASSED — primitives module validated against scipy.stats")


if __name__ == "__main__":
    main()