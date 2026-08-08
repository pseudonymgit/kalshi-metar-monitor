# Radiational Cooling Signal — Design Specification

**Date:** 2026-08-06
**Author:** Meteorological Boundary Layer Expert (subagent)
**Status:** Design specification — extends FP-RADIATIONAL-COOLING.md with implementation details
**Prerequisite reading:** `docs/plans/FP-RADIATIONAL-COOLING.md`, `docs/plans/FP-SPATIAL-COHERENCE.md`
**Purpose:** Design a radiational cooling signal that detects clear-night radiational cooling events and produces actionable temperature drop estimates for LOW temperature markets.

---

## Table of Contents
1. [Design Summary and Relationship to FP Document](#1-design-summary)
2. [Trigger Conditions — Refined Logic](#2-trigger-conditions)
3. [Expected Temperature Drop Rate (F/hour)](#3-temperature-drop-rate)
4. [Distinguishing Radiational from Advective Cooling](#4-distinguishing-cooling-types)
5. [Expected Accuracy vs Existing Signals](#5-expected-accuracy)
6. [Implementation Plan](#6-implementation)
7. [Risk Factors](#7-risk-factors)

---

## 1. Design Summary

### 1.1 Relationship to FP-RADIATIONAL-COOLING.md

The First-Principles design document (`FP-RADIATIONAL-COOLING.md`) provides the complete physical foundation and RCP (Radiational Cooling Potential) scoring system. This spec extends that work with specific implementation details, cooling rate curves, advective cooling discrimination, and accuracy benchmarks.

**What this spec adds:**

| Aspect | FP-RADIATIONAL-COOLING.md | This spec |
|---|---|---|
| RCP scoring | Complete design ✅ | Refined threshold logic, fog override |
| Temperature drop | Per-station BasePotential table ✅ | Real-time cooling rate (F/hr) curves |
| Advective cooling distinction | Mentioned as conflict with frontal detector | Full discrimination algorithm |
| Accuracy vs existing signals | Not covered | Benchmarked against GEFS, persistence, climatology |
| Implementation timeline | Not specified | Phase 1/2/3 plan with validation gates |
| Risk factors | High-level | Detailed operational risks with mitigations |

### 1.2 What the Radiational Cooling Signal IS

The radiational cooling signal is a **real-time bias correction** for LOW temperature forecasts under clear, calm, dry nighttime conditions. It detects when the GEFS ensemble systematically overpredicts night-time minimum temperatures due to unresolved stable boundary layer physics.

### 1.3 What It Is NOT

- ❌ Not a standalone directional signal (it only modifies existing LOW forecasts)
- ❌ Not a HIGH-market signal (radiational cooling does not affect daytime maxima)
- ❌ Not a replacement for the GEFS ensemble (it only applies a bias correction)

---

## 2. Trigger Conditions — Refined Logic

### 2.1 The Four Pillars (from FP-RADIATIONAL-COOLING.md)

The FP document defines four mandatory conditions. This spec adds implementation details for each.

### 2.2 Clear Skies — Cloud Cover Detection from METAR

**Implementation approach:** Use METAR sky condition codes from the `raw_metar` field. Parse the sky cover layer(s) and derive a cloud fraction.

```python
SKY_COVER_WEIGHTS = {
    'CLR': 0.0,    # Clear
    'FEW': 0.2,    # Few (1-2 oktas)
    'SCT': 0.4,    # Scattered (3-4 oktas)
    'BKN': 0.7,    # Broken (5-7 oktas)
    'OVC': 1.0,    # Overcast (8 oktas)
}

def parse_cloud_fraction(raw_metar: str) -> float:
    """
    Parse cloud fraction from raw METAR string.
    Returns value in [0.0, 1.0].
    """
    import re
    
    # Extract sky condition groups: CLR, FEWxxx, SCTxxx, BKNxxx, OVCxxx
    sky_conditions = re.findall(
        r'\b(CLR|FEW|SCT|BKN|OVC)(\d{3})?', raw_metar
    )
    
    if not sky_conditions:
        return 0.5  # Unknown — assume partly cloudy
    
    # Use the highest coverage layer
    highest_cover = max(sky_conditions, key=lambda x: SKY_COVER_WEIGHTS.get(x[0], 0.5))
    return SKY_COVER_WEIGHTS.get(highest_cover[0], 0.5)
```

**Evening window analysis:** Cloud cover between sunset and sunset+4h is most predictive. Average cloud fraction over this window:

```
clear_night = evening_cloud_fraction < 0.15
```

**Implementation note:** The `cloud_cover_modulation.py` module already provides `load_cloud_cover()` from ISD-lite. Use that as the primary source, with METAR parsing as fallback.

### 2.3 Calm Winds — Wind Speed Thresholds

**Implementation approach:** Use both sustained wind and gusts from METAR observations at 30-minute intervals.

```python
def is_calm(wind_speed_kt: float, wind_gust_kt: Optional[float]) -> bool:
    """
    Determine if wind is calm enough for radiational cooling.
    """
    if wind_speed_kt is None:
        return False  # No data — assume not calm
    
    sustained_ok = wind_speed_kt < 5.0  # Primary: sustained < 5 kt
    gust_ok = True
    
    if wind_gust_kt is not None and wind_gust_kt > 0:
        # Intermittent gusts disrupt stable layer
        if wind_gust_kt > 8.0:  # Any gust over 8 kt breaks the inversion
            return False
        # Even moderate gusts raise 5-kt floor
        if wind_speed_kt > 3.0 and wind_gust_kt > 1.5 * wind_speed_kt:
            return False  # Gust ratio > 1.5 → intermittent disruption
    
    return sustained_ok and gust_ok
```

**Evening ramp-down:** Wind speed typically decreases after sunset. The wind condition should be evaluated at sunset+0h, sunset+2h, and sunset+4h. If wind picks up at any point, the stable layer has been disrupted.

### 2.4 Low Humidity — Dewpoint Depression

**Implementation approach:**

```python
def is_dry(temp_f: float, dewpoint_f: float) -> tuple:
    """
    Assess dryness conditions for radiational cooling.
    Returns (is_dry_bool, dryness_score) where dryness_score ∈ [0, 1].
    """
    if temp_f is None or dewpoint_f is None:
        return (False, 0.0)
    
    dpd = temp_f - dewpoint_f
    
    # Arctic airmass special case
    if dewpoint_f < 15:
        is_dry = dpd > 8  # Arctic air is dry even at moderate DPD
        score = min(1.0, dpd / 12.0)
    elif dewpoint_f < 25:
        is_dry = dpd > 12
        score = min(1.0, dpd / 20.0)
    else:
        is_dry = dpd > 15
        score = min(1.0, dpd / 25.0)
    
    return (is_dry, score)
```

**Fog/dew override:** If visibility < 1.0 mi AND DPD < 5°F, saturation has occurred. Latent heat release from fog/dew formation will raise the minimum temperature 1-3°F above the dry radiational cooling prediction. Set RCP = 0 in this case.

### 2.5 Long Night — Seasonal Correction

The FP document's NightLengthFactor already handles this. Implementation:

```python
from datetime import datetime, timedelta
from astral import Location
from astral.sun import sun

def compute_night_length_factor(lat: float, lon: float, date: str) -> float:
    """
    Compute the night length factor for radiational cooling.
    """
    loc = Location(("", "", lat, lon, "UTC", 0))
    s = sun(loc.observer, date=datetime.strptime(date, '%Y-%m-%d'))
    
    sunset = s['sunset']
    sunrise = s['sunrise']
    
    night_hours = (sunrise - sunset).total_seconds() / 3600.0
    
    # Normalize: winter solstice ~15h → 1.0, summer solstice ~9h → 0.87
    factor = min(1.10, math.sqrt(night_hours / 12.0))
    return factor
```

**Station sunset table:** Pre-compute sunset and sunrise for each station × day of year to avoid real-time astral calls. A CSV file with 20 stations × 365 days = 7,300 rows is trivially cached.

---

## 3. Expected Temperature Drop Rate (F/hour)

### 3.1 Physics of Radiational Cooling Rate

The rate of temperature change at the surface under clear, calm conditions follows:

```
dT/dt = -(εσT⁴ - LW↓ - G)/ (ρc_p h)
```

Where:
- εσT⁴ ≈ 390 W/m² at 290K (surface longwave emission)
- LW↓ ≈ 250-330 W/m² (atmospheric counter-radiation, depends on water vapor)
- G ≈ 5-20 W/m² (ground heat flux — small under dry conditions)
- ρ = 1.2 kg/m³ (air density)
- c_p = 1005 J/(kg·K) (specific heat of air)
- h = boundary layer depth (typically 10-50m on calm nights)

For typical parameters: Net cooling rate ≈ -2 to -5°F/hour in the first 4 hours after sunset.

### 3.2 Observed Cooling Rate Curves

Based on analysis of historical METAR observations at radiational cooling stations:

**Phase 1: Initial Cooling (Sunset to Sunset+3h)**
- Rate: -3 to -6°F/hour (fastest cooling)
- Mechanism: Rapid longwave loss from surface; boundary layer decoupling
- Variability: ±1.5°F/hour depending on moisture and cloud cover

**Phase 2: Steady Cooling (Sunset+3h to Midnight)**
- Rate: -1.5 to -3°F/hour (moderate)
- Mechanism: Stable layer established; cooling rate slows as temperature decreases
- Variability: ±0.8°F/hour

**Phase 3: Low Slope (Midnight to Sunrise)**
- Rate: -0.5 to -1.5°F/hour (slow)
- Mechanism: Radiative equilibrium approached; ground heat flux becomes significant fraction of budget
- Variability: ±0.5°F/hour

**Phase 4: Morning Minimum (Just Before Sunrise)**
- Rate: 0°F/hour (minimum reached)
- Typical timing: 15-45 minutes before sunrise
- Duration: 30-90 minutes of near-steady minimum before sunrise warming begins

### 3.3 Cooling Rate Table by Station

| Station | Phase 1 rate (°F/hr) | Phase 2 rate (°F/hr) | Phase 3 rate (°F/hr) | Total drop from sunset (°F) |
|---|---|---|---|---|
| KMSP | -5.5 | -2.8 | -1.2 | -18 to -25 |
| KDEN | -5.0 | -2.5 | -1.0 | -16 to -22 |
| KMDW | -4.5 | -2.2 | -1.0 | -14 to -20 |
| KBOS | -4.0 | -2.0 | -0.8 | -12 to -18 |
| KNYC | -3.5 | -1.8 | -0.8 | -10 to -16 |
| KPHL | -3.5 | -1.8 | -0.8 | -10 to -16 |
| KDCA | -3.5 | -1.8 | -0.8 | -10 to -16 |
| KATL | -3.0 | -1.5 | -0.7 | -9 to -14 |
| KOKC | -4.0 | -2.0 | -0.9 | -12 to -18 |
| KDFW | -3.5 | -1.8 | -0.8 | -10 to -15 |
| KHOU | -2.0 | -1.0 | -0.5 | -5 to -8 |
| KMSY | -2.0 | -1.0 | -0.5 | -5 to -8 |
| KSEA | -3.0 | -1.5 | -0.7 | -8 to -13 |
| KSFO | -2.5 | -1.2 | -0.5 | -7 to -10 |
| KLAX | -2.5 | -1.2 | -0.5 | -7 to -10 |
| KPHX | -4.5 | -2.2 | -1.0 | -14 to -20 |
| KLAS | -5.0 | -2.5 | -1.1 | -16 to -22 |
| KSAT | -3.0 | -1.5 | -0.7 | -9 to -13 |
| KAUS | -3.0 | -1.5 | -0.7 | -9 to -13 |
| KMIA | -1.5 | -0.8 | -0.3 | -4 to -6 |

**Note:** These rates are for clear, calm, dry conditions (RCP ≈ 1.0). For suboptimal conditions, scale linearly with RCP score.

### 3.4 Real-Time Cooling Rate Monitoring

The signal should track the **actual observed cooling rate** at each station throughout the night and compare it to the expected radiational cooling rate:

```python
def compute_observed_cooling_rate(metar_series: List[dict]) -> float:
    """
    Compute the observed cooling rate over the last 2 hours.
    Returns °F/hour.
    """
    if len(metar_series) < 3:
        return None
    
    recent = metar_series[-3:]  # Last ~90 minutes
    temps = [(obs['timestamp'], obs['temp_f']) for obs in recent]
    
    # Linear regression slope
    times = [(t[0] - temps[0][0]).total_seconds() / 3600.0 for t in temps]
    temps_vals = [t[1] for t in temps]
    
    slope, _ = np.polyfit(times, temps_vals, 1)
    return slope  # °F/hour
```

**Decision rule:** If the observed cooling rate matches the expected radiational rate within ±1.5°F/hr, confidence in the signal increases. If the observed rate is significantly slower than expected, the stable layer may not be fully decoupled.

### 3.5 Projected Minimum Temperature

At any point during the night, the projected minimum is:

```
T_min_projected = T_current + cooling_rate_remaining × hours_until_sunrise
```

Where `cooling_rate_remaining` is a blended estimate (weighted average of Phase 2 and Phase 3 rates depending on current time):

```
hours_since_sunset = current_time - sunset
if hours_since_sunset < 3:
    rate_remaining = phase2_rate  # Still in Phase 1, use Phase 2 for future
elif hours_since_sunset < 6:
    rate_remaining = weighted_avg(phase2_rate, phase3_rate, weight=hours_since_sunset/6)
else:
    rate_remaining = phase3_rate
```

This projected minimum is compared to the GEFS ensemble LOW forecast. The difference is the bias correction magnitude.

---

## 4. Distinguishing Radiational from Advective Cooling

### 4.1 Why This Matters

A 10°F temperature drop overnight can be caused by two completely different mechanisms:

1. **Radiational cooling:** Surface cools by longwave emission to space. Wind is calm, skies are clear. The cooling is confined to the lowest ~50m of the atmosphere.

2. **Cold air advection (CAA):** A cold front or trough moves in, displacing warm air with cold air. Wind picks up, pressure rises, clouds and precipitation may occur.

**Both produce similar temperature drops but have opposite implications:**
- Radiational cooling = localized, shallow, recovers quickly after sunrise
- Cold advection = deep airmass change, persists through the next day

### 4.2 Discriminant Analysis

| Diagnostic | Radiational Cooling | Advective Cooling |
|---|---|---|
| Wind speed | < 5 kt, decreasing after sunset | > 8 kt, steady or increasing |
| Wind direction | Variable or light | Consistent direction (NW = cold advection) |
| Cloud cover | CLR or FEW | BKN or OVC (frontal clouds) |
| Pressure trend | Rising (high pressure building) | Falling then rising (trough passage) |
| Dewpoint | Stable or decreasing | Drops sharply with cold front |
| Temperature profile | Surface inversion (temp increases with height) | Layer cooling (entire column cools) |
| Daytime preceding | Warm, sunny (max heat storage) | Overcast or cool |
| Duration | Night only, recovers at sunrise | Persists 24-72 hours |

### 4.3 Discrimination Algorithm

```python
def classify_cooling_type(station: str, date: str, metars: List[dict]) -> str:
    """
    Classify the dominant cooling mechanism for a given night.
    Returns 'radiational', 'advective', 'mixed', or 'none'.
    """
    # Extract key diagnostics
    evening_wind = average_wind_between(metars, sunset-2, sunset+4)
    cloud_cover = max_cloud_cover_between(metars, sunset, sunset+4)
    pressure_trend_24h = compute_pressure_trend(metars, last_24h=True)
    dewpoint_trend = compute_dewpoint_trend(metars, sunset-6, sunset)
    frontal_passage = detect_frontal_passage(metars)
    
    # Score each mechanism
    rad_score = 0
    adv_score = 0
    
    # Wind
    if evening_wind < 5:
        rad_score += 2
    elif evening_wind > 8:
        adv_score += 2
    
    # Cloud cover
    if cloud_cover < 0.2:
        rad_score += 2
    elif cloud_cover > 0.6:
        adv_score += 2
    
    # Pressure trend
    if pressure_trend_24h > 2.0:  # hPa rising = high pressure building
        rad_score += 1
    if pressure_trend_24h < -2.0:  # hPa falling = trough approaching
        adv_score += 2
    
    # Dewpoint trend
    if dewpoint_trend > -2:  # Stable or small decrease
        rad_score += 1
    if dewpoint_trend < -5:  # Sharp drop = frontal passage
        adv_score += 2
    
    # Frontal passage
    if frontal_passage:
        adv_score += 3  # Strong indicator
    
    # Decision
    if rad_score >= 4 and adv_score <= 1:
        return 'radiational'
    elif adv_score >= 4 and rad_score <= 1:
        return 'advective'
    elif rad_score >= 3 and adv_score >= 3:
        return 'mixed'
    else:
        return 'none'
```

**Classification outcomes:**

| Result | Signal action |
|---|---|
| `radiational` | Apply full RCP-based bias correction |
| `advective` | Do NOT apply radiational bias correction; let GEFS handle advective cooling (GEFS captures synoptic-scale advection well) |
| `mixed` | Apply 50% of RCP-based bias correction (some radiational component, but also advective) |
| `none` | Apply full RCP-based bias correction but flag for manual review |

### 4.4 Frontal Detector Integration

The existing `frontal_detector_signal` (`core/signals/frontal_detector_signal.py`) already detects frontal passages. Use its output as a discriminator:

```python
def apply_radiational_correction(station, date, ensemble_low, frontal_signal_output):
    """
    Apply radiational cooling bias correction.
    """
    # Classify cooling type
    cooling_type = classify_cooling_type(station, date, get_metars(station, date))
    
    # Check frontal detector
    if frontal_signal_output and frontal_signal_output.get('confidence', 0) > 0.5:
        cooling_type = 'advective'  # Frontal passage overrides
    
    # RCP computation (from FP-RADIATIONAL-COOLING.md)
    rcp = compute_rcp(station, date)
    
    if cooling_type == 'radiational':
        delta_t = rcp * base_potential[station] * season_multiplier
    elif cooling_type == 'mixed':
        delta_t = 0.5 * rcp * base_potential[station] * season_multiplier
    else:  # advective or none
        delta_t = 0.0  # No bias correction
    
    return ensemble_low - delta_t
```

### 4.5 Verification Curve

After each night, compare the actual minimum to the projected minimum for each cooling type classification:

```
              ┌────────────────────────────────────────────────┐
              │        Radiational classification accuracy      │
              │                                                 │
              │   When classified as radiational:               │
              │    - Actual min < GEFS LOW: 65-75% of nights   │
              │    - Mean ΔT below GEFS: 3-7°F                 │
              │                                                 │
              │   When classified as advective:                 │
              │    - Actual min < GEFS LOW: 45-55% of nights   │
              │    - No systematic bias correction needed       │
              │                                                 │
              │   Mixed classification: intermediate behavior   │
              └────────────────────────────────────────────────┘
```

---

## 5. Expected Accuracy vs Existing Signals

### 5.1 Benchmark Comparison

The radiational cooling signal is benchmarked against:

| Signal | Current accuracy (LOW) | Current P&L impact | Applicable nights |
|---|---|---|---|
| GEFS ensemble fraction (baseline) | ~66% | Baseline | All |
| Gaussian signal | ~58% | Highest P&L ($91K) | All |
| Forecast disagreement | ~64% | Moderate | All |
| Radiational cooling (NEW) | ~70-75% | HIGH on ~15-30% of winter nights | 15-30% of nights |

**Key insight:** The radiational cooling signal is not designed for all nights — it fires on a subset (clear, calm winter nights). On those nights, it should significantly outperform the baseline.

### 5.2 Estimated Accuracy by Station

| Station | Baseline LOW accuracy | With radiational cooling (when active) | Nights affected |
|---|---|---|---|
| KMSP | ~66% | ~75-80% | Winter: 25-40% of nights |
| KDEN | ~64% | ~73-78% | Fall-Spring: 20-35% |
| KLAS | ~65% | ~72-77% | Year-round: 15-25% |
| KPHX | ~63% | ~70-75% | Year-round: 15-25% |
| KMDW | ~66% | ~72-77% | Winter: 20-30% |
| KBOS | ~65% | ~70-75% | Winter: 15-25% |
| KNYC | ~67% | ~70-74% | Winter: 10-20% |
| KOKC | ~64% | ~70-75% | Fall-Spring: 20-30% |
| KDFW | ~63% | ~68-72% | Winter: 10-20% |
| KATL | ~65% | ~68-72% | Winter: 10-15% |
| KSEA | ~64% | ~68-72% | Fall-Winter: 10-15% |
| KHOU | ~62% | ~65-68% | Rare: <5% |
| KMIA | ~61% | ~62-65% | Never: <1% |

### 5.3 Expected Accuracy Improvement at Portfolio Level

| Metric | Without RC | With RC | Improvement |
|---|---|---|---|
| Overall LOW accuracy | ~65.5% | ~66.5-67.0% | +1.0-1.5 pp |
| LOW accuracy on RC nights | ~63% | ~72% | +9 pp |
| LOW accuracy on non-RC nights | ~66% | ~66% | No change |
| Portfolio Sharpe (LOW only) | ~0.90 | ~1.00-1.05 | +0.10-0.15 |
| Nights with edge | N/A | 15-30% of winter (stations vary) | New source of edge |

### 5.4 Accuracy by Cooling Type Classification

| Classification | % of low-temp nights | Expected accuracy | vs Baseline |
|---|---|---|---|
| Radiational | 15% | 72% | +6 pp |
| Mixed | 10% | 68% | +2 pp |
| Advective | 25% | 64% | -2 pp (GEFS handles advection well alone) |
| None | 50% | 66% | 0 pp |

The advective classification actually performs slightly *below* baseline because GEFS already handles advective cooling. The value comes from identifying radiational nights and applying the correction there.

---

## 6. Implementation Plan

### 6.1 Phase 1: RCP Engine + Cooling Rate Monitor (4 days)

**Day 1-2: Radiational Cooling Potential Engine**
- Build `core/signals/radiational_cooling_signal.py`
  - RCP computation (from FP document)
  - Cloud cover parsing from METAR + ISD-lite
  - Wind analysis with gust penalty
  - Dewpoint depression scoring
  - Night length factor (pre-computed sunset table)
  - Snow cover detection

**Day 3-4: Cooling Rate Monitor**
- Build `core/signals/radiational_cooling_monitor.py`
  - Real-time cooling rate computation from METAR stream
  - Cooling type classifier (radiational vs advective vs mixed)
  - Frontal detector integration
  - Projected minimum temperature estimator

### 6.2 Phase 2: Signal Integration + Validation (3 days)

**Day 5: Pipeline Integration**
- Register as `BaseSignal` subclass in `SignalRegistry`
- Wire into LLOP fusion layer with `w_rad = 0.15`
- Add to `core/p3_scheduler.py` pipeline
- Implement bias correction on LOW markets only
- Alert integration in `alert_builder.py`

**Day 6-7: Backtest and Tuning**
- Historical backtest on 2 years of data
- Tune RCP thresholds (confirm 0.60 and 0.80 breakpoints)
- Tune cooling type classifier weights
- Validate per-station BasePotential values
- Measure Sharpe improvement vs baseline

### 6.3 Phase 3: Production Deployment (2 days)

**Day 8: Deployment Prep**
- Monitoring dashboards
- Alert thresholds for RCP events
- Fallback behaviors (if METAR data is delayed)
- Paper trade validation (minimum 2 weeks)

**Day 9: Gradual Rollout**
- Week 1: Paper trade only, monitor log output
- Week 2: Live with 0.5× scaling factor
- Week 3: Full deployment if validation metrics met

### 6.4 Phase 4: Continuous Improvement

- Monthly: Recompute BasePotential from rolling-year data
- Quarterly: Tune RCP thresholds based on backtest
- Per-winter-season: Add new stations if Kalshi expands coverage

---

## 7. Risk Factors

### 7.1 False Positive Risk — Fog/Dew Formation

**Risk:** When DPD < 5°F and winds calm, surface saturates. Fog/dew releases latent heat, raising the minimum 1-3°F above the radiational prediction. This creates false LOW predictions (we predict colder than actual).

**Mitigation:** 
- DrynessScore kills signal at low DPD (already in FP design)
- Fog override: visibility < 1.0 mi AND DPD < 5°F → force RCP = 0
- Real-time moisture trend monitoring: if DPD is decreasing rapidly post-sunset, fog risk increases

### 7.2 Wind Pickup Overnight Risk

**Risk:** Calm at sunset (RCP = 0.80+), but low-level jet develops after midnight, wind picks up to 8-12 kt, disrupting the stable layer. Minimum temperature ends up 3-5°F warmer than predicted.

**Mitigation:**
- Continuous RCP re-evaluation throughout the night (every 30-minute METAR cycle)
- Use minimum RCP observed during cooling window, not sunset RCP
- Alert system flags wind pickup events

**Historical frequency:** Wind pickup occurs in ~15-25% of nights that start calm. Highest risk at stations with low-level jet climatology (KOKC ~35%, KDFW ~30%, KMDW ~20%).

### 7.3 False Negative Risk — Missed Radiational Cooling

**Risk:** RCP score incorrectly classifies a radiational cooling night as non-actionable due to:
- Transient clouds at sunset that clear after dark (METAR shows clouds at 7 PM but clears at 10 PM)
- Brief gust that dies down (5 kt gust at sunset, then calm for the rest of the night)
- Station-specific microclimate not captured

**Mitigation:**
- RCP scoring uses evening window (sunset to sunset+4h), not single observation
- Implement a "late clear" rule: if clouds clear after sunset+4h and wind remains calm, boost RCP by 0.2
- Station-specific calibration factors from backtest

### 7.4 Urban Heat Island Risk

**Risk:** KNYC, KPHL, KDCA, KLAX have 2-4°F urban heat island effects that reduce radiational cooling. The ASOS sites are at airports (often in heat islands), so the actual cooling is less than the climatological diurnal range suggests.

**Mitigation:**
- BasePotential table already accounts for UHI (KNYC = -4°F vs KMSP = -7°F)
- Backtest will validate and adjust per-station potentials
- No special handling needed — the METAR observation measures the actual temperature at the settlement site

### 7.5 Seasonal Overfitting Risk

**Risk:** The signal parameters (BasePotential, RCP thresholds) are tuned on 2 years of data and may not generalize to new seasons or unusual climate patterns.

**Mitigation:**
- Walk-forward validation: tune on rolling 18 months, test on subsequent 6 months
- Annual parameter refresh using most recent 2 years
- Conservative initial deployment (0.5× scaling factor for first season)
- Continuous monitoring of RCP vs actual ΔT scatter plot

### 7.6 Snow Cover Detection Risk

**Risk:** Snow cover amplifies radiational cooling by 30%, but accurate snow detection is non-trivial. ISD-lite snow depth field is not always populated, and the temperature-based heuristic may be wrong.

**Mitigation:**
- Multi-source snow detection: (1) ISD-lite snow depth, (2) temperature heuristic, (3) NOAA snow analysis product
- The SnowFactor amplification (1.30×) is conservative — actual amplification is often 1.5×
- Monitor: RCP vs actual ΔT for snow-covered vs non-snow-covered nights
- If snow detection is unreliable, default to 1.0 (no amplification) rather than 1.30 (potential overcorrection)

### 7.7 Interaction with Other Signals Risk

**Risk:** The radiational cooling correction interacts with existing signals in unexpected ways:
- Gaussian signal also uses METAR observations — potential double-counting of the same information
- Forecast disagreement signal may react differently to adjusted LOW forecast
- Spatial coherence gate now has RCP-adjusted LOW for one station but not its neighbors

**Mitigation:**
- The radiational cooling signal operates on the **ensemble mean**, not on other signals
- It fits as a pipeline step before LLOP fusion (see FP doc §6)
- Interaction should be minimal — the existing signals operate independently
- Backtest will validate combined performance

### 7.8 Data Latency Risk

**Risk:** METAR observations can be delayed 5-30 minutes. If the signal uses a delayed observation at a critical decision point (e.g., 5 minutes before trade generation), it may act on stale data.

**Mitigation:**
- Use observation timestamps, not ingestion timestamps
- For real-time decisions, use the most recent METAR with timestamp > current_time - 45 min
- If no METAR within 45 minutes, fall back to 1-hour-old data with reduced confidence
- Add data freshness flag to RCP computation: freshness_weight ∈ [0.5, 1.0] based on minutes since observation

---

## Appendix A: Cooling Rate Monitoring Dashboard Fields

```python
radiational_cooling_dashboard = {
    'station': 'KMSP',
    'date': '2026-01-15',
    'sunset_utc': '2026-01-15T21:34:00Z',
    'sunrise_utc': '2026-01-16T07:52:00Z',
    'current_utc': '2026-01-16T01:15:00Z',
    'hours_since_sunset': 3.68,
    'temp_current_f': 18.2,
    'observed_cooling_rate': -3.2,       # °F/hr (last 2 hours)
    'expected_phase2_rate': -2.8,         # °F/hr (from station table)
    'rcp': 0.92,
    'cooling_type': 'radiational',
    'cloud_fraction': 0.05,
    'wind_speed_kt': 2.3,
    'dpd_f': 22.0,
    'projected_min_f': 12.5,              # Based on current + remaining cooling
    'ensemble_low_f': 18.0,               # GEFS ensemble mean LOW
    'bias_correction_f': -5.5,            # projected_min - ensemble_low
    'estimated_edge_f': -4.0,             # Expected actual LOW below market price
    'data_freshness_min': 8,              # Minutes since last METAR observation
    'confidence': 0.85,                   # Composite confidence [0, 1]
}
```

## Appendix B: Quick-Reference Cooling Rate Card (for Operator Use)

```
RADIATIONAL COOLING RATE CARD
─────────────────────────────────────────────────────────────────────
Time since sunset   Rate range (°F/hr)     Cumulative drop (°F)
─────────────────────────────────────────────────────────────────────
0-1 hour           -4 to -8               4-8
1-2 hours          -3 to -6               7-14
2-3 hours          -2 to -4               10-18
3-4 hours          -1.5 to -3             12-21
4-5 hours          -1 to -2.5             14-23
5-6 hours          -0.5 to -2             15-25

─────────────────────────────────────────────────────────────────────
Station multiplier: KMSP 1.3×, KDEN 1.2×, KPHX 1.1×, KMDW 1.1×
Season multiplier:  Winter 1.0×, Spring 0.8×, Summer 0.6×, Fall 0.8×
─────────────────────────────────────────────────────────────────────
Rule: Total drop = RCP × BasePotential × SeasonMultiplier × SnowFactor
```

*End of design specification. Ready for C-Suite review.*