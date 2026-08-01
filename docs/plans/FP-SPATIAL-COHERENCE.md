# First-Principles Spatial Coherence Gate
## Weather Trading Ensemble — Design Specification

**Date:** 2026-08-01
**Author:** First-Principles Spatial Statistics Expert (subagent)
**Status:** Design specification — requires C-Suite review before implementation
**Prerequisite reading:** `CONFIDENCE-AGREEMENT-SPEC-2026-07-06.md`, `FORECAST-AGGREGATION-INTEGRATION-DESIGN.md`
**Purpose:** Add a spatial coherence layer to the trading pipeline — when a city's prediction disagrees with its regional neighbors, reduce confidence. When the entire region agrees, boost confidence.

---

## Table of Contents
1. [Motivation — Spatial First Principles](#1-motivation)
2. [Regional Clustering Strategy](#2-regional-clustering-strategy)
3. [Consensus Computation](#3-consensus-computation)
4. [Confidence Modulation](#4-confidence-modulation)
5. [Integration Point in Pipeline](#5-integration-point)
6. [Validation Protocol](#6-validation-protocol)
7. [Edge Cases](#7-edge-cases)
8. [Implementation Notes](#8-implementation-notes)

---

## 1. Motivation

### 1.1 The Underlying Physics

Temperature is not independently drawn. The atmosphere has a finite decorrelation length — typically **500-800 km in the midlatitudes** for synoptic-scale temperature anomalies, and **200-400 km** for the mesoscale frontal features that drive day-to-day market movement.

This means: if Chicago (KMDW) is predicted to drop 6°F tomorrow, Detroit (KDTW, ~370 km away) should show a similar magnitude drop within a ~2-4 hour lag. If Detroit's prediction says "no change," one of these predictions is likely wrong.

The ensemble members already capture flow-dependent spatial correlations through their dynamical cores. But the **signal fusion layer** treats each station as independent. This spatial gate adds the spatial constraint explicitly — a Bayesian prior that temperature fields are smooth.

### 1.2 What the Existing System Misses

| Layer | What it checks | Spatial awareness |
|---|---|---|
| LLOP fusion | Signal agreement within GEFS+ECMWF for *one station* | None per-station |
| SAS (Signal Agreement Score) | Within-ensemble directional agreement | None |
| Forecast Aggregation | Cross-model (GFS/ECMWF/ICON/GEM) agreement | None |
| Conviction Score | Edge magnitude × agreement × calibration × sample size | None |

Every existing gate is **per-station independent**. This gate adds **cross-station spatial coherence** — the first gate in the pipeline that knows Chicago and Detroit are neighbors.

### 1.3 When Spatial Coherence Matters Most

| Scenario | Per-station signal | Spatial coherence | Interpretation |
|---|---|---|---|
| Synoptic cold front | Strong cold signal at KMDW, KORD, KCLE | High | High confidence — consistent feature |
| Isolated outlier | Strong cold at KMDW, neutral at KORD, KCLE | Low | Likely initialization error or model noise |
| Coastal vs inland divergence | Strong cold at KSFO, neutral at KLAX, KPHX | Inconclusive (coastal) | Maritime influence — see edge cases §7 |
| Mountain shadow | Strong cold at KDEN, neutral at KOKC | Expected low (lee side) | Topographic decoupling — see edge cases §7 |
| Summer convection | Scattered pop-up storms, 95°F everywhere | Moderate spatial but low temporal | Lacks synoptic organization — reduce conviction |

---

## 2. Regional Clustering Strategy

### 2.1 Method: Distance-Weighted Hybrid Clustering

Pure geographic clustering is insufficient — climate zones, topography, and prevailing wind patterns all matter. Pure correlation clustering is too data-hungry and regime-dependent.

**Approach:** Multi-criteria clustering using:
1. **Great-circle distance** (primary — 60% weight in distance computation)
2. **Climate zone similarity** (secondary — 25% weight) based on Köppen classification
3. **Topographic regime** (tertiary — 15% weight) — coastal vs inland, elevation, mountain barrier

Each city gets a **fuzzy membership** in all regions through a distance-decay weight, not a hard assignment.

### 2.2 Region Definitions

Using the station list, I define **6 primary regions** plus a **2 special-treatment zones** for coastal stations:

#### Region A: Northeast Corridor (NE)
| Station | City | Lat | Lon | Distance matrix (km pairs) |
|---|---|---|---|---|
| KNYC | New York | 40.7 | -74.0 | NYC-BOS: 306, NYC-PHL: 152, NYC-DCA: 365 |
| KBOS | Boston | 42.4 | -71.0 | BOS-PHL: 488, BOS-DCA: 643 |
| KPHL | Philadelphia | 39.9 | -75.2 | PHL-DCA: 213 |
| KDCA | Washington DC | 38.9 | -77.0 | DCA-PHL: 213 |
| **Climate:** Humid subtropical (Cfa) / Oceanic (Cfb) for Boston | | | | |
| **Max diameter:** ~640 km — strong synoptic coupling, winter nor'easters | | | | |
| **Special:** Tightly coupled corridor. Sea-breeze effects at coastal stations (KNYC, KBOS) but small relative to synoptic scale. | | | | |

#### Region B: Southeast (SE)
| Station | City | Lat | Lon |
|---|---|---|---|
| KATL | Atlanta | 33.6 | -84.4 |
| KMIA | Miami | 25.8 | -80.3 |
| KMSY | New Orleans | 30.0 | -90.3 |
| **Climate:** Humid subtropical (Cfa) — Miami is tropical monsoon (Am) |
| **Max diameter:** ~1,400 km (ATL-MIA ~960 km, MIA-MSY ~1,100 km) |
| **Special:** Weakest internal coherence. MIA is tropical, ATL is continental-influenced subtropical. The Florida peninsula creates east-west decoupling. **Use inverse-distance weighting heavily here.** |

#### Region C: South Central / Gulf (SC)
| Station | City | Lat | Lon |
|---|---|---|---|
| KHOU | Houston | 29.6 | -95.3 |
| KDFW | Dallas | 32.9 | -97.0 |
| KAUS | Austin | 30.2 | -97.7 |
| KSAT | San Antonio | 29.5 | -98.5 |
| KOKC | Oklahoma City | 35.4 | -97.6 |
| **Climate:** Humid subtropical (Cfa) / Temperate (Cfa) |
| **Max diameter:** ~700 km (OKC-HOU ~750 km, DFW-SAT ~480 km) |
| **Special:** Tightly coupled. Gulf moisture gradient from east to west. Spring severe weather creates sharp gradients — spatial coherence will drop during convective regimes (correctly). |

#### Region D: Midwest / Great Lakes (MW)
| Station | City | Lat | Lon |
|---|---|---|---|
| KMDW | Chicago | 41.8 | -87.7 |
| KMSP | Minneapolis | 44.9 | -93.2 |
| **Climate:** Humid continental (Dfa) |
| **Max diameter:** ~600 km |
| **Special:** Only 2 stations but tightly coupled. Lake Michigan moderates Chicago slightly. Strong cold air damming scenarios. Consider adding KCLE or KDTW if ever expanded. |

#### Region E: Mountain West (MW) — note: different from Midwest abbreviation, use RW (Rocky West)
| Station | City | Lat | Lon |
|---|---|---|---|
| KDEN | Denver | 39.8 | -104.7 |
| KPHX | Phoenix | 33.4 | -112.0 |
| KLAS | Las Vegas | 36.1 | -115.2 |
| KOKC | Oklahoma City | 35.4 | -97.6 | (shared with SC — overlapping boundary) |
| **Climate:** Semi-arid (BSk) / Hot desert (BWh) / Humid subtropical for OKC |
| **Max diameter:** ~1,500 km (DEN-PHX ~920 km, PHX-LAS ~420 km) |
| **Special:** Weakest coherence of any region. Mountain barrier between DEN and the desert southwest. Lee-side effects, orographic precipitation, elevation difference of ~1,600m between DEN and PHX. **Low baseline expected — use conservative modulation.** |

#### Region F: Pacific Coast (PAC)
| Station | City | Lat | Lon |
|---|---|---|---|
| KSEA | Seattle | 47.4 | -122.3 |
| KSFO | San Francisco | 37.6 | -122.4 |
| KLAX | Los Angeles | 33.9 | -118.4 |
| **Climate:** Mediterranean (Csb / Csa) / Oceanic (Cfb for Seattle) |
| **Max diameter:** ~1,500 km (SEA-LAX ~1,530 km, SEA-SFO ~1,090 km) |
| **Special:** Strong maritime influence. Coast range + Sierra Nevada block inland coupling. Marine layer, fog, coastal eddies are sub-grid for GEFS/ECMWF. **Different coherence rules needed — see §7.2.** |

### 2.3 Overlap Strategy: Soft Membership

Hard region boundaries create discontinuities at the border. Solution: **distance-weighted soft membership**.

For each city `s` and region `r`:
```
w(s, r) = exp(-d(s, r_centroid)^2 / (2 * sigma_r^2))
```

Where:
- `d(s, r_centroid)` = great-circle distance from city `s` to the geometric centroid of region `r`
- `sigma_r` = characteristic correlation length for region `r` (default = 400 km, adjusted per region)

But this misses the point — consensus should weight by **pairwise distance to other stations in the same region**, not distance to a centroid.

**Better approach — pairwise distance weighting:**

For a target city `s`, its spatial consensus uses all stations `j` in its home region:
```
w(j | s) = exp(-d(s, j)^2 / (2 * L^2))
```

Where:
- `d(s, j)` = great-circle distance between station `s` and station `j`
- `L` = characteristic decorrelation length (default 400 km, tunable per region)

This means:
- A station 0 km away (itself): weight = 1.0
- A station 200 km away: weight = exp(-200²/2×400²) ≈ 0.88
- A station 600 km away: weight = exp(-600²/2×400²) ≈ 0.32
- A station 1,000 km away: weight = exp(-1000²/2×400²) ≈ 0.04

**Overlap zones:** A city near a region boundary (e.g., KOKC at the SC/RW boundary) gets a secondary region membership. Its spatial coherence is computed as a **weighted blend** of both regions' consensuses:
```
w_OKC → SC = 0.65 (primary — closer to DFW and climate alignment)
w_OKC → RW = 0.35 (secondary — western influence)
```

The blending coefficients are pre-computed from relative distances to regional station clusters.

### 2.4 Region Correlation Lengths — Region-Specific Tuning

| Region | L (km) | Rationale |
|---|---|---|
| NE | 300 | Tight corridor, nor'easters have sharp gradients |
| SE | 500 | Weakly coupled, large distances — longer tail |
| SC | 350 | Gulf moisture creates moderate gradients |
| MW | 350 | Continental, moderate gradients |
| RW | 600 | Weak coupling — longer tail to avoid spurious penalties |
| PAC | 400 | Maritime influence — moderate (see §7.2 for coastal adjustment) |

---

## 3. Consensus Computation

### 3.1 What We Mean by "Consensus"

Consensus is NOT agreement on absolute temperature. Temperature varies with latitude, elevation, and distance. A station might be at 80°F while another is at 70°F — that's climatologically expected and tells us nothing about spatial coherence.

**We want directional consensus on the forecast anomaly relative to climatology, expressed as a signed magnitude:**

```
anomaly(s, t) = forecast_temp(s, t) - climo_mean(s, t_0)
```

Where `climo_mean(s, t_0)` is the 1991-2020 climatological normal for station `s` at time `t_0` (same time of year, 00Z initialization).

### 3.2 Regional Consensus Vector

For a region `R` containing stations `{s_1, s_2, ..., s_n}`:

**Step 1: Compute anomaly for each station**
```
a_i = T_forecast(s_i) - T_climo(s_i)
```

**Step 2: Compute inverse-distance weights**
```
w_{ij} = exp(-d(s_i, s_j)^2 / (2 * L_R^2))
```
where `j` is the target station being evaluated and `i` ranges over all other stations in region `R`.

**Step 3: Compute region-weighted consensus anomaly for target station j**
```
C_j = ( Σ_{i ≠ j} w_{ij} * a_i ) / ( Σ_{i ≠ j} w_{ij} )
```

This gives the **spatially expected anomaly** at station `j` based on its neighbors.

**Step 4: Compute anomaly difference**
```
Δ_j = |a_j - C_j|
```

This is the core coherence metric — how much does station `j`'s forecast deviate from what its neighbors predict it should be.

### 3.3 Directional Consensus (Secondary)

The anomaly-based consensus handles magnitude. For directional trading (UP/DOWN vs settlement), we also compute:

```
direction_agreement_j = fraction of neighbors whose forecast direction (up/down vs settlement) matches station j's direction
```

But bounded by distance:
```
dir_agreement_j = Σ_{i ≠ j} ( w_{ij} * I( dir_i == dir_j ) ) / Σ_{i ≠ j} w_{ij }
```

Where `I()` is the indicator function (1 if match, 0 if not).

### 3.4 Blended Coherence Score

Combine magnitude and directional coherence:
```
Phi_j = α * (1 - tanh( Δ_j / Δ_0 )) + β * dir_agreement_j
```

Where:
- `Δ_0` = characteristic anomaly threshold (default 3°C, tunable)
- `α = 0.6`, `β = 0.4` (directional agreement gets lower weight — magnitude is more informative)
- `tanh()` squashes large anomaly differences to near-zero coherence without creating hard cutoffs
- `Phi_j ∈ [0, 1]`

**Interpretation of Phi_j:**
- 0.8-1.0: Strong spatial coherence (neighbors agree in magnitude AND direction)
- 0.5-0.8: Moderate coherence (some disagreement)
- 0.2-0.5: Weak coherence (station is an outlier)
- 0.0-0.2: Incoherent (complete disagreement — strong red flag)

### 3.5 Numerical Example

**Scenario:** Evaluating KDFW (Dallas) in the SC region.

| Station | T_forecast | T_climo | Anomaly | Distance from DFW (km) | Weight |
|---|---|---|---|---|---|
| KDFW (self) | 102°F | 95°F | +7°F | 0 | N/A |
| KHOU | 95°F | 92°F | +3°F | 370 | 0.60 |
| KAUS | 100°F | 94°F | +6°F | 300 | 0.72 |
| KSAT | 99°F | 93°F | +6°F | 440 | 0.47 |
| KOKC | 98°F | 93°F | +5°F | 320 | 0.69 |

(L = 350 km — Midwest region)

**Consensus anomaly for KDFW:**
```
C = (0.60 * 3 + 0.72 * 6 + 0.47 * 6 + 0.69 * 5) / (0.60 + 0.72 + 0.47 + 0.69)
  = (1.8 + 4.32 + 2.82 + 3.45) / 2.48
  = 12.39 / 2.48
  = 5.0°F
```

**Anomaly difference for KDFW:**
```
Δ = |7 - 5.0| = 2.0°F
```

**Directional consensus (assuming all forecast UP vs settlement):**
```
dir_agreement = 4/4 = 1.0
```

**Blended coherence:**
```
Phi = 0.6 * (1 - tanh(2.0 / 3.0)) + 0.4 * 1.0
    = 0.6 * (1 - 0.581) + 0.4
    = 0.6 * 0.419 + 0.4
    = 0.251 + 0.4
    = 0.651
```

Delta = 2°F gives a coherence of ~0.65 — moderate. The +7°F anomaly at KDFW is warmer than its neighbors (consensus +5°F) but the direction is consistent. This would **slightly reduce** conviction but not block the trade.

---

## 4. Confidence Modulation

### 4.1 Design Principle: Continuous Modulation, Not Binary Gates

A binary pass/fail gate creates hard edges — a trade passes at Phi = 0.51 and fails at Phi = 0.49. This is statistically unsound. Instead, we apply **continuous modulation** as a multiplier on the existing conviction score.

### 4.2 Modulation Function

```
conviction_adjusted = conviction_base * M(Phi)
```

Where `M(Phi)` is the modulation factor:

**Conservative modulation (recommended — protects against over-confidence):**
```
M(Phi) = 0.5 + 0.8 * Phi
```

| Phi | M(Phi) | Effect |
|---|---|---|
| 1.0 | 1.30 | +30% conviction (strong regional agreement) |
| 0.8 | 1.14 | +14% |
| 0.65 | 1.02 | ~neutral |
| 0.5 | 0.90 | -10% |
| 0.3 | 0.74 | -26% |
| 0.1 | 0.58 | -42% |
| 0.0 | 0.50 | -50% floor |

**Rationale for caps:**
- **Upper cap at 1.3×:** Spatial coherence alone should not double conviction. The existing SAS, LLOP, calibration, and sample size scores already capture signal quality. Spatial coherence is a secondary check — boost to 1.3× max.
- **Lower cap at 0.5×:** Even with zero spatial coherence, the raw signal may still be correct (e.g., a true localized extreme). Cutting below 50% of base conviction would suppress legitimate trades.

### 4.3 Alternative: Asymmetric Modulation (More Aggressive)

```
M_asymmetric(Phi) = 
    0.5 + 0.8 * Phi          (when Phi >= 0.5)
    0.8 * Phi^0.25           (when Phi < 0.5)
```

This penalizes low-coherence signals more sharply while keeping the same boost range. **Not recommended as default** — it can over-penalize genuine localized weather.

### 4.4 Regional Modulation Bounds

Different regions have different baselines for expected coherence:

| Region | Lower bound on M | Upper bound on M | Rationale |
|---|---|---|---|
| NE | 0.5 | 1.25 | Tightly coupled, but nor'easter gradients |
| SE | 0.6 | 1.15 | Weakly coupled — less boost, less penalty |
| SC | 0.5 | 1.30 | Gulf region, synoptically coupled |
| MW | 0.5 | 1.30 | Continental coupling, strong |
| RW | 0.7 | 1.10 | Weak baseline — less boost, floor higher to avoid noise |
| PAC | 0.6 | 1.20 | Maritime — moderate, see §7.2 |

### 4.5 Interaction with Existing Conviction Score

The spatial coherence modulation applies **after** the existing conviction score computation:

**Pipeline order:**
1. LLOP fusion → LLOP probability
2. Calibration quality (ECE) → Cal factor
3. Sample size factor → Sample factor
4. Signal Agreement Score (SAS) → Agreement factor
5. Conviction = LLOP_offset × Agreement × Cal × Sample  *(existing)*
6. **NEW: Spatial Coherence → M(Phi)**
7. Conviction_adjusted = Conviction × M(Phi)
8. Gate comparison against thresholds

**No changes to existing conviction computation.** Spatial coherence is a multiplicative overlay.

### 4.6 Complete Worked Example

**Trade: KMDW (Chicago), January cold front, 00Z forecast**

| Component | Value | Source |
|---|---|---|
| LLOP fused P(up) | 0.28 | LLOP calculation |
| LLOP offset | 0.22 | \|0.28 - 0.50\| |
| SAS | 0.85 | Strong signal agreement at KMDW |
| Calibration quality | 0.92 | Recent ECE = 0.08 |
| Sample size factor | 0.95 | tanh(60/50) |

**Base conviction:**
```
Conviction_base = 0.22 * 0.85 * 0.92 * 0.95 = 0.163
```

**Spatial coherence check (MW region):**
- KMDW forecast: -8°F anomaly vs climo, strong DOWN
- KMSP forecast: -6°F anomaly, DOWN direction → agrees
- Distance: KMDW-KMSP = 560 km, L=350 km
- Weight: exp(-560²/2×350²) = exp(-1.28) = 0.28
- Consensus anomaly: -6.1°F (MSP weighted 0.28 → C = (-8*0 + -6*0.28)/0.28 = -6.0°F)  ... wait let me recalculate properly.

Actually KMSP is the only other station in MW region. So:

a_KMDW = -8°F, a_KMSP = -6°F
w(KMDW→KMSP) = exp(-560²/2×350²) = exp(-1.28) = 0.28

C_KMDW = (0.28 * -6.0) / 0.28 = -6.0°F
Δ_KMDW = |(-8) - (-6.0)| = 2.0°F

direction: both DOWN → dir_agreement = 1.0

Phi = 0.6 * (1 - tanh(2.0/3.0)) + 0.4 * 1.0
    = 0.6 * 0.419 + 0.4
    = 0.651

M(Phi) = 0.5 + 0.8 * 0.651 = 1.02

**Adjusted conviction:**
```
Conviction_adjusted = 0.163 * 1.02 = 0.166
```

Net effect: essentially neutral. The 2°F difference between KMDW and KMSP is within expected decorrelation. The direction is consistent. The spatial check confirms for this trade.

**Now the contrarian: Suppose KMDW says -8°F but KMSP says +2°F.**
C_KMDW = (0.28 * +2.0) / 0.28 = +2.0°F
Δ_KMDW = |-8 - 2| = 10°F

Phi = 0.6 * (1 - tanh(10/3)) + 0.4 * 0.0
    = 0.6 * (1.0 - 1.0) + 0.0  [tanh(3.33) ≈ 0.997]
    = 0.0

M(Phi) = 0.5

Conviction_adjusted = 0.163 * 0.5 = 0.082 (halved — correctly flagged as incoherent)

---

## 5. Integration Point

### 5.1 Where in the Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                     FORECAST INGESTION                              │
│  GEFS 31 + ECMWF 51 + (GFS|ECMWF|ICON|GEM stub)                    │
├─────────────────────────────────────────────────────────────────────┤
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              PER-STATION PROCESSING                         │   │
│  │  For each of 20 stations independently:                    │   │
│  │  1. Extract ensemble members → distribution                 │   │
│  │  2. LLOP fusion → P_up / P_down                            │   │
│  │  3. Calibration correction                                 │   │
│  │  4. Signal Agreement Score (SAS)                           │   │
│  │  5. Conviction = offset × SAS × cal × sample               │   │
│  │  6. Generate trade signal (direction + magnitude)          │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │   ● SPATIAL COHERENCE GATE (NEW) ●                         │   │
│  │   Input: All 20 station forecasts + conviction scores       │   │
│  │   Process:                                                  │   │
│  │   1. Compute anomaly for each station vs climo              │   │
│  │   2. Pairwise distances → weights (per region L)           │   │
│  │   3. Regional consensus anomaly for each station            │   │
│  │   4. Blended coherence Phi_j for each station               │   │
│  │   5. Modulation M(Phi_j) → conviction multiplier            │   │
│  │   6. Apply to each station's conviction                     │   │
│  │   Output: Conviction_adjusted per station                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              TRADE GENERATION                               │   │
│  │  For each station where conviction_adjusted ≥ threshold:    │   │
│  │  - Direction: from LLOP (up/down)                          │   │
│  │  - Sizing: proportional to conviction_adjusted              │   │
│  │  - Expiry mapping: Kalshi event matching                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

The gate operates as a **batch process across all 20 stations** after per-station LLOP/signal processing, before trade generation. This is the correct insertion point — spatial coherence needs all station forecasts computed first.

### 5.2 Code Architecture

```python
class SpatialCoherenceGate:
    """
    Computes spatial coherence modulation for 20-station temperature market ensemble.
    
    Integrates at pipeline layer 6 (between conviction and trade generation).
    """
    
    def __init__(self, config_path: str = None):
        # Region definitions: station_id -> primary_region, secondary_region, blending_weights
        self.regions = {
            'NE': {
                'stations': ['KNYC', 'KBOS', 'KPHL', 'KDCA'],
                'L': 300,  # decorrelation length (km)
                'bounds': (0.5, 1.25),
            },
            'SE': {
                'stations': ['KATL', 'KMIA', 'KMSY'],
                'L': 500,
                'bounds': (0.6, 1.15),
            },
            'SC': {
                'stations': ['KHOU', 'KDFW', 'KAUS', 'KSAT', 'KOKC'],
                'L': 350,
                'bounds': (0.5, 1.30),
            },
            'MW': {
                'stations': ['KMDW', 'KMSP'],
                'L': 350,
                'bounds': (0.5, 1.30),
            },
            'RW': {
                'stations': ['KDEN', 'KPHX', 'KLAS', 'KOKC'],
                'L': 600,
                'bounds': (0.7, 1.10),
            },
            'PAC': {
                'stations': ['KSEA', 'KSFO', 'KLAX'],
                'L': 400,
                'bounds': (0.6, 1.20),
            },
        }
        
        # Overlap blending: station -> {region: weight, ...}
        self.overlap_stations = {
            'KOKC': {'SC': 0.65, 'RW': 0.35},
        }
        
        # Climatological normals: station -> T_climo(lat, lon, doy)
        self.climo_normals = {}  # populated from external data source
        
        # Station lat/lon
        self.station_coords = {
            'KNYC': (40.7, -74.0),
            'KBOS': (42.4, -71.0),
            # ... (all 20)
        }
        
        self.alpha = 0.6   # magnitude weight in Phi
        self.beta = 0.4    # directional agreement weight
        self.delta_0 = 3.0  # characteristic anomaly threshold (°C)
    
    def great_circle_distance(self, lat1, lon1, lat2, lon2) -> float:
        """Returns distance in km using haversine formula."""
        # standard implementation
    
    def compute_weights(self, lat_j: float, lon_j: float, region: str) -> Dict[str, float]:
        """Compute inverse-distance weights from station j to all others in region."""
        L = self.regions[region]['L']
        weights = {}
        for s_i, (lat_i, lon_i) in self.region_stations.items():
            if s_i == station_j:
                continue
            d = self.great_circle_distance(lat_j, lon_j, lat_i, lon_i)
            weights[s_i] = exp(-d**2 / (2 * L**2))
        return weights
    
    def compute_coherence(self, station_id: str, forecasts: Dict[str, float],
                          climatology: Dict[str, float],
                          directions: Dict[str, str]) -> float:
        """
        Compute spatial coherence Phi for one station.
        
        Args:
            station_id: ICAO station code
            forecasts: {station: forecast_temp} for all 20 stations
            climatology: {station: climo_temp} for all 20 stations
            directions: {station: 'up'|'down'} vs settlement
            
        Returns:
            Phi ∈ [0, 1] — spatial coherence score
        """
        primary_region, primary_weight, secondary_region, secondary_weight = \
            self.get_region_membership(station_id)
        
        phi_primary = self._region_coherence(
            station_id, primary_region, forecasts, climatology, directions
        )
        
        if secondary_region and secondary_weight > 0:
            phi_secondary = self._region_coherence(
                station_id, secondary_region, forecasts, climatology, directions
            )
            return primary_weight * phi_primary + secondary_weight * phi_secondary
        
        return phi_primary
    
    def compute_modulation(self, phi: float, region: str) -> float:
        """Compute conviction multiplier M(Phi) bounded by region."""
        lower, upper = self.regions[region]['bounds']
        m = 0.5 + 0.8 * phi
        return max(lower, min(upper, m))
    
    def process_batch(self, forecasts: Dict[str, float],
                      climatology: Dict[str, float],
                      directions: Dict[str, str],
                      convictions: Dict[str, float]) -> Dict[str, float]:
        """
        Apply spatial coherence gate across all 20 stations.
        
        Returns:
            {station: conviction_adjusted}
        """
        # Step 1: Compute coherence for each station
        coherence_scores = {}
        for station_id in forecasts:
            phi = self.compute_coherence(station_id, forecasts, climatology, directions)
            coherence_scores[station_id] = phi
        
        # Step 2: Apply modulation
        result = {}
        for station_id in forecasts:
            region, _, _, _ = self.get_region_membership(station_id)
            phi = coherence_scores[station_id]
            conv = convictions[station_id]
            m = self.compute_modulation(phi, region)
            result[station_id] = conv * m
        
        return result
```

### 5.3 Data Dependencies

| Data | Source | Update cadence | Criticality |
|---|---|---|---|
| Climatological normals | NOAA NCEI 1991-2020 (or ERA5 1991-2020) | Static — annual update | HIGH — misspecified normals break anomaly computation |
| Station coordinates | Static — from table above | Never changes | LOW |
| Decorrelation lengths L | Tuning parameter | Seasonal tuning | MEDIUM |
| Blending weights | Pre-computed from coordinates | Static | LOW |

### 5.4 Performance Considerations

- The gate is O(N²) where N=20 → negligible (400 pairwise computations)
- All operations are numpy-vectorizable
- Adding the gate should add <10μs to a per-station pipeline that currently processes in ~1-5ms
- **No external API calls** — all data is derived from the forecast ingest that already runs

---

## 6. Validation Protocol

### 6.1 A/B Backtest Design

**Experimental setup:**
- **Control (A):** Current pipeline — conviction without spatial coherence gate
- **Treatment (B):** Pipeline + spatial coherence gate applied
- **Holding everything else equal:** Same LLOP, same calibration, same thresholds, same sizing

**Metrics — primary and secondary:**

| Metric | Why | Target |
|---|---|---|
| **Directional accuracy (hit rate)** | Does spatial coherence filter out wrong trades? | Improve ≥1.5pp |
| **Sharpe ratio (realized returns)** | Does filtering improve risk-adjusted returns? | Improve ≥0.1 |
| **Trade count** | Does the gate suppress too many trades? | Reduction ≤20% |
| **Win rate on rejected trades** | Were trades that the gate blocked (nearly blocked) actually losers? | >55% |
| **Average conviction of accepted trades** | Does the gate shift the conviction distribution rightward? | Increase ≥5% |

### 6.2 Backtest Period Requirements

- **Minimum:** 12 months of historical data (2025-07 through 2026-07)
- **