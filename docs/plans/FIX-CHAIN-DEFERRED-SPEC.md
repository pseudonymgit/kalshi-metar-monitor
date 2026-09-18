# FIX-CHAIN-DEFERRED-SPEC — Items 4-6 (Deferred)

**Date:** 2026-08-13
**Status:** SPEC ONLY — not implemented. Items 1-3 (P&L wiring, holdout period, degenerate-signal exclusion) are implemented; these three are deferred until the high-priority items are validated in production.

---

## Item 4: Fix kelly_fraction to be real (~1h)

**Problem:** `kelly_fraction` is currently decorative. `compute_fusion_accuracy()` returns `"kelly_weighted_trades": round(total * kelly_fraction, 2)` (ws5_common.py, formerly line 583) — it just re-labels the trade count and has zero effect on P&L or position sizing.

**Current code:**
```python
"kelly_weighted_trades": round(total * kelly_fraction, 2),
```

**Target design (choose one):**

Option A — scale trade count (simple):
```python
kelly_multiplied_trades = min(total, int(total * kelly_fraction))
```
This mimics capital-constrained position sizing: at kf=0.25 you only take 25% of the trades (the highest-confidence ones, if ordered by confidence).

Option B — wire into P&L (recommended, richer):
- Keep all trades in accuracy reporting, but scale P&L by `kelly_fraction * kelly_edge`.
- `kelly_edge = net_pnl_per_trade / gross_notional_per_trade` (edge per $1).
- `kelly_adjusted_pnl = net_pnl * kelly_fraction` (already implemented in `compute_pnl_metrics()` — see Item 1).
- Additionally gate trade eligibility by confidence when kf < 1.0: take only the top `kf`-fraction of trades ranked by fused confidence.

**Interaction with Item 1:** `compute_pnl_metrics(predictions, fee_model, kelly_fraction)` already returns `kelly_adjusted_pnl = net_pnl * kelly_fraction`. The remaining work is Option A's trade-count scaling or Option B's confidence-ranked trade gating inside `compute_fusion_accuracy()`.

**Verification:** Two configs with different kelly_fraction must show different `kelly_adjusted_pnl` AND different realized trade counts (not just a relabeled count).

**Files:** `scripts/ws5_common.py` (compute_fusion_accuracy return block + compute_pnl_metrics), possibly `scripts/run_sweep.py` (top-5 output).

---

## Item 5: Fix asymmetric floors as sweep params (~1h)

**Problem:** `run_sweep.py` hardcodes `FusionModeConfig(mode="uwc", floor_type="none")` — asymmetric floors are never swept, so the floor machinery in `core/signal_fusion.py:90-113` is dead weight in the sweep path.

**Current code:**
```python
# run_sweep.py (both the per-config loop and the holdout block)
fconfig = FusionModeConfig(mode="uwc", floor_type="none")
```

**Target design:**
- Add sweep params to `generate_config_grid()` (and the CLI):
  - `floor_type`: `"none" | "symmetric" | "asymmetric"`
  - `floor_value`: 0.50–0.70 (step 0.05)
  - `floor_asymmetry_delta`: 0.15–0.30 (step 0.05); only used when `floor_type="asymmetric"`
- Build `FusionModeConfig(mode="uwc", floor_type=ft, floor_value=fv, floor_asymmetry_delta=fd)` per config.
- Grid explosion control: fix one axis at a time or use the existing LHS machinery in `scripts/sweep/config.py` (`LEVER_BOUNDS` already defines `floor_type`, `floor_value`, `floor_asymmetry_delta` bounds).
- `FusionModeConfig.__post_init__` (core/signal_fusion.py:110-127) already resolves floor params into `asymmetric_confidence_floor_up/down` — verify the resolution is correct for `"symmetric"` (currently sets both up/down to floor_value, correct) and `"none"` (disables, correct).

**Verification:** Sweep output shows floor params in top-5; at least one config with `floor_type="asymmetric"` appears and its eval/holdout numbers differ from `floor_type="none"` at equal other params.

**Files:** `scripts/run_sweep.py` (generate_config_grid, run_sweep loop, holdout block), `core/signal_fusion.py:90-113` (only if a resolution bug surfaces).

---

## Item 6: Start price snapshotting cron (~30min)

**Problem:** No Kalshi bid/ask history is captured. Backtests assume entry at `$0.50` with a flat fee; the market-baseline script (`scripts/run_market_baseline.py`) has no price source. Real backtest prices and a market baseline need actual order-book snapshots.

**Target design:**
- New script `scripts/snapshot_kalshi_prices.py`:
  - For the 20 stations in `scripts/sweep/config.py::STATIONS`, fetch the relevant Kalshi market's bid/ask/last for the next daily settlement (temperature-up market).
  - Store to `data/kalshi_prices.db` — table `price_snapshots`:
    - `station TEXT`, `market_ticker TEXT`, `target_date TEXT`, `bid REAL`, `ask REAL`, `last REAL`, `snapshot_utc TEXT`, `source TEXT`
  - Use the existing Kalshi API integration (see `KALSHI_API_INTEGRATION_PROGRESS.md`); fail soft on API errors (log + skip).
- 1 cron job (daily, e.g., 20:00 UTC — near settlement-relative hours; align with existing cron cadence, see `cron list` / TOOLS.md cron section). Model: `ollama/qwen2.5-coder:7b` (heartbeat/cron runner), payload `kind=agentTurn`, `sessionTarget="isolated"`, delivery announce.
- Update `scripts/run_market_baseline.py` to read from `data/kalshi_prices.db` instead of (or in addition to) live API calls.

**Verification:** After one cron tick, `data/kalshi_prices.db` contains ≥1 row per station; `run_market_baseline.py` reads the DB and prints a per-station bid/ask spread summary.

**Files:** `scripts/snapshot_kalshi_prices.py` (new), `scripts/run_market_baseline.py` (update), cron registration, `data/kalshi_prices.db` (new).

---

## Sequencing note

Defer Items 4-6 until:
1. Items 1-3 ship and the sweep with holdout (overfit gate) is stable across ≥3 runs.
2. Item 4 is the natural next step — it extends the P&L wiring already in place from Item 1 (kelly_adjusted_pnl).
3. Item 5 requires the floor grid to be validated against the holdout gate so floor params don't get overfit.
4. Item 6 is independent of 4-5 and can be scheduled as a cron as soon as API keys are confirmed working in the container.
