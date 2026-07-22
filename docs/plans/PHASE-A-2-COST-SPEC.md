# Phase A.2: Cost Model & Spread — Expert Specification

**Author:** Gilfoyle (subagent)  
**Date:** 2026-07-22  
**Status:** Draft — ready for Donna/Gerri routing  
**Source Audit Date:** 2026-07-22 15:53 UTC

---

## 1. Executive Summary

The current codebase has a critical disconnect: **the centralized `MarketCostModel` in `core/market_cost_model.py` correctly knows the mean spread is 3.1¢, but at least six separate call sites still hardcode 0.5¢** (the old assumption). Additionally, two separate `KellyPositionSizer` classes compete, neither uses the correct Kelly formula for binary-option markets, and nowhere is the fill price adjusted for the bid-ask spread at entry time.

This spec identifies every site that needs attention and prescribes exactly what belongs in a single source of truth.

---

## 2. Spread Assumption — The 0.5¢ Problem

### 2.1. What the centralized model says

`core/market_cost_model.py`, line 37:
```python
MEASURED_MEAN_SPREAD = 0.031  # 3.1¢ measured mean across all markets
```
The `MARKET_COST_MODEL` singleton (line 282) correctly uses this as its default spread.

### 2.2. Where 0.5¢ is still hardcoded

| File | Line(s) | Value | Context |
|---|---|---|---|
| `core/trade_execution.py` | 255 (comment) | 0.5¢ | Comment says "spread=0.5¢" — misleading |
| `core/trade_execution.py` | 723–725 (comment) | 0.5¢ | Docstring in `_compute_round_trip_cost` still says 0.5¢ |
| `core/multi_stage_execution.py` | 204–205 | 0.005 | Limit order priced at `midpoint - 0.005` |
| `core/multi_stage_execution.py` | 208, 212, 216–217 | 0.005 | Bid/ask offsets hardcoded to 0.5¢ |
| `core/multi_stage_execution.py` | 220 | 0.005 | Clamp floor uses 0.005 |
| `core/multi_stage_execution.py` | 275 | 0.005 | Stage price clamp |
| `core/multi_stage_execution.py` | 322 | 0.01 | Market order slippage 1¢ (should use dynamic model) |
| `core/spread_calibrator.py` | 63 | 0.005 | Floor clamp: `return max(0.005, spread)` |
| `core/spread_calibrator.py` | 75 | 0.005 | Floor clamp: `return max(0.005, ask - bid)` |
| `core/market_cost_model.py` | 38 | 0.005 | `DEFAULT_SLIPPAGE = 0.005` — this is *acceptable* as a slippage floor |

### 2.3. What to do

1. **Fix comments first** — `core/trade_execution.py` lines 255 and 723–725. The docstring and inline comments should say "spread from MarketCostModel" not 0.5¢. These are misleading documentation that will confuse the next engineer.

2. **Fix `core/multi_stage_execution.py`** — The limit-order price offsets (lines 204–220) should query `MARKET_COST_MODEL.get_bid_ask(ticker)` or `MARKET_COST_MODEL.spread` to compute the offset dynamically. A universal 0.5¢ offset is wrong:
   - For a 3.1¢-wide market, 0.5¢ below mid means you're offering 1.05¢ *below* the bid — leaving massive liquidity on the table.
   - For a 1¢-wide market, 0.5¢ below mid is at the bid — correct.
   - For a 10¢-wide market, 0.5¢ below mid is nowhere near the bid — you'll never fill.
   - **Recommendation:** offset = `spread / 4` (quarter-spread) as a starting point for limit orders, parameterized.

3. **Fix `core/spread_calibrator.py`** — The floor clamp of 0.005 is too low as the *observed* spread (Kalshi minimum is 1¢, mean is 3.1¢). Change floor from `0.005` to the measured minimum, e.g., `max(0.01, spread)` — but keep a minimum of 1¢, not 0.5¢.

4. **Fix `core/multi_stage_execution.py` line 322** — The "accept 1¢ worse" slippage should derive from `MARKET_COST_MODEL.estimate_slippage()`, not a hardcoded 1¢.

---

## 3. Fill Price — Spread Must Be Included at Entry

### 3.1. Current state

**Nowhere does the entry fill price account for the spread.** The `_compute_round_trip_cost` method in `core/trade_execution.py` (line ~740) returns a cost *fraction*, but the entry price used for P&L calculation is just the market mid-price or the raw price from the API. The `estimate_slippage` method in `market_cost_model.py` (line ~164) *does* model random fill within the spread, but:
- It returns a `SlippageEstimate`, not a fill price that gets plugged into the trade.
- The paper trader / execution engine ignores it at the fill-price level.

### 3.2. What needs to change

A **fill price function** that lives in `market_cost_model.py` and is the single source of truth:

```
fill_price(mid_price, spread, direction, is_taker) -> float
```

Logic:
- **Taker (market order):** direction=buy → `mid + spread/2` (pay ask); direction=sell → `mid - spread/2` (hit bid)
- **Maker (limit order):** direction=buy → `mid - spread/4` (or parameter); direction=sell → `mid + spread/4`
- **Stochastic variant** (optional, for paper trading realism): `uniform(mid - spread/2, mid + spread/2)` weighted by direction

### 3.3. Where fill price is consumed

| File | What needs to change |
|---|---|
| `core/trade_execution.py` — `_execute_trade()` or similar | Use `fill_price()` instead of raw `market_price` for entry cost |
| `core/fee_aware_filter.py` — `evaluate_entry()` | The `net_profit` calc uses `market_price * stake` — should use `fill_price` for the entry cost leg |
| All backtest scripts that hardcode `FEE_RATE = 0.05` (see §5) | Must use `round_trip_fraction()` from the cost model |

---

## 4. Position Sizing Systems — Which Should Win

### 4.1. The three systems

| # | File | Class | Approach | Fee Awareness |
|---|---|---|---|---|
| 1 | `core/position_sizing.py` | `KellyPositionSizer` | Kelly formula: `edge / (1-fee) / variance`, per-signal win tracking | fee_rate=0 (configurable), uses rolling 30d win rate |
| 2 | `core/fee_aware_kelly_position_sizing.py` | `KellyPositionSizer` | Kelly formula: `edge / variance` then `fee_rate * 2.0` adjustment, per-signal variance, direction-aware (long/short) | fee_rate=0, same window, has proper variance calc with `num_trades >= min_trades_for_kelly` |
| 3 | `core/fee_aware_filter.py` | `FeeAwareEntryFilter` | Pre-trade dollar-edge gate (not position sizing). EV-based rejection filter. | Correctly uses `MARKET_COST_MODEL.spread` |

### 4.2. Recommendation

**System #2 (`core/fee_aware_kelly_position_sizing.py`) should win**, with the following caveats and fixes.

Reasons:
1. **Superior variance estimation** — It computes sample variance from realized returns (`calculate_variance_estimate` with `min_trades_for_kelly=10` guard). System #1 falls back to Bernoulli variance `p*(1-p)`, which is too permissive.
2. **Direction-aware** — System #2 returns negative Kelly fractions for short opportunities and preserves them (doesn't clip to 0). This is critical for a market that supports both sides.
3. **Fractional Kelly** — Both implement it, but System #2's `1.0 + fee_rate * 2.0` denominator adjustment is more realistic.
4. **Cleaner dataclass config** — `KellySizingConfig` is a dedicated config object, not overloaded in `PositionSizingConfig`.

What System #2 needs to fix:

| Issue | Current | Fix |
|---|---|---|
| `calculate_variance_estimate` hardcodes `0.95` as win return | `net_return = 0.95` on win (line ~103) | Should use `1.0 - market_price` at entry as actual win return, not a fixed 0.95 |
| Fee adjustment is hardcoded in `compute_kelly_fraction` | `/ (1.0 + self.config.fee_rate * 2.0)` (line ~152) | Should be `/(1.0 + spread_cost_per_contract / market_price)` where spread_cost comes from MarketCostModel |
| No link to MarketCostModel | Standalone class | Constructor should accept an optional `cost_model: MarketCostModel` parameter |

### 4.3. System #1 should be deprecated

`core/position_sizing.py` has the older `KellyPositionSizer` that overlaps almost entirely with System #2. The `fee_aware_kelly_position_sizing.py` version is a superset. Add a deprecation note to `position_sizing.py`'s file header and route all callers to the winner.

### 4.4. Where callers route to sizing

Search for all imports of `KellyPositionSizer` and `compute_position_size` across the codebase and redirect to the winner:
- `tests/test_position_sizing.py` (tests both)
- `tests/test_edge_cases.py`
- Any backtest script calling `KellyPositionSizer`

---

## 5. Correct Kelly Formula for Kalshi Binary Options

### 5.1. Both current implementations are wrong

The current code uses the general continuous Kelly formula `f* = edge / variance`, which is appropriate for continuous-normal returns. **Kalshi binary options have binary outcomes** (YES/NO, settle at $1 or $0). The correct formula is the **binary Kelly criterion**:

```
For a YES position bought at price P:
    Win net = (1 - P) per contract
    Loss net = -P per contract
    Expected win probability = W

    f* = [W * (1-P) - (1-W) * P] / (1-P)
       = W - P/(1-P)

    where f* is the fraction of capital to allocate
```

For a NO position (selling YES, i.e., buying NO):
```
    Win net = P per contract (you get P back if NO resolves)
    Loss net = -(1 - P) per contract
    Expected NO probability = 1-W

    f* = [(1-W) * P - W * (1-P)] / P
       = (1-W) - (1-P)/P * W
```

### 5.2. Spread-adjusted Kelly

The spread makes the effective buy price higher and sell price lower:

```
For a taker YES position at mid=M, spread=S:
    Buy price = M + S/2
    Sell price = M - S/2  (if closing early; for hold-to-settle, only buy price matters)

    For hold-to-settlement:
        Win net = 1.0 - (M + S/2)
        Loss net = -(M + S/2)
        f* = [W * (1 - M - S/2) - (1-W) * (M + S/2)] / (1 - M - S/2)
```

### 5.3. Spread-adjusted edge for filter

For the `FeeAwareEntryFilter`, the spread cost per contract should be:

```
entry_cost = spread / 2   (half-spread for the entry leg)
EV = W * (1.0 - price - entry_cost) - (1-W) * (price + entry_cost)
```

This is **approximately what the filter does**, but it applies `spread * stake` as a flat cost rather than the correct half-spread. Fix: the filter should use `entry_cost = spread / 2` not `spread`.

### 5.4. Backtest scripts with wrong fee rates

These scripts hardcode `FEE_RATE = 0.05` (5% fee), which is wrong on two counts:
- Kalshi charges 0% commission (not 5%)
- The "fee" should be spread-based, not percentage-of-profit

| File | Line | Issue |
|---|---|---|
| `scripts/ensemble_v10_phase2.5.py` | 23 | `FEE_RATE = 0.05` |
| `scripts/per_station_skill.py` | 27 | `FEE_RATE = 0.05` |
| `scripts/ensemble_v10_with_real_integration.py` | 257 | `FEE_RATE = 0.05` |
| `scripts/ensemble_v9_phase2.py` | 29 | `FEE_RATE = 0.05` |
| `scripts/split_backtest_current.py` | 31 | `FEE_RATE = 0.05` |
| `scripts/ensemble_v10_edge23_integration.py` | 27 | `FEE_RATE = 0.05` |
| `scripts/sweep_late_day_thresholds.py` | 40 | `FEE_RATE = 0.001` (0.1%) — wrong but different |
| `scripts/split_backtest_hourly.py` | 74 | `fee_rate = 0.001` — wrong |
| `scripts/p7_final_backtest.py` | 42 | `KALSHI_FEE = 0.005` (0.5% — also wrong) |

All must import `MARKET_COST_MODEL.round_trip_fraction()` instead of hardcoding.

---

## 6. Centralized Cost Model — Single Source of Truth

### 6.1. What belongs in `MarketCostModel`

The model in `core/market_cost_model.py` already has the right structure. Here's the prescribed minimal API:

```python
class MarketCostModel:
    # State
    spread: float              # Fallback spread (currently 3.1¢ — correct)
    commission: float          # 0 for Kalshi — correct
    slippage: float            # 0.5¢ fallback — acceptable floor

    # Required methods
    round_trip_fraction()      # Returns spread/2 + slippage + commission — exists
    one_way_cost()             # Returns spread/2 + slippage — exists
    get_dynamic_spread(ticker) # Returns live bid-ask spread — exists
    get_bid_ask(ticker)        # Returns (bid, ask) — exists
    estimate_slippage(ticker, size, is_buy)  # Returns SlippageEstimate — exists
    estimate_total_cost(ticker, size, is_buy) # Returns dict — exists

    # NEEDED — see §3
    compute_fill_price(mid_price, spread_or_ticker, direction, is_taker) -> float

    # NEEDED — see §5
    compute_kelly_fraction(win_prob, market_price, ticker=None) -> float

    # NEEDED — see §5
    compute_spread_adjusted_edge(win_prob, market_price, ticker=None) -> float
```

### 6.2. How modules should call it

| Caller | Method to Call | Why |
|---|---|---|
| `fee_aware_filter.py:evaluate_entry()` | `compute_spread_adjusted_edge()` | Replace inline EV calc with centralized spread-adjusted edge |
| `trade_execution.py:_compute_round_trip_cost()` | Remove entirely, replace with `MARKET_COST_MODEL.round_trip_fraction()` | Already done at lines ~740-750 — but delete the docstring / comment with 0.5¢ |
| `multi_stage_execution.py` bid/ask offsets | `MARKET_COST_MODEL.get_bid_ask(ticker)` + `MARKET_COST_MODEL.spread / 4` | Replace `0.005` |
| `position_sizing.py` and `fee_aware_kelly_position_sizing.py` | `compute_kelly_fraction()` | Replace inline Kelly formula |
| All backtest scripts with `FEE_RATE` | `MARKET_COST_MODEL.round_trip_fraction()` | Replace hardcoded percentages |

### 6.3. What does NOT belong in MarketCostModel

- **Trade execution logic** (order placement, fill monitoring) — keep in `trade_execution.py`
- **Multi-stage execution strategy** (limit-vs-market, staging) — keep in `multi_stage_execution.py`
- **Signal confidence extraction** — keep in `position_sizing.py`
- **Contract selection** (D+1 vs D+2) — `contract_selection_optimizer.py` should *call* the cost model but not own it

---

## 7. Priority Order

1. **Fix `position_sizing.py` and `fee_aware_kelly_position_sizing.py` Kelly formula** using the binary-option formula from §5. This directly impacts position size correctness.
2. **Add `compute_fill_price()` to `MarketCostModel`** and refactor `trade_execution.py` to use it for entry P&L.
3. **Add `compute_spread_adjusted_edge()` to `MarketCostModel`** and refactor `fee_aware_filter.py` to use it.
4. **Replace hardcoded `0.005` in `multi_stage_execution.py`** with dynamic spread.
5. **Replace hardcoded `FEE_RATE = 0.05` in all backtest scripts** with `round_trip_fraction()`.
6. **Deprecate `position_sizing.py` `KellyPositionSizer`** with a module-level comment pointing to `fee_aware_kelly_position_sizing.py`.
7. **Fix misleading 0.5¢ comments** in `trade_execution.py` docstrings.

---

## 8. File:Line Reference Summary

| File | Lines | Severity | Fix |
|---|---|---|---|
| `core/trade_execution.py` | 255, 723-725 | **MEDIUM** | Fix comments (0.5¢ → from MarketCostModel) |
| `core/multi_stage_execution.py` | 204-205, 208, 212, 216-217, 220, 275, 322 | **HIGH** | Replace 0.005 with dynamic spread/4 |
| `core/spread_calibrator.py` | 63, 75 | **LOW** | Floor 0.005 → 0.01 |
| `core/market_cost_model.py` | 38 | **LOW** | `DEFAULT_SLIPPAGE = 0.005` — acceptable |
| `core/position_sizing.py` | Kelly formula everywhere | **CRITICAL** | Replace with binary-option Kelly |
| `core/fee_aware_kelly_position_sizing.py` | Kelly formula everywhere | **CRITICAL** | Same |
| `core/fee_aware_filter.py` | ~80-90 | **HIGH** | Fix half-spread vs full-spread usage |
| `scripts/ensemble_v10_phase2.5.py` | 23, 280-285 | **HIGH** | Replace `FEE_RATE = 0.05` |
| `scripts/per_station_skill.py` | 27, 274-279 | **HIGH** | Same |
| `scripts/ensemble_v10_with_real_integration.py` | 257-262 | **HIGH** | Same |
| `scripts/ensemble_v9_phase2.py` | 29, 323 | **HIGH** | Same |
| `scripts/split_backtest_current.py` | 31, 239-244 | **HIGH** | Same |
| `scripts/ensemble_v10_edge23_integration.py` | 27, 360-365 | **HIGH** | Same |
| `scripts/sweep_late_day_thresholds.py` | 40, 191 | **MEDIUM** | Replace `FEE_RATE = 0.001` |
| `scripts/split_backtest_hourly.py` | 74, 80 | **MEDIUM** | Replace `fee_rate = 0.001` |
| `scripts/p7_final_backtest.py` | 42 | **MEDIUM** | Replace `KALSHI_FEE = 0.005` |
| `core/contract_selection_optimizer.py` | 42, 228 | **LOW** | `D2_SPREAD_PENALTY` and estimated_cost already use 3.1¢ — correct |

---

## 9. Test Coverage Gaps

Current tests cover:
- `tests/test_position_sizing.py` — basic Kelly math, but uses the wrong formula
- `tests/test_edge_cases.py` — Kelly edge cases

Needed tests:
- Binary-option Kelly formula correctness (compare against reference implementation)
- Spread-adjusted fill price: `mid + spread/2` for taker buy, `mid - spread/2` for taker sell
- Integration test: `MarketCostModel.compute_spread_adjusted_edge()` output matches manual EV calculation
- Backtest scripts using `round_trip_fraction()` match the old hardcoded approach within 10%