# FUNCTIONALITY SPECIFICATION - Weather Engine v1.1 (2026-07-05)

## Overview

The OpenClaw Weather Engine is a deterministic system for generating, evaluating, and acting on temperature-based trading signals for Kalshi weather markets. It operates 24/7 via a Render-hosted collection service, processes data through multiple signal layers, and executes paper trades on 20 Kalshi markets with confidence-weighted sizing and disciplined risk management.

**Key Principle:** All operation is deterministic — no AI/ML calls inside collection, signal evaluation, or trading loops. Pure Python scripts and database operations only.

## 1. System Architecture & Data Flow

### Collection Layer (Render Server)
- **METAR Data** collected every 3-5 minutes during peak hours (12:00-24:00 UTC), 5 minutes off-peak
- **NWP Forecast Data** collected daily at 06:00 UTC  
- **Kalshi Market Snapshots** collected every 5 minutes during trading window (13:00-01:00 UTC), 15 minutes off-hours
- **Storage:** Shared SQLite databases (`metar_backfill.db`, `nwp_forecasts.db`, `alerts-prod.db`)
- **Cache:** Centralized for all instance types (DEV/SBOX/PROD)

### Processing Layer (All Instances)
- **Signal Generation:** 10+ signal types evaluated in real-time against incoming METAR data, with NEW N-of-M agreement gate validation
- **Hydration Cache:** Market eligibility and ladder data maintained for all 20 stations
- **State Management:** Epoch tracking, cooldown enforcement, boundary monitoring

### Trading Layer (Paper Trading Instances)
- **Position Sizing:** Confidence-weighted based on signal strength (HIGH/MEDIUM/LOW tiers)
- **Alert System:** Structured alerts sent to Discord with market links and visual delimiters
- **Execution Tracking:** Paper trades reconciled daily, P&L tracked

## 2. Signal Catalog

### A. Agreement Layer

A NEW N-of-M agreement filter exists as a final consensus validator before trade signals are processed. The gate requires minimum consensus among signals before emitting a trade signal. Default configuration: 3 out of 9 signals must agree on direction after individual confidence thresholds. Implemented in `core/agreement_gate.py` and wired into `core/paper_trading_engine.py`.

### B. Core Structural Signals (Immediate Response)
1. **`instant_up` / `instant_down`**
   - **Trigger:** Temperature crosses integer bounds upward/downward
   - **Mechanism:** Real-time bucket tracking from METAR ingestion
   - **Direction:** UP/DOWN based on crossing direction
   - **Confidence:** Baseline (requires market eligibility check)

2. **`settlement_up`**  
   - **Trigger:** Daily maximum temperature increases
   - **Mechanism:** Settlement bucket computation from running daily max
   - **Direction:** Always UP (by definition)
   - **Confidence:** Baseline

### B. Sophisticated Predictive Signals  
3. **`reversion_after_settlement`** (`core/metar_monitor.py`)
   - **Trigger:** Temperature drops after settlement spike
   - **Logic:** After `settlement_up`, if temp falls below prior settlement value
   - **Direction:** DOWN (bets against continuation of spike)
   - **Confidence:** 0.65 (fixed)

4. **`lat_day_momentum_hourly`** (`core/late_day_momentum_hourly.py`)
   - **Trigger:** Strong temperature trends in 17:00-22:00 UTC window
   - **Logic:** Linear regression slope of temp vs. hour; threshold 1.7°F/hour
   - **Direction:** UP/DOWN based on positive/negative slope
   - **Confidence:** Scales with |slope| excess over 1.7°F/hour

5. **`calendar_climatology`** (`core/climatology_pillar.py`)
   - **Trigger:** Deviation from seasonal baseline
   - **Logic:** Compares current temp to historical average for date/station
   - **Direction:** UP/DOWN based on deviation direction from climatological norm
   - **Confidence:** 0.55 (fixed)

### C. Boundary & Pattern Signals
6. **`near_boundary_momentum_up` / `near_boundary_momentum_down`** (`core/metar_monitor.py`)
   - **Trigger:** Momentum detected when very close to integer boundaries (≤0.10°F from boundary)  
   - **Logic:** Monotonic temperature movement in boundary proximity with sufficient momentum
   - **Direction:** UP/DOWN based on movement direction
   - **Confidence:** Not explicitly computed, implicit tier

7. **`goldilocks_reversion_alert`** (`core/metar_monitor.py`)
   - **Trigger:** Temperature spikes above settlement then reverts (Tier 1 protected)
   - **Logic:** After `settlement_up`, detects "exceeded by one or more" then "reverted below settlement"
   - **Direction:** REVERSAL (bets on reversion after spike)
   - **Confidence:** Computed dynamically based on timing, magnitude, daily patterns
   - **Special:** Bypasses market eligibility check (Tier 1 protected)

8. **`goldilocks_momentum_down`** 
   - **Trigger:** Momentum detected in downward reversion after spike
   - **Logic:** Derived from the same epoch state as goldilocks_reversion but tracks downward momentum
   - **Direction:** DOWN
   - **Confidence:** Dynamic

### Signal Limitations
- **Coherence Windows:** Multiple signals for same station suppressed for 30-60 minutes after emission  
- **Market Eligibility:** Most signals require active Kalshi markets; protected signals bypass (Goldilocks tiering)
- **Data Quality:** Requires consistent METAR observations; gaps affect momentum signals

## 3. Data Flow Architecture

### Input Sources → Process → Output Destinations

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│  NWS/NOAA APIs  │───▶│  METAR Database  │───▶│ Signal Evaluation   │
│    (Live)       │    │ (metar_backfill.db)│   │ (metar_monitor.py)  │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
       │                        │                           │
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│  Open-Meteo API │───▶│  NWP Database    │───▶│ Climatology Engine  │
│    (Scheduled)  │    │ (nwp_forecasts.db)│  │ (climatology_pillar.py)│
└─────────────────┘    └──────────────────┘    └─────────────────────┘
       │                        │                           │
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│ Kalshi REST API │───▶│  Alerts DB       │───▶│ Paper Trading       │
│    (Live)       │    │ (alerts-prod.db) │   │ (multi_instance_    │
└─────────────────┘    └──────────────────┘    │    paper_trader.py  │
                                                 └─────────────────────┘
```

### Key Databases & Structures
1. `metar_backfill.db` - Raw METAR obs, settlement_epochs, daily_stats with 800K+ hourly observations
2. `nwp_forecasts.db` - GFS/ECMWF/DWD forecasts by city/date/model 
3. `alerts-prod.db` - Transition events, structured state snapshots, correlation data
4. Per-instance `paper_trading_*.db` - Trade records, daily balances, P&L reconciliation for DEV/SBOX/PROD

## 4. Alert Format & Delivery Rules

### Standard Discord Alert Format
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[INSTANCE_TAG] 📍 Station: KDEN
📊 Market: HIGH  
📈 Direction: UP
💰 Size: $46.25
🌡️ Current bucket: 87
🎯 Trading bucket: 88
📉 Market odds: 0.55
✅ Trade Conf: MEDIUM (0.65)
🔝 Top signals: reversion_after_settlement (conf=0.65), calendar_climatology (conf=0.55)
📊 Sharpe: 1.2 | Coverage: 12/20 stations
💵 Running P&L: +$127.50
🔗 Market: https://kalshi.com/markets/KXHIGHDEN-26JUL05
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Delivery Rules
- **Non-Zero Position Size:** Alert fires only if computed position size > $0
- **Instance Tags:** `[PROD]`, `[DEV]`, `[SBOX]` prepended to distinguish source
- **Visual Separation:** Horizontal bars separate individual alerts in high-volume channels
- **Market Links:** Direct clickable links to Kalshi markets with event-specific URLs
- **Scheduler Guards:** fcntl-based locks prevent concurrent instance runs
- **Failure Handling:** Silent failures for network issues, alert logging continues to JSONL files

### Health Monitoring
- **Per-Instance Files:** `data/{instance}_health.json` with status, run stats, errors
- **Scheduler Status:** Runtime state accessible via instance_config API
- **Alert Logs:** Per-instance JSONL streams at `logs/alerts_{instance}.jsonl`

## 5. Risk Gate Controls & Position Sizing

### Confidence-Weighted Sizing (v1.1)
```python
HIGH_CONFIDENCE   (≥70%) → 1.5x base size (aggressive positioning)
MEDIUM_CONFIDENCE (50-69%) → 1.0x base size (standard positioning) 
LOW_CONFIDENCE    (<50%)  → 0.5x base size (conservative positioning)
```

Applied per instance type (base sizes: PROD $10K, DEV $5K, SBOX $1K starting balance)
Position sizing respects confidence as a proportional weight without arbitrary thresholds.

### Built-in Kill Switches
- **Hydration Failure:** No market eligibility → most signals suppressed (Tier 1 excepted)
- **Eligibility Count Check:** If eligible_markets_count ≤ 0 → most signals suppressed
- **Market Cooldowns:** Station/transition cooldown prevents signal spam (30-60 min)
- **Terminal State Detection:** Markets approaching settlement halt momentum signals early

### Economic Parameters
- **Commission Rate:** 0.05% per contract (realistic fee structure)
- **Leverage Constraints:** Notional position limited to prevent ruin scenarios
- **Cash Accounting:** Paper trading tracks account balance with settlement adjustments

## 6. Implementation Details

### Deterministic Guarantees
- **No API Calls Inside Processing Loop:** All market data cached during scheduled collections
- **Pure Database Operations:** Signal evaluation uses local SQLite queries
- **Static Config:** Position sizing, thresholds, cooldown periods are code-based, not model-trained
- **Idempotent Operations:** Identical inputs always produce identical outputs

### Runtime Architecture
- **Thread Safety:** Dedicated locking primitives (`_AUDIT_LOCK`, `_SIGNAL_LOCK`, fcntl) prevent race conditions
- **State Persistence:** Volatile states serialized to SQLite for crash recovery  
- **Resource Management:** SQLite timeouts, connection pooling, file descriptor handling

## 7. Current Gaps & Known Issues

### Special Architectural Features
- **Goldilocks Separate Lane:** Spike-reversion pattern signals (goldilocks) are processed separately when GOLDILOCKS_SEPARATE_LANE flag is enabled, bypassing the agreement gate and firing independently
- **Configurable Consensus:** Agreement gate N-of-M parameters are configurable via runtime constants

### Data Gaps (by Design)
- **Forecasct Disagreement Signal:** Cannot evaluate `GFS vs NWS forecast_difference` signals until settlement data extends to cover NWP overlap period
- **Seasonal Patterns:** Summer data for KMSP stations incomplete due to METAR gaps; affects boundary momentum

### Operational Blind Spots
- **Black Swan Detection:** Systemized temperature jumps not well detected (only gradual momentum)
- **Market Liquidity:** No spread/volume awareness in position sizing
- **Regime Changes:** Climate shifts, station modifications not adaptively handled

---

**Specification Valid:** Through 2026-07-05 (P0 batch completion)  
**Authoritative:** Supersedes all informal system descriptions  
**Frozen:** Alert schema v1.0 locked until v1.5 upgrade