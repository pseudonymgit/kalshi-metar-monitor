# SPEC: Fix Fee Model (Cents Scaling + Orphan Definitions)

## Problem
`ceil(0.07 * contracts * price * (1-price))` returns cents, not dollars. At 1 contract @ $0.50, returns 1 (treated as $1.00) — correct value is $0.02. 50× error on small trades. 19 orphan definitions across codebase.

## Fix
1. Canonical `kalshi_fee()` in `scripts/sweep/config.py`: `ceil(0.07 * contracts * price * (1 - price)) / 100.0`
2. Round-trip: multiply by 2
3. All 27 files import from canonical, no orphan definitions
4. Remove `fee_deduction=[0,1]` LHS parameter — fees always applied

## Files to Modify
- `scripts/sweep/config.py` — canonical function
- `scripts/sweep/kalshi_sweep_eval.py` — import from config
- `scripts/sweep_metrics.py` — import from config
- `scripts/sweep_engine.py` — import from config
- `scripts/big_sweep.py` — import from config
- `core/position_sizer.py` — import from config
- `core/paper_trading_engine.py` — import from config
- `scripts/sweep/gefs_grid_sweep.py` — import from config
- `core/continuous_kelly.py` — import from config
- `scripts/sweep/old_signal_validation.py` — import from config
- All other files with orphan definitions

## Test
```python
assert kalshi_fee(1, 0.50) == 0.02  # 1 contract @ $0.50 = $0.02 round-trip
assert kalshi_fee(10, 0.50) == 0.18  # 10 contracts @ $0.50 = $0.175 → ceil → $0.18
```