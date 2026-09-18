# SWEEP-WIRE-SPEC: Wire All 42 Params Into Sweep

## Architecture Decision

**Chosen: Option (b) — Accept GEFS-only limitation, extend with member-level data.**

The sweep engine (`bmode_p1_backtest.py`) uses GEFS ensemble data as its single signal source. The 15-signal registry is consumed by the unified backtest path (`core/unified_backtest.py`). These are separate pipelines.

**Decision rationale:**
- GEFS has 31 member-level forecasts available in `gefs_archive.member_values` — this allows wiring `member_weighting`, `agreement_n/m`, and `fusion_mode` using member-level data
- Unified backtest path would be much slower (15 signals × 42 params × 40k samples) and is a separate pipeline
- Switch to unified backtest path would break the fast-mode sweep (355s → much longer)
- Member-level GEFS data provides a meaningful way to test agreement/fusion/member-weighting without switching pipelines

**Trade-off:** The 15-signal interaction effects (agreement gates across signals, fusion across signal families) are not tested in the GEFS-only sweep. These are tested by the correlation matrix and the unified backtest path separately.

## Params to Wire Into GEFS Path

### 1. `stop_loss_kind` [Gates]
- **Where:** `_sweep_run_backtest` trade execution loop
- **How:** After computing trade, simulate stop-loss exit:
  - `none` (0): no stop-loss
  - `fixed_pct` (1): exit at loss threshold (e.g., 50% of entry price)
  - `trailing` (2): exit when price drops X% from peak
- **Effect:** Reduces net_pnl on losing trades, may reduce max_drawdown

### 2. `position_sizing_model` [Levers]
- **Where:** `_sweep_run_backtest` sizing computation
- **How:** 
  - `fixed` (0): fixed contract count = max_contracts / 2
  - `kelly` (1): current kelly-based sizing
  - `fractional_kelly` (2): kelly × kelly_fraction
- **Effect:** Changes contract counts, affects PnL and drawdown

### 3. `fee_type` [Fees]
- **Where:** `_sweep_run_backtest` fee computation
- **How:**
  - `none` (0): no fees
  - `kalshi` (1): current kalshi_fee()
  - `forecastex` (2): 0.5¢ per contract
  - `polymarket` (3): 1% of notional
- **Effect:** Changes net_pnl calculation

### 4. `slippage_budget` [Fees]
- **Where:** `_sweep_run_backtest` entry/exit price
- **How:** Add slippage to entry_price and subtract from exit_price
- **Effect:** Reduces edge and PnL

### 5. `fee_deduction` [Fees]
- **Where:** `_sweep_run_backtest` fee computation
- **How:** Multiply total fees by fee_deduction factor
- **Effect:** Scales fee impact

### 6. `member_weighting` [Signals]
- **Where:** `_sweep_run_backtest` GEFS ensemble mean computation
- **How:**
  - `uniform` (0): simple average of member temps (current)
  - `accuracy` (1): weight by past accuracy (requires tracking)
  - `recency` (2): weight recent members higher
  - `combined` (3): combined accuracy + recency
- **Effect:** Changes ensemble mean → changes trades

### 7. `agreement_n`, `agreement_m` [Gates]
- **Where:** `_sweep_run_backtest` trade gating
- **How:** Require ≥agreement_n of agreement_m members to agree on direction. Members computed from `member_temps_c`.
- **Effect:** Gates trades when member agreement is low

### 8. `fusion_mode` [Modulators]
- **Where:** `_sweep_run_backtest` ensemble computation
- **How:**
  - `average` (0): simple mean of members (current)
  - `weighted` (1): weighted by member_weighting
  - `select_best` (2): use best-performing member
- **Effect:** Changes ensemble mean computation

## Params to Mark `validation_only` (6)

| Param | Reason | Available In |
|-------|--------|-------------|
| `kl_dro_lambda` | Training-time KL-DRO regularization; rules-based backtest has no training step | H10 harness only |
| `calibration_mode` | Only platt calibration implemented; ece_binned/beta/ensemble not available | H9 harness only |
| `goldilocks_lane_enabled` | Separate lane in production pipeline | H1 harness only |
| `trajectory_lane_enabled` | Separate lane in production pipeline | H2 harness only |
| `spatial_coherence_enabled` | Multi-station spatial coherence; proven to hurt accuracy | H4 harness only |
| `holdout_start_frac` | Validation split param; doesn't affect backtest accuracy | Level 4 validation only |

## Implementation Plan

### Phase 1: Wire `stop_loss_kind`, `position_sizing_model`, `fee_type`, `slippage_budget`, `fee_deduction`
- Add to `_sweep_run_backtest` execution logic
- These are in the trade execution path and directly affect PnL

### Phase 2: Wire `member_weighting`, `fusion_mode`, `agreement_n/m`
- Enhance `_sweep_run_backtest` to use member-level GEFS data
- Compute weighted ensemble mean based on member_weighting
- Apply agreement gate based on member votes
- Apply fusion mode to member computation

### Phase 3: Mark validation_only params
- Update `_acknowledged_zero_effect` → `_validation_only`
- These params are recorded in config but marked as validation-scope only

### Phase 4: Verification
- Run fast-mode sweep to confirm all 42 params registered
- Verify each wired param changes output