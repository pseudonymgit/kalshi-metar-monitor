# Gray Room Round 13 — Expert 1: Meteorologist — Intraday Forecast Evolution

---

## 1. GEFS Issue Time Predictive Power for Daily HIGH

### Ranking (1 = most predictive):

| Rank | GEFS Cycle | Available (UTC) | Lead to Typical HIGH | Skill Level | Rationale |
|------|-----------|-----------------|---------------------|-------------|-----------|
| **1** | **12Z** | ~16-17Z | 1-6h | **Highest** | Initialized at sunrise East Coast = most data-rich synoptic hour (radiosondes + aircraft + satellite assimilation). Captures pre-diurnal boundary layer state. |
| **2** | **06Z** | ~10-11Z | 7-12h | **High** | Available before trading day begins. Gives synoptic setup and ensemble fraction that drives 67.1% accuracy. |
| **3** | **18Z** | ~22-23Z | -1 to +3h | **Low for daily HIGH** | After HIGH for East Coast. Useful for West Coast. |
| **4** | **00Z (D-1)** | ~04-05Z | 14-20h | Lowest | Longest lead time; highest model drift. |

### Skill Decay: NOT monotonic — U-shape with inverted ramp

- **00Z → 06Z (overnight):** Skill RISES ~8-12% as lead time shrinks and observations assimilated
- **06Z → 12Z (morning):** Skill rises further (+3-5%)
- **12Z → 18Z (afternoon):** Skill DECLINES practically — 18Z often after East Coast HIGH occurred
- **18Z → settlement:** Pure METAR nowcast — model irrelevant

**The 06Z vs 12Z trade-off is operationally critical:** 12Z is more accurate, but available ~16-17Z — may miss optimal entry for East Coast. 06Z (available ~10-11Z) is the best balance of accuracy + remaining trading time.

**Operational recommendation:** Use 06Z for initial entry (67% baseline), 12Z for confirmation/scale-up on West Coast stations.

---

## 2. Optimal Intraday Signal Timing

### 2A. Frontal Passage Nowcast

| Timing (UTC) | What | Signal | Action |
|---|---|---|---|
| 09-12Z | Compare target vs upstream (NW→SE for cold fronts) | Weak | Flag potential |
| 12-15Z | Gradient: 5+°F warmer than upstream stations | **Moderate** | Reduce UP confidence |
| 15-18Z | 3+°F drop in 1h at upstream = front approaching | **Highest** | Override GEFS if front hits before HIGH |
| 18-21Z | Direct observation at target | Nowcast | If temp drops, daily HIGH already set |

**Key insight:** A cold front approaching 15-20Z can **cap** daily HIGH, invalidating GEFS prediction regardless of ensemble fraction.

**Operational rule:** When frontal nowcast confidence ≥ 0.65, apply 0.5× modulation to GEFS confidence. If frontal arrival < 3h before typical HIGH, invert direction.

### 2B. Dewpoint Depression (DPD) Modulator

| Time (UTC) | DPD Signal |
|-----------|----------------|
| 06-09Z | Pre-dawn — least affected by mixing |
| **12-14Z** | **Most predictive single measurement** |
| 14-16Z | DPD trend — rising (clearing) or falling (capping) |
| 18-20Z | Last check — HIGH largely determined |

**Recommended thresholds:**
```
DPD ≥ 15°F: 1.2× confidence (clear)
DPD 10-15°F: no modulation
DPD 5-10°F: 0.85× (partial cloud)
DPD < 5°F: 0.65× (extensive cloud, HIGH cap)

Trend (12→15Z):
  Rising > 3°F/3h: +0.1 to modulator
  Falling > 3°F/3h: -0.15 from modulator
```

**Strongest in summer.** Weaker in winter.

---

## 3. High-Confidence Prediction Timing

| Time (UTC) | Data | Confidence | Action |
|---|---|---|---|
| 04-05Z | 00Z GEFS | 40-50% | Regime only — do not enter |
| **10-11Z** | **06Z GEFS + pre-dawn METAR** | **58-65%** | **Primary entry window** |
| 12-14Z | Morning METAR + DPD | 65-72% clear sky | Confirm trajectory |
| **14-15Z** | **All morning data + upstream** | **68-75%** | **Optimal confirmation** |
| 16-17Z | 12Z GEFS | 70-78% | Best for West Coast |
| 18-20Z | Near-settlement | 75-85% EC | Position management |

---

## 4. Seasonal Failure Modes

### Summer (Jul-Aug): Key Risks

| Failure Mode | Mechanism | Impact | Mitigation |
|---|---|---|---|
| **Convective outflow** | Thunderstorm downdraft outflow — temperature drops 5-15°F in minutes | HIGH prediction invalidated | Frontal nowcast can't detect — sub-METAR scale. Use radar (not available). Accept as irreducible noise. |
| **Mesoscale convective system (MCS)** | Large organized thunderstorm complex develops unexpectedly | Caps HIGH 3-8°F below GEFS | GEFS spread often doesn't capture convective development well. **This is the biggest summer failure mode.** |
| **Shallow cumulus vs clear** | Scattered afternoon clouds limit insolation — HIGH 2-4°F below CAPE-based forecast | Systematic bias | DPD modulator helps. High DPD (~15°F) morning → if shallow cumulus develops anyway, GEFS overpredicts. |
| **Sea breeze** | Coastal stations: cool marine air pushes inland afternoon | Cools by 3-8°F at KNYC/KLAX/KSFO | Frontal nowcast can detect sea breeze as "reverse front." **Add sea breeze detection for coastal stations.** |

### Winter (Dec-Feb): Key Risks

| Failure Mode | Impact | Notes |
|---|---|---|
| Cold air damming east of Appalachians | KNYC/KBOS/KDCA stay 5-10°F colder than GEFS | Well-handled by GEFS ensemble spread if properly calibrated |
| Post-frontal temperature recovery | GEFS slow to warm after front passage | GEFS underestimates high by 3-5°F on day after front |
| Snow cover albedo | Snow reflects solar radiation → HIGH 2-5°F lower than clear-snow-free GEFS | Snow cover is a separate variable — GEFS handles well in winter |
| Inversion breakthrough | Strong surface inversion burns off late → temperature jumps 10°F in 2h | Frontal nowcast can't detect. Use METAR temperature ramp rate as check. |

---

## 5. Optimal Re-evaluation Cadence

### Recommendation: Hybrid — Time-based (GEFS cycles) + Event-driven (METAR triggers)

**Time-based (required):**
- **06Z → 12Z → 18Z**: Re-evaluate all positions
- Each GEFS refresh triggers a full re-evaluation
- Cost: 3 re-evaluations/day × adjustable positions

**Event-driven (cheap, no friction):**
- **Frontal passage detected:** Immediate re-evaluation of affected station(s) — modulate confidence but don't change position unless confidence crosses threshold
- **DPD trend violation:** If 12→15Z DPD drops 5+°F, re-evaluate confidence
- **METAR temperature anomaly:** If target station temperature differs from GEFS by > 5°F for 3 consecutive METARs, trigger re-eval

**Cost analysis:** Most event-driven triggers are confidence modulations (zero friction cost) rather than position changes (friction cost). The only friction events are GEFS-led full re-evaluations (3/day). This is well below the daily budget for friction costs.

**Operational flow:**
```
06Z: Full eval → enter/scale positions (GEFS fraction + DPD + regime)
12Z: Check DPD → modulate confidence (zero cost)
14Z: Check DPD trend → modulate or confirm
16Z: 12Z GEFS arrives → full eval for West Coast, confirmation for East
18Z: Last GEFS → position management only
Event (any time): Frontal → modulate | METAR anomaly → check
```

---

## Summary: Top 5 Recommendations

1. **Use 06Z GEFS for primary entry (~10-11Z)** — best balance of accuracy and remaining trading time. Use 12Z GEFS for West Coast confirmation/scaling only.
2. **Implement DPD threshold system** as a no-cost confidence modulator — strong summer skill for predicting HIGH ceiling. Morning DPD ≥ 15°F = 1.2× multiplier; < 5°F = 0.65×.
3. **Frontal nowcast confidence threshold at 0.65** triggers 0.5× GEFS confidence modulation. If arrival time < 3h before typical HIGH window, invert direction. This catches the most common summer failure mode.
4. **Add sea breeze detection** for coastal stations (KNYC, KLAX, KSFO, KSEA) — can be detected via the same frontal nowcast logic (onshore gradient develops morning, pushes inland afternoon). This is a free additional signal using existing infrastructure.
5. **Use 14-15Z as the decision point** for East Coast positions — by this time you have 06Z GEFS processed, 4+ hours of METAR trajectory, morning DPD, upstream station check, and 3+ hours to settlement. This is the best balance of information completeness and remaining trading time.