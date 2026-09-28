#!/usr/bin/env python3
"""
TRACK 3 / P3-D — Portfolio EV test (correlated strikes are ONE bet).

Per ROUND2-EXPERT-MEASUREMENT.md §2(c)/Blind-Spot-4: strikes on one station-day
are NOT independent. P(high>=72) and P(high>=74) are the same event viewed twice.
Per-strike EV can look positive on 20 strikes while the PORTFOLIO is one
directional bet. This module evaluates EV at the PORTFOLIO level.

Given a calibrated distribution and a strike ladder with prices, it:
  1. selects the non-dominated EV-positive strikes,
  2. computes EXPECTED portfolio P&L under the JOINT distribution (via Monte-Carlo
     over the single latent high — every strike settles off the SAME draw),
  3. compares naive-summed per-strike EV (assumes independence) vs the joint EV,
     exposing the overlap.

Deterministic given a seed. No AI/ML.
"""
import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))

from core.strike_selection_ev import p_high_ge, kalshi_fee  # noqa: E402


def per_strike_ev_positive(state_mu, state_sigma, ladder) -> List[Dict]:
    picks = []
    for s in ladder:
        x = s["strike"]; yes = s.get("yes_price", 0.5)
        p = p_high_ge(state_mu, state_sigma, x)
        fee = kalshi_fee(yes, p) if yes > 0 else 0.0
        ev = p * (1 - yes - fee) - (1 - p) * (yes + fee)
        if ev > 0:
            picks.append({"strike": x, "yes_price": yes, "p": p, "ev_per": ev})
    return picks


def naive_summed_ev(picks) -> float:
    """What you'd believe if strikes were INDEPENDENT (sum of per-strike EV)."""
    return sum(p["ev_per"] for p in picks)


def joint_ev_mc(state_mu, state_sigma, picks, n_draws=20000, seed=7):
    """
    True expected P&L AND portfolio variance: every selected strike settles off the
    SAME latent high (correct dependence).

    REVIEW FIX (2026-09-28): the original test compared sum-of-per-strike EV to the
    MC joint EV and called the gap "overlap distortion" — but E[sum] == sum[E]
    ALWAYS (linearity of expectation), so the two are equal by construction and the
    test proved nothing. Dependence shows up in the VARIANCE, not the mean.
    We now report:
      * joint EV (correct)               — equals the naive sum; not the signal
      * naive independent-portfolio VAR  — sum of per-strike variances
      * true portfolio VAR (shared draw) — the real risk
    A correlated book has true VAR >> naive VAR (the diversification is illusory).
    """
    rng = random.Random(seed)
    totals = []
    for _ in range(n_draws):
        high = rng.gauss(state_mu, max(state_sigma, 1e-6))
        pnl = 0.0
        for p in picks:
            win = 1.0 if high >= p["strike"] else 0.0
            fee = kalshi_fee(p["yes_price"], p["p"]) if p["yes_price"] > 0 else 0.0
            pnl += win * 1.0 - (p["yes_price"] + fee)
        totals.append(pnl)
    mean = sum(totals) / len(totals)
    var = sum((t - mean) ** 2 for t in totals) / len(totals)
    return mean, var


def naive_independent_variance(picks) -> float:
    """Portfolio variance IF each strike were an independent Bernoulli bet
    (Bernoulli variance p(1-p) per unit stake) — the WRONG assumption for correlated
    strikes."""
    return sum(p["p"] * (1 - p["p"]) for p in picks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="KATL")
    ap.add_argument("--date", default="26SEP08")
    ap.add_argument("--mean", type=float, default=88.0)
    ap.add_argument("--std", type=float, default=2.5)
    a = ap.parse_args()

    from core.price_intake import load_strike_ladder, SNAPSHOTS
    ladder = load_strike_ladder(SNAPSHOTS, a.station, a.date, "HIGH")
    if not ladder:
        print("PORTFOLIO: SKIP — no ladder")
        return 0

    # calibrate mu/sigma via ws_p fit
    dist = json.load(open(REPO / "data" / "ws_p_distribution.json"))
    fit = dist["fit"]
    mu = a.mean + fit["per_station_bias_F"].get(a.station, 0.0) + fit["global_offset_F"]
    sigma = max(0.8, fit["spread_a0_F"] + fit["spread_a1"] * a.std)

    picks = per_strike_ev_positive(mu, sigma, ladder)
    naive = naive_summed_ev(picks)
    joint, true_var = joint_ev_mc(mu, sigma, picks)
    naive_var = naive_independent_variance(picks)

    print(f"{a.station} {a.date}: mu={mu:.1f} sigma={sigma:.2f}")
    print(f"  selected (EV>0) strikes: {[p['strike'] for p in picks]}")
    print(f"  joint (correct) EV:            {joint:+.4f}   [== naive sum by linearity: {naive:+.4f}]")
    print(f"  naive INDEPENDENT portfolio var: {naive_var:.4f}")
    print(f"  TRUE portfolio var (shared draw): {true_var:.4f}")
    if naive_var > 0:
        ratio = true_var / naive_var
        print(f"  variance understatement factor: {ratio:.2f}x")
        print(f"  -> {len(picks)} strikes are NOT {len(picks)} independent bets;")
        print(f"     true risk is {ratio:.1f}x the naive estimate. Size as ONE bet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
