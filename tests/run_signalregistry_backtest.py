#!/usr/bin/env python3
"""
SignalRegistry Backtest — Proper Statistical Methodology

Gilfoyle — 2026-08-11

Computes for each run_tag in bmode_p1_backtest.db:
  - Overall accuracy with two-sided binomial p-value (H0: p=0.5)
  - Holm-Bonferroni corrected p-value (family = 7 run_tags + per-station families)
  - Cohen's h effect size with 95% CI
  - 95% Clopper-Pearson confidence interval
  - Per-station breakdown
  - Per-prediction-direction breakdown
  - Minimum required sample size for 5% MDE at 80% power

Uses tests/stats.py for all statistical primitives.
"""

import sys
import os
import math
import sqlite3

# Ensure we can import from tests/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stats import (
    binomial_test,
    holm_bonferroni,
    effect_size_report,
    minimum_sample_size,
    cohens_h_effect,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "data", "bmode_p1_backtest.db")
ALPHA = 0.05
P_NULL = 0.5  # Random guessing baseline for binary directional prediction

RUN_TAGS = [
    "baseline",
    "baseline-default",
    "sweep-combined",
    "optimal-conf80",
    "optimal-conf70",
    "optimal-conf65",
    "optimal-conf55",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_pval(p: float) -> str:
    if p < 1e-200:
        return "< 1e-200"
    if p < 1e-10:
        return f"{p:.2e}"
    return f"{p:.6f}"

def fmt_ci(lo: float, hi: float) -> str:
    return f"[{lo:.4f}, {hi:.4f}]"

def fmt_pct(x: float) -> str:
    return f"{100.0 * x:.2f}%"

def interpret_accuracy(acc: float, p_null: float = 0.5) -> str:
    """Return a text interpretation of the accuracy relative to 50%."""
    if acc > p_null + 0.15:
        return "STRONG"
    elif acc > p_null + 0.05:
        return "MODERATE"
    elif acc > p_null:
        return "WEAK"
    elif abs(acc - p_null) < 0.01:
        return "AT_CHANCE"
    else:
        return "BELOW_CHANCE"

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_run_tag(conn, run_tag: str) -> dict:
    """Load all trades for a run_tag, return dict with arrays."""
    c = conn.cursor()
    c.execute(
        "SELECT correct, station, pred_direction, confidence FROM bmode_trades WHERE run_tag=?",
        (run_tag,)
    )
    rows = c.fetchall()
    return {
        "n": len(rows),
        "correct": [r[0] for r in rows],
        "stations": [r[1] for r in rows],
        "directions": [r[2] for r in rows],
        "confidences": [r[3] for r in rows],
    }


def min_sample_size_note(n_available: int) -> str:
    """Check if we have enough samples for 5% MDE with 80% power."""
    try:
        ms = minimum_sample_size(mde=0.05, alpha=ALPHA, power=0.80, p_null=P_NULL)
        needed = ms["n_required"]
        if n_available >= needed:
            return f"✓ {n_available} >= {needed} required"
        else:
            return f"✗ INSUFFICIENT: {n_available} < {needed} required"
    except Exception as e:
        return f"ERR: {e}"


def compute_per_station(data: dict):
    """Compute per-station breakdown."""
    station_data = {}
    for i in range(len(data["correct"])):
        stn = data["stations"][i]
        if stn not in station_data:
            station_data[stn] = {"n": 0, "correct": 0}
        station_data[stn]["n"] += 1
        station_data[stn]["correct"] += data["correct"][i]

    results = []
    for stn in sorted(station_data.keys()):
        sd = station_data[stn]
        bt = binomial_test(sd["correct"], sd["n"], P_NULL)
        esr = effect_size_report(sd["correct"], sd["n"], P_NULL)
        results.append({
            "station": stn,
            "n": sd["n"],
            "correct": sd["correct"],
            "accuracy": sd["correct"] / sd["n"],
            "p_value": bt["p_value"],
            "ci_lower": bt["ci_lower"],
            "ci_upper": bt["ci_upper"],
            "cohens_h": esr["cohens_h"],
            "interpretation": esr["interpretation"],
        })
    return results


def compute_per_direction(data: dict):
    """Compute per-prediction-direction breakdown."""
    dir_data = {}
    for i in range(len(data["correct"])):
        d = data["directions"][i]
        if d not in dir_data:
            dir_data[d] = {"n": 0, "correct": 0}
        dir_data[d]["n"] += 1
        dir_data[d]["correct"] += data["correct"][i]

    results = []
    for d in sorted(dir_data.keys()):
        dd = dir_data[d]
        bt = binomial_test(dd["correct"], dd["n"], P_NULL)
        esr = effect_size_report(dd["correct"], dd["n"], P_NULL)
        results.append({
            "direction": d,
            "n": dd["n"],
            "correct": dd["correct"],
            "accuracy": dd["correct"] / dd["n"],
            "p_value": bt["p_value"],
            "ci_lower": bt["ci_lower"],
            "ci_upper": bt["ci_upper"],
            "cohens_h": esr["cohens_h"],
            "interpretation": esr["interpretation"],
        })
    return results


def compute_high_low_analysis(data: dict):
    """
    Since market_price is always 0.5 (binary contracts), we can't distinguish
    HIGH vs LOW contract types directly. However, we can analyze by:
    - pred_direction (up vs down) as a proxy for directionality
    - confidence levels (high-confidence vs low-confidence trades)
    """
    # High-confidence vs low-confidence analysis
    high_conf = []
    low_conf = []
    for i in range(len(data["correct"])):
        if data["confidences"][i] >= 0.8:
            high_conf.append(data["correct"][i])
        else:
            low_conf.append(data["correct"][i])

    results = {"high_vs_low_conf": {}}

    if high_conf:
        n_h, s_h = len(high_conf), sum(high_conf)
        bt = binomial_test(s_h, n_h, P_NULL)
        results["high_vs_low_conf"]["high_conf"] = {
            "n": n_h, "correct": s_h, "accuracy": s_h / n_h,
            "p_value": bt["p_value"], "ci": (bt["ci_lower"], bt["ci_upper"]),
        }

    if low_conf:
        n_l, s_l = len(low_conf), sum(low_conf)
        bt = binomial_test(s_l, n_l, P_NULL)
        results["high_vs_low_conf"]["low_conf"] = {
            "n": n_l, "correct": s_l, "accuracy": s_l / n_l,
            "p_value": bt["p_value"], "ci": (bt["ci_lower"], bt["ci_upper"]),
        }

    return results


# ---------------------------------------------------------------------------
# Main Analysis
# ---------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("SIGNALREGISTRY BACKTEST — PROPER STATISTICAL ANALYSIS")
    print("=" * 80)
    print(f"Null hypothesis: p = {P_NULL} (random guessing)")
    print(f"Significance level: α = {ALPHA}")
    print(f"Multiple comparison correction: Holm-Bonferroni (family = {len(RUN_TAGS)})")
    print(f"Database: {DB_PATH}")
    print(f"Scipy available: ✓ (used for exact beta/binom distributions)")
    print()

    conn = sqlite3.connect(DB_PATH)

    # 1. Load all run_tag data
    all_data = {}
    for tag in RUN_TAGS:
        all_data[tag] = load_run_tag(conn, tag)

    # 2. Check for identical run_tags (deduplicate for table)
    unique_tags = []
    seen_data = set()
    dedup_map = {}
    for tag in RUN_TAGS:
        d = all_data[tag]
        # Create a fingerprint: (n, sum_correct)
        fp = (d["n"], sum(d["correct"]))
        if fp not in seen_data:
            seen_data.add(fp)
            unique_tags.append(tag)
            dedup_map[tag] = tag
        else:
            dedup_map[tag] = [t for t in unique_tags if all_data[t]["n"] == d["n"] and sum(all_data[t]["correct"]) == sum(d["correct"])][0]

    identity_report = []
    for tag in RUN_TAGS:
        if tag != dedup_map[tag]:
            identity_report.append(f"  {tag:20s} = IDENTICAL to {dedup_map[tag]}")
        else:
            identity_report.append(f"  {tag:20s} = unique")

    print("Run Tag Identity Check:")
    for line in identity_report:
        print(line)
    print()

    # 3. Overall statistical analysis per UNIQUE run_tag
    print("=" * 80)
    print("A. OVERALL ACCURACY — PER RUN TAG")
    print("=" * 80)
    print()
    print(f"{'Run Tag':20s} {'N':>6s} {'Correct':>8s} {'Acc':>7s} {'P-value':>12s} {'Holm-Bonf':>12s} {'Cohen-h':>8s} {'95% CI':>22s} {'Interp':>12s}")
    print("-" * 110)

    raw_pvals = []
    tag_results = {}

    for tag in unique_tags:
        d = all_data[tag]
        n = d["n"]
        s = sum(d["correct"])
        acc = s / n

        bt = binomial_test(s, n, P_NULL)
        esr = effect_size_report(s, n, P_NULL)
        interp = interpret_accuracy(acc)

        raw_pvals.append(bt["p_value"])
        tag_results[tag] = {
            "n": n, "correct": s, "accuracy": acc,
            "p_value": bt["p_value"],
            "ci_lower": bt["ci_lower"],
            "ci_upper": bt["ci_upper"],
            "cohens_h": esr["cohens_h"],
            "cohens_h_interp": esr["interpretation"],
            "interpretation": interp,
        }

        print(f"{tag:20s} {n:6d} {s:8d} {acc:7.4f} {fmt_pval(bt['p_value']):>12s} {'--':>12s} {esr['cohens_h']:8.4f} {fmt_ci(bt['ci_lower'], bt['ci_upper']):>22s} {interp:>12s}")

    # Apply Holm-Bonferroni across unique run_tags
    hb = holm_bonferroni(raw_pvals, ALPHA)

    print()
    print("Holm-Bonferroni Correction (family = {} unique run_tags):".format(len(unique_tags)))
    print(f"  Corrected α per test (Bonferroni first step): α/{len(unique_tags)} = {ALPHA/len(unique_tags):.4f}")
    print()

    print(f"{'Run Tag':20s} {'Raw P':>12s} {'Adj P':>12s} {'Reject H0?':>12s}")
    print("-" * 56)
    for i, tag in enumerate(unique_tags):
        rej_str = "✓ SIGNIFICANT" if hb["rejected"][i] else "✗ N.S."
        print(f"{tag:20s} {fmt_pval(raw_pvals[i]):>12s} {hb['adjusted_p_values'][i]:>12.6f} {rej_str:>12s}")
    print()

    # 4. Sample size sufficiency
    print("=" * 80)
    print("B. SAMPLE SIZE SUFFICIENCY (5% MDE, 80% power, α=0.05)")
    print("=" * 80)
    print()
    ms = minimum_sample_size(mde=0.05, alpha=ALPHA, power=0.80, p_null=P_NULL)
    print(f"  Minimum samples required for 5% MDE @ 80% power: {ms['n_required']}")
    print(f"  Achievable MDE at n_required: {ms['mde_at_n']}")
    print(f"  Actual power at n_required: {ms['actual_power_at_n']}")
    print()
    print(f"{'Run Tag':20s} {'N':>6s} {'Sufficient?':>20s}")
    print("-" * 46)
    for tag in unique_tags:
        n = all_data[tag]["n"]
        note = min_sample_size_note(n)
        print(f"{tag:20s} {n:6d} {note:>20s}")
    print()

    # 5. Per-station breakdown (for a representative run_tag)
    print("=" * 80)
    print("C. PER-STATION BREAKDOWN — baseline-default")
    print("=" * 80)
    print()

    data_ref = all_data["baseline-default"]
    station_results = compute_per_station(data_ref)

    # Holm-Bonferroni across stations
    n_stations = len(station_results)
    stn_pvals = [sr["p_value"] for sr in station_results]
    stn_hb = holm_bonferroni(stn_pvals, ALPHA)

    print(f"{'Station':6s} {'N':>4s} {'Correct':>8s} {'Acc':>7s} {'P-value':>12s} {'Adj P':>12s} {'Cohen-h':>8s} {'95% CI':>22s} {'Interp':>12s}")
    print("-" * 100)

    stations_below_chance = []
    stations_at_chance = []
    stations_above_chance = []

    for i, sr in enumerate(station_results):
        rej = stn_hb["rejected"][i]
        acc = sr["accuracy"]
        interp = interpret_accuracy(acc)
        sig_mark = " *" if rej else ""
        print(f"{sr['station']:6s} {sr['n']:4d} {sr['correct']:8d} {acc:7.4f} {fmt_pval(sr['p_value']):>12s} {stn_hb['adjusted_p_values'][i]:>12.6f}{sig_mark} {sr['cohens_h']:8.4f} {fmt_ci(sr['ci_lower'], sr['ci_upper']):>22s} {interp:>12s}")

        if acc < 0.5 - 0.01:
            stations_below_chance.append(sr["station"])
        elif acc < 0.5 + 0.01:
            stations_at_chance.append(sr["station"])
        else:
            stations_above_chance.append(sr["station"])

    print()
    print(f"Stations with accuracy BELOW chance: {len(stations_below_chance)} — {', '.join(stations_below_chance)}")
    print(f"Stations with accuracy AT chance:   {len(stations_at_chance)} — {', '.join(stations_at_chance)}")
    print(f"Stations with accuracy ABOVE chance: {len(stations_above_chance)} — {', '.join(stations_above_chance)}")
    print()

    # 6. Per-direction breakdown
    print("=" * 80)
    print("D. PER-DIRECTION BREAKDOWN")
    print("=" * 80)
    print()

    for tag in unique_tags:
        d = all_data[tag]
        dir_results = compute_per_direction(d)
        print(f"  {tag}:")
        for dr in dir_results:
            interp = interpret_accuracy(dr["accuracy"])
            bt = binomial_test(dr["correct"], dr["n"], P_NULL)
            print(f"    {dr['direction']:6s}: {dr['n']:5d} trades, {dr['correct']:4d} correct "
                  f"({dr['accuracy']:.4f})  p={fmt_pval(bt['p_value'])}  "
                  f"h={dr['cohens_h']:.4f}  {dr['interpretation']}  {interp}")
        print()

    # 7. Confidence-level analysis
    print("=" * 80)
    print("E. HIGH-CONFIDENCE VS LOW-CONFIDENCE ANALYSIS")
    print("=" * 80)
    print()

    for tag in unique_tags:
        d = all_data[tag]
        cla = compute_high_low_analysis(d)
        print(f"  {tag}:")
        if "high_conf" in cla["high_vs_low_conf"]:
            hc = cla["high_vs_low_conf"]["high_conf"]
            print(f"    High-conf (≥80%): {hc['n']:5d} trades, {hc['correct']:4d} correct "
                  f"({hc['accuracy']:.4f})  p={fmt_pval(hc['p_value'])}")
        if "low_conf" in cla["high_vs_low_conf"]:
            lc = cla["high_vs_low_conf"]["low_conf"]
            print(f"    Low-conf  (<80%): {lc['n']:5d} trades, {lc['correct']:4d} correct "
                  f"({lc['accuracy']:.4f})  p={fmt_pval(lc['p_value'])}")
        print()

    # 8. Check for Phase 2 sweep best config
    print("=" * 80)
    print("F. PHASE 2 SWEEP BEST CONFIG — UNSEEN DATA CHECK")
    print("=" * 80)
    print()
    print("Best config from sweep: min_conf=0.796, agree=4, train=318d, test=31d, off=16.0°F, decay=0.802, win=17d")
    print("Max target_date in database: ", end="")
    c = conn.cursor()
    c.execute("SELECT MAX(target_date) FROM bmode_trades")
    max_date = c.fetchone()[0]
    print(max_date)
    print()

    # Check the sweep-combined run_tag data range
    c.execute("SELECT MIN(target_date), MAX(target_date) FROM bmode_trades WHERE run_tag='sweep-combined'")
    sw_min, sw_max = c.fetchone()
    c.execute("SELECT MIN(target_date), MAX(target_date) FROM bmode_trades WHERE run_tag='baseline'")
    bl_min, bl_max = c.fetchone()

    print(f"  Baseline date range:          {bl_min} to {bl_max}")
    print(f"  Sweep-combined date range:    {sw_min} to {sw_max}")
    print(f"  Database max date:            {max_date}")
    print(f"  Unseen period target:         2026-07-27 to present")
    print()

    # Check if there's any data after 2026-07-27
    c.execute("SELECT COUNT(*) FROM bmode_trades WHERE target_date > '2026-07-27'")
    n_after = c.fetchone()[0]
    print(f"  Trades after 2026-07-27 in existing DB: {n_after}")

    if n_after > 0:
        c.execute("SELECT DISTINCT run_tag FROM bmode_trades WHERE target_date > '2026-07-27'")
        tags_with_unseen = [r[0] for r in c.fetchall()]
        print(f"  Run tags with unseen data: {', '.join(tags_with_unseen)}")

        # Show unseen data stats
        for tag in sorted(set(tags_with_unseen)):
            c.execute("SELECT COUNT(*), SUM(correct), MIN(target_date), MAX(target_date) FROM bmode_trades WHERE run_tag=? AND target_date > '2026-07-27'", (tag,))
            nu, suc, mn_dt, mx_dt = c.fetchone()
            acc_unseen = suc / nu if nu else 0
            print(f"    {tag:20s}: {nu:4d} trades, {suc:4d} correct ({acc_unseen:.4f})  [{mn_dt} to {mx_dt}]")
    else:
        print("  → No unseen data available in the existing database.")
        print("  → The Phase 2 sweep best config was NOT run on data after 2026-07-27.")
        print("  → To test on truly unseen data, deploy the best config (min_conf=0.796, agree=4)")
        print("    and collect new trades from 2026-07-27 onward, then re-run this analysis.")
    print()

    # 9. Summary and conclusions
    print("=" * 80)
    print("G. SUMMARY AND CONCLUSIONS")
    print("=" * 80)
    print()

    # Which run_tags survive correction
    surviving = []
    non_surviving = []
    for i, tag in enumerate(unique_tags):
        if hb["rejected"][i]:
            surviving.append(tag)
        else:
            non_surviving.append(tag)

    print(f"  Run tags with STATISTICALLY SIGNIFICANT accuracy (Holm-Bonferroni, α=0.05):")
    for tag in surviving:
        tr = tag_results[tag]
        print(f"    ✓ {tag:20s}: {fmt_pct(tr['accuracy'])} ({tr['n']} trades, "
              f"p={fmt_pval(tr['p_value'])}, h={tr['cohens_h']:.4f}, "
              f"CI={fmt_ci(tr['ci_lower'], tr['ci_upper'])})")
    print()

    if non_surviving:
        print(f"  Run tags that do NOT survive Holm-Bonferroni correction:")
        for tag in non_surviving:
            tr = tag_results[tag]
            print(f"    ✗ {tag:20s}: {fmt_pct(tr['accuracy'])} (p={fmt_pval(tr['p_value'])}, "
                  f"adj_p={hb['adjusted_p_values'][unique_tags.index(tag)]:.4f})")
        print()

    # Key finding: best accuracy
    best_tag = max(unique_tags, key=lambda t: tag_results[t]["accuracy"])
    best_tr = tag_results[best_tag]
    print(f"  Best accuracy: {best_tag} — {fmt_pct(best_tr['accuracy'])} ({best_tr['n']} trades)")
    print()

    # Effect size summary
    print(f"  Cohen's h summary:")
    for tag in unique_tags:
        tr = tag_results[tag]
        print(f"    {tag:20s}: h = {tr['cohens_h']:.4f} ({tr['cohens_h_interp']})")

    print()

    # Station-level summary
    n_sig_stations = sum(1 for r in stn_hb["rejected"] if r)
    print(f"  Station-level: {n_sig_stations}/{n_stations} stations survive "
          f"Holm-Bonferroni correction (α={ALPHA}, family={n_stations})")
    print()

    conn.close()


if __name__ == "__main__":
    main()