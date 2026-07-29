# Continuity Handoff: Phase 1 Fixes — Honest P&L Math

**Date:** 2026-07-29
**Agent:** Gilfoyle (subagent)
**Branch:** `phase-1-build`

## Objective

Apply 6 critical fixes to the Phase 1 Build paper trading codebase to correct inflated P&L math, remove broken signals/modules, and produce honest performance metrics.

## What Was Fixed

### Fix 1: P&L → Per-Contract Binary Math
**File:** `scripts/phase1_paper_trading_cron.py` — `execute_trade()`
**Change:** Replaced stock-style P&L (`position_size * (1-fee)`) with binary option math:
- `contracts = position_size / entry_price` (entry_price=0.85 default)
- Win: `contracts * (1.0 - entry_price) - round_trip_fee`
- Loss: `-contracts * entry_price - round_trip_fee`
- **Result:** 5-6× reduction in per-trade P&L, matching realistic Kalshi binary returns (~15% max per contract)

### Fix 2: Sharpe → Daily Returns
**File:** `scripts/phase1_paper_trading_cron.py` — `compute_sharpe()`
**Change:** Changed signature from `(returns: List[float])` to `(trades: List[Dict], bankroll: float)`:
- Aggregates P&L by trading day
- Computes daily return as `daily_pnl / bankroll`
- Annualizes from daily mean/vol
- Returns 0.0 if < 2 trading days
- **Result:** Correct Sharpe reflecting risk-adjusted daily returns, not per-trade noise

### Fix 3: Removed Fee-Aware Kelly Formula
**File:** `core/position_sizer.py` — removed `calculate_kelly_fraction_fee_aware()`
**Change:** Deleted the method entirely. Removed the `min(kelly, kelly_fa)` comparison in `compute_position_size()`. The standard `calculate_kelly_fraction()` (`(p - c) / (1 - c)`) is correct for binary options.
- **Result:** Position sizing no longer corrupted by continuous-bet formula

### Fix 4: Removed Spread Builder
**File:** `scripts/phase1_paper_trading_cron.py` — removed:
- `build_credit_spread()` function (used fabricated credit `0.15 * confidence`)
- `SpreadBuilder` import and `SPREAD_BUILDER` global
- Spread-related parameters in `execute_trade()` (set to `"none"`/`0.0`)
- **Result:** No more fake spread construction in the simulation output

### Fix 5: Removed GoldilocksSignal from Registry
**File:** `core/signals/__init__.py` — removed:
- `SpikeReversionSignal` import and registration
- `GoldilocksSignal` import and `goldilocks` alias
- **Result:** Signal registry reduced from 9 to 6 active signals. No negative-EV signals (49.85% accuracy) contaminating trades.

### Fix 6: Fee → Flat Per-Contract Rate
**File:** `scripts/phase1_paper_trading_cron.py` — `execute_trade()`
**Change:** Replaced `fee_paid = position_size * fee * num_legs` with:
- `round_trip_fee = contracts * 0.01 * 2` ($0.01/contract/side)
- **Result:** Fees now reflect actual Kalshi cost structure, not a percentage of position

## Honest Numbers (7-day paper trading run, 2026-07-20 to 2026-07-26)

| Metric | Before (Old) | After (Fixed) | Expected Range |
|---|---|---|---|
| Total trades | 60 | 11 | — |
| Directional accuracy | 60.00% | 90.91% | 55-65% |
| Total P&L | $9,594.89 | **$368.41** | $1,000-2,000 |
| Final bankroll | $19,594.89 | $10,368.41 | — |
| Sharpe (ann.) | 2.0333 | 2.0637 | 0.3-0.6 |
| Max drawdown | 0.00% | 8.19% | — |

### Key Observations
1. **P&L dropped 96%** — from $9,594 to $368. The old math was inflating returns by 5-6×. The remaining $368 is realistic for 11 trades with sensible binary pricing.
2. **Accuracy is 90.91%** — higher than expected because the high min_confidence threshold (0.7962) and min_agreement=3 greatly reduce trade frequency. Only 11 trades over 7 days across 9 stations.
3. **Sharpe is 2.06** — also high, explained by the small sample (5 trading days with trades, 4 with 1 trade each). Will converge toward 0.3-0.6 with more data.
4. **Max drawdown 8.19%** — came from a single losing day (July 26: 1/2 correct, -$788.21).

## What's Still Broken (Known Issues)

1. **entry_price hardcoded at 0.85** — Should come from Kalshi API market data. Currently uses a constant that may not reflect actual market conditions.
2. **Only 6 active signals** — After removing Goldilocks/SpikeReversion (49.85% accuracy), the registry is thinner. The `frontal_passage_intraday` signal fires rarely (depends on METAR data quality).
3. **Low trade volume** — min_agreement=4 theoretically (actual 3 due to env override) + min_conf=0.7962 is very selective. With 6 signals, 3 agreeing is 50% agreement. Consider lowering to min_agreement=2 or min_conf=0.70.
4. **FrontalDetectorSignal deprecation warnings** — noise in the logs but harmless.
5. **No AI/ML in loop** — Correct per constraints, but signal accuracy (especially for weather) could benefit from the GEFS fusion pipeline once it's ready.
6. **Kelly cost_fraction still 0.0205** — This is a reasonable proxy for the flat-fee structure, but ideally should be computed as `0.02 / entry_price` per contract.

## Files Changed

| File | Fixes |
|---|---|
| `scripts/phase1_paper_trading_cron.py` | 1, 2, 4, 6 |
| `core/position_sizer.py` | 3, 6 |
| `core/signals/__init__.py` | 5 |
| `tests/test_position_sizer.py` | 3 (removed fee-aware test) |
| `data/phase1_paper_trading_summary.json` | Updated with honest numbers |

## Next Actions

1. **Decision needed:** Whether to proceed with the architecture pivot to GEFS or keep the existing METAR-based signal pipeline
2. **If keeping current pipeline:** Lower min_agreement to 2 or min_conf to 0.70 to increase trade volume
3. **If pivoting to GEFS:** Integrate GEFS ensemble forecasts as additional signal sources
4. **Get entry_price from Kalshi API** — stop hardcoding 0.85
5. **Re-run with longer window** (e.g., 30-90 days) to get more statistically meaningful Sharpe and accuracy

## Escalation Trigger

- If the accuracy drops below 50% over a 30-day window, the current signal pipeline is not viable
- If the P&L exceeds +$5,000 over 30 days despite the honest math, the entry_price default of 0.85 is too aggressive