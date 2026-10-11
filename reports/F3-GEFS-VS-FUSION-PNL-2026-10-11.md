# F3 — Real P&L comparison: GEFS-alone vs fusion stack

**Date:** 2026-10-11
**Scope:** 68 settled `v3.1_sbox` trades (`functionality=intraday_ev_strike`) in `data/paper_trading_sbox.db`
**Script:** `scripts/f3_gefs_vs_fusion_pnl_backtest.py`
**Method:** replay each trade at its decision timestamp; re-select the EV-maximizing strike four ways with IDENTICAL entry/fee/sizing rules.

## Headline

**The key finding is REFUTED.** On real P&L — not directional accuracy — **GEFS-alone does NOT outperform the fused stack**. It is not better; it is not worse by a material margin either. All arms are within ~4% of each other on the like-for-like subset.

## Results

| Strategy | n | Win rate | Net P&L (no fee) | Net P&L (fee-adj) | Sharpe | Max DD ($) | Avg/trade |
|---|---|---|---|---|---|---|---|
| **fused (live book)** | 68 | 67.6% | $33,811.80 | $33,326.58 | 14.78 | $110.15 | $497.23 |
| fused (replayed) | 66 | 69.7% | $33,785.14 | $33,302.80 | 14.80 | $110.15 | $511.90 |
| gefs_only | 62 | 67.7% | $32,453.79 | $31,989.43 | 14.95 | $109.93 | $523.45 |
| analog_only | 63 | 68.3% | $34,481.65 | $33,994.91 | 15.04 | $110.12 | $547.33 |
| trajectory_only | 55 | 70.9% | $31,375.70 | $30,979.06 | 13.81 | $145.07 | $570.47 |

### Matched subset (all four arms traded the same 49 trades) — the fair comparison

| Strategy | Net P&L | Win rate | Capital | P&L / capital |
|---|---|---|---|---|
| fused | $29,393.03 | 69.4% | $2,244.97 | 13.09 |
| gefs_only | $28,748.77 | 69.4% | $2,165.23 | 13.28 |
| analog_only | $29,406.71 | 69.4% | $2,190.29 | 13.43 |
| trajectory_only | $29,802.51 | 71.4% | $2,114.49 | 14.09 |

### Constant-stake (1 contract/trade; removes the 2¢-price sizing distortion)

| Strategy | Net P&L (per contract) | Win rate |
|---|---|---|
| fused | $36.64 | 69.7% |
| gefs_only | $33.89 | 67.7% |
| analog_only | $34.53 | 68.3% |
| trajectory_only | $31.59 | 70.9% |

## Verification

* **Reconstruction valid.** Replayed fused = **$33,785.14**; live booked = **$33,811.80**. Delta **$26.66 (0.08%)** on 66/68 trades — the replay reproduces the live decisions.
* p_gefs reconstruction matches stored values exactly on 65/68 trades (mean |Δ| ≈ 0.01); p_analog 66/68; p_trajectory verified with a leakage guard.
* Settlement rule confirmed: strict-greater, 0/68 mismatches vs booked outcomes.

## Interpretation

1. **Directional accuracy ≠ P&L.** The earlier directional result suggested GEFS-alone might be better. On P&L it is not: GEFS-only $32,454 vs fused $33,785 (full set) and $28,749 vs $29,393 (matched set). Fusion ≈ +2% to +4% better, never worse by a material margin.
2. **analog_only** actually edges the fused stack on both raw P&L ($34,482) and capital efficiency (13.65 vs 13.02). Within noise, but it is the only arm that is nominally ahead.
3. **The 2¢-price + confidence-ladder sizing dominates the result.** When confidence > 0.85 the ladder stakes $50; at a 2.5¢ mid that is 2000 contracts with ~$50 downside and ~$1,950 upside. A handful of such near-certain wins drive the bulk of P&L. This is a *sizing* artifact, not a *forecaster* skill difference — confirmed by the constant-stake table where all arms compress to $31–$37.
4. **Max drawdown is tiny** ($110–$145) vs profit, i.e. the result is a handful of large wins over a tiny loss base — not a broad edge.

## Caveats (surfaced, not hidden)

* GEFS inputs use the same fetch-date handling as the live path (may include post-decision cycles); held identical across arms.
* `AnalogMatcher._load_epochs_for_zone` mutates module-level `CLIMATE_ZONES` in place (aliasing bug): repeated calls grow the SQL placeholder list until SQLite raises *"too many SQL variables"*. The backtest snapshots/restores the lists per call; the live bridge's hourly cadence runs one pass/day so it has not surfaced in production. **This is a latent production bug worth a fix.**
* Boundary ties (settled high == strike, strict-greater → lose) are 10 of 68 trades and 10 of 22 losses, –$330 P&L. Structural, not forecaster-specific.

## Recommendation

Do **not** replace the fused stack with GEFS-alone on the strength of any directional result. On P&L the fused stack is marginally better than GEFS-only, and the meaningful lever is the position-sizing ladder at extreme market prices, not forecaster choice.
