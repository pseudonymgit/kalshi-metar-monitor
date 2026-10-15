#!/usr/bin/env python3
"""EV gate on calibrated_p (2026-10-14) — the entry path must price on the HONEST
calibrated probability, and fall CLOSED when it is unavailable.

Covers:
  * `honest_calibrated_p` formula + ceiling clamp;
  * `build_fused_p_fn` returns the calibrated value as its `p_fn` output and
    stamps `calibrated_p`/`ceiling`/`composite_conf` into the meta dict;
  * the bridge source contains the fail-closed calibrated_p guard.

Run: python3 -m pytest tests/test_ev_gate_calibrated.py -q
"""
import importlib.util
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.intraday.honest_calibration import (  # noqa: E402
    honest_calibrated_p, station_ceiling, station_ceilings,
    composite_conf_from_p, ECE, BASE_RATE, POOLED_CEILING,
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── formula ──────────────────────────────────────────────────────────────────
def test_coin_flip_stays_a_coin_flip():
    # 2026-10-14 bug-hunt finding #2: the shrink must be NEUTRAL at 0.5 — a raw
    # 0.5 must NOT become a manufactured 0.5705 YES edge (the old one-sided
    # shrink to base_rate did exactly that).
    assert abs(honest_calibrated_p(0.5, 0.82) - 0.5) < 0.01
    assert abs(honest_calibrated_p(0.5, 0.99) - 0.5) < 1e-9
    # and it must be symmetric about 0.5 (two-sided).
    lo = honest_calibrated_p(0.3, 0.99)
    hi = honest_calibrated_p(0.7, 0.99)
    assert abs((hi - 0.5) - (0.5 - lo)) < 1e-9


def test_ceiling_clamps_overconfident_signal():
    # The cap is a real clamp: a p whose SHRUNK value exceeds the ceiling is
    # pulled DOWN to it (shrink is near-identity under the 2026-10-15 ECE).
    assert honest_calibrated_p(0.9999, 0.989) == 0.989
    assert honest_calibrated_p(0.9999, 0.96) == 0.96
    # ...and a value already below the cap is left alone (by the cap).
    assert honest_calibrated_p(0.99, 0.96) < 0.989


def test_unknown_station_falls_back_to_pooled():
    assert station_ceiling("ZZZZ") == POOLED_CEILING
    # Every shipped station cap is present and near-identity (2026-10-15 refit).
    ce = station_ceilings()
    assert len(ce) == 20
    assert all(0.90 <= v <= 0.99 for v in ce.values())


def test_nan_is_rejected():
    try:
        honest_calibrated_p(float("nan"), 0.8)
    except ValueError:
        return
    raise AssertionError("NaN raw probability must raise (fail closed)")


def test_none_and_nonfinite_fail_closed():
    # ITEM 2 (2026-10-15): None raised TypeError; a NaN ceiling silently clamped
    # to the max (fail-OPEN). Both must now raise ValueError.
    for bad_p in (None, float("inf"), "x"):
        try:
            honest_calibrated_p(bad_p, 0.9)
        except ValueError:
            pass
        else:
            raise AssertionError(f"p={bad_p!r} must raise ValueError")
    for bad_ce in (None, float("nan"), float("inf")):
        try:
            honest_calibrated_p(0.7, bad_ce)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ceiling={bad_ce!r} must raise ValueError")


def test_calibration_does_not_harm_the_honest_cohort():
    """ITEM 1 invariant: brier(calibrated) <= brier(raw) on the shipped cohort.

    Fixture = the honest sole-om_gfs cohort (p_raw, per-station ceiling, y),
    n=2927. The pre-repair transform scored 0.1206 vs raw 0.1067 (worse at
    14/20 stations); the repair must not degrade the shipped signal.
    """
    import json
    fx = json.load(open(os.path.join(
        _REPO, "tests", "fixtures", "honest_cohort_2026-10-15.json")))
    rows = fx["rows"]
    assert len(rows) > 2000
    raw = sum((p - y) ** 2 for p, _, y in rows) / len(rows)
    cal = [honest_calibrated_p(p, c) for p, c, y in rows]
    cal_b = sum((p - y) ** 2 for p, (_, _, y) in zip(cal, rows)) / len(rows)
    assert cal_b <= raw + 1e-9, (
        f"calibration degraded the shipped signal: cal {cal_b:.4f} > raw {raw:.4f}")


def test_composite_conf_from_p():
    assert composite_conf_from_p(0.5) == 0.0
    assert composite_conf_from_p(0.75) == 0.5
    assert composite_conf_from_p(None) == 0.0


# ── build_fused_p_fn returns the calibrated value ────────────────────────────
class _State:
    def p_ge(self, x):
        return 1.0


class _CDF:
    def p_high_ge(self, x, high_so_far=None):
        return 1.0


class _Traj:
    def p_high_ge(self, x, high_so_far):
        return 1.0


def test_p_fn_returns_calibrated_probability():
    b = _load_bridge()
    p_fn, meta_fn = b.build_fused_p_fn(_State(), _CDF(), _Traj(), 80.0, station="KBOS")
    p = p_fn(80.0)
    meta = meta_fn(80.0)
    assert p == meta["calibrated_p"], "p_fn must return the calibrated value"
    assert p <= meta["ceiling"] + 1e-9
    assert p < 0.99, "selection must never see the legacy saturation clip"
    assert "p_fused" in meta, "the raw fused value is preserved for the record"


def test_bridge_has_fail_closed_calibrated_guard():
    """Source contract: no entry without a usable calibrated_p."""
    import inspect
    b = _load_bridge()
    src = inspect.getsource(b.run)
    assert "no_calibrated_p_fail_closed" in src
    assert 'our_prob = (calibrated_p' in src, \
        "the estimate must be the calibrated probability, not the raw fused value"
