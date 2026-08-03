# Gray Room Round 14 — Expert 3: Microstructure Engineer (Goldilocks Lane)
**Date:** 2026-08-03 | **Model:** luna-pro | **Base:** GRAY-ROOM-FUSION-LANES-FRAMING.md

---

## 0. Discovery: The Lane Already Half-Exists

Before designing anything new, I must report that **Lane 2 (Spike Reversion / Goldilocks) is already substantially built** — embedded in `core/metar_monitor.py` (~5,066 lines, 116 definitions). The `_process_temperature_event` function already:

- Detects `settlement_up` → `reversion_after_settlement` transitions within ≤5 min
- Maintains per-station-per-epoch trackers (`_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER`)
- Computes confidence via `_compute_microstructure_spike_confidence` with R7-A1 transient fix applied
- Handles both up-reversion (`microstructure_spike_reversion`) and down-reversion (`microstructure_spike_momentum_down`)
- Has a `near_boundary_momentum_up` signal at lines ~1600 (the "84.8°F → 85°F trend" variant)
- Routes through `core/lane_manager.py` as `LaneType.SPIKE_REVERSION`
- Polls the NWS API (separate from GEFS pipeline) at 60s intervals

**The framing says "separate lane" — that exists.** The framing says "restore to the simple alert concept" — the current implementation has already grown into a full signal+firing+cooldown+confidence+market-status machine.

---

## ERRORS

### [E1] 🔴 KNYC Does Not Produce 1-Minute Data — The Lane Cannot Detect Sub-2-Minute Spikes at KNYC
**Severity:** 🔴 CRITICAL — structural blocker
**Impact:** The Goldilocks concept as originally imagined (sub-minute tick detection, alerting on 1-2 tick temperature pops) is **infeasible at KNYC**. Central Park is NOT a major commercial airport. It:
- Does NOT have HF-ASOS (1-minute data)
- Does NOT broadcast 5-minute METARs
- Outputs standard METAR at ~hourly cadence (:51 past the hour)
- Temperature stored in **whole °F** (12-36 hour archival delay), converted to whole °C for transmission

**Fix:** If the Goldilocks lane must detect fleeting sub-minute ticks, it must operate on stations with HF-ASOS (major airports like KORD, KATL, KLAX, KPHX) — and even then, HF-ASOS temp is **whole °C only** (1.8°F quantization). At KNYC, the detection window is driven by the ~5-minute polling cycle of the NWS API, not sub-minute data.

**Disposition:** **ADVANCE** — accept this constraint. The lane already works on 5-minute data but must recalibrate expectations. Fleeting sub-2-minute ticks at KNYC are undetectable in real time.

---

### [E2] 🔴 The F→C→F Round-Trip Problem Destroys Bucket-Boundary Precision ±0.9°F
**Severity:** 🔴 CRITICAL
**Impact:** The existing detection pipeline already suffers this at every layer:
1. ASOS sensor: measures 0.1°F every 10s → averages to integer °F / 1 min
2. Rolling 5-min average: integer °F → convert to integer °C for METAR
3. NWS API returns `temperature.value` at 0.1°C (from T-group, which can differ from integer °C)
4. Code re-converts to °F: `temp_f = value * 9/5 + 32`
5. At 84.8°F (29.3°C): METAR reports 29°C → pipeline sees 84.2°F → **0.6°F error**

A bucket boundary at 85°F (29.4°C) requires the METAR to report 30°C (86°F equivalent) — meaning the actual temperature must be ≥29.5°C (85.1°F) to be detected as crossing 85°F. A genuine 84.9°F reading in the ASOS is invisible to the METAR pipeline.

**Fix:** Document the effective detection temperature floor. The current code at `metar_monitor.py` uses `int(math.floor(now_f))` which is correct for integer °F buckets but the input `now_f` has already been through the round-trip and is ±0.9°F uncertain.

**Disposition:** **ADVANCE** — document and accept. The pipeline already handles this implicitly; the issue is that edge cases (temp at 84.7°F reading as 85°F converted back) will generate false positives.

---

### [E3] 🟡 Settlement Uses 2-Minute Averages from NWS CLI — Not Real-Time METAR Feed
**Severity:** 🟡 HIGH (structural, not fixable)
**Impact:** The Goldilocks lane's detection pipeline polls `api.weather.gov` for METAR observations. **Kalshi settles daily temperature markets using the NWS Daily Climate Report (CLI product)** which uses **2-minute averages** of the ASOS sampled data. The METAR feed the lane watches is:
- A 5-minute rolling average (smoothed)
- Rounded to integer °C (precision lost)
- Published with 1-2 minute latency
- NOT the same computation as the CLI product

A brief spike (1-2 minutes) that the METAR pipeline partially captures may NOT appear in the CLI product (which uses 2-minute windows of the raw data) — or vice versa.

**Fix:** No algorithmic fix. The lane must treat METAR detection as a **correlated but not identical** signal to the settlement value. Trade sizing must account for the settlement-vs-METAR divergence risk. Add a reconciliation step: after settlement, log how often METAR-detected spikes matched CLI settlement highs.

**Disposition:** **ADVANCE** — document as a known divergence. Add post-settlement reconciliation tracking. This is a risk factor for position sizing, not a blocker.

---

### [E4] 🟡 The "Fleeting Tick" That Sets the Daily Max Already Triggers settlement_up — The Detection Is Backwards
**Severity:** 🟡 HIGH
**Impact:** Critical design defect in the current implementation. The existing `_process_temperature_event` detects a **microstructure spike reversion** by looking for:
1. `settlement_up` — temperature sets a NEW daily max → new bucket
2. `reversion_after_settlement` (within 5 min) — temperature drops back below old bucket

**But the Goldilocks concept is:** a spike that crosses a bucket boundary but does NOT set the daily max. Example:
- Current daily max: 86°F  
- Temp spikes to 85°F for 1 tick → crosses 85°F bucket
- But daily max remains at 86°F → NO `settlement_up` fires
- The detection engine never sees this event

What the current code detects is: "the daily max was 84°F, temp hit 85°F (new daily max), then dropped to 84°F within 5 minutes" — that's a **microstructure spike that became the daily high**. The RG7-A1 fix correctly downgrades this (set new daily max → low confidence), but the engine can't even detect the "fleeting tick that doesn't set the max" at all.

**Fix:** Add a new transition type: `instant_cross_revert` — temperature briefly crosses a bucket boundary (instant_bucket changes) and reverts within N observations WITHOUT triggering settlement_up. This is the "true Goldilocks" event. Implement as:
```python
if instant_changed and not settlement_changed:
    # Check if temp reverts to old instant bucket within N observations
    # This catches bucket-crossing ticks that don't set new daily max
```

**Disposition:** **ADVANCE** — this is the single most important fix. The existing detection misses the primary Goldilocks signal.

---

### [E5] 🟢 HF-ASOS Was Down Oct 2023–Jan 2026 — Backtest Data Has a 2-Year Gap
**Severity:** 🟢 LOW (historical)
**Impact:** If the lane relies on HF-ASOS 1-minute data for validation, the 28-month outage (Oct 2023–Jan 2026) means any backtest using this period will miss events that would have been detectable with the full feed. However, KNYC doesn't have HF-ASOS anyway, so this is moot for KNYC.

**Fix:** Acknowledge in backtest methodology. For non-KNYC stations with HF-ASOS, exclude the outage window from validation.

**Disposition:** **PARK** — relevant only if expanding to HF-ASOS stations.

---

## IDEAS

### [I1] Prediction Variant: Probability-Weighted Alert Based on Ensemble Decoupling Features
**Impact:** HIGH — directly addresses the "84.8°F → probability of hitting 85°F" requirement

The `near_boundary_momentum_up` signal in metar_monitor.py already detects monotonic temperature rise toward a bucket boundary using a 3-observation momentum window. **This is the prediction variant.** But it has two problems:

1. **No forecast context:** It only sees the last 3 observations (~15 minutes of data). It has no GEFS/ensemble information about whether the prevailing conditions (wind, cloud, boundary layer depth) support continued warming.
2. **Binary trigger:** Remarkably, the `core/goldilocks_predictive.py` file (LightGBM model, now classified as scope creep) computes weather-based features that describe the decoupling probability. These features (wind speed, cloud cover, BL height, ensemble decoupling probability) are exactly what's needed to convert the momentum detection into a probability score.

**Idea:** Strip the ML model, but keep the feature framework. Create a lightweight rule-based probability from the wind+cloud+BL features:

```
P = 0.35  # base rate (from 9% Goldilocks × 4x on favorable conditions)

P += -0.05 × (wind_speed_kt / 10)     # wind penalty: calm = higher prob
P += -0.10 × cloud_cover_frac          # cloud penalty: clear = higher prob
P += -0.04 × (bl_height_m / 500)       # BL penalty: shallow = higher prob

P = clamp(P, 0.02, 0.50)
```

Then combine with the momentum signal: `P(goldilocks | approaching boundary) = P_weather × 0.3 + P_momentum × 0.7` where P_momentum is derived from the distance-to-boundary and rate of approach.

**Test:** Backtest against the existing microphone spike tracker logs. For each detected settlement_up event, compute the pre-event probability and see if it predicts the reversion pattern.

**Disposition:** **ADVANCE** — buildable in <1 day, no ML required, directly addresses the prediction variant requirement.

---

### [I2] Cross-Station Corroboration for Non-KNYC Stations
**Impact:** MEDIUM — reduces false positives

For major airports within Kalshi's settlement network that DO have 1-minute HF-ASOS, add a cross-station check: if a spike is detected at KORD, check KMDW (within ~20km). If both show similar temperature deviation simultaneously, the signal is meteorological (real airmass change, not sensor noise). If only one station shows the spike, it's likely a local sensor artifact or noise.

**Architecture:** Lane 3 (Spatial Coherence) is already defined in `lane_manager.py` for exactly this purpose. Wire the spike reversion lane to spatial coherence lane as a confidence modifier.

**Test:** For a 30-day period, log all spike events at KORD/KMDW and compute the correlation coefficient between spike events across stations. If R < 0.3, this filter saves no false positives.

**Disposition:** **PARK** — requires expanding Goldilocks beyond KNYC first, which E1 says is structurally blocked for the sub-minute concept.

---

### [I3] CLI Product Direct Monitoring as Settlement Reconciliation
**Impact:** MEDIUM — closes the feedback loop

The NWS CLI product (the actual Kalshi settlement source) is published at `https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC`. The lane currently relies on METAR-to-CLI correlation. Add a cron job that:
1. Scrapes the CLI product daily after settlement (~12:00 UTC, ~08:00 ET)
2. Extracts the reported daily max and min
3. Compares against the lane's tracked daily max from METAR
4. Logs discrepancies in a `settlement_divergence_log`

This is the only way to validate signal detection against actual settlement outcomes. Without it, the lane is flying blind — trading on METAR signals that may not match the settlement value.

**Effort:** ~4 hours (scraper + comparison + log storage)

**Disposition:** **ADVANCE** — low effort, closes a critical feedback gap.

---

## IMPROVEMENTS / SPECS

### [S1] Spec: True Goldilocks Instant-Cross Detection (The Missing Signal Type)
**Effort:** 1-2 days
**Detail:**

The existing detection misses the scenario where temp briefly crosses a bucket boundary without setting a new daily max. This is the ORIGINAL Goldilocks concept. Implement:

```python
# In _process_temperature_event, after detecting instant_changed:
if instant_changed and not settlement_changed:
    # Crossed a bucket boundary without setting new daily max
    # Store the crossing event for reversion detection
    crossing_key = (station, epoch_id, curr_floor)  # curr_floor = crossed bucket
    _PENDING_CROSSINGS[crossing_key] = {
        "obs_time": obs_time,
        "temp_at_cross": now_f,
        "bucket_crossed": curr_floor,
        "direction": "up" if now_f > prev_f else "down",
        "crossings_remaining": 3,  # Wait for up to 3 more observations
    }

# In subsequent observations for the same epoch:
for crossing_key in list(_PENDING_CROSSINGS.keys()):
    if crossing_key[0] != station or crossing_key[1] != epoch_id:
        continue
    info = _PENDING_CROSSINGS[crossing_key]
    bucket = info["bucket_crossed"]
    
    # Revert check: has temp gone back below the crossed bucket?
    if info["direction"] == "up" and now_f < bucket:
        # GOLDILOCKS CONFIRMED: temp crossed bucket and reverted
        # Fire alert with confidence
        info["crossings_remaining"] -= 1
        if info["crossings_remaining"] <= 0:
            # Emit goldilocks_instant_cross signal
            _emit_goldilocks_event(station, obs_time, bucket, info)
            del _PENDING_CROSSINGS[crossing_key]
```

**Parameters:**
- `crossings_remaining`: 3 (= ~15 min at 5-min polling). Longer = fewer false positives but more missed events
- `revert_threshold`: `temp < bucket - 0.5°F` to confirm reversion (not just hovering at boundary)
- `min_dwell`: The spike must have been above bucket for at least 1 observation (avoiding single-reading noise)

**Edge Cases:**
- **Previous daily max already above the crossed bucket:** The crossing is definitely a "fleeting tick" that won't affect settlement → HIGH confidence
- **Previous daily max is at the crossed bucket:** The crossing may or may not set a new daily max depending on exact rounding → MEDIUM confidence
- **Temp keeps rising after crossing:** Cancel the pending crossing detection, this was the start of a real trend

**Integration with LaneManager:**
```python
LaneType.from_signal("goldilocks_instant_cross") → LaneType.SPIKE_REVERSION
```

**Disposition:** **ADVANCE** — highest-priority spec.

---

### [S2] Spec: Trend Extrapolation from Partial Ticks (The 84.8°F → 85°F Prediction Variant)
**Effort:** 2-3 days
**Detail:**

The existing `near_boundary_momentum_up` signal already detects monotonic temperature rise toward a bucket boundary. It's currently a binary signal (fires or doesn't). Upgrade to a continuous probability:

**Algorithm:**
```
Given: last 3 successive observations (each ~5 min apart)
  t0 = obs[-3].temp_f
  t1 = obs[-2].temp_f  
  t2 = obs[-1].temp_f
  t_now = obs[0].temp_f

Compute:
  momentum = (t2 - t0) / 10min  # °F per minute
  distance_to_boundary = bucket_boundary - t_now  # e.g., 85.0 - 84.8 = 0.2
  
  if momentum <= 0:
    P(crossing) = 0.0 (not warming)
  else:
    minutes_to_cross = distance_to_boundary / momentum  # 0.2 / 0.04 = 5 min
    # Is crossing plausible within the next N observations?
    if minutes_to_cross <= 30:  # Within 6 observations
      P_base = 1.0 - (minutes_to_cross / 30)  # Linear: faster = higher prob
    else:
      P_base = 0.0

Apply weather modifiers (from I1):
  P_weather = rule_based_decoupling_probability(wind, cloud, BL_height)
  
P(final) = 0.5 × P_base + 0.5 × P_weather

Alert thresholds:
  P(final) >= 0.50 → "Strong Boundary Approach" alert
  P(final) >= 0.35 → "Moderate Boundary Approach" notice
  P(final) >= 0.20 → info-only log entry
```

**Integration:** Replace the existing binary `near_boundary_momentum_up` with this continuous probability. Keep existing cooldown logic but adjust thresholds.

**Disposition:** **ADVANCE** — directly requested in the framing, replaces the LightGBM model with deterministic math.

---

### [S3] Spec: Minimal Viable Backtest Protocol
**Effort:** 3-5 days
**Detail:**

The framing asks: "how to validate without live tick data?" The system has:

1. **NCEI ASOS 5-minute archive** (free, next-day): `asos-5min-KNYC-YYYYMM.dat`
2. **IEM ASOS 1-minute archive** (free, 12-36hr delay, whole °F): `mesonet.agron.iastate.edu/request/asos/1min.phtml`
3. **Kalshi settlement data** (in DB, 6,151 rows, 1,750 dates, 20 stations)

**Protocol:**
```
Step 1: Replay NCEI 5-minute data through metar_monitor._process_temperature_event
  - Simulate 5-min polling with actual observed timestamps
  - Log all transition events (settlement_up, reversion, instant_cross)
  - Log all microstructure spike detections with their confidence scores
  - Output: detection event log for 2021-2026

Step 2: Compute "ground truth" daily max/min from two sources:
  a) Kalshi settlements (the actual outcome)
  b) IEM 1-minute archive (max of all 1-min readings for that day)
  Difference (b) - METAR max = "how much did METAR miss?"
  
Step 3: Compare detection event log against ground truth:
  - True positive: detection fired AND METAR max ≠ IEM 1-min max AND Kalshi settlement confirmed
  - False positive: detection fired BUT METAR max == IEM 1-min max (no spike occurred)
  - False negative: detection didn't fire BUT METAR max ≠ IEM 1-min max (missed spike)
  
Step 4: Report:
  - Precision = TP / (TP + FP)
  - Recall = TP / (TP + FN)  
  - Event capture rate = what % of Goldilocks events does the pipeline detect?
  - Baseline: random (9% climatology)
  - Target: Precision > 0.70, Recall > 0.50
```

**Tools:**
- `scripts/goldilocks_labeling.py` — already exists, labels historical data
- `scripts/goldilocks_feature_engineering.py` — already exists
- Use `core/replay_engine.py` — replay mechanism exists in the codebase
- NCEI data: download from `https://www.ncei.noaa.gov/data/automated-surface-observing-system-five-minute/access/`

**Disposition:** **ADVANCE** — executable on existing NCEI/IEM archives.

---

### [S4] Spec: Lane Separation — Data Flow Isolation
**Effort:** Already done — document the architecture
**Detail:**

The separated architecture is already in place:

```
┌─────────────────────┐     ┌───────────────────────┐
│  GEFS Cron Pipeline │     │  METAR Monitor Loop    │
│  (Lane 1:           │     │  (Lane 2: Spike Rev.)  │
│   Directional)      │     │                       │
│                     │     │  60s polling cycle     │
│  GEFS archive DB    │     │  api.weather.gov       │
│  ECMWF backfill     │     │  (separate from GEFS)  │
│  18/00/06/12Z runs  │     │                        │
└─────────┬───────────┘     └───────────┬───────────┘
          │                             │
          └──────────────┬──────────────┘
                         ▼
               ┌─────────────────────┐
               │   LaneManager       │
               │   resolve_trades()  │
               │   per-lane P&L      │
               │   overlap prevention │
               └─────────────────────┘
                         │
                         ▼
               ┌─────────────────────┐
               │    Trade Executor   │
               │    (L1+L2 signals)  │
               └─────────────────────┘
```

**What is NOT isolated yet:**
- The `near_boundary_momentum_up` signal (trend approach to bucket boundary) uses METAR data and belongs in Lane 2, but it's currently evaluated inside the same function as the microstructure spike detection — no code-level separation.
- Signal state persistence (`_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER`) uses the same DB as the main pipeline. Should use a separate SQLite DB for Lane 2 state.

**Fix:**
```python
# Add GOLDILOCKS_DB config parameter
GOLDILOCKS_DB = os.getenv("GOLDILOCKS_DB", "/opt/render/project/src/data/goldilocks_state.db")
```
Route all Lane 2/SIGNAL_MICROSTRUCTURE state persistence to this separate DB. The GEFS pipeline never touches it.

**Disposition:** **ADVANCE** — documented architecture with one concrete build item (separate state DB).

---

## ELEPHANTS

### [Elephant 1] 🐘 The "Fleeting Tick" Concept Was Founded on a Mistaken Observation
The origin story: "A trader watched the NWS live feed and saw a 1-2 minute blip at KNYC." The Goldilocks-SIGNAL-LUNA analysis concluded the feed was the **NWS Time Series Viewer** which displays **5-minute ASOS data**. A 1-2 minute blip in the 5-minute data means the spike was visible for **one 5-minute window, then gone** — the trader didn't see "sub-minute" data, they saw a single 5-minute observation.

**The uncomfortable truth:** The trader's "fleeting tick" was never sub-second or sub-minute. It was visible in the **public 5-minute data** for at least one full bucket. The "whale advantage" was not sub-minute data access — it was **automated threshold detection** on the same data the trader was watching, plus faster reaction.

**The Goldilocks lane as designed (sub-minute tick detection) solves a problem that may not exist.** What exists is a **reaction-time and automation** gap, not a data-access gap. A trading bot polling the same public NWS API at 60s intervals and applying the instant_cross_revert detection (Spec S1) would have detected the same event the "whale" detected.

**Disposition:** **ADVANCE** — swap the architecture's data source requirement from "sub-minute exotic feed" to "5-minute public feed with automated detection." The edge is automation, not data exclusivity.

---

### [Elephant 2] 🐘 The Existing Implementation Has Grown to 5,000+ Lines and Lost Simplicity
`metar_monitor.py` is 5,066 lines with 116 functions. The Goldilocks/microstructure spike detection is woven through ~10 separate locations across the file — `_process_temperature_event`, `_evaluate_deterministic_signal_layer`, `_compute_microstructure_spike_confidence`, `_emit_signal_alert`, `_emit_alert`, plus state persistence, cooldown management, and the `near_boundary_momentum_up` and `near_boundary_momentum_down` variants.

The framing says "restore to the simple alert concept" and "the ML model was scope creep." But the **non-ML code is also scope creep**. What was originally intended as a simple alert rule (temp crosses bucket + reverts → fire alert) is now a full signal fusion layer with:
- Per-station cooldown (300s)
- Per-boundary cooldown (600s)
- Epoch-level deduplication
- Hydration cache validation
- Eligible markets check
- Tier 1 bypass (for no-eligible-market scenarios)
- Near-miss auditing (7 different near-miss types)
- Two confidence computation functions
- Momentum window calculations (3 observation windows)
- State persistence to SQLite
- Post-settlement reconciliation

**The recommendation: Extract Lane 2 into its own module.**

Move all Goldilocks/microstructure code from `metar_monitor.py` into a new file: `core/lane2_goldilocks.py` or `core/spike_detector.py`. The interface should be:
- `ingest_observation(station, temp_f, obs_time, daily_max)` → `Optional[AlertEvent]`
- `get_probability(station)` → `float (0-1)`
- `reset_epoch(station)` — called at settlement boundary

That's it. No cooldowns, no hydration checks, no market eligibility, no near-miss auditing in the detection module itself. Those are LaneManager concerns.

**Disposition:** **ADVANCE** — refactor for the panel to debate. This is the "restore to simple" action.

---

### [Elephant 3] 🐘 The Settlement Pipeline Divergence Is a Silent Portfolio Risk
The Goldilocks lane watches METAR data. Kalshi settles on NWS CLI data. These are different pipelines with different aggregation windows (5-min rolling avg vs 2-min avg), different precision (whole °C vs whole °F), and different rounding conventions.

**If the lane fires a trade on a METAR-detected spike and Kalshi settles using CLI data that didn't see the spike, the trade loses.** This is not a theoretical edge case — the METAR pipeline explicitly uses 5-minute rolling averages that suppress sub-5-minute transients, while the CLI 2-minute averages are more sensitive to brief extremes. They diverge.

Currently, **there is ZERO post-settlement verification** — no cross-check between detected spikes and actual Kalshi settlement outcomes. The lane is trading blind on this dimension.

**Fix:** Implement I3 (CLI reconciliation) before any real-money trading in this lane.

**Disposition:** **ADVANCE** — prerequisite to paper trading.

---

## PANEL DISCUSSION (Cross-Expert Issues)

### [P1] Momentum Window Size vs METAR Polling Frequency (E3/E5/E6)
The current `near_boundary_momentum_up` signal uses a 3-observation window. At the 60s polling interval in the NWS API, this gives a 2-3 minute view. At 5-minute METAR cadence, a 3-obs window covers 10-15 minutes.

**Conflict:** E5 (Implementation Engineer) may say 60s polling is wasteful when the data only refreshes every ~5 min. E6 (Meteorological Analyst) may say that temperature cannot change meaningfully in 2-3 minutes (physical constraint), making the window too short.

**Proposed resolution:** 
- Poll at 60s but use NWS API's `If-Modified-Since` header to minimize bandwidth
- Set momentum window to 3 observations + only use windows where at least 2 observations have unique temperature values
- E6 should determine the minimum physically meaningful temperature change rate: if the maximum sustained warming rate at KNYC is ~4°F/h under extreme conditions, that's ~0.33°F in 5 minutes. The 0.002 °F/sec threshold is ~0.6°F in 5 minutes — too high. Recommend lowering to 0.001 °F/sec.

**Disposition:** **ADVANCE** for rate limit investigation; **PARK** for the physics of temperature change rates (defer to E6).

---

### [P2] Landed Microstructure Work vs. Gray Room Lane Doctrine (E2/E3)
**Conflict:** The existing implementation is in Lane 2 (Spike Reversion). The Gray Room framing says "separate lane architecture." E2 (Signal Fusion Architect) may want all lanes to converge into the Bayesian cascade. The microstructure lane by design trades different phenomena (transient deviations, not trend predictions) on different timescales (minutes vs hours-days) — it should NOT enter the cascade.

**Resolution:** Lane 2 stays independent. The Bayesian cascade is for Lane 1 (directional ensemble signals) only. Lane 2 signals are "side bets" that the cascade doesn't touch. The LaneManager routes them independently but tracks per-lane P&L so we can see which lane is winning.

**Disposition:** **ADVANCE** — this is already how LaneManager works. No code change needed, just documentation.

---

## CLEANUP STATUS

| Category | Total | ADVANCE | PARK | KILL |
|----------|-------|---------|------|------|
| **ERRORS** | 5 | 4 | 1 | 0 |
| **IDEAS** | 3 | 2 | 1 | 0 |
| **IMPROVEMENTS / SPECS** | 4 | 4 | 0 | 0 |
| **ELEPHANTS** | 3 | 3 | 0 | 0 |
| **PANEL** | 2 | 1 | 1 | 0 |
| **TOTAL** | 17 | 14 | 3 | 0 |

---

## WHAT TO DO NEXT

| Order | Item | Effort | Type | Depends On | Expert |
|-------|------|--------|------|------------|--------|
| 1 | Implement **instant_cross_revert** detection (Spec S1) | 1-2d | SPEC | None | E3/Gilfoyle |
| 2 | Implement **trend extrapolation** probability (Spec S2) | 2-3d | SPEC | S1 (shares state) | E3/Gilfoyle |
| 3 | Run **minimal viable backtest** (Spec S3) | 3-5d | SPEC | S1 (needs event log) | E3/Gilfoyle |
| 4 | **Refactor Lane 2 out of metar_monitor.py** into `core/lane2_goldilocks.py` (Elephant 2) | 2-3d | SPEC | None | E5/Gilfoyle |
| 5 | Implement **CLI reconciliation** (I3 + Elephant 3) | 0.5d | IDEA | None | E3/Gilfoyle |
| 6 | Separate **Lane 2 state DB** (Spec S4) | 0.5d | SPEC | #4 (refactor) | E5 |
| 7 | Rule-based weather **decoupling probability** (I1) | 0.5d | IDEA | #1 (alert triggers) | E3/E6 |
| 8 | KNYC data latency **documentation** (E1, E2) | 0.25d | ERROR | None | Any |
| 9 | **Momentum threshold recalibration** 0.002→0.001 (P1) | 0.25d | PANEL | Backtest results | E3/E6 |
| 10 | Post-settlement **reconciliation tracking** (I3 production) | 1d | IDEA |