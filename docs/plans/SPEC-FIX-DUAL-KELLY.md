# SPEC: Fix Dual-Kelly in lever_manager.py

## Problem
`lever_manager.py` has 2 Kelly formulas and 2 cap regimes, silently switching path-dependently:
- Stage 1: `kelly_pct = edge / (1.0 - market_price)` — correct
- Stage 2: `variance_adjusted_kelly()` — different formula, different cap
- Path-dependent: same inputs can produce different outputs

## Fix: One Kelly, One Path
1. Remove `_stage2_variance_adjustment()` — variance adjustment is a separate multiplier, not a different Kelly
2. Keep `_stage1_kelly_base()` as the sole Kelly formula
3. Variance adjustment becomes a multiplicative modifier on position size, not on the Kelly formula itself
4. Remove path-dependent behavior — same inputs always produce same outputs

## Formula
```python
kelly_pct = edge / (1.0 - entry_price)  # binary options Kelly
kelly_frac = sweep_config.get("kelly_fraction", 0.5)
capital = self.state.capital  # tracked bankroll
position_size_dollars = kelly_pct * kelly_frac * capital
n_contracts = min(max_contracts, int(position_size_dollars / entry_price))
```

## Files to Modify
- `core/lever_manager.py` — consolidate to single path
- `core/continuous_kelly.py` — ensure consistent (already uses correct formula)

## Test
- Same inputs → same outputs every time
- edge=0.05, entry_price=0.10 → kelly_pct=0.0556, not 0.556
- edge=0.05, entry_price=0.50 → kelly_pct=0.10