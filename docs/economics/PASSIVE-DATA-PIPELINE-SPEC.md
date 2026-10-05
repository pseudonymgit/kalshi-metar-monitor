<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# Passive Data Pipeline Spec — Bridge from Schema to Executable Economics

**Author:** Marty Byrde (CFO)  
**Date:** 2026-08-23  
**Status:** Collection Spec (implementable, not deployed)  
**Gate:** 🔒 Requires Stream 3 dry-run clearance before execution
**Boundaries:** ❌ No credentials used in this spec, ❌ no shared worktree mutation, ❌ no API calls

---

## 1. Purpose

Define the **data collection pipeline** that populates the cost and risk schemas from passive Kalshi API data. This is the bridge between "schema complete" (delivered) and "executable economics" (pending). When Stream 3's dry-run gate passes, this spec becomes the implementation blueprint.

**Three-layer separation:**
- **Schema Complete** ✅ — Cost schema tables, risk/sizing tables, calibration table, acceptance gates
- **Pipeline Spec** 📄 — This document: what to collect, from where, at what frequency, with what structure
- **Executable Economics** 🔒 — Live P&L with real fills, fees, slippage, sizing (requires gate clearance)

---

## 2. Data Sources

### 2.1 Kalshi Public REST API (No Credentials Required for Read-Only)

| Endpoint | Purpose | Auth Required | Rate Limit |
|----------|---------|---------------|------------|
| `GET /trade-api/v2/markets?ticker={ticker}` | Market metadata, current price, close time | No | ~10 req/s |
| `GET /trade-api/v2/markets/{ticker}` | Single market detail | No | ~10 req/s |
| `GET /trade-api/v2/portfolio/markets` | Open positions for logged-in user | **Yes** | — |

> **Important:** The credential-free endpoints above are documented in Kalshi's public API docs. The spec references them for completeness. No actual calls will be made until the gate opens.

### 2.2 Market Structure Data (Passive Collection)

| Data Type | Collection Method | Storage Target | Frequency |
|-----------|-----------------|----------------|-----------|
| Level-1 quote (best bid/ask) | Poll `GET /markets/{ticker}` | `quote_snapshots` | Every 30s per active market |
| Level-2 depth (5 levels) | Poll `GET /markets/{ticker}/orderbook` | `quote_snapshots` | Every 60s per active market |
| Trade history (fills) | WebSocket or REST poll | `fill_attempts` | Every fill (event-driven) or 5min poll |
| Settlement results | Poll `GET /markets/{ticker}` after close | `trade_journal` + `settlement_truth` | After market close + 1 hour |
| Market list (all active) | Poll `GET /markets?limit=100&status=open` | `market_registry` | Every 15 min |

### 2.3 Active Market Identification

Target markets for weather contracts:

```
TICKER PATTERNS:
  HIGHTEMP-{DATE}-{STATION}   → HIGH market
  LOWTEMP-{DATE}-{STATION}    → LOW market
  
STATIONS (20 verified):
  KNYC, KLAX, KORD, KATL, KBOS, KDEN, KDFW, KDTW, KSEA, KMIA,
  KMSP, KPHL, KPHX, KSFO, KIAH, KMCO, KOKC, KMSY, KLAS, KSTL
  
DATE FORMAT: DDMMMYY (e.g., 27JUN26)
```

Peak active contracts at any time: ~40 markets (20 stations × HIGH/LOW for today+tomorrow).

---

## 3. Collection Pipeline Architecture

### 3.1 Pipeline Stages

```
┌─────────────────────────────────────────────────────────────────┐
│                    DATA COLLECTION LAYER                         │
│  (Credential-free, read-only API calls, no trade execution)      │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Stage 1: Market Discovery                                        │
│  ┌─────────────────────────────────────┐                         │
│  │ Poll /markets endpoint for          │ ──→ market_registry    │
│  │ weather tickers (15min cron)        │     table              │
│  └──────────────────────┬──────────────┘                         │
│                         │ discover new/existing                  │
│                         ▼                                        │
│  Stage 2: Quote Collection                                        │
│  ┌─────────────────────────────────────┐                         │
│  │ For each active weather market:     │ ──→ quote_snapshots    │
│  │  - Level-1: poll every 30s          │     table              │
│  │  - Level-2: poll every 60s          │                         │
│  │  - Guard: skip on market close      │                         │
│  └──────────────────────┬──────────────┘                         │
│                         │ aggregate                               │
│                         ▼                                        │
│  Stage 3: Market State Snapshot                                    │
│  ┌─────────────────────────────────────┐                         │
│  │ For each active market at end-of-day│ ──→ portfolio_state    │
│  │  - Record closing bid/ask           │     snapshot table      │
│  │  - Record open interest             │                         │
│  │  - Record settlement price (if due) │                         │
│  └──────────────────────┬──────────────┘                         │
│                         │ capture results                         │
│                         ▼                                        │
│  Stage 4: Settlement Capture                                      │
│  ┌─────────────────────────────────────┐                         │
│  │ For settled markets:                │ ──→ settlement_truth   │
│  │  - Poll 1h after close              │     table (existing)    │
│  │  - Record settlement_bucket + price │                         │
│  │  - Flag for trade reconciliation    │                         │
│  └─────────────────────────────────────┘                         │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Storage Requirements

| Table | Est. Daily Rows | Est. Storage/Day | Retention |
|-------|----------------|------------------|-----------|
| `quote_snapshots` | 40 markets × 60 polls/hr × 8hr = ~19,200 | ~3 MB | 90 days (rolling) |
| `fill_attempts` | 40 markets × ~5 fills/day = ~200 | ~0.1 MB | 90 days |
| `market_registry` | ~40 rows (static, updated) | ~0.01 MB | Indefinite |
| `portfolio_state` | 2 snapshots/day × 40 markets = 80 | ~0.05 MB | Indefinite |
| `cost_model_calibration` | ~20 station×market_type × 1 row | ~0.01 MB | Latest version only (historical in versioned DB) |

**Total daily storage: ~3.2 MB.** 90-day retention = ~288 MB peak. Negligible operational cost.

### 3.3 Sampling Strategy

| Context | Frequency | Rationale |
|---------|-----------|-----------|
| **Normal trading hours** (10:00-17:00 UTC) | Every 30s Level-1, 60s Level-2 | Captures within-day spread variation |
| **Open/close windows** (first/last 15 min) | Every 10s Level-1, 30s Level-2 | Higher volatility at boundaries |
| **Outside trading hours** | 1 snapshot at market-close only | No meaningful quote changes |
| **Weekends/holidays** | No collection | Markets closed |
| **Expiration day** | Every 5s last 10 min before close | Maximum volatility, most important fills |

---

## 4. Data Flow: API Response → Database Table

### 4.1 Market Discovery ↔ `market_registry`

**API Endpoint:** `GET /trade-api/v2/markets?limit=100&status=open`

**Response mapping:**
```
API Response Field                → market_registry Column
ticker                           → market_ticker       (PK)
title                            → market_name
status                           → status
close_time                       → settlement_datetime
yes_bid / yes_ask                → not stored (live quotes go to quote_snapshots)
volume                           → not needed at this stage
```

**market_registry schema:**
```sql
CREATE TABLE market_registry (
    market_ticker       TEXT PRIMARY KEY,
    market_name         TEXT NOT NULL,
    station             TEXT,
    market_type         TEXT CHECK (market_type IN ('HIGH','LOW')),
    target_date         DATE,
    status              TEXT CHECK (status IN ('open','closed','settled')),
    settlement_datetime TIMESTAMPTZ,
    first_discovered_at TIMESTAMPTZ DEFAULT NOW(),
    last_updated_at     TIMESTAMPTZ DEFAULT NOW(),
    is_tracked          BOOLEAN DEFAULT TRUE,
    notes               TEXT
);
```

**Filter:** Only weather tickers matching `HIGHTEMP-*` or `LOWTEMP-*` are inserted. Non-weather markets are excluded.

### 4.2 Quote Collection ↔ `quote_snapshots`

**API Endpoint:** `GET /trade-api/v2/markets/{ticker}` (Level-1) + `GET /trade-api/v2/markets/{ticker}/orderbook` (Level-2)

**Response mapping (Level-1):**
```
Response Field                  → quote_snapshots Column
ticker                          → market_ticker
yes_bid                         → best_bid_price
yes_bid_size (if available)     → best_bid_size
yes_ask                         → best_ask_price
yes_ask_size (if available)     → best_ask_size
```

**Response mapping (Level-2):**
```
Orderbook Bids Array (5 levels) → bid_prices[], bid_sizes[]
Orderbook Asks Array (5 levels) → ask_prices[], ask_sizes[]
```

**Computed fields (insert-time):**
```
spread_cents    = best_ask_price - best_bid_price
mid_price       = (best_bid_price + best_ask_price) / 2
total_bid_depth = SUM(bid_sizes)
total_ask_depth = SUM(ask_sizes)
spread_bps      = (spread_cents / mid_price) × 10000
```

### 4.3 Fill Attempts ↔ `fill_attempts`

> **Note:** Fill-level data requires API methods that may need authentication (`GET /portfolio/orders`). If credential-free, this stage collects from public market trade history endpoints if available, or is deferred to the next gate.

**Alternative approach (no auth):** Poll `GET /markets/{ticker}/trades?limit=10` for recent fills. This shows market-wide trade activity without revealing our fills specifically. Useful for estimating fill probability and depth consumption.

### 4.4 Settlement Capture ↔ `settlement_truth` (Existing Table)

**API Endpoint:** `GET /trade-api/v2/markets/{ticker}` (after `close_time`)

**Response mapping:**
```
Response Field                  → settlement_truth Column
ticker                          → market_ticker
result                          → settlement_bucket   (e.g., "3")
settlement_price                → settlement_price    ($0 or $1)
```

**Note:** This table already exists and is populated from historical settlement data. The pipeline extends it to live settlement capture.

### 4.5 Data Flow Diagram (ASCII)

```
             ┌──────────────────┐
             │  Kalshi API      │
             │  (read-only)     │
             └────────┬─────────┘
                      │
          ┌───────────┴───────────┐
          │  Polling Scheduler    │
          │  (cron, 30s-60s)     │
          └───────────┬───────────┘
                      │
          ┌───────────┴───────────┐
          │  Response Parser      │
          │  (schema validator)   │
          └───────────┬───────────┘
                      │
          ┌───────────┴───────────────────────────────────────┐
          │            INSERT INTO database                    │
          ├───────────────────┬─────────────────┬──────────────┤
          │ quote_snapshots   │ fill_attempts   │ settlement   │
          │ (30s L1, 60s L2)  │ (per trade)     │ (post-close) │
          └───────────────────┴─────────────────┴──────────────┘
                      │
          ┌───────────┴───────────────────────────────────────┐
          │            COST MODEL CALIBRATION PIPELINE          │
          ├───────────────────┬─────────────────┬──────────────┤
          │ TWAS spread       │ Fill prob vs    │ Slippage     │
          │ estimation        │ actual fill     │ depth-walk   │
          │                   │ rate            │ validation   │
          └───────────────────┴─────────────────┴──────────────┘
                      │
          ┌───────────┴───────────────────────────────────────┐
          │  cost_model_calibration  │  acceptance_gate_result │
          │  (station×market_type)   │  (pass/fail per gate)   │
          └───────────────────────────────────────────────────┘
                      │
                      ▼
          Executable Economics
          (after Stream 3 dry-run gate)
```

---

## 5. Pipeline Implementation Spec

### 5.1 Collector Script Structure

```python
# Script: scripts/collect_market_data.py
# Schedule: cron (every 30s during market hours)
# Auth: None (public API only)
# Boundaries: No credentials, no trade execution, no shared worktree mutation

# ── Config ──
STATIONS = [...]          # 20 verified station IDs (from METAR mapping)
MARKET_PATTERN = r'^(HIGH|LOW)TEMP-(\d{2}[A-Z]{3}\d{2})-([A-Z]{4})$'
POLL_INTERVAL_L1 = 30     # seconds
POLL_INTERVAL_L2 = 60     # seconds
STORAGE_PATH = "data/passive_market_data/"  # Outside shared worktree

# ── Stages ──
# Stage 1: Discover active weather markets (15min cron)
#   - Parse /markets response
#   - Filter by ticker pattern
#   - Upsert into market_registry
#
# Stage 2: Collect quotes for active markets (30-60s cron)
#   - Fetch Level-1 (best bid/ask)
#   - Fetch Level-2 (5-level depth) every other run
#   - Compute derived fields (spread, mid, depth)
#   - INSERT into quote_snapshots
#
# Stage 3: Capture settlement (post-close cron)
#   - Check market_registry for recently-closed markets
#   - Fetch settlement data
#   - UPSERT into settlement_truth
#
# Stage 4: Run calibration (daily cron)
#   - Compute cost model estimates vs actuals
#   - INSERT new calibration_version
#   - Evaluate acceptance gate
```

### 5.2 Scheduling

| Cron | Frequency | Stage | Notes |
|------|-----------|-------|-------|
| Market discovery | Every 15 min during trading hours (10:00-17:00 UTC) | 1 | Discovers new weather contracts as they list |
| Quote collection | Every 30s (L1), every 60s (L2) | 2 | Primary data volume driver (~19K rows/day) |
| Settlement check | Every 60 min | 3 | Polls closed markets for settlement results |
| Calibration run | Once daily after market close (18:00 UTC) | 4 | Updates cost_model_calibration table |

### 5.3 Error Handling

| Failure Mode | Detection | Recovery |
|-------------|-----------|----------|
| API timeout (>5s) | Timeout | Retry once after 1s. If still fails, skip and log. |
| API 429 (rate limit) | HTTP 429 | Exponential backoff: 1s, 2s, 4s, 8s, skip. Log rate-limit event. |
| API 5xx | HTTP 5xx | Retry once after 2s. If still fails, skip and flag for manual review. |
| Connection error | Socket error | Backoff and retry up to 3 times. After 3 failures, log alert. |
| Invalid response (missing fields) | Schema validation | Skip row, log malformed response for review. Never INSERT partial data. |
| DB write failure | INSERT error | Log error, buffer to local JSON for replay. |

### 5.4 Data Quality Monitoring

| Check | Frequency | Alert If |
|-------|-----------|----------|
| Quote count by station | Daily | Any station has <10 quotes between 10:00-17:00 UTC |
| Spread outlier detection | Daily | Any quote has spread > 5× station's 7-day moving average |
| Missing settlement | Daily | Any closed market missing settlement after 24h |
| Fill rate drop | Daily | Station fill rate drops >20pp from 7-day average |
| Collection lag | Every run | Time since last successful quote > 120s for any tracked market |

---

## 6. Schema-to-Collection Mapping

### Schema Tables (from Cost Schema v1) ↔ Collection Source

| Schema Table | Collection Stage | Source Endpoint | Pre-population Needed? |
|-------------|-----------------|-----------------|----------------------|
| `quote_snapshots` | Stage 2 | `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook` | None (first run populates) |
| `fill_attempts` | Stage 2 (public trades) | `GET /markets/{ticker}/trades` | None (first run populates) |
| `market_registry` | Stage 1 | `GET /markets?limit=100` | None (first run populates) |
| `portfolio_state` | Stage 3 (end-of-day) | Derived from quote_snapshots | First run needs previous day to compute |
| `cost_model_calibration` | Stage 4 (daily) | Aggregate from quote_snapshots + fill_attempts | None (computed after 1 day of data) |
| `acceptance_gate_result` | Stage 4 (daily) | Computed from calibration | None (computed after calibration) |
| `trade_journal` | — | Requires auth | ❌ **Blocked** — needs credential |
| `settlement_truth` | Stage 3 | `GET /markets/{ticker}` post-close | Historical data already populated |

### Key Gap: `trade_journal`

The `trade_journal` table from the cost schema **cannot be populated without a credential**. It requires trade-specific data (entry price, exit price, fill details) tied to our orders. Until the gate opens, this table remains a schema-only artifact.

**Workaround:** The calibration pipeline can still run using `fill_attempts` data from public trade history and `quote_snapshots`. It won't have *our* trade-specific P&L, but it will have market-wide fill probability estimation, spread calibration, and slippage modeling. When the credential becomes available, the `trade_journal` population can be backfilled as a one-time migration.

---

## 7. Pipeline Dependencies Graph

```
Acceptance Gate ─┐
                 ├── Cost Schema v1 ──┐
                 │                    ├── Quote Snapshots ←── API Level-1 (no auth)
                 │                    ├── Fill Attempts   ←── API Trades (no auth)
                 │                    └── Trade Journal   ←── ❌ Requires auth
                 │
                 ├── Risk/Sizing Schema ──┐
                 │                        ├── Daily P&L    ←── Derived from trade_journal ❌
                 │                        ├── Drawdown      ←── Derived from daily P&L ❌
                 │                        ├── Concentration ←── Derived from trade_journal ❌
                 │                        └── Bootstrap CI  ←── Derived from daily P&L ❌
                 │
                 └── Calibration Table ───←── Quote + Fill data (no auth) ✅
```

**Green path (credential-free):** Quote snapshots + fill attempts → calibration table → spread accuracy, fee formula, fill probability, slippage bounds validated. ~40% of acceptance criteria gateable.

**Red path (needs credential):** Trade journal → daily P&L → drawdown → concentration → bootstrap CI. ~60% of acceptance criteria, including the full portfolio Sharpe gate.

---

## 8. Implementation Sequence (After Dry-Run Clearance)

| Step | Task | Depends On | Est. Effort | Credential? |
|------|------|-----------|-------------|-------------|
| 1 | Deploy market discovery cron (15min) | None | 2h | ❌ No |
| 2 | Deploy quote collection cron (30s L1) | Step 1 | 4h | ❌ No |
| 3 | Deploy quote collection cron (60s L2) | Step 2 | 2h | ❌ No |
| 4 | Deploy settlement capture cron (60min) | Step 1 | 2h | ❌ No |
| 5 | Deploy calibration pipeline (daily) | Steps 2-4 | 4h | ❌ No |
| 6 | Backfill trade_journal from credential | Step 5 | 4h | **✅ Yes** |
| 7 | Compute daily portfolio P&L | Step 6 | 2h | **✅ Yes** |
| 8 | Run Good Enough acceptance gate | Step 7 | 1h | **✅ Yes** |

**Credential-free path delivers Steps 1-5:** market discovery, quote collection, settlement capture, and calibration pipeline. This populates ~40% of the acceptance criteria and provides cost-model validation before any trade execution.

---

## 9. Credential-Free Dry Run Plan

### Phase 1: Pipeline Scaffolding (No API Calls)

| Task | What | Deliverable |
|------|------|-------------|
| 1a | Write collector script skeleton | `scripts/collect_market_data.py` (type-stub, no live fetches) |
| 1b | Write DB migration for market_registry table | SQL migration script |
| 1c | Write DB migration for quote_snapshots table | SQL migration script (cost-schema-compatible) |
| 1d | Write DB migration for cost_model_calibration table | SQL migration script |
| 1e | Write cron entry specs for all 3 schedules | Cron config documentation |

### Phase 2: Offline Validation (No API Calls)

| Task | What | Deliverable |
|------|------|-------------|
| 2a | Run fixture JSON generation | 3 fixture files from FIXTURE-VALIDATION-SPEC.md |
| 2b | Run fixture → SQLite pipeline | Validate schema, indexes, FK constraints |
| 2c | Run acceptance gate on fixture data | Gate output JSON |
| 2d | Validate all 7 criteria against fixture data | Pass/fail report |

### Phase 3: Live Dry Run (After Gate Clearance)

| Task | What | Deliverable |
|------|------|-------------|
| 3a | Enable market discovery with empty filter (read-only test) | One API call to verify connectivity |
| 3b | Enable quote collection for 1 station × 2 markets | 1 hour of snapshot data |
| 3c | Run calibration pipeline on collected data | First calibration_version |
| 3d | Evaluate acceptance gate on 1-hour data | Partial gate result |

---

## 10. Boundary Enforcement

| Prohibited Action | Enforcement | Check |
|------------------|-------------|-------|
| Credential access | No credential variable in script config | Script uses public endpoints only |
| Trade execution | No order placement code in pipeline | All endpoints are GET-only |
| Shared worktree mutation | All data written to `data/passive_market_data/` | Script path validation |
| AI in trading loop | No LLM calls in collector loop | Script is deterministic only |
| Live capital changes | No notional or bankroll spec | Schema-only; no execution path |

---

*End of Passive Data Pipeline Spec v1*  
*Next: Generate fixture JSON files for offline validation (no credentials needed)*