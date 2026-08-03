# Gray Room — Expert 4: Pattern Matching Specialist (Trajectory Lane)

**Date:** 2026-08-03 08:52 UTC
**Model:** `openai/gpt-5.6-luna-pro`
**Role:** Pattern Matching Specialist — Trajectory Lane Design
**Pre-read:** Gray Room Fusion Lanes Framing (not yet written separately — see Round 8–12 synthesis docs)
**Context:** GEFS-only sweep positive ($85K gross/$24-47K fee-netted corrected). ECMWF backfill complete. 82-member sweep on HOLD pending 5 prerequisites.

---

## Executive Summary

The trajectory lane as originally conceived — "85°F with X humidity and Y pressure. A string of epochs brought us here. When we've seen this pattern before, what bucket(s) did we land in?" — is a **meteorological epoch-sequence matching system**, NOT a settlement-epoch analog system (Phase 3) and NOT a linear-trend gate (`test_trajectory_gate.py`).

The existing Phase 3 `p3_trajectory_tracer.py` matches **settlement epochs** (intraday temperature dynamics — jumps, reversions, excursions) which is a fundamentally different domain. The `core/trajectory/` `.pyc` files implement intraday temperature path prediction with Kalman filtering — also different. The `test_trajectory_gate.py` is a simple 5-day linear regression trend gate.

**What's missing:** A true meteorological trajectory matching system that answers: "Given the last N days of [temperature, humidity, pressure, wind] at this station, what settlement buckets did historically similar trajectories produce?"

---

## 1. Errors Identified

### E1. CRITICAL: The Phase 3 trajectory code matches the wrong kind of epoch

**What:** `p3_trajectory_tracer.py` operates on **settlement epochs** — periods between state transitions in the intraday temperature trace. Its features (settlement_jump_magnitude, reversion_occurred, max_excursion_above_settlement) describe market microstructure, not meteorological state. The `core/trajectory/` `.pyc` modules are Kalman-filtered hourly temperature path predictors.

**Why this is wrong for the trajectory lane:** The trajectory lane concept requires matching sequences of **meteorological epochs** (daily T, RH, P, wind vectors) against historical sequences. Settlement-epoch matching finds "days where the temperature did similar intraday wiggles" — not "days that followed a similar multi-day weather trajectory." These are orthogonal dimensions.

**Impact:** If anyone tries to repurpose `p3_trajectory_tracer.py` or the `trajectory_module.pyc` as the trajectory lane, they will get a system that matches market structure, not weather patterns. This would produce false analogs (same settlement jump, completely different weather trajectory).

**Fix:** Build a new trajectory matching module that operates on daily meteorological state vectors (T, RH, P, wind) aggregated from METAR observations, not settlement epochs.

**DISPOSITION: ADVANCE** — Recognize this as a distinct system, not a Phase 3 rebrand.

---

### E2. HIGH: `test_trajectory_gate.py` is a gate, not a matching system — scope creep

**What:** The existing test script implements:
- 5-day linear regression of settlement temps → up/down/flat direction
- 0.5°F/day slope threshold
- Veto on contradiction, 1.5× scale-up on confirmation

**Problems:**
1. **Linear regression on 5 points** is statistically meaningless. Five daily temps over 5 days with ±1°F observation noise gives slope standard error of ~±0.5°F/day. The threshold (0.5°F/day) is the same magnitude as the noise floor.
2. **Settlement temps, not forecast temps.** It uses actual historical settlement temperatures to compute the trajectory. This is a static label, not a predictive feature.
3. **Binary override logic** (veto or scale up) violates the "guide, not gate" requirement. The task says to be a trade GUIDE that helps trade selection, not a gate that breaks trades.
4. **Only temperature.** No humidity, pressure, wind. The 85°F with X humidity and Y pressure original concept is reduced to a single-variable linear slope.

**Impact:** If this script is mistaken for the trajectory lane, it will:
- Miss trajectory patterns that don't have a clean linear trend (e.g., warm→warm→warm trend vs warm→cool→warm oscillation)
- Fail to provide bucket-level recommendations (only directional up/down)
- Act as a hard veto, not a confidence modifier

**Fix:** KILL the gate approach. Replace with sequence-matching. The gate's functionality (veto trades on trend contradiction) could be a **downstream consumer** of the trajectory matching system, not the matching system itself.

**DISPOSITION: ADVANCE** — Explicitly designate this as a scope-creep artifact that should not be treated as the trajectory lane.

---

### E3. HIGH: Existing trajectory `.pyc` files are source-less and unmaintainable

**What:** The `core/trajectory/` directory contains only compiled `.pyc` files — `trajectory_module.pyc`, `trajectory_kalman.pyc`, `trajectory_signal.pyc`, `trajectory_features.pyc`, `trajectory_backtest.pyc`, `trajectory_spline.pyc` — with no corresponding `.py` source files. From bytecode analysis, `trajectory_module.py` orchestrates Kalman filtering, gap filling, analog matching, and signal derivation for intraday temperature path prediction.

**Problems:**
1. No source = unmodifiable, undebuggable, un-reviewable
2. Depends on Kalman filtering (numpy/scipy) but has no source to verify correctness
3. Designed for intraday temperature path prediction, not multi-day meteorological sequence matching
4. Uses METAR + NWP forecasts + climatology as inputs — not the settlement-epoch corpus

**Impact:** If someone tries to "just use the existing trajectory module" as the trajectory lane, they will be fighting a black-box Kalman filter designed for a different problem. The `__pycache__` cannot be extended to match epoch sequences of temperature+humidity+pressure.

**Fix:** Reconstruct the source or mark the directory as abandoned. Document what the `.pyc` files do (from the module code object: `compute_trajectory(station, trading_date, market_type, metar_history, nwp_forecasts, climatology, runtime_state)` → Kalman-gap-filled hourly path). Build the new trajectory matching module separately.

**DISPOSITION: ADVANCE** — The `.pyc` files are not the trajectory lane. They are a different system that happens to share the name "trajectory."

---

## 2. Design Specification: Trajectory Lane Matching System

### 2.1 Core Concept

**What:** A meteorological epoch-sequence matching system. Given the last N epochs (e.g., 3-5 days) of observed weather conditions at a station, find historically similar sequences in the corpus. From the matched sequences, determine which settlement bucket(s) the trajectory most commonly led to.

```
┌─────────────────────────────┐
│  Last N days of METAR obs   │
│  [T, RH, P, Wdir, Wspeed]  │  ← Query trajectory
│  Day 1  Day 2  ...  Day N   │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Sequence matching engine   │
│  (DTW or subsequence match) │
│                              │
│  Corpus: 1,750 dates ×      │
│  21 stations = 36,750       │
│  station-date sequences     │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Bucket outcome distribution│
│  Out of 127 similar         │
│  sequences:                 │
│  Below 85°F → 83 (65%)      │
│  At least 85°F → 44 (35%)   │
│  At least 90°F → 12 (9%)    │
└─────────────────────────────┘
```

### 2.2 Epoch Sequence Matching Method

**Use Dynamic Time Warping (DTW) with subsequence matching, NOT k-NN on raw vectors.**

Why DTW:
- Handles variable-length sequences naturally (5-day vs 3-day trajectories)
- Warps time to find the best alignment — "warm→hot→warm" matches "warm→hot→hot→warm" structurally
- Works with our 3-5 day query window and the corpus's irregularly spaced dates (weekends, holidays, missing METAR)
- Computationally tractable: 36,750 sequences × 5 days × 5 features × 21 stations = ~2M distance calculations. With optimized DTW early-abandon, this is <100ms per query.

**Not:** Euclidean distance on raw vectors (time-shift-invariant), not cosine similarity (loses magnitude), not Pearson correlation (loses scale).

**Sequence length:**
- Primary: N=5 days (captures synoptic pattern — frontal passages, heatwave build-up, cold air advection)
- Secondary: N=3 days (captures short-term persistence — useful in summer for heat dome dynamics)
- The system should compute both and let the matching scores determine which is more discriminative per query

**Missing data handling:**
- Gaps of 1-2 days (rain, missing METAR): fill with interpolated GEFS ensemble mean for the missing day. Mark as "gap-filled" in metadata for downstream confidence discount.
- Gaps >2 days: exclude the trajectory from that station-date, but don't drop the whole query — compute on available epochs.

### 2.3 Minimal Feature Set

**Primary (essential):**

| Feature | Why | Source | Normalization |
|---------|-----|--------|:------------:|
| **Temperature (°F)** | Primary driver of settlement bracket | METAR daily T_max/T_min | Z-score per station×season |
| **Humidity (RH%)** | Modulates heat perception; heat index | METAR daily mean RH | Z-score per station |
| **Pressure (mb/hPa)** | Synoptic regime identifier — high pressure = stable, low = dynamic | METAR daily mean MSLP | Z-score per station |
| **Wind speed (knots)** | Advection rate — how fast air mass is moving | METAR daily mean wind | Z-score per station×season |
| **Wind direction (8-point)** | Air mass origin — polar, maritime, continental | METAR daily mean direction | 8-bin categorical |

**Secondary (minimally sufficient — add if data exists):**

| Feature | Why | Priority |
|---------|-----|:--------:|
| **Day of year (circular)** | Seasonal context — same T in January ≠ same T in July | HIGH — already available, $0 cost |
| **Cloud cover (oktas)** | Radiational cooling potential — clear nights → colder | MEDIUM — METAR has it, may have gaps |
| **Precipitation (in)** | Cold front passage marker, post-frontal regime | MEDIUM — METAR has it |
| **Dewpoint depression (°F)** | Humidity persistence measure; heat wave indicator | LOW — derived from T + RH |

**What NOT to include:**
- Visibility (too noisy, METAR QC issues per Round 8 E3)
- Snow depth (sparse, only relevant for 3-4 stations)
- Soil temperature (not in METAR, would need NWP)
- Solar radiation (not reliably in METAR over history)

**Total primary features: 5 (T, RH, P, WS, WD). With secondary seasonal context: 6.**

### 2.4 Corpus Construction

**Corpus = {(station, date, [T, RH, P, WS, WD] × 5 days)}**

The corpus is built from:
- **METAR observations:** 1,468,161 records available (from pipeline state). Each station-date gets aggregated daily values: T_max (for HIGH market), T_min (for LOW market), mean RH, mean MSLP, mean wind speed, prevailing wind direction.
- **Settlement data:** 6,171 records across 1,750 unique dates × 21 stations. This provides the outcome label: which bucket did the settlement temperature fall into?
- **Date range:** ~2021-01 through 2026-07 (~5.5 years of data)

**Corpus size estimate:**

| Station-days with METAR+T+RH+P+WS+WD | ~35,000 |
|:-------------------------------------|:-------:|
| Station-days also linked to settlement | ~6,000 |
| Unique sequential 5-day trajectories | ~32,000 |
| Unique 3-day trajectories | ~34,000 |

### 2.5 Data Sufficiency Assessment

**Question:** Do we have enough epochs for meaningful pattern matching?

**Answer:** **Yes, marginally sufficient for pooled analysis. No, not sufficient for per-station trajectory matching.**

**Why pooled is sufficient:**
- 1,750 unique dates × 21 stations = 36,750 station-days
- For a 5-feature trajectory with 5 time steps = 25-dimensional sequence space
- With 36,750 sequences distributed across ~10-15 major synoptic patterns (cold front, heat dome, zonal flow, ridge, trough, backdoor cold front, marine layer, etc.), we have ~2,500-3,500 examples per pattern class
- DTW with k-NN (k=5-10) on pooled data: statistically meaningful. Expect 3-5 well-populated trajectory clusters per station-season.

**Why per-station is NOT sufficient:**
- Each station has only ~1,750 dates / 21 ≈ 83 trajectories per season per station
- For a 5-feature DTW match, you want ≥30 similar trajectories to produce a stable bucket distribution
- Some stations (OKC, SAT, MSY) will have thin coverage for specific seasons
- **Mitigation:** Pool across stations within the same climate zone. Group stations into 4-5 clusters (Northeast: NYC, PHL, DCA; Midwest: MDW, MSP, ORD; South: ATL, HOU, MIA, MSY; West: LAX, SEA, SFO, PHX; Interior: DEN, OKC, DFW, SAT). Trajectory matching compares against the full pool but weights same-climate-station analogs at 2×.

**What we gain from 36,750 station-days:**
- Per pattern archetype (e.g., "5-day warm-up trajectory with rising humidity"): ~50-200 analogs
- Per bucket outcome distribution: ±3-5pp standard error at 50% base rate — actionable
- For extreme buckets (≥90°F, ≤20°F): fewer analogs but higher edge — ±5-10pp standard error — still useful as a confidence modifier

### 2.6 Bucket-Level Recommendation

**The output is NOT a single bucket. It's a distribution:**

```
Trajectory: KMDW [Mon→Tue→Wed→Thu→Fri]
  T: 72→78→82→85→86°F
  RH: 65→60→55→50→45%
  P: 1018→1018→1019→1020→1019 mb
  WD: SW→SW→SW→W→NW

Matched analogs (n=127):
  Bucket distribution:
    Below 85°F  → 83/127 (65%, ±4pp)  ← HIGH: BUY (strong)
    At least 85°F → 44/127 (35%, ±4pp) ← HIGH: SKIP
    At least 90°F → 12/127  (9%, ±3pp)  ← HIGH: SKIP (no edge)

Trajectory recommendation:
  PRIMARY: Below 85°F (65% confidence, 83 analogs)
  SECONDARY: At least 85°F (35% confidence, 44 analogs)
  TRADEABLE: Below 85°F HIGH only — edge > 5pp at current market price
```

The recommendation is:
- **PRIMARY** bucket: highest analog density, most statistically stable
- **SECONDARY** bucket: alternative outcome with meaningful support
- **TRADEABLE** bucket(s): where trajectory probability differs from market price by ≥5pp (the edge threshold)
- **Not tradeable:** buckets where trajectory probability is too close to market price, or analog count is too thin (<20)

**How bucket confidence is computed:**
```
p_bucket = count_analogs_landing_in_bucket / total_analogs
se_bucket = sqrt(p_bucket × (1-p_bucket) / total_analogs)  # binomial SE
traj_bucket_estimate = max(p_bucket - 1.645 × se_bucket, 0)  # 90% lower bound
```

Use the 90% lower-bound estimate for conservative recommendations. This penalizes thin analog sets naturally (fewer analogs = wider SE = lower conservative estimate).

---

## 3. Integration: How the Trajectory Lane Interacts with the GEFS Pipeline

### 3.1 Guide, Not Gate

The trajectory lane NEVER:
- Vetoes a trade (that was the gate's design)
- Overrides the GEFS ensemble fraction
- Rejects the GEFS-only or 82-member combined probability

The trajectory lane ALWAYS:
- Produces a **trajectory probability estimate** per bucket
- Provides a **confidence score** (how well-populated is the analog cluster)
- Flags **regime divergence** (GEFS says UP but trajectory analogs say DOWN → interesting, not override)
- Logs everything for downstream consumption

### 3.2 Confidence Modulation

The trajectory lane output modulates the GEFS ensemble fraction through a **confidence multiplier** on the position sizing:

```
combined_confidence = w_gefs × p_gefs + w_traj × p_traj

where:
  w_gefs = 1.0 (base — GEFS is the primary signal)
  w_traj = 0.15 × traj_quality (trajectory confidence multiplier)

  traj_quality:
    analog_count: 0 if <20, 0.3 if 20-50, 0.6 if 50-100, 1.0 if >100
    dtw_score: mean DTW distance of top-10 matches [0, 1]
    station_match: 1.0 if same-station matches exist, 0.5 if cross-station only
    recency: 1.0 if trajectory corpus includes last 90 days, 0.5 otherwise

  traj_quality = 0.4×analog_count + 0.3×dtw_score + 0.2×station_match + 0.1×recency
```

**When to pay attention (w_traj ≥ 0.10):**
- Strong analog population (>100 matches)
- High DTW similarity (top-10 mean distance < 0.25)
- Same-station matches dominate
- Outcome distribution is clean (one bucket has >60% of analogs)

**When to ignore (w_traj < 0.05):**
- Few analogs (<30)
- Poor DTW matches
- Only cross-station analogs
- Flat outcome distribution (no bucket >35%)

### 3.3 Regime Divergence Flag

A specialized output: when GEFS and trajectory lane disagree directionally.

```
GEFS says:  P(at least 85°F) = 0.65  →  BUY
Trajectory says:  65% of analogs settled below 85°F  →  SKIP
```

The divergence flag is logged for Donna's status brief and the Gray Room synthesis, but DOES NOT override the trade. It's intelligence for:
- Position sizing reduction (Dan can manually trim)
- Gray Room post-mortem analysis
- Model improvement feedback (which signal was right?)

### 3.4 Screenshot Comparison

The trajectory lane produces a structured diagnostic packet per station-date:

```json
{
  "station": "KMDW",
  "date": "2026-08-03",
  "trajectory_days": 5,
  "trajectory_features": {
    "temp_f": [72, 78, 82, 85, 86],
    "rh_pct": [65, 60, 55, 50, 45],
    "pressure_mb": [1018, 1018, 1019, 1020, 1019],
    "wind_kts": [8, 10, 12, 8, 6],
    "wind_dir": ["SW", "SW", "SW", "W", "NW"]
  },
  "analog_count": 127,
  "analog_same_station": 43,
  "analog_same_climate": 84,
  "mean_dtw_distance": 0.18,
  "bucket_distribution": {
    "below_85": {"count": 83, "pct": 0.654, "se": 0.042},
    "at_least_85": {"count": 44, "pct": 0.346, "se": 0.042},
    "at_least_90": {"count": 12, "pct": 0.094, "se": 0.026}
  },
  "recommended_buckets": [
    {"bucket": "below_85", "traj_prob": 0.59, "action": "CONSIDER_POSITION"}
  ],
  "gefs_comparison": {
    "gefs_below_85": 0.55,
    "gefs_at_least_85": 0.45,
    "divergence": false
  },
  "traj_quality": 0.32,
  "w_traj": 0.048
}
```

### 3.5 Pipeline Integration Diagram

```
METAR data ─────► Trajectory Feature Builder ──► Trajectory DB
                       │                              │
                       │                              ▼
                       │                    DTW Sequence Matcher
                       │                              │
                       ▼                              ▼
GEFS ensemble ─────► GEFS fraction        Bucket distribution + quality
  fraction           pipeline                    │         │
                       │                         │         │
                       ▼                         ▼         ▼
                 ┌─────────────────────────────────────────┐
                 │       Trade Selection Aggregator        │
                 │  (GEFS primary, trajectory modulator)   │
                 └─────────────────────────────────────────┘
                       │
                       ▼
                 Position sizer (Kelly)
                       │
                       ▼
                 Trade execution
```

The trajectory lane runs **in parallel** with the GEFS pipeline. No shared state. No interdependency. The only connection is the `Trade Selection Aggregator` module which takes both as inputs.

---

## 4. Implementation Specification

### 4.1 Module Structure

```
core/trajectory_lane/
├── __init__.py
├── trajectory_lane.py          # Main entry point: compute_trajectory_lane()
├── feature_builder.py          # METAR → daily feature vectors (T, RH, P, WS, WD)
├── sequence_matcher.py         # DTW epoch-sequence matching
├── corpus_manager.py           # Build/update/query the trajectory corpus DB
├── bucket_aggregator.py        # Matched sequences → bucket distribution
├── confidence_scorer.py        # Analog count → traj_quality, w_traj
├── diagnostic_packet.py        # Structured JSON output
└── schema.py                   # DB schemas for trajectory corpus
```

### 4.2 Key Functions

```python
def compute_trajectory_lane(
    station: str,
    date: str,  # target settlement date
    metar_history: List[HourlyMetarOb],  # last 5+ days of observations
    trajectory_db: TrajectoryCorpus,
) -> TrajectoryLaneResult:
    """
    Main entry point.
    
    1. Build query trajectory (5-day or 3-day feature sequence)
    2. DTW-match against corpus
    3. Aggregate bucket distribution from matched analogs
    4. Compute confidence scores
    5. Return structured result with recommendations
    """
    features = build_trajectory_features(metar_history)
    matches = dtw_sequence_match(features, trajectory_db, top_k=200)
    buckets = aggregate_bucket_distribution(matches[:200])
    quality = score_trajectory_confidence(matches)
    return TrajectoryLaneResult(...)
```

### 4.3 DTW Implementation Notes

- Use FastDTW with radius=5 for O(N×R) performance instead of O(N²)
- Early abandon: if accumulated distance exceeds current best top-200 threshold, skip
- Normalize each feature to [0, 1] per station×season using fixed min/max (like Phase 3's MAX_RANGES approach)
- Feature weight vector: T=0.35, RH=0.20, P=0.20, WS=0.15, WD=0.10
- WD distance: circular (S→SW = 1 step, S→N = 4 steps, max 4)
- Station gate: boost same-station matches by 2× in weight, but don't exclude cross-station

### 4.4 Corpus Refresh

- Daily update after settlement data lands
- Append the completed station-date to the corpus
- Recalculate station×season z-score normalization parameters on a rolling 90-day window
- No full rebuild needed — append-only, lazy normalization

---

## 5. Ideas

### I1. ADVANCE: Climate-Zone Pooling for Thin Analog Sets

When same-station analogs are <30, pool across stations within the same climate zone. Climate zones (from E6's liquidity tiers + geography):

| Zone | Stations | Notes |
|:----:|:---------|:------|
| Northeast | NYC, PHL, DCA, BOS | Maritime continental |
| South | ATL, MIA, MSY, HOU | Humid subtropical |
| Midwest | MDW, MSP, ORD | Continental |
| Interior | DEN, OKC, DFW, AUS, SAT | Semi-arid/continental |
| West | LAX, SFO, SEA, PHX | Mediterranean/desert/marine |

Cross-zone matching is allowed but gets a 0.5× weight penalty. Same-zone matching gets 1.0×. Same-station matching gets 2.0×.

**Impact:** Triples effective analog count for thin stations (PDX, MCO) from ~20 to ~60-80.

---

### I2. ADVANCE: Multi-Length Matching (N=3 and N=5)

Run both 3-day and 5-day trajectory matching. Report the result with the higher `traj_quality` score as the primary. This adapts to synoptic speed — cold fronts (fast, 3-day window captures them) vs heat domes (slow, need 5-day window).

**Implementation:** Run DTW twice (3-day and 5-day windows). Pick the longer window if both have ≥30 analogs and DTW distance within 20%. Otherwise pick the one with more analogs.

---

### I3. ADVANCE: Trajectory + GEFS Cross-Validation Packet

For each station-date, the diagnostic packet should also include:
- GEFS ensemble mean temp trajectory (forecast for the last 5 days, not just f024)
- Actual observed temp trajectory
- Trajectory analog bucket distribution
- GEFS fraction per bucket

This allows a human (Dan, Gray Room) to visually compare: "GEFS predicted this trajectory. The actual trajectory was that. Historical analogs with this actual trajectory landed here."

**Value:** Creates an auditable trail for Gray Room analysis. Every divergence between GEFS and trajectory becomes a teachable moment.

---

### I4. PARK: Regime-Persistence Accumulator

If trajectory lane recommends the same bucket for 3+ consecutive days at the same station, and each has >50 analogs with >60% bucket share, escalate to a "trajectory regime persistence" flag. This catches persistent weather patterns (e.g., 5-day heat wave trajectory pattern that keeps repeating).

**Why PARK:** This is a secondary signal that adds complexity. Only useful when the trajectory lane is operational and showing stable patterns.

---

### I5. KILL: Full Bayesian Trajectory Modeling

Don't build a Bayesian hierarchical model over trajectory outcomes. The DTW + binomial aggregation approach is simpler and sufficient. A full Bayesian treatment (GP over trajectory space) would require custom inference code and offers marginal improvement over the simple binomial approach at our data scale.

---

## 6. Elephants

### Elephant 1: The trajectory lane won't add much edge on its own — it's a second-order signal

**What:** The GEFS ensemble fraction is a powerful first-order signal. It directly forecasts temperature probability from 31/82 ensemble members. The trajectory lane is a second-order signal — it uses the observed weather path to predict outcomes. Given that GEFS already incorporates the current state into its forecasts, the trajectory lane's information is partially overlapping.

**Why it matters:** The trajectory lane may only add 1-3pp of independent edge on top of the GEFS pipeline. If the GEFS ensemble already produces 55-58% accuracy, adding 1-3pp is worth having but won't transform the system. The trajectory lane's primary value is:
1. **Redundancy** — if GEFS data feed is down, trajectory lane provides a fallback using only METAR data
2. **Calibration cross-check** — big divergences between GEFS and trajectory signal a regime where the NWP may be wrong
3. **Market microstructure insight** — the trajectory bucket distribution feeds into what the market is pricing vs what history says

**Mitigation:** Don't over-invest. The trajectory lane should cost <2 engineering days to implement and test. If it's taking longer, the marginal value isn't there.

**DISPOSITION: ADVANCE** — Acknowledge the marginal value ceiling. Keep scope tight.

---

### Elephant 2: METAR data coverage for RH, pressure, and wind is sparser than T

**What:** The METAR backfill has 1.4M records, but temperature is the most reliably reported field. RH and pressure observations have more gaps, especially at smaller airports and non-24h reporting stations. Wind data is generally good but variable.

**What the data actually looks like (rough estimate):**

| Feature | Coverage (of 36,750 station-days) |
|---------|:---------------------------------:|
| Temperature | ~95% (35K) |
| Humidity | ~75% (28K) |
| Pressure | ~70% (26K) |
| Wind speed | ~85% (31K) |
| Wind direction | ~80% (30K) |

**Why it matters:** If humidity and pressure have 25-30% gaps, the DTW matching is forced to either (a) drop the missing features and match on fewer dimensions, or (b) fill gaps with GEFS forecasts, introducing model correlation. Either approach reduces the trajectory lane's independence from the GEFS pipeline — which undermines the primary value of redundancy.

**Mitigation:** (1) Audit METAR coverage per station per feature before building the trajectory corpus. (2) If a feature has <50% coverage at a station, drop it from the trajectory vector for that station (adaptive dimensionality). (3) Use GEFS-forecast fill only as a last resort, with clear metadata tagging.

**DISPOSITION: ADVANCE** — Must verify before building. If RH+P coverage is too thin, reduce to T+WS+WD primary features.

---

### Elephant 3: Settlement bucket data at trajectory-relevant granularity is thin for extreme temperatures

**What:** The trajectory lane's bucket-level recommendation (e.g., "Below 85°F" vs "At least 90°F") requires sufficient examples in extreme buckets. With 1,750 dates × 21 stations, the distribution per station is approximately:

| Station | Below 30°F | 30-50°F | 50-70°F | 70-85°F | Above 85°F |
|---------|:----------:|:-------:|:-------:|:-------:|:----------:|
| KNYC | 40 (2%) | 350 (20%) | 750 (43%) | 450 (26%) | 160 (9%) |
| KMDW | 180 (10%) | 350 (20%) | 550 (31%) | 350 (20%) | 320 (18%) |
| KMIA | 0 (0%) | 15 (1%) | 200 (11%) | 700 (40%) | 835 (48%) |
| KDEN | 300 (17%) | 500 (29%) | 500 (29%) | 300 (17%) | 150 (8%) |

Extreme buckets (below 30°F or above 85°F) have 40-320 examples per station over 5.5 years. For trajectory matching with 5-day windows, consecutive sequences in extreme temperatures are rarer — maybe 3-5 sequences per winter/season.

**Why it matters:** The bucket distribution from trajectory matching for extreme buckets will have ±5-15pp standard error vs ±2-4pp for mid-range buckets. The trajectory lane's recommendations for extreme buckets are inherently noisier than for mild buckets.

**Mitigation:** (1) Report confidence intervals, not point estimates. (2) Require ≥20 analogs for an extreme bucket to produce a tradeable recommendation. (3) For extreme buckets, use the pooled climate-zone analog set, not per-station only.

**DISPOSITION: ADVANCE** — Acceptable limitation. Don't let the analytics trade at thin-analog extremes without proper confidence bounds.

---

### Elephan
### Elephant 4: The trajectory lane and Phase 3 settlement-epoch matching must not be merged

**What:** There's a natural temptation to combine the trajectory lane with the existing Phase 3 settlement-epoch analog system — they share the word "trajectory" and both use analog matching. But they operate on different data, answer different questions, and have different failure modes.

| Dimension | Phase 3 Settlement Epoch | Trajectory Lane (New) |
|:----------|:------------------------:|:---------------------:|
| **Input data** | Settlement traces (jumps, reversions, excursions) | METAR obs (T, RH, P, wind) |
| **Match target** | Intraday price dynamics | Multi-day weather trajectory |
| **Features** | 14 settlement-derived | 5 meteorological (T,RH,P,WS,WD) |
| **Time horizon** | Hours (intra-epoch) | Days (3-5 day sequence) |
| **Output** | Settlement bucket outcome | Bucket distribution |
| **Corpus** | Settlement_epochs table (fewer records) | METAR × settlements (36K+ records) |
| **Confidence signal** | Match score | Analog count + DTW distance |

**Why it matters:** Merging them would produce a system that does neither well. The Phase 3 code looks for "which days had the same intraday temperature path as today?" The trajectory lane looks for "which days followed the same multi-day weather pattern as the last 5 days?" These are complementary, not mergeable.

**Mitigation:** Keep them as separate modules with a clean integration layer. The settlement-epoch system feeds GEFS calibration (ECE tracking). The trajectory lane feeds trade selection confidence. Different pipelines, different databases, different consumers.

**DISPOSITION: ADVANCE** — Two separate systems, one integration contract.

---

## 7. Complete Disposition Table

| # | Item | Type | Disposition |
|---|------|:----:|:-----------:|
| E1 | Phase 3 trajectory matches wrong epoch type (settlement, not meteorological) | ERROR | **ADVANCE** |
| E2 | `test_trajectory_gate.py` is a gate, not a matching system — scope creep | ERROR | **ADVANCE** |
| E3 | `core/trajectory/` `.pyc` files are source-less, unmaintainable, different purpose | ERROR | **ADVANCE** |
| — | **TRAJECTORY LANE DESIGN SPEC (Section 2)** | SPEC | **ADVANCE** |
| — | DTW epoch-sequence matching with multi-length (3-day and 5-day) | SPEC | **ADVANCE** |
| — | 5-feature set: T, RH, P, WS, WD + seasonal phase | SPEC | **ADVANCE** |
| — | Bucket distribution as output, not single bucket | SPEC | **ADVANCE** |
| — | 90% lower-bound confidence for conservative recommendations | SPEC | **ADVANCE** |
| — | Integration: guide, not gate (w_traj cap at 0.15) | SPEC | **ADVANCE** |
| — | Climate-zone pooling for thin analog sets | SPEC | **ADVANCE** |
| I1 | Climate-zone pooling | IDEA | **ADVANCE** |
| I2 | Multi-length matching (N=3 and N=5) | IDEA | **ADVANCE** |
| I3 | Trajectory + GEFS cross-validation diagnostic packet | IDEA | **ADVANCE** |
| I4 | Regime-persistence accumulator | IDEA | **PARK** |
| I5 | Full Bayesian trajectory modeling | IDEA | **KILL** |
| Ele1 | Trajectory lane adds only 1-3pp marginal edge (second-order signal) | ELEPHANT | **ADVANCE** |
| Ele2 | METAR RH/pressure coverage is sparser than T — threatens independence | ELEPHANT | **ADVANCE** |
| Ele3 | Extreme bucket analogs are thin — ±5-15pp SE | ELEPHANT | **ADVANCE** |
| Ele4 | Trajectory lane and Phase 3 must not be merged | ELEPHANT | **ADVANCE** |

---

## 8. Implementation Budget

| Component | Effort | Dependencies | Priority |
|:----------|:------:|:------------:|:--------:|
| METAR coverage audit (T,RH,P,WS,WD per station) | 1h | METAR DB | **Pre-build** |
| Trajectory feature builder (METAR → daily vectors) | 2h | Coverage audit | **Phase 1** |
| DTW sequence matcher with FastDTW | 4h | Feature builder | **Phase 1** |
| Corpus manager (build/refresh trajectory corpus from METAR + settlements) | 3h | Feature builder + settlements DB | **Phase 1** |
| Bucket aggregator + confidence scorer | 2h | Sequence matcher | **Phase 1** |
| Diagnostic packet output + serialization | 1h | Aggregator | **Phase 1** |
| Integration into trade selection aggregator (w_traj) | 1h | Diagnostic packet | **Phase 2** |
| Climate-zone pooling | 1h | Corpus manager | **Phase 2** |
| Dashboard integration (screenshot comparison display) | 2h | Diagnostic packet | **Phase 3** |
| **Total** | **~17h** | — | — |

**Comparison:** The trajectory confirmation gate (scope creep) was 1 script, ~200 lines. The trajectory lane matching system is a proper module with DTW, corpus management, confidence scoring, and integration — about 5-8× the scope but proportionate to the value.

---

## 9. Answer: Is the corpus sufficient for 1,750+ settlement dates?

**Yes, at the pooled/climate-zone level. Marginally at the per-station level.**

- **Pooled:** 36,750 station-days → ~2,500-3,500 analogs per synoptic pattern → ±2-4pp SE on bucket distribution. Statistically sound.
- **Per-station:** ~1,750 days per station → ~83 per season → 10-30 analogs for trajectory matching → ±5-15pp SE. Marginal — climate-zone pooling essential.
- **Extreme buckets:** Thin everywhere. Pooled climate-zone approach is the only way to get statistically meaningful recommendations for ≥90°F or ≤30°F buckets without waiting 2-3 more years of data.
- **Multi-year calibration baseline:** Yes, 5.5 years (2021-2026) is sufficient for seasonal pattern matching. The corpus covers all 4 seasons for all stations.

**1,750 settlement dates × 21 stations = 36,750 station-days** is enough to build a useful trajectory matching system. But the per-station trajectory space is sparse — climate-zone pooling and multi-length matching are not optional, they're essential for statistical validity at thin-extreme buckets.

---

*End of Expert 4: Pattern Matching Specialist — Trajectory Lane Design*
