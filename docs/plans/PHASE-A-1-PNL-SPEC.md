# Phase A.1 — Settlement Price & P&L Fix: Expert Specification

**Focus:** Backtest P&L correctness (`core/unified_backtest.py`) and paper-trading settlement (`core/pnl_tracking.py`)
**Author:** Quant Finance / Market Micro
**Date:** 2026-07-22
**Priority:** HIGHEST — every downstream metric (Sharpe, drawdown, position sizing) depends on correct P&L

---

## Table of Contents

1. [Scope & Relationship to Phase A Spec](#1-scope)
2. [Bug 1: Hardcoded 50°F Fallback in Settlement](#2-bug-1-hardcoded-50f-fallback)
3. [Bug 2: Direction-Based Settlement Instead of Strike-Based](#3-bug-2-direction-based-settlement)
4. [Bug 3: Simplified P&L in Backtest Metrics](#4-bug-3-simplified-pl-in-backtest-metrics)
5. [Correct P&L Formula](#5-correct-pl-formula)
6. [Settlement Data Requirements](#6-settlement-data-requirements)
7. [Implementation Order](#7-implementation-order)
8. [Risk & Edge Cases](#8-risk--edge-cases)

---

## 1. Scope

This spec covers **P&L correctness** across two systems:

| System | File | Purpose |
|---|---|---|
| Paper trading engine | `core/pnl_tracking.py` | Real-time settlement of paper trades |
| Unified backtest | `core/unified_backtest.py` | Historical simulation for signal evaluation |

The existing `docs/plans/PHASE-A-EXPERT-SPEC.md` covers the broader Phase A refactor (spread model, cost model, risk controls, position sizing, etc.). This spec is a **deep dive on the settlement P&L algebra** — the foundation that everything else in Phase A depends on.

---

## 2. Bug 1: Hardcoded 50°F Fallback in Settlement

### Location

**File:** `core/pnl_tracking.py`, line 65

```python
# CURRENT (line 65):
settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0
```

### What It Does

When `prior_settlement_value` is `NULL` (no prior day's settlement bucket available), the code falls back to comparing the observed settlement temperature against **50°F**. This is a completely arbitrary threshold:

- `settlement_value > 50.0` → YES pays $1, P&L generates profit
- `settlement_value <= 50.0` → YES pays $0, P&L is a loss

### Why It's Wrong

Kalshi weather markets are strike-based, not fixed at 50°F. The payout depends on whether the observed temperature exceeds (HIGH market) or falls below (LOW market) the **contract's strike price** — which can be 70°F, 85°F, 35°F, etc.

The 50°F threshold is meaningless for any practical US station:
- **Miami (KMIA):** Median high is ~85°F — 50°F is always colder, so trades always win (always > 50°F)
- **Denver (KDEN):** Median high is ~65°F — 50°F is still almost always exceeded
- **Minneapolis (KMSP):** Median high in January is ~25°F — 50°F is always colder, so trades always lose

This introduces a **systematic bias** that makes the P&L completely unreliable.

### Impact

- **Every trade without a prior_settlement_bucket** gets a false settlement value
- At the start of a new station's trading history, the first day's settlement is always wrong
- The bias differs by station latitude, making cross-station comparison invalid
- All downstream metrics (Sharpe, drawdown, calibration) are corrupted

---

## 3. Bug 2: Direction-Based Settlement Instead of Strike-Based

### Location

**File:** `core/pnl_tracking.py`, lines 61–62

```python
# CURRENT (lines 61-62):
if prior_settlement_value is not None:
    settlement_contract_value = 1.0 if settlement_value > prior_settlement_value else 0.0
```

### What It Does

When `prior_settlement_value` exists, the settlement is determined by **direction**: whether today's temperature is higher than yesterday's. This is a **direction prediction** — not a **strike-price prediction**.

### Why It's Wrong

Kalshi weather contracts pay based on whether the observed temperature crosses a **specific strike price**, not whether it goes up or down from yesterday.

**Worked example:**

| Scenario | Yesterday | Today | Strike (85°F) | Direction (today > yest) | Strike (today > 85°F) |
|---|---|---|---|---|---|
| A | 80°F | 82°F | 85°F | UP ✓ | 82 < 85 → LOSE ✗ |
| B | 90°F | 88°F | 85°F | DOWN ✓ | 88 > 85 → WIN ✓ |
| C | 80°F | 86°F | 85°F | UP ✓ | 86 > 85 → WIN ✓ |
| D | 90°F | 84°F | 85°F | DOWN ✓ | 84 < 85 → LOSE ✗ |

In scenarios A and D, the direction-based settlement says WIN but the strike-based settlement says LOSE. The two metrics are **structurally different** and can be anti-correlated.

### Impact on the Backtest

**File:** `core/unified_backtest.py`, function `load_station_data()`, lines 157–158

```python
# CURRENT (lines 157-158):
market[r[0]] = {
    'direction': 'up' if r[1] > r[2] else 'down',
    ...
}
```

The backtest **ground truth labels** are direction-based. This means the backtest is training signals to predict "will tomorrow be warmer than today?" rather than "will tomorrow exceed the strike price?"

This is a **different prediction problem** — and it means the backtest accuracy cannot be directly mapped to Kalshi settlement P&L.

### Impact on P&L Metrics

**File:** `core/unified_backtest.py`

The `compute_sharpe()` function (line 84) and `max_drawdown()` function (line 120) use the direction-based correctness to compute P&L:

```python
# compute_sharpe (line 86):
gross = 2 * conf if ok else -2 * conf

# max_drawdown (line 130):
position = min(bet * conf, bankroll * 0.08)
if ok:
    bankroll += position * 0.95
else:
    bankroll -= position
```

These formulas assume:
- A correct prediction → fixed profit (2× confidence or 0.95× position)
- An incorrect prediction → fixed loss (−2× confidence or −1× position)

Neither formula accounts for:
1. **Strike price** — the payout threshold
2. **Entry price** — what you paid for the contract
3. **Spread** — the bid-ask cost
4. **Contract size** — $1 binary payout

---

## 4. Bug 3: Simplified P&L in Backtest Metrics

### Location

**File:** `core/unified_backtest.py`

| Function | Lines | Current Formula | Problem |
|---|---|---|---|
| `compute_sharpe()` | 84–92 | `gross = 2*conf if ok else -2*conf` | No strike, no spread, no entry price |
| `max_drawdown()` | 120–136 | `min(bet*conf, bankroll*0.08)` × 0.95/−1 | Hardcoded bet size, no strike, no spread |

### `compute_sharpe()` — Detailed Breakdown

**Current (lines 84–92):**
```python
def compute_sharpe(returns, fee_rate=FEE_RATE):
    if not returns:
        return 0.0
    vals = []
    for conf, ok in returns:
        gross = 2 * conf if ok else -2 * conf
        fee = fee_rate * conf
        vals.append(gross - fee)
    # ... compute mean/std Sharpe
```

**What's wrong:**

1. **`2 * conf` is arbitrary** — It assumes a correct prediction pays 2× the confidence, which has no relationship to actual Kalshi payout mechanics. Why 2? Why not 1 × conf, or conf²?

2. **No strike price** — The payout doesn't depend on how far the strike is from the observed temperature. A 90°F day with an 85°F strike is the same P&L as a 70°F day with a 65°F strike — both are "correct" but the entry prices would differ.

3. **No spread cost** — The 3.1¢ measured mean spread is never subtracted.

4. **No entry price** — The Sharpe assumes you always pay 0.50¢ (mid) for contracts, but the actual entry price depends on where the market is trading relative to the strike.

### `max_drawdown()` — Detailed Breakdown

**Current (lines 120–136):**
```python
def max_drawdown(results, initial=250.0, bet=10.0):
    bankroll = initial
    peak = bankroll
    max_dd = 0.0
    for pred, actual, conf in results:
        ok = (pred == actual)
        position = min(bet * conf, bankroll * 0.08)
        if ok:
            bankroll += position * 0.95
        else:
            bankroll -= position
```

**What's wrong:**

1. **`initial=250.0`** — This is a hardcoded initial bankroll that doesn't match the paper trading engine's $10,000 initial capital.

2. **`bet=10.0`** — Hardcoded bet size, not derived from Kelly or any position sizing model.

3. **`0.95` multiplier** — The 5% haircut on wins is a vague approximation of "costs" but doesn't match the actual 3.1¢ spread.

4. **No strike price** — Same issue as Sharpe.

---

## 5. Correct P&L Formula

### 5.1 Kalshi Binary Settlement Mechanics

Each Kalshi weather contract is a **binary option** that pays $1.00 if the condition is met, $0.00 if not.

For a **HIGH market** with strike price **K** (e.g., 85°F):
```
Payout = $1.00  if observed_max_temp > K
Payout = $0.00  if observed_max_temp ≤ K
```

For a **LOW market** with strike price **K** (e.g., 30°F):
```
Payout = $1.00  if observed_min_temp < K
Payout = $0.00  if observed_min_temp ≥ K
```

### 5.2 P&L for BUY_YES (Going Long)

```
Given:
  entry_price = price paid for the YES contract (0.0 to 1.0)
  strike = contract strike price (°F)
  observed_temp = actual observed temperature (°F)
  market_type = 'HIGH' or 'LOW'
  quantity = number of contracts

For HIGH market:
  if observed_temp > strike:
    payout = 1.0          # contract expires ITM
  else:
    payout = 0.0          # contract expires OTM

  P&L = (payout - entry_price) × quantity

For LOW market:
  if observed_temp < strike:
    payout = 1.0          # contract expires ITM
  else:
    payout = 0.0          # contract expires OTM

  P&L = (payout - entry_price) × quantity
```

### 5.3 P&L for BUY_NO (Going Short via YES)

```
Given:
  entry_price = price paid for the YES contract (e.g., 0.60)
  strike, observed_temp, market_type, quantity

  # Buying NO is equivalent to buying YES at (1 - entry_price)
  effective_no_price = 1.0 - entry_price

  For HIGH market:
    if observed_temp > strike:
      no_payout = 0.0      # YES pays $1, so NO pays $0
    else:
      no_payout = 1.0      # YES pays $0, so NO pays $1

  P&L = (no_payout - effective_no_price) × quantity
```

### 5.4 P&L with Spread

```
Given:
  bid = best bid price for YES contract
  ask = best ask price for YES contract
  mid = (bid + ask) / 2

  For BUY_YES:
    fill_price = ask                     # you buy at the ask
    realized_pnl = (payout - ask) × quantity

  For BUY_NO (sell YES at bid):
    fill_price = 1.0 - bid               # effective NO price
    realized_pnl = (no_payout - (1.0 - bid)) × quantity

  Spread cost per contract = ask - bid   # embedded in fill price
```

### 5.5 Corrected Backtest P&L

For the backtest, P&L per trade should be:

```
entry_price = 0.50  # default mid-price assumption (can be refined)
spread = 0.031      # measured mean spread (3.1¢)

For a correct prediction (pred == actual):
  # Trade was profitable — payout is $1, you paid entry_price
  gross_pnl = (1.0 - entry_price) × quantity  # = 0.50 × quantity
  net_pnl = gross_pnl - (spread / 2) × quantity  # subtract half-spread

For an incorrect prediction (pred != actual):
  # Trade lost — payout is $0, you paid entry_price
  gross_pnl = (0.0 - entry_price) × quantity  # = -0.50 × quantity
  net_pnl = gross_pnl - (spread / 2) × quantity  # still subtract half-spread
```

**Better: project ahead-of-time volatility from entry_price:**

```
  # entry_price reflects market-implied probability of temp > strike
  # If signal predicts UP and market_price is 0.65:
  #   entry_price = 0.65 (you pay 65¢ for a $1 contract)
  #   payout = $1 if correct, $0 if wrong
  #   P&L_correct = (1.0 - 0.65) × qty = 0.35 × qty
  #   P&L_wrong  = (0.0 - 0.65) × qty = -0.65 × qty
```

### 5.6 Corrected Sharpe Formula

```python
def compute_sharpe_strike_based(results, strike_price, entry_price=0.50, spread=0.031):
    """
    Compute Sharpe ratio using strike-based P&L.
    
    Args:
        results: list of (pred, actual, conf) tuples
        strike_price: the contract strike price
        entry_price: assumed entry price (default 0.50 = mid)
        spread: measured mean spread (0.031)
    
    Returns:
        Sharpe ratio (annualized, assuming 252 trading days)
    """
    if not results:
        return 0.0
    
    daily_returns = []
    half_spread = spread / 2.0
    
    for pred, actual, conf in results:
        is_correct = (pred == actual)
        if is_correct:
            gross_pnl = (1.0 - entry_price)
        else:
            gross_pnl = (0.0 - entry_price)
        net_pnl = gross_pnl - half_spread  # subtract half-spread per trade
        daily_returns.append(net_pnl)
    
    n = len(daily_returns)
    if n == 0:
        return 0.0
    
    mean = sum(daily_returns) / n
    var = np.var(daily_returns, ddof=1) if n > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    daily_sharpe = mean / std
    
    # Annualize: Sharpe × sqrt(252)
    annualized_sharpe = daily_sharpe * math.sqrt(252)
    
    return annualized_sharpe
```

### 5.7 Corrected Max Drawdown Formula

```python
def max_drawdown_strike_based(results, initial_capital=10000.0, entry_price=0.50, spread=0.031, max_position_pct=0.25):
    """
    Compute max drawdown using strike-based P&L.
    
    Args:
        results: list of (pred, actual, conf) triples
        initial_capital: starting bankroll ($10,000 to match paper trading)
        entry_price: assumed entry price per contract
        spread: measured mean spread
        max_position_pct: maximum position as fraction of capital
    
    Returns:
        max_drawdown as fraction (0.0 to 1.0)
    """
    bankroll = float(initial_capital)
    peak = bankroll
    max_dd = 0.0
    half_spread = spread / 2.0
    
    for pred, actual, conf in results:
        is_correct = (pred == actual)
        
        # Position size: conf-weighed, capped at max_position_pct of capital
        # In dollars: each contract costs entry_price, notional = entry_price * qty
        # We want: position_dollars = conf * bankroll * max_position_pct
        position_dollars = min(conf, 1.0) * bankroll * max_position_pct
        quantity = position_dollars / entry_price if entry_price > 0 else 0
        quantity = int(quantity)  # whole contracts
        
        if quantity <= 0:
            continue
        
        if is_correct:
            gross_pnl = (1.0 - entry_price) * quantity
        else:
            gross_pnl = (0.0 - entry_price) * quantity
        
        net_pnl = gross_pnl - (half_spread * quantity)
        bankroll += net_pnl
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    return max_dd
```

### 5.8 Summary of Formula Changes

| Metric | Old Formula | New Formula | Impact |
|---|---|---|---|
| `compute_sharpe` | `2×conf if ok, -2×conf if not` | `(1.0 - entry_price) - half_spread if ok, (0.0 - entry_price) - half_spread if not` | Eliminates arbitrary 2× scaling; adds spread cost |
| `max_drawdown` | `min(bet×conf, 0.08×capital) × 0.95/-1` | `conf × capital × 0.25 / entry_price × [(1.0-entry_price) - half_spread / (0.0-entry_price) - half_spread]` | Matches $10K capital, adds spread, removes arbitrary 0.95/0.08 |
| `compute_brier` | No change | No change | Brier is purely statistical, not financial |
| `compute_ece` | No change | No change | ECE is purely statistical, not financial |

---

## 6. Settlement Data Requirements

### 6.1 Current Data Limitations

**Table: `settlement_epochs`** (in `metar_backfill.db`)

```sql
CREATE TABLE settlement_epochs (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    market_type TEXT,             -- 'HIGH' or 'LOW'
    local_trading_date TEXT NOT NULL,
    settlement_bucket INTEGER NOT NULL,   -- observed temperature bucket (°F)
    prior_settlement_bucket INTEGER,       -- previous day's bucket
    settlement_jump_magnitude INTEGER,
    epoch_status TEXT NOT NULL,             -- 'closed', 'open'
    reversion_occurred INTEGER NOT NULL DEFAULT 0,
    first_reversion_timestamp_utc TEXT,
    max_excursion_above_settlement REAL NOT NULL DEFAULT 0,
    terminal_state_reached INTEGER NOT NULL DEFAULT 0
);
```

**What's missing:**
1. **No `strike_price` column** — the contract's strike price is not stored in this table
2. **No `ticker` column** — the Kalshi ticker (e.g., `KXHIGHKDEN-26JAN25-85`) is not stored
3. **No `entry_price` column** — the price paid for the contract is not stored in the settlement epoch

### 6.2 Required Data Schema Changes

**Add to `trades` table** (paper trading DB):

```sql
ALTER TABLE trades ADD COLUMN strike_price INTEGER;   -- e.g., 85 (°F)
ALTER TABLE trades ADD COLUMN ticker TEXT;              -- e.g., KXHIGHKDEN-26JAN25-85
ALTER TABLE trades ADD COLUMN entry_price REAL;         -- price paid (0.0-1.0), already exists as trade_price
```

**Add to `settlement_epochs` table** (metar DB):

```sql
ALTER TABLE settlement_epochs ADD COLUMN strike_price INTEGER;
```

### 6.3 Data Sources

**Strike price source:** Kalshi API market discovery endpoint.

Each Kalshi weather market has a ticker in the format:
```
KX{HIGH|LOW}{KALSHI_CODE}-{DATE}-{STRIKE}
```

Example: `KXHIGHKDEN-26JAN25-85` → strike = 85°F, station = KDEN, type = HIGH, date = 2026-01-25

The `core/market_monitor.py` function `_market_strike_value()` (line 133) already extracts the strike from market data. This should be called when a trade is placed.

**Entry price source:** The market price at the time of trade entry, stored as `trade_price` in the `trades` table.

### 6.4 Query for Settlement with Strike

**New query for settlement processing:**

```python
# Query to get settlement data WITH strike price
c.execute("""
    SELECT t.trade_uuid, t.station, t.trade_type, t.trade_price, t.market_price,
           t.quantity, t.strike_price,
           p.settlement_bucket, p.prior_settlement_bucket, p.market_type
    FROM trades t
    JOIN (SELECT station, market_type, local_trading_date, settlement_bucket, 
                 prior_settlement_bucket
          FROM metar.settlement_epochs
          WHERE epoch_status = 'closed' AND local_trading_date = ?) p
    ON t.station = p.station AND t.trade_date_utc = date(p.local_trading_date)
       AND t.market_type = p.market_type
    WHERE t.status = 'open'
      AND t.strike_price IS NOT NULL
""", (settlement_date,))
```

### 6.5 Kalshi Ticker Parsing

For backtest strike assignment, parse the ticker pattern:

```python
import re

TICKER_PATTERN = re.compile(
    r'^KX(?P<market_type>HIGH|LOW)'
    r'(?P<station>[A-Z]{4})'
    r'-(?P<date>\d{2}[A-Z]{3}\d{2,4})'
    r'-(?P<strike>\d+)$'
)

def parse_kalshi_ticker(ticker: str) -> dict:
    """Parse a Kalshi weather ticker into components.
    
    Example: KXHIGHKDEN-26JAN25-85 → {
        'market_type': 'HIGH',
        'station': 'KDEN',
        'date': '26JAN25',
        'strike': 85
    }
    """
    m = TICKER_PATTERN.match(ticker.upper())
    if not m:
        return None
    return {
        'market_type': m.group('market_type'),
        'station': m.group('station'),
        'date': m.group('date'),
        'strike': int(m.group('strike')),
    }
```

---

## 7. Implementation Order

### Step 1: Fix the 50°F Fallback in `pnl_tracking.py`

**File:** `core/pnl_tracking.py`, lines 61–65

**Change:**
```python
# OLD (wrong):
if prior_settlement_value is not None:
    settlement_contract_value = 1.0 if settlement_value > prior_settlement_value else 0.0
else:
    settlement_contract_value = 1.0 if settlement_value > 50.0 else 0.0

# NEW (strike-based):
strike_price = trade_row.get('strike_price')  # from trades table

if strike_price is not None:
    if market_type == 'HIGH':
        settlement_contract_value = 1.0 if settlement_value > strike_price else 0.0
    elif market_type == 'LOW':
        settlement_contract_value = 1.0 if settlement_value < strike_price else 0.0
    else:
        settlement_contract_value = 0.0
else:
    # No strike available — cannot settle, skip this trade
    continue
```

**Note:** The `continue` on missing strike is intentional — it's safer to skip settlement than to settle with a wrong value. This creates a visible gap (unsettled trades) that must be addressed.

### Step 2: Store Strike Price at Trade Entry

**File:** `core/trade_execution.py`, function `place_paper_trade()`

**Changes:**
1. Add DB migration: `ALTER TABLE trades ADD COLUMN strike_price INTEGER;`
2. At trade entry, resolve the market's strike price from Kalshi API or ticker parsing
3. Store `strike_price` in the trade record

**Strike resolution logic:**
```python
def resolve_strike_price(station: str, market_type: str, trade_date: str) -> Optional[int]:
    """Resolve the strike price for a trade from market data."""
    # Priority 1: Kalshi API market discovery
    markets = kalshi_api.get_markets_for_station(station, market_type, trade_date)
    if markets and len(markets) == 1:
        strike = _market_strike_value(markets[0])
        if strike is not None:
            return int(strike)
    
    # Priority 2: Parse from cached ticker
    ticker = _construct_ticker(station, market_type, trade_date)
    parsed = parse_kalshi_ticker(ticker)
    if parsed and parsed['strike']:
        return parsed['strike']
    
    # Priority 3: Fallback to settlement_epochs data
    # (only if the epoch has been closed and we know the bucket)
    return None  # Cannot determine — skip trade
```

### Step 3: Fix `compute_sharpe()` in `unified_backtest.py`

**File:** `core/unified_backtest.py`, lines 84–92

**Change the function signature and implementation:**

```python
def compute_sharpe(returns, entry_price=0.50, spread=0.031):
    """Compute Sharpe ratio from (pred, actual, conf) pairs using strike-based P&L."""
    if not returns:
        return 0.0
    vals = []
    half_spread = spread / 2.0
    for conf, ok in returns:
        if ok:
            net_pnl = (1.0 - entry_price) - half_spread
        else:
            net_pnl = (0.0 - entry_price) - half_spread
        vals.append(net_pnl)
    n = len(vals)
    if n == 0:
        return 0.0
    mean = sum(vals) / n
    var = np.var(vals, ddof=1) if n > 1 else 0.01
    std = math.sqrt(var) if var > 0 else 0.01
    daily_sharpe = mean / std if std > 0 else 0.0
    return daily_sharpe * math.sqrt(252)  # annualize
```

### Step 4: Fix `max_drawdown()` in `unified_backtest.py`

**File:** `core/unified_backtest.py`, lines 120–136

```python
def max_drawdown(results, initial_capital=10000.0, entry_price=0.50, 
                 spread=0.031, max_position_pct=0.25):
    """Compute max drawdown using strike-based P&L."""
    bankroll = float(initial_capital)
    peak = bankroll
    max_dd = 0.0
    half_spread = spread / 2.0
    
    for pred, actual, conf in results:
        ok = (pred == actual)
        position_dollars = min(conf, 1.0) * bankroll * max_position_pct
        quantity = int(position_dollars / entry_price) if entry_price > 0 else 0
        
        if quantity <= 0:
            continue
        
        if ok:
            gross_pnl = (1.0 - entry_price) * quantity
        else:
            gross_pnl = (0.0 - entry_price) * quantity
        
        net_pnl = gross_pnl - (half_spread * quantity)
        bankroll += net_pnl
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    return max_dd
```

### Step 5: Update `run_backtest()` to Wire Strike-Based Metrics

**File:** `core/unified_backtest.py`, function `run_backtest()`, lines 272–294

**Changes:**
1. Pass `entry_price` and `spread` parameters to `run_backtest()`
2. Call updated `compute_sharpe()` and `max_drawdown()` with the new parameters
3. Add `entry_price` as a configurable parameter (default 0.50)

### Step 6: Add Strike to Backtest Ground Truth (Optional / Future)

**File:** `core/unified_backtest.py`, `load_station_data()`

**Long-term fix:** The backtest should shift from direction-based labels to strike-based labels. This requires:
1. A strike price mapping for each station × date × market_type
2. The ground truth becomes: `'up' if observed_temp > strike_price else 'down'` (for HIGH markets)
3. This changes the prediction problem — signals must be retrained

**Why this is optional for now:** The direction-based backtest is a valid prediction problem (direction of temperature change). It's a different target variable than strike-based settlement, but it's internally consistent. The critical fix is the P&L formula, not the prediction target.

### Step 7: Add Strike Column to `settlement_epochs` Table

**File:** `core/` (schema migration)

```sql
ALTER TABLE settlement_epochs ADD COLUMN strike_price INTEGER;
```

**Backfill:** For existing epochs, the strike price can be inferred from the market data or stored as NULL. New epochs created by the Kalshi API should include the strike price from the market ticker.

---

## 8. Risk & Edge Cases

### 8.1 Missing Strike Price

**Risk:** If `strike_price` is `NULL` for a trade, the settlement cannot be computed.

**Mitigation:** Skip settlement with a warning log. Do NOT fall back to direction-based or 50°F-based settlement. Unsettled trades are visible in the trading dashboard and can be reconciled manually.

### 8.2 Multiple Strikes per Station × Date

**Risk:** On a given day, multiple strike prices may be available for the same station (e.g., KXHIGHKDEN-26JAN25-85 and KXHIGHKDEN-26JAN25-90). The current system assumes one strike per trade.

**Mitigation:** Each trade has exactly one `strike_price` stored at entry time. The system settles the trade against that specific strike. This is already handled by the architecture (one trade per ticker).

### 8.3 Market Type Mismatch

**Risk:** The `trade.market_type` might not match the strike price (e.g., HIGH market with a 30°F strike).

**Mitigation:** Validate `market_type` vs `strike_price` at trade entry. A HIGH market should have a strike > 50°F (summer contracts). A LOW market should have a strike < 50°F (winter contracts). Flag mismatches to the alert system.

### 8.4 Spread Cost Assumption

**Risk:** The 3.1¢ measured mean spread is an average — actual spreads vary by market and time.

**Mitigation:** Use the dynamic spread from `MarketCostModel.get_dynamic_spread()` when available; fall back to 3.1¢ mean. The P&L variance from spread variation is small (~0.5¢) compared to the P&L variance from settlement outcome (±50¢).

### 8.5 Entry Price Assumption in Backtest

**Risk:** The backtest uses a default `entry_price=0.50` because it doesn't have real market prices for historical dates.

**Mitigation:** The 0.50 entry price is a neutral assumption. Actual entry prices vary from 0.05 to 0.95 depending on how far the market is from the strike. In a future phase, the backtest could pull historical market prices from the Kalshi API or a cached market data table.

### 8.6 Backward Compatibility

**Risk:** Existing backtest results and paper trading P&L records are based on direction-based settlement.

**Mitigation:** The fix is a **breaking change**. All existing backtest results should be regenerated after the fix. Old paper trading records can be reconciled by running a one-time settlement recalc with the new formula (using `strike_price` from the ticker).

---

## Appendix A: File Change Summary

| File | Lines | Change Type | Description |
|---|---|---|---|
| `core/pnl_tracking.py` | 61–65 | Bug fix | Replace direction-based settlement with strike-based; remove 50°F fallback |
| `core/pnl_tracking.py` | 49 | Query change | Add `strike_price` to SELECT; add WHERE `t.strike_price IS NOT NULL` |
| `core/trade_execution.py` | ~89 | New logic | Fetch and store `strike_price` at trade entry |
| `core/unified_backtest.py` | 84–92 | Bug fix | Rewrite `compute_sharpe()` with strike-based P&L |
| `core/unified_backtest.py` | 120–136 | Bug fix | Rewrite `max_drawdown()` with strike-based P&L |
| `core/unified_backtest.py` | ~272 | Wiring | Pass `entry_price`, `spread` to corrected metric functions |
| Schema migration | — | Migration | `ALTER TABLE trades ADD COLUMN strike_price INTEGER` |
| Schema migration | — | Migration | `ALTER TABLE settlement_epochs ADD COLUMN strike_price INTEGER` |
| (New) `core/ticker_utils.py` | — | New module | Kalshi ticker parsing and strike extraction |

---

## Appendix B: Validation Checklist

After implementation, verify each of the following:

- [ ] Trade with `strike_price = 85`, `observed_temp = 90`, `market_type = 'HIGH'` → P&L = `(1.0 - entry_price) × qty`
- [ ] Trade with `strike_price = 85`, `observed_temp = 80`, `market_type = 'HIGH'` → P&L = `(0.0 - entry