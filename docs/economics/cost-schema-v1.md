<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Executable Cost Schema v1 — Passive Kalshi Market Structure

> **Owner:** Marty Byrde (CFO)  
> **Sprint:** Weather Engine Economics  
> **Status:** Schema Design (v1)  
> **Date:** 2026-08-23  
> **Design Principle:** Build cost models from passive market-structure data only. No live API calls, no credentials, no trade execution. All fields are defined for offline/passive population from Kalshi order-book snapshots, historical fills, and market data feeds.

---

## Table of Contents

1. [Bid/Ask Spread Model](#1-bidask-spread-model)
2. [Fill/No-Fill Model](#2-fillno-fill-model)
3. [Fee Model Canonicalization](#3-fee-model-canonicalization)
4. [Slippage Model](#4-slippage-model)
5. [Trade Journal Schema](#5-trade-journal-schema)
6. [Acceptance Criteria](#6-acceptance-criteria)

---

## 1. Bid/Ask Spread Model

### 1.1 Quote Snapshot Schema

Each passive observation captures the state of a single market at a point in time.

```sql
CREATE TABLE quote_snapshots (
    snapshot_id       BIGSERIAL PRIMARY KEY,
    market_ticker     TEXT      NOT NULL,       -- e.g. "HIGHTEMP-27JUN26-A:USW00013739"
    station           TEXT      NOT NULL,       -- Weather station ID
    market_type       TEXT      NOT NULL,       -- 'HIGH' or 'LOW'
    event_timestamp   TIMESTAMPTZ NOT NULL,    -- When the quote was observed (UTC)
    
    -- Level-1 (best bid/ask)
    best_bid_price    NUMERIC(10,4),            -- Best bid, in cents/share
    best_bid_size     INTEGER,                  -- Contracts at best bid
    best_ask_price    NUMERIC(10,4),            -- Best ask, in cents/share
    best_ask_size     INTEGER,                  -- Contracts at best ask
    spread_cents      NUMERIC(10,4),            -- best_ask - best_bid, in cents
    mid_price         NUMERIC(10,4),            -- (best_bid + best_ask) / 2
    
    -- Depth (Level-2, up to 5 levels each side)
    bid_prices        NUMERIC(10,4)[],          -- Array of bid prices, best to worst (size 5)
    bid_sizes         INTEGER[],                -- Array of bid sizes, best to worst (size 5)
    ask_prices        NUMERIC(10,4)[],          -- Array of ask prices, best to worst (size 5)
    ask_sizes         INTEGER[],                -- Array of ask sizes, best to worst (size 5)
    
    -- Computed fields
    total_bid_depth   INTEGER,                  -- Sum of contracts across all bid levels observed
    total_ask_depth   INTEGER,                  -- Sum of contracts across all ask levels observed
    spread_bps        NUMERIC(10,4),            -- Spread as basis points of mid_price (0.01% units)
    
    -- Metadata
    snapshot_source   TEXT      NOT NULL,       -- 'kalshi_ws', 'kalshi_rest', 'historical_feed'
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_quotes_market_time ON quote_snapshots(market_ticker, event_timestamp);
CREATE INDEX idx_quotes_station_type_time ON quote_snapshots(station, market_type, event_timestamp);
```

### 1.2 Spread Estimation from Market Structure

Spread is estimated from **observed passive data** — the schema is designed for offline population, not real-time API calls.

**Basic formula:**

```
spread_cents = best_ask_price - best_bid_price
mid_price    = (best_bid_price + best_ask_price) / 2
spread_bps   = (spread_cents / mid_price) * 10000
```

**Special cases (must be handled as sentinel values):**
- No bid present → spread is undefined; market is one-sided → flag as `LIQUIDITY_WARNING`
- No ask present → same treatment
- No bid and no ask → market is effectively closed → flag as `NO_QUOTE`
- Spread exceeds 10% of mid-price → flag as `WIDE_SPREAD_WARNING`

### 1.3 Time-Weighted Average Spread (TWAS)

When evaluating a trade signal that spans a window, compute TWAS over the window.

```sql
-- Computed from quote_snapshots for a given market over interval [t1, t2]
CREATE TABLE twas_computed (
    computation_id    BIGSERIAL PRIMARY KEY,
    market_ticker     TEXT      NOT NULL,
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    num_snapshots     INTEGER   NOT NULL,
    twas_cents        NUMERIC(10,4),   -- Time-weighted average spread in cents
    twas_bps          NUMERIC(10,4),   -- Time-weighted average spread in bps
    min_spread_cents  NUMERIC(10,4),
    max_spread_cents  NUMERIC(10,4),
    spread_volatility NUMERIC(10,4),   -- Std dev of spread / mean spread (coeff of variation)
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Algorithm for TWAS:**

```
Given: N snapshots at times t_i with spreads s_i
Weights: w_i = (t_{i+1} - t_{i-1}) / 2    (trapezoidal weighting)
         or w_i = (t_{i+1} - t_i) for forward-weight (simpler)
TWAS = SUM(s_i * w_i) / SUM(w_i)
```

### 1.4 Spread Cost Per Contract

For a single contract crossing the spread:

```
entry_spread_cost = spread_estimate / 2    -- half-spread for entry
exit_spread_cost  = spread_estimate / 2    -- half-spread for exit
round_trip_spread_cost = spread_estimate   -- full spread
```

These are **theoretical minimum costs** — actual fills may be better or worse depending on patience, order type, and fill probability (see Section 2).

---

## 2. Fill/No-Fill Model

### 2.1 Trade Journal Entry Schema (Submission Record)

Each attempted fill is recorded in the `fill_attempts` table.

```sql
CREATE TABLE fill_attempts (
    attempt_id      BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT      NOT NULL,       -- FK to trade_journal
    signal_id       BIGINT,                     -- FK to signal origin (if tracked)
    
    -- Order specification
    station         TEXT        NOT NULL,
    market_type     TEXT        NOT NULL,       -- 'HIGH' or 'LOW'
    side            TEXT        NOT NULL,       -- 'BUY' or 'SELL'
    order_type      TEXT        NOT NULL,       -- 'MARKET', 'LIMIT', 'STOP_LIMIT'
    limit_price     NUMERIC(10,4),              -- For limit orders; NULL for market
    quantity        INTEGER     NOT NULL,       -- Contracts requested
    submitted_at    TIMESTAMPTZ NOT NULL,
    
    -- Fill result
    fill_status     TEXT        NOT NULL,       -- 'FULL_FILL', 'PARTIAL_FILL', 'NO_FILL', 'CANCELED'
    fill_price      NUMERIC(10,4),              -- Weighted average fill price; NULL if no fill
    fill_quantity   INTEGER,                    -- Number of contracts actually filled
    first_fill_at   TIMESTAMPTZ,                -- Timestamp of first fill event
    last_fill_at    TIMESTAMPTZ,                -- Timestamp of final fill (or cancellation)
    
    -- Fill gap tracking (for partial fills)
    partial_reason  TEXT,                       -- 'LIQUIDITY_DRIED', 'ORDER_EXPIRED', 'USER_CANCEL'
    unfilled_qty    INTEGER,                    -- Quantity that was not filled
    
    -- No-fill detail
    no_fill_reason  TEXT,                       -- 'NO_BID_ASK', 'INSUFFICIENT_DEPTH', 
                                                -- 'SPREAD_TOO_WIDE', 'PRICE_MOVED_AWAY',
                                                -- 'ORDER_TIMEOUT', 'CANCELED_BEFORE_FILL'
    
    -- Reconciliation with market structure at submission
    snapshot_id     BIGINT,                     -- FK to quote_snapshots at/near submission time
    spread_at_submit NUMERIC(10,4),
    bid_depth_at_submit INTEGER,
    ask_depth_at_submit INTEGER,
    
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fill_trades ON fill_attempts(trade_id);
CREATE INDEX idx_fill_status ON fill_attempts(fill_status);
```

### 2.2 Fill Probability Estimator (Liquidity-Based)

Formula estimated from passive market structure observations:

```
fill_probability = base_prob × spread_factor × depth_factor × time_factor
```

**Components:**

| Factor | Formula | Notes |
|--------|---------|-------|
| `base_prob` | 0.90 | Default for liquid markets |
| `spread_factor` | max(0.5, 1 - 0.1 × (spread_bps / 5)) | Wider spread → lower fill prob |
| `depth_factor` | min(1.0, side_depth / contract_qty) | If depth < qty, prob drops |
| `time_factor` | max(0.3, min(1.0, days_to_expiry / 7)) | ≤7 days → fill prob decays; >30 days → stable at 1.0 |

**For limit orders specifically:** Add `limit_price_delta` factor:

```
limit_factor = min(1.0, max(0.0, 1 - abs(limit_price - mid_price) / (spread_cents * 2)))
```

**Conservative fallback:** If no market-structure data is available:

```
fill_probability = 0.75   -- default for HIGH markets
fill_probability = 0.65   -- default for LOW markets (typically less liquid)
```

### 2.3 Expected Fill Cost

```
E[fill_cost] = (spread / 2) × fill_probability 
               + no_fill_penalty × (1 - fill_probability)
```

Where:
- `spread / 2` = half-spread cost if filled (entering)
- `no_fill_penalty` = cost of missed opportunity / system overhead when order does not fill
- Default `no_fill_penalty` = `0.5 × expected_edge` (conservative: half the predicted edge is lost on a no-fill)

**No-fill rate estimation from passive data** — observe over historical snapshots:

```
estimated_no_fill_rate = count(NO_FILL attempts) / count(all attempts)
```

This rate is **market-type and liquidity-dependent** and should be tracked per station × market_type over rolling windows:

```sql
CREATE TABLE fill_rate_estimates (
    estimate_id         BIGSERIAL PRIMARY KEY,
    station             TEXT NOT NULL,
    market_type         TEXT NOT NULL,
    window_days         INTEGER NOT NULL,       -- e.g. 30-day rolling
    num_attempts        INTEGER NOT NULL,
    full_fill_rate      NUMERIC(6,4),           -- fraction fully filled
    partial_fill_rate   NUMERIC(6,4),           -- fraction partially filled (some quantity)
    no_fill_rate        NUMERIC(6,4),           -- fraction completely unfilled
    avg_fill_ratio      NUMERIC(6,4),           -- avg (fill_qty / requested_qty) across all attempts
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 3. Fee Model Canonicalization

### 3.1 Kalshi Fee Formula (Verified External Sources)

**Per-contract fee:**

```
fee_cents = ceil(0.07 × P × (1 - P) × 100) / 100
```

Where:
- `P` = contract price (as decimal, 0 to 1. e.g., 0.55 for 55¢)
- `0.07` = 7% taker fee rate
- Result is **rounded up to nearest cent** (ceil to 0.01)

**Example:**
- Contract priced at 55¢ (P = 0.55)
- `fee = ceil(0.07 × 0.55 × 0.45 × 100) / 100`
- `= ceil(0.07 × 0.2475 × 100) / 100`
- `= ceil(1.7325) / 100`
- `= 2 / 100 = $0.02` per contract

### 3.2 Fee Tier Schema

```sql
CREATE TABLE fee_tiers (
    tier_id         SERIAL PRIMARY KEY,
    tier_name       TEXT NOT NULL,               -- 'TAKER', 'MAKER', 'REBATE'
    fee_rate        NUMERIC(8,6) NOT NULL,       -- As decimal: 0.07 for taker
    fee_type        TEXT NOT NULL,               -- 'PER_CONTRACT', 'PER_ROUND_TRIP', 'PER_VOLUME'
    applies_to      TEXT NOT NULL,               -- 'ENTRY', 'EXIT', 'BOTH', 'NET_SETTLEMENT'
    volume_threshold NUMERIC(12,2),              -- Monthly volume in $ for tier qualification; NULL if none
    effective_date  DATE NOT NULL,
    notes           TEXT
);

-- Seed data (verified as of 2026-08-23)
INSERT INTO fee_tiers VALUES
(1, 'TAKER',   0.070000, 'PER_CONTRACT', 'ENTRY', NULL, '2025-01-01', 'Standard 7% taker fee per contract on entry'),
(2, 'MAKER',   0.017500, 'PER_CONTRACT', 'ENTRY', NULL, '2025-01-01', '~1.75% maker rebate, net ~1.75% paid'),
(3, 'TAKER_EXIT', 0.070000, 'PER_CONTRACT', 'EXIT', NULL, '2025-01-01', '7% taker fee on exit if closed before settlement'),
(4, 'SETTLEMENT', 0.000000, 'PER_CONTRACT', 'BOTH', NULL, '2025-01-01', 'No fee at settlement if held to expiry');
```

### 3.3 Round-Trip Fee Calculation

```
entry_fee    = price_of_fee_formula(entry_price, tier='TAKER')
exit_fee     = price_of_fee_formula(exit_price, tier='TAKER_EXIT')  [if closed early]
             = 0                                                    [if held to settlement]
round_trip   = entry_fee + exit_fee

Using market-maker rebate:
entry_fee_rebate = price_of_fee_formula(entry_price, tier='MAKER')
round_trip_maker = entry_fee_rebate + exit_fee   (exit still at taker rate)
```

**Fee per contract for a round trip (standard taker):**

| Entry Price | Exit Price | Entry Fee | Exit Fee | Round Trip |
|-------------|-----------|-----------|---------:|-----------:|
| 55¢         | 65¢       | 2¢        | 2¢       | **4¢**     |
| 15¢         | 85¢       | 1¢        | 1¢       | **2¢**     |
| 5¢          | 95¢       | 1¢        | 1¢       | **2¢**     |
| 80¢         | 20¢       | 2¢        | 2¢       | **4¢**     |

### 3.4 Position-Size Fee Scaling

Kalshi fees are **per-contract, not per-dollar-volume**. Therefore:
- No volume discount tier kicks in for individual trades (unless the account has high monthly volume)
- Fee per contract is **independent of position size**
- Total fee = fee_per_contract × num_contracts (linear scaling)

**Edge case:** If Kalshi introduces volume-based fee tiers in the future, the `volume_threshold` column in `fee_tiers` supports adding them without schema changes.

---

## 4. Slippage Model

### 4.1 Slippage Estimation Schema

Slippage is estimated from **order size vs. available depth** at submission time.

```sql
CREATE TABLE slippage_estimates (
    estimate_id         BIGSERIAL PRIMARY KEY,
    attempt_id          BIGINT,                 -- FK to fill_attempts
    trade_id            BIGINT NOT NULL,        -- FK to trade_journal
    order_quantity      INTEGER NOT NULL,
    
    -- Depth at submission
    best_bid_size       INTEGER,
    best_ask_size       INTEGER,
    next_bid_sizes      INTEGER[],              -- Depth at levels 2-5 on bid side
    next_ask_sizes      INTEGER[],              -- Depth at levels 2-5 on ask side
    total_bid_depth     INTEGER,
    total_ask_depth     INTEGER,
    
    -- Computed slippage
    market_order_slippage_cents   NUMERIC(10,4), -- Worst-case if market order
    limit_order_delay_ms          INTEGER,        -- Estimated ms to fill at limit
    expected_slippage_cents       NUMERIC(10,4), -- Blended estimate
    slippage_bps                  NUMERIC(10,4),
    
    -- Actual (post-fill, for validation)
    actual_slippage_cents         NUMERIC(10,4), -- actual_vs_mid_at_submission
    slippage_error_cents          NUMERIC(10,4), -- expected - actual
    
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.2 Slippage Cost Functions

**Market order slippage (worst-case):**

```
For a BUY order of Q contracts:
  - Walk the ask book from best ask outward
  - Slip through levels until Q is filled
  - market_order_slippage = (average_fill_price - mid_price_per_share)
  
For a SELL order of Q contracts:
  - Walk the bid book from best bid outward
  - market_order_slippage = (mid_price - average_fill_price)
```

**Algorithm:**

```
function calc_market_order_slippage(order_qty, ask_prices[], ask_sizes[], mid_price):
    remaining = order_qty
    total_cost = 0
    total_filled = 0
    for i in 0..len(ask_prices)-1:
        fill = min(remaining, ask_sizes[i])
        total_cost += fill * ask_prices[i]
        total_filled += fill
        remaining -= fill
        if remaining == 0: break
    if remaining > 0:
        // Insufficient depth — use worst_level_price + spread/2 as estimate
        // Or reject the trade entirely (see Conservative Default)
        return INF  // signal that depth is insufficient
    avg_price = total_cost / total_filled
    return avg_price - mid_price
```

**Limit order fill delay:**

```
limit_order_delay_ms = f(order_qty, limit_price_distance, market_volatility)
```

Heuristic for passive estimation:

```
If limit_price = mid_price and spread < 2¢ and bid_depth > order_qty:
    delay_ms ≈ 100-500ms (high probability of fast fill)
    
If limit_price = mid_price and spread > 2¢:
    delay_ms ≈ 1000-5000ms (may need to wait)

If limit_price is inside spread (aggressive limit):
    delay_ms ≈ 0-200ms (near-market order speed)
```

### 4.3 Blended Expected Slippage

Conservative approach: blend between market order and limit order behaviors.

```
If order_qty ≤ min(bid_depth, ask_depth):
    // Can fill entirely at best level
    expected_slippage = spread / 4   (can reasonably expect to split the spread)
Else if order_qty ≤ min(bid_depth, ask_depth) * 2:
    // Will need to eat into second level on one side
    expected_slippage = spread / 2   (conservative half-spread)
Else:
    // Likely large slippage
    expected_slippage = min(depth_walk_slippage, spread)
```

### 4.4 Conservative Default

If no depth information is available at order time:

```
expected_slippage = spread / 2       // Assume market order pays half the spread
```

This is the **safe overestimate** — protects against optimistic PnL projections. Review against actuals during validation.

---

## 5. Trade Journal Schema

### 5.1 Main Trade Journal Table

```sql
CREATE TABLE trade_journal (
    trade_id            BIGSERIAL PRIMARY KEY,
    signal_id           BIGINT,                 -- FK to signal events table (if tracked)
    
    -- Trade identification
    station             TEXT        NOT NULL,
    market_type         TEXT        NOT NULL,   -- 'HIGH' or 'LOW'
    contract_ticker     TEXT        NOT NULL,   -- e.g. "HIGHTEMP-27JUN26-A:USW00013739"
    trade_direction     TEXT        NOT NULL,   -- 'LONG' (buy YES, sell later) or 
                                                -- 'SHORT' (sell YES, buy back later) or
                                                -- 'SPREAD' (paired legs)
    
    -- Timing
    entry_time          TIMESTAMPTZ NOT NULL,
    exit_time           TIMESTAMPTZ,            -- NULL if still open
    expiry              DATE        NOT NULL,   -- Contract settlement date
    
    -- Entry prices (weighted averages if multiple fills)
    entry_price         NUMERIC(10,4) NOT NULL,
    entry_fill_count    INTEGER,                -- Number of fills to build position
    entry_fill_ratio    NUMERIC(6,4),            -- Fraction of target that was filled at entry
    
    -- Exit prices (weighted averages if multiple fills)
    exit_price          NUMERIC(10,4),
    exit_fill_count     INTEGER,
    exit_fill_ratio     NUMERIC(6,4),
    
    -- Position sizing
    target_contracts    INTEGER     NOT NULL,   -- Planned position size
    actual_contracts    INTEGER,                -- Actual contracts filled
    notional_value      NUMERIC(12,2),          -- entry_price × actual_contracts
    
    -- Cost breakdown
    fee_paid            NUMERIC(10,4),          -- Total fees paid (entry + exit)
    spread_cost_entry   NUMERIC(10,4),          -- Estimated spread cost at entry
    spread_cost_exit    NUMERIC(10,4),          -- Estimated spread cost at exit
    slippage_cost       NUMERIC(10,4),          -- Estimated slippage cost
    total_transaction_cost  NUMERIC(10,4),      -- fee + spread + slippage
    cost_per_contract   NUMERIC(10,4),          -- total_transaction_cost / actual_contracts
    
    -- Profit & Loss (gross and net)
    gross_pnl           NUMERIC(12,4),          -- exit_proceeds - entry_cost (before costs)
    net_pnl             NUMERIC(12,4),          -- gross_pnl - total_transaction_cost
    pnl_bps             NUMERIC(12,4),          -- net_pnl / notional_value * 10000
    
    -- Signal metadata
    confidence_at_entry NUMERIC(6,4),           -- Signal confidence (0-1)
    signal_strength     NUMERIC(6,4),           -- Signal magnitude (e.g., predicted move)
    expected_edge       NUMERIC(10,4),          -- Expected edge per contract at entry
    
    -- Settlement
    settlement_price    NUMERIC(10,4),          -- Final settlement price (OK if held to expiry)
    settlement_pnl      NUMERIC(12,4),          -- PnL from settlement
    final_outcome       TEXT,                   -- 'WIN', 'LOSS', 'BREAK_EVEN', 'OPEN'
    
    -- Audit
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trade_version       INTEGER NOT NULL DEFAULT 1  -- For reconciliation tracking
);

CREATE INDEX idx_trades_station ON trade_journal(station);
CREATE INDEX idx_trades_time ON trade_journal(entry_time);
CREATE INDEX idx_trades_signal ON trade_journal(signal_id);
CREATE INDEX idx_trades_outcome ON trade_journal(final_outcome);
```

### 5.2 Trade Legs Table (for multi-part trades / partial fills)

```sql
CREATE TABLE trade_legs (
    leg_id              BIGSERIAL PRIMARY KEY,
    trade_id            BIGINT NOT NULL REFERENCES trade_journal(trade_id),
    leg_sequence        INTEGER NOT NULL,       -- Order within the trade
    
    -- Leg type
    leg_type            TEXT NOT NULL,           -- 'ENTRY', 'EXIT', 'ROLL', 'ADJUST'
    side                TEXT NOT NULL,           -- 'BUY' or 'SELL'
    
    -- Fill details
    price               NUMERIC(10,4) NOT NULL,
    quantity            INTEGER NOT NULL,
    filled_at           TIMESTAMPTZ NOT NULL,
    order_type          TEXT NOT NULL,           -- 'MARKET', 'LIMIT', 'STOP'
    
    -- Cost attribution
    fee                 NUMERIC(10,4),           -- Fee for this leg
    spread_estimate     NUMERIC(10,4),           -- Spread cost for this leg
    slippage_estimate   NUMERIC(10,4),           -- Slippage cost for this leg
    
    -- Reconciliation
    attempt_id          BIGINT,                  -- FK to fill_attempts
    snapshot_id         BIGINT,                  -- FK to quote_snapshots
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_legs_trade ON trade_legs(trade_id, leg_sequence);
```

### 5.3 Reconciliation Views

**Signal-to-Trade reconciliation:**

```sql
-- View: matches signal events to executed trades
CREATE VIEW signal_trade_reconciliation AS
SELECT 
    s.signal_id,
    s.station,
    s.market_type,
    s.signal_generated_at,
    s.confidence_score,
    s.signal_strength,
    s.predicted_direction,
    t.trade_id,
    t.entry_time,
    t.exit_time,
    t.entry_price,
    t.exit_price,
    t.actual_contracts,
    t.target_contracts,
    t.fill_ratio_combined,
    t.net_pnl,
    t.total_transaction_cost,
    CASE 
        WHEN t.trade_id IS NULL THEN 'SIGNAL_WITHOUT_TRADE'
        WHEN t.entry_fill_ratio < 0.5 THEN 'POOR_FILL'
        WHEN t.entry_fill_ratio >= 0.5 THEN 'EXECUTED'
        ELSE 'UNKNOWN'
    END AS execution_status
FROM signal_events s
LEFT JOIN trade_journal t ON s.signal_id = t.signal_id;
```

**Portfolio state snapshot reconciliation:**

```sql
-- View: trade journal matched to portfolio snapshots
CREATE VIEW portfolio_position_reconciliation AS
SELECT 
    p.snapshot_id,
    p.snapshot_time,
    p.station,
    p.market_type,
    p.total_contracts_held,
    p.mark_to_market_value,
    t.trade_id,
    t.entry_time,
    t.entry_price,
    t.actual_contracts,
    t.total_transaction_cost,
    t.gross_pnl,
    t.net_pnl,
    CASE 
        WHEN t.exit_time IS NOT NULL AND t.exit_time <= p.snapshot_time THEN 'CLOSED'
        WHEN t.exit_time IS NULL OR t.exit_time > p.snapshot_time THEN 'OPEN'
        ELSE 'UNKNOWN'
    END AS position_status
FROM portfolio_snapshots p
LEFT JOIN trade_journal t ON p.station = t.station 
    AND p.market_type = t.market_type
    AND t.entry_time <= p.snapshot_time;
```

### 5.4 Fill Ratio Tracking

For the trade_journal table, we need a combined fill ratio for the entire trade:

```sql
-- Computed column on trade_journal
-- entry_fill_ratio = actual_contracts / target_contracts (capped at 1.0)
-- exit_fill_ratio  = exit_contracts_closed / actual_contracts (capped at 1.0)
-- fill_ratio_combined = entry_fill_ratio * exit_fill_ratio
```

If fill ratio is below 0.8 for entry:
- Flag as `EXECUTION_WARNING` — model may overestimate edge
- Cost model should apply a **fill ratio penalty**: `effective_cost = cost / fill_ratio` (fewer contracts means each contract bears more of the total fixed cost)

---

## 6. Acceptance Criteria

### 6.1 Definition of "Good Enough"

The cost model is accepted when all of the following are met:

| # | Criterion | Threshold | Validation Method |
|---|-----------|-----------|-------------------|
| 1 | **Spread estimation accuracy** | Within ±0.5¢ of actual spread for ≥90% of market observations | Compare TWAS-derived estimates vs direct measurements on held-out quote snapshots |
| 2 | **Fee calculation accuracy** | 100% match with Kalshi fee schedule | Unit test against known fee table (10 test cases across price range 1¢-99¢) |
| 3 | **Fill probability calibration** | Predicted fill rate within ±5pp of observed fill rate per station×market_type bucket | Historical fill_attempts analysis: compare estimate vs actual by bucket |
| 4 | **Slippage estimate bounds** | Expected slippage ≥ actual slippage in ≥80% of trades (conservative) | Backtest on historical trades with known fills vs. estimated depth walk |
| 5 | **Total cost uncertainty** | Cost model variance ≤ 25% of expected edge | Standard deviation of (total_actual_cost - total_estimated_cost) divided by mean expected edge per trade |
| 6 | **Schema completeness** | All fields defined, nullable correctly, FK constraints specified | Schema review |
| 7 | **Reconciliation coverage** | Can join trade_journal ↔ fill_attempts ↔ quote_snapshots ↔ signal_events | Schema walkthrough |

### 6.2 Upper/Lower Bounds on Expected Costs

**Per-station, per-market-type cost bounds:**

| Market Type | Cost Component | Lower Bound | Upper Bound | Notes |
|-------------|---------------|-------------|-------------|-------|
| **HIGH** (liquid) | Spread cost (entry) | 0.25¢ | 1.0¢ | Liquid; tight spreads |
| **HIGH** (liquid) | Fee (round trip) | 1¢ | 4¢ | Depends on entry price |
| **HIGH** (liquid) | Slippage | 0¢ | 0.5¢ | Small orders likely at best |
| **HIGH** (liquid) | **Total per contract** | **1.25¢** | **5.5¢** | |
| **LOW** (less liquid) | Spread cost (entry) | 0.5¢ | 2.0¢ | Wider spreads typical |
| **LOW** (less liquid) | Fee (round trip) | 1¢ | 4¢ | Same fee schedule |
| **LOW** (less liquid) | Slippage | 0¢ | 1.0¢ | More depth variability |
| **LOW** (less liquid) | **Total per contract** | **1.5¢** | **7.0¢** | |

**Expected edge thresholds (net of costs):**
- Minimum viable edge per contract: **≥ total_upper_bound × 2** (2× cost floor to be worth taking)
  - HIGH markets: ≥ 11¢ expected edge per contract
  - LOW markets: ≥ 14¢ expected edge per contract
- Trades with expected edge < cost floor should be **auto-rejected**

### 6.3 Variance Budget

```
total_cost_uncertainty ≤ 0.25 × expected_edge
```

**Implementation:** For each trade, compute:

```
cost_estimate = fee + spread_cost + slippage_cost
cost_variance = var(fee) + var(spread_cost) + var(slippage_cost)

where:
  var(fee) = 0 (deterministic given entry and exit prices)
  var(spread_cost) = (max_historical_spread - min_historical_spread)^2 / 16  
    (approximate variance from historical TWAS observations)
  var(slippage_cost) = (market_order_slippage - limit_order_slippage)^2 / 4
    (conservative: treat distribution as uniform between market and limit extremes)

cost_std = sqrt(cost_variance)
cost_uncertainty_ratio = cost_std / expected_edge
```

**Pass condition:** `cost_uncertainty_ratio ≤ 0.25`

If this ratio exceeds 0.25 for a given station×market_type, that bucket needs:
- More quote snapshots to tighten spread estimates
- Better depth profiling to reduce slippage variance
- Possible exclusion from trading until data improves

### 6.4 Model Acceptance Gate

```
Accept if: 
  (1) All 7 acceptance criteria pass, OR 
  (2) Criteria 1-5 all pass and 6-7 are documented as minor gaps with planned fixes

Block if:
  (1) Criteria 1-5 fail for ≥2 stations × market_type combinations
  (2) Total cost uncertainty > 0.35 × expected_edge (worse than threshold + 10pp buffer)
  (3) No-fill rate data is unavailable for ≥60% of target station×market_type buckets
```

---

## Appendix A: Cost Model Execution Pipeline (Data Flow)

```
quote_snapshots (passive feed)
       │
       ▼
twas_computed ──► spread_cost estimates
       │
       ▼
fill_attempts ──► fill_probability model ──► fill_rate_estimates
       │                                       │
       ▼                                       ▼
slippage_estimates                       expected_fill_cost
       │                                      
       ▼                                      
trade_journal ◄──── trade_legs
       │              
       ▼
reconciliation_views ──► acceptance_criteria checks
```

## Appendix B: Schema Versioning

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| v1 | 2026-08-23 | Marty Byrde (subagent) | Initial executable cost schema design |

*This schema is designed for passive data population only — no live API calls, credentials, or trade execution required during population.*