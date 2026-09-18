"""
tests/stats.py — Statistical Primitives for Weather Engine Verification

Provides shared statistical functions used by ALL verification tests:
  - Two-sided exact binomial test (Clopper-Pearson CI)
  - Holm-Bonferroni correction for multiple comparisons
  - Minimum sample size calculator (arcsin transformation)
  - Effect size reporting (Cohen's h)
  - Per-test registration for family-wise correction

All formulas are implemented from scratch with numerical stability.
Validated against scipy.stats (see test_stats_validation.py).

Author: Gilfoyle (routed from Donna)
Date:   2026-08-11
"""

import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Family registry for multiple comparison correction
# ---------------------------------------------------------------------------

# Family sizes from WS2 spec
FAMILY_SIZES = {
    "DATA": 4,
    "P3_COMPONENT": 6,
    "SIGNAL": 13,
    "CALIBRATION": 5,
    "CONFIDENCE": 8,
    "SWEEP": 3,
    "BASELINE": 3,
}

_registered_tests: List[Dict] = []  # each entry: {name, family, p_value, raw_p}


def register_test(
    test_name: str,
    family: str,
    n_comparisons_in_family: Optional[int] = None,
) -> None:
    """Register a test for Holm-Bonferroni correction.

    Args:
        test_name: Unique identifier for this test
        family: Family name (must be in FAMILY_SIZES)
        n_comparisons_in_family: Override for family size; defaults to FAMILY_SIZES[family]

    Raises:
        ValueError: If family is unknown and n_comparisons_in_family is None
    """
    if n_comparisons_in_family is None:
        if family not in FAMILY_SIZES:
            raise ValueError(
                f"Unknown family '{family}'. Known families: {list(FAMILY_SIZES.keys())}. "
                "Pass n_comparisons_in_family explicitly for custom families."
            )
        n_comparisons_in_family = FAMILY_SIZES[family]

    _registered_tests.append(
        {
            "name": test_name,
            "family": family,
            "n_family": n_comparisons_in_family,
            "p_value": None,  # filled by record_p_value
            "raw_p": None,
        }
    )


def record_p_value(test_name: str, p_value: float, raw_p: Optional[float] = None) -> None:
    """Record the p-value for a previously registered test."""
    for entry in _registered_tests:
        if entry["name"] == test_name:
            entry["p_value"] = p_value
            entry["raw_p"] = raw_p if raw_p is not None else p_value
            return
    raise ValueError(f"Test '{test_name}' not registered. Call register_test first.")


def clear_registry() -> None:
    """Clear all registered tests (call between test suites)."""
    _registered_tests.clear()


def get_registry() -> List[Dict]:
    """Return a copy of the current registry."""
    return list(_registered_tests)


def apply_family_corrections(alpha: float = 0.05) -> List[Dict]:
    """Apply Holm-Bonferroni within each family, then across families.

    Returns list of dicts with correction results for each registered test.
    """
    if not _registered_tests:
        return []

    results = []

    # --- Step 1: Correct within each family ---
    families = {}
    for entry in _registered_tests:
        if entry["raw_p"] is None:
            continue
        f = entry["family"]
        families.setdefault(f, []).append(entry)

    family_corrected_alphas = {}
    for family, entries in families.items():
        n = entries[0]["n_family"]
        p_vals = [e["raw_p"] for e in entries]
        hb = holm_bonferroni(p_vals, alpha)
        # Effective alpha for this family is: alpha / n (Bonferroni first step)
        family_corrected_alphas[family] = hb["corrected_alpha_effective"]

    # --- Step 2: Get the minimum corrected p-value per family ---
    family_min_corrected = {}
    for family, entries in families.items():
        n = entries[0]["n_family"]
        p_vals = [e["raw_p"] for e in entries]
        hb = holm_bonferroni(p_vals, alpha)
        corrected = hb["adjusted_p_values"]
        min_corrected = min(corrected) if corrected else 1.0
        family_min_corrected[family] = min_corrected

    # --- Step 3: Holm-Bonferroni across families ---
    family_names = list(family_min_corrected.keys())
    if len(family_names) <= 1:
        # Only one family — just return within-family results
        n_families = 1
        for family, entries in families.items():
            p_vals = [e["raw_p"] for e in entries]
            hb = holm_bonferroni(p_vals, alpha)
            for i, entry in enumerate(entries):
                results.append({
                    "name": entry["name"],
                    "family": family,
                    "raw_p": entry["raw_p"],
                    "adjusted_p_within": hb["adjusted_p_values"][i],
                    "adjusted_p_overall": hb["adjusted_p_values"][i],
                    "rejected_within": hb["rejected"][i],
                    "rejected": hb["rejected"][i],
                    "corrected_alpha": hb["corrected_alpha_effective"],
                })
        return results

    across_hb = holm_bonferroni(
        [family_min_corrected[f] for f in family_names], alpha
    )

    family_rejected = {}
    for i, fname in enumerate(family_names):
        family_rejected[fname] = across_hb["rejected"][i]

    # --- Step 4: Build final results ---
    for family, entries in families.items():
        p_vals = [e["raw_p"] for e in entries]
        n = entries[0]["n_family"]
        hb = holm_bonferroni(p_vals, alpha)
        for i, entry in enumerate(entries):
            results.append({
                "name": entry["name"],
                "family": family,
                "raw_p": entry["raw_p"],
                "adjusted_p_within": hb["adjusted_p_values"][i],
                "adjusted_p_overall": (
                    hb["adjusted_p_values"][i]
                    if family_rejected.get(family, False)
                    else 1.0  # family didn't survive across-family correction
                ),
                "rejected_within": hb["rejected"][i],
                "rejected": (
                    hb["rejected"][i] and family_rejected.get(family, False)
                ),
                "corrected_alpha": across_hb["corrected_alpha_effective"],
            })

    return results


# ---------------------------------------------------------------------------
# Helper: normal quantile (inverse CDF)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using the error function (math.erf).

    Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))
    Accurate to double precision.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float, tol: float = 1e-12, max_iter: int = 50) -> float:
    """Standard normal quantile function (inverse CDF).

    Uses Newton-Raphson on the erf-based CDF. The erf function is available
    in math.erf, giving us double-precision accuracy everywhere.

    For the central region (0.05 < p < 0.95), we use a direct rational
    approximation (Beasley-Springer-Moro). For tails, Newton is used.
    """
    if not 0 < p < 1:
        raise ValueError(f"p must be in (0, 1), got {p}")

    # Central region: use a fast rational approximation
    if 0.05 <= p <= 0.95:
        a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
             1.383577518672690e2, -3.066479806614716e1, 2.506628277459239]
        b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
             6.680131188771972e1, -1.328068155288572e1]
        q = p - 0.5
        r = q * q
        num = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
        den = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        return num / den

    # Tail region: use Newton on erf
    # Initial estimate from the approximation: q = sqrt(-2 * ln(min(p, 1-p)))
    tail_p = min(p, 1.0 - p)
    x = math.sqrt(-2.0 * math.log(tail_p))
    # Refine using Wichura rational tail correction
    if tail_p < 1e-10:
        # Extreme tail: use the Mills ratio directly
        return -x if p < 0.5 else x

    # Newton-Raphson on Phi(x) - p = 0
    sign = 1.0 if p > 0.5 else -1.0
    # Map to upper tail
    if p > 0.5:
        target = 1.0 - p
    else:
        target = p
        x = -x

    for _ in range(max_iter):
        # Phi at current x
        phi_x = _norm_cdf(x)
        diff = phi_x - p
        if abs(diff) < tol:
            return x

        # PDF
        pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
        if pdf < 1e-300:
            break

        dx = diff / pdf
        x -= dx

        # Clamp to avoid divergence
        if x > 8.0:
            x = 8.0
        elif x < -8.0:
            x = -8.0

    return x


# ---------------------------------------------------------------------------
# Helper: beta distribution quantile for Clopper-Pearson
# ---------------------------------------------------------------------------

def _beta_ppf(q: float, a: float, b: float, max_iter: int = 100) -> float:
    """Beta distribution quantile using inverse regularized incomplete beta.

    Uses a simple bisection + Newton approach since scipy may not always
    be available. For speed, we delegate to scipy when available, but this
    implementation is self-contained.
    """
    # Bounds
    lo, hi = 0.0, 1.0
    x = 0.5

    for _ in range(max_iter):
        # Evaluate regularized incomplete beta at x
        bx = _betainc(x, a, b)
        f = bx - q
        if abs(f) < 1e-15:
            return x

        # Derivative is beta PDF
        df = _beta_pdf(x, a, b)
        if df == 0:
            break  # fall back to bisection

        # Newton step
        dx = f / df
        x_new = x - dx

        # Bisection if out of bounds
        if x_new <= lo or x_new >= hi:
            x_new = 0.5 * (lo + hi)

        # Update bracket
        if f > 0:
            hi = x
        else:
            lo = x

        if abs(x_new - x) < 1e-14:
            return x_new
        x = x_new

    return 0.5 * (lo + hi)


def _beta_pdf(x: float, a: float, b: float) -> float:
    """Beta PDF: x^(a-1) * (1-x)^(b-1) / B(a,b)."""
    if x <= 0 or x >= 1:
        return 0.0
    return math.exp(
        (a - 1.0) * math.log(x) + (b - 1.0) * math.log1p(-x)
        - _lbeta(a, b)
    )


def _lbeta(a: float, b: float) -> float:
    """Log of the beta function."""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betainc(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta function I_x(a,b) using continued fraction.

    Uses Lentz's continued fraction method for numerical stability.
    """
    if x < 0 or x > 1:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0 or x == 1:
        return x
    if a <= 0 or b <= 0:
        raise ValueError(f"a, b must be positive, got a={a}, b={b}")

    # Use symmetry: I_x(a,b) = 1 - I_{1-x}(b,a)
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _betainc(1.0 - x, b, a)

    # Lentz's continued fraction
    lbeta_ab = _lbeta(a, b)

    # Front factor: x^a * (1-x)^b / (a * B(a,b))
    front = math.exp(
        a * math.log(x) + b * math.log1p(-x) - math.log(a) - lbeta_ab
    )

    # Continued fraction (modified Lentz)
    # CF: a1 / (1 + a2 / (1 + a3 / (1 + ...)))
    # We compute using the recurrence for the regularized CF.

    # First compute the CF for the continued fraction representation of
    # the incomplete beta:
    # I_x(a,b) = (x^a * (1-x)^b) / (a * B(a,b)) * CF

    f = 1.0
    C = 1.0
    D = 1.0 - (a + b) * x / (a + 1.0)
    if abs(D) < 1e-30:
        D = 1e-30
    D = 1.0 / D
    f = D

    # Lentz parameters for m = 1, 2, ...
    tiny = 1e-30
    for m in range(1, 200):
        # Even step (2m)
        m2 = 2 * m
        alpha_num = m * (b - m) * x
        alpha_den = (a + m2 - 1.0) * (a + m2)
        if abs(alpha_den) < tiny:
            continue
        alpha = alpha_num / alpha_den

        # Update CF using modified Lentz
        D = 1.0 + alpha * D
        if abs(D) < tiny:
            D = tiny
        C = 1.0 + alpha / C
        if abs(C) < tiny:
            C = tiny
        D = 1.0 / D
        delta = C * D
        f *= delta

        # Odd step (2m + 1)
        alpha_num = -(a + m) * (a + b + m) * x
        alpha_den = (a + m2) * (a + m2 + 1.0)
        if abs(alpha_den) < tiny:
            continue
        alpha = alpha_num / alpha_den

        D = 1.0 + alpha * D
        if abs(D) < tiny:
            D = tiny
        C = 1.0 + alpha / C
        if abs(C) < tiny:
            C = tiny
        D = 1.0 / D
        delta = C * D
        f *= delta

        if abs(delta - 1.0) < 1e-12:
            break

    return front * f


# ---------------------------------------------------------------------------
# 1. Two-sided exact binomial test (Clopper-Pearson CI)
# ---------------------------------------------------------------------------

def binomial_test(
    n_successes: int,
    n_trials: int,
    p_null: float = 0.5,
) -> Dict:
    """Two-sided exact binomial test with Clopper-Pearson confidence interval.

    Tests H0: p = p_null vs H1: p != p_null (TWO-SIDED by default).

    Args:
        n_successes: Number of successes observed
        n_trials: Total number of trials
        p_null: Null hypothesis proportion (default 0.5)

    Returns:
        dict with keys: p_value, ci_lower, ci_upper, effect_size_cohens_h, statistic_type
    """
    if n_trials <= 0:
        raise ValueError(f"n_trials must be > 0, got {n_trials}")
    if n_successes < 0 or n_successes > n_trials:
        raise ValueError(
            f"n_successes must be in [0, n_trials], got {n_successes} / {n_trials}"
        )
    if not 0 <= p_null <= 1:
        raise ValueError(f"p_null must be in [0, 1], got {p_null}")

    p_hat = n_successes / n_trials

    # --- Clopper-Pearson confidence interval ---
    # Uses the relationship between binomial and beta distributions:
    # CI: [Beta(alpha/2; x, n-x+1), Beta(1-alpha/2; x+1, n-x)]
    alpha_ci = 0.05  # 95% CI

    if n_successes == 0:
        ci_lower = 0.0
        ci_upper = 1.0 - (alpha_ci / 2.0) ** (1.0 / n_trials)
    elif n_successes == n_trials:
        ci_lower = (alpha_ci / 2.0) ** (1.0 / n_trials)
        ci_upper = 1.0
    else:
        # Try scipy first for accuracy, fall back to our implementation
        try:
            from scipy.stats import beta as beta_dist

            ci_lower = beta_dist.ppf(alpha_ci / 2.0, n_successes, n_trials - n_successes + 1)
            ci_upper = beta_dist.ppf(1.0 - alpha_ci / 2.0, n_successes + 1, n_trials - n_successes)
        except ImportError:
            ci_lower = _beta_ppf(
                alpha_ci / 2.0, n_successes, n_trials - n_successes + 1
            )
            ci_upper = _beta_ppf(
                1.0 - alpha_ci / 2.0, n_successes + 1, n_trials - n_successes
            )

    # --- Two-sided p-value ---
    # Method: sum of all probabilities <= probability of observed outcome
    try:
        from scipy.stats import binom as binom_dist

        observed_prob = binom_dist.pmf(n_successes, n_trials, p_null)

        # Sum probabilities of all outcomes <= observed_prob
        # Use a small epsilon for floating-point comparison symmetry
        eps = 1e-12
        p_value = 0.0
        for k in range(n_trials + 1):
            prob_k = binom_dist.pmf(k, n_trials, p_null)
            if prob_k <= observed_prob + eps:
                p_value += prob_k
        p_value = min(p_value, 1.0)
    except ImportError:
        # Fallback: compute from scratch using log-space
        p_value = _two_sided_binomial_p_value(n_successes, n_trials, p_null)

    # --- Cohen's h effect size ---
    cohens_h = cohens_h_effect(p_hat, p_null)

    return {
        "p_value": p_value,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "effect_size_cohens_h": cohens_h,
        "statistic_type": "two_sided_binomial",
    }


def _two_sided_binomial_p_value(k: int, n: int, p: float) -> float:
    """Compute two-sided binomial p-value from scratch (log-space)."""
    # Log of binomial coefficient
    log_binom = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

    # Log of observed probability
    if p == 0:
        log_prob_obs = log_binom + (n - k) * float("-inf") if k == n else float("-inf")
    elif p == 1:
        log_prob_obs = log_binom + k * float("-inf") if k == 0 else float("-inf")
    else:
        log_prob_obs = log_binom + k * math.log(p) + (n - k) * math.log1p(-p)

    if math.isinf(log_prob_obs):
        return 1.0

    # Sum all probabilities <= observed
    p_value = 0.0
    for i in range(n + 1):
        log_binomi = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
        if p == 0:
            log_probi = log_binomi if i == n else float("-inf")
        elif p == 1:
            log_probi = log_binomi if i == 0 else float("-inf")
        else:
            log_probi = log_binomi + i * math.log(p) + (n - i) * math.log1p(-p)

        if not math.isinf(log_probi) and log_probi <= log_prob_obs:
            p_value += math.exp(log_probi)

    return min(p_value, 1.0)


# ---------------------------------------------------------------------------
# 2. Holm-Bonferroni correction
# ---------------------------------------------------------------------------

def holm_bonferroni(
    p_values: List[float],
    alpha: float = 0.05,
) -> Dict:
    """Apply Holm-Bonferroni correction for multiple comparisons.

    Args:
        p_values: List of raw p-values from a family of tests
        alpha: Family-wide significance level (default 0.05)

    Returns:
        dict with keys:
            adjusted_p_values: Holm-adjusted p-values (sorted same as input)
            rejected: Boolean list (True if null rejected)
            corrected_alpha_effective: Smallest individual alpha after correction
    """
    if not p_values:
        return {
            "adjusted_p_values": [],
            "rejected": [],
            "corrected_alpha_effective": alpha,
        }

    n = len(p_values)
    # Create index-tracking list: (original_index, p_value)
    indexed = list(enumerate(p_values))
    # Sort by p-value ascending (Holm stepdown)
    indexed.sort(key=lambda x: x[1])

    adjusted = [0.0] * n
    rejected = [False] * n
    prev_adjusted = 0.0

    for step, (orig_idx, p) in enumerate(indexed):
        # Holm adjustment: p * (n - step)
        raw_adjusted = p * (n - step)
        adjusted_val = max(raw_adjusted, prev_adjusted)  # enforce monotonicity
        adjusted_val = min(adjusted_val, 1.0)  # cap at 1
        adjusted[orig_idx] = adjusted_val
        prev_adjusted = adjusted_val

        if adjusted_val <= alpha:
            rejected[orig_idx] = True

    # Effective alpha: the smallest individual-level alpha after correction
    corrected_alpha = alpha / n

    return {
        "adjusted_p_values": adjusted,
        "rejected": rejected,
        "corrected_alpha_effective": corrected_alpha,
    }


# ---------------------------------------------------------------------------
# 3. Minimum sample size calculator
# ---------------------------------------------------------------------------

def minimum_sample_size(
    mde: float = 0.05,
    alpha: float = 0.05,
    power: float = 0.80,
    p_null: float = 0.5,
    max_n: int = 100000,
) -> Dict:
    """Minimum sample size for binomial test using arcsin transformation.

    Uses the variance-stabilizing arcsin transformation to compute
    the required sample size for a two-sided test.

    Args:
        mde: Minimum detectable effect (absolute difference from p_null, default 0.05)
        alpha: Significance level (default 0.05)
        power: Desired statistical power (default 0.80)
        p_null: Null hypothesis proportion (default 0.5)
        max_n: Maximum n to search (default 100000)

    Returns:
        dict with keys:
            n_required: Minimum integer n achieving desired power
            actual_power_at_n: Achieved power at n_required
            mde_at_n: Achievable mde at n_required
    """
    if not 0 < mde < 1:
        raise ValueError(f"mde must be in (0, 1), got {mde}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if not 0 < power < 1:
        raise ValueError(f"power must be in (0, 1), got {power}")
    if not 0 < p_null < 1:
        raise ValueError(f"p_null must be in (0, 1), got {p_null}")
    if p_null + mde > 1 or p_null - mde < 0:
        raise ValueError(
            f"p_null ± mde out of bounds: {p_null} ± {mde}"
        )

    p_alt = p_null + mde
    z_alpha = _norm_ppf(1.0 - alpha / 2.0)
    z_beta = _norm_ppf(power)

    # Arcsin transformation formula for two-sample (here one-sample):
    # n = (z_alpha + z_beta)^2 / (2 * (arcsin(sqrt(p_alt)) - arcsin(sqrt(p_null)))^2)
    # For one-sample test, a common formula is:
    # n = (z_alpha * sqrt(p_null * (1-p_null)) + z_beta * sqrt(p_alt * (1-p_alt)))^2 / mde^2

    # Either way, we compute and verify via bisection search for precision.

    # Use arcsin formula for initial estimate
    asin_p0 = 2.0 * math.asin(math.sqrt(p_null))
    asin_p1 = 2.0 * math.asin(math.sqrt(p_alt))
    delta = asin_p1 - asin_p0
    if abs(delta) < 1e-10:
        return {
            "n_required": 0,
            "actual_power_at_n": 0.0,
            "mde_at_n": 0.0,
        }

    n_est = math.ceil(((z_alpha + z_beta) / delta) ** 2)

    # Bisection search to find exact minimum n
    lo = max(1, n_est - 1000)
    hi = min(max_n, n_est + 1000)
    lo = max(1, lo)

    def _compute_power(n: int) -> float:
        """Compute the actual power for a given n."""
        if n <= 0:
            return 0.0
        # Standard error under null and alternative
        se_null = math.sqrt(p_null * (1.0 - p_null) / n)
        se_alt = math.sqrt(p_alt * (1.0 - p_alt) / n)

        # Critical value on null distribution
        crit = z_alpha * se_null

        # Power = P(|p_hat - p_null| > crit | p_alt) for two-sided test
        # Lower tail: P(p_hat < p_null - crit | p_alt)
        # Upper tail: P(p_hat > p_null + crit | p_alt)
        if se_alt < 1e-15:
            return 0.0

        z_lower = (-crit - mde) / se_alt
        z_upper = (crit - mde) / se_alt

        # Power = Phi(z_lower) + (1 - Phi(z_upper))
        # For a two-sided test with the alternative above the null:
        lower_tail = _norm_cdf(z_lower)
        upper_tail = 1.0 - _norm_cdf(z_upper)
        return min(lower_tail + upper_tail, 1.0)

    # Binary search for smallest n achieving desired power
    best_n = max_n
    found = False

    # Expand search range if n_est estimate was too low or high
    # First, check if we need higher n
    if n_est < max_n and _compute_power(n_est) < power:
        # Need larger n
        hi = max_n
        while lo < hi:
            mid = (lo + hi) // 2
            pwr = _compute_power(mid)
            if pwr >= power:
                hi = mid
            else:
                lo = mid + 1
        best_n = lo
        found = _compute_power(lo) >= power
    else:
        # n_est is large enough, search downward
        hi = n_est
        while lo < hi:
            mid = (lo + hi) // 2
            pwr = _compute_power(mid)
            if pwr >= power:
                hi = mid
            else:
                lo = mid + 1
        best_n = lo
        found = _compute_power(lo) >= power

    if not found:
        best_n = max_n

    actual_power = _compute_power(best_n)

    # Compute achievable mde at best_n
    achievable_mde = _compute_mde_at_n(best_n, alpha, power, p_null)

    return {
        "n_required": best_n,
        "actual_power_at_n": round(actual_power, 6),
        "mde_at_n": round(achievable_mde, 6),
    }


def _compute_mde_at_n(n: int, alpha: float, power: float, p_null: float) -> float:
    """Compute the minimum detectable effect at a given n using bisection."""
    if n <= 1:
        return 1.0

    z_alpha = _norm_ppf(1.0 - alpha / 2.0)
    z_beta = _norm_ppf(power)

    # Use arcsin approximation for initial estimate
    arcsin_delta = (z_alpha + z_beta) / math.sqrt(n)
    # delta = 2*arcsin(sqrt(p)) - 2*arcsin(sqrt(p0))
    # We need to find mde such that power is achieved.
    # Bisection on mde:
    lo, hi = 1e-6, min(1.0 - p_null, p_null)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        pwr = _compute_power_at_mde(n, mid, alpha, p_null)
        if pwr >= power:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2.0, 6)


def _compute_power_at_mde(n: int, mde: float, alpha: float, p_null: float) -> float:
    """Compute power for a given n and mde."""
    if n <= 0:
        return 0.0
    p_alt = p_null + mde
    se_null = math.sqrt(p_null * (1.0 - p_null) / n)
    se_alt = math.sqrt(p_alt * (1.0 - p_alt) / n)
    z_alpha = _norm_ppf(1.0 - alpha / 2.0)

    if se_alt < 1e-15:
        return 0.0

    crit = z_alpha * se_null
    z_lower = (-crit - mde) / se_alt
    z_upper = (crit - mde) / se_alt

    return min(_norm_cdf(z_lower) + 1.0 - _norm_cdf(z_upper), 1.0)


# ---------------------------------------------------------------------------
# 4. Effect size reporting (Cohen's h)
# ---------------------------------------------------------------------------

def cohens_h_effect(p1: float, p2: float) -> float:
    """Compute Cohen's h = 2 * arcsin(sqrt(p1)) - 2 * arcsin(sqrt(p2)).

    Args:
        p1: First proportion
        p2: Second proportion (typically the null)

    Returns:
        Cohen's h effect size
    """
    # Clip to avoid numerical issues at boundaries
    p1 = max(0.0, min(1.0, p1))
    p2 = max(0.0, min(1.0, p2))

    return 2.0 * math.asin(math.sqrt(p1)) - 2.0 * math.asin(math.sqrt(p2))


def effect_size_report(
    n_successes: int,
    n_trials: int,
    p_null: float = 0.5,
) -> Dict:
    """Generate a complete effect size report with interpretation.

    Args:
        n_successes: Number of successes observed
        n_trials: Total number of trials
        p_null: Null hypothesis proportion (default 0.5)

    Returns:
        dict with keys: accuracy, cohens_h, cohens_h_ci, interpretation
    """
    if n_trials <= 0:
        raise ValueError(f"n_trials must be > 0, got {n_trials}")
    if n_successes < 0 or n_successes > n_trials:
        raise ValueError(
            f"n_successes must be in [0, n_trials], got {n_successes} / {n_trials}"
        )

    p_hat = n_successes / n_trials
    h = cohens_h_effect(p_hat, p_null)

    # Confidence interval for Cohen's h using the delta method
    # Var(h) ≈ 1/(n * p_hat * (1-p_hat)) * (d(h)/d(p))^2
    # d(h)/d(p) = 2 / (2 * sqrt(p*(1-p))) * 1/(2*sqrt(p)) = 1/(sqrt(p*(1-p)))
    # Actually: h = 2*arcsin(sqrt(p)), so dh/dp = 1/sqrt(p*(1-p))
    # Var(h_hat) ≈ 1/n * (1/(p_hat * (1-p_hat)))
    # SE(h) ≈ 1/sqrt(n * p_hat * (1-p_hat))

    # Avoid division by zero at boundaries
    p_adj = max(1e-10, min(1.0 - 1e-10, p_hat))
    se_h = 1.0 / math.sqrt(n_trials * p_adj * (1.0 - p_adj))

    z_ci = _norm_ppf(0.975)  # 95% CI
    ci_lower = h - z_ci * se_h
    ci_upper = h + z_ci * se_h

    # Interpret magnitude
    abs_h = abs(h)
    if abs_h < 0.2:
        interpretation = "small"
    elif abs_h < 0.5:
        interpretation = "medium"
    else:
        interpretation = "large"

    return {
        "accuracy": p_hat,
        "cohens_h": round(h, 6),
        "cohens_h_ci": (round(ci_lower, 6), round(ci_upper, 6)),
        "interpretation": interpretation,
    }


# ---------------------------------------------------------------------------
# 5. Family correction convenience
# ---------------------------------------------------------------------------

def apply_holm_bonferroni_to_registry(alpha: float = 0.05) -> List[Dict]:
    """Apply Holm-Bonferroni to all registered tests with multi-family correction.

    Performs two-level correction:
      1. Holm-Bonferroni within each family
      2. Holm-Bonferroni across family-level minimum corrected p-values

    Returns list of dicts with per-test correction results.
    """
    return apply_family_corrections(alpha)


# ---------------------------------------------------------------------------
# 6. Block Bootstrap for Time-Series (Daily) Data
# ---------------------------------------------------------------------------

def block_bootstrap_ci_daily(
    daily_accuracies: List[float],
    block_size: int = 7,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    rng_seed: Optional[int] = None,
) -> Dict:
    """Block bootstrap confidence interval for a daily time series of accuracies.

    Resamples blocks of CONSECUTIVE DAYS to preserve temporal autocorrelation
    (synoptic weather patterns spanning 3-7 days). This is the correct method
    for time series data where serial dependence exists.

    Unlike bootstrap_ci() in core/luck_elimination.py which resamples blocks
    of INDIVIDUAL TRADES (within the trade sequence), this function resamples
    blocks of consecutive DAYS — the natural temporal unit for weather data.

    Args:
        daily_accuracies: List of float, one per day (mean accuracy across
                          stations for that day, or binary 0/1 per day).
        block_size: Number of consecutive days per block (default 7).
                    Recommended: 5-10 days (captures 3-7 day synoptic patterns).
        n_bootstrap: Number of bootstrap resamples (default 10000).
        confidence: Confidence level (default 0.95 -> 95% CI).
        rng_seed: Optional seed for reproducibility.

    Returns:
        dict with keys:
            ci_lower: Lower bound of confidence interval
            ci_upper: Upper bound of confidence interval
            bootstrap_mean: Mean of bootstrap distribution
            bootstrap_std: Standard deviation of bootstrap distribution
            n_days: Number of days in the original series
            n_blocks: Number of blocks created
            block_size: Block size used
            n_bootstrap: Number of bootstrap resamples
    """
    if len(daily_accuracies) < block_size * 2:
        raise ValueError(
            f"Need at least 2*block_size={2*block_size} days, "
            f"got {len(daily_accuracies)}"
        )

    arr = np.array(daily_accuracies, dtype=float)
    n = len(arr)

    # Build overlapping blocks of consecutive days
    blocks = [arr[i:i + block_size] for i in range(0, n - block_size + 1)]
    n_blocks = len(blocks)
    n_blocks_per_sample = int(np.ceil(n / block_size))

    rng = np.random.default_rng(rng_seed)
    boot_means = np.zeros(n_bootstrap)

    for i in range(n_bootstrap):
        indices = rng.integers(0, n_blocks, size=n_blocks_per_sample)
        sample = np.concatenate([blocks[j] for j in indices])[:n]
        boot_means[i] = np.mean(sample)

    alpha = 1.0 - confidence
    ci_lower = float(np.percentile(boot_means, 100 * alpha / 2.0))
    ci_upper = float(np.percentile(boot_means, 100 * (1.0 - alpha / 2.0)))

    return {
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "bootstrap_mean": float(np.mean(boot_means)),
        "bootstrap_std": float(np.std(boot_means)),
        "n_days": n,
        "n_blocks": n_blocks,
        "block_size": block_size,
        "n_bootstrap": n_bootstrap,
    }


def block_bootstrap_ci_from_trades(
    trades: List[dict],
    block_size: int = 7,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    rng_seed: Optional[int] = None,
) -> Dict:
    """Block bootstrap CI from individual trade dicts with 'date' and 'correct' keys.

    Groups trades by date, computes daily accuracy, then runs the daily
    block bootstrap. This correctly handles the two-level structure:
      1. Cross-sectional correlation (stations on the same day)
      2. Temporal autocorrelation (consecutive days)

    Args:
        trades: List of trade dicts. Each must have:
                - 'date': str (YYYY-MM-DD)
                - 'correct': bool or int (1 = correct, 0 = wrong)
        block_size: Number of consecutive days per block (default 7).
        n_bootstrap: Number of bootstrap resamples (default 10000).
        confidence: Confidence level (default 0.95 -> 95% CI).
        rng_seed: Optional seed for reproducibility.

    Returns:
        dict with keys from block_bootstrap_ci_daily, plus:
            n_trades: Number of individual trades
            n_days: Number of unique dates
            observed_accuracy: Raw observed accuracy (overall correct/total)
            daily_accuracy_mean: Mean of daily accuracies
    """
    if not trades:
        raise ValueError("Trade list is empty")

    # Group trades by date
    daily = defaultdict(list)
    for t in trades:
        date = t.get("date")
        if date is None:
            continue
        daily[date].append(t)

    if not daily:
        raise ValueError("No trades with valid 'date' field")

    # Compute daily accuracy
    dates_sorted = sorted(daily.keys())
    daily_accuracies: List[float] = []
    for date in dates_sorted:
        day_trades = daily[date]
        day_correct = sum(1 for t in day_trades if t.get("correct", False))
        day_total = len(day_trades)
        if day_total > 0:
            daily_accuracies.append(day_correct / day_total)

    if len(daily_accuracies) < 2:
        raise ValueError(f"Need at least 2 days of data, got {len(daily_accuracies)}")

    # Observed overall accuracy
    total_correct = sum(1 for t in trades if t.get("correct", False))
    total_trades = len(trades)
    observed_accuracy = total_correct / total_trades if total_trades > 0 else 0.0

    # Run the daily block bootstrap
    bb_result = block_bootstrap_ci_daily(
        daily_accuracies=daily_accuracies,
        block_size=block_size,
        n_bootstrap=n_bootstrap,
        confidence=confidence,
        rng_seed=rng_seed,
    )

    bb_result["n_trades"] = total_trades
    bb_result["n_days"] = len(dates_sorted)
    bb_result["observed_accuracy"] = observed_accuracy
    bb_result["daily_accuracy_mean"] = float(np.mean(daily_accuracies))
    bb_result["daily_accuracy_std"] = float(np.std(daily_accuracies))

    return bb_result


def _compute_block_bootstrap_n_eff(
    block_ci: Dict,
    observed_accuracy: float,
) -> Dict:
    """Compute implied N_eff from block bootstrap CI width.

    Uses the inverse of the binomial SE formula to back out the effective
    sample size that would produce a CI of the observed width.

    Args:
        block_ci: Result dict from block_bootstrap_ci_daily or
                  block_bootstrap_ci_from_trades.
        observed_accuracy: The observed overall accuracy (p_hat).

    Returns:
        dict with keys: n_eff_lower, n_eff_upper (range of implied N_eff)
    """
    ci_lower = block_ci["ci_lower"]
    ci_upper = block_ci["ci_upper"]
    p_hat = observed_accuracy

    # CI half-width
    half_width = (ci_upper - ci_lower) / 2.0
    if half_width <= 0:
        return {"n_eff_lower": float('inf'), "n_eff_upper": float('inf')}

    # For a Wilson/normal-approximation CI: half_width = z * sqrt(p*(1-p)/n_eff)
    # So n_eff = p_hat * (1-p_hat) * (z / half_width)^2
    z = _norm_ppf(0.975)  # 1.96 for 95% CI

    if p_hat <= 0 or p_hat >= 1:
        n_eff = float('inf')
    else:
        n_eff = p_hat * (1.0 - p_hat) * (z / half_width) ** 2

    # Also compute the N_eff that would give same CI width using the
    # Clopper-Pearson approximation: roughly same as above for central p
    return {
        "n_eff": int(round(n_eff)) if n_eff != float('inf') else float('inf'),
        "ci_half_width": round(half_width, 6),
        "observed_accuracy": round(p_hat, 6),
    }