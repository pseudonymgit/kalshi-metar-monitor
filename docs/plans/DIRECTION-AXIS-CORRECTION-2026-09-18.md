# Direction-Axis Correction — Strike-Crossing vs Day-Over-Day (v4, date-tiered)

**Date:** 2026-09-18
**Owner:** Gilfoyle (subagent, Priority 0)
**Repo:** `prototypes/weather-engine-source` (`temp-sprint-branch`, dirty tree preserved, staged for Dan's merge review — NOT committed)
**Reconciled against:** `docs/plans/S5-SOURCE-RECONCILIATION-2026-09-17.md` + Dan's refinements (authoritative weather truth + close the recency gap).

---

## 0. The authoritative model (Dan's refinement — TWO axes + DATE-TIERED ground truth)

1. **PREDICTION-ACCURACY axis** (what the signal/calibration scores against): a **DATE-TIERED source ladder** — because no single source covers the full recency range:
   - **Deep history (→ 2026-09-12):** GHCN daily high/low (`ghcn_settlements.db`) — authoritative weather truth, 6-day NOAA lag.
   - **Confirmed-recent (9/13 → 9/16):** Kalshi `source_type='finalized'` `kalshi_temp` — economic truth, 2-day lag.
   - **Freshest (9/13 → now):** WeatherAPI `max_temp_f` (`weatherapi_archive.db`) — best-available recency bridge, **NOT authoritative** (commercial re-analysis; reconcile later when GHCN catches up).
2. **P&L axis** (did the trade pay out): Kalshi `source_type='finalized'` — separate column, never the label.

For the strike-crossing label, the target is **date-tiered weather truth × the Kalshi strike list**. The METAR median proxy is still replaced. The strict `>`/`<` operators are unchanged (correct per Kalshi).

**Why date-tiered:** anchoring to GHCN alone leaves the newest markets (settling 9/13 → now) with NO ground truth — a 6-day gap during which Kalshi has already finalized (9/13–16) and only WeatherAPI is current (9/17–18).

## 1. The Bug (one sentence)

~15 files score signals against **warming-vs-yesterday** (`settlement_bucket > prior_settlement_bucket`) — and against a **METAR proxy temp** — when the correct ground truth for a HIGH/LOW win/loss is **strike-crossing** against the **date-tiered weather truth** (GHCN → Kalshi finalized → WeatherAPI) vs the **real Kalshi strike**.

## 2. Definitive File Map

### 2a. STRIKE-CROSSING, correct target (no change needed)

| File | Line(s) | Notes |
|---|---|---|
| `core/pnl_tracking.py` | 81, 83 | `settlement_temp > strike` (HIGH) / `< strike` (LOW). CORRECT. |
| `core/pnl_tracking.py` | 179-180 | `was_correct = (predicted_breach == contract_is_itm)`. CORRECT. |
| `core/paper_trading_engine.py` | 2526 | `settlement_contract_value > 0.5` (strike-derived). CORRECT. |
| `core/order_manager.py` | 304-453 | Strike ladder (`greater`/`less`/`between`). CORRECT. |
| `core/kalshi_monitor.py` | 225-264, 2273-2296 | Strike ladder extraction. CORRECT. |
| `core/market_monitor.py` | 133-260 | `_market_strike_value` / `_assemble_structured_snapshot_markets` — the **authoritative strike extractor**. (Referenced, not modified.) |

### 2b. DAY-OVER-DAY / WRONG-TEMP (fixed in this pass)

| File | Line(s) | Fix |
|---|---|---|
| `core/calibrate_all_signals.py` | 150 (median proxy) + `load_station_data` | Removed median-temp proxy; now reads `settlement_truth` (GHCN authoritative weather truth + real strike); strike-crossing direction; skip untrustworthy dates |
| `core/unified_backtest.py` | 276 + `load_station_data` | `settlement_truth` temp + strike; strike-crossing; skip untrustworthy |
| `core/paper_trading_engine.py` | 795, 812, 828 | SQL `> strike_price` on `settlement_truth` (temp + strike + trustworthy) |
| `core/signal_pipeline.py` | 86, 103, 119 | same `settlement_truth` SQL |
| `core/climatology_pillar.py` | 290-292 | strike-crossing when strike present, else day-over-day |
| `core/paper_test_controller.py` | 588-593 | Added `strike_price`/`strike_type`; `"flat"` = "did not cross strike"; `"unknown"` when no strike |
| `core/p3_backtest_engine.py` | 226-234 | `determine_direction` takes strike; strike-crossing first |
| `scripts/phaseB_calibration_pipeline.py` | 167 | `settlement_truth` + strike-crossing + skip untrustworthy |
| `scripts/phaseB_signal_benchmarks.py` | 107, 211 | `settlement_truth` + strike-crossing + skip untrustworthy |
| `scripts/basic_backtest.py` | 88 | `settlement_truth` strike-crossing SQL |
| `scripts/backtest_low_markets.py` | 29-30 | `settlement_truth` `< strike` (LOW) |
| `scripts/nwp_analog_ensemble.py` | 122, 137 | `settlement_truth` strike-crossing |
| `scripts/split_backtest.py` | 360, 788 | `get_actual_direction` gains strike param; strike-crossing (reads `settlement_epochs.strike_price`, day-over-day fallback retained for signal-feature `prior`) |
| `scripts/split_backtest_hourly.py` | 364, 783 | same |

### 2c. DAY-OVER-DAY but NOT a win/loss label (documented, lower priority)

| File | Line(s) | Classification |
|---|---|---|
| `core/order_manager.py` | 1144 | `_derive_attention_phrase` UI text ("NEW SETTLEMENT / NO REVERSION YET"). Not a target-variable bug. |
| `core/kalshi_monitor.py` | 791 | same UI phrase. |
| `core/decision_output.py` | 130, 134 | Reversion *prediction* heuristic. A signal, not ground truth. |

### 2d. Running-max artifact (documented, not re-fixed)

`core/p3_backtest_engine_v2.py:11` — predecessor compared `settlement_bucket > prior_settlement_bucket` which is "always 'up'" for a running daily max. Already identified as fundamentally broken; `v2` is corrected.

## 3. Shared Seam

New file `core/settlement_outcome.py` — single source of truth:

```python
strike_crossing_direction(settlement_bucket, strike_price, market_type) -> 'up'|'down'|None
contract_is_itm(settlement_bucket, strike_price, market_type) -> bool|None
```

Strict semantics (matches Kalshi): HIGH `>` strike = up; LOW `<` strike = down; tie = loss. Returns `None` when strike unknown → callers **skip**, never fall back to day-over-day.

## 4. Persisting the Real Settlement Truth + Strike (date-tiered)

New/updated script: **`scripts/migrate_add_strike_price.py`** (reconciliation-aware, date-tiered, idempotent, non-destructive).

What it does:
1. Adds `strike_price REAL` + `strike_type TEXT` to `settlement_epochs` (additive).
2. Builds `settlement_truth` side table in `metar_backfill.db` with the **two-axis + date-tiered** model:
   - `settlement_temp_f` = **date-tiered ground truth** (the label source):
     - `date <= 2026-09-12` → GHCN `high_f`/`low_f` (authoritative weather truth)
     - `2026-09-13 <= date <= 2026-09-16` → Kalshi finalized `kalshi_temp` (economic truth)
     - `date >= 2026-09-17` (and any gap) → WeatherAPI `max_temp_f`/`min_temp_f` (recency bridge)
   - `kalshi_temp_f` = Kalshi finalized temp (separate P&L-axis column, never the label).
   - `strike_price` + `strike_type` from `market_monitor`-style extraction (`greater`/`less`/`between`).
   - `trustworthy` flag = 0 for 2026-05-28 and 2026-08-10 (GHCN-vs-Kalshi disagreement dates).
3. Backfills `settlement_epochs.strike_price`/`strike_type` where a real strike exists.

**Result (executed):**
```
settlement_truth rows: 1,239,518
  temp_source=ghcn (deep history -> 2026-09-12): 1,238,681
  temp_source=kalshi_finalized (09-13 -> 09-16): 79
  temp_source=weatherapi (09-17 -> now, recency bridge): 758
  kalshi_temp_f populated (P&L axis, separate): 1,669
  with strike: 56
  untrustworthy (flagged): 84
settlement_epochs rows backfilled w/ strike: 56
```

**Tier boundaries (explicit):**
- `GHCN_MAX_DATE = "2026-09-12"` — GHCN is authoritative through this date (NOAA 6-day lag).
- `KALSHI_MAX_DATE = "2026-09-16"` — Kalshi finalized is available through this date (2-day lag).
- Beyond `KALSHI_MAX_DATE`, only WeatherAPI is current (through 2026-09-18 today) — the recency bridge.

**Recency gap now closed:** 2026-09-17 and 2026-09-18 (the freshest markets) previously had NO ground truth; they now carry WeatherAPI temp (40 rows/day = 20 stations × HIGH/LOW). These are flagged as recency-bridge (best-available, reconcile later).

### ⚠️ Critical data-coverage finding (unchanged)

The **strike list is not persisted historically anywhere in the repo.** `market_monitor` extracts strikes live, but only `market_snapshots` (2026-09-07/08, 20 stations) captured them. So the strike-crossing correction can only actually score the **56 rows** with a real strike; the rest correctly **skip** (no mislabeling) until the historical strike list is backfilled from Kalshi market metadata (§6).

## 5. Impact — How Many "Correct" Labels Flip

Measured on the 47 clean epochs that have a real Kalshi strike + GHCN authoritative weather temp (the only ground-truth strike data):

| Metric | Value |
|---|---|
| Day-over-day direction ≠ strike-crossing direction | **20 / 47 = 42.6%** |

**Over 40% of historical "correct" labels are on the wrong axis.** The flip is structural, not noise: a temp that rises vs yesterday but stays below strike → day-over-day `"up"` but HIGH market **lost** (`"down"`); a temp flat vs yesterday but crossing above strike → day-over-day `"down"` but HIGH **won** (`"up"`).

The 47-epoch sample is narrow (only the 2026-09 window has real strikes), so 42.6% is a *mechanism* demonstration, not a full-history rate. The full-history flip rate is unmeasurable until strikes are backfilled.

## 6. What's Next (blocked on data, not code)

1. **Historical strike backfill (required before re-scoring):** fetch the per-epoch Kalshi strike for the full history. Source: Kalshi trade API historical market metadata (`/markets/{ticker}`) — the strike is in the *market* ticker (`KXHIGHLAX-26JUL27-T80`), which is NOT currently persisted (`kalshi_settlements` stores only the *event* ticker). **Owner: Gilfoyle. Dan needed: decide whether to pull historical strikes via API or accept the 56-row partial coverage.**
2. **Re-run calibration/backtest after strike backfill.** The calibration-repair and calibration-bridge workers (`CALIBRATION-REPAIR-2026-09-18.md`, `CALIBRATION-BRIDGE-2026-09-18.md`) produced **pre-axis-fix** output and **must be re-run** once strikes are populated.
3. **Persist strike at trade time going forward** so this never recurs: `pnl_tracking._get_strike_price` already retrieves the strike live — it should persist it to `trades`/`settlement_epochs`, not just use it transiently.
4. **(Recency-bridge caveat):** WeatherAPI (`weatherapi` tier) is commercial re-analysis that won't exactly match Kalshi's settlement source. It is a *best-available bridge* for 9/17–18, to be **reconciled/replaced when GHCN catches up**. Do not treat it as authoritative.
5. **(S5 open decision, pre-existing):** whether `kalshi_settlements.source_type='historical_api'` (12,105 rows) is authoritative or proxy-only. This correction uses GHCN/Kalshi-finalized/WeatherAPI for the label and Kalshi `finalized` for P&L; `historical_api` is excluded per S5 recommendation.

## 7. Boundaries Honored

- ✅ No live trading. ✅ No destructive cleanup (additive schema + UPDATE backfill).
- ✅ Staged for Dan's review — nothing committed. ✅ Dirty tree preserved.
- ✅ Strict `>`/`<` operators untouched (correct).
- ✅ Settlement-epochs label repair (9/17) untouched.
- ✅ No new source invented — reused `ghcn_settlements` (authoritative weather truth), `kalshi_settlements.finalized` (P&L axis + confirmed-recent tier), `weatherapi_archive.daily` (recency bridge), and `market_monitor`'s existing strike extraction.

## 8. Files Touched (all staged, uncommitted)

- `core/settlement_outcome.py` (new)
- `core/calibrate_all_signals.py`, `core/unified_backtest.py`, `core/paper_trading_engine.py`, `core/signal_pipeline.py`, `core/climatology_pillar.py`, `core/paper_test_controller.py`, `core/p3_backtest_engine.py`
- `scripts/phaseB_calibration_pipeline.py`, `scripts/phaseB_signal_benchmarks.py`, `scripts/basic_backtest.py`, `scripts/backtest_low_markets.py`, `scripts/nwp_analog_ensemble.py`, `scripts/split_backtest.py`, `scripts/split_backtest_hourly.py`
- `scripts/migrate_add_strike_price.py` (new, reconciliation-aware)
- `data/metar_backfill.db` — `settlement_epochs` gained `strike_price`/`strike_type`; new `settlement_truth` side table (gitignored, local data change)
