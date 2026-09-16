"""
P0 intraday core tests — EntryTimingEngine (Σ.1b).

Deterministic math only. No network, no DB. These tests pin the entry
decision cascade and Kelly sizing so a silent bug can't slip through
before paper trading.
"""

import pytest

from core.intraday.entry_engine import (
    DEFAULT_EDGE_THRESHOLD,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_POSITION_USD,
    DEFAULT_MIN_PROB_GATE,
    EntrySignal,
    EntryTimingEngine,
    RiskGuardrails,
    SignalType,
)


# ── RiskGuardrails ──────────────────────────────────────────────

def test_default_guardrails_are_within_clamp_bounds():
    g = RiskGuardrails()
    assert 0.5 <= g.min_prob_gate <= 0.95
    assert 0.0 <= g.edge_threshold <= 0.5
    assert 10.0 <= g.max_position_usd <= 10000.0
    assert 0.0 <= g.kelly_fraction <= 1.0


def test_guardrails_clamp_out_of_range_values():
    g = RiskGuardrails(
        min_prob_gate=0.99,   # above 0.95 clamp
        edge_threshold=2.0,   # above 0.5 clamp
        max_position_usd=1.0,  # below 10 clamp
        kelly_fraction=5.0,   # above 1.0 clamp
    )
    assert g.min_prob_gate == 0.95
    assert g.edge_threshold == 0.5
    assert g.max_position_usd == 10.0
    assert g.kelly_fraction == 1.0


# ── EntrySignal ────────────────────────────────────────────────

def test_entry_signal_is_entry_flag():
    yes = EntrySignal(
        signal_type=SignalType.BUY, position_size=1.0, edge=0.1,
        our_probability=0.6, market_price=0.5, confidence=0.7,
    )
    no = EntrySignal(
        signal_type=SignalType.PASS, position_size=0.0, edge=0.0,
        our_probability=0.5, market_price=0.5, confidence=0.7,
    )
    assert yes.is_entry is True
    assert no.is_entry is False


# ── EntryTimingEngine: decision cascade ────────────────────────

def test_buy_when_probability_above_market_and_edge_clears():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=0.62, market_price=0.55, confidence=0.7,
    )
    assert sig.signal_type == SignalType.BUY
    assert sig.is_entry is True
    assert sig.position_size > 0.0


def test_sell_when_probability_below_market():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=0.38, market_price=0.45, confidence=0.7,
    )
    assert sig.signal_type == SignalType.SELL
    assert sig.position_size > 0.0


def test_pass_when_edge_below_threshold():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=0.56, market_price=0.55, confidence=0.7,
    )
    # edge = 0.01 < DEFAULT_EDGE_THRESHOLD (0.03)
    assert sig.signal_type == SignalType.PASS
    assert sig.position_size == 0.0


def test_pass_when_probability_too_close_to_coinflip():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=0.51, market_price=0.40, confidence=0.7,
    )
    # prob deviation below min_prob_gate (0.55)
    assert sig.signal_type == SignalType.PASS
    assert sig.position_size == 0.0


def test_position_size_capped_at_max():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=0.99, market_price=0.01, confidence=0.9,
    )
    assert sig.signal_type == SignalType.BUY
    assert 0.0 < sig.position_size <= DEFAULT_MAX_POSITION_USD


# ── Kelly sizing ──────────────────────────────────────────────

def test_kelly_size_is_zero_at_no_edge():
    engine = EntryTimingEngine()
    kelly = engine._compute_kelly_size(our_probability=0.5, market_price=0.5)
    assert kelly == 0.0


def test_kelly_size_monotonic_in_edge():
    engine = EntryTimingEngine()
    small = engine._compute_kelly_size(our_probability=0.6, market_price=0.5)
    large = engine._compute_kelly_size(our_probability=0.8, market_price=0.5)
    assert large > small


def test_kelly_size_symmetric_buy_sell():
    engine = EntryTimingEngine()
    buy = engine._compute_kelly_size(our_probability=0.7, market_price=0.5)
    sell = engine._compute_kelly_size(our_probability=0.3, market_price=0.5)
    # Symmetric probabilities around 0.5 → symmetric Kelly
    assert abs(buy - sell) < 1e-6


def test_trading_hour_always_true():
    # Kalshi weather markets are 24/7 per the module docstring
    assert EntryTimingEngine._is_trading_hour(0) is True
    assert EntryTimingEngine._is_trading_hour(23) is True


# ── Clamping of inputs ─────────────────────────────────────────

def test_probabilities_are_clamped_to_unit_interval():
    engine = EntryTimingEngine()
    sig = engine.evaluate_hour(
        hour=14, hour_before_settlement=4,
        our_probability=1.5, market_price=-0.5, confidence=0.7,
    )
    assert 0.0 <= sig.our_probability <= 1.0
    assert 0.0 <= sig.market_price <= 1.0
