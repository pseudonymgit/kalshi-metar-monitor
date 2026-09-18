<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# MicrostructureSpikeDetector — Shadow Spec

**Authority:** Gerri Kellman governance ruling (2026-08-22)
**Status:** SHADOW SPEC — design document only, no implementation
**Version:** 1.0
**Author:** Gilfoyle (subagent)
**Base Resolution:** Gray Room R7 A1-A9, R14 Expert 3 S1-S4, I1-I3, Elephant 2

---

## Table of Contents

1. [Scope & Definitions](#1-scope--definitions)
2. [Intraday METAR Observation Pipeline](#2-intraday-metar-observation-pipeline)
3. [Spike Detection Algorithm](#3-spike-detection-algorithm)
4. [Dual-Hypothesis Engine](#4-dual-hypothesis-engine)
5. [Confidence Calibration](#5-confidence-calibration)
6. [Settlement Gate](#6-settlement-gate)
7. [Station-Rank-Selective Activation](#7-station-rank-selective-activation)
8. [Integration with 4-Signal Core](#8-integration-with-4-signal-core)
9. [Dual-Hypothesis Predictions](#9-dual-hypothesis-predictions)
10. [State Management & Persistence](#10-state-management--persistence)
11. [Backtest Protocol](#11-backtest-protocol)
12. [Implementation Prerequisites](#12-implementation-prerequisites)

---

## 1. Scope & Definitions

### 1.1 What This Lane Is

The **MicrostructureSpikeDetector** is an intraday trading lane that detects transient temperature spikes at Kalshi bucket boundaries using real-time METAR observations. It replaces the demoted daily-level `spike_reversion`/`goldilocks` signal and is the canonical path for the transient spike edge.

### 1.2 Physical Edge

Transient temperature spikes (30-90 minute phenomena) at bucket boundaries create a mean-reversion opportunity:
- Temperature briefly surges 1-3°F above an established daily pattern
- Crosses a bucket boundary (e.g., 85°F) for 1-3 METAR observations
- Reverts without setting the daily maximum settlement value
- Kalshi settlement uses the daily high/low, so the crossing was "fleeting" — the settlement value does NOT change
- The market overpriced the crossed bucket → reversion trade is profitable

**Key constraint:** This edge operates at INTRADAY granularity (30-90 min). It was destroyed by daily aggregation in the old `spike_reversion` signal (48.15% holdout).

### 1.3 Architecture Classification

**Status:** Lane (posterior modifier), NOT a Signal
**Mechanism:** Probability-mass shift on posterior distribution from main ensemble
**Granularity:** Event-driven (fires on new METAR observations)
**Not sweepable:** Daily sweep destroys intraday edge
**Capital allocation:** Separate from 4-signal core (per R7 C3)

### 1.4 Relationship to Existing Code

| Component | Relationship | Action |
|-----------|-------------|--------|
| `core/lane_goldilocks.py` | Predecessor — GoldilocksLane class | REFACTOR into this spec |
| `core/metar_monitor.py` embedded tracker | Real-time alert pipeline in `_process_temperature_event` | EXTRACT into MicrostructureSpikeDetector |
| `core/signals/spike_reversion_signal.py` | Daily-level signal being demoted | ARCHIVE (disabled in registry) |
| `core/dual_hypothesis_engine.py` | H1/H2 classifier | KEEP as dependency |
| `core/station_rank_selective.py` | Per-station accuracy filter | INTEGRATE |
| `core/rolling_calibration.py` | 30-day rolling calibration | INTEGRATE |
| `core/settlement_execution_gate.py` | Settlement timing gate | INTEGRATE |
| `core/lane_manager_v2.py` | Lane orchestration | INTEGRATE |
| `core/metar_qc_parser.py` | Quality control flags | INTEGRATE |

---

## 2. Intraday METAR Observation Pipeline

### 2.1 Data Sources

| Source | Endpoint | Latency | Granularity | Precision |
|--------|----------|---------|-------------|-----------|
| **NWS API** | `api.weather.gov/stations/{ICAO}/observations` | ~1-2 min | 5-min rolling avg | 0.1°C → ~0.18°F |
| **IEM ASOS** | `mesonet.agron.iastate.edu/request/asos.py`| 12-36 hr | 1-min | Whole °F |
| **NCEI 5-min** | `ncei.noaa.gov/data/asos-five-minute/` | Next day | 5-min | Whole °F |
| **TG-FTP** | `tgftp.nws.noaa.gov/data/observations/metar/` | ~1-2 min | Per METAR cycle | Whole °C |

**Primary feed for real-time:** NWS API (60s polling, `If-Modified-Since` for efficiency)
**Backtest/validation feed:** IEM 1-min OR NCEI 5-min archives
**Fallback:** TG-FTP (text-only, whole °C — ±0.9°F uncertainty)

### 2.2 Polling Architecture

```
NWS API  ─┬─ 60s timer ──→ If-Modified-Since header → response[features]  
           │                                                    │
           ▼                                                    ▼
    _parse_nws_collection()                           _parse_nws_collection()
           │                                                    │
           └─────────────────────┬──────────────────────────────┘
                                 ▼
                       MicrostructureSpikeDetector.ingest()
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
            Spike Detection                Daily Max Tracking
            (window, threshold,             (running_daily_max)
             confirmation)
```

**Polling contract:**
- 60s interval (configurable via `METAR_POLL_SECONDS`)
- Use `If-Modified-Since` HTTP header to avoid full payload on each poll
- Fall back to IEM CSV if NWS returns 5xx or empty
- Record `last_fetch_status` per station in ingestion runtime
- Bootstrap: 60-min lookback on first contact

### 2.3 Observation Acceptance Criteria

| Criterion | Value | Rationale |
|-----------|-------|-----------|
| Maximum staleness | 10 min (600s) | Older obs reflect stale state |
| Grace period | 15 min (900s) | Late arrivals still accepted |
| Dedup window | Strictly newer timestamp | Equal/older timestamps dropped |
| Day boundary reset | Station-local midnight | Cross-day observations invalid |
| Temperature sanity | -50°F to 130°F | Physical bounds |
| Max obs-to-obs delta | 5°F | Sensor error filter |
| Minimum deviation from 5-min mean | 1.5°F | Must exceed quantization noise floor (~1.8°F from whole-°C encoding) |

### 2.4 F→C→F Round-Trip Awareness

**Documented limitation (from R14 E2):**
- ASOS sensor: 0.1°F every 10s → averaged to integer °F per minute
- METAR encoding: integer °F → integer °C for transmission
- NWS API returns `temperature.value` at 0.1°C (re-converted to °F in code)
- At 84.8°F (29.3°C): METAR reports 29°C → pipeline sees 84.2°F → **0.6°F error**

**Mitigation:** No algorithmic fix. The spec acknowledges ±0.9°F precision uncertainty at bucket boundaries. Trade sizing must account for this. The `MIN_DEVIATION_FOR_SPIKE = 1.5°F` parameter sets the detection floor above this noise.

### 2.5 Station-Specific Data Quality

**KNYC constraint (from R14 E1):**
- Central Park does NOT have HF-ASOS (1-min data)
- Outputs standard METAR at ~hourly cadence (:51 past the hour)
- Temperature in whole °C only
- Detection window: ~5-min polling cycle of NWS API, not sub-minute
- The flecking tick concept was never sub-second — it was a single 5-min observation visible in public data

**Impact:** The lane works on all stations including KNYC, but with acknowledged latency constraints. No station is excluded on data availability grounds alone — the station-rank filter handles performance.

---

## 3. Spike Detection Algorithm

### 3.1 Two Event Types

The detector must handle **two distinct microstructure events:**

| Event | Detection Trigger | Prediction | Confidence Base |
|-------|-------------------|------------|-----------------|
| **Type A: Instant-Cross-Revert** | Temp crosses bucket boundary WITHOUT setting new daily max | Reversion to old bucket (H1 transient) | High (0.50-0.70) |
| **Type B: Settlement-Up + Revert** | Temp crosses bucket, becomes new daily max, then reverts within 30 min | Reversion below new bucket | Medium (0.10-0.50) |

### 3.2 Type A: Instant-Cross-Revert (True Goldilocks)

This is the **primary signal**. It detects bucket-boundary crossings that do NOT become new daily max values.

**Algorithm:**

```
For each new observation (station, temp_f, obs_time):

1. Track running_daily_max for the station-local day
   → max_temp_today = max(all temps this day)

2. Compute instant_bucket = floor(temp_f)
   Previous instant_bucket from last observation

3. IF instant_bucket_changed (previous != current):
   AND current temp does NOT set a new daily max (temp_f < max_temp_today):
   
   → This is an "instant cross" — the bucket boundary was crossed
     without changing the daily settlement high.
   
   → Create a PendingCrossing entry:
       crossing_key = (station, epoch_id, instant_bucket)
       PENDING_CROSSINGS[crossing_key] = {
           "obs_time": obs_time,
           "temp_at_cross": temp_f,
           "bucket_crossed": instant_bucket,
           "direction": "up" if temp_f > prev_temp else "down",
           "max_temp_during_spike": temp_f,
           "min_temp_during_spike": temp_f,
           "crossings_remaining": 3,  # max 3 obs wait
           "daily_max_before_cross": max_temp_today,
       }

4. For each subsequent observation with same epoch:
   Check all PENDING_CROSSINGS for this station+epoch:
   
   IF crossing direction is UP (spike up → predict DOWN):
     Update max_temp_during_spike
     IF temp_f < bucket_crossed - REVERSION_MARGIN(0.2°F):
       → CONFIRMED: temp reverted below boundary
       → Fire MicrostructureSpikeDetector signal
       → Confidence from §5.1
       → Remove from PENDING_CROSSINGS
   
   IF crossing direction is DOWN (spike down → predict UP):
     Update min_temp_during_spike
     IF temp_f > bucket_crossed + REVERSION_MARGIN(0.2°F):
       → CONFIRMED: temp bounced above boundary
       → Fire down-reversion signal (lower confidence per §5.3)
       → Remove from PENDING_CROSSINGS
   
   IF observations_since_cross > 3:
     → Timeout: spike was not transient, but a structural shift
     → Cancel pending crossing
   
   IF temp_f continues away from boundary (spike growing):
     → Cancel: this was the start of a real trend, not a transient
```

**Parameters:**

| Parameter | Default | Range | Rationale |
|-----------|---------|-------|-----------|
| `crossings_remaining` | 3 | [2, 6] | At 5-min METAR cadence: 10-30 min. Longer = fewer FPs, more missed events |
| `reversion_margin` | 0.2°F | [0.1, 0.5] | Must cross back clearly. Higher = fewer FPs, slower detection |
| `min_deviation_for_spike` | 1.5°F | [1.0, 3.0] | Must exceed quantization noise floor |
| `max_crossing_duration_min` | 30 | [15, 60] | Beyond this → structural, not transient |

### 3.3 Type B: Settlement-Up + Revert (Existing Detection)

This is the **existing detection** in `metar_monitor.py`'s `_process_temperature_event`. It detects:

1. `settlement_up` — temp sets new daily max → new bucket
2. `reversion_after_settlement` — temp drops back below old bucket within 5 min

**This is a weaker signal** (R7 A1 fix: confidence base = 0.10). The spike DID set the daily max, so it's not truly "fleeting." However, rapid reversion within 30 min still has some edge.

**Keep this as detection path Type B**, but always downgrade confidence via §5.1 transience check.

### 3.4 Trend Extrapolation (Prediction Variant)

**Purpose:** Predict that a METAR observation will cross a bucket boundary BEFORE it happens, based on warming momentum.

**Algorithm (from R14 S2):**

```
Given: last 3 successive observations
  t0 = obs[-3].temp_f
  t1 = obs[-2].temp_f
  t2 = obs[-1].temp_f
  t_now = obs[0].temp_f

Compute:
  momentum = (t2 - t0) / 10min        # °F per minute
  distance_to_boundary = boundary - t_now

  IF momentum <= 0:
    P_cross = 0.0                     # Not warming
  ELSE:
    minutes_to_cross = distance_to_boundary / momentum
    P_base = max(0.0, 1.0 - minutes_to_cross / 30.0)  # Linear: faster = higher prob

Apply weather modifiers (rule-based, no ML):
  P_weather = 0.35                    # Base
  P_weather -= 0.05 * (wind_speed_kt / 10)
  P_weather -= 0.10 * cloud_cover_frac
  P_weather -= 0.04 * (bl_height_m / 500)
  P_weather = clamp(P_weather, 0.02, 0.50)

P_final = 0.5 * P_base + 0.5 * P_weather

Alert thresholds:
  P_final >= 0.50 → "Strong Boundary Approach"
  P_final >= 0.35 → "Moderate Boundary Approach"
  P_final >= 0.20 → info-only (no trade)
```

**Integration:** This is a **separate output** from the confirmatory detection. It generates a probability that can be used for pre-positioning or early entry. It does NOT replace the confirmatory Type A/Typer B detection — those are the primary trade triggers.

---

## 4. Dual-Hypothesis Engine

### 4.1 Hypothesis Definitions

| Hypothesis | Name | Meaning | Trading Action |
|------------|------|---------|----------------|
| **H1** | **Transient Spike** | Temperature spike is a fleeting microstructure event (30-90 min). Will revert before settlement. | **TRADE**: Enter reversion position |
| **H2** | **Structural Shift** | Temperature spike reflects a genuine meteorological change (front passage, sea breeze, etc.). Will NOT revert. | **SKIP**: No trade |

### 4.2 Classification Rules

The DualHypothesisEngine (existing module at `core/dual_hypothesis_engine.py`) classifies H1 vs H2 using:

**Primary criterion — running_max_delta:**
- `running_max_delta = max_temp_during_spike - running_daily_max_at_spike`
- If `running_max_delta < 0.3°F` → the spike did NOT push the daily max → **H1 transient** (high confidence)
- If `running_max_delta >= 0.3°F` → the spike set a new daily high → **H2 structural** (low confidence)

**Secondary criteria (H1 confirmation):**
- Duration above boundary < 30 minutes
- Less than 3 consecutive observations above boundary
- Dewpoint stable (±0.3°F) during spike
- Wind gust < 5 kt during spike
- Neighboring stations do NOT show correlated spike (regional weather event → H2)
- No frontal passage indicator in GEFS ensemble for ±3 hours around spike

**H2 indicators:**
- Duration above boundary > 30 minutes
- 3+ consecutive observations above boundary
- Dewpoint drop > 0.3°F (dry air advection)
- Wind gust > 5 kt
- Regional correlation (neighbors also spike)
- GEFS frontal passage flag within ±3 hours
- Temperature continues rising after initial spike (no reversion within 3 observations)

### 4.3 Prediction Mapping

| Spike Direction | H1 Prediction | H2 Behavior |
|----------------|---------------|-------------|
| **UP spike** (temp surges above boundary) | Predict DOWN (reversion to old bucket) | Temp stays high → settlement may be affected |
| **DOWN spike** (temp drops below boundary) | Predict UP (reversion to old bucket) | Temp stays low → settlement may be affected |

**Asymmetric confidence:** UP-spike reversion (down) is 15% more reliable than DOWN-spike reversion (up), applied via §5.3 discount.

### 4.4 Integration

The DualHypothesisEngine is called with:
```python
hypothesis = dhe.classify(
    station=station,
    spike_delta=running_max_delta,
    duration_minutes=crossing_duration,
    obs_count=observations_since_cross,
    dewpoint_delta=dp_change_during_spike,
    wind_gust_kt=max_gust_during_spike,
    is_settlement_up=did_settlement_change,
    neighbors_spiking=regional_correlation,
    gefs_frontal_flag=front_detected,
    temp_trend=rising_or_falling_after_spike,
)
```

Returns `HypothesisResult` with:
- `hypothesis`: H1 or H2
- `confidence_factor`: [0.0, 1.0]
- `features`: dict of all input features for logging

---

## 5. Confidence Calibration

### 5.1 R7-A1 Corrected Confidence

**This is the Gray Room's most important fix.** The previous logic gave HIGH confidence when the spike set the daily max (`is_daily_high=True`), which is backwards. The corrected logic:

```python
running_max_delta = max_temp_after_up - running_daily_max_at_spike
TRANSIENT_THRESHOLD = 0.3   # °F

if running_max_delta < TRANSIENT_THRESHOLD:
    # Transient spike — did NOT push daily max. THIS IS THE REAL EDGE.
    base = 0.50              # Strong signal
else:
    # Spike set new daily max. The edge doesn't apply here.
    base = 0.10              # Weak signal — was the structural event
```

### 5.2 Rolling Window Calibration

**Purpose:** Adjust confidence based on recent historical accuracy of the lane at each station.

**Mechanism (from R7 A9):**
- Maintain a 30-day rolling window of lane predictions per station
- For each prediction, record: (predicted_direction, confidence, actual_outcome)
- Compute: `station_accuracy = correct_predictions / total_predictions` over last 30 days
- Compute: `station_calibration = mean(actual_outcome) / mean(confidence)`
- Adjust raw confidence: `adjusted_conf = raw_conf * station_calibration`
- Apply floor: `final_conf = max(adjusted_conf, CONFIDENCE_FLOOR)`

**Station-specific confidence floor:**
- Default floor: 0.50
- If station_accuracy < 0.40 over last 30 predictions: raise floor to 0.65
- If station_accuracy >= 0.70 and station has ≥ 20 predictions: lower floor to 0.40

**Cold start:**
- New station: use global lane average (taken from all stations, last 30 days)
- If global average unavailable: use 0.50 default

### 5.3 Asymmetric Direction Discounts

**Up-spike reversion (predict DOWN):**
- More reliable direction (edge is temperature spike → anticipated drop)
- Base discount: ×1.05 boost
- Margin bonus: `min(running_max_delta × 0.10, 0.15)`
- Observations bonus: `min(obs_since_spike × 0.02, 0.20)`
- Time bonus: `day_fraction_at_spike × 0.20`

**Down-spike reversion (predict UP):**
- Less reliable (temperature drops are less likely to be fleeting transients)
- Base discount: ×0.85 penalty
- Margin bonus: `min(running_max_delta × 0.08, 0.10)`
- Observations bonus: `min(obs_since_spike × 0.015, 0.15)`
- Time bonus: `day_fraction_at_spike × 0.15`

**Final confidence computation:**
```python
confidence = base + bonus_margin + bonus_obs + bonus_time
confidence *= direction_multiplier  # 1.05 for up-spike, 0.85 for down-spike
confidence = clamp(confidence, 0.0, 1.0)
confidence = rolling_calibration.apply(station, confidence)
```

---

## 6. Settlement Gate

### 6.1 When to Stop Trading for the Day

**Purpose:** Prevent microstructure spike trades after the daily settlement trajectory is established. Late-day spikes are less likely to revert before settlement.

| Condition | Action | Rationale |
|-----------|--------|-----------|
| Local time after 14:00 ET (18Z) in summer | REDUCE position to 50% | Daily max likely established |
| Local time after 16:00 ET (20Z) in summer | STOP new trades | Settlement imminent |
| Local time after 17:00 ET (21Z) in winter | STOP new trades | Early settlement |
| Terminal rung reached (high/low market) | STOP all trades for that bucket | No further transitions possible |
| Settlement event occurred | STOP all trades | Day is settled |
| Less than 30 min to scheduled settlement | STOP new trades | Insufficient reversion window |
| Current daily max - bucket already > 3 rungs | STOP for that bucket | Reversion unlikely to cross enough rungs |

### 6.2 Late-Day Exceptions

If the spike occurs after the settlement gate and the confidence exceeds 0.80:
- The trade may proceed at REDUCED size (50% of normal Kelly allocation)
- Rationale: Very high confidence late-day spikes (e.g., after 18Z but confidence > 0.80 with H1 transient classification) may still be profitable

### 6.3 Settlement Divergence Risk

**Critical gap (from R14 I3, Elephant 3):**
- METAR pipeline watches NWS 5-min rolling average data
- Kalshi settles on NWS CLI product (2-min averages of raw ASOS data)
- These are DIFFERENT computations with DIFFERENT aggregation windows
- A spike visible in METAR may NOT appear in CLI settlement — and vice versa

**Mitigations:**
1. Trade sizing must account for METAR-vs-CLI divergence risk: 0.85x multiplier on Kelly allocation
2. Post-settlement reconciliation: cron job scrapes CLI daily max/min and compares against lane-tracked max from METAR
3. If divergence rate exceeds 10% over 30 days: halt lane trading and investigate

### 6.4 CLI Reconciliation Specification

Add a daily cron (00:00 UTC) that:
1. Scrapes NWS CLI product from `forecast.weather.gov/product.php?site={WFO}&product=CLI&issuedby={CITY}`
2. Extracts reported daily max/min temperature
3. Loads lane-tracked daily max from `metar_monitor._STATE["running_daily_max"]`
4. Logs discrepancies in `settlement_divergence_log` table
5. If divergence > 5% for 3 consecutive days: alert

**Effort:** ~4 hours to implement

---

## 7. Station-Rank-Selective Activation

### 7.1 Station Selection Criteria

The lane activates per station based on:

| Criterion | Minimum | Target |
|-----------|---------|--------|
| Historical lane accuracy | 40% | ≥60% |
| Minimum lane predictions (30-day) | 5 | ≥15 |
| MICAS metric (microstructure capture rate) | 0.30 | ≥0.50 |
| Max daily swings | ≥2°F typical | ≥3°F |
| Station data quality tier | Tier 2+ | Tier 1 |

### 7.2 Station Rank Classification

**Tier 1 (Full lane deployment):**
- Station accuracy ≥ 60% over last 30 days
- ≥ 15 lane predictions in last 30 days
- MICAS ≥ 0.50
- Full Kelly allocation, no exceptions

**Tier 2 (Reduced lane deployment):**
- Station accuracy ≥ 40% over last 30 days
- ≥ 5 lane predictions in last 30 days  
- MICAS ≥ 0.30
- 0.5x Kelly allocation, late-day gate enforced strictly

**Tier 3 (Disabled):**
- Station accuracy < 40%
- OR < 5 predictions in last 30 days
- No trades from this station

### 7.3 MICAS Metric (Microstructure Capture Accuracy Score)

```python
def compute_micas(station: str, rolling_window_days: int = 30) -> float:
    """Compute Microstructure Capture Accuracy Score.
    
    MICAS measures how well the lane captures genuine microstructure events:
    - TP: detection fired AND METAR max ≠ IEM 1-min max (spike occurred)
    - FP: detection fired BUT METAR max == IEM 1-min max (false positive)
    - FN: no detection BUT METAR max ≠ IEM 1-min max (missed spike)
    
    MICAS = TP / (TP + FP + FN)
    """
```

### 7.4 Initial Activation

| Station | Tier | Rationale |
|---------|------|-----------|
| KMDW | 1 | High historical volume, 70.59% standalone accuracy |
| KDEN | 1 | 71.43% standalone accuracy |
| KAUS | 1 | 66.67% standalone accuracy |
| KATL | 1 | 61.96% ensemble accuracy, strong daily swings |
| KBOS | 2 | 66.67% standalone (low sample: 3) |
| KLAX | 2 | 60.61% ensemble accuracy, moderate |
| KNYC | 2 | 21.95% standalone — poor, but microstructure edge exists |
| KSEA | 3 | 21.28% — disabled |
| KPHX | 3 | 17.54% — disabled |
| KPHL | 3 | 22.22% — disabled |
| Others | 3 | Insufficient evidence or poor accuracy |

---

## 8. Integration with 4-Signal Core

### 8.1 Architecture Position

```
┌──────────────────────────────────────────────┐
│  GEFS Ensemble (Prior — 66.17% baseline)      │
│  P(bucket) ← distribution                     │
└─────────────┬─────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────┐
│  4-SIGNAL CORE (daily ensemble)              │
│                                              │
│  gaussian, gaussian_v2, pressure_delta,      │
│  forecast_disagreement                        │
│                                              │
│  Each returns (direction, confidence)         │
│  → Ensemble vote → Posterior₁                │
└─────────────┬─────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────┐
│  LANE LAYER (posterior modifier)              │
│                                              │
│  Lane 1: NowcastingLane                      │
│    → frontal passage detection               │
│    → Probability-mass shift dict             │
│                                              │
│  Lane 2: MicrostructureSpikeDetector         │
│    → Transient spike at bucket boundary      │
│    → returns shift threshold                 │
│    → applied only when spike confirmed       │
│                                              │
│  Posterior₂ ∝ Post₁ × (1 + Σ(lane_shifts))  │
└─────────────┬─────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────┐
│  Final EV: Σ P(bucket) × payoff(bucket)      │
│  → Kelly sizing → position                   │
└──────────────────────────────────────────────┘
```

### 8.2 Lane Output Contract

The MicrostructureSpikeDetector exposes:

```python
class MicrostructureSpikeResult:
    """Output from MicrostructureSpikeDetector.evaluate()."""
    spike_detected: bool           # True if microstructure spike confirmed
    direction: Optional[str]       # 'up' if spike was DOWN (bounce), 'down' if spike was UP (revert)
    confidence: float              # 0.0-1.0 calibrated confidence
    bucket_boundary: int           # Which bucket was crossed
    spike_type: str                # 'instant_cross' or 'settlement_up_revert'
    hypothesis: str                # 'H1' (transient) or 'H2' (structural)
    details: Dict[str, Any]        # Full event context for logging
    
    def get_prob_shift(self) -> Dict[int, float]:
        """Returns probability-mass shift to apply to posterior.
        
        For 'down' prediction (spike UP → revert DOWN):
            shift[bucket_crossed] = -0.02  # reduce P of staying above
            shift[bucket_crossed - 1] = +0.02  # increase P of below
            
        For 'up' prediction (spike DOWN → bounce UP):
            shift[bucket_crossed] = -0.02
            shift[bucket_crossed + 1] = +0.02
            
        Returns empty dict if spike_detected is False.
        """
```

### 8.3 LaneManagerV2 Integration

```python
class MicrostructureSpikeDetector:
    # ... full implementation ...
    
    def evaluate(self, station: str, obs_temp: float, obs_time: datetime,
                 prev_temp: Optional[float] = None,
                 daily_max: Optional[float] = None,
                 gefs_frontal_flag: bool = False,
                 neighbor_temps: Optional[List[float]] = None,
                 wind_gust_kt: Optional[float] = None,
                 dewpoint_f: Optional[float] = None) -> MicrostructureSpikeResult:
        ...
    
    def get_prob_shift(self, station: str) -> Dict[int, float]:
        """
        Returns the accumulated probability-mass shift for this station.
        Called by LaneManagerV2 during posterior blending.
        
        Returns empty dict if no active spike for this station.
        The shift is applied ONCE and then cleared (ephemeral modifier).
        """
        ...
```

### 8.4 Capital Allocation (Gray Room R7 C3)

The 4-signal core and microstructure lane operate under **separate capital allocations**:

| Allocation | Component | Capital % | Rationale |
|------------|-----------|-----------|-----------|
| Primary | 4-signal core (daily ensemble) | 70% | Established 56% holdout accuracy, 1.75 Sharpe |
| Lane 1 | Nowcasting/frontal passage | 10% | Posterior modifier, ~15-20% fire rate |
| Lane 2 | MicrostructureSpikeDetector | 15% | Posterior modifier, ~10-15% fire rate |
| Reserve | Cash | 5% | Contingency |

**Key rule:** No capital from the 4-signal core is ever diverted to lanes. Lane capital is a separate budget. Lanes do NOT affect the core's position sizing — they add independent edge above the core baseline.

### 8.5 Pipeline Interaction Rules

1. **4-signal core runs first** — produces Posterior₁ (direction, confidence, bucket probabilities)
2. **MicrostructureSpikeDetector runs second** — event-driven, fires on new METAR observation
3. **If spike detected:**
   - Apply `get_prob_shift()` to Posterior₁ → Posterior₂
   - Recompute expected value with modified posterior
   - If EV change exceeds threshold (0.05¢): generate trade signal
4. **If no spike detected:** Posterior₂ = Posterior₁ (no change)
5. **LaneManagerV2** tracks per-lane P&L for performance monitoring
6. **Cooldowns are LANE-level, not core-level:**
   - Station cooldown: 300s between lane trades
   - Boundary cooldown: 900s per boundary

### 8.6 Integration with Existing metar_monitor Pipeline

The MicrostructureSpikeDetector replaces the embedded spike detection in `_process_temperature_event` and `_evaluate_deterministic_signal_layer`. The extraction plan:

1. Extract `_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER`, tracker maintenance, and confidence computation into `core/spike_detector.py` (MicrostructureSpikeDetector class)
2. Replace `_evaluate_deterministic_signal_layer`'s microstructure logic with a call to `MicrostructureSpikeDetector.evaluate()`
3. The `_emit_signal_alert` pathway stays in `metar_monitor.py` as the alert delivery mechanism
4. The `_compute_microstructure_spike_confidence` function remains but is called through the new class

---

## 9. State Management & Persistence

### 9.1 In-Memory State

```python
# Per-station state
_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER: Dict[Tuple[str, int], Dict[str, Any]]  # (station, epoch_id) → tracker
_SIGNAL_EPOCH_COUNTER: Dict[str, int]  # station → current epoch counter
_SIGNAL_STATION_LAST_EMIT: Dict[str, float]  # station → last emit timestamp (for cooldown)
_SIGNAL_BOUNDARY_LAST_EMIT: Dict[Tuple[str, int, int], float]  # (station, boundary, epoch) → timestamp
_PENDING_CROSSINGS: Dict[Tuple[str, int, int], Dict[str, Any]]  # (station, epoch, bucket) → crossing info
_DAILY_MAX_TRACKER: Dict[Tuple[str, str], float]  # (station, local_date) → running_daily_max
```

### 9.2 Persistence

**Separate SQLite DB** for lane state (R14 S4 — data flow isolation):

```python
GOLDILOCKS_DB = os.getenv("GOLDILOCKS_DB", "/opt/render/project/src/data/goldilocks_state.db")
```

Tables:
- `spike_events` — all confirmed spike detections with metadata
- `station_calibration` — rolling 30-day calibration data per station
- `settlement_divergence_log` — METAR-vs-CLI divergence records

The GEFS pipeline NEVER touches this DB.

### 9.3 Bootstrap & Recovery

On restart:
1. Load last known state from `GOLDILOCKS_DB` (last 24 hours of trackers)
2. Rehydrate `_DAILY_MAX_TRACKER` from `metar_monitor._STATE["running_daily_max"]`
3. Clear old trackers (> 24 hours stale)
4. Begin normal polling

---

## 10. Implementation Prerequisites

### 10.1 Required Modules (Existing)

| Module | File | Status | Integration |
|--------|------|--------|-------------|
| DualHypothesisEngine | `core/dual_hypothesis_engine.py` | Existing | H1/H2 classification |
| GoldilocksLane (predecessor) | `core/lane_goldilocks.py` | Existing | Refactor base |
| METARQualityParser | `core/metar_qc_parser.py` | Existing | QC gate integration |
| StationRankSelective | `core/station_rank_selective.py` | Existing | Station tier filter |