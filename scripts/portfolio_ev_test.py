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


def joint_ev_mc(state_mu, state_sigma, picks, n_draws=20000, seed=7) -> float:
    """
    True expected P&L: every selected strike settles off the SAME latent high.
    Buy 1 YES contract on each selected strike. Payout = 1 if high>=strike.
    Cost = yes_price (+ fee). EV computed by averaging over draws of `high`.
    """
    rng = random.Random(seed)
    total = 0.0
    for _ in range(n_draws):
        high = rng.gauss(state_mu, max(state_sigma, 1e-6))
        pnl = 0.0
        for p in picks:
            win = 1.0 if high >= p["strike"] else 0.0
            fee = kalshi_fee(p["yes_price"], p["p"]) if p["yes_price"] > 0 else 0.0
            pnl += win * 1.0 - (p["yes_price"] + fee)
        total += pnl
    return total / n_draws


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
    joint = joint_ev_mc(mu, sigma, picks)

    print(f"{a.station} {a.date}: mu={mu:.1f} sigma={sigma:.2f}")
    print(f"  selected (EV>0) strikes: {[p['strike'] for p in picks]}")
    print(f"  naive summed per-strike EV (INDEPENDENCE, wrong): {naive:+.4f}")
    print(f"  JOINT portfolio EV (shared latent high, correct):  {joint:+.4f}")
    if naive != 0:
        print(f"  overlap distortion: naive is {naive/joint if joint else float('inf'):.2f}x the true EV"
              if joint else "  overlap distortion: joint EV <= 0 while naive > 0 — INDEPENDENCE ASSUMPTION LIES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
