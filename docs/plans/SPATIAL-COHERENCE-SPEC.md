# Spatial Coherence Verification — Design Specification

**Date:** 2026-08-06
**Author:** Meteorological Spatial Statistics Expert (subagent)
**Status:** Design specification — extends FP-SPATIAL-COHERENCE.md with station clustering, distance decay, and coastal handling
**Prerequisite reading:** `docs/plans/FP-SPATIAL-COHERENCE.md`, `docs/reference/C-SUITE-ROSTER.md` (station metadata)
**Purpose:** Design a cross-station verification system that modulates signal confidence based on nearby station agreement, with specific station clustering, distance decay functions, and coastal/inland handling for our 20-station network.

---

## Table of Contents
1. [Design Summary and Relationship to FP Document](#1-design-summary)
2. [Station Clustering Logic](#2-station-clustering)
3. [Distance Decay Function](#3-distance-decay)
4. [Minimum Confirming Stations](#4-minimum-confirming-stations)
5. [Coastal vs Inland Station Handling](#5-coastal-vs-inland)
6. [Prevailing Wind Direction and Upstream Weighting](#6-prevailing-wind)
7. [Implementation Plan](#7-implementation)
8. [Expected Accuracy Improvement](#8-expected-accuracy)
9. [Risk Factors](#9-risk-factors)

---

## 1. Design Summary

### 1.1 Relationship to FP-SPATIAL-COHERENCE.md

The First-Principles document (`FP-SPATIAL-COHERENCE.md`) provides the complete theoretical framework for spatial coherence — region definitions, consensus computation, anomaly-based distance weighting, and confidence modulation. This spec extends that work with:

**What this spec adds:**

| Aspect | FP-SPATIAL-COHERENCE.md | This spec |
|---|---|---|
| Station clustering | Region-based grouping | Hierarchical clustering + k-means for all 20 stations |
| Distance decay | Gaussian (exp(-d²/2L²)) | Extended: angular decay + altitude penalty |
| Minimum confirming stations | Implicit (weighted consensus) | Explicit tiered thresholds |
| Coastal vs inland | Mentioned as special case | Full handling: marine layer, sea breeze |
| Prevailing wind | Not addressed | Upstream/downstream weighting |
| Implementation timeline | Not specified | Phase 1/2/3 plan with validation gates |

### 1.2 Network Size

Our 20 active stations span 6 climate zones across the continental US, from tropical (KMIA) to continental (KMSP) to Mediterranean (KSEA). The median inter-station distance is ~700 km. This is a **sparse network** — the spatial coherence gate must work with this sparsity and not penalize stations that lack nearby neighbors.

---

## 2. Station Clustering Logic

### 2.1 Dual-Approach Clustering

We use two complementary methods:
1. **Primary: Hierarchical clustering** based on pairwise distance matrix + climate zone — produces stable, interpretable clusters
2. **Validation: k-means clustering** on feature vectors (lat, lon, elevation, climate zone code) — confirms assignments

### 2.2 Hierarchical Cluster Analysis

Using average-linkage clustering on the 20×20 great-circle distance matrix, cut at 600 km → 6 raw clusters. The dendrogram reveals that KPHL groups with Pacific stations (distance artifact, not climate). We correct this via climate zone labels.

### 2.3 Final Climate-Zone-Adjusted Clusters

| Region | Stations | Count | Mean Distance | Climate Type |
|---|---|---|---|---|
| **NE** (Northeast) | KNYC, KBOS, KPHL, KDCA | 4 | 360 km | Humid subtropical / Oceanic |
| **SE** (Southeast) | KATL, KMIA, KMSY | 3 | 960 km | Humid subtr. / Tropical monsoon |
| **SC** (South Central) | KHOU, KDFW, KAUS, KSAT, KOKC | 5 | 440 km | Humid subtropical |
| **MW** (Midwest) | KMDW, KMSP | 2 | 560 km | Humid continental |
| **RW** (Rocky West) | KDEN, KPHX, KLAS | 3 | 870 km | Semi-arid / Desert |
| **PAC** (Pacific) | KSEA, KSFO, KLAX | 3 | 1,090 km | Mediterranean / Oceanic |

KOKC has dual membership: SC (primary, 65% weight) + RW (secondary, 35% weight) due to dryline positioning.

### 2.4 Inter-Cluster Distance Matrix

```
         NE      SE      SC      MW      RW      PAC
NE       360     1,100   1,400   1,200   2,400   3,900
SE       1,100   960     900     1,400   2,100   3,200
SC       1,400   900     440     700     1,100   2,100
MW       1,200   1,400   700     560     1,400   2,800
RW       2,400   2,100   1,100   1,400   870     1,600
PAC      3,900   3,200   2,100   2,800   1,600   1,090
```

Key: SC is the geographic hub; PAC is the most isolated.

### 2.5 Station Coordinates Reference

| Station | City | Lat | Lon | Elev (ft) | Region |
|---|---|---|---|---|---|
| KNYC | New York | 40.78 | -73.97 | 42 | NE |
| KBOS | Boston | 42.36 | -71.01 | 20 | NE |
| KPHL | Philadelphia | 39.87 | -75.23 | 30 | NE |
| KDCA | Washington DC | 38.85 | -77.04 | 15 | NE |
| KATL | Atlanta | 33.64 | -84.43 | 1,026 | SE |
| KMIA | Miami | 25.79 | -80.29 | 8 | SE |
| KMSY | New Orleans | 29.99 | -90.25 | 3 | SE |
| KHOU | Houston | 29.65 | -95.28 | 72 | SC |
| KDFW | Dallas-Fort Worth | 32.90 | -97.04 | 607 | SC |
| KAUS | Austin | 30.19 | -97.67 | 542 | SC |
| KSAT | San Antonio | 29.53 | -98.47 | 810 | SC |
| KOKC | Oklahoma City | 35.39 | -97.60 | 1,301 | SC+RW |
| KMDW | Chicago | 41.79 | -87.75 | 620 | MW |
| KMSP | Minneapolis | 44.88 | -93.22 | 841 | MW |
| KDEN | Denver | 39.86 | -104.67 | 5,431 | RW |
| KPHX | Phoenix | 33.43 | -112.02 | 1,135 | RW |
| KLAS | Las Vegas | 36.08 | -115.17 | 2,181 | RW |
| KSEA | Seattle | 47.45 | -122.31 | 433 | PAC |
| KSFO | San Francisco | 37.62 | -122.37 | 13 | PAC |
| KLAX | Los Angeles | 33.94 | -118.41 | 128 | PAC |

---

## 3. Distance Decay Function

### 3.1 Primary: Gaussian Distance Decay

`w = exp(-d² / 2L²)` where L is decorrelation length. Extended with:
1. **Angular correction** — stations in same direction from target matter more
2. **Altitude penalty** — stations at different elevations are less relevant

### 3.2 Enhanced Distance Weighting

```python
def compute_spatial_weight(target, candidate, wind_direction=None):
    # 1. Great-circle distance
    d_km = great_circle_distance(target['lat'], target['lon'],
                                 candidate['lat'], candidate['lon'])
    # 2. Baseline Gaussian decay
    L = get_decorrelation_length(target['region'], candidate['region'])
    w_distance = math.exp(-d_km**2 / (2 * L**2))
    
    # 3. Angular enhancement (wind direction)
    w_angular = 1.0
    if wind_direction is not None:
        bearing = compute_bearing(target['lat'], target['lon'],
                                  candidate['lat'], candidate['lon'])
        angle_diff = abs(bearing - wind_direction)
        if angle_diff > 180:
            angle_diff = 360 - angle_diff
        if angle_diff <= 60:
            w_angular = 1.0       # Upwind
        elif angle_diff <= 120:
            w_angular = 0.7       # Cross-wind
        else:
            w_angular = 0.5       # Downwind
    
    # 4. Altitude penalty
    elev_diff = abs(target['elev'] - candidate['elev'])
    if elev_diff > 2000:
        w_altitude = 0.3
    elif elev_diff > 1000:
        w_altitude = 0.6
    elif elev_diff > 500:
        w_altitude = 0.8
    else:
        w_altitude = 1.0
    
    return max(w_distance * w_angular * w_altitude, 0.01)
```

### 3.3 Decorrelation Lengths (L)

```python
BASE_L = {'NE': 300, 'SE': 500, 'SC': 350, 'MW': 350, 'RW': 600, 'PAC': 400}
SEASONAL_L = {'winter': 0.85, 'spring': 1.00, 'summer': 1.15, 'fall': 1.00}

def get_decorrelation_length(r1, r2, season='fall'):
    L = BASE_L[r1] if r1 == r2 else min(BASE_L[r1], BASE_L[r2])
    return L * SEASONAL_L.get(season, 1.0)
```

### 3.4 Example Weight Computations

**KMDW (Chicago) with NW wind (cold advection):**
| Candidate | Dist (km) | L (km) | w_dist | w_angular | Elev diff | w_alt | Total |
|---|---|---|---|---|---|---|---|
| KMSP | 560 | 350 | 0.28 | 1.0 | 221 | 1.0 | **0.28** |
| KBOS | 1,370 | 300 | 0.00 | 0.5 | 600 | 0.8 | 0.00 |
| KOKC | 1,100 | 350 | 0.01 | 0.5 | 681 | 0.8 | 0.00 |

Result: KMSP is the only meaningful confirmor — correct, cold fronts move SE through the Midwest.

**KDFW (Dallas) with S wind (Gulf moisture):**
| Candidate | Dist (km) | L (km) | w_dist | w_angular | Elev diff | w_alt | Total |
|---|---|---|---|---|---|---|---|
| KAUS | 300 | 350 | 0.69 | 1.0 | 65 | 1.0 | **0.69** |
| KSAT | 440 | 350 | 0.45 | 1.0 | 203 | 1.0 | **0.45** |
| KHOU | 370 | 350 | 0.58 | 1.0 | 535 | 0.8 | **0.46** |
| KOKC | 320 | 350 | 0.65 | 0.5 | 694 | 0.8 | **0.26** |

Result: All SC stations contribute. KOKC gets angular penalty (downwind of DFW with southerly flow).

---

## 4. Minimum Confirming Stations

### 4.1 Tiered Thresholds

```python
MIN_CONFIRMING = {
    'NE': {'min_ct': 2, 'min_wsum': 0.8},   # 4 stations available
    'SE': {'min_ct': 2, 'min_wsum': 0.6},   # 3 stations, lower baseline
    'SC': {'min_ct': 3, 'min_wsum': 1.2},   # 5 stations available
    'MW': {'min_ct': 1, 'min_wsum': 0.3},   # Only 1 neighbor
    'RW': {'min_ct': 1, 'min_wsum': 0.4},   # Only 2 neighbors
    'PAC': {'min_ct': 1, 'min_wsum': 0.4},  # Only 2 neighbors
}
```

### 4.2 Confirmation Criteria

A station confirms if its forecast anomaly direction matches the target's AND magnitudes aren't wildly different (>6°C difference = no confirmation despite same direction):

```python
def is_confirming(target_anomaly, neighbor_anomaly, target_dir, neighbor_dir):
    if target_dir != neighbor_dir:
        return False
    if abs(abs(target_anomaly) - abs(neighbor_anomaly)) > 6:
        return False
    return True
```

### 4.3 Confidence Modulation

```python
def compute_confirm_modulation(region, weighted_confirm_count, total_weighted_count):
    thresh = MIN_CONFIRMING[region]
    confirm_frac = weighted_confirm_count / max(total_weighted_count, 0.01)
    
    if weighted_confirm_count < thresh['min_wsum'] or confirm_frac < 0.3:
        M = 0.5 + 0.3 * confirm_frac           # Penalize
    elif weighted_confirm_count >= thresh['min_wsum'] and confirm_frac >= 0.7:
        M = 1.0 + 0.2 * confirm_frac           # Boost
    else:
        M = 0.8 + 0.4 * confirm_frac           # Near neutral
    
    bounds = REGION_BOUNDS[region]
    return max(bounds[0], min(bounds[1], M))
```

### 4.4 Region Bounds

| Region | Lower | Upper | Rationale |
|---|---|---|---|
| NE | 0.50 | 1.25 | Tight coupling, strong range |
| SE | 0.60 | 1.15 | Weak coupling, narrow range |
| SC | 0.50 | 1.30 | Synoptic coupling, full range |
| MW | 0.50 | 1.30 | Continental, full range |
| RW | 0.70 | 1.10 | Weak baseline, conservative |
| PAC | 0.60 | 1.20 | Maritime, moderate |

### 4.5 Isolated Stations

MW, RW, PAC have deliberately low thresholds to avoid penalizing isolated stations. For PAC (1,000+ km apart, L=400 km), inter-station weights are naturally ~0.05-0.15 — gate should rarely fire strongly, yielding near-neutral modulation for most PAC trades.

---

## 5. Coastal vs Inland Station Handling

### 5.1 Coastal Station Identification

| Station | Coast dist | Coast type | Marine influence |
|---|---|---|---|
| KSEA | 15 km | Puget Sound | Strong |
| KSFO | 5 km | Pacific | Very strong |
| KLAX | 20 km | Pacific | Strong |
| KBOS | 8 km | Atlantic | Strong |
| KNYC | 12 km | Atlantic | Moderate |
| KPHL | 130 km | Delaware R. | Weak |
| KDCA | 180 km | Potomac R. | Weak |
| KMIA | 5 km | Atlantic+Gulf | Very strong |
| KMSY | 30 km | Gulf | Strong |
| KHOU | 50 km | Gulf | Strong |
| Others | 250+ km | None | None |

### 5.2 Marine Layer Handling

Coastal stations (especially PAC) have sharp local gradients from marine layer/fog that do NOT reflect synoptic patterns. **Three mitigations:**

1. **Reduce PAC decorrelation length** to 300 km (from 400 km) — narrows spatial window
2. **Marine layer flag:** if active, reduce confidence in spatial coherence for that station
3. **Sea breeze exclusion:** if wind direction shifts offshore→onshore in last 6 hours, set modulation to 1.0 (neutral) — sea breeze creates non-synoptic gradients

### 5.3 Sea Breeze Detection

```python
def detect_sea_breeze(station, metars):
    if station not in COASTAL_STATIONS:
        return False
    recent_winds = metars[-6:]  # Last 3 hours
    if len(recent_winds) < 3:
        return False
    onshore = get_onshore_direction(station)
    offshore = [(d + 180) % 360 for d in onshore]
    prev_dirs = [w['wind_direction'] for w in recent_winds[:-2] if w['wind_direction']]
    curr_dirs = [w['wind_direction'] for w in recent_winds[-2:] if w['wind_direction']]
    if not prev_dirs or not curr_dirs:
        return False
    return any(d in offshore for d in prev_dirs) and any(d in onshore for d in curr_dirs)
```

### 5.4 Coastal Penalty Map

```python
COASTAL_PENALTY = {
    'inland': {'coastal': 0.5},   # Inland target: coastal neighbors half weight
    'coastal': {'coastal': 0.8, 'inland': 1.0},  # Coastal target: near-full for others
}
```

Prevents KATL from being penalized because KMIA (tropical coastal) shows a different pattern.

### 5.5 Upstream/Downstream for Coastal Stations

- **Onshore flow:** PAC stations have limited confirming power from each other
- **Offshore flow:** Continental air pushes to coast — inland stations become informative

```python
def is_upstream(target, candidate, wind_direction):
    bearing = compute_bearing(target['lat'], target['lon'],
                              candidate['lat'], candidate['lon'])
    angle_diff = abs(bearing - wind_direction)
    if angle_diff > 180: angle_diff = 360 - angle_diff
    return angle_diff <= 90  # Candidate is upstream of target
```

Upstream: no penalty. Downstream: 0.5× weight reduction.

---

## 6. Prevailing Wind Direction and Upstream Weighting

### 6.1 Prevailing Wind Computation

Vector-average wind direction over the last 6 hours:

```python
def compute_prevailing_wind(station, metars):
    recent = metars[-12:]  # 6 hours at 30-min intervals
    u_sum = v_sum = 0.0
    count = 0
    for obs in recent:
        if obs['wind_direction_deg'] is None or obs['wind_speed_kt'] is None:
            continue
        wd = math.radians(obs['wind_direction_deg'])
        ws = obs['wind_speed_kt']
        u_sum += ws * math.sin(wd)
        v_sum += ws * math.cos(wd)
        count += 1
    if count < 3:
        return None
    u_avg, v_avg = u_sum/count, v_sum/count
    if abs(u_avg) < 0.1 and abs(v_avg) < 0.1:
        return None  # Calm
    return math.degrees(math.atan2(u_avg, v_avg)) % 360
```

### 6.2 Upstream Weighting

```python
def compute_upstream_weight(target, candidate, wind_direction):
    """Returns factor in [0.25, 1.0] applied to spatial weight."""
    if wind_direction is None:
        return 1.0
    # Wind direction = FROM. Convert to direction GOING TO:
    wind_going_to = (wind_direction + 180) % 360
    bearing_to_target = compute_bearing(candidate['lat'], candidate['lon'],
                                        target['lat'], target['lon'])
    angle_diff = abs(wind_going_to - bearing_to_target)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff
    return math.exp(-angle_diff**2 / (2 * 60**2))
```

### 6.3 Example: Cold Air Advection from Canada

KMDW (Chicago) with NNW wind 340°. Wind is going TO 160° (SSE). Bearing from KMSP to KMDW is ~155° (SSE). Angle difference: |160-155| = 5°. KMSP is strongly upstream → upstream weight ≈ 0.997.

### 6.4 Final Composite Weight

```python
final_weight = w_distance * w_angular * w_altitude * w_coastal * w_upstream
```

---

## 7. Implementation Plan

### 7.1 Phase 1: Core Spatial Coherence Engine (4 days)

**Day 1: Station Metadata and Distance Matrix**
- Build `core/signals/spatial_gate/station_metadata.py`
  - Hard-coded station coordinates, elevation, region assignments
  - Pre-compute 20×20 great-circle distance matrix
  - Pre-compute inter-station bearing matrix (for angular weighting)
  - Load station climatology from `station_climatology` table

**Day 2: Distance Weighting Module**
- Build `core/signals/spatial_gate/distance_weights.py`
  - Gaussian distance decay with seasonal factors
  - Angular enhancement relative to prevailing wind direction
  - Altitude penalty
  - Coastal penalty map
  - Upstream/downstream asymmetry

**Day 3: Confirmation Logic**
- Build `core/signals/spatial_gate/confirmation_logic.py`
  - Anomaly computation (forecast vs climatology)
  - Direction matching (cooler/warmer/neutral)
  - Weighted confirmation counting
  - Threshold evaluation per region
  - Modulation factor computation

**Day 4: Pipeline Integration**
- Build `core/signals/spatial_gate/spatial_coherence_gate.py`
  - `SpatialCoherenceGate` class with `process_batch()` method
  - Takes: forecasts dict, climatology dict, directions dict, convictions dict
  - Returns: adjusted convictions dict
  - Integration hook in `core/p3_scheduler.py` between LLOP fusion and trade generation
  - Unit tests for all modules

### 7.2 Phase 2: Backtest and Tuning (3 days)

- Historical backtest on 2 years of data
- Tune per-region decorrelation lengths
- Tune seasonal factors
- Validate coastal penalty coefficients
- Measure: trade count reduction, accuracy change, Sharpe change
- Compare against baseline (no spatial gate)

### 7.3 Phase 3: Production Deployment (2 days)

- Monitoring dashboards for spatial coherence score distribution
- Per-station coherence time series
- Alerting when coherence drops below 0.3 for high-conviction trades
- Paper trade validation (minimum 2 weeks)
- Gradual rollout: 0.5× scaling → full

---

## 8. Expected Accuracy Improvement

### 8.1 Estimated Impact on Directional Accuracy

| Scenario | Current accuracy | With gate | Improvement |
|---|---|---|---|
| All trades, all stations | ~57% | ~58-59% | +1-2 pp |
| Trades with strong spatial coherence (Phi ≥ 0.8) | ~57% | ~62-64% | +5-7 pp |
| Trades with low spatial coherence (Phi ≤ 0.3) | ~57% | ~48-52% (blocked/reduced) | N/A |
| Trades in SC region (best clustered) | ~56% | ~60% | +4 pp |
| Trades in PAC region (weakest coherence) | ~58% | ~58% | ~0 pp |

### 8.2 Estimated Impact on Sharpe Ratio

| Scenario | Current Sharpe | With gate | Improvement |
|---|---|---|---|
| All trades | ~0.9 | ~1.0 | +0.1 |
| SC region trades | ~0.85 | ~1.0 | +0.15 |
| NE region trades | ~0.9 | ~1.05 | +0.15 |
| MW region trades | ~0.95 | ~1.05 | +0.10 |
| PAC region trades | ~0.85 | ~0.85 | ~0 |

PAC region sees minimal improvement because stations are too far apart for meaningful spatial coherence.

### 8.3 Trade Suppression Rate

| Region | Trades suppressed | Accuracy of suppressed trades | Net benefit |
|---|---|---|---|
| NE | ~10-15% | ~45-48% (would be losers) | + |
| SE | ~5-10% | ~48-50% | + |
| SC | ~10-15% | ~42-47% | ++ |
| MW | ~10-15% | ~45-48% | + |
| RW | ~2-5% | ~50-52% | ~ |
| PAC | ~1-3% | ~50-52% | ~ |

---

## 9. Risk Factors

### 9.1 Climatology Mismatch Risk

**Risk:** Station climatology normals are derived from limited data (2 years vs the standard 30-year normal). Anomaly-based coherence is only as good as the climatology baseline.

**Mitigation:** Use the existing `station_climatology` table (which has 5+ years) plus ERA5 gridded climatology as a backstop. Monitor for systematic drift in anomaly computation.

### 9.2 Seasonal Overfitting Risk

**Risk:** Decorrelation lengths tuned on 2 years of data may not generalize. Summer L values may be too long or too short for future summers.

**Mitigation:** Walk-forward validation (18-month train, 6-month test). Seasonal factors are small (±15%) so overfitting impact is limited.

### 9.3 Coastline Boundary Risk

**Risk:** The coastal/inland distinction is coarse. KBOS (coastal) and KPHL (inland-ish, 130 km from coast) may sometimes behave similarly and other times differently depending on synoptic setup.

**Mitigation:** The coastal classification is a continuum, not binary. Stations like KPHL and KDCA get intermediate treatment — they are not penalized as heavily as pure coastal stations (KMIA, KSFO) but receive some adjustment.

### 9.4 Data Latency Risk

**Risk:** Spatial coherence requires forecasts for all 20 stations. If one station's data is delayed, the entire batch process is delayed.

**Mitigation:** Process with available stations only. Missing stations are excluded from both targets and neighbors. The coherence computation adapts automatically — fewer neighbors means lower weighted counts, which the modulation function handles.

### 9.5 Model Divergence Risk

**Risk:** The GEFS ensemble may produce forecasts that are internally consistent (low spread) but wrong (e.g., missing a cutoff low). Spatial coherence would find high agreement among wrong forecasts, falsely boosting confidence.

**Mitigation:** Spatial coherence only measures inter-station agreement, not absolute accuracy. It cannot catch model-wide systematic errors. The trajectory confirmation gate is better suited for this (by comparing against historical analogs). Spatial and trajectory gates are multiplicative, so a model-wide error caught only by trajectory will still be penalized.

---

## Appendix A: Implementation Checklist

- [ ] Station metadata with coordinates, elevations, region assignments
- [ ] Pre-computed 20×20 great-circle distance matrix
- [ ] Pre-computed 20×20 bearing matrix
- [ ] Seasonal decorrelation length factors
- [ ] Wind direction vector averaging (prevailing wind)
- [ ] Gaussian distance decay with L(temperature, region, season)
- [ ] Angular enhancement (upwind/crosswind/downwind)
- [ ] Altitude penalty function
- [ ] Coastal penalty map
- [ ] Sea breeze detection
- [ ] Upstream weighting function
- [ ] Anomaly computation (forecast vs climatology)
- [ ] Direction classification (cooler/warmer/neutral)
- [ ] Weighted confirmation counting
- [ ] Region-specific threshold evaluation
- [ ] Modulation factor computation with bounds
- [ ] Batch processing pipeline
- [ ] Unit tests (all modules)
- [ ] Integration test (full pipeline with spatial gate)
- [ ] Backtest comparison (with/without gate)
- [ ] Monitoring dashboard
- [ ] Alert thresholds

*End of design specification. Ready for C-Suite review.*