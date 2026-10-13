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
  fee(c)       = ceil(0.07 * c * (1-c) * 100)/100    (Kalshi fee, canonical per contract)
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
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.fees import fee_per_contract as _fee_per_contract  # noqa: E402  (canonical fee)

# F1 (2026-10-13): per-contract EXECUTION cost (half-spread + slippage ~0.0205),
# the fallback used when no live bid/ask is available. This is the SAME value
# core/market_cost_model.py exposes as EXECUTION_COST_RATE (single source).
try:
    from core.market_cost_model import EXECUTION_COST_RATE as _FALLBACK_EXEC_COST  # noqa: E402
except Exception:  # pragma: no cover - never let a cost import break selection
    _FALLBACK_EXEC_COST = None

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
    """Kalshi fee in dollars for ONE contract (price in dollars 0-1).

    H1 (Sprint H, 2026-10-07): the canonical Kalshi taker fee is
        ceil(0.07 * P * (1-P) * 100) / 100
    (7% of notional, rounded up to the whole cent). The prior WAVE-A/1 form
    `ceil(0.07 * price)` dropped the p*(1-p) factor and mispriced away from
    P=0.5 — e.g. P=0.90 was over-charged 7x ($0.07 vs the true $0.01). The fee
    depends on the CONTRACT PRICE only, not on the model probability, so `p` is
    retained for call-site compatibility but unused.
    """
    return _fee_per_contract(price)


def ev_yes(mu: float, sigma: float, x: float, cost_yes: float, p_fn=None,
           entry_cost: float = 0.0, exit_cost: float = 0.0) -> float:
    p = (p_fn(x) if p_fn else p_high_ge(mu, sigma, x))
    fee = kalshi_fee(cost_yes, p) if cost_yes > 0 else 0.0
    if entry_cost or exit_cost:
        # F1 (2026-10-13): explicit execution-cost term. entry_cost is the actual
        # fill price paid to cross the book (mid + half-spread); exit_cost is the
        # cost to unwind at settlement/exit. The kalshi fee is charged on both legs
        # (round trip), matching the canonical fee model:
        #   ev = p * (1 - entry_cost - fee) - (1 - p) * (exit_cost + fee)
        return p * (1.0 - entry_cost - fee) - (1.0 - p) * (exit_cost + fee)
    # Legacy (no execution model): entry_cost == exit_cost == cost_yes.
    return p * (1 - cost_yes - fee) - (1 - p) * (cost_yes + fee)


def ev_no(mu: float, sigma: float, x: float, cost_no: float, p_fn=None,
          entry_cost: float = 0.0, exit_cost: float = 0.0) -> float:
    p = (p_fn(x) if p_fn else p_high_ge(mu, sigma, x))
    fee = kalshi_fee(cost_no, p) if cost_no > 0 else 0.0
    if entry_cost or exit_cost:
        # F1 (2026-10-13): NO leg mirrors YES with p -> 1-p.
        return (1.0 - p) * (1.0 - entry_cost - fee) - p * (exit_cost + fee)
    return (1 - p) * (1 - cost_no - fee) - p * (cost_no + fee)


def _entry_exec_cost(strike_row: Dict, cost_model, fail_closed: bool) -> float:
    """F1: per-contract execution cost (fraction of $1) to add to the mid.

    Live path: ``cost_model.get_dynamic_spread(ticker)`` -> half-spread.
    Fallback:  the measured/execution cost (``EXECUTION_COST_RATE`` ~0.0205).
    Fail-closed: with no live spread AND no fallback, raise instead of silently
    pricing off the mid (entry_cost == mid).

    ``cost_model`` is any object exposing ``get_dynamic_spread(ticker) -> spread``;
    its own internal fallback (measured mean spread) always yields a usable value.
    """
    if cost_model is not None:
        ticker = strike_row.get("ticker")
        live_spread = None
        if ticker:
            try:
                live_spread = cost_model.get_dynamic_spread(ticker)
            except Exception:
                live_spread = None
        if live_spread is not None and live_spread > 0:
            # One-way execution cost = half-spread + slippage, matching the
            # composition of EXECUTION_COST_RATE (MEASURED_MEAN_SPREAD/2 +
            # DEFAULT_SLIPPAGE) so live and fallback costs are interchangeable.
            slippage = float(getattr(cost_model, "slippage", 0.0) or 0.0)
            return float(live_spread) / 2.0 + slippage
    if _FALLBACK_EXEC_COST is not None:
        return float(_FALLBACK_EXEC_COST)
    if fail_closed:
        raise ValueError(
            "F1: execution spread unknown for strike "
            f"{strike_row.get('strike')!r} and no fallback available (fail-closed)"
        )
    return 0.0


def select_strike(mu: float, sigma: float, strikes: List[Dict], min_edge: float = 0.0,
                  p_fn=None, cost_model=None, fail_closed: bool = False) -> Optional[Dict]:
    """
    strikes: list of {strike: float, yes_price: float, no_price: float,
                      ticker?: str} (prices in dollars, 0-1).
    p_fn: optional callable x -> P(high >= x). REQUIRED when the caller holds a
          constrained/truncated distribution (e.g. intraday high_so_far); passing
          only (mu, sigma) would bypass that constraint and misprice locked wins.
    cost_model: F1 (2026-10-13) optional execution-cost model (e.g.
          core.market_cost_model.MARKET_COST_MODEL). When supplied, each strike's
          EV is net of the modeled execution cost (mid + half-spread at entry,
          half that at exit) rather than priced off the bare mid. When omitted,
          behaviour is unchanged (legacy mid + taker-fee pricing).
    fail_closed: when True and a strike has no resolvable spread and no fallback,
          raise instead of silently pricing off the mid.
    Returns the best {strike, side, ev, p, entry_cost} or None.
    """
    best = None
    for s in strikes:
        x = s["strike"]
        p = (p_fn(x) if p_fn else p_high_ge(mu, sigma, x))
        yes_px = float(s.get("yes_price", 0.5))
        no_px = float(s.get("no_price", 1.0 - yes_px))
        if cost_model is not None:
            # F1 (2026-10-13): round-trip execution model. The leg is bought at its
            # own mid + half-spread, and (for the intraday book, which exits before
            # settlement) unwound at the same cross cost. Hold-to-settlement callers
            # may pass a cost model with a zero exit via ev_* directly.
            ec = _entry_exec_cost(s, cost_model, fail_closed)
            entry_yes, exit_yes = yes_px + ec, yes_px + ec
            entry_no, exit_no = no_px + ec, no_px + ec
            e_yes = ev_yes(mu, sigma, x, yes_px, p_fn=p_fn,
                           entry_cost=entry_yes, exit_cost=exit_yes)
            e_no = ev_no(mu, sigma, x, no_px, p_fn=p_fn,
                         entry_cost=entry_no, exit_cost=exit_no)
        else:
            ec = 0.0
            entry_yes = yes_px
            e_yes = ev_yes(mu, sigma, x, yes_px, p_fn=p_fn)
            e_no = ev_no(mu, sigma, x, no_px, p_fn=p_fn)
        for side, ev, leg_entry in (("yes", e_yes, entry_yes), ("no", e_no, no_px + ec)):
            if best is None or ev > best["ev"]:
                best = {"strike": x, "side": side, "ev": round(ev, 4),
                        "p": round(p, 4), "entry_cost": round(leg_entry, 5)}
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
