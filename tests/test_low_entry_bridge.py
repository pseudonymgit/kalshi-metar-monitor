#!/usr/bin/env python3
"""Workstream AF (2026-10-14) — LOW-lane entry bridge (prepare-only).

Mirrors the HIGH bridge: same 13:30 pin, same gate/grade stack, sole om_gfs Tmin
forecast, same Kelly sizing. Shares the SBOX book via the additive `lane` column.
NOT wired to a cron/live path.

Run: python3 -m pytest tests/test_low_entry_bridge.py -v
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


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTminState:
    """Stand-in: p_ge(X) = P(daily_min >= X); monotone increasing in X."""

    def __init__(self, mu=60.0, sigma=3.0):
        self.mu, self.sigma = mu, sigma

    def p_ge(self, x):
        import math
        if x <= self.mu:
            return 1.0
        return 0.5 * (1.0 - math.erf(((x - self.mu) / self.sigma) / math.sqrt(2.0)))


@pytest.mark.unit
def test_lane_constant_and_disabled_by_default():
    lb = _load_low()
    assert lb.LANE == "LOW"
    # Arming flag exists and defaults OFF (prepare-only).
    assert "LOW_BRIDGE_ENABLED" in open(
        os.path.join(_REPO, "scripts", "low_entry_bridge.py")).read()
    assert lb.LOW_BRIDGE_ENABLED is False


@pytest.mark.unit
def test_sole_tmin_p_fn_is_p_min_le_and_monotone_rising():
    lb = _load_low()
    st = _FakeTminState(mu=60.0, sigma=3.0)
    p_fn, meta_fn = lb.sole_tmin_p_fn(st, station="KATL")
    vals = [p_fn(x) for x in (54.0, 58.0, 60.0, 62.0, 66.0)]
    assert all(0.0 <= v <= 1.0 for v in vals)
    # P(min <= X) is NON-DECREASING in X.
    assert vals[0] <= vals[1] <= vals[2] <= vals[3] <= vals[4]
    m = meta_fn(60.0)
    assert m["lane"] == "LOW"
    assert m["sole_forecast"] is True
    assert m["p_gefs"] == pytest.approx(1.0 - round(st.p_ge(60.0), 4), abs=1e-4)
    ceiling = m["ceiling"]
    assert all(v <= ceiling + 1e-9 for v in vals)


@pytest.mark.unit
def test_low_ladder_reads_the_backfill_csv():
    lb = _load_low()
    # A station-day that exists in the backfilled LOW price series.
    ladder = lb.low_ladder_from_csv("KATL", "2026-09-07")
    assert ladder, "expected a LOW ladder for KATL 2026-09-07 from the CSV"
    for row in ladder:
        assert 0.0 < row["yes_price"] < 1.0
        assert row["no_price"] == pytest.approx(1 - row["yes_price"], abs=1e-4)
        assert row["ticker"].startswith("KXLOW")
    strikes = [r["strike"] for r in ladder]
    assert strikes == sorted(strikes)


@pytest.mark.unit
def test_run_is_prepare_only_and_lane_tagged():
    """Source contract: run() books lane=LOW, is never cron-wired."""
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert 'lane=LANE' in src, "LOW bookings must carry the lane tag"
    assert '"LOW"' in src
    assert "record_trade(" in src
    # No scheduling / live-wiring primitives in the module.
    full = open(os.path.join(_REPO, "scripts", "low_entry_bridge.py")).read()
    assert "cron" not in full.lower() or "NOT WIRED" in full


@pytest.mark.unit
def test_lane_column_shared_book_contract():
    """The shared record_trade seeds the lane column; HIGH is the default."""
    from scripts.sbox_intraday_ev_bridge import _ensure_lane_column
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_uuid TEXT)")
    c.execute("INSERT INTO trades (trade_uuid) VALUES ('a')")
    _ensure_lane_column(c)
    _ensure_lane_column(c)  # idempotent
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    assert "lane" in cols
    assert c.execute("SELECT lane FROM trades WHERE trade_uuid='a'").fetchone()[0] == "HIGH"
    c.close()
