#!/usr/bin/env python3
"""F4 wired (verification, 2026-10-13) — the EV/selection path must use calibrated_p.

The register asked whether entry selects on raw `p_fused` or the ceiling-clamped
`calibrated_p`. Trace of the wiring:

  * `multi_forecaster_fusion.fuse()` returns `calibrated_p` as its FIRST value and
    also stamps it into `meta["calibrated_p"]`.
  * `fuse_strike` packs the fusion dict as `{"p_fused": round(float(pf),4), **meta}`
    — i.e. the key literally named `p_fused` ALREADY carries the calibrated
    (ceiling-clamped) probability, and `calibrated_p` repeats it.
  * `build_fused_p_fn.p_fn` returns `_calc(x)["p_fused"]` — so `select_strike`
    prices EV off the calibrated probability, not the raw clip.

These tests pin that contract so a future refactor cannot silently reintroduce
selection on the un-calibrated value.

Run: python3 -m pytest tests/test_f4_calibration_gate.py -v
"""
import importlib.util
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from core.intraday.multi_forecaster_fusion import fuse, fuse_strike  # noqa: E402


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fuse_strike_p_fused_is_the_calibrated_probability():
    """The dict key 'p_fused' == calibrated_p (ceiling-clamped), and never saturates."""
    d = fuse_strike(lambda x: 1.0, lambda x: 1.0, lambda x: 1.0, 80.0)
    assert d["p_fused"] == d["calibrated_p"], \
        "the selection input (`p_fused` key) must already be the calibrated value"
    assert d["p_fused"] < 0.99, "the saturation clip must not reach selection"
    assert d["p_fused"] <= d["ceiling"] + 1e-9


def test_fuse_first_return_is_calibrated_and_matches_meta():
    cal, meta = fuse(0.99, 0.97, 1.0)
    assert cal == meta["calibrated_p"]
    assert cal <= meta["ceiling"] + 1e-9


def test_build_fused_p_fn_selection_uses_pfused_key():
    """Source contract: the p_fn handed to select_strike reads the calibrated key."""
    import inspect
    b = _load_bridge()
    src = inspect.getsource(b.build_fused_p_fn)
    assert 'return _calc(x)["p_fused"]' in src, \
        "selection probability must come from the fused/calibrated key"


def test_saturated_inputs_cannot_reach_099_in_selection():
    """F2 regression at the F4 boundary: legacy 0.99 saturation must be gone."""
    legacy_clip = 0.99
    d = fuse_strike(lambda x: 0.999, lambda x: 0.999, lambda x: 0.6, 80.0)
    assert d["p_fused"] != legacy_clip, "selection must not see the legacy 0.99 clip"
    assert d["p_fused"] == d["calibrated_p"]
