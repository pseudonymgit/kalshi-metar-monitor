# Backtest Audit — Continuity Record

## Objective
Audit and fix the mismatch between what signals predict (directional change) and how `unified_backtest.py` validates accuracy (strike level).

## Current State
- **CONFIRMED**: Mismatch existed. All 22 signals predict `'up'`/`'down'` as directional change (today vs yesterday's temp), but unified_backtest.py validated against strike price level (settlement > median of training period).
- **FIX APPLIED**: Three files modified:
  1. `core/unified_backtest.py` — `load_station_data()` now tracks `prev_bucket`; `actual_direction` computed as day-over-day comparison
  2. `scripts/phaseB_combinatorial_search.py` — same fix
  3. `scripts/phaseB_calibration_pipeline.py` — same fix
- **VERIFIED**: Core 5 signals, all 20 stations produce:
  - agree=1: 61.08% accuracy, 27K trades, Sharpe 0.208
  - agree=2: 63.68% accuracy, 20K trades, Sharpe 0.275
  - agree=3: 65.54% accuracy, 12K trades, Sharpe 0.343

## Next Actions
1. Re-run Phase B calibration with the fixed target (isotonic regression will now fit to correct directional change)
2. Re-run Phase B combinatorial search to get meaningful agreement-gated results
3. Add `strike_accuracy` to `unified_backtest.py` as a separate metric for actual Kalshi market performance
4. Consider modifying signals to predict strike-based outcomes for production trading

## Files Involved
- `core/unified_backtest.py` — FIXED
- `scripts/phaseB_combinatorial_search.py` — FIXED
- `scripts/phaseB_calibration_pipeline.py` — FIXED
- `docs/plans/BACKTEST-AUDIT-FINDINGS.md` — Full audit writeup

## Stop Conditions
- [x] Audit complete — mismatch confirmed and documented
- [x] Fix implemented
- [x] Fix verified with backtest returning ~61-66% accuracy

## Escalation Trigger
- If accuracy drops below 55% after fix → check for other issues
- If seasonal_regime signal causes errors in ensemble → exclude from backtests
- If calibration pipeline needs re-run → requires ~30 min wall time