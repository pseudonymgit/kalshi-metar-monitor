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
    honest_calibrated_p, station_ceiling, composite_conf_from_p,
    ECE, BASE_RATE, POOLED_CEILING,
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge",
        os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── formula ──────────────────────────────────────────────────────────────────
def test_base_rate_absorbed_for_unknown_signal():
    # A raw 0.5 (coin flip) is pulled toward the base rate by ECE/2.
    v = honest_calibrated_p(0.5, 0.99)
    assert abs(v - (0.5 * (1 - ECE / 2) + BASE_RATE * ECE / 2)) < 1e-9


def test_ceiling_clamps_overconfident_signal():
    assert honest_calibrated_p(0.99, 0.7519) == 0.7519
    assert honest_calibrated_p(0.99, 0.79) == 0.79


def test_unknown_station_falls_back_to_pooled():
    assert station_ceiling("ZZZZ") == POOLED_CEILING
    # KBOS has a measured per-station ceiling in the refit artifact.
    assert station_ceiling("KBOS") < POOLED_CEILING


def test_nan_is_rejected():
    try:
        honest_calibrated_p(float("nan"), 0.8)
    except ValueError:
        return
    raise AssertionError("NaN raw probability must raise (fail closed)")


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
