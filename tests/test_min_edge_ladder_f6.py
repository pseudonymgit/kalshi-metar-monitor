#!/usr/bin/env python3
"""F6 (2026-10-13) — min-edge must gate every candidate, not only the argmax.

Before F6 the bridge picked the max-EV strike, then checked the
disagreement-scaled min-edge on that single pick; if it failed, the whole station
was skipped instead of falling through to the next-best strike. `select_strike`
now takes `min_edge_scaler` (strike -> edge multiplier) and applies each
candidate's own floor BEFORE ranking.

Run: python3 -m pytest tests/test_min_edge_ladder_f6.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.strike_selection_ev import select_strike  # noqa: E402


# Two strikes: s1 has the higher raw EV but a stricter (disagreement-widened)
# floor; s2 has lower EV but clears its own floor. p_fn maps strike -> P(high>=x).
def _p_fn(x):
    return {80.0: 0.50, 81.0: 0.62}.get(x, 0.5)


_STRIKES = [
    {"strike": 80.0, "yes_price": 0.30, "no_price": 0.70},
    {"strike": 81.0, "yes_price": 0.45, "no_price": 0.55},
]


def test_argmax_without_scaler_picks_top_strike():
    """Legacy behaviour: the single highest-EV strike clears the flat floor."""
    picked = select_strike(80.0, 3.0, _STRIKES, min_edge=0.10, p_fn=_p_fn)
    assert picked is not None
    assert picked["strike"] == 80.0          # ev 0.18 > floor 0.10
    assert abs(picked["ev"] - 0.18) < 1e-6


def test_scaler_skips_failing_top_and_selects_next_best():
    """Top strike fails its widened floor -> next-best that clears its floor wins."""
    scaler = lambda x: 2.0 if x == 80.0 else 1.0   # 80 -> floor 0.20, 81 -> floor 0.10
    picked = select_strike(80.0, 3.0, _STRIKES, min_edge=0.10, p_fn=_p_fn,
                           min_edge_scaler=scaler)
    assert picked is not None, "must fall through, not skip the station"
    assert picked["strike"] == 81.0
    assert picked["ev"] > picked["required_edge"]
    assert abs(picked["required_edge"] - 0.10) < 1e-9


def test_all_candidates_failing_floors_returns_none():
    scaler = lambda x: 5.0   # floor 0.50 for every strike
    assert select_strike(80.0, 3.0, _STRIKES, min_edge=0.10, p_fn=_p_fn,
                         min_edge_scaler=scaler) is None


def test_scaler_only_changes_the_floor_not_the_ev():
    flat = select_strike(80.0, 3.0, _STRIKES, min_edge=0.0, p_fn=_p_fn)
    scaled = select_strike(80.0, 3.0, _STRIKES, min_edge=0.0, p_fn=_p_fn,
                           min_edge_scaler=lambda x: 1.25)
    assert flat["ev"] == scaled["ev"]
    assert flat["strike"] == scaled["strike"]
