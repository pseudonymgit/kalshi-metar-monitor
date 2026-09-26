"""Tests for the deterministic NWP ensemble tie-breaker (core/nwp_tiebreak.py).

Covers:
  - determinism (same input -> same output)
  - magnitude-weighted lean direction
  - fixed-priority fallback on a perfect dead heat
  - honest low confidence cap
  - graceful handling of missing models / all-zero deltas
"""
import sys
import math

sys.path.insert(0, "prototypes/weather-engine-source")

from core.nwp_tiebreak import (
    resolve_tie,
    inverse_mae_weights,
    TIEBREAK_MIN_CONF,
    TIEBREAK_MAX_CONF,
    MODEL_PRIORITY,
)


class TestNwpTiebreak:
    def test_weights_normalized_and_ordered(self):
        w = inverse_mae_weights()
        assert abs(sum(w.values()) - 1.0) < 1e-9
        # ECMWF has the lowest MAE -> highest weight
        assert w["ecmwf"] == max(w.values())
        assert w["gem"] == min(w.values())

    def test_deterministic(self):
        case = {"gfs": 1.0, "ecmwf": 0.5, "icon": -0.2, "gem": -0.1}
        assert resolve_tie(case) == resolve_tie(case)

    def test_up_lean(self):
        d, c, r = resolve_tie({"gfs": 1.0, "ecmwf": 0.5, "icon": -0.2, "gem": -0.1})
        assert d == "up"
        assert r == "weighted_delta"

    def test_down_lean(self):
        d, c, r = resolve_tie({"gfs": 0.1, "ecmwf": 0.1, "icon": -1.0, "gem": -1.0})
        assert d == "down"
        assert r == "weighted_delta"

    def test_confidence_capped_low(self):
        # Large lean still must not fabricate high confidence.
        d, c, r = resolve_tie({"gfs": 50.0, "ecmwf": 40.0, "icon": -0.1, "gem": -0.1})
        assert d == "up"
        assert TIEBREAK_MIN_CONF <= c <= TIEBREAK_MAX_CONF

    def test_dead_heat_uses_fixed_priority(self):
        # Construct an exact zero weighted sum: choose two opposite deltas
        # whose weight*value products cancel. The dead-heat branch is a
        # defensive guard (measure-zero with real forecast values).
        w = inverse_mae_weights()
        d, c, r = resolve_tie({
            "gfs": 1.0 / w["gfs"],
            "ecmwf": 0.0,
            "icon": 0.0,
            "gem": -1.0 / w["gem"],
        })
        assert d is not None
        assert r.startswith("priority_fallback:")

    def test_opposite_magnitudes_resolve_by_weight(self):
        # +1,+1,-1,-1 is NOT a dead heat: unequal weights decide it.
        d, c, r = resolve_tie({"gfs": 1.0, "ecmwf": 1.0, "icon": -1.0, "gem": -1.0})
        assert d == "up"
        assert r == "weighted_delta"

    def test_all_zero_returns_no_direction(self):
        d, c, r = resolve_tie({"gfs": 0.0, "ecmwf": 0.0, "icon": 0.0, "gem": 0.0})
        assert d is None
        assert c == 0.0

    def test_missing_models_ignored(self):
        d, c, r = resolve_tie({"gfs": 2.0, "ecmwf": 1.0, "icon": None, "gem": None})
        assert d == "up"

    def test_no_usable_models(self):
        d, c, r = resolve_tie({"gfs": None, "ecmwf": None, "icon": None, "gem": None})
        assert d is None

    def test_confidence_never_nan(self):
        _, c, _ = resolve_tie({"gfs": 0.5, "ecmwf": -0.5, "icon": 0.0, "gem": 0.0})
        assert not math.isnan(c)
        assert 0.0 <= c <= 1.0
