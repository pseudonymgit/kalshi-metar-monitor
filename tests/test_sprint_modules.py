#!/usr/bin/env python3
"""
Tests for the Track 1-4 sprint modules (scoped review, 2026-09-28).

Covers the specific defects found in review so they can't regress:
  * F1  — EV selection must honor the intraday high_so_far truncation
  * F2  — portfolio EV: dependence shows in VARIANCE, not the mean
  * F3  — G1 target-invariance harness catches a leaky predictor
  * F11 — strike ladders must not mix threshold (T) and bucket (B) tickers
  * D1  — threshold is the default contract semantic
  * fee — kalshi_fee is a function of price only (Wave A/1)
  * eval — brier/ece raise on length mismatch (Wave A/3)

Run: SKIP_WEATHER_COLLECTOR=1 .venv-gefs/bin/python -m pytest tests/test_sprint_modules.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))


def test_kalshi_fee_price_only():
    from core.strike_selection_ev import kalshi_fee
    # fee must not depend on p (Wave A/1): called with/without p must agree
    assert kalshi_fee(0.40) == kalshi_fee(0.40, 0.5) == kalshi_fee(0.40, 0.9)
    assert kalshi_fee(0.0) == 0.0
    assert kalshi_fee(0.90) == 0.07


def test_eval_metrics_length_guard():
    from core.eval_metrics import brier, ece
    with pytest.raises(ValueError):
        brier([0.5, 0.6], [1])
    with pytest.raises(ValueError):
        ece([0.5, 0.6], [1])


def test_f1_ev_honors_truncation():
    """F1: select() must price P(high>=X)=1 for X<=high_so_far (locked win)."""
    from core.intraday.distribution_adapter import from_ws_p, update_from_intraday, select
    dist = json.load(open(ROOT / "data" / "ws_p_distribution.json"))
    st = from_ws_p(dist, "KNYC", 75.0, 2.5, 7)
    st2 = update_from_intraday(st, high_so_far=78.0, day_fraction_elapsed=0.6)
    best = select(st2, [{"strike": 75.0, "yes_price": 0.9, "no_price": 0.1}])
    assert best is not None and best["p"] == 1.0, "truncation not applied on EV path"


def test_f11_and_d1_ladder_semantics():
    """F11/D1: threshold filter returns only T tickers; bucket only B; they differ."""
    from core.price_intake import load_strike_ladder, SNAPSHOTS
    thr = load_strike_ladder(SNAPSHOTS, "KATL", "26SEP08", "HIGH", contract_semantic="threshold")
    bkt = load_strike_ladder(SNAPSHOTS, "KATL", "26SEP08", "HIGH", contract_semantic="bucket")
    if not thr and not bkt:
        pytest.skip("no snapshot data")
    assert all(s["ticker"].rsplit("-", 1)[-1].startswith("T") for s in thr)
    assert all(s["ticker"].rsplit("-", 1)[-1].startswith("B") for s in bkt)
    # HIGH ladder must not contain LOW tickers
    assert all("KXLOW" not in s["ticker"] for s in thr)


def test_g1_negative_control():
    """F3: the invariance harness must catch a leaky predictor."""
    from scripts.distribution_gate import gate_g1
    g = gate_g1()
    assert g.details.get("negative_control_caught") is True, "G1 harness is vacuous"
    assert g.details.get("clean_invariant") is True


def test_f2_portfolio_variance_understatement():
    """F2: correlated strikes have true variance > naive independent variance."""
    import scripts.portfolio_ev_test as p
    picks = p.per_strike_ev_positive(89.5, 7.5, [
        {"strike": 84.0, "yes_price": 0.2}, {"strike": 88.5, "yes_price": 0.25},
        {"strike": 91.0, "yes_price": 0.1}])
    if not picks:
        pytest.skip("no EV-positive picks")
    _, true_var = p.joint_ev_mc(89.5, 7.5, picks)
    naive_var = p.naive_independent_variance(picks)
    assert true_var > naive_var, "shared-draw variance should exceed naive independent"
