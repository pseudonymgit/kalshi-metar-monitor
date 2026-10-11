#!/usr/bin/env python3
"""
Intraday entry-engine integrity tests (Sprint C / E2-b, 2026-10-11).

Locks the fail-closed confidence rule: a missing/absent confidence must NEVER be
coerced to a mid (0.5) value — the same fail-open class the codebase has been
eliminating. An absent input becomes an explicit PASS (excluded), and the signal
carries `confidence=None` so callers can tell "absent" from "genuinely mid".

Also locks the overlay fold: an overlay trigger with an absent confidence cannot
be folded into a real-looking number (fold_confidence fails closed; the composed
decision is left unchanged).

Deterministic. No AI/ML. No DB access.
"""
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.entry_engine import (  # noqa: E402
    EntrySignal, EntryTimingEngine, SignalType,
)
from core.intraday.timing_overlay_gate import (  # noqa: E402
    evaluate_with_overlay, fold_confidence,
)


def _engine():
    return EntryTimingEngine()


def _args(**over):
    base = dict(hour=16, hour_before_settlement=6, our_probability=0.62,
                market_price=0.55)
    base.update(over)
    return base


# ─── the fail-closed sentinel (E2-b) ────────────────────────────────────────
def test_missing_confidence_is_fail_closed_pass():
    """The core bug: a missing confidence used to default to 0.5 (mid) and could
    still book a trade. It must now be excluded (PASS), not a mid-confidence
    entry."""
    sig = _engine().evaluate_hour(confidence=None, **_args())
    assert sig.signal_type == SignalType.PASS
    assert sig.position_size == 0.0
    assert not sig.is_entry
    # the absent input is surfaced as absent, never rewritten to 0.5
    assert sig.confidence is None


def test_missing_confidence_does_not_match_mid_default():
    """A missing input must NOT be indistinguishable from a real 0.5 confidence.

    With a genuinely-mid confidence (0.5) the engine still evaluates normally
    (edge gate decides); with an absent confidence it PASSes. If the sentinel
    were the old fail-open 0.5, these two would collapse to the same decision.
    """
    absent = _engine().evaluate_hour(confidence=None, **_args())
    present_mid = _engine().evaluate_hour(confidence=0.5, **_args())
    assert absent.signal_type == SignalType.PASS
    assert present_mid.signal_type != SignalType.PASS
    assert absent.confidence != present_mid.confidence


def test_non_finite_confidence_is_fail_closed():
    for bad in (float("nan"), float("inf"), float("-inf")):
        sig = _engine().evaluate_hour(confidence=bad, **_args())
        assert sig.signal_type == SignalType.PASS, bad
        assert sig.confidence is None, bad


def test_real_confidence_still_enters():
    """A present, finite confidence is unaffected: the entry still fires."""
    sig = _engine().evaluate_hour(confidence=0.7, **_args())
    assert sig.signal_type in (SignalType.BUY, SignalType.SELL)
    assert sig.position_size > 0.0
    assert sig.confidence == pytest.approx(0.7)


def test_confidence_is_clamped_not_sentineled():
    """Out-of-range finite confidence clamps (never becomes a sentinel/None)."""
    sig = _engine().evaluate_hour(confidence=1.7, **_args())
    assert sig.confidence == pytest.approx(1.0)


# ─── the overlay fold fails closed ──────────────────────────────────────────
def test_fold_confidence_rejects_absent():
    with pytest.raises(ValueError):
        fold_confidence(None, 0.7)
    with pytest.raises(ValueError):
        fold_confidence(0.7, None)
    with pytest.raises(ValueError):
        fold_confidence(float("nan"), 0.7)


def test_fold_confidence_real_values():
    assert fold_confidence(0.6, 0.8) == pytest.approx(0.7)


class _Sig:
    def __init__(self, direction, confidence):
        self.direction = direction
        self.confidence = confidence


def test_overlay_with_absent_confidence_leaves_engine_unchanged():
    """An agreeing overlay trigger whose confidence is absent must not fold a
    fake 0.5 — the raw engine decision is returned unchanged (fail-closed)."""
    engine = _engine()
    raw = engine.evaluate_hour(confidence=0.7, **_args())
    out = evaluate_with_overlay(
        engine, **_args(), confidence=0.7,
        overlay_signal=_Sig(direction="UP", confidence=None), enabled=True)
    assert out.signal_type == raw.signal_type
    assert out.position_size == pytest.approx(raw.position_size)
    assert out.confidence == pytest.approx(raw.confidence)


def test_overlay_with_real_confidence_folds():
    engine = _engine()
    raw = engine.evaluate_hour(confidence=0.7, **_args())
    out = evaluate_with_overlay(
        engine, **_args(), confidence=0.7,
        overlay_signal=_Sig(direction="UP", confidence=0.9), enabled=True)
    # 0.5*0.7 + 0.5*0.9 = 0.8
    assert out.confidence == pytest.approx(0.8)
    assert out.signal_type == raw.signal_type


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
