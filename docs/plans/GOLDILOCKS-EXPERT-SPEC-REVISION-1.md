# Goldilocks Expert Spec — Revision 1

**Date:** 2026-08-12 02:40 UTC
**Author:** Donna Paulsen (integrating 5 expert reviews + bucket analysis + v2 characterization)

## Executive Summary

This revision updates the Goldilocks Expert Spec (v1 at `docs/plans/GOLDILOCKS-EXPERT-SPEC.md`) with the results of the bucket boundary characterization analysis and Dan's corrections. The analysis confirmed five experts' original verdict but revealed a restructured opportunity.

**From: "Detect a 1-minute temperature spike before Kalshi settles"**
**To: "Detect statistical bucket-boundary approach from T-group data in real-time, overlay as confidence modifier"**

The mechanism is different. The data resolution is coarser. But the insight — that temperature can cross integer boundaries transiently — is real and measurable at 5-15 minute cadence with METAR/T-group data.

---

## What Changed From v1

### Corrections (Dan's feedback)
- **BCF threshold was misleading**: 0.67% overall with 5-min gap filter, but 1.81% at KNYC (only station with real 1-min temp). Not <1% — the station with proper data is above the kill threshold.
- **"No cross" events matter**: Temperature spikes that approach a bucket boundary without crossing it are equally tradeable — the market doesn't know the difference if the boundary is statistically near.
- **50+ opportunities/year is fine**: At KNYC's 1.81% rate with 365 days = 6.6 events/year at a single station. With 20 stations at varying rates, even 0.67% overall = ~49 events/year across all stations. Rare but workable.
- **Keep both directions**: LOW markets are where Goldilocks events actually cluster (overnight). HIGH events are rare and METAR is good enough for daily max anyway.

### New Findings (v2 analysis)

#### 1. METAR Fidelity Gap — Settled
- **Only 2.35% of days** have ≥1°F difference between METAR-sampled and true 1-min daily max
- Mean absolute error: **0.04°F** — negligible for directional prediction
- 909/938 KNYC days (96.9%) show exactly 0°F difference
- **Implication: METAR is sufficient for HIGH market daily max prediction**
- Spending effort on 1-min data for HIGH markets yields no material improvement

#### 2. Reversion Speed — Fast But Feasible
- Median reversion: **3.0 minutes** aggregate
- Per-station median range: 2.25-4.0 minutes
- Peak bin: 2-3 minutes (53 of 122 events)
- No events revert in <1 minute — execution timing is not a barrier
- **Goldilocks window is 2-4 minutes** for most events

#### 3. Time-of-Day Clustering — The Big Signal
- **Goldilocks is a nighttime story**
- Circular mean hour: **23:04** (late evening)
- Concentration r: **0.60** (strong clustering compared to baseline 0.02)
- Only 19.7% of events occur during daytime (7am-7pm)
- Peak hours: 23:00, 03:00, 05:00 — late evening through pre-dawn
- **Mechanism**: Overnight low temperatures cross bucket boundaries during:
  - Cold front passage (temperature drops abruptly, crosses boundary downward)
  - Radiational cooling reversal (temperature stops dropping, rises through a boundary)
  - Stationary boundary oscillations (temperature hovers at the edge of a bucket)
- **For LOW markets**: Goldilocks events cluster when the overnight low is at risk, which is exactly when LOW markets settle

#### 4. Station Ranking — KLAX Emerges as Best Candidate

| Rank | Station | Rate/Day | Clean Rate | Median Rev | Notes |
|---|---|---|---|---|---|
| 1 | KLAX | 0.0190 | 0.0084 | 3.0 min | Best clean event rate |
| 2 | KNYC | 0.0181 | 0.0021 | 2.5 min | Most DCF-flagged, but only 1-min station |
| 3 | KHOU | 0.0127 | — | — | Moderate rate |
| 4 | KAUS | 0.0106 | — | — | Moderate rate |
| 5 | KSEA | 0.0084 | — | — | — |
| 6 | KDCA | 0.0089 | — | — | — |
| — | KPHX | 0.0021 | — | 2.25 min | Lowest rate |
| — | KLAS | 0.0021 | — | 4.0 min | Lowest rate, slowest reversion |

- **KLAX**: Best clean Goldilocks rate (~3/year without sensor artifacts)
- **KNYC**: Second best rate but 12/17 events flagged by dewpoint correlation filter
- **KPHX/KLAS**: Effectively zero Goldilocks opportunity

#### 5. Regime Correlation — No Smoking Gun
- **No condition reached 2x likelihood ratio** for Goldilocks events
- Wind speed: 7.05 kt (events) vs 7.61 kt (baseline) — slightly calmer at event times
- Wind direction shift >45°: 1.2x ratio (mildly elevated)
- Season: DJF/MAM slightly favored
- **Dominant signal is night-time clustering, not macro regime**
- This means Goldilocks events can't be predicted by wind thresholds or frontal passages alone — they require real-time detection

#### 6. Sensor Artifacts — The Big False Positive

- **Self-heating bias (SHR > 0.6)**: 0 events flagged across all stations
  - Even calm, sunny airport locations have enough wind to ventilate the sensor
  - SHR is a conceptual risk, not a practical problem for airport ASOS units
- **Dewpoint correlation (DCF > 0.3)**: 117 of 122 events flagged
  - At KNYC: 12 of 17 events flagged
  - At KLAX: 151 of 252 events flagged (checking all events, not just Goldilocks)
  - **Warning**: DCF at 0.3°F threshold may be too tight for 10-15 minute gaps
  - Normal dewpoint change over 15 minutes can exceed 0.3°F
  - Need per-station, per-gap-length adaptive DCF thresholds

---

## Revised Opportunity Structure

### What Works

| Component | Data Source | Cadence | Status |
|---|---|---|---|
| Daily max/min prediction | METAR | 5-15 min | **Good enough** — METAR sufficient |
| Bucket boundary approach | T-group (0.1°C) | 5-15 min | **Viable** — statistical approach detectable |
| Goldilocks spike detection | IEM ASOS 1-min | ≤1 min | **Dead** — no real-time source, sparse temp |
| Microstructure trend overlay | T-group + METAR | 5-15 min | **To build** — integrate as confidence modifier |
| Self-heating artifact filter | METAR wind + solar | per observation | **Deploy** — use adaptive DCF |
| Low-liquidity discount | Kalshi market data | per trade | **Deployed** — Tier 1/2/3 multiplier |

### What Doesn't

| Component | Why It Fails |
|---|---|
| Standalone Goldilocks lane | Not enough events to trade as independent strategy; 0.67% BCF overall |
| 1-minute temperature | 19/20 stations don't have it; KNYC has it but no real-time feed |
| Self-heating as primary filter | Airport wind keeps sensors ventilated — SHR never >0.6 |
| HIGH market Goldilocks | METAR already samples daily max within 1°F 97.6% of the time |
| Regime-based prediction | No condition reaches 2x likelihood — events are conditionally unpredictable |

### Revised Architecture

```
Instead of:  GoldilocksSignal → standalone lane → position
     Use:  T-group trend detection → f024 confidence modifier → SignalRegistry pipeline
```

The surviving approach is to:
1. Monitor METAR/T-group observations at 5-15 minute cadence
2. Detect when temperature is statistically approaching a bucket boundary (0.1°C increments)
3. If price hasn't fully adjusted → predict which direction the daily extreme will move
4. Apply as a confidence modifier (not a standalone signal) in the GEFS pipeline
5. Target LOW markets specifically (overnight clustering)

**No machine learning. Deterministic math only.** The trend is computed as:
- Short-term moving average (3-5 observations) vs medium-term average (10-15 observations)
- Boundary proximity: distance from current temp to next integer °F threshold
- Price dislocation: difference between current market price and what boundary-approach would imply

### Next Actions

1. **Integrate this revision into the Goldilocks lane** — mark as confidence modifier, not standalone lane
2. **Build T-group trend overlay** — add to SignalRegistry as f024
3. **Update falsification tests** — kill conditions still apply but with revised thresholds:
   - BCF ≥5% → now 1.81% at KNYC (meets investigate, not greenlight)
   - Sharpe ≥1.5 → still requires backtesting
   - Self-heating ≤30% → confirmed (0% in practice)
4. **Scale back IEM ASOS investment** — data is archival, 12-36h latency, cannot support real-time trading
5. **Further investigation**: KLAX microstructure (cleanest events), overnight LOW clustering mechanism

### Files
- Bucket analysis: `scripts/goldilocks_bucket_analysis.py`, `scripts/goldilocks_bucket_analysis_v2.py`
- Expert spec v1: `docs/plans/GOLDILOCKS-EXPERT-SPEC.md`
- This revision: `GOLDILOCKS-EXPERT-SPEC.md` (in-place update)
