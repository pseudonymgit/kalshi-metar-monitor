<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Executable P&L Consumer — Spec for Passive Collector Data

**Owner:** Marty Byrde (CFO) — economics lane  
**Date:** 2026-08-23  
**Status:** Design Spec (implementable, not deployed)  
**Boundaries:** ❌ No credentials, ❌ No API, ❌ No trading, ❌ No shared worktree mutation  
**Gate:** Requires Stream 3 dry-run clearance before implementation

---

## 1. Purpose

Define the P&L consumer that ingests passive Kalshi market structure data and produces executable daily portfolio P&L. This is the bridge between "schema complete" (cost + risk/sizing tables designed) and "executable economics" (daily Sharpe, drawdown, acceptance gate with real data).

### Separation

| Layer | Status | Depends On |
|-------|--------|------------|
| Schema (tables, DDL, FKs) | ✅ Complete | Nothing |
| Fixture validation (gate logic proven) | ✅ Complete | Nothing |
| **P&L Consumer spec** (this doc) | 📄 In progress | Nothing (design only) |
| P&L Consumer implementation | 🔒 Blocked | Stream 3 gate + live data |
| Executable acceptance gate | 🔒 Blocked | P&L Consumer + 20 days data |

---

## 2. Architecture Overview

### 2.1 Data Flow

```
passive_collector (cron job, no auth)
  │
  ├── quote_snapshots  ← Kalshi market quotes (30s L1, 60s L2)
  ├── market_registry   ← Kalshi market discovery
  └── settlement_truth ← Kalshi settlement results
         │
         ▼
  ┌─────────────────────────────────────────────┐
  │         P&L CONSUMER (this spec)             │
  │                                              │
  │  1. READ trade signals from signal_journal   │
  │     (or wherever signal decisions live)      │
  │                                              │
  │  2. MATCH signal → market → quote snapshot   │
  │     (nearest timestamp, 5-min window)        │
  │                                              │
  │  3. COMPUTE estimated costs:                 │
  │     - Spread cost (TWAS from depth)          │
  │     - Fill probability (liquidity model)     │
  │     - Slippage (depth-walk algorithm)        │
  │     - Fee (Kalshi 7% formula)                │
  │                                              │
  │  4. RECORD in trade_journal:                 │
  │     - Estimated net PnL                      │
  │     - Actual net PnL (if settlement known)   │
  │     - Fee + spread + slippage decomposition  │
  │                                              │
  │  5. ROLL UP to daily portfolio PnL:          │
  │     - daily_pnl table                        │
  │     - drawdown_state                         │
  │     - concentration_snapshot                 │
  │                                              │
  │  6. CALIBRATE cost model:                    │
  │     - cost_model_calibration                 │
  │     - Compare estimates vs actuals           │
  │                                              │
  │  7. EVALUATE acceptance gate:                │
  │     - Good Enough (Sharpe ≥0.5)              │
  │     - Promotion (Sharpe ≥1.0, drawdown ≤20%) │
  └─────────────────────────────────────────────┘
```

### 2.2 Two Modes

| Mode | Condition | What Computes | Acceptance Gate |
|------|-----------|---------------|-----------------|
| **Estimated** (pre-settlement) | Signal exit before settlement | Cost-estimated net PnL | Partial (cost model only) |
| **Settled** (post-settlement) | Market closed + settlement known | True PnL ($0 or $1) | Full (validates cost estimates) |

The consumer always runs both. The gap between estimated and settled PnL is the validation signal for the cost model.

---

## 3. Inputs

### 3.1 Source: `signal_journal` (PWW or equivalent)

Must contain at minimum:

| Field | Type | Source |
|-------|------|--------|
| signal_id | UUID | Signal generator (e.g., anomaly detector) |
| condition_id | hex | Polymarket/Kalshi market identifier |
| entry_ts | TIMESTAMP | Signal generated |
| entry_price | DECIMAL(8,4) | Market price at signal time |
| position_contracts | INTEGER | Intended position size |
| direction | TEXT | BUY_YES / BUY_NO |

### 3.2 Source: `quote_snapshots` (Passive Collection)

From the passive data pipeline. Schema defined in cost-schema-v1.md §1.1. Consumer queries nearest quote to signal entry time.

### 3.3 Source: `settlement_truth` (Passive Collection)

| Field | Type | Description |
|-------|------|-------------|
| market_ticker | TEXT | Kalshi market ID |
| condition_id | TEXT | Polymarket/Kalshi condition |
| settlement_bucket | TEXT | e.g., "3" for 3°F bucket |
| settlement_price | DECIMAL(8,4) | $0 or $1 |
| settlement_time | TIMESTAMP | When settled |

---

## 4. Consumer Pipeline — Detailed Steps

### 4.1 Step 1: Signal Ingestion

```
READ signal_journal rows where:
  - entry_ts >= last_processed_ts (incremental)
  - direction = 'BUY_YES' (or supported direction)

PRODUCE: staged_signal_events (in-memory or scratch table)
```

**Stop condition:** If signal_journal has 0 unprocessed rows, skip.

### 4.2 Step 2: Quote Matching

```
FOR each signal:
  market_ticker = derive_ticker_from_condition(condition_id)
  q = SELECT * FROM quote_snapshots 
      WHERE market_ticker = ticker
      ORDER BY ABS(EXTRACT(EPOCH FROM (event_timestamp - entry_ts)))
      LIMIT 1
  
  VALIDATE: |q.event_timestamp - entry_ts| ≤ 5 minutes
  If no match within 5min → flag as MISSING_QUOTE, use spread/2 default cost

PRODUCE: signal_with_quote (joined rows)
```

**Stop condition:** If ≥50% of signals have no matching quote within 5min, STOP — collection frequency is insufficient.

### 4.3 Step 3: Cost Estimation

```
FOR each signal_with_quote:
  costs = {}
  
  # Fee (deterministic, Kalshi 7% formula)
  costs['fee_entry'] = ceil(0.07 * entry_price * (1-entry_price) * 100) / 100 * position_contracts
  costs['fee_exit']  = ceil(0.07 * exit_price * (1-exit_price) * 100) / 100 * position_contracts  # if exit known
  
  # Spread cost (TWAS from depth)
  spread_cents = compute_twas(q.bid_prices, q.bid_sizes, q.ask_prices, q.ask_sizes)
  costs['spread_cost'] = spread_cents * position_contracts
  
  # Fill probability
  fill_prob = estimate_fill_probability(q.best_bid_size, q.best_ask_size, spread_cents)
  if fill_prob < 0.5:  # low liquidity
    costs['no_fill_penalty'] = (1 - fill_prob) * position_contracts * entry_price * 0.5
  
  # Slippage (conservative depth-walk)
  slippage = estimate_slippage(q.bid_prices, q.bid_sizes, q.ask_prices, q.ask_sizes, position_contracts)
  if total_depth < 10: slippage *= 1.5  # thin-market calibration factor
  costs['slippage_cost'] = slippage * position_contracts

PRODUCE: cost_estimates (per-signal)
```

**Stop condition:** If any cost component is >3× its expected upper bound (from cost-schema §6.2), flag as OUTLIER and exclude from calibration.

### 4.4 Step 4: Trade Journal Population

```
INSERT INTO trade_journal for each signal:
  trade_id = UUID(signal_id)
  entry_price, exit_price, contract_count = from signal
  fee_entry, fee_exit = from cost_estimates
  spread_cost = from cost_estimates
  slippage_cost = from cost_estimates
  gross_pnl = (exit_price - entry_price) * contract_count  (if exit known)
  net_pnl = gross_pnl - fee_entry - fee_exit - spread_cost - slippage_cost
  confidence_at_entry = from signal metadata (if available)
  fill_ratio_combined = 1.0 (estimated until fill data available)
```

### 4.5 Step 5: Settlement Resolution

```
WHEN settlement_truth has new data:
  FOR each trade_journal row where exit_ts > market settlement_time:
    actual_pnl = (settlement_price - entry_price) * contract_count - costs
    UPDATE trade_journal SET:
      settlement_hit = 'YES' | 'NO'
      actual_net_pnl = actual_pnl
      settlement_bucket = settlement_bucket
  
  COMPARE estimated vs actual PnL
  COMPUTE calibration bias per station×market_type
  INSERT into cost_model_calibration
```

### 4.6 Step 6: Daily Portfolio Rollup

```
DAILY (EOD cron):
  INSERT INTO daily_pnl:
    date = CURRENT_DATE
    trade_count = SUM(signals settled today)
    gross_pnl = SUM(gross_pnl)
    fee_paid = SUM(fee_entry + fee_exit)
    spread_slippage_cost = SUM(spread_cost + slippage_cost)
    net_pnl = gross_pnl - fees - costs
    bankroll_start = previous day's bankroll_end
    bankroll_end = bankroll_start + net_pnl
    daily_return = net_pnl / bankroll_start
  
  INSERT INTO drawdown_state:
    current_bankroll = daily_pnl.bankroll_end
    peak = MAX(peak, current)
    drawdown = (peak - current) / peak
    warning = drawdown >= 0.10
    hard_stop = drawdown >= 0.20
```

### 4.7 Step 7: Acceptance Gate Evaluation

```
WEEKLY or on-demand:
  Compute daily Sharpe: (mean daily_return / std daily_return) * sqrt(252)
  Compute bootstrap CI on Sharpe (stationary, 1000 resamples, block=5)
  Compute max drawdown
  Compute HHI concentration
  Evaluate Good Enough gate (§6.2):
    - Sharpe ≥ 0.5
    - CI lower bound ≥ 0.0
    - Drawdown ≤ 30%
  Evaluate Promotion gate:
    - Sharpe ≥ 1.0
    - CI lower bound ≥ 0.5
    - Drawdown ≤ 20%
    - HHI ≤ 0.15
```

---

## 5. Outputs

### 5.1 Primary Tables (All Already Schema-Defined)

| Table | Schema | Write Mode | Update Frequency |
|-------|--------|-----------|-----------------|
| `trade_journal` | cost-schema-v1.md §5.1 | Upsert | Per signal processed |
| `daily_pnl` | risk-sizing-schema-v1.md §2 | Insert | Daily EOD |
| `drawdown_state` | risk-sizing-schema-v1.md §3 | Upsert | Daily EOD |
| `concentration_snapshot` | risk-sizing-schema-v1.md §4 | Insert | Weekly |
| `cost_model_calibration` | cost-schema-v1.md §5 (fixture spec) | Insert | Per settlement batch |
| `bootstrap_results` | risk-sizing-schema-v1.md §5 | Insert | Weekly |
| `acceptance_gate_result` | risk-sizing-schema-v1.md §6 | Insert | Weekly |

### 5.2 Computed Metrics (Derived)

| Metric | Formula | Valid After |
|--------|---------|-------------|
| Daily Sharpe (annualized) | (mean(r) / std(r)) × √252 | 20+ trading days |
| Max drawdown | max(peak-to-trough pct) | Any window |
| Herfindahl-Hirschman Index | Σ(station_weight²) | Any day with positions |
| Bootstrap CI (Sharpe 95%) | percentile(sharpe_resamples, [2.5, 97.5]) | 20+ days |
| Cost model uncertainty ratio | cost_std / expected_edge | Per settlement batch |
| Effective sample size (N_eff) | n / (1 + 2 Σ ρ_k) | 30+ days |

---

## 6. Implementation Effort

| Component | Effort | Dependencies | Credential? |
|-----------|--------|-------------|-------------|
| Signal ingestion adapter | 2h | PWW signal_journal schema | ❌ No |
| Quote matching (nearest neighbor) | 3h | quote_snapshots collection cron | ❌ No |
| Fee calculation (Kalshi 7%) | 0.5h | Cost schema §3 | ❌ No |
| TWAS spread estimator | 2h | Cost schema §1 | ❌ No |
| Fill probability estimator | 2h | Cost schema §2 | ❌ No |
| Slippage depth-walk algorithm | 3h | Cost schema §4 | ❌ No |
| Trade journal writer | 1h | Cost schema §5 | ❌ No |
| Settlement resolver | 3h | settlement_truth collection | ❌ No |
| Daily portfolio rollup | 2h | Risk schema §2 | ❌ No |
| Drawdown tracker | 1h | Risk schema §3 | ❌ No |
| Concentration calculator | 2h | Risk schema §4 | ❌ No |
| Bootstrap CI engine | 4h | Risk schema §5 | ❌ No |
| Cost calibration accumulator | 2h | Fixture spec §5 | ❌ No |
| Acceptance gate evaluator | 2h | Risk schema §6 | ❌ No |
| **Total** | **~28h** | | |

---

## 7. Acceptance Criteria for the Consumer

The P&L Consumer passes its own acceptance gate when:

| # | Criterion | Threshold | Verification |
|---|-----------|-----------|-------------|
| 1 | Signal-quote match rate | ≥90% within 5min | Run against 1 day of passive data |
| 2 | Fee arithmetic | 100% match with formula | Unit test against 20 known cases |
| 3 | Spread estimation | ±0.5¢ vs known spread | Fixture data validation |
| 4 | Fill probability | ±5pp vs historical fill rate | Against PWW fill_attempts |
| 5 | Slippage conservative | ≥80% of estimates ≥ actual | Backtest on fill data |
| 6 | Daily PnL arithmetic | 0 arithmetic errors | Cross-check all 7 trade journal fields |
| 7 | Drawdown computation | Peak > current → drawdown > 0 | Simple monotonic test |
| 8 | No regression on fixture data | Gate result same as fixture baseline | Compare gate-output.json |

---

## 8. Boundary Enforcement (as Executed by the Consumer)

| Prohibited | Enforcement |
|-----------|-------------|
| Credential access | No config path for credentials. Consumer reads from DB only. |
| Trade execution | No order placement code in any consumer module |
| AI in P&L loop | No LLM calls in Steps 1-7. Deterministic computation only. |
| Shared worktree mutation | All writes to consumer's own data path, not `prototypes/weather-engine-source/` |
| Unchecked estimates | Every cost estimate must include an uncertainty range (±X%) |

---

## 9. Dependencies After Gate Opens

```
┌────────────────────────────────────────────┐
│   Stream 3 Dry-Run Gate                    │
└────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────┐
│   1. Deploy quote collection                │  (2h, no auth)
│   2. Deploy market discovery                │  (1h, no auth)
│   3. Deploy settlement capture              │  (1h, no auth)
└────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────┐
│   4. Run P&L Consumer (estimated mode)     │  (4h, no auth)
│      → trade_journal rows populated        │
│      → daily_pnl starts accumulating       │
└────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────┐          ┌──────────────────────────────┐
│   5. After first settlement batch          │  ──►   │   cost_model_calibration      │
│      → compare estimated vs actual PnL     │         │   first version populated     │
└────────────────────────────────────────────┘         └──────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────┐
│   6. After 20-30 trading days of data      │
│      → daily Sharpe computed               │
│      → drawdown + concentration evaluated │
│      → acceptance gate run                 │
└────────────────────────────────────────────┘
```

---

## 10. Key Open Questions (Non-Blocking for Design)

| Question | Answer Needed From | Impact |
|----------|-------------------|--------|
| Does PWW signal_journal have `condition_id` that links to Kalshi tickers? | PWW data review | Signal ingestion adapter design |
| Is Kalshi fee same for YES and NO contracts? | Kalshi docs | Fee formula generalizes to both |
| What is the thin-market slippage factor for individual stations? | Live calibration | Defaults to 1.5× until data available |
| What batch size for signal ingestion (real-time vs batch)? | PWW pipeline schedule | Trade journal write frequency |
| Should we compute MtM for same-day partial fills? | Trade journal business rules | Minor section in §4.3 |

---

*End of Executable P&L Consumer Spec v1*