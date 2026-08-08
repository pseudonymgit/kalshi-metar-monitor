# Trajectory Confirmation Gate — Design Specification

**Date:** 2026-08-06
**Author:** Meteorological Pattern Matching Expert (subagent)
**Status:** Design specification — requires C-Suite review before implementation
**Prerequisite reading:** `docs/plans/TRAJECTORY-LANE-DESIGN.md`, `docs/plans/GOLDILOCKS-TRAJECTORY-DESIGN-FRAME.md`, `docs/plans/FP-SPATIAL-COHERENCE.md`
**Purpose:** Design a confirmation gate that uses epoch-based analog matching to confirm or reject a signal's direction prediction before it reaches trade generation.

---

## Table of Contents
1. [Concept: Confirmation Gate vs Heavy Informant](#1-concept)
2. [Epoch Definition and Construction](#2-epoch-definition)
3. [Epoch Similarity Matching](#3-epoch-similarity-matching)
4. [Trajectory Match Criteria](#4-trajectory-match-criteria)
5. [Minimum Matching Epochs](#5-minimum-matching-epochs)
6. [Confidence Modulation](#6-confidence-modulation)
7. [Data Source Recommendations](#7-data-sources)
8. [Implementation Plan](#8-implementation)
9. [Expected Accuracy Improvement](#9-expected-accuracy)
10. [Risk Factors](#10-risk-factors)

---

## 1. Concept: Confirmation Gate vs Heavy Informant

### 1.1 Distinction from the Existing Trajectory Lane

The existing trajectory lane (`TRAJECTORY-LANE-DESIGN.md`) is designed as a **heavy informant** — it weights the GEFS probability with a secondary trajectory-based estimate but never overrides. Its maximum influence is `w_traj = 0.15 × traj_quality`.

The **Confirmation Gate** is different:

| Aspect | Heavy Informant (Existing) | Confirmation Gate (This Spec) |
|---|---|---|
| Output | Probability blend | Pass/Fail + confidence modulation |
| Max influence on conviction | 0.15 weight in probability blend | 0.0× to 1.3× multiplier on conviction |
| Blocking power | Cannot block | Can block if trajectory contradicts signal strongly |
| Analog requirement | Any count ≥ 20 | Tiered: ≥10 (soft), ≥30 (gate), ≥100 (strong confirm) |
| Decision threshold | Continuous blend | Stepped: PASS, WEAK, FAIL with clear rules |
| Target use case | Daily guidance | High-conviction trade confirmation |

**Design philosophy:** The confirmation gate is a **safety layer** — it catches the cases where the GEFS ensemble says one direction, but historical analogues say the opposite pattern leads to a different outcome. It does not override the ensemble on strength; it only overrides on *direction conflict*. If ensemble and trajectory agree on direction, the gate boosts confidence.

### 1.2 When the Gate Fires

The confirmation gate evaluates every signal before trade generation. It returns one of three dispositions:

| Disposition | Definition | Action |
|---|---|---|
| **CONFIRM** | Trajectory direction agrees with signal direction | Increase conviction by up to 1.3× |
| **NEUTRAL** | Trajectory confidence too low to evaluate, or trajectory direction is ambiguous | No change to conviction |
| **OVERRIDE** | Trajectory direction contradicts signal direction with high confidence | Block trade, or reduce conviction to 0.3× for manual review |

**The gate only blocks on OVERRIDE.** This is the key difference from a simple modulation — a CONFIRM boosts, a FAIL blocks.

---

## 2. Epoch Definition and Construction

### 2.1 What Is an Epoch?

An **epoch** is a 24-hour meteorological state vector at a single station. Unlike the existing trajectory system which uses raw METAR observations, we use **analysis fields** from reanalysis + observed METARs to build a richer state vector.

### 2.2 Epoch Vector

Each epoch `E(s, d)` for station `s` on date `d` contains:

```python
epoch = {
    # Fields from METAR observations (observed, high confidence)
    'temp_max_f': float,          # Daily max temperature
    'temp_min_f': float,          # Daily min temperature
    'temp_mean_f': float,         # Daily mean temperature
    'dewpoint_mean_f': float,     # Daily mean dewpoint
    'rh_mean_pct': float,         # Daily mean relative humidity
    'wind_speed_mean_kt': float,  # Daily mean wind speed
    'wind_dir_mode': int,         # Prevailing wind direction (modal, degrees)
    'pressure_mean_mb': float,    # Daily mean sea-level pressure
    'pressure_trend': float,      # 24h pressure change (mb)
    
    # Fields from ISD-lite cloud cover
    'cloud_cover_mean': float,    # Mean sky cover fraction [0,1]
    
    # Derived fields
    'temp_range_f': float,        # temp_max_f - temp_min_f
    'dp_depression_mean': float,  # temp_mean_f - dewpoint_mean_f
    'wind_speed_max_kt': float,   # Max wind speed in 24h
    
    # Synoptic regime label (from pattern classifier)
    'regime': str,                # e.g., 'cold_front', 'high_pressure', 'maritime'
    
    # Outcome
    'settlement_high': float,     # Actual HIGH settlement temperature
    'settlement_low': float,      # Actual LOW settlement temperature
    'settlement_bucket_high': str, # Kalshi bucket for HIGH
    'settlement_bucket_low': str,  # Kalshi bucket for LOW
}
```

### 2.3 Epoch Labeling — Outcome Resolution

Each epoch must be paired with the outcome it led to. For trading, this means:

- **HIGH market outcome:** The settlement temperature for the next trading day's HIGH bucket
- **LOW market outcome:** The settlement temperature for the next trading day's LOW bucket

The epoch stores **tomorrow's** outcome, not today's. This is the ground truth.

```python
# For epoch ending on date D:
epoch['settlement_high'] = SETTLEMENT_HIGH(D+1)  # Next day's HIGH
epoch['settlement_low'] = SETTLEMENT_LOW(D+1)    # Next day's LOW
```

### 2.4 Feature Selection and Weights for Distance Computation

Not all features in the epoch vector contribute equally to pattern discrimination. Based on first-principles meteorology:

| Feature | Weight | Rationale |
|---|---|---|
| `temp_max_f` | 0.20 | Primary thermal signature; captures daytime boundary layer state |
| `temp_min_f` | 0.15 | Nighttime thermal state; captures stable boundary layer |
| `pressure_mean_mb` | 0.15 | Synoptic regime fingerprint; captures airmass identity |
| `pressure_trend` | 0.10 | Direction of synoptic evolution (rising = high building, falling = trough) |
| `wind_dir_mode` | 0.10 | Advection origin (NW = cold/dry, SW = warm/moist) |
| `wind_speed_mean_kt` | 0.08 | Advection rate; mixing depth indicator |
| `dewpoint_mean_f` | 0.08 | Moisture content; airmass identifier |
| `cloud_cover_mean` | 0.07 | Radiative regime; insolation proxy |
| `temp_range_f` | 0.05 | Diurnal amplitude; stability/cloud cover indicator |
| `rh_mean_pct` | 0.02 | Redundant with dewpoint; low weight |

**Total: 1.00**

### 2.5 Epoch Corpus Construction

The corpus is built from the combination of:
1. **METAR observations** (observed temps, wind, pressure, dewpoint) — `data/metar_backfill.db`
2. **ISD-lite cloud cover** — `data/isd_lite_raw.db`
3. **Settlement outcomes** — `data/kalshi_settlements.db`
4. **ERA5 reanalysis fields** (optional enrichment — see §7)

**Target corpus size:** Minimum 3 years of daily epochs for each of the 20 stations = ~21,900 station-days. The existing backfill covers 2+ years; ongoing operations continue to grow this.

**Corpus refresh:** New epoch appended daily after settlement resolves (D+1 outcome known). This ensures the corpus self-updates.

---

## 3. Epoch Similarity Matching

### 3.1 Distance Metric

We use **cosine-weighted Euclidean distance** over the feature vector, not DTW (Dynamic Time Warping). Rationale:

- The trajectory lane uses DTW for multi-day sequence matching (5-day lookback)
- The confirmation gate matches **single epochs** (state vectors), not sequences
- For single-epoch matching, weighted Euclidean distance is simpler, faster, and equally effective
- Cosine weighting prevents high-magnitude features from dominating

```
d(E_q, E_c) = sqrt( Σ_i w_i × ((v_q_i - v_c_i) / σ_i)² )
```

Where:
- `E_q` = query epoch (today's observed state + tomorrow's forecast)
- `E_c` = candidate epoch (historical state)
- `w_i` = feature weight from §2.4
- `v_q_i`, `v_c_i` = feature values
- `σ_i` = feature standard deviation across the corpus (z-score normalization)

### 3.2 Z-Score Normalization

Each feature is normalized to z-scores using the station's own climatology:

```
z_i = (v_i - μ_i_station) / σ_i_station
```

Where `μ_i_station` and `σ_i_station` are the mean and standard deviation of feature `i` for station `s` across all epochs in the corpus.

**Why station-specific normalization?** A 90°F day in Phoenix is normal; a 90°F day in Seattle is extreme. Without station-specific normalization, the distance computation would over-weight absolute temperature differences.

### 3.3 The Query Epoch

The confirmation gate runs **after** the GEFS ensemble produces its forecast but before trade generation. The query epoch is constructed from:

1. **Observed weather:** The last N days of observations (N = 5 recommended) — what actually happened
2. **Forecast for tomorrow:** The GEFS ensemble's forecast for the target date — what the model predicts

The query epoch's outcome field is *unknown* — that's what we're trying to predict.

```
epoch_query = {
    # Observed (certain)
    'temp_max_f': observed_yesterday_max,
    'temp_min_f': observed_yesterday_min,
    ...  # all observed fields from last complete day
    
    # Forecast (uncertain — from GEFS mean)
    'temp_max_f_tomorrow': gefs_mean_high,
    'temp_min_f_tomorrow': gefs_mean_low,
    ...  # forecast fields
}
```

### 3.4 Matching Algorithm

```
def find_analog_epochs(query_epoch, corpus, top_k=200):
    """Find the top-K most similar historical epochs."""
    
    # Exclude epochs within 14 days of query date (temporal autocorrelation)
    corpus_filtered = [e for e in corpus 
                       if abs(e['date'] - query_epoch['date']) > 14]
    
    # Compute distances
    distances = []
    for candidate in corpus_filtered:
        d = weighted_euclidean(query_epoch, candidate, FEATURE_WEIGHTS, STATION_SIGMAS)
        distances.append((d, candidate))
    
    # Sort by distance (ascending)
    distances.sort(key=lambda x: x[0])
    
    return distances[:top_k]
```

**Computational complexity:** O(N × K) where N = corpus size (~22,000 epochs), K = feature count (10). This is ~220,000 operations per query — trivially fast on modern hardware (~5ms per station).

### 3.5 Temporal Exclusion Window

Epochs within 14 calendar days of the query date are excluded from the analog set. This prevents temporal autocorrelation from inflating similarity scores. A September 5 query should not match a September 3 epoch from a different year — the seasonal cycle would make them appear similar but the information is not independent.

---

## 4. Trajectory Match Criteria

### 4.1 What Constitutes a Trajectory Match

A **trajectory match** is an analog epoch whose **forward outcome** (the settlement temperature on D+1) is consistent with the signal's directional prediction. We categorize the outcome direction relative to climatology:

```
outcome_direction = {
    'cool':  settlement_temp < climo_mean - climo_std * 0.5,
    'neutral': abs(settlement_temp - climo_mean) <= climo_std * 0.5,
    'warm':  settlement_temp > climo_mean + climo_std * 0.5,
}
```

A trajectory is a **match** if:
- Signal predicts DOWN (LOW market): analog outcome_direction is 'cool'
- Signal predicts UP (LOW market): analog outcome_direction is 'warm'
- Signal predicts DOWN (HIGH market): analog outcome_direction is 'cool'
- Signal predicts UP (HIGH market): analog outcome_direction is 'warm'

### 4.2 Match Quality Tiers

| Tier | Distance percentile | Description |
|---|---|---|
| **Excellent** | Top 5% of corpus | Nearly identical synoptic pattern |
| **Good** | 5-15% | Similar pattern, minor differences in moisture or wind |
| **Fair** | 15-30% | Broadly similar regime, some feature mismatch |
| **Poor** | 30-50% | Some shared features, but significant differences |
| **No match** | >50% | Too dissimilar to be informative |

### 4.3 Match by Pressure Pattern

Pressure pattern match is assessed independently through a **synoptic regime classifier**:

```
PRESSURE_PATTERNS = {
    'ridge':    high pressure building, rising pressure trend
    'trough':   low pressure approaching, falling pressure trend
    'zonal':    flat pressure gradient, minimal trend
    'blocking': anomalous high stationary, persistent pattern
    'front':    sharp pressure gradient, temperature advection
}
```

A trajectory match requires the **pressure pattern** to align within 1 regime category. A trough pattern should match other trough epochs, not ridge epochs.

### 4.4 Wind Regime Match

Wind regime is assessed through wind direction alignment:

```
wind_regime = {
    'northerly': wind_dir ∈ [315°, 45°]     # cold advection
    'easterly':  wind_dir ∈ [45°, 135°]     # maritime/cool
    'southerly': wind_dir ∈ [135°, 225°]    # warm advection
    'westerly':  wind_dir ∈ [225°, 315°]    # dry/continental
}
```

Match requires wind regime to be the same **or adjacent** (northerly can match northerly or westerly). Wind regimes that differ by 180° (e.g., northerly vs southerly) are anti-correlated and treated as non-matches.

### 4.5 Moisture Match

Moisture is assessed through dewpoint depression (DPD):

```
DPD = temp_mean - dewpoint_mean

moisture_regime = {
    'dry':    DPD > 20°F     # desert or continental polar airmass
    'moderate': DPD ∈ [10°F, 20°F]  # typical continental
    'moist':  DPD ∈ [5°F, 10°F]     # maritime or Gulf airmass
    'saturated': DPD < 5°F   # fog/threat of precipitation
}
```

Match requires moisture regime to be within 1 category. Dry should not match moist.

### 4.6 Composite Trajectory Match Score

```
match_score = w_pressure × I(pressure_pattern_match)
            + w_wind × I(wind_regime_match)
            + w_moisture × I(moisture_regime_match)
            + w_temp × analog_temp_similarity
```

Where:
- `I()` is the indicator function (1 if match, 0 if not)
- `w_pressure = 0.35` — pressure pattern is the strongest synoptic identifier
- `w_wind = 0.25` — wind regime determines advection source
- `w_moisture = 0.20` — moisture determines airmass
- `w_temp = 0.20` — raw temperature similarity (continuous, not binary)

The analog is classified as a **trajectory match** if `match_score ≥ 0.60`.

### 4.7 Forward Outcome Distribution

For each query, we compute the distribution of settlement outcomes among the Top-K trajectory matches:

```
top_k = min(200, number_of_high_quality_matches)

# For each analog in top_k:
outcomes = [analog['settlement_bucket'] for analog in top_k]

# Outcome distribution:
P_traj(bucket) = count(bucket in outcomes) / len(outcomes)
```

Example output:
```
KMDW, 2026-01-15, HIGH market:
  Analog count: 127
  P_traj(below_30): 0.45
  P_traj(30-35):    0.31
  P_traj(35-40):    0.16
  P_traj(above_40): 0.08
  Trajectory direction: DOWN (below_30 + 30-35 = 76% probability of cold)
```

---

## 5. Minimum Matching Epochs

### 5.1 Gate Threshold Structure

The confirmation gate uses **tiered thresholds** based on analog count:

```
ANALOG_COUNT_TIERS = {
    'insufficient': (0, 9),      # Too few analogs — gate does nothing
    'minimal':      (10, 29),    # Soft modulation only
    'adequate':     (30, 99),    # Gate activates
    'strong':       (100, None), # Gate operates at full strength
}
```

### 5.2 Disposition by Analog Count

| Analog count | Can confirm? | Can override? | Recommended action |
|---|---|---|---|
| 0-9 | No | No | Return NEUTRAL — insufficient data |
| 10-29 | Yes (soft) | No | Modulation only (M = 0.85-1.15), cannot block |
| 30-99 | Yes | Yes (soft) | Full gate, override reduces conviction to 0.5× |
| 100+ | Yes | Yes (full) | Full gate, override blocks trade |

### 5.3 Why 30 Epochs as the Gate Threshold?

**Statistical rationale:** With 30 independent analog epochs, the standard error of the bootstrap proportion estimate is:

```
SE = sqrt(p × (1-p) / n)

For p = 0.65 (direction agreement), n = 30:
SE = sqrt(0.65 × 0.35 / 30) = 0.087

95% confidence interval: p ± 1.96 × SE = 0.65 ± 0.17 = [0.48, 0.82]
```

At n=30, the lower bound of the confidence interval (0.48) is below 0.50 (random), meaning the trajectory signal is informative but not definitive. At n=100:

```
SE = sqrt(0.65 × 0.35 / 100) = 0.048
95% CI: 0.65 ± 0.094 = [0.556, 0.744]
```

The CI lower bound is now above 0.50 — statistically significant at the 95% level. This is why n=100 is the threshold for full-strength gate operation.

**Corpus growth requirement:** The corpus needs sufficient historical data to produce 30+ analogs for a typical query. With 3 years (1095 days) of data per station and roughly 5% of epochs being similar to any given query, we expect ~55 analogs from cross-station pooling — adequate for the gate to function.

### 5.4 Climate Zone Pooling

When same-station analog count < 30, pool across climate zones as in the existing trajectory lane design:

| Zone | Stations | Pooling strategy |
|---|---|---|
| Northeast | KNYC, KBOS, KPHL, KDCA | Same climate, tight coupling |
| Midwest | KMDW, KMSP | Continental, similar latitude |
| South Central | KHOU, KDFW, KAUS, KSAT, KOKC | Gulf-influenced continental |
| Southeast | KATL, KMIA, KMSY | Humid subtropical + tropical |
| Rocky West | KDEN, KPHX, KLAS | Semi-arid/desert, high elevation |
| Pacific | KSEA, KSFO, KLAX | Maritime Mediterranean |

**Cross-zone matching is allowed but penalized:** Apply a distance multiplier of 1.5× to the Euclidean distance when matching across zones. This preferentially selects same-zone analogs but uses cross-zone analogs when necessary.

---

## 6. Confidence Modulation

### 6.1 Gate Disposition Logic

```python
def evaluate_gate(signal_direction: str,       # 'up' or 'down'
                  trajectory_distribution: dict, # {bucket: probability}
                  analog_count: int,
                  match_score: float) -> dict:
    """
    Returns gate disposition for a single station-market pair.
    """
    # Step 1: Determine trajectory direction
    if trajectory_distribution is None or analog_count < 10:
        return {'disposition': 'NEUTRAL', 'reason': 'insufficient_analogs', 'modulation': 1.0}
    
    # Compute trajectory's implied direction
    # For HIGH markets: 'up' = warmer than climo, 'down' = cooler than climo
    # For LOW markets: 'up' = warmer than climo, 'down' = cooler than climo
    p_warm = sum(p for bucket, p in trajectory_distribution.items() if bucket_is_warm(bucket))
    p_cool = sum(p for bucket, p in trajectory_distribution.items() if bucket_is_cool(bucket))
    
    if p_warm > 0.55:
        traj_direction = 'up'
    elif p_cool > 0.55:
        traj_direction = 'down'
    else:
        traj_direction = 'neutral'
    
    # Step 2: Compare directions
    if traj_direction == 'neutral':
        return {'disposition': 'NEUTRAL', 'reason': 'ambiguous_trajectory', 'modulation': 1.0}
    
    if signal_direction == traj_direction:
        # CONFIRM — boost conviction
        return confirm_disposition(analog_count, match_score)
    else:
        # OVERRIDE — reduce or block
        return override_disposition(analog_count, match_score)
```

### 6.2 Confirm Disposition (Signal Confirmed)

```python
def confirm_disposition(analog_count, match_score):
    boost = 1.0
    
    # Base boost from analog count
    if analog_count >= 100:
        boost = 1.30
    elif analog_count >= 50:
        boost = 1.20
    elif analog_count >= 30:
        boost = 1.15
    elif analog_count >= 10:
        boost = 1.10
    
    # Additional boost from match quality
    boost += 0.10 * (match_score - 0.60)  # max additional +0.04 at match_score=1.0
    
    boost = min(1.30, boost)
    
    return {
        'disposition': 'CONFIRM',
        'reason': f'trajectory_confirms_direction',
        'modulation': boost,
        'analog_count': analog_count,
        'traj_agreement_pct': max(p_warm, p_cool),
    }
```

### 6.3 Override Disposition (Signal Contradicted)

```python
def override_disposition(analog_count, match_score):
    if analog_count >= 100 and match_score >= 0.80:
        # Strong override — block the trade
        return {
            'disposition': 'OVERRIDE',
            'reason': 'strong_trajectory_contradiction',
            'modulation': 0.0,      # Block
            'block': True,
            'analog_count': analog_count,
        }
    elif analog_count >= 50 and match_score >= 0.70:
        # Moderate override — severely reduce
        return {
            'disposition': 'OVERRIDE',
            'reason': 'moderate_trajectory_contradiction',
            'modulation': 0.30,     # 70% reduction
            'block': False,
            'analog_count': analog_count,
        }
    elif analog_count >= 30:
        # Weak override — reduce
        return {
            'disposition': 'OVERRIDE',
            'reason': 'weak_trajectory_contradiction',
            'modulation': 0.50,     # 50% reduction
            'block': False,
            'analog_count': analog_count,
        }
    else:
        # Minimal analogs — soft modulation only
        return {
            'disposition': 'NEUTRAL',
            'reason': 'insufficient_analogs_for_override',
            'modulation': 0.85,     # 15% reduction
            'block': False,
            'analog_count': analog_count,
        }
```

### 6.4 Integration with Conviction Score

The modulation is applied as a **multiplier** on the existing conviction score, consistent with how the spatial coherence gate works:

```
conviction_adjusted = conviction_base × M_trajectory
```

Where `M_trajectory` is the modulation factor from the gate (0.0 = block, 1.3 = max boost).

**Pipeline order:**

```
1. LLOP fusion → P_up / P_down
2. Calibration correction
3. Signal Agreement Score (SAS)
4. Base conviction = offset × agreement × cal × sample
5. Spatial Coherence Gate → M_spatial
6. TRAJECTORY CONFIRMATION GATE → M_trajectory (NEW)
7. Conviction_final = conviction_base × M_spatial × M_trajectory
8. Trade if conviction_final ≥ threshold
```

### 6.5 Interaction with Spatial Coherence Gate

The two gates operate independently and multiplicatively. A trade that is:
- CONFIRMED by trajectory (M = 1.20) AND
- HIGHLY COHERENT spatially (M = 1.15)
Receives net modulation: 1.20 × 1.15 = 1.38× conviction boost.

A trade that is:
- OVERRIDE by trajectory (M = 0.30) AND
- INCOHERENT spatially (M = 0.60)
Receives net modulation: 0.30 × 0.60 = 0.18× conviction — very likely blocked.

The multiplication is intentional — if both gates flag the same trade, the combined penalty should be severe.

---

## 7. Data Source Recommendations

### 7.1 Primary Source: METAR Observations (Already Collected)

| Data | Source | Table | Coverage |
|---|---|---|---|
| Temperature | `data/metar_backfill.db` | `metar_observations` | 2+ years, 848K+ records |
| Dewpoint | Same | Same | Same |
| Wind speed/direction | Same | Same | Same |
| Pressure | Same | Same | Same |
| Cloud cover | `data/isd_lite_raw.db` | `isd_lite_raw` | 10+ years |
| Settlement outcomes | `data/kalshi_settlements.db` | `settlement_epochs` | 2+ years, 6,171+ records |

**Status: ✅ Available now.** This is sufficient to build a functional confirmation gate without new data ingestion.

### 7.2 Recommended Enhancement: ERA5 Reanalysis

ERA5 provides **homogeneous, gap-free** hourly fields at 0.25° resolution for 1940-present. This is valuable for:

1. **Pressure fields at hub height** (850 hPa, 500 hPa) — more stable than station-level pressure
2. **Upper-air wind patterns** — 850 hPa wind direction identifies advection better than surface wind
3. **Total column water vapor** — better moisture identifier than surface dewpoint
4. **Downward longwave radiation** — directly quantifies radiational cooling potential
5. **10m wind speed** — calibrated, homogeneous across all stations

**Integration approach:** Use ERA5 fields to **augment** the epoch vector, not replace METARs. The METAR observations remain the ground truth for surface conditions. ERA5 provides synoptic-scale context.

**Availability:** ERA5 is free from Copernicus Climate Data Store (CDS). Download is via `cdsapi` Python client. A 0.25° × hourly grid for 20 stations × 5 years is approximately 20 × 365 × 5 × 24 = 876,000 grid-point-hours — about 500 MB compressed, 2 GB uncompressed.

**Implementation effort:** 2-3 days to set up the CDS download pipeline and integrate ERA5 fields into the epoch builder.

### 7.3 Recommended Enhancement: GEFS Reforecast

GEFS v12 reforecasts provide 31-member ensemble hindcasts for 2000-present, running at 00Z and 12Z cycles. This is valuable because:

1. **Direct model output** — matches the live GEFS signal exactly
2. **Ensemble spread** — historical spread-skill relationship
3. **Model bias quantification** — GEFS systematic bias by station × season

**Integration:** For each historical epoch, store:
- GEFS 31-member mean HIGH and LOW forecast
- GEFS ensemble spread (standard deviation)
- GEFS systematic bias (forecast - actual)

This enables the gate to answer: *"When the GEFS made this kind of forecast in the past, what actually happened?"*

**Availability:** GEFS reforecast data is available via NOMADS (NOAA) and can be downloaded using `herbie` Python package. Implementation effort: 3-5 days.

### 7.4 Minimum Viable Data Path

The confirmation gate can be built and validated using **only existing METAR data** (source §7.1). The ERA5 and GEFS reforecast enhancements add statistical power but are not blocking dependencies.

**Priority for data source integration:**

| Phase | Data source | Effort | Benefit | When |
|---|---|---|---|---|
| MVP | METAR only (existing) | 0 days | 60% of potential | Now |
| Phase 2 | ERA5 reanalysis | 3 days | +20% | After MVP validation |
| Phase 3 | GEFS reforecast | 5 days | +15% | After Phase 2 |
| Continuous | Live METAR → daily epoch appending | 0.1 hr/day | Self-improving corpus | Ongoing |

---

## 8. Implementation Plan

### 8.1 Phase 1: MVP — METAR-Only Gate (5 days)

**Day 1-2: Epoch Builder**
- Build `core/signals/trajectory_gate/epoch_builder.py`
  - Parse METAR observations into daily epoch vectors
  - Compute derived fields (temp range, DPD, pressure trend)
  - Save to SQLite: `data/trajectory_gate.db`
- SQLite schema:
```sql
CREATE TABLE epochs (
    id INTEGER PRIMARY KEY,
    station TEXT NOT NULL,
    date TEXT NOT NULL,
    temp_max_f REAL,
    temp_min_f REAL,
    temp_mean_f REAL,
    dewpoint_mean_f REAL,
    pressure_mean_mb REAL,
    pressure_trend_24h REAL,
    wind_speed_mean_kt REAL,
    wind_dir_mode INT,
    cloud_cover_mean REAL,
    dp_depression_mean REAL,
    rh_mean_pct REAL,
    temp_range_f REAL,
    settlement_high_f REAL,
    settlement_low_f REAL,
    UNIQUE(station, date)
);
CREATE INDEX idx_epochs_station_date ON epochs(station, date);
CREATE INDEX idx_epochs_pressure ON epochs(pressure_mean_mb);
CREATE INDEX idx_epochs_temp ON epochs(temp_mean_f);
```

**Day 3: Analog Matcher**
- Build `core/signals/trajectory_gate/analog_matcher.py`
  - Weighted Euclidean distance computation
  - Z-score normalization using station climatology
  - Top-K retrieval with temporal exclusion (14-day window)
  - Climate zone pooling fallback

**Day 4: Gate Decision Engine**
- Build `core/signals/trajectory_gate/confirmation_gate.py`
  - Gate disposition logic (CONFIRM / NEUTRAL / OVERRIDE)
  - Conviction modulation computation
  - Integration with existing conviction pipeline
  - Integration with spatial coherence gate (multiplicative)

**Day 5: Testing and Validation**
- Unit tests for epoch builder (verify 20 stations, correct feature extraction)
- Unit tests for analog matcher (verify distance computation, temporal exclusion)
- Unit tests for gate decision engine (verify all threshold cases)
- Backtest comparison: conviction distribution with vs without gate

### 8.2 Phase 2: ERA5 Augmentation (3 days)

- Set up CDS API access
- Download ERA5 hourly fields for station grid points
- Integrate 850 hPa temperature, wind, humidity into epoch vectors
- Add upper-air feature matching to analog distance computation
- Validate: does ERA5-augmented matching improve analog quality vs METAR-only?

### 8.3 Phase 3: GEFS Reforecast (5 days)

- Download GEFS v12 reforecast for all stations (2000-present)
- Store GEFS ensemble mean and spread per station × date
- Add "GEFS forecast signature" to epoch vector
- Enable query-by-GEFS-forecast: "When GEFS predicted this pattern before, what happened?"
- Validate: does GEFS reforecast matching outperform METAR-only matching?

### 8.4 Ongoing: Corpus Maintenance

- Daily cron: after settlement resolves, append new epoch to corpus
- Monthly: recompute z-score normalization statistics (rolling window)
- Quarterly: purge epochs > 5 years old if corpus exceeds performance budget

---

## 9. Expected Accuracy Improvement

### 9.1 Estimated Impact on Directional Accuracy

| Scenario | Current accuracy | With gate | Improvement |
|---|---|---|---|
| All trades, all stations | ~56-58% (baseline) | ~58-60% | +1-2 pp |
| Trades with CONFIRM disposition | ~56-58% | ~62-65% | +4-7 pp |
| Trades with OVERRIDE disposition | ~56-58% | ~30-40% (on blocked trades) | N/A — blocked |
| Trades with NEUTRAL disposition | ~56-58% | ~56-58% | No change |

The gate's value is in (a) boosting confidence on trades the analog ensemble confirms, and (b) suppressing trades the analog ensemble contradicts. The net effect on portfolio-level accuracy is modest (+1-2 pp) but meaningful at the margins.

### 9.2 Estimated Impact on Sharpe Ratio

| Scenario | Current Sharpe | With gate | Improvement |
|---|---|---|---|
| All trades | ~0.8-1.0 | ~0.9-1.15 | +0.1-0.15 |
| HIGH-only | ~0.7 | ~0.8 | +0.1 |
| LOW-only | ~0.9 | ~1.05 | +0.15 |

The larger impact on LOW markets is expected — LOW markets are more influenced by stable boundary layer processes that the analog ensemble captures well.

### 9.3 Trade Suppression Rate

| Disposition | % of trades affected | Net trade reduction |
|---|---|---|
| CONFIRM (boost) | ~25-35% | +5-10% more trades (boosted to threshold) |
| OVERRIDE (block) | ~5-10% | -5-10% fewer trades (blocked) |
| OVERRIDE (reduce) | ~8-15% | -5-8% (conviction cut by 50-70%) |
| NEUTRAL | ~50-60% | 0% (no change) |

**Net trade reduction:** ~5-15% fewer trades overall, with most of the reduction from trades that would have been losers.

---

## 10. Risk Factors

### 10.1 Temporal Autocorrelation Risk

**Risk:** Epochs within 14 days of the query date are excluded. But what if the same weather pattern repeats at 15 days? The exclusion window is arbitrary and may exclude valid analogs.

**Mitigation:** The 14-day window is based on typical synoptic-scale weather pattern persistence (Rossby wave period ~10-20 days). A 14-day exclusion ensures that temporal autocorrelation does not inflate match scores. Extending to 21 days would reduce the analog pool by ~20% — not worth the trade-off.

### 10.2 Climate Change Non-Stationarity Risk

**Risk:** As the climate warms, historical analogs from 3-5 years ago may no longer be representative of current patterns. A 90°F day in 2022 is different from a 90°F day in 2026 in terms of what comes next.

**Mitigation:**
1. **Rolling corpus:** Only keep epochs from the last 3 years (discard older data)
2. **Deseasonalized anomalies:** The z-score normalization removes the mean, so a +2σ day in 2022 maps to a +2σ day in 2026
3. **Trend-aware matching:** Include a 12-month trailing mean temperature offset to recenter the climatology
4. **Monitor:** Track analog quality over time — if mean distance increases year-over-year, reduce the corpus window

### 10.3 Sparse Analog Pool Risk

**Risk:** For some queries (rare synoptic patterns), the analog count may be < 10 even with cross-station pooling. The gate correctly returns NEUTRAL in this case, but it means the gate is not adding value for those trades.

**Mitigation:** As the corpus grows, the analog pool improves. After 5 years of daily epochs (36,500 station-days), the expected analog count for any query is ~75 (assuming 0.2% match rate) — well above the 30-threshold.

### 10.4 Regime-Dependent Quality Risk

**Risk:** The gate performs differently in different weather regimes. It may be excellent at catching cold-front false signals but miss heatwave mispredictions.

**Mitigation:** Regime-specific performance tracking. If the gate is consistently wrong in certain regimes (e.g., blocking patterns), add regime as a feature weight or adjust thresholds per regime.

### 10.5 Integration with Spatial Coherence Gate Risk

**Risk:** The two gates are multiplicative. In a worst case, a trade could be penalized twice for the same reason (e.g., an isolated station with no neighbors AND no analogs).

**Mitigation:** The multiplication is intentional but conservative. The worst-case modulation is 0.0 × 0.5 = 0.0 (blocked outright). However, if the station has no analogs (trajectory gate NEUTRAL, M=1.0) AND no neighbors (spatial gate low, M=0.5), the net is 0.5 — a 50% reduction, not a block. This prevents double-penalizing isolated stations.

### 10.6 Data Quality Risk

**Risk:** METAR observations can have instrument errors, missing fields, or QC failures. A bad pressure reading or spurious wind gust can corrupt the epoch vector.

**Mitigation:**
1. Use `metar_qc_parser.py` flags to exclude suspect observations
2. Require minimum 6 hourly observations per day to construct a valid epoch
3. Pressure trend requires both endpoints (24h apart) — if either is missing, trend is None
4. Wind direction mode requires at least 3 observations
5. If any critical field is missing, the epoch is excluded from the corpus

---

## Appendix A: Comparison with Existing Trajectory Lane

| Feature | Trajectory Lane (Heavy Informant) | Confirmation Gate (This Spec) |
|---|---|---|
| Primary method | DTW multi-day sequence matching | Weighted Euclidean single-epoch matching |
| Output | Probability blend (w_traj cap 0.15) | Disposition (CONFIRM/NEUTRAL/OVERRIDE) |
| Max boost | +15% weight | +30% conviction |
| Can block? | No | Yes (OVERRIDE) |
| Analog requirement | ≥20 | ≥30 for gate, ≥100 for strong |
| Temporal window | 3-5 day sequences | Single-day epoch + 14-day exclusion |
| Cross-station pooling | Climate zones | Climate zones + distance-weighted |
| Data sources | METAR + settlement | METAR + settlement + optional ERA5/GEFS |
| Integration | LLOP fusion weight | Conviction multiplier (post-fusion) |

**Recommendation:** Build and deploy both. The trajectory lane provides a lightweight probability adjustment for all trades. The confirmation gate provides a hard safety layer for high-stakes trades. They are complementary, not redundant.

---

*End of design specification. Ready for C-Suite review.*