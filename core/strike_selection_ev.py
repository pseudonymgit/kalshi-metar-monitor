#!/usr/bin/env python3
"""
GS-D1 — Strike selection from the calibrated P(high >= X) distribution.

Given:
  - a calibrated predictive distribution for a station-day (emOS Gaussian from
    GEFS — see scripts/ws_p_distribution.py),
  - a Kalshi strike grid with YES prices,
select the strike maximizing expected value, accounting for the fee model.

EV logic (per strike X, YES side):
  p            = P(high >= X)                       (calibrated)
  fee(c)       = ceil(0.07 * c)                     (Kalshi fee depends on price only, per side)
  EV_yes(X)    = p*(1 - cost_yes - fee) - (1-p)*(cost_yes + fee)
  EV_no(X)     = (1-p)*(1 - cost_no  - fee) - p*(cost_no  + fee)
  Choose max EV across strikes and sides; only fire if EV > min_edge.

The distribution parameters (mu, sigma) come from the WS-P artifact. This module
is pure selection logic — no I/O, no network — so it is unit-testable.

B-Mode compliant. No AI/ML.
"""
import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

min_edge = 0.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_high_ge(mu: float, sigma: float, x: float) -> float:
    """Calibrated P(high >= X) for a Gaussian predictive distribution."""
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    # P(high >= x) = 1 - Phi((x - mu)/sigma)
    return 1.0 - norm_cdf((x - mu) / sigma)


def kalshi_fee(price: float, p: float = None) -> float:
    """Kalshi fee in dollars per contract (price in dollars 0-1).

    WAVE-A/1 (2026-09-28): fee depends on the CONTRACT PRICE only, not on the
    model probability. The prior `0.07 * price * p * (1 - p)` wrongly scaled the
    fee by p*(1-p) (which peaks at 0.25), understating the true fee up to 4x.
    Correct Kalshi general fee: ceil(0.07 * price)
    """
    p = 0.5 if p is None else p  # retained for call-site compatibility; unused
    if price <= 0:
        return 0.0
    return math.ceil(0.07 * price * 100) / 100.0


def ev_yes(mu: float, sigma: float, x: float, cost_yes: float) -> float:
    p = p_high_ge(mu, sigma, x)
    fee = kalshi_fee(cost_yes, p) if cost_yes > 0 else 0.0
    return p * (1 - cost_yes - fee) - (1 - p) * (cost_yes + fee)


def ev_no(mu: float, sigma: float, x: float, cost_no: float) -> float:
    p = p_high_ge(mu, sigma, x)
    fee = kalshi_fee(cost_no, p) if cost_no > 0 else 0.0
    return (1 - p) * (1 - cost_no - fee) - p * (cost_no + fee)


def select_strike(mu: float, sigma: float, strikes: List[Dict]) -> Optional[Dict]:
    """
    strikes: list of {strike: float, yes_price: float, no_price: float}
             (prices in dollars, 0-1).
    Returns the best {strike, side, ev, p} or None.
    """
    best = None
    for s in strikes:
        x = s["strike"]
        p = p_high_ge(mu, sigma, x)
        e_yes = ev_yes(mu, sigma, x, s.get("yes_price", 0.5))
        e_no = ev_no(mu, sigma, x, s.get("no_price", 0.5))
        for side, ev in (("yes", e_yes), ("no", e_no)):
            if best is None or ev > best["ev"]:
                best = {"strike": x, "side": side, "ev": round(ev, 4), "p": round(p, 4)}
    if best is None or best["ev"] <= min_edge:
        return None
    return best


def build_strike_grid(mu: float, center_step: float = 1.0, span: float = 18.0) -> List[float]:
    base = round(mu)
    n = int(span / center_step)
    return [base + k * center_step for k in range(-n, n + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="data/ws_p_distribution.json")
    ap.add_argument("--station", default="KNYC")
    ap.add_argument("--mean", type=float, required=True, help="GEFS ens mean (F) for the day")
    ap.add_argument("--std", type=float, required=True, help="GEFS ens std (F) for the day")
    ap.add_argument("--month", type=int, default=None)
    args = ap.parse_args()

    d = json.load(open(args.dist))
    a0, a1 = d["fit"]["spread_a0_F"], d["fit"]["spread_a1"]
    bias = d["fit"]["per_station_bias_F"].get(args.station, 0.0)
    offset = d["fit"]["global_offset_F"]
    mb = d["fit"].get("month_bias_F", {}).get(str(args.month), 0.0) if args.month else 0.0

    mu = args.mean + bias + offset + mb
    sigma = max(0.8, a0 + a1 * args.std)

    grid = build_strike_grid(mu)
    # No live prices here — demonstrate with a neutral 0.5/0.5 book and a modeled
    # fair-price book (yes_price = p) to show the EV surface.
    strikes = [{"strike": x, "yes_price": p_high_ge(mu, sigma, x),
                "no_price": 1 - p_high_ge(mu, sigma, x)} for x in grid]
    best = select_strike(mu, sigma, strikes)
    print(f"{args.station}: mu={mu:.1f}F sigma={sigma:.2f}F")
    print(f"strike grid: {grid[0]:.0f}..{grid[-1]:.0f}")
    print(f"best (at fair prices, edge≈0): {best}")


if __name__ == "__main__":
    main()
