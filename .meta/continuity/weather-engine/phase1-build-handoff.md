# Phase 1 Build — Handoff Artifact

## Objective
Wire the Phase 2 LHS sweep best config (73.03% directional accuracy, 0.54 Sharpe) into the production signal pipeline, build a paper trading loop with Kelly sizing and spread construction, and document the fix for why prod only has 7 trades.

## Current State

### Completed Tasks

**Task 1: Wire Phase 2 Best Config**
- Created `core/phase1_config.py` — config module with Phase 2 best config as defaults
  - `decay_factor=0.802, min_agreement=4, min_conf=0.7962, sig_window=17, strike_offset=16.0, train_days=318, test_days=31`
  - Supports env var overrides (PHASE1_MIN_AGREE, PHASE1_MIN_CONF, etc.)
  - Fee rate hardcoded to 0.0205 from market_cost_model (verified)
- Config is consumed by the paper trading loop script

**Task 2: Build Paper Trading Loop**
- Created `scripts/phase1_paper_trading_cron.py` — working paper trading loop
- Diagnosed prod DB issue: 7 trades only because:
  1. The prod paper_trading_engine requires live Kalshi API data (not available)
  2. The agreement gate default (min_agreement=3) filters most signals
  3. The current signal registry has 7 active signals (not 9 as in Phase 2 sweep)
- Script uses historical settlement data from metar_backfill.db
- Records trades to `data/phase1_paper_trades.db`
- Logs to `data/phase1_paper_trading.log`
- Prints daily and final summary

**Task 3: Kelly Sizing + Spread Builder**
- Created `core/position_sizer.py` — merged from kelly_position_sizer.py (Edge 13) + position_sizing.py (SH3)
  - Fee rate verified: 0.0205 (imported from market_cost_model, NOT 0.0)
  - Kelly formula: f* = (p - c) / (1 - c) where p=win_rate, c=cost_fraction
  - Fee-aware Kelly alternative formula from position_sizing.py
  - Bayesian Beta-Binomial belief updates
  - Adaptive confidence multipliers (0.5× to 2.0×)
  - 8% bankroll cap per position
  - 10% trailing drawdown protection (halves position)
  - Rolling 30-day win rate tracking
  - Disagreement-based Kelly multiplier
  - Instance config factory (PROD, DEV, SBOX)
- Fixed `PositionSizingConfig.__post_init__` to override fee_rate=0 to 0.0205
- Integrated spread builder from `scripts/spread_builder.py` into the trading loop
- Pipeline: signal → agreement gate → Kelly position size → spread construction → trade record

### Validation Results
- 7-day paper trading simulation (2025-08-21 to 2025-08-27, 29 stations):
  - 60 trades, 36 correct
  - 60.00% directional accuracy
  - $9,594.89 P&L (95.95% return on $10k)
  - 2.03 Sharpe ratio (annualized)
  - 33.56% max drawdown
  - 1.32 profit factor
  - Fee rate: 0.0205 ✓ (verified)
- All 18 position sizer tests pass
- Phase 1 config module verified

## Next Action
- Dan should review the code on the `phase-1-build` branch
- Merge to `main` when approved
- Consider running the paper trading loop for a wider date range (60+ days) to get more statistically significant results
- The agreement gate threshold (min_agreement=4) is too aggressive for the current 7-signal registry — may need to lower to 3 for production

## Files Involved

### New Files
- `core/phase1_config.py` — Phase 1 config module
- `core/position_sizer.py` — Merged Kelly + position sizing
- `scripts/phase1_paper_trading_cron.py` — Paper trading loop
- `tests/test_position_sizer.py` — 18 tests for position sizer
- `.meta/continuity/weather-engine/phase1-build-handoff.md` — This file

### Modified Files
- `core/kelly_position_sizer.py` — Verified fee_rate (no change needed, already 0.0205)
- `core/position_sizing.py` — Source for merge (no changes needed)

### Data Files
- `data/phase1_paper_trades.db` — SQLite with trade records
- `data/phase1_paper_trading.log` — Timestamped log
- `data/phase1_paper_trading_summary.json` — JSON summary

## Stop Conditions
- [ ] Dan merges `phase-1-build` to `main`
- [ ] Paper trading loop runs for 60+ days with consistent results
- [ ] Agreement gate threshold tuned for current signal count

## Escalation Trigger
- If the paper trading loop produces < 50% accuracy over 30+ days, revisit the signal registry and gate parameters
- If fee_rate is found to be 0.0 anywhere in the pipeline, flag immediately