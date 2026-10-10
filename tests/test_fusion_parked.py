#!/usr/bin/env python3
"""C2 (GR3 D4) — fusion stays PARKED: unwired, hard-asserted, documented.

The live backtest path must be a majority vote, not the LLOP/UWC chain. These
checks are source/signature level on purpose: they must hold independently of
whether the metar DB is reachable (C4 fixes its read-only open).

Run: python3 -m pytest tests/test_fusion_parked.py -v
     python3 tests/test_fusion_parked.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO = os.path.join(os.path.dirname(__file__), "..")


def _ub_src():
    return open(os.path.join(_REPO, "core", "unified_backtest.py")).read()


def test_fusion_hard_asserted_off():
    src = _ub_src()
    assert 'assert use_fusion is False, "fusion not wired"' in src, \
        "unified_backtest lacks the C2 fusion-parked assertion"


def test_use_fusion_defaults_false():
    import core.signal_fusion as sf
    import core.fusion_logic as fl
    sf.SignalFusionEngine = fl.SignalFusionEngine
    sf.TimeDecaySignalManager = fl.TimeDecaySignalManager
    import core.unified_backtest as ub
    sig = inspect.signature(ub.run_backtest)
    assert sig.parameters["use_fusion"].default is False, \
        "run_backtest.use_fusion must default False (fusion PARKED)"


def test_fusion_engine_never_constructed_on_live_path():
    src = _ub_src()
    # The only assignment to fusion_engine must be None; no live instantiation.
    assert "fusion_engine = None" in src, "fusion_engine not pinned to None"
    assert "fusion_engine = SignalFusionEngine(" not in src, \
        "live path still constructs a fusion engine"


def test_fusion_adapter_is_stub():
    path = os.path.join(_REPO, "core", "fusion_adapter.py")
    assert os.path.exists(path), "fusion_adapter.py missing"
    src = open(path).read()
    assert "PARKED" in src, "fusion_adapter does not document its parked state"
    assert "SignalFusionEngine(" not in src, "fusion_adapter wires a real engine"


def test_fusion_parked_prereqs_documented():
    src = _ub_src()
    # The blocking prerequisites must be named so a future wiring attempt is
    # forced past them.
    for token in ("n_eff", "rho", "LLOP", "D5", "D6", "D7"):
        assert token in src, f"C2 parked-prereq note missing token: {token}"


if __name__ == "__main__":
    test_fusion_hard_asserted_off()
    test_use_fusion_defaults_false()
    test_fusion_engine_never_constructed_on_live_path()
    test_fusion_adapter_is_stub()
    test_fusion_parked_prereqs_documented()
    print("test_fusion_parked: PASS")
