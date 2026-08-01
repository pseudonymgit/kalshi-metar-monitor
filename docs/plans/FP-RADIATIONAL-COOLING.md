# First-Principles Radiational Cooling Detection Signal — LOW Temperature Markets

**Date:** 2026-08-01
**Author:** First-Principles Meteorological Signal Design Expert (subagent)
**Status:** Design specification — requires C-Suite review before implementation
**Prerequisite reading:** `CONFIDENCE-AGREEMENT-SPEC-2026-07-06.md`, `FORECAST-AGGREGATION-INTEGRATION-DESIGN.md`, `docs/plans/FP-SPATIAL-COHERENCE.md`
**Purpose:** Detect nights with strong radiational cooling conditions from METAR observations and produce a bias-corrected LOW temperature estimate that undercuts the ensemble consensus.

---

## Table of Contents
1. [Physical Mechanism](#1-physical-mechanism)
2. [METAR Fields Required](#2-metar-fields-required)
3. [Radiational Cooling Potential Score](#3-radiational-cooling-potential-score)
4. [Expected Temperature Drop by Station](#4-expected-temperature-drop-by-station)
5. [Signal Construction: Standalone vs Bias Correction](#5-signal-construction)
6. [Integration Point in Pipeline](#6-integration-point)
7. [Validation Protocol](#7-validation-protocol)
8. [Edge Cases and Failure Modes](#8-edge-cases)
9. [Implementation Notes](#9-implementation-notes)

---

## 1. Physical Mechanism

### 1.1 Why NWP Models Underestimate Radiational Cooling

Radiational cooling is a **surface-layer-only** phenomenon governed by the surface energy balance:

```
Q_net = SW↓ - SW↑ + LW↓ - LW↑ - SH - LH - G
```

During the night, SW↓ = 0, so the surface cools entirely through net longwave radiation loss:

```
Q_net = LW↓ - LW↑ - SH - LH - G
```

Where:
- **LW↑** = εσT⁴ (surface emits ~390 W/m² at 290K)
- **LW↓** = ε_atm σT⁴ (atmosphere emits back, dependent on cloud cover and water vapor)
- **SH** = sensible heat flux (wind-driven mixing)
- **LH** = latent heat flux
- **G** = ground heat flux

**The NWP model's systematic warm bias on clear, calm nights** arises because:

1. **Grid-scale averaging masks sub-grid cold pooling** — Models represent a 3-13 km grid cell, but radiational cooling creates **patchy cold pockets** at ≤100 m scales (microscale pooling). The LOW observed at a specific METAR site can be 3-10°F colder than the grid-cell mean.

2. **Turbulence parameterization overestimates mixing** — Stable boundary layer (SBL) schemes in NWP models are tuned to avoid catastrophic collapse. They retain residual turbulent mixing even when the observed near-surface wind is dead calm. This residual mixing in the model keeps the surface 2-5°F warmer than reality.

3. **Soil moisture initialization errors** — Wet soil stores more daytime heat and releases it overnight, raising the LOW. Dry soil (common after high-pressure settling) releases heat rapidly. Models often initialize with climatological soil moisture, not current.

4. **Snow albedo feedback** — Fresh snow reflects ~80-90% of incoming solar, reducing daytime heat storage. The subsequent night's cooling starts from a lower base. NWP models under-predict snow albedo enhancement and its spatial heterogeneity.

### 1.2 The Four Trigger Conditions

Radiational cooling requires all four conditions to be met simultaneously:

| Condition | METAR Observable | Physical Rationale |
|---|---|---|
| **Clear skies** | Cloud cover = CLR/FEW, ceiling ≥ 20,000 ft | No cloud deck to absorb outgoing LW and re-radiate it back to surface |
| **Calm winds** | Wind speed < 5 kt sustained (ideally < 3 kt) | No mechanical turbulence to mix warm air from above the inversion |
| **Dry air** | Dewpoint depression (temp - dewpoint) ≥ 15°F, or dewpoint ≤ 25°F | Low water vapor reduces LW↓ (atmospheric counter-radiation); dry air radiates more efficiently |
| **Long night** | Solar elevation angle / time of year | More hours of net radiative loss → deeper cooling; winter solstice delivers ~15 hours of cooling vs ~9 hours at summer solstice |

**Note:** All four must be present. Three of four is not enough. A clear, calm, but saturated night will form fog/dew — the latent heat release from condensation actually *raises* the LOW above the clear-calm-dry case.

---

## 2. METAR Fields Required

### 2.1 Core Fields (from `metar_observations` table)

| Field | Column | Unit | Use | Nullable? |
|---|---|---|---|---|
| Temperature | `temp_f` | °F | Compute dewpoint depression | No |
| Dewpoint | `dewpoint_f` | °F | Compute dewpoint depression, absolute dryness | No |
| Wind speed | `wind_speed_kt` | kt | Detect calm conditions | Yes (gust in knots preferred for sustained assessment) |
| Wind gust | `wind_gust_kt` | kt | Detect gusty interruptions to stable layer | Yes |
| Visibility | `visibility_mi` | mi | Proxy for fog, haze, or obstruction to visibility caused by moisture | Yes |
| Ceiling | `ceiling_ft` | ft | Cloud base height; high/none = favorable | Yes |

### 2.2 Cloud Cover (from `isd_lite_raw` table)

| Field | Source Column | Codes | Use |
|---|---|---|---|
| Sky condition code | `raw_line` (field index 9) | 0=CLR, 2=FEW, 4=SCT, 6=BKN, 8=OVC, 9=MISSING | Compute cloud cover fraction for radiational cooling score |

Already available via `cloud_cover_modulation.py::load_cloud_cover()`.

### 2.3 Derived Fields

| Field | Formula | Use |
|---|---|---|
| Dewpoint depression (DPD) | `temp_f - dewpoint_f` | Direct dryness metric; higher → more efficient radiation |
| Gustiness ratio | `wind_gust_kt / max(wind_speed_kt, 1.0)` | If > 2.0, gusts disrupt stable layer even at low mean wind |
| Pre-sunset temperature | `temp_f` at observation nearest 6 PM local | Starting temperature for cooling; higher start → potentially larger drop |
| Hour since sunset | UTC timestamp - sunset time (computed from lat/lon) | Normalize observation timing; cooling deepens through the night |

### 2.4 Database Sources

- **METAR real-time:** `metar_observations` table in `data/metar_backfill.db` (updated every 3-30 min)
- **Cloud cover:** `isd_lite_raw` table in `data/isd_lite_raw.db` (sky condition field)
- **Aggregate data:** `six_hour_aggregates` table (pre-computed 6-hour windows for trend)

---

## 3. Radiational Cooling Potential Score

### 3.1 Score Architecture

The Radiational Cooling Potential (RCP) score is a composite index from 0.0 to 1.0, designed as a continuous signal (not pass/fail). Each sub-component is independently computed and multiplicatively combined.

**General principle:** RCP = CloudScore × WindScore × DrynessScore × NightLengthFactor × SnowFactor

The multiplicative form ensures that a single zero component kills the signal — all four conditions must be present.

### 3.2 Cloud Cover Score

Derived from the ISD-lite sky condition code aggregated over the **evening window** (sunset - 2 hours to sunset + 4 hours). Evening cloud cover is more predictive than midnight cloud cover because it determines the initial 4 hours of cooling.

```
SKY_CODE_TO_FRACTION = {
    0: 0.00,   # CLR — perfect
    2: 0.125,  # FEW — mostly clear
    4: 0.375,  # SCT — scattered clouds may interfere
    6: 0.625,  # BKN — broken clouds block radiation
    8: 1.00,   # OVC — fully overcast; no radiational cooling
}

# Cloud cover fraction is weighted towards the hours immediately after sunset:
# - First 3 hours post-sunset: weight = 0.60
# - Hours 3-6 post-sunset: weight = 0.30
# - Pre-sunset: weight = 0.10
```

**CloudScore = 1.0 - weighted_cloud_fraction**

| Condition | CloudScore | Interpretation |
|---|---|---|
| CLR or FEW all evening | 0.90 - 1.00 | Ideal |
| SCT developing after sunset | 0.60 - 0.89 | Weak signal |
| BKN/OVC anytime | 0.00 - 0.20 | Kill the signal |

### 3.3 Wind Speed Score

Wind speed is the most critical and most dynamic variable. We use **sustained wind** (not gusts) from the nearest observation(s) around the evening transition.

```
calm_threshold = 3.0      # kt — dead calm
moderate_threshold = 8.0  # kt — fully disrupted

WindScore = max(0.0, 1.0 - (windspeed_kt - calm_threshold) / (moderate_threshold - calm_threshold))
```

| Wind Speed | WindScore | Effect |
|---|---|---|
| < 3 kt | 1.00 | Dead calm; stable layer fully decoupled |
| 3-5 kt | 0.60 - 1.00 | Light wind; some mixing but still favorable |
| 5-8 kt | 0.00 - 0.60 | Moderate wind; disrupted inversion |
| > 8 kt | 0.00 | Mechanical mixing prevents radiational cooling |

**Gust penalty:** If `wind_gust_kt` > 2× `wind_speed_kt` and gusts exceed 10 kt, apply a 0.5× multiplier to WindScore (intermittent gusts disrupt the stable layer).

### 3.4 Dewpoint Depression Score

```
dpd = temp_f - dewpoint_f   # °F

if dewpoint_f < 15°F:
    # Arctic airmass — extremely dry; full score even at moderate DPD
    dpd_effective = max(dpd, 20.0)
elif dewpoint_f < 25°F:
    # Very dry continental airmass
    dpd_effective = max(dpd, 15.0)
else:
    dpd_effective = dpd

DrynessScore = min(1.0, dpd_effective / 25.0)
```

| DPD Range | DrynessScore | Interpretation |
|---|---|---|
| ≥ 25°F | 1.00 | Desert-dry; maximum longwave transparency |
| 15-25°F | 0.60 - 1.00 | Dry; strong radiational cooling possible |
| 10-15°F | 0.40 - 0.60 | Moderate humidity; reduced but not killed |
| 5-10°F | 0.20 - 0.40 | Moist; weak radiational cooling |
| < 5°F | 0.00 - 0.20 | High humidity; fog/dew potential dominates; no radiational cooling signal |

### 3.5 Night Length Factor

```
night_length_hours = time_of_sunset_to_sunrise(lat, lon, date)

# Normalized: winter solstice ~15h → 1.0, summer solstice ~9h → 0.5
# Base cooling is proportional to sqrt(night length) (energy balance)

NightLengthFactor = min(1.0, sqrt(night_length_hours / 12.0))
```

| Season | Typical Night Length | Factor |
|---|---|---|
| December solstice (40°N) | 14.5 - 15.0 h | 1.00 - 1.10 |
| Equinox | 12.0 h | 1.00 |
| June solstice (40°N) | 9.0 - 9.5 h | 0.87 - 0.89 |
| Winter (KMSP @ 45°N) | 15.5 h | 1.14 |
| Summer (KPHX @ 33°N) | 10.0 h | 0.91 |

Note: Clamped to 1.10 max — seasonal variation is real but a 15-hour night doesn't give 50% more cooling than a 12-hour night because the temperature drop is logarithmic (Newton's law of cooling).

### 3.6 Snow Cover Factor

Snow cover amplifies radiational cooling through:
1. **High albedo** → reduced daytime heat storage
2. **Low thermal conductivity** → snow insulates ground, preventing upward heat flux G
3. **High emissivity** → snow surface radiates near-perfectly (ε ≈ 0.97)

```
if snow_cover_present(station, date):
    SnowFactor = 1.30    # 30% amplification
else:
    SnowFactor = 1.00
```

Snow cover detection: Use the `isd_lite_raw` `raw_line` field index 10 (snow depth indicator), or a simpler approach: check if the current observation `temp_f` and `dewpoint_f` are below 32°F with no liquid precipitation reported.

**For stations where snow is climatologically unlikely** (KPHX, KMIA, KLAX, KSFO, KHOU, KMSY, KATL, KAUS, KSAT, KDFW, KOKC, KPHL, KDCA), the SnowFactor defaults to 1.00 and should not be applied.

### 3.7 Composite Score

```
RCP = CloudScore × WindScore × DrynessScore × NightLengthFactor × SnowFactor
```

**Thresholds for actionable signals:**

| RCP Range | Classification | Action |
|---|---|---|
| ≥ 0.80 | **Strong radiational cooling** | Fire LOW-minus bias correction at full magnitude |
| 0.60 - 0.79 | **Moderate potential** | Fire LOW-minus bias correction at 50% magnitude, or wait for confirming obs |
| 0.40 - 0.59 | **Weak potential** | Monitor; do not trade unless ensemble confidence is very low |
| 0.20 - 0.39 | **Ambient** | No signal; normal ensemble handling |
| < 0.20 | **Suppressed** | Do not apply; conditions are unfavorable |

### 3.8 Dynamic Score Update

The RCP score must be **continuously recomputed** as new METAR observations arrive throughout the night. The critical update times are:

| Time | Action |
|---|---|
| **Sunset - 2h to sunset + 1h** | Initial RCP assessment; pre-position if ≥ 0.70 |
| **Sunset + 2h to sunset + 4h** | Confirm RCP with evening cooling rate; first temperature reading to compare against ensemble LOW|
| **Sunset + 4h to midnight** | Re-evaluate every 30 min; update bias if wind or cloud conditions change |
| **Midnight to 6 AM** | Monitor for wind pickup, cloud advection, or sudden warming |
| **6 AM to sunrise** | Evaluate morning minimum and compare to ensemble forecast; record validation data |

---

## 4. Expected Temperature Drop by Station

### 4.1 Physics-Based Drop Magnitude

The expected temperature drop below the ensemble forecasted LOW follows:

```
ΔT_rad = RCP × BasePotential(station, season) + SnowBonus(station, season)
```

Where `BasePotential` is derived from the station's climatological diurnal temperature range under clear, calm conditions (CCDR — clear-calm diurnal range).

### 4.2 Station Table

BasePotential estimates use:
- **Climatological clear-sky diurnal range** from ISD-lite/NOAA normals
- **Average windspeed** on clear nights
- **Elevation and basin topography** (cold air pooling potential)
- **Latitude** (night length and solar angle)

| ICAO | City | Climate Zone | BasePotential (°F) | SnowBonus (°F) | Year-Round? | Notes |
|---|---|---|---|---|---|---|
| **KMSP** | Minneapolis | Humid continental (Dfa) | -7 | -3 | Winter only | Strongest effect: cold basin, frequent snow, high latitude. Clear-calm winter nights can drop 10-15°F below model forecast. |
| **KDEN** | Denver | Semi-arid (BSk) | -6 | -2 | Fall-Spring | High elevation, dry air, frequent winter inversions. Radiational cooling is a well-known aviation hazard here. |
| **KMDW** | Chicago | Humid continental (Dfa) | -5 | -2 | Winter only | Great Lakes can moderate (lake effect clouds); score kills when clouds present. |
| **KBOS** | Boston | Humid continental (Dfb) | -5 | -2 | Winter only | Coastal moderating effect when onshore flow present. |
| **KNYC** | New York | Humid subtropical (Cfa) | -4 | -1 | Winter | Urban heat island reduces magnitude but doesn't eliminate. |
| **KPHL** | Philadelphia | Humid subtropical (Cfa) | -4 | -1 | Winter | Similar to NYC. |
| **KDCA** | Washington DC | Humid subtropical (Cfa) | -4 | -1 | Winter | Urban heat island effect. |
| **KATL** | Atlanta | Humid subtropical (Cfa) | -4 | 0 | Winter | Shorter nights, higher humidity — effect weaker but still tradeable. |
| **KOKC** | Oklahoma City | Humid subtropical (Cfa) | -4 | 0 | Winter | Dryline can produce very dry air → enhanced effect. |
| **KDFW** | Dallas | Humid subtropical (Cfa) | -3 | 0 | Winter | Gulf moisture often limits DPD. |
| **KHOU** | Houston | Humid subtropical (Cfa) | -2 | 0 | Rare | High humidity nearly always kills the signal. |
| **KMSY** | New Orleans | Humid subtropical (Cfa) | -2 | 0 | Rare | Similar to Houston. |
| **KSEA** | Seattle | Mediterranean (Csb) | -3 | 0 | Fall-Winter | Frequent cloud cover kills signal; rare clear winter nights produce strong cooling. |
| **KSFO** | San Francisco | Mediterranean (Csb) | -2 | 0 | Fall | Marine layer fog limits applicability. |
| **KLAX** | Los Angeles | Mediterranean (Csa) | -2 | 0 | Fall-Winter | Marine layer is the dominant night-time feature. |
| **KPHX** | Phoenix | Hot desert (BWh) | -5 | 0 | **Year-round** | Desert climate provides ideal conditions year-round. Very dry air, clear skies, calm nights common. However, the absolute LOW rarely matters much in summer (LOW still 80°F). |
| **KLAS** | Las Vegas | Hot desert (BWh) | -5 | 0 | **Year-round** | Same as Phoenix — ideal desert conditions. Basin topography enhances cold pooling. |
| **KSAT** | San Antonio | Humid subtropical (Cfa) | -3 | 0 | Winter | Gulf moisture interference. |
| **KAUS** | Austin | Humid subtropical (Cfa) | -3 | 0 | Winter | Similar to SAT. |
| **KMIA** | Miami | Tropical (Aw) | -1 | 0 | Never | Ocean-moderated, high humidity — radiational cooling rarely exceeds 1-2°F. |

### 4.3 Seasonal Adjustment Multiplier

The BasePotential should be adjusted by a seasonal factor:

```
SeasonalMultiplier = 0.6 + 0.4 × (1 - cos(2π × (day_of_year - 21) / 365)) / 2
```

This gives:
- **Winter solstice (Dec 21):** 1.00× (full potential)
- **Equinox:** 0.80× (80% of winter)
- **Summer solstice (Jun 21):** 0.60× (60% of winter — short nights)

**Year-round stations** (KPHX, KLAS) use the full seasonal adjustment. Their summer LOW can still undercut models but the absolute edge in °F is smaller.

### 4.4 Example: Full Calculation

**Scenario:** KMSP, January 15, clear skies, calm wind 2 kt, dewpoint -5°F (DPD = 25°F), snow cover.

```
CloudScore = 0.95  (CLR, no clouds)
WindScore = 1.00   (2 kt)
DrynessScore = 1.00 (DPD ≥ 25°F, dewpoint < 15°F)
NightLengthFactor = min(1.0, sqrt(15.0/12.0)) = 1.10
SnowFactor = 1.30  (fresh snow, deep winter)

RCP = 0.95 × 1.00 × 1.00 × 1.10 × 1.30 = 1.36
(Clamped to 1.00 for score purposes)
```

Temperature drop: `ΔT = 1.0 × (-7°F) + (-3°F) = -10°F`

If the ensemble says LOW = 5°F, the radiational cooling-adjusted expectation is **-5°F**. This is a massive edge if the market is pricing near the ensemble.

---

## 5. Signal Construction

### 5.1 Recommendation: Ensemble Bias Correction (Not Standalone Signal)

**Decision: Radiational cooling should be implemented as an ensemble bias correction, not a standalone directional signal.**

Rationale:
1. Radiational cooling is a **systematic NWP model bias**, not an independent meteorological phenomenon. It always *modifies* the ensemble forecast; it doesn't replace it.
2. The signal has no directional polarity on HIGH markets — the mechanism only affects LOW temperatures.
3. As a bias correction, it integrates naturally into existing confidence-weighted fusion without requiring a new classification lane.
4. Standalone signals require their own calibration history; we don't have labeled radiational cooling nights yet (that's what the validation protocol in §7 builds).

### 5.2 Bias Correction Formula

```
LOW_adj = LOW_ensemble - ΔT_rad

where:
  ΔT_rad = RCP × BasePotential(station, month) × SeasonalMultiplier × SnowFactor
  RCP ∈ [0, 1] (from §3)
  BasePotential ∈ [1, 7] °F (from §4.2)
  SeasonalMultiplier ∈ [0.6, 1.0] (from §4.3)
  SnowFactor ∈ [1.0, 1.3] (from §3.6)
```

### 5.3 Confidence Modulation on LOW Markets Only

The bias correction feeds into the **LOW market exceedance probability**:

```
# Original LOW exceedance probability (from ensemble fraction)
P_original = fraction_of_members_below_threshold(T*)

# Adjusted LOW with radiational cooling
LOW_adj = LOW_ensemble - ΔT_rad

# The adjusted distribution is shifted by ΔT_rad
# If we assume the ensemble spread σ is approximately correct:
shift_std = ΔT_rad / σ_ensemble

# Adjusted exceedance probability
P_adjusted = P_original + shift_std * pdf_at_threshold
# (Simplified linear approximation for small shifts; full Gaussian cdf shift preferred)
```

The confidence of the correction is proportional to RCP:

```
correction_confidence = min(1.0, RCP / 0.60)   # 0.0 at RCP<0.2, 1.0 at RCP≥0.6
```

### 5.4 Blended Probability

Following the LLOP (Log-Odds Linear Opinion Pool) framework already in use:

```
logit(P_final) = logit(P_ensemble) + w_rad × logit(P_rad)

where:
  w_rad = 0.15 × correction_confidence   # capped at 0.15 weight
  P_rad = P_adjusted from §5.3
```

The 0.15 weight cap means the radiational cooling correction can shift the final probability by at most ~15% of the log-odds distance — significant but not dominant. This prevents overfitting to a single phenomenon.

### 5.5 Highway Sign

When the bias correction fires, the alert system should display:

```
[RADIATIONAL COOLING] KMSP: RCP=0.92 ΔT=-8°F
  →  Clear, calm (2 kt), dry (DPD=22°F), snow cover
  →  Ensemble LOW = 12°F → Adjusted LOW = 4°F
  →  P(LOW < 10°F): 0.38 → 0.61
```

This fits the existing alert format pattern (see `alert_state_machine.py`, `alert_builder.py`).

---

## 6. Integration Point in Pipeline

### 6.1 Pipeline Position

The radiational cooling bias correction fits best as a **post-ensemble, pre-fusion** modulation:

```
                        ┌──────────────────────┐
                        │   METAR Observations  │
                        │  (3-30 min cadence)   │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Cloud Cover Loader   │
                        │  (isd_lite_raw.db)    │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  RCP Calculator       │  ◄── NEW MODULE
                        │  §3: score(cloud,     │
                        │       wind, dpd,      │
                        │       night_length,   │
                        │       snow) → [0,1]   │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  ΔT Estimator         │  ◄── NEW MODULE
                        │  §4: BasePotential ×  │
                        │       RCP × season    │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Ensemble LOW Output  │
                        │  (82-member, GEFS+    │
                        │   ECMWF)              │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Bias Correction      │  ◄── NEW INTEGRATION
                        │  §5: LOW_adj =        │
                        │  LOW_ens - ΔT_rad     │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Signal Fusion / LLOP │
                        │  (existing signal     │
                        │   fusion pipeline)    │
                        └──────────────────────┘
```

### 6.2 Module Structure

Two new modules in `core/signals/`:

```
core/signals/
├── radiational_cooling_signal.py     ← Main signal class (BaseSignal subclass)
└── radiational_cooling_lib.py        ← Pure functions (RCP compute, ΔT estimate)
```

The `radiational_cooling_signal.py` follows the existing `BaseSignal` pattern:

```python
class RadiationalCoolingSignal(BaseSignal):
    """
    Radiational Cooling Bias Correction Signal.

    Detects clear, calm, dry nights that produce LOW temperatures
    below ensemble NWP forecasts. Returns a bias correction ΔT and
    confidence weight for the LLOP fusion layer.

    Evaluate returns: (direction='down', confidence=correction_confidence)
    where 'down' means "LOW will be lower than ensemble predicts."
    """

    @property
    def name(self) -> str:
        return "radiational_cooling"

    @property
    def min_lookback(self) -> int:
        return 1  # Only needs current METARs + ISD cloud cover

    def evaluate(self, idx, days) -> Tuple[Optional[str], float]:
        # 1. Get current METAR observations for the target night
        # 2. Load cloud cover from ISD-lite for the evening window
        # 3. Compute RCP score
        # 4. If RCP ≥ 0.60, return ('down', correction_confidence)
        # 5. Else return (None, 0.0)
```

### 6.3 Registration

In `core/signals/__init__.py`, the `SignalRegistry`:

```python
from .radiational_cooling_signal import RadiationalCoolingSignal

class SignalRegistry:
    def __init__(self, db_path):
        self.signals = {
            # ... existing signals ...
            "radiational_cooling": RadiationalCoolingSignal(db_path),
        }
```

### 6.4 Data Flow Requirements

| Input | Source | Minimum Frequency | Latency Tolerance |
|---|---|---|---|
| METAR temp_f, dewpoint_f | metar_observations | Every 30 min | Up to 60 min |
| METAR wind_speed_kt | metar_observations | Every 30 min | Up to 30 min |
| Cloud cover (evening) | isd_lite_raw | Daily (evening window) | Up to 3 hours |
| Ensemble LOW forecast | NWP pipeline | Twice daily (00z, 12z) | Not time-sensitive |
| Snow cover | isd_lite_raw station depth or climatology | Daily | Up to 12 hours |

---

## 7. Validation Protocol

### 7.1 Objective

Measure: **On nights with RCP ≥ 0.70, does the actual LOW temperature fall below the ensemble forecast mean?**

### 7.2 Backtest Framework

Use the existing `backfill_and_backtest.py` infrastructure to:

1. **Label nights**: For each station × date in the historical database, compute RCP from historical METAR + ISD data
2. **Get actual LOW**: From METAR observations, find the minimum temperature between sunset and sunrise the next day
3. **Get ensemble LOW mean**: From stored GEFS/ECMWF ensemble data (available in `forecast_disagreement.py` or `multi_model_ensemble.py`)
4. **Compute ΔT_actual**: `LOW_actual - LOW_ensemble_mean`
5. **Compare with ΔT_predicted**: The RCP-based ΔT from §4

### 7.3 Metrics

| Metric | Target | Calculation |
|---|---|---|
| **Hit rate** | ≥ 60% | `P(LOW_actual < LOW_ensemble | RCP ≥ 0.70)` — proportion of nights where actual LOW undercut ensemble |
| **Mean absolute error reduction** | ≥ 1.5°F | `MAE(ensemble_LOW - actual) - MAE(adjusted_LOW - actual)` |
| **Bias correction accuracy** | RMSE < 4°F | RMSE between predicted ΔT_rad and actual ΔT |
| **Spearman correlation** | ρ ≥ 0.5 | Correlation between RCP score and actual ΔT |
| **False positive rate** | < 20% | Nights with RCP ≥ 0.70 where actual LOW ≥ ensemble LOW |

### 7.4 Seasonal Bucket Analysis

```
             ┌───────────────────────────────────────┐
             │      Winter    Spring    Fall  Summer  │
             │      (DJF)     (MAM)    (SON)  (JJA)  │
├────────────┼───────────────────────────────────────┤
│ KMSP       │   TBD         TBD       TBD    TBD    │
│ KDEN       │   TBD         TBD       TBD    TBD    │
│ KPHX       │   TBD         TBD       TBD    TBD    │
│ ...        │   TBD         TBD       TBD    TBD    │
└────────────┴───────────────────────────────────────┘
```

Fill in after backtest. Expected patterns:
- **Midwest/Northeast**: Strong winter signal, no summer signal
- **Desert SW**: Moderate all year — LOW temperature matters less in summer markets
- **Southeast/Coastal**: Minimal signal year-round

### 7.5 Backtest Data Availability

| Data Source | Historical Coverage | Available? |
|---|---|---|
| METAR observations | ≥ 2 years (via backfill) | ✅ Already in `data/metar_backfill.db` (1.4M+ records) |
| ISD-lite cloud cover | ≥ 10 years | ✅ Already in `data/isd_lite_raw.db` |
| GEFS ensemble 31-member | ~2 years | ✅ In ensemble storage |
| ECMWF ensemble 51-member | ~2 years | ✅ In ensemble storage |
| Actual settlement LOW | 2+ years | ✅ In `settlement_epochs` table |

### 7.6 Minimum Backtest Requirements

Before deploying live:
- **Minimum 200 nights** with RCP ≥ 0.70 across all stations (any season)
- **Minimum 50 nights** per high-impact station (KMSP, KDEN, KPHX)
- **Minimum 3 distinct winter seasons** for snow-affected stations

---

## 8. Edge Cases and Failure Modes

### 8.1 Fog / Dew Formation (False Positive Risk)

When DPD < 5°F and winds are calm, the surface layer becomes saturated. **Fog or dew formation releases latent heat**, which actually *raises* the minimum temperature 1-3°F above what a dry radiational cooling model would predict.

**Mitigation:** The DrynessScore component already kills the signal at low DPD. But there's a narrow regime where DPD is 3-5°F at sunset, then drops to saturation at 2 AM. The initial RCP (based on evening obs) would be ~0.20-0.30, correctly non-actionable.

**Enhancement:** Add a fog override: if visibility < 1.0 mi AND DPD < 3°F, force RCP = 0.

### 8.2 Wind Picking Up Overnight

The most common failure mode: calm at sunset (RCP initial = 0.80+), but a low-level jet develops after midnight. Wind speed increases to 8-12 kt, disrupting the stable layer and raising the LOW 3-5°F above the biased forecast.

**Mitigation:** The RCP score must be **continuously re-evaluated** throughout the night. The bias correction should be based on the **minimum RCP observed** during the cooling window, not just the sunset RCP.

```
ΔT_rad = RCP_min_during_night × BasePotential × ...
```

### 8.3 Lake/Ocean Effect on Cloud Cover

For stations near large water bodies (KMDW = Lake Michigan, KSEA = Puget Sound, KSFO/KLAX = Pacific), nighttime cloud cover can develop even when the synoptic conditions are clear. Lake-effect clouds form when cold air moves over warmer water.

**Mitigation:** The CloudScore component captures this naturally (it just measures actual cloud cover). No special handling needed — the score will correctly reflect that clouds are present.

### 8.4 Urban Heat Island

KNYC, KPHL, KDCA, KLAX all have significant urban heat island effects. The actual LOW at the ASOS site (typically at an airport) may be 2-4°F warmer than surrounding rural areas — but the ASOS observation is what determines the Kalshi settlement.

**Mitigation:** The BasePotential table already accounts for this (KNYC has -4°F potential vs KMSP's -7°F). The urban heat island doesn't eliminate radiational cooling — it reduces its magnitude. The ASOS site is the settlement source, so our bias correction targets that specific observation point.

### 8.5 Mountain/Valley Cold Air Drainage

Stations in or near topographic basins (KDEN = high plains/foothills, KPHX = Salt River Valley, KLAS = Las Vegas Valley) experience enhanced radiational cooling through **cold air drainage** — dense, cold air flows downhill and pools in the valley floor where the ASOS is located. This can add 2-4°F of additional drop beyond flat-terrain radiational cooling.

**Mitigation:** The BasePotential for KDEN (-6°F already reflects basin-enhanced cooling). For KPHX and KLAS, the basin effect is partially captured in the diurnal range. No separate drainage factor is added, but the backtest should validate whether actual ΔT systematically exceeds predicted ΔT for these stations.

### 8.6 Low Sun Angle / High Latitude

For KMSP (45°N), the winter sun angle is only ~22° above the horizon at noon. This means **daytime heating is weak** even under clear skies, so the starting temperature before radiational cooling is already low. The absolute LOW can be extreme but the *drop below ensemble* may be smaller on cold days than the BasePotential suggests (because the ensemble already predicts a low temperature).

**Mitigation:** Apply a diminshing-returns cap: if ensemble LOW is already < 0°F, cap ΔT_rad at 5°F (the model is already near saturation for cold error).

### 8.7 Precipitating Clouds

All precipitation (rain, snow, drizzle) immediately kills radiational cooling — precipitation requires cloud cover and releases latent heat. However, virga (precipitation evaporating before reaching the ground) can cause a false temperature drop that mimics radiational cooling but is transient.

**Mitigation:** The METAR QC parser already identifies virga (`metar_qc_parser.py` flags `VIRGA` as `SUSPECT`). If virga is present, set RCP = 0 (false positive protection).

### 8.8 Sudden Cold Front vs Radiational Cooling

A passing cold front can produce a 10°F drop in 3 hours — indistinguishable from radiational cooling in the temperature trace. However, a cold front adds mass (pressure rise, wind shift, clouds/precip), while radiational cooling occurs under stable high pressure with no wind shift.

**Mitigation:** The co-located `frontal_detector` signal can identify cold fronts. If the frontal detector fires (confidence > 0.5), do NOT apply radiational cooling bias correction for that station × date — the cold front signal takes priority.

---

## 9. Implementation Notes

### 9.1 Priority: LOW Market Only

Radiational cooling **only** affects LOW temperature markets. It has zero impact on HIGH markets. The signal should be explicitly gated:

```python
if market_type != "low":
    return  # No-op for HIGH markets
```

### 9.2 Data Dependencies

| Dependency | Already Exists? | Module |
|---|---|---|
| METAR observations | ✅ | `data/metar_backfill.db` → `metar_observations` table |
| Cloud cover (ISD-lite) | ✅ | `data/isd_lite_raw.db` → `cloud_cover_modulation.py` |
| Snow cover | ⚠️ Partial | ISD-lite has snow depth field but parser not yet built |
| Ensemble LOW mean | ✅ | `forecast_disagreement.py` → `multi_model_ensemble.py` |
| Frontal detector (conflict check) | ✅ | `core/signals/frontal_detector_signal.py` |
| Virga flag parser | ✅ | `core/metar_qc_parser.py` |

### 9.3 Snow Cover Detection Enhancement

If ISD-lite snow depth is not readily parsed, a fallback heuristic:

```python
def snow_cover_present(station: str, date: str, temp_f: float) -> bool:
    """Heuristic: if station is snow-prone and temp ≤ 32°F with no liquid precip."""
    snow_stations = {"KMSP", "KDEN", "KMDW", "KBOS", "KNYC", "KPHL", "KDCA", "KSEA"}
    if station not in snow_stations:
        return False
    if temp_f > 33.0:  # Allow 1°F margin
        return False
    # Check METAR for snow indicators
    # If raw_metar contains "SN" or "SG" or "BLSN" → snow present
    return True
```

### 9.4 Sunset/Sunrise Calculation

Sunset and sunrise times for each station × date can be computed using the `astral` library or approximated with a solar-position formula. The station latitude/longitude from the station registry is sufficient.

**Priority:** Compute sunset for each station to define the evening observation window. The calculation is deterministic and fast.

### 9.5 Confidence Weight Tuning

The LLOP weight `w_rad = 0.15` in §5.4 is an initial estimate. The backtest should tune this:

```
w_rad ∈ [0.05, 0.30]  # Search space
# Walk-forward optimization on rolling windows
# Choose w_rad that maximizes backtest Sharpe on LOW-only nights with RCP ≥ 0.70
```

### 9.6 Alert Integration

The existing `alert_builder.py` alert system should include radiational cooling events as a separate alert type (`alert_type = "radiational_cooling"`) for monitoring purposes, even though the signal itself operates as a bias correction rather than a standalone trade trigger.

### 9.7 Estimated Marginal Value

| Metric | Estimated Improvement |
|---|---|
| LOW market accuracy | +2-4% raw accuracy on winter nights |
| Nights affected | 15-30% of winter nights at KMSP, KDEN |
| Edge per night | 0.5-2.0% probability shift in LOW exceedance |
| Sharpe improvement | +0.05 to +0.15 on portfolio (LOW-only subset) |
| Data cost | $0 (all data already collected) |

---

## Appendix A: RCP Calculation Pseudocode

```python
def compute_rcp(station: str, date: str, metar_db_path: str, isd_db_path: str) -> dict:
    """
    Compute Radiational Cooling Potential score for a station on a given night.
    
    Returns dict with keys: 'rcp', 'cloud_score', 'wind_score',
    'dryness_score', 'night_length', 'snow_factor', 'components'
    """
    # 1. Get evening METAR observations (sunset - 2h to sunset + 4h)
    sunset = compute_sunset(station, date)
    window_start = sunset - timedelta(hours=2)
    window_end = sunset + timedelta(hours=4)
    
    metars = get_metars_in_window(station, window_start, window_end, metar_db_path)
    if not metars:
        return {'rcp': 0.0, 'reason': 'no_metar_data'}
    
    # 2. Cloud cover from ISD-lite
    cloud_frac = get_weighted_cloud_cover(station, date, window_start, window_end, isd_db_path)
    cloud_score = 1.0 - cloud_frac
    
    # 3. Wind speed (calm = best)
    avg_wind = average_wind(metars)
    wind_score = max(0.0, 1.0 - (avg_wind - 3.0) / 5.0) if avg_wind > 3.0 else 1.0
    
    # Gust penalty
    max_gust = max_gust_in_window(metars)
    if max_gust > 10.0 and max_gust / max(avg_wind, 1.0) > 2.0:
        wind_score *= 0.5
    
    # 4. Dewpoint depression
    avg_dpd = average_dewpoint_depression(metars)
    if dewpoint < 15:
        dpd_effective = max(avg_dpd, 20.0)
    elif dewpoint < 25:
        dpd_effective = max(avg_dpd, 15.0)
    else:
        dpd_effective = avg_dpd
    dryness_score = min(1.0, dpd_effective / 25.0)
    
    # 5. Night length
    sunrise = compute_sunrise(station, date)
    night_hours = (sunrise - sunset).total_seconds() / 3600.0
    night_length_factor = min(1.10, math.sqrt(night_hours / 12.0))
    
    # 6. Snow cover
    snow_factor = 1.30 if check_snow_cover(station, date, metars) else 1.00
    
    # 7. Composite
    rcp = cloud_score * wind_score * dryness_score * night_length_factor * snow_factor
    rcp = min(1.0, rcp)
    
    # 8. Virga override
    if virga_detected(metars):
        rcp = 0.0
    
    # 9. Frontal passage override
    if frontal_passage_detected(station, date):
        rcp = 0.0
    
    return {
        'rcp': rcp,
        'cloud_score': cloud_score,
        'wind_score': wind_score,
        'dryness_score': dryness_score,
        'night_length': night_length_factor,
        'snow_factor': snow_factor,
        'avg_wind_kt': avg_wind,
        'avg_dpd_f': avg_dpd,
        'cloud_fraction': cloud_frac
    }
```

## Appendix B: Temperature Drop Estimation Pseudocode

```python
def estimate_delta_t(station: str, date: str, rcp: float) -> float:
    """
    Estimate the expected temperature drop below ensemble forecast.
    Returns ΔT in °F (positive = cooler than ensemble).
    """
    # Base potential by station and month
    base_potential = STATION_BASE_POTENTIALS[station]  # °F
    
    # Seasonal adjustment
    month = int(date[5:7])
    day_of_year = date_to_doy(date)
    season_mult = 0.6 + 0.4 * (1 - cos(2*pi*(day_of_year - 21)/365)) / 2
    
    # Snow bonus
    snow_bonus = STATION_SNOW_BONUSES.get(station, 0.0)
    snow_active = snow_bonus if check_snow_cover(station, date) else 0.0
    
    # Total
    delta_t = rcp * base_potential * season_mult + snow_active
    
    # Caps
    ensemble_low = get_ensemble_low(station, date)
    if ensemble_low < 0:
        delta_t = min(delta_t, 5.0)  # Diminishing returns on extreme cold
    
    return round(delta_t, 1)
```

---

*End of design specification. Ready for C-Suite review.*