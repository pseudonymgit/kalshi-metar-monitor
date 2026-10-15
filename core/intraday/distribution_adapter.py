#!/usr/bin/env python3
"""
GS-D1 — Intraday distribution refresh adapter.

Bridges the calibrated GEFS P(high >= X) distribution (WS-P) into the intraday
stack: as METAR comes in during the day, the day's high-so-far constrains the
distribution, and the strike-selection EV layer (core/strike_selection_ev.py)
re-picks strikes against live prices.

Design (integration plan):
  - `DistributionState` holds (mu, sigma) for a station-day, seeded from the
    WS-P emOS fit (ens_mean + bias + offset + month_bias ; sigma = a0 + a1*std).
  - `update_from_intraday(high_so_far, hours_elapsed)` truncates the distribution:
    the remaining daily max cannot be below the observed high (P(high>=X)=1 for
    X<=high_so_far), and the conditional mean for the rest of the day is blended.
  - `select(...)` delegates to the EV layer.

Pure functions, no I/O — unit-testable and safe to call every intraday cycle.

B-Mode compliant. No AI/ML.
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.strike_selection_ev import p_high_ge, select_strike


@dataclass
class DistributionState:
    station: str
    date: str
    mu: float          # calibrated predictive mean (F)
    sigma: float       # CURRENT predictive std (F)
    high_so_far: Optional[float] = None  # observed max so far today (F)
    base_sigma: Optional[float] = None   # unshrunk daily sigma (set once at seed)
    low_so_far: Optional[float] = None   # observed min so far today (F) — LOW axis

    def p_ge(self, x: float) -> float:
        # WAVE-A/2 (2026-09-28): enforce the monotonicity boundary EXACTLY.
        # The daily max is >= high_so_far by construction, so P(high>=X)=1 for
        # every X <= high_so_far. A shifted Gaussian still leaves mass below
        # high_so_far (impossible events) -> we truncate explicitly.
        #
        # F2 (2026-10-14): the LOW axis mirror. The daily min is <= low_so_far,
        # so P(min>=X)=0 for every X >= low_so_far (equivalently P(min<=X)=1 —
        # a locked win). Enforced here so every caller of state.p_ge() sees the
        # truncation, not just the LOW bridge's p_fn wrapper.
        if self.high_so_far is not None and x <= self.high_so_far:
            return 1.0
        if self.low_so_far is not None and x >= self.low_so_far:
            return 0.0
        return p_high_ge(self.mu, self.sigma, x)


def from_ws_p(dist: dict, station: str, ens_mean: float, ens_std: float,
              month: int, date: str = "") -> DistributionState:
    """Seed a DistributionState from the WS-P artifact.

    WAVE-A/4 (2026-09-28): `date` is now passed through (was hardcoded "").
    E2E/F15: records `base_sigma` so the intraday shrink is applied to the daily
    sigma ONCE per call, not compounded across calls.
    """
    fit = dist["fit"]
    bias = fit["per_station_bias_F"].get(station, 0.0)
    offset = fit["global_offset_F"]
    mb = fit.get("month_bias_F", {}).get(str(month), 0.0)
    sigma = max(0.8, fit["spread_a0_F"] + fit["spread_a1"] * ens_std)
    return DistributionState(station, date, ens_mean + bias + offset + mb, sigma,
                             high_so_far=None, base_sigma=sigma)


def update_from_intraday(state: DistributionState, high_so_far: float,
                         day_fraction_elapsed: float) -> DistributionState:
    """
    Constrain the daily-max distribution by the observed high-so-far.

    WAVE-A/2: records high_so_far so p_ge() can enforce P(high>=X)=1 for
    X<=high_so_far exactly (truncation, not just a mean shift).

    E2E/F15 (2026-09-28) — CRITICAL FIX: the shrink is now applied to `base_sigma`
    (the unshrunk daily sigma recorded at seed), NOT to the running sigma. The old
    form did `sigma = state.sigma * sqrt(remaining)`, which COMPOUNDED on every
    call: a real hourly replay (≈20 calls) drove sigma 8.42 -> 0.37, turning the
    distribution into a spike and making p_ge() a step function (a false p=1.0 on
    strikes ABOVE the observed high). Recomputing from base_sigma each call makes
    the update idempotent w.r.t. call count for a given (high_so_far, fraction).
    """
    df = max(0.0, min(1.0, day_fraction_elapsed))
    remaining = max(0.05, 1.0 - df)
    # Preserve the running observed max across successive calls.
    hsf = max(high_so_far, state.high_so_far) if state.high_so_far is not None else high_so_far
    base = state.base_sigma if state.base_sigma is not None else state.sigma
    mu = max(state.mu, hsf)
    sigma = base * math.sqrt(remaining)
    return DistributionState(state.station, state.date, mu, sigma,
                             high_so_far=hsf, base_sigma=base)


def update_from_intraday_low(state: DistributionState, low_so_far: Optional[float],
                             day_fraction_elapsed: float) -> DistributionState:
    """
    LOW-axis mirror of `update_from_intraday`: shrink the daily-min sigma by the
    fraction of the day remaining, on the observed running-low bound.

    PWW-2026-10-15 (ITEM2): the LOW bridge built its Tmin p_fn from the FULL-day
    sigma (3.0 F) all day, while HIGH shrinks sigma as observations accrue. This
    reuses the exact E2E/F15 mechanics (recompute from `base_sigma`, never
    compound) so the LOW sigma shrinks once the running low is observed. The mu
    is pinned to the OBSERVED running low (the daily min cannot exceed it — the
    running low is the best point estimate for the realized min so far), i.e. the
    `min` mirror of HIGH's `mu = max(state.mu, hsf)`. The `low_so_far` bound
    itself is preserved for p_ge()'s truncation.
    """
    df = max(0.0, min(1.0, day_fraction_elapsed))
    remaining = max(0.05, 1.0 - df)
    # Preserve the running observed min across successive calls (min, not max).
    # `low_so_far` is Optional: without an observed bound the sigma still shrinks
    # by the remaining-day fraction, but mu is left at the forecast value.
    lsf: Optional[float] = None
    if low_so_far is not None and state.low_so_far is not None:
        lsf = min(low_so_far, state.low_so_far)
    elif low_so_far is not None:
        lsf = low_so_far
    elif state.low_so_far is not None:
        lsf = state.low_so_far
    base = (getattr(state, "base_sigma", None) or state.sigma)
    mu = min(state.mu, lsf) if lsf is not None else state.mu
    sigma = base * math.sqrt(remaining)
    return DistributionState(state.station, state.date, mu, sigma,
                             high_so_far=state.high_so_far, low_so_far=lsf,
                             base_sigma=base)


def select(state: DistributionState, strikes: List[Dict], min_edge: float = 0.0) -> Optional[Dict]:
    """Pick the best-EV strike against live prices.

    REVIEW FIX (2026-09-28): pass `p_fn=state.p_ge` so the EV layer uses the
    CONSTRAINED distribution (which returns P(high>=X)=1 for X<=high_so_far).
    Previously this delegated as select_strike(mu, sigma, ...), which recomputed a
    plain Gaussian and BYPASSED the high_so_far truncation — mispricing any strike
    at or below the already-observed max (a locked win priced as uncertain).
    """
    return select_strike(state.mu, state.sigma, strikes, min_edge=min_edge,
                         p_fn=state.p_ge)


if __name__ == "__main__":
    import json
    from pathlib import Path
    dist = json.load(open(Path(__file__).resolve().parents[2] / "data" / "ws_p_distribution.json"))
    st = from_ws_p(dist, "KNYC", ens_mean=75.0, ens_std=2.5, month=7)
    print(f"seeded: mu={st.mu:.1f} sigma={st.sigma:.2f}")
    st2 = update_from_intraday(st, high_so_far=78.0, day_fraction_elapsed=0.6)
    print(f"after 14:00 high=78: mu={st2.mu:.1f} sigma={st2.sigma:.2f}")
    print("P(high>=80)=", round(st2.p_ge(80), 3))
