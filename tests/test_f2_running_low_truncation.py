#!/usr/bin/env python3
"""F2 (2026-10-14) — running-low truncation for the LOW lane.

The LOW axis core is ``T(p) = floor(running_low@pin) - 0.5`` with
``p = P(min <= T)``. The running-low truncation is the LOW mirror of the HIGH
``high_so_far``: the daily minimum cannot be ABOVE anything already observed, so
for every strike at/below the observed running low the YES proposition
P(min <= X) is 1.0 — a LOCKED WIN. Before the fix the LOW lane computed none of
this and mispriced locked wins as uncertain (design-doc/reality divergence).

Covers: the shared truncation envelope (`truncated_p_ge` /
`make_truncated_p_fn`), `DistributionState.p_ge` on the LOW axis, the LOW
bridge's `sole_tmin_p_fn` truncation, the `running_low()` METAR reader, and the
backfill fallback.

Run: python3 -m pytest tests/test_f2_running_low_truncation.py -v
"""
import importlib.util
import inspect
import os
import sqlite3
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.intraday.distribution_adapter import DistributionState  # noqa: E402
from core.strike_selection_ev import (  # noqa: E402
    make_truncated_p_fn, p_high_ge, truncated_p_ge,
)


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTminState:
    """p_ge(X) = P(daily_min >= X); decreasing in X. No truncation of its own."""

    def __init__(self, mu=60.0, sigma=3.0, low_so_far=None):
        self.mu, self.sigma, self.low_so_far = mu, sigma, low_so_far

    def p_ge(self, x):
        import math
        if self.low_so_far is not None and x >= self.low_so_far:
            return 0.0
        if x <= self.mu:
            return 1.0
        return 0.5 * (1.0 - math.erf(((x - self.mu) / self.sigma) / math.sqrt(2.0)))


# ── the shared envelope ──────────────────────────────────────────────────────
@pytest.mark.unit
def test_truncated_p_ge_high_so_far_boundary():
    assert truncated_p_ge(0.2, 70.0, high_so_far=70.0) == 1.0   # equality -> fact
    assert truncated_p_ge(0.2, 69.0, high_so_far=70.0) == 1.0
    assert truncated_p_ge(0.2, 71.0, high_so_far=70.0) == 0.2   # above -> raw


@pytest.mark.unit
def test_truncated_p_ge_low_so_far_boundary_is_the_mirror():
    # Daily min cannot exceed the observed low: P(min>=X)=0 for X >= low_so_far.
    assert truncated_p_ge(0.8, 65.0, low_so_far=65.0) == 0.0    # equality -> fact
    assert truncated_p_ge(0.8, 66.0, low_so_far=65.0) == 0.0
    assert truncated_p_ge(0.8, 64.0, low_so_far=65.0) == 0.8    # below -> raw


@pytest.mark.unit
def test_truncated_p_ge_no_bound_is_identity():
    assert truncated_p_ge(0.37, 70.0) == 0.37


@pytest.mark.unit
def test_make_truncated_p_fn_wraps_a_raw_surface():
    raw = lambda x: p_high_ge(60.0, 3.0, x)
    fn = make_truncated_p_fn(raw, low_so_far=58.0)
    # x >= running low -> the daily min is already <= x -> P(min>=x)=0 (locked).
    assert fn(58.0) == 0.0
    assert fn(61.0) == 0.0
    # x BELOW the running low still needs the forecast.
    assert fn(50.0) == pytest.approx(raw(50.0))


# ── DistributionState LOW axis ───────────────────────────────────────────────
@pytest.mark.unit
def test_distribution_state_low_so_far_truncation():
    st = DistributionState("KATL", "2026-09-07", mu=60.0, sigma=3.0, low_so_far=58.0)
    assert st.p_ge(58.0) == 0.0   # equality -> observed min is fact
    assert st.p_ge(59.0) == 0.0   # above observed min -> impossible
    assert 0.0 < st.p_ge(50.0) < 1.0
    assert st.p_ge(50.0) == pytest.approx(p_high_ge(60.0, 3.0, 50.0))


@pytest.mark.unit
def test_distribution_state_high_axis_unchanged_without_low_so_far():
    st = DistributionState("KNYC", "2026-09-07", mu=80.0, sigma=3.0, high_so_far=78.0)
    assert st.low_so_far is None
    assert st.p_ge(78.0) == 1.0
    assert 0.0 < st.p_ge(81.0) < 1.0


# ── LOW bridge p_fn ──────────────────────────────────────────────────────────
@pytest.mark.unit
def test_sole_tmin_p_fn_forces_locked_win_to_the_ceiling():
    lb = _load_low()
    st = _FakeTminState(mu=60.0, sigma=3.0, low_so_far=58.0)
    p_fn, meta_fn = lb.sole_tmin_p_fn(st, station="KATL", low_so_far=58.0)
    # A strike at/ABOVE the observed running low (58) is already breached -> the
    # daily min is <= it -> P(min<=X) == 1 -> calibrated value pinned at the
    # station ceiling (a locked win, not an uncertain value).
    m = meta_fn(58.0)
    assert m["running_low_locked"] is True
    assert m["p_gefs"] == 1.0
    assert p_fn(58.0) == pytest.approx(m["ceiling"])
    assert p_fn(70.0) == pytest.approx(m["ceiling"])
    # A strike BELOW the observed running low is priced by the Gaussian.
    assert 0.0 < p_fn(30.0) < m["ceiling"]
    assert meta_fn(30.0)["running_low_locked"] is False


@pytest.mark.unit
def test_sole_tmin_p_fn_is_monotone_and_unaffected_without_bound():
    lb = _load_low()
    st = _FakeTminState(mu=60.0, sigma=3.0)
    p_fn, _ = lb.sole_tmin_p_fn(st, station="KATL", low_so_far=None)
    vals = [p_fn(x) for x in (50.0, 55.0, 60.0, 65.0, 70.0)]
    assert all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
    assert vals[0] < vals[-1]


# ── running_low from METAR ───────────────────────────────────────────────────
def _metar_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE metar_observations (station TEXT, timestamp_utc TEXT, temp_f REAL)")
    c.executemany("INSERT INTO metar_observations VALUES (?,?,?)", rows)
    c.commit()
    c.close()


@pytest.mark.unit
def test_running_low_reads_the_observed_min_up_to_the_pin(monkeypatch, tmp_path):
    lb = _load_low()
    db = tmp_path / "metar.db"
    # KATL local 2026-09-07 = America/New_York. Local day start 00:00 EDT =
    # 04:00Z; 13:30 pin = 17:30Z. The 20:00Z obs is AFTER the pin -> must not count.
    _metar_db(db, [("KATL", "2026-09-07T05:00:00+00:00", 74.0),
                   ("KATL", "2026-09-07T10:00:00+00:00", 72.0),
                   ("KATL", "2026-09-07T20:00:00+00:00", 50.0),   # after pin
                   ("KATL", "2026-09-06T23:00:00+00:00", 40.0)])  # prior local day
    monkeypatch.setattr(lb, "METAR", db)
    assert lb.running_low("KATL", "2026-09-07") == pytest.approx(72.0)


@pytest.mark.unit
def test_running_low_none_without_observations(monkeypatch, tmp_path):
    lb = _load_low()
    db = tmp_path / "metar.db"
    _metar_db(db, [("KATL", "2026-09-07T20:00:00+00:00", 50.0)])  # only after pin
    monkeypatch.setattr(lb, "METAR", db)
    assert lb.running_low("KATL", "2026-09-07") is None


# ── backfill fallback + wiring contract ──────────────────────────────────────
@pytest.mark.unit
def test_ladder_surfaces_running_low_at_pin():
    lb = _load_low()
    ladder = lb.low_ladder_from_csv("KATL", "2026-09-07")
    assert ladder
    assert all(r.get("running_low_at_pin") is not None for r in ladder)
    # Constant per station-day (the backfill carries one pin reading).
    assert len({r["running_low_at_pin"] for r in ladder}) == 1
    assert lb.running_low_at_pin(ladder) == ladder[0]["running_low_at_pin"]


@pytest.mark.unit
def test_run_arms_the_running_low_truncation():
    """Source contract: run() computes the running low and arms it on the state."""
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert "running_low(st" in src, "run must compute the running low"
    assert "state.low_so_far = rlow" in src, "the state must carry the bound"
    assert "low_so_far=rlow" in src, "the p_fn must be armed with the bound"
