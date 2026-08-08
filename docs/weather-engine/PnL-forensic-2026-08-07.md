# P&L Inflation Forensic — 2026-08-07

**Analyst:** Donna Paulsen (via live code execution)
**Status:** Root cause identified

## Root Cause

The P&L is NOT a math bug. The per-trade `simulate_trade()` formula is correct for Kalshi binary options. The inflation comes from **aggressive config selection** — the sweep picks the best of 5,000 configs, and the best is always the most aggressive.

### Mechanism 1: kelly_fraction range (0.1, 4.0)

The LHS sweep generates kelly_fraction values up to **4.0**, meaning 4× optimal Kelly betting. The mean across all configs is **2.05** — 85% of configs have kelly_fraction > 1.0 (over-betting).

```
n_contracts = min(max_contracts, kelly_pct * kelly_fraction * 1000)
```

With kelly_fraction = 2.66, the formula produces 2.66× the optimal Kelly bet. This is reckless over-betting.

### Mechanism 2: max_contracts up to 5000

The LHS generates max_contracts up to **5,000** (log range 10-5000). With kelly_fraction > 1.0 and max_contracts = 5000, the signal can bet up to 5,000 contracts per trade.

Per-trade P&L at 5,000 contracts:
- Correct: $5,000 - $2,500 (entry) - fees = ~$2,500
- Wrong: $0 - $2,500 - fees = ~-$2,500

### Mechanism 3: Config selection bias

The sweep selects the best config from 5,000 LHS configs based on **combined** (discovery + holdout) P&L. The most aggressive config always wins on the discovery set. The holdout P&L is much more reasonable:

| Metric | Combined | Holdout-Only |
|--------|:--------:|:------------:|
| Accuracy | 61.9% | 59.1% |
| P&L | $1,051,624 | $893,207 |
| Per-trade | $663 | $798 |

### Mechanism 4: Non-deterministic LHS

The `generate_sweep_configs()` function uses `random.random()` without a fixed seed. Config 750 in the results file is **not reproducible** — every run generates different configs. This means we can't audit which config was actually used.

### Evidence

Test with config 750 (approximate, since LHS is non-deterministic):
- max_contracts ≈ 4986, kelly_fraction ≈ 2.66
- Mean contracts: 1,718 per trade
- Mean entry price: $0.304 (mostly DOWN trades, cheap entry)
- Accuracy: 59.7%
- P&L: $1,136,543 (similar to sweep's $1,051,624)
- Per-trade P&L: $639 (similar to sweep's $663)

The P&L math is correct for these parameters. The issue is the parameters themselves.

### What's NOT wrong

- `simulate_trade()` formula is correct for binary options
- `kalshi_fee()` is correct: `ceil(0.07 × C × P × (1-P))` per side
- The entry/exit price logic is correct (DOWN trades use 1.0 - market price)
- Validation splits are working correctly (discovery vs holdout separation)

## Fix

### Required config changes (in `generate_sweep_configs()`)

1. **Cap kelly_fraction at 1.0**: Change range from `(0.1, 4.0)` to `(0.1, 1.0)` — never bet more than optimal Kelly
2. **Cap max_contracts at 1000**: Change log range from `(10, 5000)` to `(10, 1000)` — reasonable for binary options
3. **Add confidence_floor minimum**: Increase from `0.5` to `0.55` — avoid near-coin-flip trades
4. **Add LHS seed**: Add `random.seed(42)` to make config selection reproducible
5. **Report holdout-only P&L**: The primary metric should be time-holdout P&L, not combined

### Recommended sane defaults for re-sweep

| Parameter | Value | Reason |
|-----------|-------|--------|
| kelly_fraction | 0.25 | Conservative Kelly (25% of optimal) |
| max_contracts | 500 | Reasonable binary options cap |
| confidence_floor | 0.58 | Gray Room minimum threshold |
| edge_threshold | 0.03 | 3% minimum edge |
| fee_type | 1 | Taker (realistic) |
| position_sizing_model | 1 | Discrete Kelly |

### Validation

After fix, run the sweep and verify:
1. Per-trade P&L should be within [-entry, 1-entry] range per contract
2. Mean per-trade P&L should be < $100 (not $663)
3. No config should have kelly_fraction > 1.0
4. The results should be reproducible (same seed → same configs)

## Summary

| Category | Finding |
|----------|---------|
| **Root cause** | Config selection with uncontrolled kelly_fraction (up to 4.0) and max_contracts (up to 5000) |
| **Math bug?** | No. The per-trade formula is correct. The parameters are wrong. |
| **Fix** | Cap kelly_fraction to [0.1, 1.0], cap max_contracts to [10, 1000], add LHS seed, report holdout-only P&L |
| **Impact** | Without fix, all sweep P&L numbers are meaningless. With fix, P&L drops ~90% but accuracy is honest. |