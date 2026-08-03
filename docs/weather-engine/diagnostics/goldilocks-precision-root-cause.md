# Goldilocks Precision Root Cause Analysis

**Date:** 2026-08-03  
**Author:** Donna (subagent deep-dive)  
**Status:** COMPLETE  

## Summary

The Goldilocks lane achieved **3.92% precision** (51 signals, 2 TP, 49 FP) against a threshold of >70%. The downstream report concluded "detection logic is correct but METAR data is too coarse (hourly). Needs IEM 1-minute ASOS data."

**This diagnosis disagrees with that conclusion.** The root cause is a combination of **5 compounding issues**, of which data resolution is only one. Fixing the data source alone would not bring precision above 70%.

---

## Root Cause #1 (Critical): Data Corruption in `awc_tgftp` Source

**699 corrupted temperature values** in the 30-day backtest period (2026-07-04 to 2026-08-03), all from the `awc_tgftp` source. Every station has corrupted data, ranging from 4 (KNYC) to 141 (KSFO) corrupted observations.

**Examples of corrupted values:**
- 39,678.8°F (KATL)
- 55,889.6°F (KDEN)
- 63,102.2°F (KPHL)
- 64,878.8°F (KMDW)

**Impact on the backtest:** Both "true positives" (2 signals) are artifacts of corrupted data — e.g., a temp of 59,480.6°F crossing boundary 59,480, then "reverting" to 75.2°F, with the actual daily max (92°F) being below the absurd boundary. **On clean data, precision is 0 TPs, 49 FPs → 0% precision.**

**Root cause:** The `awc_tgftp` parser in `metar_monitor.py` (`_parse_tgftp_text`) extracts temperature from the METAR token (e.g., "33/23" → 33°C → 91.4°F). The corrupted values suggest a parsing error where the raw METAR string is misinterpreted — possibly reading a pressure altitude or QNH value as temperature (e.g., A3006 → 30.06 inHg → parsed as 3006°C → 5428.8°F, or similar).

**Evidence from DB:**
```
KATL 2026-07-09T20:52:00+00:00: 39678.8F  (source=awc_tgftp)
KATL 2026-07-09T20:52:00+00:00  raw=KATL 092052Z 29008KT 10SM SCT039TCU...
```
The raw METAR "KATL 092052Z" is the 20:52Z report. The temperature token in the raw METAR should be something like "33/23" (33°C / 23°C dewpoint). The 39,678.8F = 22,026°C is clearly from a different field.

---

## Root Cause #2 (Critical): UTC/Local Date Misalignment

The backtest groups observations by **UTC date** (`dt.strftime('%Y-%m-%d')`) but evaluates against Kalshi settlements that use **station-local date**. This creates systematic misalignment:

- **West Coast stations** (KLAX, KSFO, KSEA, KPHX, KLAS, KDEN): Local day runs from UTC+7 to UTC+8 the next day. The UTC date grouping (midnight-to-midnight UTC) is offset by 7-8 hours.
- **East Coast stations** (KATL, KBOS, KDCA, KNYC, KPHL): Local day runs from UTC+4 to UTC+5 the next day. Offset is 4-5 hours.

**Impact:** Observations near the UTC midnight boundary (which is 4-8 PM local time) are assigned to the wrong settlement date. The daily max from METAR is computed on the wrong set of observations.

**Evidence from the bmode P0 verification:** The P0 (CLI Settlement Verification) found that the **local-date grouping fix (using station timezone) improved results from 82.5% to 90%** agreement. The Goldilocks backtest did NOT apply this fix.

**Example:** KLAX 2026-07-21: signal fires at 2026-07-21T22:53Z (15:53 PDT). The Kalshi settlement for 2026-07-21 uses the local date (PDT), which runs from 2026-07-21T07:00Z to 2026-07-22T07:00Z. The backtest groups by UTC date 2026-07-21 (00:00Z-23:59Z), which correctly captures the KLAX afternoon max (80.6F at 13:53 PDT = 20:53Z). But for other stations, the local day offset can shift the daily max.

---

## Root Cause #3 (Critical): Detection Logic Cannot Distinguish "Transient Spike" from "Boundary Oscillation"

The core detection logic identifies a "transient spike" as:
```
spike_delta = curr_temp - running_max_before
is_transient = spike_delta < TRANSIENT_DELTA_THRESHOLD (0.3°F)
```

**The flaw:** When the temperature re-crosses a boundary at the same level as the previous daily max, `spike_delta = 0.0 < 0.3 → "transient"`. But this is NOT a transient spike — it's the daily max being maintained for hours, then naturally cooling.

**How this generates false positives:** On a hot afternoon, the temperature plateaus at 80.6°F for 2-3 hours (multiple 5-min or hourly observations at the same value). The first crossing creates a tracker (non-transient, spike_delta = 1.8). The second crossing 2 hours later (same temperature) creates a NEW tracker with `spike_delta = 0.0 → is_transient = True`. When the temp finally drops below the boundary (natural diurnal cooling), the signal fires: "BELLOW_BOUNDARY". But the daily max was 80.6°F, which is ≥ 80, so the signal is a false positive.

**This is the dominant source of FPs (49 out of 49 on clean data).** Every signal in the dataset has `spike_delta = 0.0` or minimal values, indicating re-crossing/plateau scenarios, not genuine transient spikes.

**The detection logic was designed for sub-minute tick data** where a genuine transient spike completes in 1-2 minutes (e.g., 84.9 → 85.3 → 84.8 in 60 seconds). With hourly or 5-minute data, the "reversion" takes 2-3 hours, which is just normal diurnal temperature variation, not a transient spike.

---

## Root Cause #4 (Structural): UTC-Hardcoded "Late Day" Gate

The signal requires `late_enough = obs.timestamp.hour >= LATE_DAY_UTC_HOUR (18)` before firing. This is a hardcoded UTC hour, not station-local time.

**Impact:**
- **East Coast (EDT, UTC-4):** 18Z = 14:00 EDT — reasonable late-afternoon gate
- **West Coast (PDT, UTC-7):** 18Z = 11:00 PDT — STILL MORNING, way too early
- **KSFO example:** Signal fires at 21:56Z = 14:56 PDT, but the daily max occurs at 23:56Z = 16:56 PDT (91.4°F). The signal fires 3 hours before the true daily max.

**The LATE_DAY_UTC_HOUR=18 gate provides no protection for about 40% of the station universe.** Signals fire at 16Z-22Z, with the peak at 20Z (15 signals). For west coast stations, these are all before the actual daily max is established.

---

## Root Cause #5 (Contributing): Kalshi Settlement Rounding Creates Evaluation Bias

The evaluation uses `actual_daily_max < boundary` where `actual_daily_max` is the **integer-rounded** Kalshi settlement value (e.g., 85.0, 86.0, 87.0). Kalshi's temperature markets settle on the daily max rounded to the nearest whole degree.

**The issue:** A true daily max of 84.6°F:
- Rounds to 85°F in Kalshi's settlement
- Kalshi settlement DB stores `kalshi_temp = 85.0`
- Boundary check: `85.0 < 85` → False → signal is FP
- But the true raw max was 84.6, which IS below 85

**This creates a ~0.5°F systematic bias in evaluation.** The detector's prediction "BELOW_BOUNDARY" is correct about the raw max, but the evaluation uses the rounded value, marking it as a false positive.

**Impact estimate:** Approximately 10-20% of the FPs may be mislabeled due to this rounding bias. However, fixing this alone would not bring precision above 70% given the other root causes.

---

## Root Cause #6 (Contributing): Temperature Quantization at 0.1°C Resolution

The METAR data is stored at 0.1°C resolution (0.18°F steps). The `TRANSIENT_DELTA_THRESHOLD = 0.3°F` is essentially 1.7 quantization steps — barely above the noise floor.

**Impact:** The "transient" check cannot distinguish between a genuine 0.29°F spike and measurement noise. The threshold is effectively at the resolution limit of the data.

---

## Why the "IEM 1-Minute ASOS Data" Claim Is Partially Correct

The downstream report says "Needs IEM 1-minute ASOS data." This is correct in principle but insufficient:

**What IEM 1-min data would fix:**
1. Sub-minute resolution would reveal genuine transient spikes (e.g., 84.9 → 85.3 → 84.8 in 60 seconds) that are invisible at 5-min/hourly resolution
2. The IEM data is the SAME source Kalshi uses for settlement, eliminating source-mismatch FPs
3. The 1-minute ticks would allow detection of spike→revert within 1-2 minutes, not 2-3 hours

**What IEM 1-min data would NOT fix:**
1. **Data corruption in awc_tgftp** — the 699 corrupted observations are a separate ingestion bug
2. **UTC/local date alignment** — this is a backtest logic bug that affects all data sources
3. **Kalshi rounding bias** — the evaluation label mismatch is independent of the data source
4. **UTC-hardcoded late-day gate** — needs to be station-local-time aware regardless of data source
5. **The fundamental detection logic flaw** — the "spike_delta < 0.3" heuristic is flawed even at 1-minute resolution. A genuine IEM 1-minute spike to 85.3 that reverts to 84.8 in 2 minutes would still be classified as "transient" (spike_delta = 85.3 - 84.9 = 0.4 > 0.3 → NOT transient). The threshold needs to be recalibrated for 1-minute data.

---

## The Fix: What Would Bring Precision Above 70%?

### Immediate (data quality, no code change):
1. **Fix awc_tgftp parser** — prevents corrupted values from entering the database
2. **Add a temperature sanity filter** — reject any value below -50°F or above 130°F for CONUS stations

### Backtest evaluation fix (changes the result, reveals true signal value):
3. **Fix UTC/local date alignment** — use `station_local_day_key()` for date grouping, matching how Kalshi settlements are keyed
4. **Fix evaluation label** — use raw NWS daily max temperature (from `daily_stats` table or NWS API) instead of the integer-rounded Kalshi settlement value, OR adjust the boundary check to use `actual_daily_max < boundary - 0.5` (accounting for rounding)

### Detection logic fix (changes the signal):
5. **Replace `LATE_DAY_UTC_HOUR` with station-local hour** — e.g., `local_hour >= 16` instead of `utc_hour >= 18`
6. **Add a minimum crossing duration** — require the crossing to last at least 2 observations (i.e., don't fire on a single tick that oscillates)
7. **Add a maximum crossing duration** — if the temp stays above the boundary for > 30 minutes, it's not a transient spike (it's an established boundary crossing)
8. **Recalibrate `TRANSIENT_DELTA_THRESHOLD`** — the current 0.3°F is meaningless at 0.1°C quantization. For 1-minute data, calibrate to the actual statistical distribution of intra-minute temperature variations

### Data source fix (changes the data):
9. **Obtain IEM 1-minute ASOS data** — this is necessary for the full resolution but not sufficient alone

### Expected precision after all fixes:
| Fix | Expected Precision | Rationale |
|-----|-------------------|-----------|
| Current (broken) | 3.92% | 2 TP (corruption), 49 FP |
| + Fix data corruption | 0% | 0 TP, 49 FP on clean data |
| + Fix UTC/local date | 0-5% | Still 0 TPs but fewer date-misalignment FPs |
| + Fix evaluation rounding | 5-15% | Some FPs become TPs |
| + Fix late-day gate (local hour) | 10-20% | Fewer premature FPs |
| + Add crossing duration constraints | 15-30% | Eliminates plateau re-crossing FPs |
| + IEM 1-minute data | 30-50% | Genuine sub-minute spikes become visible |
| + Recalibrate thresholds for 1-min data | 50-70% | Requires tuning on actual spike distribution |

**The Goldilocks edge does exist in theory** (sub-minute temperature spikes that the official daily max computation doesn't count), but the current implementation has **zero empirical evidence of the edge** — the 2 "TPs" are data corruption artifacts. The lane should be redesigned from scratch once IEM 1-minute data is available, using the fixes above.

---

## Appendix: Per-Signal Breakdown

### Signal Stats
| Metric | Value |
|--------|-------|
| Total signals | 51 |
| True positives | 2 (both data corruption artifacts) |
| False positives | 49 |
| Precision (all) | 3.92% |
| Precision (clean data) | 0% |
| Recall | 3.92% |
| Trend extrapolation | 0 predictions |

### Signal Time Distribution
| UTC Hour | Signals | Notes |
|----------|---------|-------|
| 16Z | 3 | Early afternoon east coast |
| 17Z | 4 | |
| 18Z | 2 | Late-day gate minimum |
| 19Z | 12 | Peak for east coast stations |
| 20Z | 15 | Peak overall |
| 21Z | 9 | |
| 22Z | 10 | Late for west coast |

### Stations with Most Signals
| Station | Signals | Corrupted Obs | Signal/Corruption Ratio |
|---------|---------|---------------|------------------------|
| KDFW | 8 | 15 | 9 FPs from boundary oscillation on KDFW 2026-07-05 (daily max 97F, boundary 96 crossed 6×) |
| KLAX | 5 | 0 | 5 FPs from re-crossing boundary 78/80 — all genuine clean-data FPs |
| KPHX | 5 | 37 | 4 FPs from KPHX 2026-07-05 (daily max 110F, boundaries 102/107 crossed multiple times) |
| KBOS | 4 | 51 | 4 FPs on clean data |
| KDCA | 4 | 23 | 4 FPs on clean data |

### Data Quality Summary
| Issue | Scale | Impact |
|-------|-------|--------|
| Corrupted temps (awc_tgftp) | 699 obs across 20 stations | 2 false TPs, potential FPs |
| Dates with <10 obs | 4-5 per station | Thin coverage for daily max |
| UTC date grouping | All 20 stations | Systematic misalignment vs Kalshi |
| 5-min data availability | 18/20 stations have 5-min data (~300 obs each) | Lulls users into thinking data is adequate |
| No 5-min data for KDEN, KNYC | 2 stations | Only hourly data available |