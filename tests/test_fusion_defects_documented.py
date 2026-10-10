#!/usr/bin/env python3
"""C3 (GR3 D5, D6, D7) — fusion composition defects stay documented + PARKED.

No behavior change: fusion remains unwired (C2). These checks assert the known
defects carry their documentation and the dangerous correction is still inert.

Run: python3 -m pytest tests/test_fusion_defects_documented.py -v
     python3 tests/test_fusion_defects_documented.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_REPO = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    return open(os.path.join(_REPO, rel)).read()


def test_d5_llop_weight_mismatch_documented():
    src = _src("core/fusion_logic.py")
    assert "D5" in src and "positional mismatch" in src.lower(), \
        "fusion_logic does not document the D5 LLOP weight positional mismatch"


def test_d7_confidence_not_probability_documented():
    src = _src("core/fusion_logic.py")
    assert "D7" in src and "confidence is a re-scaled score" in src.lower(), \
        "fusion_logic does not document D7 (confidence != probability)"


def test_d6_neff_rho_dead_field_documented():
    src = _src("core/signal_fusion.py")
    assert "D6" in src and "dead config field" in src.lower(), \
        "signal_fusion does not document the D6 dead n_eff/rho field"


def test_c3_header_marks_parked():
    for rel in ("core/fusion_logic.py", "core/signal_fusion.py"):
        assert "PARKED" in _src(rel), f"{rel} lacks the C3 PARKED header"


def test_fusion_still_unwired():
    # C3 must not have wired anything: C2's assertion must still hold.
    src = _src("core/unified_backtest.py")
    assert 'assert use_fusion is False, "fusion not wired"' in src, \
        "C3 changed the fusion wiring state"


if __name__ == "__main__":
    test_d5_llop_weight_mismatch_documented()
    test_d7_confidence_not_probability_documented()
    test_d6_neff_rho_dead_field_documented()
    test_c3_header_marks_parked()
    test_fusion_still_unwired()
    print("test_fusion_defects_documented: PASS")
