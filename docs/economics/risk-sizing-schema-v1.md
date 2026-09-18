<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Executable Risk/Sizing Schema v1 — Weather Engine Economics

**Author:** Marty Byrde (CFO) / Subagent: marty-resource-allocator  
**Date:** 2026-08-23  
**Status:** Schema Design Document (implementable, not deployed)  
**License:** Internal — Weather Engine Project  

---

## Table of Contents

1. [Sizing Model (Kelly-based)](#1-sizing-model-kelly-based)
2. [Daily Portfolio P&L Schema](#2-daily-portfolio-pl-schema)
3. [Drawdown Tracking Schema](#3-drawdown-tracking-schema)
4. [Concentration & Correlation Model](#4-concentration--correlation-model)
5. [Bootstrap Confidence Intervals](#5-bootstrap-confidence-intervals)
6. [Acceptance Model](#6-acceptance-model)
7. [Appendix: Reference Calculations](#appendix-reference-calculations)

---

## 1. Sizing Model (Kelly-based)

### 1.1 Core Kelly Formula

The Kelly criterion determines the fraction of bankroll to allocate to a binary contract:

```
f* = (p * b - q) / b
```

Where:
- `f*` = Kelly fraction (portion of bankroll to risk)
- `p` = win probability (model confidence / 100)
- `q` = loss probability (1 - p)
- `b` = net odds received on the wager

For Kalshi binary contracts (YES/NO):

```
b = (1 - contract_price) / contract_price
```

Derivation for a YES contract purchased at price `P` (where outcome pays $1 per contract):
- If YES wins: return = $(1 - P) per contract
- If YES loses: loss = $-P per contract
- Net odds b = (1 - P) / P

### 1.2 Fractional Kelly

Position size (in $ notional risk, not number of contracts):

```
position_dollars = f_kelly * f_star * bankroll
contract_count = floor(position_dollars / contract_price)

Where:
  f_kelly       = Kelly fraction (0.0 to 1.0) — calibrated from backtest
  f_star        = Full Kelly fraction from [1.1]
  bankroll      = Current total bankroll ($)
  contract_price= Current YES contract price ($) for the target market
```

**Schema Fields — Standalone Sizing Record:**

| Field | Type | Description |
|-------|------|-------------|
| `market_id` | string | Kalshi market ID |
| `timestamp` | datetime (UTC) | Decision timestamp |
| `win_probability` | float (0–1) | Model confidence for YES outcome |
| `contract_price` | float (0–1) | Current YES contract price |
| `bankroll` | float (≥0) | Current bankroll at decision time |
| `full_kelly_fraction` | float | f* from [1.1] |
| `kelly_fraction_used` | float | Calibrated fraction (e.g., 0.25) |
| `recommended_size_dollars` | float | f_kelly × f* × bankroll |
| `max_allowed_dollars` | float | Per-market concentration limit [1.3] |
| `actual_size_dollars` | float | min(recommended_size, max_allowed) |
| `contract_count` | integer | floor(actual_size / contract_price) |
| `position_risk_dollars` | float | P&L if wrong: contract_count × contract_price |
| `notes` | string | Optional — override reason, signal quality flag |

### 1.3 Per-Market Concentration Limit

```
max_allowed_dollars = min(
    bankroll * concentration_limit_pct,
    bankroll * (1 / total_active_markets) * stretch_factor
)
```

**Configurable Parameters:**

| Field | Default | Range | Description |
|-------|---------|-------|-------------|
| `concentration_limit_pct` | 0.05 | 0.01–0.20 | Max % of bankroll on any single market |
| `stretch_factor` | 1.0 | 1.0–2.0 | Allows concentration at low market count |
| `min_contracts` | 1 | ≥1 | Minimum contract count (avoid sub-penny rounding) |

### 1.4 Kelly Fraction Calibration

Calibrate `f_kelly` from historical backtest output:

```
Step 1: Run passive backtest with full Kelly (f_kelly = 1.0)
  - Record max_drawdown, terminal_bankroll, Sharpe

Step 2: Sweep f_kelly ∈ [0.05, 0.1, 0.15, 0.2, 0.25, 0.33, 0.5, 1.0]
  - For each fraction, recompute:
      terminal_value = initial_bankroll × ∏(1 + r_i)  (r_i = trade return)
      max_drawdown_depth = max peak-to-trough
      sharpe = mean(return) / std(return) × sqrt(periods_per_year)

Step 3: Select f_kelly that maximizes:
      utility = terminal_value - λ_dd × max_drawdown_depth - λ_trade × var(trade_frequency)

  Where:
    λ_dd    = drawdown penalty weight (default: 0.1)
    λ_trade = trade-frequency penalty (default: 0.01)

Step 4: If no fraction satisfies max_drawdown ≤ 20%, cap f_kelly at the
         highest fraction that does and flag for strategy review.
```

**Calibration Output Schema:**

| Field | Type | Description |
|-------|------|-------------|
| `f_kelly_candidate` | float | Fraction tested |
| `terminal_bankroll` | float | Final bankroll after backtest |
| `max_drawdown` | float | Peak-to-trough drawdown % |
| `sharpe_ratio` | float | Annualized Sharpe |
| `trade_count` | integer | Total trades executed |
| `utility_score` | float | Composite = terminal_value - penalties |
| `passes_drawdown_test` | boolean | max_drawdown ≤ 20% |

**Journal Entry for Each Sized Trade:**

| Field | Type | Description |
|-------|------|-------------|
| `trade_id` | string (UUID) | Unique trade identifier |
| `sizing_record` | object | Copy of fields from [1.2] |
| `execution_price` | float | Actual fill price (may differ from quote) |
| `execution_cost` | float | Spread + slippage cost |
| `fees_paid` | float | Taker fee = contract_count × contract_price × 0.07 |
| `net_risk` | float | total cost including fees |
| `outcome` | enum: WIN/LOSS/PENDING | Trade result |
| `pnl_realized` | float | Actual P&L when closed |

---

## 2. Daily Portfolio P&L Schema

### 2.1 `daily_pnl` Table Schema

```sql
CREATE TABLE daily_pnl (
    date                DATE NOT NULL PRIMARY KEY,
    total_pnl           DECIMAL(12,2),      -- Gross PnL - fees - slippage
    gross_pnl           DECIMAL(12,2),      -- Σ(realized_trade_pnl + mtm_change)
    net_pnl             DECIMAL(12,2),      -- gross_pnl - fees_paid - spread_slippage_cost
    fees_paid           DECIMAL(12,2),      -- Σ(taker_fees) for trades closed today
    spread_slippage_cost DECIMAL(12,2),     -- Σ(entry_price - avg_fill_price) × contracts
    trade_count         INTEGER,            -- Number of trades (opens + closes)
    active_markets      INTEGER,            -- Markets with open positions at EOD
    win_count           INTEGER,            -- Trades closed today with PnL > 0
    loss_count          INTEGER,            -- Trades closed today with PnL < 0
    bankroll_start      DECIMAL(12,2),      -- Bankroll at start of trading day (00:00 UTC)
    bankroll_end        DECIMAL(12,2),      -- Bankroll at end of trading day (23:59 UTC)
    daily_return        DECIMAL(8,6),       -- net_pnl / bankroll_start (can be negative)
    cumulative_return   DECIMAL(8,6),       -- Running product of (1 + daily_return)
    notes               TEXT                -- Optional: market events, system notes
);
```

### 2.2 Daily Return Calculation

```
daily_return = net_pnl / bankroll_start

Where:
  bankroll_start = bankroll_end of previous day
                 = initial_bankroll if no previous day exists
  bankroll_end   = bankroll_start + net_pnl
```

**Edge cases:**
- `bankroll_start ≤ 0` → set daily_return = 0, flag "bankrupt" in notes
- Division by zero not possible (bankroll >= 0 by invariant)

### 2.3 Multi-Day Trades — Mark-to-Market (MtM)

For positions held overnight (open at market close, not closed):

```
Daily realized PnL = Σ(closed_trade_pnl) for trades fully closed today
Daily MtM change   = Σ(open_position_value_eod - open_position_value_sod)

Where:
  open_position_value = contract_count × current_market_price
```

**Policy:** Use **mark-to-market** for daily return calculation, NOT cash-basis-only. This gives a true economic picture:
- A trade opened today at $0.50 and held to EOD at $0.55 → MtM gain recorded today
- When closed tomorrow at $0.60 → remainder recorded tomorrow
- MtM entries are reversed and replaced by realized PnL on close

**Schema for Open Position Valuation:**

```sql
CREATE TABLE open_positions (
    trade_id        UUID PRIMARY KEY,
    market_id       TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('YES', 'NO')),
    contract_count  INTEGER NOT NULL,
    entry_price     DECIMAL(8,4) NOT NULL,
    current_price   DECIMAL(8,4) NOT NULL,
    sod_value       DECIMAL(12,2),     -- contract_count × price at start of day
    eod_value       DECIMAL(12,2),     -- contract_count × price at end of day
    mtm_change      DECIMAL(12,2),     -- eod_value - sod_value
    opened_at       TIMESTAMP,
    expected_close  TIMESTAMP          -- market expiration
);
```

### 2.4 Inactive Days Handling

**Rule:** Every calendar day MUST have a `daily_pnl` row between first_trade_date and today.

```
If the engine did not trade on a given date:
  trade_count   = 0
  net_pnl       = 0.0
  daily_return  = 0.0
  cumulative_return = prior_cumulative_return (unchanged)
  bankroll_end  = bankroll_start (unchanged)
  active_markets = open_positions count (may be > 0 even without new trades)

Data generation:
  - Backtesting: generate continuous date spine programmatically
  - Production: record at EOD regardless of activity; system fills defaults
```

**Why required:** Excluding inactive days inflates Sharpe ratio by removing risk-free days. The return series must include every elapsed day to produce an unbiased annualized Sharpe.

---

## 3. Drawdown Tracking Schema

### 3.1 Drawdown State

```sql
CREATE TABLE drawdown_state (
    date                    DATE PRIMARY KEY,
    current_bankroll        DECIMAL(12,2) NOT NULL,
    peak_bankroll           DECIMAL(12,2) NOT NULL,   -- All-time high
    current_drawdown_pct    DECIMAL(6,4) NOT NULL,    -- (peak - current) / peak
    max_drawdown_pct        DECIMAL(6,4) NOT NULL,    -- Historical max depth
    max_drawdown_peak_date  DATE,                      -- Date of peak_bankroll
    max_drawdown_trough_date DATE,                     -- Date when max depth occurred
    days_since_peak         INTEGER NOT NULL,          -- Calendar days since last ATH
    consecutive_drawdown_days INTEGER DEFAULT 0,       -- Streak of non-positive returns
    drawdown_status         TEXT DEFAULT 'NORMAL'      -- NORMAL | WARNING | HARD_STOP
);
```

### 3.2 Drawdown Thresholds

| Field | Value | Trigger | Action |
|-------|-------|---------|--------|
| `warning_threshold` | 0.10 (10%) | current_drawdown_pct ≥ 0.10 | Reduce f_kelly by 50%; flag to operator |
| `hard_stop_threshold` | 0.20 (20%) | current_drawdown_pct ≥ 0.20 | Halt all new positions; liquidate open positions at best effort; escalate to Marty |
| `recovery_threshold` | 0.05 (5%) | current_drawdown_pct drops below 0.05 after trigger | Restore normal sizing |

**Schema-Level Fields (configurable constants, not per-row):**

```yaml
drawdown_constants:
  warning_threshold: 0.10
  hard_stop_threshold: 0.20
  recovery_threshold: 0.05
  f_kelly_reduction_at_warning: 0.50   # Multiply current f_kelly by this
  liquidation_speed: "best_effort"     # At hard stop
  escalate_to: "Marty Byrde (CFO)"
```

### 3.3 Time-Under-Water Tracking

Derived table from `drawdown_state`:

```sql
CREATE TABLE drawdown_recovery_events (
    event_id            UUID PRIMARY KEY,
    start_date          DATE NOT NULL,           -- First day drawdown crossed warning
    end_date            DATE,                     -- Day drawdown recovered below warning
    trough_bankroll     DECIMAL(12,2) NOT NULL,   -- Lowest bankroll during event
    trough_drawdown_pct DECIMAL(6,4) NOT NULL,    -- Maximum depth during event
    duration_days       INTEGER,                  -- end_date - start_date
    recovery_days       INTEGER,                  -- Days from trough to recovery
    trigger_stop        BOOLEAN DEFAULT FALSE,    -- Did this event trigger hard stop?
    notes               TEXT
);
```

**Recovery Time Distribution Schema:**

```sql
CREATE TABLE recovery_time_distribution (
    bucket_label       TEXT PRIMARY KEY,          -- e.g., "0-5 days", "6-10 days", ">30 days"
    bucket_low         INTEGER,                   -- Inclusive lower bound
    bucket_high        INTEGER,                   -- Inclusive upper bound (NULL = unbounded)
    event_count        INTEGER,
    pct_of_total       DECIMAL(5,2),
    avg_severity_pct   DECIMAL(6,4)
);
```

---

## 4. Concentration & Correlation Model

### 4.1 Portfolio-Level Concentration Exposure

```sql
CREATE TABLE concentration_snapshot (
    date                    DATE NOT NULL,
    total_bankroll          DECIMAL(12,2) NOT NULL,
    per_station_exposure    JSONB,        -- { "KLAX": 0.023, "KORD": 0.017, ... }
    per_market_type_exposure JSONB,       -- { "temperature": 0.045, "precipitation": 0.038 }
    sector_exposure         DECIMAL(6,4), -- Total weather sector (always 1.0 = all weather)
    top_station_pct         DECIMAL(6,4), -- Highest single-station exposure %
    top_market_type_pct     DECIMAL(6,4), -- Highest single-type exposure %
    herfindahl_index        DECIMAL(8,6), -- Σ(station_weight²) — market concentration
    PRIMARY KEY (date)
);
```

**Exposure Calculation:**

```
station_weight_i = Σ(position_dollars_for_station_i) / bankroll
market_type_weight_j = Σ(position_dollars_for_type_j) / bankroll

Herfindahl-Hirschman Index (HHI):
  HHI = Σ(station_weight_i²)
  
  Interpretation:
    HHI < 0.10  → Well diversified
    HHI 0.10-0.15 → Moderate concentration
    HHI > 0.15  → Concentrated — flag for review
```

### 4.2 Correlation Estimator

Use **rolling realized pairwise return correlation** between stations:

```sql
CREATE TABLE station_correlation_matrix (
    snapshot_date       DATE NOT NULL,
    station_a           TEXT NOT NULL,
    station_b           TEXT NOT NULL,
    pairwise_corr       DECIMAL(8,6),       -- Pearson correlation of daily returns
    rolling_window_days INTEGER NOT NULL,    -- Days used (default: 60)
    n_obs               INTEGER,            -- Actual observations in window
    significant_95      BOOLEAN,            -- p < 0.05
    PRIMARY KEY (snapshot_date, station_a, station_b)
);

-- Enforce lower-triangular: station_a < station_b
-- INSERT should check: IF station_a >= station_b THEN swap
```

**Default correlation assumptions (from existing analysis):**

| Parameter | Value | Source |
|-----------|-------|--------|
| Pairwise station rho | ~0.0625 | Existing empirical analysis |
| Window | 60 trading days | ~3 months of data (~2.5 correlation halflife) |
| Min obs for valid estimate | 20 days | Below this, use default rho = 0.0625 |

**Implementation guidance — rolling estimator:**

```
Given daily return series for stations A and B over window D:
  r_a = array of daily_returns for station A
  r_b = array of daily_returns for station B
  
  pairwise_corr = Pearson(r_a, r_b) = cov(r_a, r_b) / (σ_a × σ_b)

  Where:
    cov(r_a, r_b) = Σ((r_a_i - μ_a) × (r_b_i - μ_b)) / (n - 1)
    σ_a = sqrt(Σ(r_a_i - μ_a)² / (n - 1))
```

### 4.3 Correlation-Adjusted Portfolio Variance

Compute portfolio-level risk using the full correlation matrix:

```
Portfolio Variance = Wᵀ Σ W

Where:
  W = weight vector (station weights from [4.1])
  Σ = covariance matrix derived from:
        Cov(i,j) = σ_i × σ_j × R(i,j)
        where σ_i = daily return std for station i
              R(i,j) = pairwise correlation from [4.2]

Portfolio Std = sqrt(Portfolio Variance)

Annualized Portfolio Std = Portfolio Std × sqrt(252)
```

**Schema for portfolio risk snapshot:**

```sql
CREATE TABLE portfolio_risk_snapshot (
    date                    DATE PRIMARY KEY,
    portfolio_variance      DECIMAL(16,10),
    portfolio_std_daily     DECIMAL(10,6),
    portfolio_std_annual    DECIMAL(10,6),
    assumed_rho             DECIMAL(8,6),       -- Average pairwise rho used
    n_stations              INTEGER,
    hhi_index               DECIMAL(8,6),
    diversification_ratio   DECIMAL(8,6),       -- Σ(weight_i × σ_i) / portfolio_std_daily
    max_single_station_std  DECIMAL(10,6),
    notes                   TEXT
);
```

**Diversification Ratio:**

```
DR = Σ(w_i × σ_i) / σ_portfolio

Where:
  w_i        = weight of station i
  σ_i        = daily std of station i
  σ_portfolio= sqrt(Wᵀ Σ W)

Interpretation:
  DR = 1.0  → No diversification benefit (perfect correlation)
  DR > 1.0  → Diversification working (typical range: 1.5-3.0 for weather)
```

---

## 5. Bootstrap Confidence Intervals

### 5.1 Block Bootstrap Methodology

Stationary bootstrap (Politis & Romano, 1994) with geometric block lengths:

```yaml
bootstrap_config:
  method: "stationary_bootstrap"
  n_resamples: 10000                          # Number of bootstrap resamples
  default_block_size: 5                       # Expected geometric block length (trading days)
  seed: 42                                    # Reproducible seed
  metrics:
    - "sharpe_ratio"
    - "mean_daily_return"
    - "max_drawdown"
    - "pct_5_pnl"
    - "pct_25_pnl"
    - "pct_50_pnl"
    - "pct_75_pnl"
    - "pct_95_pnl"
```

**Block Size Recommendation:**

```
Optimal block size based on autocorrelation:
  Step 1: Compute autocorrelation function (ACF) of daily returns
  Step 2: Find lag L where ACF first crosses zero (or becomes insignificant)
  Step 3: block_size = floor(L * 0.5)  (half the autocorrelation length)

  Conservative default: block_size = 5 (if ACF data unavailable)
```

### 5.2 Bootstrap Results Table

```sql
CREATE TABLE bootstrap_results (
    analysis_date           DATE NOT NULL,
    metric                  TEXT NOT NULL,         -- 'sharpe_ratio', 'mean_daily_return', etc.
    point_estimate          DECIMAL(12,6),
    ci_lower_95            DECIMAL(12,6),          -- 2.5th percentile
    ci_upper_95            DECIMAL(12,6),          -- 97.5th percentile
    ci_lower_90            DECIMAL(12,6),          -- 5th percentile
    ci_upper_90            DECIMAL(12,6),          -- 95th percentile
    p_value                DECIMAL(8,6),           -- Bootstrap p-value for H0: metric ≤ 0
    n_resamples            INTEGER,
    block_size             INTEGER,
    n_effective            INTEGER,                -- Effective sample size (autocorrelation-adjusted)
    std_error              DECIMAL(12,6),           -- Bootstrap standard error
    PRIMARY KEY (analysis_date, metric)
);
```

### 5.3 Effective Sample Size (N_eff)

**Bartlett's formula** for autocorrelation-adjusted sample size:

```
n_eff = n_total / (1 + 2 × Σ(ρ_k))

Where:
  n_total = total calendar days in return series
  ρ_k     = autocorrelation at lag k
  Σ runs from k=1 to k=K where K = max lag with significant autocorrelation
```

**Truncation:** For small samples (n < 60), use `n_eff = n_total` as default and flag with note.

### 5.4 Bootstrap Procedure (Pseudocode)

```
function stationary_bootstrap(return_series, n_resamples, expected_block_length):
    n = len(return_series)
    results = []
    
    for i in 1..n_resamples:
        indices = []
        t = uniform_random(0, n-1)  # Starting position
        while len(indices) < n:
            block_length = geometric_random(mean=expected_block_length)
            for j in 0..block_length-1 and len(indices) < n:
                idx = (t + j) % n     # Wrap around for circularity
                indices.append(idx)
            t = uniform_random(0, n-1)  # New random start
        resampled = return_series[indices]
        results.append(compute_metrics(resampled))
    
    return {
        metric: {
            point_estimate: compute_metric(original_series),
            ci_lower_95: percentile(results[metric], 2.5),
            ci_upper_95: percentile(results[metric], 97.5),
            ci_lower_90: percentile(results[metric], 5.0),
            ci_upper_90: percentile(results[metric], 95.0),
            std_error: std(results[metric]),
            p_value: count_where_below_zero / n_resamples
        }
        for metric in metrics
    }
```

---

## 6. Acceptance Model

### 6.1 Metric Thresholds

| Metric | "Good Enough" | "Promotion Grade" | Weight | Calculation |
|--------|-------------|------------------|--------|-------------|
| Portfolio Sharpe (daily, full-cost) | ≥0.5 | ≥1.0 | 0.30 | Annualized from `daily_pnl` returns, inclusive of all fees and slippage |
| Bootstrap CI lower bound (Sharpe) | ≥0.0 | ≥0.5 | 0.20 | 95% CI lower bound from [5.2] |
| Max Drawdown | ≤30% | ≤20% | 0.20 | From `drawdown_state.max_drawdown_pct` |
| Single-station concentration | ≤25% | ≤15% | 0.10 | From `concentration_snapshot.top_station_pct` |
| N_eff / N_total ratio | ≥0.5 | ≥0.7 | 0.10 | Bartlett adjustment from [5.3] |
| Bootstrap p-value (Sharpe > 0) | ≤0.10 | ≤0.05 | 0.10 | From `bootstrap_results.p_value` |

**Composite Score:**

```
composite_score = Σ(weight_i × normalized_score_i)

Where:
  normalized_score_i = actual / target_for_promotion  (capped at 1.0)
  
  Promotion gate:  composite_score ≥ 0.80 AND all individual "Good Enough" PASSED
  Good Enough gate: composite_score ≥ 0.60 AND all individual "Good Enough" PASSED
```

### 6.2 Acceptance Gate Checklist

```yaml
checklist_promotion_grade:
  - metric: portfolio_sharpe_annualized
    operator: ">="
    threshold: 1.0
    source: daily_pnl_returns_full_cost
  - metric: sharpe_ci_lower_95
    operator: ">="
    threshold: 0.5
    source: bootstrap_results WHERE metric = 'sharpe_ratio'
  - metric: max_drawdown_pct
    operator: "<="
    threshold: 0.20
    source: drawdown_state.max_drawdown_pct
  - metric: top_station_concentration
    operator: "<="
    threshold: 0.15
    source: concentration_snapshot.top_station_pct
  - metric: n_eff_ratio
    operator: ">="
    threshold: 0.7
    source: n_effective / n_total from bootstrap_results
  - metric: sharpe_p_value
    operator: "<="
    threshold: 0.05
    source: bootstrap_results WHERE metric = 'sharpe_ratio'

checklist_good_enough:
  - metric: portfolio_sharpe_annualized
    operator: ">="
    threshold: 0.5
  - metric: sharpe_ci_lower_95
    operator: ">="
    threshold: 0.0
  - metric: max_drawdown_pct
    operator: "<="
    threshold: 0.30
  - metric: top_station_concentration
    operator: "<="
    threshold: 0.25
  - metric: n_eff_ratio
    operator: ">="
    threshold: 0.5
  - metric: sharpe_p_value
    operator: "<="
    threshold: 0.10
```

### 6.3 PASS/FAIL Result Schema

```sql
CREATE TABLE acceptance_gate_result (
    analysis_date           DATE NOT NULL,
    gate_type               TEXT NOT NULL,       -- 'promotion_grade' | 'good_enough' | 'promotion_dry_run'
    overall_result          TEXT NOT NULL,       -- 'PASS' | 'FAIL' | 'PENDING'
    composite_score         DECIMAL(6,4),
    checklist_results       JSONB,              -- [{metric, operator, threshold, actual, pass: bool}, ...]
    failing_metrics         TEXT[],              -- Names of failed checks
    recommendation          TEXT,                -- 'PROMOTE', 'HOLD', 'PAUSE', 'REJECT'
    recommended_action      TEXT,                -- Human-readable next steps
    reviewed_by             TEXT,                -- Agent/operator who ran gate
    reviewed_at             TIMESTAMP,
    notes                   TEXT,
    PRIMARY KEY (analysis_date, gate_type)
);
```

### 6.4 Promotion Decision Flow

```
New backtest or trading period available
                    │
                    ▼
         Run Good Enough Gate
         ┌─────────────────────┐
         │ All checks PASS?    │──NO──→ HOLD: flag failing metrics, iterate
         └──────────┬──────────┘
                    │ YES
                    ▼
           Engines healthy enough to
           continue accumulating data
                    │
                    ▼
         Run Promotion Grade Gate
         ┌─────────────────────┐
         │ All checks PASS?    │──NO──→ CONTINUE ACCUMULATION
         └──────────┬──────────┘         (not a regression)
                    │ YES
                    ▼
         Flag for Marty Review
         ┌────────────────────────┐
         │ Marty signs off?      │──NO──→ HOLD with notes
         └──────────┬────────────┘
                    │ YES
                    ▼
            PROMOTE: Enable scaled
            allocation & paper trading
                    │
                    ▼
         Set baseline bankroll,
         begin live paper trading
         with Good Enough sizing
```

---

## Appendix: Reference Calculations

### A.1 Current State (from existing analysis)

| Metric | Value | Status |
|--------|-------|--------|
| Daily Sharpe (full cost, corr-corrected) | 0.35–0.44 | ❌ Below Good Enough (0.5) |
| Trade-level proxy Sharpe | 1.60–4.65 | ⚠️ Inflated — not usable |
| Pairwise station correlation | ~0.0625 | ✅ Acceptable for diversification |
| Fee model | 7% taker of notional | ✅ Official Kalshi rate |
| Initial bankroll (configurable) | $1,000 | ✅ Sizing target |

### A.2 Example Calculation: Kelly for YES at P=0.60

```
Given:
  p = 0.65 (model confidence)
  P = 0.60 (contract price)
  bankroll = $1,000
  f_kelly = 0.25

b = (1 - 0.60) / 0.60 = 0.6667
f* = (0.65 × 0.6667 - 0.35) / 0.6667 = 0.1250 (12.5% full Kelly)

position_dollars = 0.25 × 0.1250 × $1,000 = $31.25
contract_count = floor($31.25 / 0.60) = 52 contracts
position_risk = 52 × $0.60 = $31.20
fees = $31.20 × 0.07 = $2.18
net_risk = $31.20 + $2.18 = $33.38
max_allowed = $1,000 × 0.05 = $50.00  → actual = $31.20 (under limit)
```

### A.3 Example: Correlation-Adjusted Portfolio Std

```
Given:
  2 stations, equal weights (50/50)
  σ_A = 0.012 (daily)
  σ_B = 0.015 (daily)
  ρ = 0.0625

σ_portfolio = sqrt(0.5² × 0.012² + 0.5² × 0.015² + 2 × 0.5 × 0.5 × 0.012 × 0.015 × 0.0625)
            = sqrt(0.000036 + 0.00005625 + 0.000005625)
            = sqrt(0.000097875)
            = 0.009893 (daily)

Annualized = 0.009893 × sqrt(252) = 0.1570 (15.7%)

vs assuming ρ=0:
  σ_portfolio(ρ=0) = sqrt(0.000036 + 0.00005625) = 0.009608
  Difference: +2.96% due to correlation

vs assuming ρ=1:
  σ_portfolio(ρ=1) = sqrt(0.000036 + 0.00005625 + 2 × 0.5 × 0.5 × 0.012 × 0.015 × 1.0)
                    = sqrt(0.000036 + 0.00005625 + 0.00009)
                    = 0.01350
  Underestimation from ignoring correlation: NA (this is a bound, not estimator)
```

### A.4 Acceptance Status (current snapshot)

Based on current Sharpe estimate of 0.35–0.44:

| Gate | Status | Notes |
|------|--------|-------|
| Good Enough | ❌ FAIL | Sharpe 0.35 < 0.50 threshold |
| Promotion Grade | ❌ FAIL | Far below all thresholds |

The current economics do not yet support either gate. The schema is ready for when strategy improvements (lower fees, better signal, or contract selection) bring metrics into range.

---

*End of Schema Document v1*