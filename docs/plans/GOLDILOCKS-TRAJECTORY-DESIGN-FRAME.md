# Goldilocks & Trajectory — Design Session Frame

**Workstream leads:** Donna (orchestration), Gilfoyle (technical), Gray Room experts (design)
**Status:** DESIGN PHASE — NOT YET BUILT

---

## Goldilocks Lane (original concept, restored)

**What it is:** A real-time microstructure alert lane, separate from the daily GEFS pipeline.

**How it works (Dan's description):**
- Current temp is 84°F. Next Kalshi bucket is 85-86.
- Temp pops to 85°F for 1-2 minutes — a fleeting tick, not sustained.
- Most market participants can't catch this. The alert fires so you CAN.
- Prediction variant: temp is 84.8°F and the trend is warming. Probably going to hit 85. Trade before it ticks.

**What it is NOT:**
- ❌ NOT a daily directional signal (that was killed at 49.85%)
- ❌ NOT an ML model (the LightGBM thing was scope creep)
- ❌ NOT a GEFS pipeline integration

**Requirements:**
- Sub-minute METAR tick monitoring
- Bucket boundary detection (temp approaching a strike)
- Fleeting temperature spike detection (1-2 minute pops)
- Trend extrapolation from partial ticks (84.8 → likely 85)
- Alert mechanism (separate from GEFS trade signals)
- Separate paper trading history

**Not in scope:** Daily directional prediction, model training, feature engineering.

---

## Trajectory Lane (original concept, restored)

**What it is:** A weather-trajectory matching system that informs trade selection, not dictates it.

**How it works (Dan's description):**
- Current conditions: 85°F specific humidity X, pressure Y.
- A string of epochs brought us here (yesterday was 80°F, two days ago 78°F, etc.).
- Based on historical data, what happens next? When we've seen this pattern before, what bucket(s) did we land in?
- Which bucket(s) should we trade for tomorrow?

**What it is NOT:**
- ❌ NOT a gate that blocks trades (the Gray Room's "trajectory confirmation gate" was wrong)
- ❌ NOT a complex multi-variable gate module
- ❌ NOT a daily override of the GEFS signal

**What it IS:**
- A guide — helps trade selection, doesn't break trades
- Historical pattern matching (analog ensemble approach, but trajectory-focused)
- Multiple-epoch lookback (not single-step)
- Bucket-level recommendation (not just direction)

**Not in scope:** Overriding GEFS signals. Complex gate logic. Daily direction prediction (GEFS handles that).

---

## Pipeline Configuration (from sweep results)

Dan's picks for the GEFS pipeline:

| Component | Status | Rationale |
|-----------|--------|-----------|
| GEFS ensemble fraction calibration | ✅ Baseline (66.2%) | Best accuracy |
| Gaussian signal | ✅ KEEP | Highest P&L ($91K) — accuracy is secondary |
| Forecast disagreement | ✅ KEEP | 64.55%, closest to baseline, adds robustness |
| Station sizing | ✅ KEEP | Proven +$7K P&L, better Sharpe |
| Goldilocks lane | 🔄 DESIGN PHASE | Separate lane, not a signal |
| Trajectory lane | 🔄 DESIGN PHASE | Trade guide, not a gate |
| Everything else | 🗑️ DROPPED | No signal beat the baseline |

**Next step:** Gray Room session for Goldilocks and Trajectory design.