# Adaptive Thresholds Import Fix — 2026-09-17

**Branch:** `temp-sprint-branch`
**Status:** staged-for-review (not committed)

## Problem

`core/paper_trading_engine.py` could not load. Line 42 imports
`filter_signals_by_adaptive_threshold` from `core.adaptive_thresholds`, and
line 1249 calls `filter_signals_by_adaptive_threshold(signal_dicts, self._trade_journal)`.

But `core/adaptive_thresholds.py` never defined a symbol by that name. The
module defines:

- `AdaptiveThresholdRegistry.filter_signals(self, signals)` (method, line ~331)
- `get_adaptive_threshold(signal_name, station)` (module-level, line ~479)
- `filter_signals(signals)` (module-level, line ~490, delegates to registry)

No `filter_signals_by_adaptive_threshold` symbol existed, so the import raised
`ImportError` and the engine could not load.

## Root cause

A later-added call-site signature (the second `trade_journal` argument) was
never reconciled to a surviving symbol after the recovery-lineage reconciliation.
The current `filter_signals` implementation does not consume a trade journal —
the `_trade_journal` object is initialized "for adaptive thresholds (Phase 4.4)"
but is not actually read by the filtering logic.

## Fix

Added a single module-level symbol in `core/adaptive_thresholds.py`:

```python
def filter_signals_by_adaptive_threshold(signals: List[dict],
                                          trade_journal=None) -> List[dict]:
    """Filter signals by adaptive threshold (paper_trading_engine entry point)."""
    return get_registry().filter_signals(signals)
```

This is an alias of the module-level `filter_signals`, delegating to the
registry's existing `filter_signals`. The optional `trade_journal` argument is
accepted and ignored for backward compatibility with the call site — **no new
filtering logic was introduced, and no existing symbols were removed or altered**.

The stale import of `get_adaptive_threshold` on `paper_trading_engine.py` line 42
was left intact (it is unused but harmless); no import changes were needed
beyond the new symbol resolving.

## Verification

- `python3 -c "import core.paper_trading_engine"` → **succeeds** (no ImportError).
- `python3 -m pytest tests/test_adaptive_thresholds_import.py -v` → **passes**
  (2 tests: symbol delegation + clean import).

## Scope / stop conditions honored

- No filtering logic rewritten.
- No existing symbols deleted.
- No trading/backtests run.
- No data/DB touched.

## Receipt

- `commit=<staged only, not committed>`
- `clean=<not applicable — staged-for-review>`
- Files changed: `core/adaptive_thresholds.py` (add symbol),
  `tests/test_adaptive_thresholds_import.py` (add test),
  `docs/plans/ADAPTIVE-THRESHOLDS-IMPORT-FIX-2026-09-17.md` (this receipt).

## Owner / review trigger

- **Owner:** Gilfoyle (technical)
- **Review trigger:** Dan's merge approval (all merges through Dan).
