# Gray Room Prompt Design — First Principles

**Method:** Before designing the session, strip down what makes a Gray Room session work.

---

## The Gray Room Prompt Design Problem

The previous session had 7 experts, 4 questions, 66 findings. Too many experts, too many questions. The outputs were valuable but **disagreements exceeded resolutions.** The panel had to resolve 5 major conflicts.

First-principles: a Gray Room session should produce **divergence-reduced, actionable output.** Every expert disagreement costs synthesis time and increases the chance that nothing gets built.

**The failure modes of the previous session:**
1. Too many experts (7) → conflicting findings that needed panel resolution
2. Too many questions (4) → each expert could only go surface-deep on each
3. Base packet was too large → experts couldn't read everything
4. No differential analysis → experts didn't compare "what was built" vs "what was wanted"

---

## Redesigned Protocol for This Session

### 1. One session per lane

Goldilocks and Trajectory are fundamentally different problems. Don't combine them in one session.

- **Session A: Goldilocks Lane Redesign** — microstructure alert system
- **Session B: Trajectory Lane Redesign** — epoch-sequence pattern matcher

### 2. Fewer experts, sharper questions

3 experts per session, each with ONE question. No overlapping questions.

### 3. Differential analysis built in

Each expert receives:
- "What Dan originally wanted" (the concept)
- "What was actually built" (the implementation)
- "What failed" (the diagnostic results)
- Their job: identify the divergence and design the fix

### 4. Build-ready output only

No "this might work" findings. Every item must be:
- A spec (algorithm, inputs, outputs, edge cases)
- A build estimate (hours or days)
- A test plan (how we know it works)

### 5. Pre-read is mandatory, limited, and scoped

Each expert gets exactly 3 documents to read:
1. The one-page concept
2. The diagnostic report
3. The question brief (one page)

No more. If they need more data, they fetch it themselves.

---

## Session A: Goldilocks Lane Redesign

### Concept (One Page)

**What Dan originally wanted:**
- Monitor bucket boundaries in real-time
- When temp fleetingly crosses a boundary (too short for most to catch), alert
- Prediction variant: temp is 84.8°F and warming → probably going to hit 85 → trade it
- Separate lane from the GEFS pipeline

**What was actually built:**
- LightGBM ML model (scope creep)
- Spike-reversion signal in metar_monitor.py (detects new-max spikes, not bucket crosses)
- `instant_cross_revert` extraction (3.92% precision — 5 compounding bugs)

**What failed (diagnostics):**
- ASOS chain: 10-sec samples → 5-min rolling average → whole °C encoding. A 1-minute spike is invisible.
- Data corruption: 699 observations with impossible temps (59,480°F)
- UTC/local date misalignment: hardcoded UTC late-day gate
- Detection can't distinguish "transient spike" from "boundary oscillation"
- Kalshi rounding bias: 84.6°F → reported as 85°F

**What needs to be true for viability:**
- Detection precision > 0.70, recall > 0.50
- Works on METAR data (no exotic feeds required)
- Sub-second to sub-minute response time
- Separate from GEFS pipeline

### Experts

| Expert | Role | Question | Model |
|--------|------|----------|-------|
| E1 | Microstructure Market Engineer | Design the alert trigger logic. Given ASOS constraints (5-min average, whole °C), what detection algorithm achieves >70% precision? Must work on current METAR data with no exotic feeds. Include: minimum tick duration, temperature delta threshold, false positive filter, Kalshi rounding compensation. | luna-pro |
| E2 | Sensor Physics / Data Scientist | Model the ASOS measurement chain end-to-end. Given the 10-sec sample → 1-min integer → 5-min average → whole °C encoding pipeline, what signals survive? Is the self-heating model correctable? What's the theoretical maximum precision on METAR data vs IEM 1-minute data? | luna-pro |
| E3 | Adversarial Analyst | Stress-test all designs. What happens when temp oscillates at a boundary for 3 hours? What if 84.8°F stays at 84.8°F? What's the worst-case false positive rate? | luna-pro |

### Output Format

Each expert produces a spec document in `docs/plans/GOLDILOCKS-REDESIGN-EXPERT-N.md`:

1. The algorithm (pseudocode or Python)
2. Test plan (how to validate)
3. Build estimate (hours)
4. Assumptions and risks
5. Differential analysis: what went wrong before and why this design is different

---

## Session B: Trajectory Lane Redesign

### Concept (One Page)

**What Dan originally wanted:**
- Current conditions: 85°F with X humidity and Y pressure
- A string of epochs brought us here (the trajectory)
- Based on historical data, what happens next? When we've seen this pattern before, what bucket(s) did we land in?
- Which bucket(s) should we trade for tomorrow?
- A trade GUIDE, not a gate — helps selection, doesn't break trades

**What was actually built:**
- `p3_trajectory_tracer.py` — matches settlement epochs (intraday price dynamics), not weather epochs
- `test_trajectory_gate.py` — binary veto gate, not matching system
- `trajectory_confirmation_gate.pyc` — orphaned, unbuildable
- DTW research spike — `normalize_features()` never called, temperature dominates 74% of distance, temporal leakage 98.8%

**What failed (diagnostics):**
- `normalize_features()` was defined but never called — dead code
- Temperature domination: 74% of DTW distance because pressure, wind, dewpoint unnormalized
- Temporal leakage: 98.8% same-station matches because candidate window overlaps query dates
- KMDW temp_f=41486.0 — corrupted sensor data not filtered
- Wind direction encoding broken (0-360 → unreliable feature)
- Effective feature set: 1-2 features out of 5
- 26.3% bucket agreement vs 31.6% climatology baseline — worse than predicting most common bucket

**What needs to be true for viability:**
- Bucket agreement > 60% (must clearly beat random)
- Works with current METAR/ERA5 historical data
- Climate-zone pooling must add signal, not noise
- Integration as weighted modifier on GEFS sizing, not override

### Experts

| Expert | Role | Question | Model |
|--------|------|----------|-------|
| E1 | Time Series / Pattern Matching Engineer | Design the trajectory matching algorithm. Given 5+ years of METAR/ERA5 data per station, what matching method achieves >60% bucket agreement? Must handle: corrupted data filtering, feature normalization, temporal deduplication, climate-zone pooling. Output spec must include: distance metric, feature weights, sequence length, match count threshold, confidence calculation. | luna-pro |
| E2 | Synoptic Meteorologist | Select the correct features. Is 5 features (T, Td, P, WS, WD) sufficient for air mass identification? What's the minimum set? What's the optimal set? Should pressure be sea-level or station-level? Should wind direction be treated as categorical or continuous? Should we add 850mb temperature? Precip? Cloud cover? | luna-pro |
| E3 | Adversarial Analyst | Stress-test the trajectory designs. What happens when DTW finds a match from March for an August query? What if two stations in the same climate zone have opposite trajectories? What's the maximum credible bucket agreement on this dataset? | luna-pro |

### Output Format

Each expert produces a spec document in `docs/plans/TRAJECTORY-REDESIGN-EXPERT-N.md`:

1. The algorithm (pseudocode or Python)
2. Test plan (how to validate)
3. Build estimate (hours)
4. Assumptions and risks
5. Differential analysis: what went wrong before and why this design is different

---

## Session C: Panel Discussion (Joint)

After both individual sessions, a joint panel where all 6 experts review each other's designs:

1. Goldilocks E1 and Trajectory E1 present their algorithms (5 min each)
2. Cross-cutting issues: can Goldilocks use trajectory data? Can trajectory use Goldilocks events?
3. Adversarial analysts (E3s from both) present stress-test findings
4. Each design gets final disposition: ADVANCE (build now), PARK (needs more data), KILL (not viable)
5. Build order: Goldilocks first (2-3d) or Trajectory first (17h)? Or neither?

---

## Execution Order

1. Dispatch Session A (Goldilocks) — 3 experts in parallel
2. Dispatch Session B (Trajectory) — 3 experts in parallel  
3. After both complete: dispatch Session C (Joint Panel)
4. Synthesize all output into `docs/plans/GOLDILOCKS-TRAJECTORY-REDESIGN-SYNTHESIS.md`
5. Dispatch B-mode loop to implement