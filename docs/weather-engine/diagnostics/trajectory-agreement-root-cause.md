# Trajectory Bucket Agreement — Root Cause Analysis

**Date:** 2026-08-03
**Author:** Deep-dive subagent (via Donna)
**Result:** 26.3% exact bucket agreement
**Files:** `scripts/research_trajectory.py`, `data/bmode_post_gray_room_log.md`, `docs/weather-engine/backtests/trajectory_research_20260803.json`

---

## Executive Summary

The trajectory research spike achieved 26.3% exact bucket agreement. This is **not significantly better than a trivial climatology baseline** and is **worse than the in-sample oracle** (always predict 90-94 = 31.6%). The algorithm is producing a computationally expensive reproduction of the KMDW summer temperature distribution, not a predictive analog system.

Seven distinct root causes were identified, ranked by impact:

1. **CRITICAL — `normalize_features()` is dead code** (never called)
2. **CRITICAL — Temporal leakage** (overlapping candidate windows create fake near-perfect matches)
3. **HIGH — Corrupted data** (41486°F in KMDW on 7/17 corrupts all late-July queries)
4. **HIGH — Climate-zone pooling is a no-op** (station_boost=2.0 + only 1 same-zone station)
5. **MEDIUM — Broken wind direction encoding** (circular → scalar)
6. **MEDIUM — Feature set is temperature-dominated** (other features are meteorologically relevant but negligible in the unnormalized distance)
7. **LOW — Tiny evaluation set** (n=19, ±20pp CI)

---

## 1. DTW Implementation: The `normalize_features()` Bug

### Finding: The function is defined but never called

```python
# Line 178 — defined
def normalize_features(features: Dict[str, float], stats: dict) -> Dict[str, float]:
    """Z-score normalize features using pre-computed per-station stats."""
    ...

# Lines 203-209 — compute_feature_distance uses RAW values
def compute_feature_distance(f1, f2):
    """..."""
    for feat_name, weight in FEATURE_WEIGHTS.items():
        diff = f1.get(feat_name, 0.0) - f2.get(feat_name, 0.0)
        total_dist += weight * (diff ** 2)
    return math.sqrt(total_dist / total_weight)
```

`normalize_features()` is **never referenced** in the DTW matching path. The distance computation operates on raw, unscaled feature values.

### Impact on distance metric

The 5 features have wildly different scales:

| Feature | Typical Range | Typical Diff² | Weight | Weighted Contribution |
|---------|-------------|------|--------|----------------------|
| temp_f | 30-100°F | ~400 (20°F diff) | 0.35 | 140 (74%) |
| dewpoint_f | 10-80°F | ~400 (20°F diff) | 0.20 | 80 (42%) but correlated with temp |
| pressure_mb | 1000-1030 | ~25 (5 mb diff) | 0.20 | 5 (3%) |
| wind_speed_kt | 0-40 kt | ~100 (10 kt diff) | 0.15 | 15 (8%) |
| wind_dir_enc | 0-7 | ~4 (2 dir diff) | 0.10 | 0.4 (<1%) |

**Temperature dominates the distance metric ~74% of the signal.** The other features are meteorologically relevant but numerically negligible. The DTW is effectively a temperature-trajectory matcher, not a multi-variable pattern matcher.

### Sakoe-Chiba band is effectively no constraint

The code calls `dtw_distance(..., radius=3)` on length-5 sequences. With sequences of length 5, a radius of 3 means the band covers the entire matrix (|i-j| ≤ 3 > 5-1). The DTW is **unconstrained** — there is no meaningful warping constraint, and the "FastDTW" label is misleading. The algorithm is computing standard Euclidean distance on 5-element sequences with a minor warping allowance.

The config reports `radius=5` but the actual call uses `radius=3`. The docstring claims "early-abandon" but the code has no early-abandon logic.

---

## 2. Temporal Leakage: Overlapping Candidate Windows

### Finding: Candidate windows can share 4 of 5 days with the query

The query for date D is a 5-day window `[D-4, D]`. Candidate windows are all 5-day sequences ending on any date `< D`. A candidate ending on `D-1` has window `[D-5, D-1]`, which shares 4 days with the query (`[D-4, D-1]`).

```
Query:     [D-4, D-3, D-2, D-1, D]
Candidate: [D-5, D-4, D-3, D-2, D-1]  ← ends D-1, overlaps 4 of 5
Candidate: [D-6, D-5, D-4, D-3, D-2]  ← ends D-2, overlaps 3 of 5
...
Candidate: [D-9, D-8, D-7, D-6, D-5]  ← ends D-5, NO overlap
```

### Impact

The overlapping candidates produce DTW distances of ~0.5-2.0 (near-identical sequences). With station_boost=2.0, these become adjusted distances of ~0.25-1.0. These dominate the top 200, squeezing out any real cross-zone or same-zone analogs.

**This is the primary reason the top 200 are 100% same-station KMDW for 11 of 19 queries.** The algorithm is matching the query to itself, shifted by 1-4 days, not to meteorologically independent analogs.

### Fix

Exclude candidates whose window overlaps the query. Minimum separation: candidate end date ≤ query date - 5 (no shared days). This ensures the "analog" is truly an independent historical pattern, not yesterday's weather.

---

## 3. Corrupted Data: 41486°F on KMDW 2026-07-17

### Finding: The `MAX(temp_f)` aggregation picks up a sensor error

The SQL query in `load_trajectory_corpus()` uses `MAX(temp_f)` with no bounds check:

```sql
SELECT date_utc, MAX(temp_f) as temp_f, ...
FROM metar_observations
WHERE station=? AND temp_f IS NOT NULL [...]
GROUP BY date_utc
```

KMDW on 2026-07-17 has an observation with `temp_f=41486.0` (and `dewpoint_f=3545.6`). `MAX(temp_f)` returns 41486.0, corrupting the daily feature. This value propagates into all 5-day sequences that include 7/17.

### Impact

| Query Period | Query includes 7/17? | Top Match Distance | Diagnosis |
|-------------|---------------------|-------------------|-----------|
| 7/3-7/7 | No | 0.5-2.0 | Clean |
| 7/13-7/16 | No | 0.5-1.0 | Clean |
| 7/17-7/26 | **Yes** | **444-4578** | **Corrupted** |

The distance of 444-4578 for the late-July queries is not a "bad analog" — it's a **bug.** The DTW compares 41486°F against ~84-95°F, producing a distance dominated by (41486 - 90)² × 0.35 ≈ 6 × 10⁸.

### Scope

KMDW has 20 observations with `temp_f > 130°F` (all corrupted). Across all 73 stations in the DB, there are hundreds of corrupted observations. The MAX(temp_f) aggregation picks up the single worst value per day, amplifying the problem.

### Fix

Add a physical bounds check in `build_feature_vector()`:
```python
if temp > 130 or temp < -50: return None
```

Also filter dewpoint, pressure, and wind speed beyond reasonable bounds.

---

## 4. Climate-Zone Pooling is a No-Op

### Finding: Station_boost + tiny Midwest pool = same-station domination

The code implements a SOFT weighting, not a hard pool filter:

```python
if cand_station == "KMDW":
    station_boost = 2.0    # distance halved
elif same_zone:
    station_boost = 1.0    # unchanged
else:
    station_boost = 0.5    # distance doubled
```

The Midwest zone has only **2 stations** in the 20-station list: KMDW and KMSP. KORD is listed in the zone definition but is NOT in the `STATIONS` list, so it's never loaded from the DB.

### Quantitative evidence

| Match Type | Count (19 queries) | Percentage |
|-----------|-------------------|-----------|
| Same-station (KMDW) | 3,754 | 98.8% |
| Same-zone (KMSP) | 4 | 0.1% |
| Cross-zone | 42 | 1.1% |

**Only 4 same-zone matches across all 19 queries.** The "climate-zone pooling" is effectively dead code. The algorithm is comparing KMDW against its own history, not against a pooled regional set.

### Zone quality

The zone definitions are also meteorologically questionable:
- **Interior:** KDEN (Denver, semi-arid high-elevation) grouped with KDFW (Dallas, humid subtropical), KAUS/KSAT (Gulf-influenced). Real Köppen zones differ.
- **West:** KLAX (marine Mediterranean) grouped with KPHX (desert). Different climate regimes entirely.

### Fix

1. Add KORD to the `STATIONS` list
2. Implement a **hard pool filter** (only candidates from the same zone, no station_boost)
3. Reconsider zone definitions using Köppen climate classification or a data-driven approach (k-means on temperature/humidity vectors)
4. Better approach: use the top 5-10 stations by geographic proximity + climate similarity to the query station, rather than fixed zones

---

## 5. Broken Wind Direction Encoding

### Finding: Circular variable encoded as scalar

```python
def encode_wind_dir(deg):
    if deg is None: return 4.0  # South (default)
    directions = [0, 45, 90, 135, 180, 225, 270, 315]
    return float(min(directions, key=lambda x: abs(x - deg)) / 45.0)
```

This encodes 8 wind directions as scalars 0-7, where:
- N = 0, NE = 1, E = 2, ... NW = 7

**Problem:** NW (7) and N (0) are adjacent in real terms (337.5° and 0° are 22.5° apart) but **7 units apart** in the encoded scalar. Euclidean distance treats them as maximally different when they are actually nearly identical.

Also: calm/missing wind is encoded as 4.0 (South), which is a direction convention that creates false directional signals.

### Fix

Use sine/cosine encoding:
```python
import math
u_wind = math.sin(math.radians(deg))
v_wind = math.cos(math.radians(deg))
```

This maps direction to a unit circle where N and NW are correctly close (cos 0° = 1, cos 337.5° = 0.92, distance ≈ 0.08). For missing wind, use (0, 0) to indicate "no direction" rather than a false direction.

---

## 6. Feature Set is Temperature-Dominated

### Finding: The 5 features collapse to effectively 1-2 features

Because `normalize_features()` is never called, the distance metric is dominated by temperature (74%) and dewpoint (which is temperature-correlated). The remaining features contribute <10% of the distance signal.

### What's missing

For predicting the NEXT day's max temperature bucket, the most relevant features are:
1. **Max temp trend** (rate of change over the 5 days) — already implicitly captured
2. **Dewpoint at time of max temp** — a known physical cap on daytime max (wet-bulb effect)
3. **Temperature range** (max-min) — clear vs cloudy proxy
4. **Cloud cover / solar radiation** — not available from METAR directly
5. **Previous day's max temp** — strong autocorrelation for summer patterns

### Winds are the wrong predictors

Wind speed and direction are weak predictors of max temperature on a daily scale. Their primary use is for frontal detection (wind shift + pressure change), which is relevant for pattern matching but not for temperature prediction. Without normalization, they contribute noise, not signal.

---

## 7. Tiny Evaluation Set

### Finding: 19 query dates, 11 skipped

| Metric | Value |
|--------|-------|
| Available query dates | 30 |
| Successfully matched | 19 (63%) |
| Skipped (data gaps) | 11 (37%) |
| Exact bucket agreement | 5/19 = 26.3% |
| 95% CI (Agresti-Coull) | [11.4%, 49.2%] |

The CI of ±20pp means the result is statistically indistinguishable from 10-50%. The 26.3% finding is not robust.

### Data gaps

KMDW has significant METAR data gaps in the query period:

| Date | Obs | Issue |
|------|-----|-------|
| 7/3 | 1 | Missing 23 hours |
| 7/4 | 1 | Missing 23 hours |
| 7/6 | 1 | Missing 23 hours |
| 7/7 | 1 | Missing 23 hours |
| 7/8 | **0** | **Completely missing** |
| 7/9 | 4 | Sparse |

The 11 skipped queries (7/8-7/12 and others) are all affected by the 7/8 gap. The 5-day lookback window for any date in that range includes 7/8, which has zero observations, causing `valid = False`.

---

## Benchmarks

### Exact bucket agreement comparison

| Method | Exact | Within-1 | Direction (≥85°F) |
|--------|-------|---------|-------------------|
| **DTW algorithm** | **26.3%** | **63.2%** | **68.4%** |
| Random (8 uniform buckets) | 12.5% | ~25% | ~50% |
| Always predict 90-94 (oracle) | 31.6% | — | — |
| Always predict 85-89 (climatology) | 10.5% | 57.9% | 57.9% |
| Always predict 80-84 | 15.8% | 42.1% | 36.8% |

**Key finding: The DTW's 26.3% is worse than the in-sample oracle (31.6%) and is statistically indistinguishable from random at n=19 (p ≈ 0.09).** The within-1-bucket rate of 63.2% is marginally better than always-predict-85-89 (57.9%), a 5.3pp improvement.

### Direction-only

Direction-only (>/< 85°F threshold) is 68.4% — marginally above 50% chance, but not compelling. The algorithm's mode bucket is almost always 85-89 or 90-94, and the actual July KMDW temps cluster around 73-96, so the direction prediction is essentially "July in Chicago is usually warm" (trivial).

---

## The Real Issue: The Algorithm Predicts Climatology, Not Patterns

The mode bucket is 85-89 for 11 of 19 queries and 90-94 for 5 of 19. This mirrors the KMDW summer temperature distribution (actuals: 90-94 ×6, 75-79 ×5, 80-84 ×3, 85-89 ×2, 95-99 ×2, 70-74 ×1). The algorithm is reproducing the **climatological distribution** of summer KMDW temps, not adding pattern-based skill.

The analog set's bucket distribution has entropy = 1.60 (vs uniform 2.08 for 8 buckets). The distribution is spread across 5-6 buckets, meaning the algorithm is saying "any of these 5 buckets is possible" — which is a vacuous statement for summer Chicago.

---

## Recommended Fixes (Ranked by Impact)

### Priority 1: Fix the data pipeline
- Add physical bounds checks in `build_feature_vector()` (filter temp > 130°F, wind > 150 kt, pressure < 800 mb, pressure > 1100 mb)
- This prevents the 41486°F corruption from corrupting the entire match set

### Priority 2: Call `normalize_features()`
- Compute z-scores per station × season (monthly or 60-day rolling window)
- This makes all features comparable and allows pressure, wind, and dewpoint to contribute meaningfully
- Single biggest impact on match quality

### Priority 3: Fix temporal leakage
- Exclude candidates whose window overlaps the query window
- Require candidate_end_date <= query_date - 5 (no shared days)
- This forces the algorithm to find truly independent analogs, not overlapping windows

### Priority 4: Implement hard zone pooling
- Remove the soft station_boost and replace with a hard pool: only candidates from the same climate zone
- Add KORD to the STATIONS list
- Reconsider zone definitions or use k-NN geographic proximity
- With hard pooling, the algorithm must find genuinely cross-station analogs

### Priority 5: Fix wind direction encoding
- Replace scalar encoding with sin/cos encoding (`u_wind = sin(deg)`, `v_wind = cos(deg)`)
- Handle missing wind as (0, 0) not (4.0, 4.0)

### Priority 6: Report the full distribution, not just the mode
- The mode bucket is a weak representative (<30% of analog mass)
- Report CRPS, Brier score, or log-loss for the full distribution
- Report the "top 3 buckets" coverage rate (what fraction of the actuals fall in the top 3 analog buckets)

### Priority 7: Larger evaluation set
- Run on 200+ query dates (not 19) across all 20 stations (not just KMDW)
- Use a proper train/test split (leave-last-year-out)

---

## Appendix: What the Corrected Agreement Rate Would Be

If fix #1 (data filtering) and fix #3 (temporal leakage) were applied:

| Fix Applied | Expected Impact |
|-------------|----------------|
| Data filtering | The late-July queries (7/17-7/26) would have valid distances again. Their mode would still be 85-89 (climatology), but the distances would be meaningful. |
| Temporal leakage fix | The top 200 would include non-overlapping matches. This might shift the mode for some queries. Aggressive estimate: 30-35% exact, 65-70% within-1. |
| Normalization | Would allow pressure and wind to contribute. This might help distinguish between similar-temperature patterns with different weather regimes. |
| Hard zone pooling | Would force the algorithm to find analogs from KMSP, KORD, etc. This might ADD diversity but could also REDUCE accuracy if those stations have different microclimates. |

Realistic ceiling with all fixes: **35-40% exact agreement**, which is marginally better than the 31.6% oracle baseline. The trajectory lane may never exceed ~50% exact agreement because the problem is fundamentally hard (predicting a 5°F bucket from a 5-day weather pattern).

---

## Code Artifacts Found

| Issue | Location | Lines |
|-------|----------|-------|
| Dead `normalize_features()` | `scripts/research_trajectory.py` | 178-182 (defined, never called) |
| No bounds check in `build_feature_vector()` | `scripts/research_trajectory.py` | 83-96 |
| Station_boost=2.0 dominates pool | `scripts/research_trajectory.py` | 371-378 |
| Circular wind encoding | `scripts/research_trajectory.py` | 70-77 |
| Overlapping candidates allowed | `scripts/research_trajectory.py` | 366-369 (only `cand_date >= query_date` check) |
| KORD referenced but not in stations | `scripts/research_trajectory.py` | 46-49 |
| `dtw_quality` always "good" | `scripts/research_trajectory.py` | 431-434 |
| Config says radius=5, actual call radius=3 | `scripts/research_trajectory.py` | 385 vs 511 |
| No early-abandon despite docstring claim | `scripts/research_trajectory.py` | 213-214 |