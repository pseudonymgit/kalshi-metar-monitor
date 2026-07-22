# Phase A — Expert Specification: Fix the Foundation (P&L Correctness)

**Author:** Market Micro / Quant Finance Specialist  
**Date:** 2026-07-22  
**Priority:** HIGHEST — P&L correctness is the foundation of all downstream decisions

---

## Table of Contents

1. [Settlement Price Fix — P&L Calculation](#1-settlement-price-fix)
2. [Spread & Fill Price Model](#2-spread--fill-price-model)
3. [Centralized Cost Model](#3-centralized-cost-model)
4. [Risk Controls Wiring](#4-risk-controls-wiring)
5. [Position Sizing — Which System Wins](#5-position-sizing)
6. [NwpDirectSignal Fix](#6-nwpdirectsignal-fix)
7. [Test Fixes — 4 Skipped Tests](#7-test-fixes)
8. [dual_polarity_signal.py Import Fix](#8-dual_polarity_signalpy-import-fix)
9. [Implementation Order](#9-implementation-order)

---

## 1. Settlement Price Fix

### 1.1 The Bug

**File:** `core/pnl_tracking.py`, function `process_settlements_for_date()`, lines 60–65

```python
if prior_settlement_value is not None:
    settlement_contract_value = 1.0 if settlement_value > prior_settlement_value else 0.0
else:
    # Fallback: compare against 50°F midpoint if no prior data
    settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0
```

**What's wrong:**
The code currently determines whether a trade wins by comparing today's observed temperature against yesterday's temperature (`settlement_bucket > prior_settlement_bucket`), or against a hardcoded 50°F fallback. This is direction prediction, not strike-based settlement.

Kalshi weather markets settle based on a **strike price** (e.g., 85°F). The payout is:
- **HIGH market** (e.g., KXHIGHKATL-240101-85): YES pays $1 if observed_max_temp > 85°F
- **LOW market** (e.g., KXLOWKATL-240101-30): YES pays $1 if observed_max_temp < 30°F

The current code doesn't use the strike price at all. It compares today's temp to yesterday's temp, which is a **direction** calculation, not a **settlement** calculation. These are structurally different:

| Scenario | Direction (today vs yesterday) | Strike (today vs 85°F) |
|---|---|---|
| Yesterday: 80°F, Today: 82°F, Strike: 85°F | UP (correct) | 82 < 85 → LOSE |
| Yesterday: 90°F, Today: 88°F, Strike: 85°F | DOWN (correct) | 88 > 85 → WIN |

The 50°F fallback is even worse — it's meaningless for any US station.

### 1.2 The Fix

#### Required data: store strike price on trade entry

The `trades` table needs a `strike_price` column (integer, °F). When `place_paper_trade()` runs, it must fetch the strike price from the market ticker metadata and store it alongside the trade.

**Source for strike price:** `core/market_monitor.py` already has `_market_strike_value()` (lines 133–154) that extracts strike from market data. Use this function (or the KalshiAPI market discovery endpoint) at trade entry time.

**DB schema change:**
```sql
ALTER TABLE trades ADD COLUMN strike_price INTEGER;
```

#### Correct settlement formula

For a **HIGH** market (HIGH = daily max temperature):
```
settlement_contract_value = 1.0 if observed_temp > strike_price else 0.0
```

For a **LOW** market (LOW = daily min temperature):  
```
settlement_contract_value = 1.0 if observed_temp < strike_price else 0.0
```

**New code in `process_settlements_for_date()`:**

```python
# Fetch strike_price from the trade record
strike_price = trade_row.get('strike_price')  # from trades table

if strike_price is not None:
    if market_type == 'HIGH':
        settlement_contract_value = 1.0 if settlement_value > strike_price else 0.0
    elif market_type == 'LOW':
        settlement_contract_value = 1.0 if settlement_value < strike_price else 0.0
    else:
        # Unknown market type — default to zero payout
        settlement_contract_value = 0.0
elif prior_settlement_value is not None:
    # Fallback: direction-based comparison (better than 50°F, still wrong but graceful)
    settlement_contract_value = 1.0 if settlement_value > prior_settlement_value else 0.0
else:
    # No strike, no prior — skip settlement (cannot determine outcome)
    continue
```

### 1.3 Backtest Impact

The `unified_backtest.py` (line 84) also uses direction comparison:
```python
'direction': 'up' if r[1] > r[2] else 'down',
```

This is the **backtest's ground truth label** — it determines whether the actual outcome was UP or DOWN. If the production system moves to strike-based P&L, the backtest must also shift to strike-based labels. However, the backtest currently lacks a strike price mapping.

**Recommendation:** Keep the backtest directional for now (it's a different comparison: direction prediction vs settlement). The backtest predicts "will temp go up/down?" and validates against `settlement_bucket > prior_settlement_bucket`. This is a valid prediction target. The P&L in the **paper trading engine** must use strike-based settlement because that's how Kalshi actually pays out. Align them by adding a strike_price column to the backtest context in a future phase.

### 1.4 Math — Correct P&L Formula

```
For a BUY_YES order at entry_price p on a HIGH market at strike K:

  If observed_temp > K:
    payout = $1.00 per contract
    P&L = (1.00 - p) × quantity
  Else:
    payout = $0.00 per contract
    P&L = (0 - p) × quantity = -p × quantity

For a BUY_NO order at entry_price p on a HIGH market at strike K:

  If observed_temp > K:
    payout = $0.00 per contract
    P&L = (0 - payment_for_no) × quantity = -(1-p) × quantity
  Else:
    payout = $1.00 per contract
    P&L = (1.00 - (1-p)) × quantity = p × quantity
```

Where `p` is the entry_price (the price paid for the YES contract, 0.0–1.0).

---

## 2. Spread & Fill Price Model

### 2.1 Current State

**File:** `core/cost_utils.py` — `get_cost()` returns a spread fraction but is **not called** by the trade execution or P&L pipeline.

**File:** `core/trade_execution.py` — `_compute_round_trip_cost()` (line 718) uses `MARKET_COST_MODEL.round_trip_fraction()` which returns `(spread/2) + commission + slippage`. The spread is hardcoded to `MEASURED_MEAN_SPREAD = 0.031` (3.1¢) via `market_cost_model.py`.

**File:** `core/market_cost_model.py` — `MarketCostModel` has a `get_dynamic_spread()` method and `estimate_slippage()` but these are **not wired into the trade execution path**.

### 2.2 Problem

- Spread is assumed at 0.5¢ in documentation but actual mean is 3.1¢
- Fill price doesn't include spread — the paper trade uses `market_price` as both entry and fill
- `cost_utils.py` is a dead module (imported nowhere)
- `market_cost_model.py` dynamic spread is not used in the fill price calculation
- Partial fills are not modeled

### 2.3 The Fix

#### 2.3.1 Fill Price Model

Replace the current fill price logic in `place_paper_trade()` (trade_execution.py, ~line 89):

**Current:**
```python
market_price = self._get_market_price(station, date, market_type)
# ... later used as fill_price = market_price
```

**New logic:**
```python
from .market_cost_model import MARKET_COST_MODEL

# Get live bid/ask (or fallback to measured mean spread)
ticker = self._construct_ticker(station, market_type, date)
depth = MARKET_COST_MODEL.get_market_depth_snapshot(ticker)

if depth is not None:
    bid = depth.yes_bid
    ask = depth.yes_ask
    mid = (bid + ask) / 2.0
else:
    # Fallback: use market_price with measured mean spread
    mid = market_price
    half_spread = MARKET_COST_MODEL.spread / 2.0
    bid = mid - half_spread
    ask = mid + half_spread

# Fill price depends on direction
if signal_direction == 'UP':  # Buying YES (going long temp)
    fill_price = ask  # Pay the ask
elif signal_direction == 'DOWN':  # Buying NO (going short temp)
    # When buying NO, the effective price = 1 - ask_price
    # But we can also model as buying NO at (1 - bid) or similar
    # Kalshi convention: NO price = 1 - YES price
    fill_price = 1.0 - bid  # You sell the YES at bid, effectively buying NO
else:
    fill_price = mid  # Fallback
```

#### 2.3.2 Spread Cost in P&L

The spread cost should be **subtracted from P&L at settlement time**, not added as a separate fee. The fill price already accounts for spread by paying the ask (buying) or receiving the bid (selling).

**Key formula for P&L with spread:**
```
# For BUY_YES at ask:
P&L = (settlement_payout - ask_price) × quantity

# For BUY_NO at (1 - bid):
# The effective price for NO = 1 - bid_price (since YES/NO sum to $1)
P&L = (settlement_payout_NO - (1 - bid_price)) × quantity
```

#### 2.3.3 Partial Fill Model

Add a `fill_quantity` field to the trade record. In `place_paper_trade()`:

```python
# Estimate fill fraction
slippage_est = MARKET_COST_MODEL.estimate_slippage(
    ticker=ticker,
    position_size_contracts=requested_quantity,
    is_buy=(signal_direction == 'UP')
)
fill_quantity = int(requested_quantity * slippage_est.expected_fill_fraction)
fill_price = slippage_est.expected_fill_price
```

This models market impact: larger orders relative to liquidity get partially filled, and the fill price is worse.

#### 2.3.4 Math — Spread & Fill Model

```
Let:
  s = bid-ask spread (measured mean = 0.031, i.e., 3.1¢)
  m = mid price = (bid + ask) / 2
  δ = slippage factor (function of order_size / market_depth)

For a BUY (long):
  fill_price = ask + δ = m + s/2 + δ
  expected_fill_cost = fill_price per contract

For a SELL (short/BUY_NO):
  fill_price = bid - δ = m - s/2 - δ
  expected_fill_credit = fill_price per contract

Round-trip cost (buy-and-hold-to-settlement):
  cost_per_contract = fill_price (buy) - settlement_payout
  Expected cost ≈ s/2 + δ (for buy), s/2 + δ (for sell)

For a $1 contract, the 3.1¢ spread means:
  Market maker buys at bid, sells at ask
  Taker pays ask (~51.55¢) for a contract worth 50¢ mid
  Taker receives bid (~48.45¢) when selling a contract worth 50¢ mid
  Net cost to taker per round-trip: ~3.1¢ (the spread)
```

---

## 3. Centralized Cost Model

### 3.1 Current State

Three conflicting cost models exist:

| Module | Purpose | Notes |
|---|---|---|
| `core/market_cost_model.py` | Centralized MarketCostModel | Has dynamic spread, slippage, depth — but **not wired** into trade execution |
| `core/cost_utils.py` | Standalone `get_cost()` | Dead code — imported nowhere |
| `core/position_sizing.py` | `fee_rate = 0.0` | Hardcoded zero, ignores spread |
| `core/trade_execution.py` | `_compute_round_trip_cost()` | Calls MARKET_COST_MODEL but doesn't use dynamic spread |
| `core/pnl_tracking.py` | Imports MARKET_COST_MODEL | Doesn't use it for settlement |

### 3.2 Architecture

**Single source of truth:** `core/market_cost_model.py` (already exists, already has the right design)

**What goes in it:**
- `MEASURED_MEAN_SPREAD = 0.031` — measured empirical mean (3.1¢)
- `DEFAULT_COMMISSION = 0.0` — Kalshi charges zero commission
- `DEFAULT_SLIPPAGE = 0.005` — 0.5¢ fallback slippage
- `MarketCostModel.round_trip_fraction()` — returns (spread/2 + commission + slippage)
- `MarketCostModel.get_dynamic_spread(ticker)` — live API fetch with fallback
- `MarketCostModel.estimate_slippage(ticker, contracts)` — depth-aware
- `MarketCostModel.estimate_total_cost(ticker, contracts)` — full cost estimate

**Remove `cost_utils.py` entirely** — it's dead code that adds confusion.

### 3.3 How Other Modules Should Call It

| Module | What to call | When |
|---|---|---|
| `trade_execution.py` | `MARKET_COST_MODEL.get_dynamic_spread(ticker)` | Getting fill price at trade entry |
| | `MARKET_COST_MODEL.estimate_slippage(ticker, qty, is_buy)` | Estimating fill quantity |
| | `MARKET_COST_MODEL.round_trip_fraction()` | Round-trip cost estimation |
| `pnl_tracking.py` | `MARKET_COST_MODEL.spread` | For P&L reconciliation metadata |
| `position_sizing.py` | `MARKET_COST_MODEL.round_trip_fraction()` | For edge calculation (replacing hardcoded fee_rate) |
| `fee_aware_kelly_position_sizing.py` | `MARKET_COST_MODEL.round_trip_fraction()` | For Kelly edge adjustment |
| All other modules | `from core.market_cost_model import MARKET_COST_MODEL` | Singleton import |

### 3.4 Deprecation Path

1. `cost_utils.py` → Delete (or mark as `raise DeprecationWarning`)
2. `position_sizing.py` `fee_rate` → Replace with `MARKET_COST_MODEL.round_trip_fraction()` call
3. `fee_aware_kelly_position_sizing.py` `fee_rate` → Same
4. All hardcoded `0.5` cent spread references → Replace with `MARKET_COST_MODEL.spread`

---

## 4. Risk Controls Wiring

### 4.1 Current State

**File:** `core/risk_controls.py` — Contains `RiskManager`, `RiskConfig`, `RiskState`, `TradeResult`, `check_kill_switches()`, and all the check infrastructure. This is a **solid implementation** with proper kill switches, daily loss limits, drawdown limits, and consecutive loss limits.

**File:** `core/paper_trading_engine.py` — Imports risk controls and wires them:
- `PaperTrader.__init__()` stores `self.risk_config`
- `PaperTrader.check_kill_switches()` calls `_check_kill_switches()`
- `daily_paper_run()` calls `check_kill_switches()` before trading
- `process_settlements_for_date()` in `pnl_tracking.py` already calls `self._risk_manager.update_after_trade()` (line ~152)

**Problem:** The risk manager is not consistently wired. The `RiskManager` is created but its state is not persisted across restarts, and the kill switches are checked only at the start of `daily_paper_run()` — not continuously during trade execution.

### 4.2 The Fix

#### 4.2.1 Wire RiskManager into place_paper_trade()

In `trade_execution.py:place_paper_trade()`, add a pre-trade risk check:

```python
# Before executing trade, check risk state
risk_state = self._risk_manager.evaluate()
if risk_state.halted:
    return {
        'status': 'skipped',
        'reason': f'Risk halt: {risk_state.halt_reason}',
        ...
    }
```

#### 4.2.2 Wire RiskManager after each trade

In `trade_execution.py:place_paper_trade()`, after successful execution:

```python
# Record trade in risk manager
trade_result = TradeResult(
    trade_id=trade_uuid,
    pnl=0.0,  # Realized P&L at trade entry is 0
    is_profitable=True,  # Neutral at entry
    trade_date=date
)
self._risk_manager.update_after_trade(trade_result)
```

#### 4.2.3 Persist risk state across restarts

Add a `risk_state` table to the paper trading DB:

```sql
CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY,
    current_capital REAL NOT NULL,
    peak_capital REAL NOT NULL,
    daily_pnl REAL NOT NULL DEFAULT 0.0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    losing_trades INTEGER NOT NULL DEFAULT 0,
    last_trade_date TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
```

Load on startup, save on every `update_after_trade()` call.

#### 4.2.4 Kill switch wire points

Kill switches should be checked at **three points**:
1. **Before trade entry** — `daily_paper_run()` → `check_kill_switches()` (already done)
2. **During trade entry** — `place_paper_trade()` → `_risk_manager.evaluate()` (new)
3. **After settlement** — `process_settlements_for_date()` → `_risk_manager.update_after_trade()` (already done)

#### 4.2.5 Risk config defaults

Current defaults in `RiskConfig` (line 30–40):
- `max_daily_loss_percentage: 0.05` → 5% of account per day
- `max_drawdown_percent: 0.15` → 15% from peak
- `max_consecutive_losses: 3` → 3 consecutive losses
- `initial_capital: 10000.0`

These are reasonable. The 3-consecutive-loss limit is conservative — consider increasing to 5 after Phase A stabilization.

---

## 5. Position Sizing

### 5.1 Current State — Three Conflicting Systems

| System | File | Approach |
|---|---|---|
| **System 1: `compute_position_size()`** | `core/position_sizing.py` | Confidence-weighted with Kelly. Uses `KellyPositionSizer` with `fee_rate=0.0`, `fraction_kelly=0.5`, `max_position_fraction=0.25` |
| **System 2: `KellyPositionSizer`** | `core/fee_aware_kelly_position_sizing.py` | Separate implementation of Kelly with `fraction_kelly=0.5`, `max_position_pct=0.25`, `window_days=30` |
| **System 3: `position_sizing.py` `compute_position_size()`** | `core/position_sizing.py` | Has `get_config_for_instance()` PROD/DEV/SBOX factory |

### 5.2 Which One Wins

**Winner: System 1 in `core/position_sizing.py`**

Reasons:
1. **More complete** — Has confidence tier classification, multilayered clamping, signal-specific confidence extraction
2. **Already has the correct Kelly formula** — `calculate_kelly_fraction()` uses `edge / variance` with fractional Kelly
3. **Has the PROD/DEV/SBOX config factory** — Ready for deployment
4. **System 2 (`fee_aware_kelly_position_sizing.py`) duplicates** — It's a near-copy with slightly different variable names. It adds nothing that System 1 doesn't have.

### 5.3 Correct Kelly Formula

The correct fractional Kelly formula for binary outcomes:

```
edge = 2 × win_rate - 1

variance = win_rate × (1 - win_rate)    # Bernoulli variance
or
variance = σ²(returns)                   # Historical variance of realized returns

kelly_fraction = edge / variance

fractional_kelly = kelly_fraction × fraction_kelly_factor

position_size = fractional_kelly × current_balance × confidence
```

Where:
- `win_rate` = 30-day rolling win rate
- `fraction_kelly_factor` = 0.5 (50% fractional Kelly for conservatism)
- `confidence` = signal confidence (0.0–1.0), modulates the Kelly stake
- `position_size` is capped at `max_position_fraction × balance` (25%)

**Edge adjustment for spread cost:**

```
edge_adjusted = edge - spread_cost_per_trade
```

Where `spread_cost_per_trade = MARKET_COST_MODEL.round_trip_fraction()` (≈ 0.0205 for 3.1¢ spread + 0.5¢ slippage).

### 5.4 What to Remove

**Delete `core/fee_aware_kelly_position_sizing.py`** — or gut it and re-export from `core/position_sizing.py`:

```python
# fee_aware_kelly_position_sizing.py → re-export only
from core.position_sizing import KellyPositionSizer, KellyPositionSizingConfig, compute_position_size
```

### 5.5 Config Consolidation

The `position_sizing.py` `KellyPositionSizer` (line ~80) uses `fee_rate=0.0` and `fraction_kelly=0.5`. Replace the hardcoded fee_rate with:

```python
from core.market_cost_model import MARKET_COST_MODEL
self.cost_fraction = MARKET_COST_MODEL.round_trip_fraction()
```

And use it in edge calculation:
```python
def calculate_kelly_fraction(self, edge, win_rate, signal_id='default'):
    # Apply spread cost adjustment
    adjusted_edge = edge - self.cost_fraction
    # ... rest of Kelly calculation
```

---

## 6. NwpDirectSignal Fix

### 6.1 The Bug

**File:** `core/signals/nwp_direct_signal.py`, line 48–52

```python
def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
    """Standard evaluate interface - opens DB connection and evaluates."""
    # This method is called by the backtest engine, but we need station context.
    # The base class evaluate_for_station calls this after loading days data.
    # Since NwpDirectSignal doesn't use historical days (it queries NWP DB),
    # this is a placeholder. Real callers should use evaluate_for_station().
    return None, 0.0
```

The `evaluate()` method is a **no-op** — it always returns `(None, 0.0)`. The `unified_backtest.py` calls `sig.evaluate(idx, days)` on all signals, but for `nwp_direct`, this always returns null, so the signal never fires in the backtest.

The `evaluate_for_station()` method (line 56+) works correctly — it queries the NWP forecasts DB — but the backtest engine doesn't call it.

### 6.2 The Fix

The `evaluate(idx, days)` method needs to work **without** station context (which is a design limitation of the backtest engine's interface). There are two approaches:

**Option A (Minimal fix):** Extract station from the `days` data.

```python
def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
    """Evaluate using NWP forecasts for the current station."""
    if not days or idx < 0:
        return None, 0.0
    
    # The backtest engine calls this per-station, so all days belong to one station
    # Extract station from the first day if available
    # The station is not in the days dict, so we need another approach.
    
    # Try to get station from the backtest context
    # This requires threading station through the evaluate call
    return None, 0.0  # Still broken without station context
```

**Option B (Structural fix):** Change the backtest engine to pass station context.

```python
# In unified_backtest.py, line 246:
direction, confidence = sig.evaluate(idx, days, station=station)
```

**Option C (Recommended):** Change `evaluate()` to accept an optional `station` parameter, and update the backtest engine to pass it.

In `nwp_direct_signal.py`:
```python
def evaluate(self, idx: int, days: list, station: str = None) -> Tuple[Optional[str], float]:
    if station is None:
        return None, 0.0  # Can't evaluate without station
    # Use the date from days[idx] and station to call evaluate_for_station
    date = days[idx]['date'] if idx < len(days) else None
    if date is None:
        return None, 0.0
    return self.evaluate_for_station(station, date, market_type='HIGH')
```

In `unified_backtest.py` (line 246):
```python
direction, confidence = sig.evaluate(idx, days, station=station)
```

**Note:** This requires updating the `evaluate()` signature across all signals, or using `**kwargs` for the optional station parameter. The cleanest approach is to add `**kwargs` to the base `evaluate()` signature in `base_signal.py`:

```python
def evaluate(self, idx: int, days: list, **kwargs) -> Tuple[Optional[str], float]:
    raise NotImplementedError
```

### 6.3 Data Return

When properly implemented, `NwpDirectSignal.evaluate()` should return:

```python
# Returns:
#   direction: 'up' or 'down' — predicted temperature direction
#   confidence: 0.0 to 1.0 — confidence based on model agreement and magnitude
```

The `evaluate_for_station()` method already does this correctly. The fix is just wiring it into the backtest path.

---

## 7. Test Fixes

### 7.1 The 4 Skipped Tests

The file `tests/test_signals.py` has no `@pytest.mark.skip` decorators, but some tests have conditional skips based on data availability. The tests that need function name corrections are likely in `test_position_sizing.py` referencing the `fee_aware_kelly_position_sizing.py` module.

**Analysis of `test_position_sizing.py`:**

The `TestFeeAwareKelly` class (lines 360–410) imports from `core.fee_aware_kelly_position_sizing`. If `fee_aware_kelly_position_sizing.py` is restructured/deleted, these tests will fail.

**The 4 tests that need correction:**

1. **`test_import`** (line 361) — Imports `KellyPositionSizer` and `KellySizingConfig` from `fee_aware_kelly_position_sizing`. Change to import from `position_sizing`:

```python
from core.position_sizing import KellyPositionSizer, PositionSizingConfig as KellySizingConfig
```

2. **`test_add_result_tracking`** (line 369) — Same import + uses `add_result()` instead of `add_win_result()`. Change to:

```python
sizer = KellyPositionSizer(KellySizingConfig())
# Replace add_result() with add_win_result() — the position_sizing module uses add_win_result
sizer.add_win_result("2025-06-01", True)
sizer.add_win_result("2025-06-02", False)
```

3. **`test_compute_kelly_fraction`** (line 381) — Uses `compute_kelly_fraction()` which takes `(edge, win_rate)` in `position_sizing.py` vs `(win_rate)` in `fee_aware_kelly_position_sizing.py`. Change:

```python
# position_sizing.py signature: calculate_kelly_fraction(edge, win_rate, signal_id)
edge = 2 * 0.6 - 1  # = 0.2
frac = sizer.calculate_kelly_fraction(edge, 0.6)
```

4. **`test_negative_edge_short_signal`** (line 393) — Same issue. Change to:

```python
edge = 2 * 0.4 - 1  # = -0.2
frac = sizer.calculate_kelly_fraction(edge, 0.4)
```

5. **`test_position_size_computation`** (line 405) — The `compute_position_size()` method exists in `fee_aware_kelly_position_sizing.py` but not in `position_sizing.py`. The `position_sizing.py` module has `compute_position_size()` as a **standalone function**, not a method. Fix:

```python
from core.position_sizing import compute_position_size, KellyPositionSizer, PositionSizingConfig
sizer = KellyPositionSizer(fee_rate=0.0, fraction_kelly=0.5, window_days=30)
size, tier, meta = compute_position_size(
    signal_type="persistence",
    confidence=0.8,
    current_balance=10000.0,
    market_price=0.5,
    kelly_sizer=sizer,
)
assert size >= 0
assert meta["confidence_tier"] in ("high", "medium", "low")
```

### 7.2 Exact Changes Summary

| Test | File | Line | Change |
|---|---|---|---|
| `test_import` | `test_position_sizing.py` | 361 | Import from `position_sizing` instead of `fee_aware_kelly_position_sizing`; use `PositionSizingConfig` |
| `test_add_result_tracking` | `test_position_sizing.py` | 369 | Use `add_win_result()` instead of `add_result()` |
| `test_compute_kelly_fraction` | `test_position_sizing.py` | 381 | Pass `(edge, win_rate)` instead of `(win_rate)` to match `position_sizing.py` signature |
| `test_negative_edge_short_signal` | `test_position_sizing.py` | 393 | Same as above — add edge parameter |
| `test_position_size_computation` | `test_position_sizing.py` | 405 | Use standalone `compute_position_size()` function with `kelly_sizer=` parameter |

---

## 8. dual_polarity_signal.py Import Fix

### 8.1 The Bug

**File:** `core/signals/dual_polarity_signal.py`, line 35

```python
from ..station_effects import get_wind_delta_t, is_warming_wind
```

This is a **relative import** using `..` to go up two directories. The `core/signals/` package is at `core/signals/`, and `station_effects.py` is at `core/station_effects.py`. The relative import `..station_effects` means:
- `..` → go up from `core/signals/` to `core/`
- `..station_effects` → `core.station_effects`

This should work if the package is installed correctly, but it fails when the module is run directly or when the Python path is not set up for relative imports.

### 8.2 The Fix

Change to an **absolute import**:

```python
from core.station_effects import get_wind_delta_t, is_warming_wind
```

This is the standard pattern used by other signal files in the same directory (e.g., `base_signal.py` imports `from ..station_effects` should also be checked).

**Check all files in `core/signals/` for relative imports:**

```bash
grep -rn "from \.\.\|from \." core/signals/ --include="*.py"
```

**Files that use relative imports:**

| File | Current | Should Be |
|---|---|---|
| `dual_polarity_signal.py:35` | `from ..station_effects import ...` | `from core.station_effects import ...` |
| (Check `base_signal.py` and others for similar issues) | | |

---

## 9. Implementation Order

### Priority 1: Settlement Price Fix (P&L Correctness)
**Files:** `core/pnl_tracking.py`, `core/trade_execution.py`
**Impact:** Every P&L calculation is wrong without this. Nothing else matters until this is fixed.

### Priority 2: Spread & Fill Price Model
**Files:** `core/trade_execution.py`, `core/market_cost_model.py`
**Impact:** P&L is still wrong without spread. The 3.1¢ spread is material — it's 3.1% of notional.

### Priority 3: Centralized Cost Model
**Files:** `core/market_cost_model.py` (almost done), `core/cost_utils.py` (delete), `core/position_sizing.py`, `core/fee_aware_kelly_position_sizing.py`
**Impact:** Prevents future cost model divergence.

### Priority 4: Risk Controls Wiring
**Files:** `core/trade_execution.py`, `core/risk_controls.py`, `core/pnl_tracking.py`
**Impact:** Risk controls exist but are not fully wired. This is a production safety issue.

### Priority 5: Position Sizing Consolidation
**Files:** `core/position_sizing.py` (keep), `core/fee_aware_kelly_position_sizing.py` (delete)
**Impact:** Code cleanup, eliminates duplicate logic.

### Priority 6: NwpDirectSignal Fix
**Files:** `core/signals/nwp_direct_signal.py`, `core/unified_backtest.py`
**Impact:** Unblocks a signal that's currently