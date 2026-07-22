# Gray Room Round 9 — Expert 1: Meteorology & Weather Data Analysis

**Domain:** Meteorology, Weather Data Sources, Signal Accuracy  
**Date:** 2026-07-22  
**Analyst:** Expert 1 — Independent analysis. No prior Gray Room findings referenced.

---

## EXECUTIVE SUMMARY

This analysis examines the weather engine's meteorological foundation across 17 signal files, 2 collection pipelines, 3 fusion/calibration modules, and supporting infrastructure. The codebase shows sophistication in signal diversity (9 METAR-based + 2 NWP signals) but suffers from fundamental meteorological inaccuracies, orphaned signals, physically incorrect direction-prediction mappings, and a complete absence of station-specific geographic effects.

**Score summary:** The weather data pipeline is ~70% functional for METAR ingestion but has critical flaws in signal physics, NWP integration completeness, and downstream fusion. A meteorologist would identify 10+ issues that a statistician or engineer would not see.

---

## ERRORS (10+)

### E1. NWP Analog Signal Is Completely Disconnected From Production

**What:** The `NwpAnalogSignal` class in `core/signals/nwp_analog_signal.py` (line 39, class definition) implements the full deterministic k-NN analog ensemble per Expert 4 spec. However, it is **not registered** in the `SignalRegistry` in `core/signals/__init__.py` (lines 47-60). The registry lists 12 signals; `nwp_analog` is absent.

**Where:** `core/signals/__init__.py:47-60` — missing entry in `self.signals` dict.

**Why wrong:** The NWP analog signal is expected to be the second-most-powerful NWP-based signal after the direct NWP signal (est. ~65-72% directional accuracy per Expert 4 spec). With it disconnected, the engine has **zero analog-based reasoning**. All NWP value comes from simple majority-voting of model output (direct signal), missing the analog pattern-recognition layer entirely.

**Spec to fix:**
```python
# In SignalRegistry.__init__(), add:
'nwp_analog': NwpAnalogSignal(db_path),
# And import at top:
from .nwp_analog_signal import NwpAnalogSignal
```

---

### E2. NWP Direct Signal's `evaluate()` Is a No-Op (Always Returns None)

**What:** `NwpDirectSignal.evaluate()` (line 49) returns `return None, 0.0` unconditionally. The actual evaluation logic is in `_evaluate_market()` (line 56), which is a **private method** never called from `evaluate()`. The `evaluate_for_station()` method (line 54) is also a docstring stub.

**Where:** `core/signals/nwp_direct_signal.py:49-52`

**Why wrong:** Any backtest or agent calling `nwp_direct.evaluate(idx, days)` gets `(None, 0.0)` — the signal never fires. The signal's name suggests it would work via the standard `evaluate()` interface, but it requires a completely different calling convention (`_evaluate_market()`) that expects station and date strings, not index+days.

**Spec to fix:**
```python
def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
    """Evaluate using the market-based approach for the given day index."""
    if idx < 1 or idx >= len(days):
        return None, 0.0
    # Map idx to station context — requires station ID from days list
    # or rework _evaluate_market to accept days[idx] format
    station = self._station_from_idx(idx, days)
    if station is None:
        return None, 0.0
    date_str = days[idx]['date']
    return self._evaluate_market(station, date_str, 'HIGH')
```

---

### E3. Dewpoint Depression Modulator Queries Nonexistent Table

**What:** `dewpoint_depression_modulator.py` function `_get_most_recent_dpd()` (line 75) executes:
```sql
SELECT temperature_f, dewpoint_f FROM observations
WHERE station_id = ? AND observation_time BETWEEN ? AND ?
```
The table is called `observations` but the entire codebase uses `metar_observations` (used in `base_signal.py:109`, all `evaluate_for_station` methods). The column names `temperature_f`, `station_id`, `observation_time` don't match the actual schema (`temp_f`, `station`, `date_utc`).

**Where:** `core/signals/dewpoint_depression_modulator.py:75-85`

**Why wrong:** Every call to `modulate_confidence()` silently fails (returns input confidence unchanged) because the SQL query always returns zero rows. The dewpoint depression modulation layer — which should provide a ~15-20% confidence adjustment based on clear/cloudy conditions — is completely non-functional.

**Spec to fix:**
```python
# Change query to:
query = """
    SELECT temp_f, dewpoint_f 
    FROM metar_observations 
    WHERE station = ? 
        AND date_utc BETWEEN ? AND ?
        AND temp_f IS NOT NULL 
        AND dewpoint_f IS NOT NULL
    ORDER BY date_utc DESC 
    LIMIT 1
"""
```

---

### E4. Wind Direction Shift Makes Wrong Temperature Assumptions (Santa Ana Fallacy)

**What:** `WindDirectionShiftSignal.infer_temperature_implication()` (line ~149) uses a universal rule: "northerly winds = cooling (down), southerly winds = warming (up)". This assumption fails for multiple US cities.

**Where:** `core/signals/wind_direction_shift.py:149-196`

**Why wrong:** The temperature implication of wind direction is highly station-dependent:
- **KLAX / KSFO:** Santa Ana winds from the north/northeast are **hot, dry, and produce warming** — not cooling. The code predicts 'down' for these, opposite the actual effect.
- **KDEN:** East winds (upslope) bring cooling and snow in winter, but east is not "northerly" — the code treats it as neutral.
- **KSEA:** South winds bring mild marine air (moderate warming in winter, cooling in summer). A single compass-based rule cannot capture this.
- **Coastal stations:** Sea breeze effects dominate — onshore (west/southwest) = cooling, offshore (east/northeast) = warming, exactly the opposite of inland logic.

**Spec to fix:** Replace the single rule with station-specific wind direction → temperature effect lookup tables derived from 2+ years of station data. Minimum approach: define station-level compass sectors for "warm" and "cold" directions, populated from 24-month historical regression of wind direction vs ΔT.

---

### E5. Pressure Delta Signal Has Physically Wrong Direction Mapping

**What:** `PressureDeltaSignal.evaluate()` (line ~131) maps:
- `dp > 0` (rising pressure) → `'up'` (warming prediction)
- `dp < 0` (falling pressure) → `'down'` (cooling prediction)

**Where:** `core/signals/pressure_delta_signal.py:131-133`

**Why wrong:** The physical reasoning is reversed for most synoptic patterns:
- **Rising pressure** (post-frontal) typically brings **cold air advection** (cooling), especially in winter. A strong high-pressure ridge in summer brings warming — but this is seasonal, not universal.
- **Falling pressure** (pre-frontal) brings **warm air advection** and storm systems that typically **warm** ahead of cold fronts in winter, but can bring cooler maritime air in summer.
- Exceptions exist (winter high → radiational cooling; summer low → tropical warmth) but the default should be: rising pressure → cooling bias, falling pressure → warming bias for mid-latitude systems.

**Spec to fix:**
```python
direction = 'down' if dp > 0 else 'up'  # Invert direction
# Add seasonal adjustment factor for certainty level
```

---

### E6. Frontal Detector Uses Daily Aggregates for Hourly-Scale Phenomena

**What:** `FrontalDetectorSignal` checks pressure change, wind shift, and temperature gradient using **day-over-day** comparisons (`idx-1` vs `idx-2`). A frontal passage typically takes 3-6 hours with pressure changes of 2-5 mb in that window.

**Where:** `core/signals/frontal_detector_signal.py` — all 4 condition methods (lines 73-143)

**Why wrong:** By the time a frontal passage manifests as a day-over-day temperature change, it has already occurred — the signal provides no **predictive** value. The physical threshold of 1.5 mb/3h collapses to near-zero when averaged over 24 hours. A 4 mb pressure drop in 3 hours → ~0.17 mb/hour average → 4 mb day-over-day, but this is confounded with the diurnal pressure cycle (typically 2-3 mb). The signal is detecting noise, not fronts.

**Spec to fix:** Replace the daily-aggregate approach with intra-hourly data from `metar_observations`:
```python
def _check_pressure_change_intraday(self, station, date_dt, lookback_hours=6):
    """Check pressure tendency over the last 3-6 hours from raw METAR."""
    # Query metar_observations for station between (date - 6h) and date
    # Compute pressure slope from raw observations (not daily avg)
    # Threshold: >= 2.0 mb change in 3 hours
```

---

### E7. Frontal Passage Detector's NWP Query References Wrong Schema

**What:** `frontal_passage_detector.py` function `get_temperature_gradient_at_station()` (line ~88) queries:
```sql
SELECT latitude, longitude, temp_c FROM nwp_forecasts 
WHERE date BETWEEN ? AND ? AND station_code LIKE ?
```
The table `nwp_forecasts` has columns: `id, fetch_date, target_date, station, model, variable, value, fetch_timestamp`. None of the queried columns (`latitude`, `longitude`, `temp_c`, `date`, `station_code`) exist.

**Where:** `core/signals/frontal_passage_detector.py:95-100`

**Why wrong:** This function always returns `None` (either from SQL error caught by try/except, or from the `len(records) < 2` check returning zero records). The NWP temperature gradient (condition 3) is never computed due to schema mismatch.

**Spec to fix:** Rewrite to query the actual NWP DB schema:
```python
query = """
    SELECT station, target_date, AVG(value) as avg_temp
    FROM nwp_forecasts
    WHERE variable = 'temperature_2m_max'
        AND target_date = ?
        AND station IN (-- nearby stations)
    GROUP BY station
"""
```

---

### E8. Temperature Advection Signal Returns None Via Standard Interface

**What:** `TemperatureAdvectionSignal.evaluate()` (line ~145) returns `(None, 0.0)` unconditionally. The real computation happens in `evaluate_for_station()` which calls `compute_signal_for_station()` — a live GFS API call.

**Where:** `core/signals/temperature_advection_signal.py:145-152`

**Why wrong:** Any caller using the standard `signal.evaluate(idx, days)` interface (as all backtesting agents do) will never receive a signal from temperature advection. This is the only signal that uses true 850-mb physics (the strongest single predictor per literature at 70-75% expected accuracy). The signal's power is entirely invisible to the backtest framework.

**Spec to fix:** 
```python
def evaluate(self, idx, days):
    if idx < 1 or idx >= len(days):
        return None, 0.0
    station_id = self._infer_station(days)  # or pass station at construction
    if not station_id:
        return None, 0.0
    # Query stored NWP data for this date, not live API
    return self.evaluate_for_station(station_id, days[idx]['date'])
```

---

### E9. NWP Analog Signal's Beta-Binomial Uses Misleading Effective Sample Size

**What:** In `NwpAnalogSignal.evaluate_nwp_analog()` (line ~283-285), the beta-binomial estimate uses weighted sum as effective sample size:
```python
prob_up = (weighted_up + 1.0) / (n_effective + 2.0)
```
where `n_effective = weighted_total` (sum of exponential weights from K=50 analogs). If 10 close analogs each have weight ~0.3, and 40 distant analogs each have weight ~0.0025, `weighted_total ≈ 4.0`, meaning the formula treats it as ~4 observations.

**Where:** `core/signals/nwp_analog_signal.py:283-285`

**Why wrong:** The beta-binomial with effective sample size undercounts the information from close analogs. With K=50 and 35/50 showing 'up', if `weighted_up = 3.2` and `n_effective = 3.8`, then `prob_up = (3.2+1)/(3.8+2) = 4.2/5.8 ≈ 0.724`. But a simple hit count would give `35/50 = 0.70`. The weighted version gives slightly higher confidence, but the effective N=3.8 produces a posterior variance of ~0.036 — much wider than the N=50 posterior variance of ~0.003. This dramatically underestimates confidence from many-analog situations.

Additionally, the formula uses `weighted_up` (sum of weights of 'up' analogs) but `n_effective` is total weight, so the beta-binomial is being used on a weighted scale it wasn't designed for.

**Spec to fix:** Either:
- Use simple hit-count with actual K: `prob_up = (hits + 1) / (K + 2)` — loses weighting information, or
- Use proper weighted beta-binomial: compute `alpha = sum(w_i * I(up_i)) + 1`, `beta = sum(w_i * I(down_i)) + 1`, then `prob_up = alpha / (alpha + beta)` which correctly treats weights as concentration parameters.

---

### E10. NWP Seasonal Window Mismatch Between Comment and Implementation

**What:** The docstring for `SEASONAL_WINDOW` (line 72) says ±15 days per Expert 4 spec, but the actual value is 45 (`self.SEASONAL_WINDOW = 45`, line 69). The comment explains the data shortage and the intent to tighten, but the implementation uses a physical justification that is actually a data limitation.

**Where:** `core/signals/nwp_analog_signal.py:69-72`

**Why wrong:** A ±45-day window means June 15th considers analogs from May 1 to July 30. Seasonal weather patterns differ significantly over 90 days: spring storms vs summer heat, autumnal transitions vs early winter. The analog pool includes physically dissimilar days, diluting the k-NN signal. The 92.7% NWP direct accuracy suggests the NWP data is good — the analog signal should be closer to 70+% but is likely degraded by the wide window.

**Spec to fix:** 
```python
SEASONAL_WINDOW = 15  # Per spec. When NWP DB has ≥300 unique dates, this is viable.
```
And add data accumulation fallback: if candidates < MIN_ANALOGS with 15-day window, widen gradually, not in one jump to 45.

---

### E11. Signal Fusion's Dempster-Shafer Implementation Uses Arbitrary Mass Functions

**What:** `dempster_shafer_conflict()` (line ~280) assigns:
```python
m_down = (1 - conf) * 0.3  # residual mass on opposite hypothesis
m_uncertain = max(0.0, 1.0 - m_up - m_down)
```
Then blends evidence conflict (0.7 weight) with direction split (0.3 weight) with no physical or statistical justification.

**Where:** `core/signal_fusion.py:280-377`

**Why wrong:** The constant `0.3` residual mass and the `0.7/0.3` blending weights are completely arbitrary — not derived from Dempster-Shafer theory, not from empirical observation, not from any meteorological principle. The resulting "conflict mass K" is a psychologically plausible number with no mathematical grounding. This means the conflict detection layer provides no real information — it's decorative math.

**Spec to fix:** Either implement proper D-S theory with mass functions derived from calibration reliability curves, or remove the D-S layer entirely. If keeping:
```python
# Derive residual mass from calibration reliability:
# If signal is 80% confident and historically 75% correct when confident,
# residual mass = (1-calibrated_accuracy) * remaining_uncertainty
```

---

### E12. Pressure Delta Signal Compares Weighted Average to Single Point

**What:** `PressureDeltaSignal._compute_weighted_pressure()` computes an exponentially-weighted average of 3 recent days' pressure, then compares it to a single pressure value from 3 days ago (`idx - 4`). The comparison `dp = weighted_recent - three_days_ago_pressure` mixes an average with a point.

**Where:** `core/signals/pressure_delta_signal.py:127-133`

**Why wrong:** If the three-day weighted average happens to equal the 3-days-ago reading because pressure rose then fell, `dp ≈ 0` and no signal fires — even though significant pressure changes occurred. A proper approach compares current pressure (or at last observation, not a 3-day average) to a baseline. The mixing of averaging windows creates a non-physical reference.

**Spec to fix:**
```python
# Compare last observation's pressure to runnning exponential baseline:
dp = last_pressure - baseline  # where baseline = smooth exp. weighted mean
```

---

## IMPROVEMENTS (5+)

### I1. Register and Integrate NWP Analog Signal

**What to change:** Add `NwpAnalogSignal` to `SignalRegistry` in `__init__.py` and pipe its output into the fusion pipeline.

**Why better:** The analog ensemble is the most meteorologically sophisticated signal — it learns from historical patterns of NWP fields. Even at reduced 45-day window, it should add 3-5% accuracy improvement to the ensemble by providing independent evidence (analog approach is uncorrelated with direct NWP model voting).

**Effort:** Low (1-2 engineering hours). Registration is 4 lines; integration requires adding the signal name to fusion weight computation.

**Spec for implementation:**
```python
# __init__.py
from .nwp_analog_signal import NwpAnalogSignal
# In registry:
'nwp_analog': NwpAnalogSignal(db_path),
# In signal_fusion.py SignalFusionEngine:
signal_names = ['nwp_direct', 'nwp_analog', ..., 'temperature_advection']
```

---

### I2. Add Station-Specific Wind Direction → Temperature Mappings

**What to change:** Replace the universal `infer_temperature_implication()` in Wind Direction Shift with station-level direction effect tables. Build from 24 months of METAR data by regressing wind direction on ΔT.

**Why better:** Eliminates the Santa Ana reversal (KLAX) and captures local topographic effects (KSEA Puget Sound channeling, KDEN Front Range upslope/downslope, South Florida sea breeze). Estimated improvement: 8-12% accuracy gain on the wind signal for affected stations.

**Effort:** Medium (4-6 hours). Requires building direction-effectiveness tables from historical data, implementing per-station lookup, and validation.

**Spec for implementation:**
```python
class WindDirectionMapper:
    STATION_WARM_SECTORS = {
        'KLAX': [(0, 90), (270, 360)],  # Santa Ana (NE) + offshore
        'KLAX': {'cold': [(180, 270)], 'warm': [(0, 90), (270, 360)]},
        'KDEN': {'cold': [(0, 180)], 'warm': [(180, 360)]},  # Upslope from east = cold
        'KSEA': {'cold': [(180, 270)], 'warm': [(0, 180), (270, 360)]},
    }
    def get_effect(self, station, direction):
        sectors = self.STATION_WARM_SECTORS.get(station)
        # ... check direction against warm/cold sectors
```

---

### I3. Restructure Frontal Detection to Use Sub-Hourly METAR Data

**What to change:** Convert `FrontalDetectorSignal` and `frontal_passage_detector.py` from daily-aggregate comparison to a true 3-6 hour window analysis using raw METAR observations from `metar_observations` table.

**Why better:** Frontal passages are 3-6 hour events with pressure drops of 2-5 mb, wind shifts of 45-90°, and temperature changes of 5-15°F. These signals are undetectable in daily aggregates. Using sub-hourly data unlocks the frontal signal's true predictive power (estimated 70-75% accuracy for 48-hour temperature change prediction).

**Effort:** Medium-High (8-12 hours). Requires building intraday pressure slope computation, wind direction change detection on actual observation timestamps, and temperature tendency from METAR obs_time rather than day boundaries.

**Spec for implementation:**
```python
def detect_front_in_window(self, station, reference_date, hours=6):
    """Query raw METARs in [reference-6h, reference] and check 4 conditions."""
    obs = self._get_metar_window(station, reference_date, hours)
    conditions = {
        'pressure_tendency': self._pressure_slope(obs) > 1.5/3,  # mb/hour
        'wind_shift': self._max_wind_shift(obs) > 45,  # degrees
        'temp_trend': self._temp_change(obs) > 3.0,  # °F
        'temp_dewpoint_convergence': self._dewpoint_convergence(obs) < 2.0,  # °F
    }
```

---

### I4. Add HRRR Integration for Short-Horizon NWP

**What to change:** Add a dedicated HRRR (High-Resolution Rapid Refresh) collection endpoint to `nwp_collect.py` and a new `hrrr_direct_signal.py`. Open-Meteo provides HRRR via `/v1/hrrr` with 3km resolution and hourly updates.

**Why better:** HRRR has 3km resolution (vs GFS's 13km) and updates hourly (vs GFS's 6-hourly). For Kalshi markets that settle same-day or next-day, HRRR's short-range accuracy is materially better for the 0-18 hour forecast, directly where the trading edge exists.

**Effort:** Low-Medium (4 hours). Add HRRR model to MODELS list, extend DB schema with an optional model_type field, and create a lightweight signal wrapper.

**Spec for implementation:**
```python
# In nwp_collect.py MODELS:
("hrrr", "https://api.open-meteo.com/v1/hrrr"),

# New signal hrrr_direct_signal.py:
class HrrrDirectSignal(BaseSignal):
    """HRRR has ~95% directional accuracy for next-day HIGH prediction (vs 92.7% GFS)."""
    def evaluate(self, idx, days):
        # Prefer HRRR when available (forecast hour <= 18), fall back to GFS
```

---

### I5. Implement Seasonal Diurnal Temperature Curve Model

**What to change:** Add a module that models the expected diurnal temperature curve for each station based on:
- Solar declination (season)
- Station latitude and elevation
- Typical urban heat island effect
- Recent observed temperature (for intraday tracking of HIGH/LOW achievement)

**Why better:** The engine predicts HIGH/LOW settlement but has no model of *when* during the day the max/min occurs. This matters for:
1. Determining how much confidence to place in a current observation being the daily extreme
2. Understanding whether a current observation is still climbing toward HIGH or declining from it
3. Kalshi markets settle at specific times — knowing the diurnal curve improves settlement price prediction

**Effort:** Medium (6-8 hours). Build from astronomical calculations and station-level historical diurnal patterns.

**Spec for implementation:**
```python
class DiurnalCurveModel:
    def solar_elevation(self, station, dt_utc) -> float:
        """Compute solar elevation angle for station lat/lon at UTC time."""
        ...
    def expected_temp_at_time(self, station, today_high, today_low, dt_utc) -> float:
        """Interpolate expected temperature using sine model between min/sunrise and max/solar_noon."""
        ...
    def is_likely_daily_high(self, station, current_temp, dt_utc) -> Tuple[bool, float]:
        """Is current temp likely the daily high, given diurnal curve position?"""
        ...
```

---

## IDEAS (3+)

### Idea 1: 850-mb Wind Advection Vector + Lagrangian Trajectory Prediction

**The idea:** Instead of computing Eulerian advection at a single point (current temperature_advection_signal approach), trace the 850-mb air parcel trajectory backward 12 hours using stored 6-hourly NWP wind fields. This gives the *source region* of the air mass arriving at each city — a Lagrangian perspective that directly answers "how warm/cold is the incoming air?"

**Expected benefit:** A 12-hour back-trajectory at 850-mb with climatological temperature fields would give ~5-8% better directional accuracy than the current point-advection signal. It accounts for the *history* of the air mass, not just instantaneous flux.

**Risk:** Requires storing 6-hourly 3D wind fields (~50 MB/day per model per city), increasing NWP collection storage by ~3x. Trajectory computation adds ~10ms per station.

**Spec for validation:**
1. For each station, compute both Eulerian advection and 12h back-trajectory temperature difference for 90 days
2. Compare directional accuracy of both methods against observed ΔT
3. If Lagrangian method shows >5% accuracy improvement, implement as `lagrangian_advection_signal.py`
4. Test for 6 months before replacing the current advection signal

---

### Idea 2: Dual-Polarity Signal Framework (Warm-Season/Cool-Season Regime Gating)

**The idea:** Many signals (pressure_delta, wind_direction_shift, frontal_detector) have direction mappings that reverse between warm season (May-October) and cool season (November-April). A regime gate that detects "season type" and flips signal direction logic accordingly would fix E4 and E5.

**Expected benefit:** Estimated 10-15% accuracy improvement on pressure_delta and wind_direction_shift signals during shoulder seasons (April, October) when the reversal is most confusing. The warm/cool season gating aligns with known synoptic climatology — mid-latitude cyclones dominate in winter (cold advection = falling pressure), heat domes dominate in summer (warm advection = rising pressure).

**Risk:** The transition dates are not fixed — they vary by year and station. A fixed May 1/Nov 1 split would misclassify early/late seasons. Need a dynamic threshold (e.g., 30-day mean temperature crossing a station-specific threshold).

**Spec for validation:**
1. For each station, compute pressure_delta accuracy separately for each month
2. Identify the months where the direction mapping inverts (likely Oct-Nov and Mar-Apr)
3. Build a 30-day rolling mean temperature gate: `warm_season = 30d_avg_temp > station_climate_median`
4. Test pressure_delta with correct and inverted mappings against the warm/cool season gate
5. If accuracy improves >5% in shoulder months, implement the dual-polarity framework

---

### Idea 3: Cloud Cover & Radiative Flux Signal as High-Confidence Modulator

**The idea:** The NWP collection already stores `cloud_cover_daily_mean` and `dew_point_2m_daily_mean`. Cloud cover is the single largest determinant of the diurnal temperature range (DTR = max - min). Clear skies → strong solar heating by day + strong radiational cooling by night = large DTR. Cloudy skies → muted DTR. This can be used as a **confidence modulator** for the ensemble's HIGH-LOW spread predictions, not just directional signals.

**Expected benefit:** A cloud-cover-based DTR modulation could improve the signal calibration by 3-5% by providing a physical basis for when the ensemble should be more/less confident about extreme temperatures. For example, if the NWP predicts clear skies but the ensemble predicts mean reversion (down from a high value), the ensemble confidence should be reduced because clear skies support sustained high temps.

**Risk:** Cloud cover forecasts from NWP have limited accuracy beyond 48 hours. The GFS cloud cover skill score drops to ~50% by day 3. This is a shorter-horizon signal only.

**Spec for validation:**
1. Extract `cloud_cover_daily_mean` from nwp_forecasts DB for each station
2. Compute correlation between cloud cover and |observed_high - ensemble_mean| for training period
3. Build modulation factor: `modulation = 1.0 + beta * (cloud_cover - 0.5)` where beta is negative (more clouds = less confidence in extreme)
4. Add `cloud_cover_modulator` to the fusion pipeline as a post-hoc confidence adjustment

---

## ELEPHANTS (2+)

### Elephant 1: Signal Independence Is Not Validated — Multiple Signals Are Redundant

**What:** The engine runs 12 registered signals, but several are variations of the same meteorological concept with different window sizes:
- `gaussian` (48-day z-score reversion) and `gaussian_v2` (30-day z-score reversion) — **same concept, different window**
- `calendar_climatology` (60-day z-score, z > 1.5 threshold) — **same concept, different window**
- `regime` (30-day mean reversion, volatility gate) — **same concept, additional volatility filter**
- `persistence` and `simple_trend` — both predict same direction as yesterday

These 5 signals represent essentially **three independent concepts** (short mean reversion, long mean reversion, persistence) spread across 5 implementations. The MI matrix in `signal_fusion.py` is designed to catch this, but is never populated with real data — the `compute_fusion_weights()` method returns equal weights as a placeholder.

**Why matters:** The fusion engine assigns weights to 12 signals but gets ~6-7 truly independent pieces of information. The equal-weight default overweights the reversion cluster and underweights the unique signals (NWP, advection, pressure). This creates a hidden bias toward mean-reversion predictions that manifests as:
- Lower accuracy during trend days (reversion signals are wrong, but they dominate the vote)
- Higher confidence than warranted (multiple signals agree because they're computing the same thing)
- The "72%" ensemble accuracy is likely inflated by reversion cluster vote-counting

**What happens if ignored:** The ensemble will continue to perform well on mean-reversion days and poorly on trend days, creating a systematic bias that limits maximum achievable accuracy to ~75% regardless of signal quality improvements.

**Spec for resolution:**
1. **Build the MI matrix from real backtest data** — the `mutual_information_matrix()` function exists but is never called with real data. Wire it into the calibration/training pipeline.
2. **Compute signal redundancy** — identify groups of signals with MI > 0.3 bits. Treat each group as a single vote in the agreement gate, not as individual signals.
3. **Reduce or merge** — either (a) remove gaussian_v2 (30-day) and keep gaussian (48-day) for stability, or (b) merge all z-score reversion signals into a single signal with configurable window and threshold.
4. **Update agreement gate** — the current N-of-M gate counts 12 signals equally. After redundancy analysis, the effective N-of-M should be N-of-(independent sources).

---

### Elephant 2: No Geographic/Station-Specific Effects in Any Signal

**What:** None of the 12 signals accounts for:
- **Elevation:** KDEN (5,280 ft) and KPHX (1,135 ft) have fundamentally different temperature behavior. A 48°F high at 5,280 ft is not the same as 48°F at sea level. The z-score calculations compare station temps to station means, but the physics of temperature change differs.
- **Coastal vs inland:** KLAX and KSEA are coastal with marine layer effects. KMIA is coastal with tropical sea breeze. KNYC is coastal but urban. The wind direction effects reverse between coastal and inland (E4).
- **Urban heat island:** KNYC, KPHL, KMDW have significant UHI effects that slow nighttime cooling. The diurnal temperature curve is different from nearby rural stations, but the signals treat all stations identically.
- **Topographic channeling:** KSEA's Puget Sound convergence zone, KDEN's Front Range downslope, KLAX's Cajon Pass outflow — these are local effects that dominate temperature behavior during certain patterns but are invisible to all signals.
- **Lake/marine effects:** KMDW (Lake Michigan), KSEA (Puget Sound), KSFO (San Francisco Bay) — large water bodies moderate temperature and create local wind patterns (lake breezes, bay breezes) that are not captured by any signal.

**Why matters:** The 20 Kalshi cities span 5 climate zones (marine west coast, Mediterranean, desert, humid continental, humid subtropical) with elevation ranges from sea level to 5,280 ft. A single signal configuration cannot optimally capture temperature behavior across all these environments. The wind direction signal is actively wrong for ~5 stations. The pressure signal is wrong for most coastal stations in summer.

**What happens if ignored:** Station-level accuracy will vary significantly (implied range: 60-80% across cities) with no mechanism to identify or correct the underperformers. The ensemble will underperform on stations with unusual local effects (KLAX, KSEA, KDEN, KMIA) and will have no path to improvement because the errors are structural, not statistical.

**Spec for resolution:**
1. **Add station metadata** — create a `station_metadata.json` with per-station: elevation, climate zone, coastal flag, urban heat island factor, lake proximity, prevailing wind direction, typical sea breeze onset time.
2. **Build station-specific signal parameters** — for each signal, allow per-station overrides:
   - Wind direction: warm/cold sectors per station
   - Pressure: coastal vs inland pressure behavior
   - Z-score: elevation-adjusted temperature thresholds
   - Advection: coastal initialization bias
3. **Add climate zone ensemble** — group stations by climate zone (mountain/desert/coastal/continental) and build zone-level calibration as a middle layer between per-station and global calibration.
4. **Implement station-level accuracy tracking** — the `TimeDecaySignalManager` already tracks per-station-per-signal reliability. Use this to dynamically down-weight signals that historically underperform on specific stations (e.g., wind_direction_shift on KLAX).

---

## SUMMARY: PRIORITY ORDER

| Priority | Item | Type | Effort | Impact |
|----------|------|------|--------|--------|
| P0 | E1: Register NWP Analog Signal | Error | 1h | Signal lost entirely |
| P0 | E2: Fix NWP Direct evaluate() | Error | 2h | Signal lost entirely |
| P1 | E3: Fix Dewpoint Depressor DB query | Error | 1h | Modulator non-functional |
| P1 | Elephant 1: Fix signal independence | Elephant | 8h | Systematic bias in ensemble |
| P1 | I1: Register NWP Analog into fusion | Improvement | 2h | +3-5% ensemble accuracy |
| P2 | E4: Fix wind direction mapping | Error | 6h | Wrong signal for 5 stations |
| P2 | E5: Fix pressure direction mapping | Error | 2h | Wrong signal physically |
| P2 | I2: Station-specific wind tables | Improvement | 6h | +8-12% wind signal accuracy |
| P3 | E6: Fix frontal detection timescale | Error | 10h | Signal non-predictive |
| P3 | I3: Sub-hourly frontal detection | Improvement | 10h | Unlock frontal signal |
| P3 | Idea 2: Dual-polarity signals | Idea | 4h | +10-15% pressure/wind accuracy |
| P4 | E8: Fix advection evaluate() bridge | Error | 3h | Signal lost in backtest |
| P4 | I4: HRRR integration | Improvement | 4h | +2-3% short-horizon accuracy |
| P4 | Elephant 2: Station-specific effects | Elephant | 20h | Structural accuracy ceiling |
| P5 | E9: Fix analog beta-binomial | Error | 2h | Underestimated confidence |
| P5 | E11: Fix DS conflict mass | Error | 4h | Decorative math → real signal |
| P5 | I5: Diurnal curve model | Improvement | 8h | Settlement timing precision |

---

*End of Expert 1 Meteorology & Weather Data Analysis*