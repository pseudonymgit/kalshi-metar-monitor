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
    sigma: float       # calibrated predictive std (F)
    high_so_far: Optional[float] = None  # observed max so far today (F)

    def p_ge(self, x: float) -> float:
        # WAVE-A/2 (2026-09-28): enforce the monotonicity boundary EXACTLY.
        # The daily max is >= high_so_far by construction, so P(high>=X)=1 for
        # every X <= high_so_far. A shifted Gaussian still leaves mass below
        # high_so_far (impossible events) -> we truncate explicitly.
        if self.high_so_far is not None and x <= self.high_so_far:
            return 1.0
        return p_high_ge(self.mu, self.sigma, x)


def from_ws_p(dist: dict, station: str, ens_mean: float, ens_std: float,
              month: int, date: str = "") -> DistributionState:
    """Seed a DistributionState from the WS-P artifact.

    WAVE-A/4 (2026-09-28): `date` is now passed through (was hardcoded "").
    """
    fit = dist["fit"]
    bias = fit["per_station_bias_F"].get(station, 0.0)
    offset = fit["global_offset_F"]
    mb = fit.get("month_bias_F", {}).get(str(month), 0.0)
    sigma = max(0.8, fit["spread_a0_F"] + fit["spread_a1"] * ens_std)
    return DistributionState(station, date, ens_mean + bias + offset + mb, sigma)


def update_from_intraday(state: DistributionState, high_so_far: float,
                         day_fraction_elapsed: float) -> DistributionState:
    """
    Constrain the daily-max distribution by the observed high-so-far.

    WAVE-A/2: records high_so_far so p_ge() can enforce P(high>=X)=1 for
    X<=high_so_far exactly (truncation, not just a mean shift). The mean is also
    floored at the observed max; sigma shrinks as the day progresses.
    """
    df = max(0.0, min(1.0, day_fraction_elapsed))
    remaining = max(0.05, 1.0 - df)
    # Preserve the running observed max across successive calls.
    hsf = max(high_so_far, state.high_so_far) if state.high_so_far is not None else high_so_far
    mu = max(state.mu, hsf)
    sigma = state.sigma * math.sqrt(remaining)
    return DistributionState(state.station, state.date, mu, sigma, high_so_far=hsf)


def select(state: DistributionState, strikes: List[Dict], min_edge: float = 0.0) -> Optional[Dict]:
    """Pick the best-EV strike against live prices."""
    return select_strike(state.mu, state.sigma, strikes)


if __name__ == "__main__":
    import json
    from pathlib import Path
    dist = json.load(open(Path(__file__).resolve().parents[2] / "data" / "ws_p_distribution.json"))
    st = from_ws_p(dist, "KNYC", ens_mean=75.0, ens_std=2.5, month=7)
    print(f"seeded: mu={st.mu:.1f} sigma={st.sigma:.2f}")
    st2 = update_from_intraday(st, high_so_far=78.0, day_fraction_elapsed=0.6)
    print(f"after 14:00 high=78: mu={st2.mu:.1f} sigma={st2.sigma:.2f}")
    print("P(high>=80)=", round(st2.p_ge(80), 3))
