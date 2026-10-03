#!/usr/bin/env python3
"""
Unit tests for the 15m timing overlay composition gate
(core/intraday/timing_overlay_gate.py).

Covers plan §5 composition policy:
  * agreement  -> 0.5*engine_conf + 0.5*sig.confidence folded into the engine
  * disagreement -> engine decision returned unchanged
  * silent (no trigger) -> engine decision returned unchanged
  * engine PASS -> unchanged (nothing to enrich)
  * confidence math is exact
  * flag is opt-in and reversible

Synthetic only: no DB reads, no AI/ML.
"""
from core.intraday.entry_engine import EntryTimingEngine, SignalType
from core.intraday.timing_15m import Timing15mSignal
from core.intraday.timing_overlay_gate import (
    OVERLAY_CONF_WEIGHT,
    OVERLAY_FLAG_ENV,
    evaluate_with_overlay,
    fold_confidence,
    overlay_enabled,
)


def _mk_signal(direction="UP", confidence=0.8, trigger="approach_up"):
    return Timing15mSignal(
        station="KNYC", timestamp="2026-06-15 16:00", trigger=trigger,
        direction=direction, confidence=confidence, high_so_far=79.0,
        strike=80.0, gap=1.0, rate_1h=1.2, vol_15m=0.6,
    )


def _engine_args(**over):
    base = dict(hour=16, hour_before_settlement=6,
                our_probability=0.62, market_price=0.55, confidence=0.6)
    base.update(over)
    return base


# ── Confidence math ──────────────────────────────────────────────────────────

def test_fold_confidence_is_fifty_fifty():
    assert fold_confidence(0.6, 0.8) == 0.5 * 0.6 + 0.5 * 0.8
    assert fold_confidence(0.0, 1.0) == 0.5
    assert OVERLAY_CONF_WEIGHT == 0.5


# ── Agreement fold ───────────────────────────────────────────────────────────

def test_agreeing_up_folds_confidence():
    engine = EntryTimingEngine()
    sig = _mk_signal(direction="UP", confidence=0.9)
    out = evaluate_with_overlay(engine, **_engine_args(), overlay_signal=sig)
    assert out.signal_type == SignalType.BUY
    assert out.confidence == 0.5 * 0.6 + 0.5 * 0.9


def test_agreeing_down_folds_confidence():
    engine = EntryTimingEngine()
    # our_prob < market -> SELL -> engine direction DOWN
    sig = _mk_signal(direction="DOWN", confidence=0.9, trigger="fade_down")
    out = evaluate_with_overlay(
        engine, **_engine_args(our_probability=0.40, market_price=0.55),
        overlay_signal=sig)
    assert out.signal_type == SignalType.SELL
    assert out.confidence == 0.5 * 0.6 + 0.5 * 0.9


def test_fold_does_not_change_sizing_or_direction():
    engine = EntryTimingEngine()
    raw = engine.evaluate_hour(**_engine_args())
    out = evaluate_with_overlay(
        engine, **_engine_args(), overlay_signal=_mk_signal(confidence=0.95))
    assert out.signal_type == raw.signal_type
    assert out.position_size == raw.position_size
    assert out.edge == raw.edge


# ── Disagreement / silent no-ops ─────────────────────────────────────────────

def test_disagreeing_overlay_is_noop():
    engine = EntryTimingEngine()
    raw = engine.evaluate_hour(**_engine_args())
    # engine BUY (UP) but overlay says DOWN -> unchanged
    out = evaluate_with_overlay(
        engine, **_engine_args(), overlay_signal=_mk_signal(direction="DOWN"))
    assert out.signal_type == raw.signal_type
    assert out.confidence == raw.confidence


def test_silent_overlay_is_noop():
    engine = EntryTimingEngine()
    raw = engine.evaluate_hour(**_engine_args())
    out = evaluate_with_overlay(engine, **_engine_args(), overlay_signal=None)
    assert out.signal_type == raw.signal_type
    assert out.confidence == raw.confidence
    assert out.position_size == raw.position_size


def test_engine_pass_stays_pass_even_with_agreeing_overlay():
    engine = EntryTimingEngine()
    # prob 0.50 -> below min_prob_gate -> PASS, regardless of overlay
    out = evaluate_with_overlay(
        engine, **_engine_args(our_probability=0.50, market_price=0.50),
        overlay_signal=_mk_signal(direction="UP", confidence=1.0))
    assert out.signal_type == SignalType.PASS


def test_disabled_flag_is_noop():
    engine = EntryTimingEngine()
    raw = engine.evaluate_hour(**_engine_args())
    out = evaluate_with_overlay(
        engine, **_engine_args(), overlay_signal=_mk_signal(confidence=0.99),
        enabled=False)
    assert out.confidence == raw.confidence


# ── Flag resolution (opt-in, reversible) ─────────────────────────────────────

def test_overlay_flag_env(monkeypatch):
    monkeypatch.delenv(OVERLAY_FLAG_ENV, raising=False)
    assert overlay_enabled() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv(OVERLAY_FLAG_ENV, v)
        assert overlay_enabled() is True
    for v in ("0", "false", "", "off"):
        monkeypatch.setenv(OVERLAY_FLAG_ENV, v)
        assert overlay_enabled() is False


def test_overlay_flag_explicit_beats_env(monkeypatch):
    monkeypatch.setenv(OVERLAY_FLAG_ENV, "1")
    assert overlay_enabled(False) is False
    monkeypatch.setenv(OVERLAY_FLAG_ENV, "0")
    assert overlay_enabled(True) is True
