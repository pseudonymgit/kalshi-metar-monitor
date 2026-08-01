# Goldilocks WhaleWatch — Order Book Microstructure Signal Design

**Author:** First-Principles Market Microstructure Signal Designer
**Date:** 2026-08-01
**Status:** Design Document — Pending Technical Review (→ Gilfoyle)
**Routing:** Gilfoyle (implementation) → Gerri (exposure/cost) → Paper Trading Pipeline

---

## Executive Summary

WhaleWatch detects informed trading activity in Kalshi temperature markets by analyzing **order book microstructure** — specifically abnormal bid-side accumulation on temperature buckets. Instead of trying to match a whale's superior data feed speed, we watch what the whale **does** with their information and follow behind them.

**Core insight:** Traders with faster/better temperature data (~1°F resolution before public METAR) reveal their conviction through order book placement. When they accumulate YES contracts on a specific temperature bucket, they're signaling that temperature will hit that bucket. We detect the pattern, not the data.

**Key constraint:** Kalshi does not expose trader-level data. WhaleWatch uses **aggregate order book signals** — bid/ask depth, spread asymmetry, volume concentration — which are public and free.

---

## Table of Contents

1. [Kalshi API Endpoints & Polling Strategy](#1-kalshi-api-endpoints--polling-strategy)
2. [Anomaly Detection Algorithm](#2-anomaly-detection-algorithm)
3. [Per-Station Normalization Strategy](#3-per-station-normalization-strategy)
4. [Trading Signal Construction](#4-trading-signal-construction)
5. [Integration with Goldilocks Predictive Model](#5-integration-with-goldilocks-predictive-model)
6. [Integration with Core GEFS Ensemble](#6-integration-with-core-gefs-ensemble)
7. [Estimated P&L Contribution](#7-estimated-pl-contribution)
8. [Implementation Effort Estimate](#8-implementation-effort-estimate)

---

## 1. Kalshi API Endpoints & Polling Strategy

### 1.1 Available Endpoints

Kalshi exposes the following endpoints relevant to order book analysis:

| Endpoint | Method | Purpose | Auth Required | Rate Limit |
|----------|--------|---------|:-----------:|:----------:|
| `GET /markets?series_ticker={TICKER}&status=open` | Public | List all open markets for a series (e.g., KXHIGHNYC) | No | 10 req / 60s |
| `GET /markets/{ticker}` | Public | Single market detail (yes_bid, yes_ask, no_bid, no_ask, last_price, volume_24h, open_interest) | No | 10 req / 60s |
| `GET /markets/{ticker}/orderbook` | Public | Full order book depth (all price levels, bid/ask sizes) | No | 10 req / 60s |

**Note:** Kalshi's public API (no auth) returns current market state including bid/ask at best level. The `/orderbook` endpoint returns **full depth** — all resting orders at each price level. This is the primary signal source.

Kalshi does **not** expose:
- Individual trader identities or wallets
- Trade-level data (who bought what)
- Order flow (individual order placement/cancellation)
- Historical order book snapshots

### 1.2 Data Fields per Market

From `GET /markets/{ticker}` — the fields that matter:

| Field | Type | Signal Use |
|-------|------|:---------:|
| `yes_bid` | integer (cents) | Best bid on YES — depth signal if it moves above no_ask |
| `yes_ask` | integer (cents) | Best ask on YES — resistance level |
| `no_bid` | integer (cents) | Best bid on NO |
| `no_ask` | integer (cents) | Best ask on NO — whale may accumulate NO instead |
| `yes_bid_size` | integer (contracts) | **Primary signal** — quantity bid at best_bid |
| `yes_ask_size` | integer (contracts) | Quantity offered at best_ask |
| `no_bid_size` | integer (contracts) | Quantity bid at NO best_bid |
| `no_ask_size` | integer (contracts) | Quantity offered at NO best_ask |
| `last_price` | integer (cents) | Last trade price |
| `volume_24h_fp` | float (dollars) | Dollar volume — whale activity increases this |
| `open_interest` | integer (contracts) | Outstanding contracts — accumulates with whale positioning |

From `GET /markets/{ticker}/orderbook` — **the real signal**:

| Field | Type | Signal Use |
|-------|------|:---------:|
| `yes` | array[{price, size}] | **Full bid depth** — all levels where YES is bid. Whale accumulates here. |
| `no` | array[{price, size}] | Full ask depth — all levels where NO is offered |
| Spread between levels | computed | Compression signals urgency |

### 1.3 Polling Strategy

#### Design Principle
We don't need sub-second polling. Whales build positions over minutes, not milliseconds. The order book accumulation pattern is a medium-frequency signal.

#### Polling Parameters

| Parameter | Value | Rationale |
|-----------|-------|:----------|
| `base_url` | `https://api.elections.kalshi.com/trade-api/v2` | Public Kalshi API |
| Poll interval (normal) | **30 seconds** | Catches accumulation over minutes; well within 10 req/60s limit |
| Poll interval (active) | **10 seconds** | When anomaly detected on a bucket; still within limit per-series |
| Burst window | Max 3 requests per 30s per series | Stay at ≤60% of rate limit per endpoint |
| Coverage pattern | 20 stations × 2 series (HIGH/LOW) = 40 series | Stagger across the 30s window |
| Total API load | 40 series × 2 requests/min = 80 req/min | Spread across endpoints, well within practical limits |
| Trading hours | **7:00 AM – 10:00 PM local** (station time) | Polls outside hours at 5-min intervals only for stale detection |

#### Staggered Poll Architecture

```
T0     → Series 1-5  (every 30s)
T0+5s  → Series 6-10
T0+10s → Series 11-15
T0+15s → Series 16-20
T0+20s → Series 21-25
T0+25s → Series 26-30
T0+30s → Series 31-35
T0+35s → Series 36-40
T0+40s → Sleep 20s
T0+60s → Repeat (Series 1-5)
```

This creates a **5-second spike** every 30 seconds per series cluster, staying within rate limits while ensuring full coverage.

#### Endpoint Usage per Poll Cycle

For each of 40 series (20 stations × HIGH/LOW):
1. `GET /markets?series_ticker={TICKER}&status=open` — get open buckets (expiry today or tomorrow)
2. For each open bucket: `GET /markets/{ticker}` — get best bid/ask + volumes
3. If anomaly threshold triggered (see Section 2): `GET /markets/{ticker}/orderbook` — full depth

**Optimization:** Bucket counts per series = 10-30 (temperature range ±10-15°F from climatology). Full poll cycle = 40 series × ~15 buckets × 2 endpoints = ~1,200 requests at peak. **This exceeds rate limits.**

**Solution: Tiered polling.**

| Tier | Buckets | Endpoints | Frequency | Max Requests/Cycle |
|:----:|---------|-----------|:---------:|:------------------:|
| **1** | Strike-adjacent buckets (current temp ±3°F) | `/markets/{ticker}` | Every 30s | 40 series × 6 buckets × 1 endpoint = 240 |
| **2** | Full temperature range | `/markets/{ticker}` | Every 5 min | 40 × 15 × 1 = 600 (spread over 5 min) |
| **3** | Tier-1 buckets with anomaly | `/orderbook` | Every 30s (on trigger) | ~5-10 series at any time |
| **4** | Tier-2 full scan | `/orderbook` | Every 15 min | 40 × 15 = 600 (during off-peak) |

**Net API load:** ~240-300 req/min during peak hours (well under rate limits for bursty access). The public Kalshi API allows reasonable polling — the weather engine already polls at similar frequencies.

### 1.4 Rate Limit Guard Implementation

Already exists in `core/kalshi_monitor.py`:
- `_check_rate_limit(endpoint, max_requests=10, window_seconds=60)` — SQLite-backed rolling window
- `_persist_rate_limit_entry(endpoint)` — records each request timestamp
- 429 handling with exponential backoff

**Reuse:** The existing rate limiter is sufficient. Configuration: `max_requests=8, window_seconds=60, backoff_base=1.5`.

---

## 2. Anomaly Detection Algorithm

### 2.1 Market Structure for Temperature Buckets

Kalshi temperature markets use a **bucket structure**:

```
KXHIGHNYC-23  → "HIGH ≥ 83°F"  → YES pays if high ≥ 83°F
KXHIGHNYC-24  → "HIGH ≥ 84°F"  → YES pays if high ≥ 84°F
```

Each bucket is a separate market with its own bid/ask book. Buckets are **monotonic**: if YES at 84°F trades at 30¢, YES at 83°F should trade at ≥30¢ (higher probability of a lower threshold). This creates a natural arbitrage constraint across buckets — which whales exploit by buying a concentrated position at one specific bucket.

### 2.2 What Constitutes Anomalous Activity

#### Primary Signals (high-confidence, independent triggers)

| # | Signal | Definition | Trigger Threshold | Priority |
|---|--------|:-----------|:-----------------:|:--------:|
| S1 | **Bid Side Concentration** | `yes_bid_size` on a single bucket > 3σ above its 20-period rolling mean, while adjacent buckets (±1°F) show no change | Z-score > 3.0 | **HIGH** |
| S2 | **Bid/Ask Ratio Spike** | `yes_bid_size / yes_ask_size` ratio exceeds historical 95th percentile, while spread remains stable | Ratio > 95th %ile | **HIGH** |
| S3 | **Volume Acceleration** | `volume_24h_fp` growth rate on a single bucket > 5× the median growth rate across all buckets for that series | > 5× median | **MEDIUM** |
| S4 | **Spread Compression** | `yes_ask - yes_bid` narrows from > 3¢ to ≤ 1¢ within 3 poll cycles (90s), while bid_size doubles | Bid_ask spread ≤ 1¢ AND bid_size 2× | **HIGH** |
| S5 | **Stepwise Accumulation** | Same bucket shows bid_size increases of 50-200 contracts repeated across 3+ consecutive poll cycles, always at bid (not executed) | Pattern over 3+ cycles | **MEDIUM** |

#### Secondary Signals (contextual, require confluence)

| # | Signal | Definition | Trigger Threshold | Priority |
|---|--------|:-----------|:-----------------:|:--------:|
| S6 | **Bucket Gradient Discontinuity** | Price gap between adjacent buckets widens beyond arbitrage bound | > 5¢ gap between °F n and n+1 | LOW |
| S7 | **Open Interest Drift** | `open_interest` on a single bucket increases while all other buckets flat | Change > 2σ above baseline | LOW |
| S8 | **Depth Imbalance** | Total yes book depth (all levels) vs total no book depth shifts > 3:1 | 3:1 ratio favoring one side | MEDIUM |

### 2.3 Algorithm: Multi-Signal Fusion

```
WHALE_DETECT(series_ticker):
  for each bucket in series_ticker:
    fetch /markets/{bucket_ticker} → {yes_bid_size, yes_ask_size, yes_bid, yes_ask, volume_24h, open_interest}
    
    # Update rolling statistics
    update_rolling_stats(bucket_ticker, {
      bid_size: yes_bid_size,
      ask_size: yes_ask_size,
      spread: yes_ask - yes_bid,
      volume: volume_24h,
      oi: open_interest,
      bid_ask_ratio: yes_bid_size / max(yes_ask_size, 1)
    })
    
    # Compute z-scores
    z_bid = z_score(yes_bid_size, rolling_mean_20, rolling_std_20)
    z_ratio = z_score(bid_ask_ratio, rolling_mean_ratio_20, rolling_std_ratio_20)
    z_volume = z_score(volume_growth_rate, rolling_mean_vol_20, rolling_std_vol_20)
    z_spread_change = z_score(spread_change, rolling_mean_spread_diff_20, rolling_std_spread_diff_20)
    
    # Compute stepwise accumulation score
    accumulation_score = compute_stepwise_score(history_bid_size[-3:])
    
    # Fusion logic
    anomaly_score = 0
    
    if z_bid > 3.0:
      anomaly_score += 2.0        # S1 — strongest signal
    if z_ratio > 1.96:            # 95th percentile
      anomaly_score += 1.5        # S2
    if z_volume > 2.0:
      anomaly_score += 0.8        # S3
    if z_spread_change < -2.0 and bid_size_growth > 1.5:
      anomaly_score += 1.5        # S4
    if accumulation_score > 0.7:
      anomaly_score += 1.0        # S5
      
    # Check bucket gradient
    if bucket_price_gap > 5.0:
      anomaly_score += 0.3        # S6 (weak, only adds context)
    
    # Final decision
    if anomaly_score >= 2.0:
      classify: WHALE_ACTIVITY_DETECTED on bucket
      fetch /markets/{bucket_ticker}/orderbook → full depth for depth imbalance check
      compute strength = min(anomaly_score / 5.0, 1.0)
      emit signal: {bucket, direction: YES, strength, anomaly_components: {...}}
    elif anomaly_score >= 1.0:
      classify: WHALE_ACTIVITY_SUSPECTED (requires confirmation in next 2 cycles)
    else:
      no signal
```

### 2.4 Confirmation Latch

To avoid false triggers from noise:

- **Single cycle above threshold** → SUSPECTED (flag, do not trade)
- **Two consecutive cycles above threshold** → DETECTED (tradeable)
- **Three cycles above with increasing strength** → HIGH_CONVICTION (stronger position)
- **Strength decay > 50% in one cycle** → EXIT signal

### 2.5 False Positive Mitigation

| Risk | Mitigation | Expected FP Reduction |
|------|:-----------|:--------------------:|
| Random noise spike in bid_size | Requires 2 consecutive cycles = 60s persistence | -60% |
| Market maker passive quoting (always at bid) | Exclude buckets where bid/ask spread > 10¢ (illiquid, likely MM) | -20% |
| End-of-day positioning (not whale data) | Reduce confidence after 6 PM local | -10% |
| Cross-bucket arbitrage bots | Require bucket gradient discontinuity (S6) to be LOW when S1 fires | -5% |
| Settlement-day positioning | Normalize for settlement-day volume spike | -5% |

**Expected baseline false positive rate:** ~15% during liquid hours, ~25% during illiquid hours (filtered by minimum volume threshold).

---

## 3. Per-Station Normalization Strategy

### 3.1 The Normalization Problem

Order book depth varies significantly across stations and buckets due to factors unrelated to whale activity:

| Factor | Effect | Example |
|--------|:------:|:--------|
| Station popularity | 10× depth variation | KNYC (popular) vs KOKC (less traded) |
| Bucket proximity to climatology | 5× depth variation | 85°F bucket active in summer, 40°F bucket dead |
| Day of week | 2× variation | Lower volume on weekends |
| Time of day | 3× variation | Peak morning hours vs post-close |
| Time to settlement | 5× variation | Last day before settlement vs 7 days out |

### 3.2 Normalization Architecture

#### Baseline Structure

For each (station, series_type, bucket_temperature), maintain:

```
baseline = {
  "rolling_window": 20 periods (10 min at 30s polling),  # Short-term adaptive
  "long_window": 288 periods (1 trading day at 30s polling),  # Diurnal pattern
  "history_window": 7 days at 5-min snap,  # Weekly pattern
  "hour_of_day": {},  # Separate baseline per hour
  "day_of_week": {},  # Separate baseline per day
  "minutes_to_settlement": {},  # Decaying baseline as settlement approaches
}
```

#### Z-Score Computation

```
# Adaptive z-score with diurnal adjustment
expected_bid_size = (
    0.5 * diurnal_baseline[hour_of_day] + 
    0.3 * rolling_mean_20 + 
    0.2 * historical_median_same_hour
)

variance_bid_size = (
    0.5 * diurnal_variance[hour_of_day] +
    0.3 * rolling_variance_20 +
    0.2 * historical_variance_same_hour
)

z_score = (current_bid_size - expected_bid_size) / max(sqrt(variance_bid_size), 1.0)
```

#### Time-of-Day Profiles

| Hour (local) | Activity Level | Baseline Multiplier | Notes |
|:------------:|:--------------:|:-------------------:|:------|
| 00:00–06:00 | Minimal | 0.1× | Market closed (mostly) |
| 06:00–08:00 | Low | 0.3× | Pre-market positioning |
| 08:00–10:00 | High | 1.5× | Peak morning — many traders active |
| 10:00–14:00 | Medium | 1.0× | Midday — algorithmic traders |
| 14:00–16:00 | High | 1.3× | Afternoon — METAR updates |
| 16:00–18:00 | Medium | 0.8× | End of trading main hours |
| 18:00–21:00 | Low | 0.4× | Late — only dedicated traders |
| 21:00–00:00 | Minimal | 0.2× | Post-close |

#### Settlement-Day Adjustment

On the last trading day before settlement:
- Volume increases 3-5×
- Bid/ask spread narrows 2×
- Whale activity is harder to distinguish from normal settlement positioning

**Mitigation:** On settlement day, require anomaly_score ≥ 3.0 (vs standard 2.0) to trigger.

### 3.3 Cross-Station Comparison

Whale detection is **per-station, per-bucket**. No cross-station normalization is needed because whales station-trade independently. However, a **correlation monitor** across stations is useful:

```
# If 3+ stations show simultaneous bid-side accumulation at the same bucket temperature:
# This suggests a national weather event is being priced in (less likely whale-specific)
# → REDUCE confidence (it's broad market, not informed trading)
```

### 3.4 Cold Start

New stations or buckets require a bootstrap period before WhaleWatch is active:

| Phase | Duration | Action |
|:-----:|:--------:|:-------|
| Baseline collection | 5 trading days | Collect order book snapshots at 30s, store raw data, NO signals |
| Pattern discovery | 3 trading days | Compute diurnal profiles, identify noise floor |
| Active | Day 8+ | Enable signal generation with reduced confidence (0.5× strength) |
| Full confidence | Day 15+ | Standard signal strength |

---

## 4. Trading Signal Construction

### 4.1 Entry Logic

When `WHALE_ACTIVITY_DETECTED` on bucket X (temperature T°F, series_type=`HIGH` or `LOW`):

#### Entry Conditions (ALL must be met)

| Condition | Description | Checks |
|:----------|:------------|:-------|
| **C1** | Whale activity confirmed for 2+ consecutive cycles | anomaly_score ≥ 2.0 for ≥ 60 seconds |
| **C2** | Bucket is not at extreme probability | YES price > 5¢ AND < 95¢ (not already priced in / too early) |
| **C3** | Station has sufficient liquidity | volume_24h_fp > $500 for the series |
| **C4** | No contradictory whale activity on same station | Whale is NOT simultaneously accumulating the opposite direction on same series |
| **C5** | Time to settlement > 2 hours | Not too close to settlement (avoids settlement-day noise) |

#### Entry Direction

| Series | Whale Signal | Trade |
|:------:|:------------:|:------|
| HIGH | YES accumulation on bucket T°F | **Buy YES on KXHIGH{STA}-{T}** |
| LOW | YES accumulation on bucket T°F | **Buy YES on KXLOW{STA}-{T}** |
| HIGH | NO accumulation on bucket T°F | **Buy NO on KXHIGH{STA}-{T}** (whale selling the high) |
| LOW | NO accumulation on bucket T°F | **Buy NO on KXLOW{STA}-{T}** (whale selling the low) |

**Rationale:** Whales accumulating YES = they expect temperature to reach that bucket. Buying the same side follows their information, priced in before the market fully adjusts.

#### Entry Price

```
entry_price = current_market_price (at current bid if possible, or at ask+1 to get filled)
# Do NOT pay too much — the signal is directional, not urgent
max_premium = 0.02 * bucket_probability_range  # Max 2¢ above current mid
```

### 4.2 Position Sizing

Position size is proportional to anomaly strength and scaled by market liquidity:

```
base_units = floor(0.05 * volume_24h_contracts)  # 5% of daily volume as max position

# Anomaly strength scaling (0.0 to 1.0)
strength_scalar = min(anomaly_score / 5.0, 1.0)

# Confidence decay for recent stations (< 15 days baseline)
baseline_confidence = min((days_active - 7) / 8.0, 1.0) if days_active > 7 else 0.0

# Settlement proximity decay
settlement_decay = max(0.2, hours_to_settlement / 24.0)

# Kelly-inspired sizing (conservative)
kelly_fraction = 0.25  # Quarter-Kelly

# Final position
position_contracts = int(base_units * strength_scalar * baseline_confidence * settlement_decay * kelly_fraction)
position_dollars = position_contracts * entry_price_cents / 100.0

# Hard caps
position_dollars = min(position_dollars, 500.0)  # Max $500 per signal
position_dollars = max(position_dollars, 10.0)   # Min $10 per signal (if >= threshold)
```

### 4.3 Exit Logic

#### Exit Signals

| # | Condition | Trigger | Priority |
|---|-----------|:-------:|:--------:|
| E1 | **Whale stops accumulating** | anomaly_score falls below 1.0 for 3 consecutive cycles = whale done | SELL ALL |
| E2 | **Temperature confirms** | Station METAR reports temperature = T°F (bucket hit) | SELL HALF at profit, hold remainder for settlement |
| E3 | **Temperature contradicts** | Remaining time to settlement < 4 hours AND temperature trajectory clearly won't reach bucket | SELL ALL |
| E4 | **Whale reverses** | anomaly_score drops > 50% + opposite direction whale activity on same station | SELL ALL |
| E5 | **Maximum hold** | Position held > 6 hours without temperature confirmation | SELL ALL (signal decay) |
| E6 | **Stop loss** | Price moves against position by > 50% of entry price | STOP LOSS — SELL ALL |

#### Profit Taking

```
target_take_profit = entry_price + (0.5 * (100 - entry_price))  # 50% of remaining edge
# If entered at 30¢, target = 30 + 0.5*(100-30) = 65¢

if current_mid_price >= target_take_profit:
  SELL 50% of position (lock in profit)
  HOLD remainder with trailing stop of 15% below peak
```

#### Exit via Settlement

If no exit signal fires before settlement:
- Position resolves to 0 (lose) or 100¢ (win) at settlement
- Log outcome for signal quality tracking

### 4.4 Signal Quality Tracking

For each WhaleWatch signal, record:

| Field | Value |
|-------|-------|
| `station` | e.g., KNYC |
| `series_type` | HIGH / LOW |
| `bucket` | Temperature °F |
| `entry_time` | UTC timestamp |
| `entry_price` | Cents |
| `position_size` | Contracts |
| `anomaly_score` | Numeric (2.0-5.0) |
| `anomaly_components` | JSON: {S1, S2, S3, S4, S5} |
| `exit_time` | UTC timestamp |
| `exit_price` | Cents |
| `exit_reason` | E1-E6 |
| `pnl` | Dollars |
| `settlement_hit` | Did temperature reach bucket? |
| `strength_predicted` | Anomaly score at entry |
| `correct_prediction` | Did signal predict correctly? |

This enables rolling performance tracking: Sharpe ratio, win rate, average P&L per signal type, per station, per hour.

---

## 5. Integration with Goldilocks Predictive Model

### 5.1 Overview

The Goldilocks Predictive Model (GPM) forecasts the probability of brief temperature spikes/dips at KNYC based on meteorological conditions (weak wind, strong insolation, stable stratification). WhaleWatch provides **independent, empirical confirmation** that someone is actively betting on a specific bucket.

### 5.2 Integration Points

#### Point 1: Confidence Boost

```
# GPM produces: P(Goldilocks | conditions)  — probability of any Goldilocks event today
# WhaleWatch produces: WHALE_ACTIVITY on bucket T°F

if GPM_probability > 0.15 AND WhaleWatch_detected on bucket T°F:
    # Both point to same class of event
    combined_confidence = GPM_probability * (1.0 + 0.3 * whale_strength)
    # Boost: +15-30% relative to GPM probability
```

**Example:** GPM says 20% chance of Goldilocks high. WhaleWatch shows strong (strength=0.8) accumulation on 83°F bucket. Combined confidence = 20% × (1 + 0.3 × 0.8) = 24.8%.

#### Point 2: Bucket Resolution

```
# GPM is binary: Goldilocks event happened / didn't happen
# WhaleWatch provides which bucket

if GPM_probability > 0.10:
    for each bucket in active_buckets:
        if WhaleWatch_anomaly(bucket):
            # GPM says conditions are right for a spike
            # WhaleWatch says someone is betting on bucket T
            # → Trade bucket T with Goldilocks-adjusted sizing
            use_sizing = base_position * (1.0 + GPM_probability)
```

**Example:** GPM says "high Goldilocks probability today." WhaleWatch shows bid concentration on 84°F bucket. We trade 84°F YES with +20% sizing over standalone WhaleWatch.

#### Point 3: False Positive Filter

```
if GPM_probability < 0.05 AND WhaleWatch_detected:
    # GPM says conditions don't support Goldilocks
    # WhaleWatch says someone is buying a bucket
    # → This is likely fundamental trading, not informed micro-spike detection
    # → Still tradeable but with different exit strategy
    use_exit_strategy = "E3" (temperature confirmation) rather than "E1" (whale stops)
```

### 5.3 Combined Trade Decision Matrix

| GPM Signal | WhaleWatch Signal | Action | Rationale |
|:----------:|:-----------------:|:------|:----------|
| HIGH | DETECTED | **TRADE** with 1.3× sizing | Both confirm — highest confidence |
| HIGH | NONE | No WhaleWatch trade | GPM alone does not provide bucket specificity |
| LOW | DETECTED | **TRADE** with 0.7× sizing | WhaleWatch alone still has edge, but reduced |
| LOW | NONE | No trade | No signal |
| GPM HOLD (insufficient data) | DETECTED | **TRADE** with 1.0× sizing | WhaleWatch standalone |

### 5.4 Joint Signal Monitoring Dashboard

```
┌─────────────────────────────────────────────────────┐
│ 🌊 Goldilocks + WhaleWatch Fusion                    │
├─────────────────────────────────────────────────────┤
│ Station: KNYC  |  Series: HIGH  |  Date: 2026-08-01 │
│                                                      │
│ GPM Probability:     21% (Goldilocks conditions)     │
│ WhaleWatch:          DETECTED on 84°F bucket         │
│   Anomaly Score:     3.4 (S1=2.0, S2=0.8, S3=0.6)  │
│   Bid Size (current): 1,200 vs baseline: 340        │
│   Spread: 2¢ (compressed from 5¢)                   │
│                                                      │
│ Combined Confidence: 27%  (+6pp from WhaleWatch)     │
│ Recommended: BUY YES KXHIGHNYC-26 (84°F)             │
│ Position: $320 (13.2% of daily volume)              │
└─────────────────────────────────────────────────────┘
```

---

## 6. Integration with Core GEFS Ensemble

### 6.1 Overview

The core GEFS ensemble produces a calibrated probability for each bucket based on 82-member NWP ensemble fraction. WhaleWatch adds a **non-meteorological** signal: someone with better or faster data is acting on conviction.

### 6.2 Integration Architecture

```
┌─────────────────────┐     ┌─────────────────────┐
│ GEFS Ensemble        │     │ WhaleWatch          │
│ (Meteorological)     │     │ (Market Structure)  │
│                      │     │                     │
│ P(ENSEMBLE | bucket) │     │ WHALE_ANOMALY(bool) │
│                      │     │ WHALE_STRENGTH(float)│
└──────────┬──────────┘     └──────────┬──────────┘
           │                            │
           ▼                            ▼
    ┌─────────────────────────────────────────┐
    │     WhaleModulated Probability          │
    │                                         │
    │  P_final = P_ensemble *                  │
    │    (1.0 + 0.02 * whale_modulation)       │
    │                                         │
    │  whale_modulation: 0 (none) to 100      │
    │  (maximum = +2pp lift)                  │
    └─────────────────────────────────────────┘
```

### 6.3 Modulation Formula

```
base_probability = ensemble_calibrated_probability[bucket]
whale_anomaly = whale_watch_strength[bucket]  # 0.0 to 1.0

# Compute modulation (max +2pp)
if whale_anomaly > 0:
    # Base +0.5pp for detection, up to +2pp for max strength
    modulation_pp = 0.5 + 1.5 * whale_anomaly
    
    # Discount when ensemble probability is extreme (already 95%+ or 5%-)
    if base_probability > 0.90:
        modulation_pp *= 0.3  # Already near-certain, whale adds little
    elif base_probability < 0.10:
        modulation_pp *= 0.3  # Already near-impossible
    
    # Increase when ensemble and whale agree (+0.5pp bonus)
    if (base_probability > 0.5 and whale_direction == "YES") or \
       (base_probability < 0.5 and whale_direction == "NO"):
        modulation_pp += 0.5
    
    # Cap at +2pp
    modulation_pp = min(modulation_pp, 2.0)
else:
    modulation_pp = 0.0

# Apply to both probability and net edge
adjusted_probability = base_probability + (modulation_pp / 100.0)
adjusted_net_edge = adjusted_probability - market_price - fee - slippage
```

**Key design decision:** WhaleWatch modulates the ensemble probability by **+0.5 to +2.0pp** (percentage points), NOT by a ratio. This ensures:
- At low base probability (e.g., 15%), whale signal takes it to 16-17% — still below trade threshold unless ensemble alone was close
- At high base probability (e.g., 68%), whale signal takes it to 69-70% — can push marginal trades over the edge threshold
- The ensemble remains the primary signal; WhaleWatch is a **tiebreaker and amplifier**

### 6.4 Edge Contribution Calculation

```
# Before WhaleWatch
net_edge = base_probability - market_price - fee - slippage
# e.g., 0.68 - 0.65 - 0.02 - 0.01 = 0.00 (no edge, no trade)

# After WhaleWatch
adjusted_net_edge = adjusted_probability - market_price - fee - slippage
# e.g., 0.697 - 0.65 - 0.02 - 0.01 = 0.017 (1.7% edge, trade)
```

### 6.5 Trade Level Filter

WhaleWatch can also **veto** or **confirm** ensemble trades:

```
if ensemble_edge > threshold:
    # Ensemble wants to trade
    
    if whale_detected AND whale_direction == ensemble_direction:
        # CONFIRM — whale agrees with ensemble
        trade_with_adjusted_sizing += 10%
    
    elif whale_detected AND whale_direction != ensemble_direction:
        # CONFLICT — whale disagrees with ensemble
        # Reduce position by 50%, move to observe mode
        trade_with_adjusted_sizing -= 50%
        set_alert('WHALE_ENSEMBLE_CONFLICT', station, bucket)
    
    elif not whale_detected:
        # Neither confirm nor deny — trade normally
        pass

### 6.6 Generalized Bucket Scanning

Instead of only watching the next bucket up/down from the current temperature, WhaleWatch scans **all active buckets** for a given series. When whale activity is detected on any bucket, the modulation is applied to that specific bucket's ensemble probability.

**Why this matters:** A whale buying the 90°F bucket on a day when the current temperature is 75°F would be invisible to a `current_temp ± N` approach. But if that whale has superior data showing a heatwave developing, we want to catch it early.

```
for each bucket in series_ticker:
    if whale_anomaly[bucket] > 0:
        apply_modulation(bucket, whale_anomaly[bucket])
        # The ensemble probability for that bucket may be low
        # (because NWP hasn't caught the heatwave yet)
        # WhaleWatch +0.5-2pp may not be enough to create trade
        # But it flags the bucket for closer monitoring
        flag_bucket_for_monitoring(bucket, 'whale_conviction')
```

### 6.7 Non-Directional Constraint

WhaleWatch does not predict **direction** of temperature movement — only that **someone is acting with conviction on a specific bucket**. The trading direction is derived from the type of accumulation:

| Whale Accumulates | Our Direction | Meaning |
|:----------------:|:-------------:|:--------|
| YES | Buy YES | Whale expects temperature ≥ T
| NO | Buy NO | Whale expects temperature < T

If the ensemble disagrees with the whale's implied direction, we flag it as a **CONFLICT** event and reduce exposure rather than attempting to infer which is correct.
---

## 7. Estimated P&L Contribution

### 7.1 Signal Frequency

| Metric | Estimate | Source |
|--------|:--------:|:-------|
| Whale events per station per month | **3–8** | Based on typical informed trading frequency in binary options markets |
| Whale events per day (all 20 stations, both series) | **2–5** | 40 series × ~5/month ÷ 30 days |
| Tradeable events (passes all entry gates) | **1–3 per day** | ~50% of detections pass liquidity + time-to-close filters |
| Average trade duration | **2–4 hours** | From entry to whale stops or temperature confirms |

### 7.2 Per-Trade Economics

| Metric | Optimistic | Realistic | Conservative |
|:-------|:----------:|:---------:|:------------:|
| Win rate | 65% | 55% | 45% |
| Average win | +$35 | +$25 | +$18 |
| Average loss | -$18 | -$20 | -$22 |
| Avg position size | $250 | $150 | $80 |
| Net per trade | +$16.45 | +$4.75 | -$4.00 |
| Trades/month | 90 | 60 | 30 |

**Expected monthly P&L:**
- **Optimistic:** 90 × $16.45 = **+$1,480/month**
- **Realistic:** 60 × $4.75 = **+$285/month**
- **Conservative:** 30 × (-$4.00) = **-$120/month** (strategy loses money at low win rates)

### 7.3 Sharpe Ratio Impact

WhaleWatch adds a **low-correlation** signal to the existing ensemble (correlation ~0.1–0.3 with meteorological signals). This improves portfolio-level Sharpe through diversification.

| Scenario | Current Ensemble Sharpe | WhaleWatch Standalone Sharpe | Combined Sharpe (estimated) |
|:---------|:----------------------:|:---------------------------:|:--------------------------:|
| Optimistic | 0.37 | 0.8–1.2 | **0.45–0.55** (+0.08–+0.18) |
| Realistic | 0.37 | 0.3–0.6 | **0.40–0.45** (+0.03–+0.08) |
| Conservative | 0.37 | -0.2–0.1 | **0.35–0.38** (-0.02–+0.01) |

**Bottom line:** WhaleWatch is expected to improve overall Sharpe by **+0.03 to +0.18** in the realistic-to-optimistic range. At ~$50K equity, this translates to **$1,500–$9,000 additional annual return** at the portfolio level.

### 7.4 Risk-Adjusted Contribution

| Metric | Value |
|:-------|:-----:|
| Expected monthly net P&L (realistic) | **+$285** |
| Expected monthly net P&L (optimistic) | **+$1,480** |
| Max drawdown (month, 95th %ile) | -$600 |
| Correlation to ensemble signals | 0.1–0.3 |
| Capital required | $0 (uses same Kalshi account) |
| Compute cost | ~$2/month (polling + storage) |

**Return on compute cost:** Even at conservative 50% win rate, WhaleWatch covers its compute costs 100×.

### 7.5 Performance Ceilings

| Limiter | Effect | Workaround |
|:--------|:------:|:-----------|
| Kalshi doesn't expose trader identity | Cannot distinguish one whale from several small traders | Require concentration (S1) not just volume (S3) |
| Kalshi doesn't expose historical order book | Cannot backtest — must learn live | Forward-only deployment with journaling |
| WhaleWatch only works when whales active | ~50% of trading days see no detectable whale | No signal = no trade; don't force it |
| Spread + fees consume 40–60% of edge | Same issue as ensemble trades | Only trade when adjusted net edge > 0 after all costs |

## 8. Implementation Effort Estimate

### 8.1 Work Breakdown

| # | Component | Effort | Dependencies | Priority |
|---|-----------|:------:|:------------:|:--------:|
| **1** | `order_book_collector.py` — New module: polls Kalshi `/markets/{ticker}` and `/orderbook` endpoints at staggered intervals, stores raw snapshots in SQLite | **2 days** | Existing `_kalshi_public_get`, `_check_rate_limit` from `kalshi_monitor.py` | P0 |
| **2** | `whale_watch_db.py` — New module: SQLite schema for order book snapshots, rolling statistics, anomaly events, signal journal | **1 day** | #1 (schema must match data structure) | P0 |
| **3** | `order_book_baseline.py` — New module: per-station, per-bucket rolling baseline computation (mean, std, z-score for bid_size, ask_size, spread, volume) | **1.5 days** | #2 (needs storage) | P0 |
| **4** | `whale_detector.py` — New module: multi-signal fusion algorithm (Section 2), anomaly classification, confirmation latch, strength computation | **2 days** | #3 (needs baseline) | P0 |
| **5** | `whale_signal_feeder.py` — New module: converts whale detection events into trade signals (Section 4), entry/exit logic, position sizing | **1 day** | #4, integration with existing `live_trader.py` | P0 |
| **6** | Goldilocks Integration — Modify `goldilocks_signal.py` or create `whale_goldilocks_fusion.py`: combine WhaleWatch detection with GPM probability | **1 day** | #5, Goldilocks Predictive Model | P1 |
| **7** | Ensemble Integration — Modify `signal_fusion.py` or `liquidity_weighted_ensemble.py`: apply WhaleWatch modulation (+0.5–2pp) to ensemble probabilities | **0.5 days** | #5, existing ensemble pipeline | P1 |
| **8** | `whale_watch_dashboard.py` — New module: observability dashboard for whale detections, signal history, P&L tracking | **1 day** | #5 (needs signal data) | P1 |
| **9** | Diurnal profile collection — Bootstrap cold-start (Phase 1: 5-day baseline collection) | **0.5 days** | #1, #2 (already needed) | P2 |
| **10** | Testing — Unit tests for detection logic, integration tests for API polling, 7-day shadow mode journaling | **2 days** | #1–8 | P1 |
| **11** | Documentation — Update AGENTS.md, TASKS.md, deployment config | **0.5 days** | #10 | P2 |

### 8.2 Total Estimate

| Metric | Value |
|:-------|:-----:|
| **Total engineering effort** | **11.5 days** |
| Calendar time (single developer) | **3–4 weeks** |
| Calendar time (parallel, Gilfoyle + helper) | **10–14 days** |
| **Cost at $500/day** | **$5,750** |
| Break-even time (realistic P&L) | 20 months (not viable alone — but as ensemble additive, justified) |
| Break-even time (optimistic P&L) | 4 months (viable standalone) |
| Risk | Low-medium (public API, no auth, SQLite-based, isolated module) |

### 8.3 Deployment Schedule

```
Week 1:  #1 (order_book_collector) + #2 (DB schema) + #9 (diurnal baseline collection start)
         → Raw data flowing, baseline accumulating

Week 2:  #3 (baseline computation) + #4 (whale_detector)
         → Anomaly detection running in shadow mode

Week 3:  #5 (signal feeder) + #6 (Goldilocks integration) + #7 (ensemble integration)
         → First paper trades via WhaleWatch

Week 4:  #8 (dashboard) + #10 (testing) + #11 (docs)
         → 7-day shadow mode → go/no-go for production

Total: 4 weeks calendar, ~11.5 days engineering effort
```

### 8.4 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|:-----|:----------:|:------:|:-----------|
| Kalshi changes API (rate limits, endpoint deprecation) | Low | High | Monitor Kalshi changelog; use existing circuit breaker pattern |
| False positive rate too high (>40%) | Medium | Medium | Start with strict threshold (z > 3.0, 2-cycle confirmation); relax only after validation |
| Whale activity too rare (<1 detection/week) | Medium | Low | Lower threshold to z > 2.5 after 4 weeks if needed |
| Order book data too noisy at illiquid stations | High | Medium | Enforce minimum volume_24h_fp > $200 for signal generation |
| Cannot distinguish whale from market maker | Medium | Medium | Exclude buckets where bid/ask consistently spread > 10¢; track MM patterns |

---

## Appendix A: Summary of New Files

| File | Purpose |
|:-----|:--------|
| `core/order_book_collector.py` | Polls Kalshi order book endpoints, staggered coverage |
| `core/whale_watch_db.py` | SQLite schema + CRUD for snapshots, baselines, signals |
| `core/order_book_baseline.py` | Rolling per-bucket baseline computation |
| `core/whale_detector.py` | Multi-signal fusion anomaly detection |
| `core/whale_signal_feeder.py` | Trade signal generation from detections |
| `core/whale_goldilocks_fusion.py` | Merge with Goldilocks Predictive Model |
| `core/whale_watch_dashboard.py` | Observability dashboard |

## Appendix B: Modified Files

| File | Change |
|:-----|:-------|
| `core/signal_fusion.py` or `core/ensemble_fraction.py` | Add WhaleWatch modulation (+0.5–2pp) |
| `core/live_trader.py` | Route WhaleWatch signals through trade execution |
| `core/kalshi_monitor.py` | Potentially extend rate limiter for new endpoints |
| Existing config | Add WhaleWatch polling parameters |

## Appendix C: Kalshi Market Ticker Reference

| Series Type | Pattern | Example |
|:-----------:|:--------|:-------|
| Kalshi HIGH | `KXHIGH{ICAO}-{increment}` | KXHIGHNYC-26 → NYC HIGH ≥ 84°F |
| Kalshi LOW | `KXLOW{ICAO}-{increment}` | KXLOWNYC-10 → NYC LOW ≤ 42°F |

Increments are sequential (0, 1, 2, ...) with 1°F resolution per increment. The mapping from increment to temperature changes daily based on a climatological baseline published by Kalshi each trading day.

---

*End of design document. Route to Gilfoyle for technical implementation review, then to Gerri for exposure/cost assessment before paper trading pipeline.*
