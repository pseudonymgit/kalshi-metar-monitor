#!/usr/bin/env python3
"""
Regression test for the adaptive-thresholds import fix (2026-09-17).

`core/paper_trading_engine.py` imports `filter_signals_by_adaptive_threshold`
from `core/adaptive_thresholds`, but the symbol was missing after the recovery
lineage reconciliation. This test locks in that:

  * the symbol exists and is the entry-point alias of the registry's
    `filter_signals` behavior (no invented logic);
  * `core.paper_trading_engine` imports cleanly (no ImportError).

Usage:
    python3 -m pytest tests/test_adaptive_thresholds_import.py -v
"""


def test_filter_signals_by_adaptive_threshold_exists_and_delegates():
    from core.adaptive_thresholds import (
        filter_signals,
        filter_signals_by_adaptive_threshold,
    )

    # The missing symbol must now resolve.
    assert callable(filter_signals_by_adaptive_threshold)

    # It must delegate to the same underlying registry filtering as
    # module-level filter_signals (same output for same input, no journal
    # consumption).
    signals = [
        {"type": "metar_nowcast", "station": "KNYC", "confidence": 0.99},
        {"type": "metar_nowcast", "station": "KNYC", "confidence": 0.0},
    ]
    assert filter_signals_by_adaptive_threshold(signals, trade_journal=None) == \
        filter_signals(signals)


def test_paper_trading_engine_imports_cleanly():
    import core.paper_trading_engine  # noqa: F401  (would raise ImportError)
