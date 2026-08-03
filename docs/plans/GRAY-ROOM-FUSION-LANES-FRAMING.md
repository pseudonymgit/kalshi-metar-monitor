# Gray Room Session — Fusion + Lanes Design

**Date:** 2026-08-03
**Status:** PRE-DISPATCH — awaiting Dan go-ahead

---

## SOP Protocol

### Rounds
1. **Individual expert rounds** — each expert receives the same base packet, answers their specific question independently. Time-boxed.
2. **Panel discussion** — experts review each other's findings, debate disagreements, resolve conflicts.
3. **Synthesis** — Donna consolidates all outputs into the session synthesis document.

### Output categories
Every finding is classified into one of:

| Category | Meaning |
|----------|---------|
| **ERRORS** | Something is broken, needs fix. Include severity (🔴/🟡/🟢), impact, and fix. |
| **IDEAS** | Unproven. May add value if validated. Include test or spec. |
| **IMPROVEMENTS / SPECS** | Ready to build. Include effort estimate and implementation path. |
| **ELEPHANTS** | The obvious thing nobody wants to say. Stated plainly. |

### Disposition rules
Every item gets one of:
- **ADVANCE** → buildable spec, ready to implement
- **PARK** → viable but needs more data or a prerequisite
- **KILL** → not worth building, document why

No item leaves without a disposition. No "let's think about this" without a concrete recommendation.

---

## Base Packet (Same for All Experts)

### System State (as of 2026-08-03 08:40 UTC)

**What's running:**
- GEFS ensemble-mean-threshold model: **66.17% accuracy**, 2,096 trades/year, +$50,166 P&L, Sharpe 11.36
- Direction-specific calibration: UP/DOWN bins, empirical win rate per bin
- 20 stations, all verified against Kalshi settlement data
- Kalshi API price feed (live, 0.50 fallback)
- Corrected Kelly formula: `edge/(1-c)` with market price reference
- Risk controls: RiskManager + StopLossMonitor
- Station sizing: -50% on 4 losers (KLAS, KPHL, KMDW, KMIA), +20% on top 4 winners
- ECMWF backfill: PID 958921, at 2025-02-25, 758 dates, still running
- Full 8-signal sweep completed: NONE beat the GEFS baseline

**Data available:**
- GEFS archive: 363,440 rows, 2,036 dates, 31 members, 20 stations, 9 steps
- ECMWF TIGGE: 75,799 rows, 758 dates, 50 members, 20 stations (backfilling)
- Kalshi settlements: 6,151 rows, 1,750 dates, 20 stations
- METAR backfill: 385MB, 20 stations, 2021-2026
- ERA5 reanalysis: 40,760 rows, 20 stations, 2021-2026
- NWP forecasts: 417,180 rows, 4 models (GFS, ECMWF, ICON, GEM), 2,045 dates
- Calibration curves: direction-specific, all 20 stations

**Gray Room history (relevant to this session):**
- R13 Expert 3: "Bayesian cascade > voting ensemble" — the core reason for this session
- R13/R14: 26 findings, 19 ADVANCE — all implemented
- FP-MULTI-SIGNAL-FUSION.md: 497-line design doc for the 3-layer cascade
- SIGNAL-REVIEW-FIRST-PRINCIPLES-2026-08-03.md: 24-item review of all signals
- GOLDILOCKS-TRAJECTORY-DESIGN-FRAME.md: original concept restorations

**Key reference documents:**
- `docs/plans/FP-MULTI-SIGNAL-FUSION.md` — Bayesian cascade design
- `docs/plans/GOLDILOCKS-TRAJECTORY-DESIGN-FRAME.md` — lane designs
- `docs/plans/SIGNAL-REVIEW-FIRST-PRINCIPLES-2026-08-03.md` — signal review
- `docs/plans/GRAY-ROOM-ROUND13-EXPERT3-SIGNAL-FUSION.md` — original Bayesian cascade recommendation
- `core/signals/gaussian_signal.py` — 48-day z-score reversion
- `core/signals/forecast_disagreement_signal.py` — 7-day disagreement
- `core/goldilocks_predictive.py` — existing (ML-based, will be replaced)
- `core/frontal_detector.py` — existing (unwired)

---

## Questions

### Question 1: Bayesian Cascade Fusion
**Framing:** R13 Expert 3 recommended "Bayesian cascade > voting ensemble" because signals predict different things at different timescales and cannot be averaged at the same layer. The 3-layer design exists but was never built.

**Assigned experts:** E1 (Bayesian Statistician), E2 (Signal Fusion Architect), E7 (Adversarial Analyst)

**What we need:**
- Design the cascade math: prior → posterior update rules for each signal type
- Validate the 3-layer decomposition: temperature belief → settlement belief → bet sizing
- Temporal freshness weighting: half-lives per signal (GEFS 6h, frontal 2h, nowcast 1.5h)
- Implementation path for the GEFS cron pipeline
- ECMWF 82-member integration: how does the second ensemble pool enter the cascade?

**What we DON'T need:**
- ❌ New signals — stick to what exists
- ❌ ML models — deterministic math only
- ❌ Architectural review — we're past that, we need a buildable spec

**Output expected:** ERRORS (if any assumption is wrong), IDEAS (if the cascade opens new paths), IMPROVEMENTS/SPECS (the cascade math), ELEPHANTS (the obvious thing about fusion that nobody says).

---

### Question 2: Goldilocks Lane
**Framing:** Original concept: sub-minute METAR temperature tick monitoring at bucket boundaries. When temp pops to 85 for 1-2 ticks (fleeting), alert so we can trade. Prediction variant: temp is 84.8 and warming → probably going to hit 85 → trade it. The ML model (LightGBM) was scope creep — restore to the alert concept.

**Assigned experts:** E3 (Microstructure Engineer), E6 (Meteorological Analyst), E7 (Adversarial Analyst)

**What we need:**
- Alert trigger logic: what constitutes a "fleeting tick"? Minimum tick duration? Minimum temperature delta?
- Trend extrapolation from partial ticks: 84.8 → probability of hitting 85
- Separate lane architecture: data flow that doesn't touch the GEFS pipeline
- Minimum viable backtest: how to validate this without live tick data
- Weather physics validation: does a 1-minute tick at 85°F actually mean anything physically? Or is it sensor noise?

**What we DON'T need:**
- ❌ ML models — the LightGBM thing was scope creep
- ❌ Daily directional signals — killed at 49.85% negative EV
- ❌ Integration into the GEFS pipeline — separate lane

**Output expected:** SPEEC (the alert logic), ERRORS (if the METAR data can't support sub-minute ticks), ELEPHANTS (obvious issues with sub-minute trading), IDEAS (prediction variant design).

---

### Question 3: Trajectory Lane
**Framing:** Original concept: 85°F with X humidity and Y pressure. A string of epochs brought us here. Based on historical data, what happens next? When we've seen this pattern before, what bucket(s) did we land in? Which bucket(s) should we trade for tomorrow? A trade GUIDE, not a gate — it HELPS trade selection, doesn't break trades.

**Assigned experts:** E4 (Pattern Matching Specialist), E6 (Meteorological Analyst), E7 (Adversarial Analyst)

**What we need:**
- Trajectory matching method: epoch-sequence matching (not k-NN on raw data)
- Minimal feature set: temperature, humidity, pressure — what else? What's sufficient?
- Bucket-level recommendation output: not just direction, which bucket(s) to trade
- Integration as a guide, not a gate: how does it interact with the GEFS pipeline without overriding?
- Historical data sufficiency: do we have enough epochs for meaningful pattern matching?

**What we DON'T need:**
- ❌ Complex gate logic — the trajectory_confirmation_gate .pyc was scope creep
- ❌ Trade breaker — this should HELP trades, not block them
- ❌ Single-step prediction — the trajectory is a multi-epoch string

**Output expected:** SPECS (the matching algorithm), ERRORS (if data is insufficient), ELEPHANTS (if the pattern matching doesn't generalize), IDEAS (for how trajectory informs bucket selection beyond direction).

---

### Question 4: ECMWF 82-Member Integration Path
**Framing:** ECMWF backfill is at 758 dates (2021-01-01 to 2025-02-25) and running. When complete, we'll have 82 members (31 GEFS + 51 ECMWF). The fusion design must account for this as two ensemble pools with different physics.

**Assigned experts:** E1 (Bayesian Statistician), E5 (Implementation Engineer)

**What we need:**
- Two-pool fusion: does the Bayesian cascade handle GEFS and ECMWF as separate pools?
- Correlation handling: what if GEFS-ECMWF correlation is >0.85? Equal pooling vs weighted?
- Implementation path: how to add the second pool without breaking the current pipeline
- Testing strategy: how to validate that 82 members beat 31

**What we DON'T need:**
- ❌ Waiting for the backfill to complete before designing — design the integration now
- ❌ Re-testing signals that we already swept

**Output expected:** SPECS (the 82-member integration), ERRORS (if the cascade math doesn't extend), ELEPHANTS (if 82 members don't actually help).

---

## Expert Roster

| Expert | Role | Model | Questions | Panel? |
|--------|------|-------|-----------|--------|
| E1 | Bayesian Statistician | luna-pro | Q1 (cascade math), Q4 (82-member integration) | Yes |
| E2 | Signal Fusion Architect | luna-pro | Q1 (3-layer decomposition) | Yes |
| E3 | Microstructure Engineer | luna-pro | Q2 (Goldilocks alert logic) | Yes |
| E4 | Pattern Matching Specialist | luna-pro | Q3 (trajectory matching) | Yes |
| E5 | Implementation Engineer | luna-pro | Q4 (integration path), all (build path) | Yes |
| E6 | Meteorological Analyst | luna-pro | Q2 (tick physics), Q3 (weather patterns) | Yes |
| E7 | Adversarial Analyst | gpt-5.4 (if available) or luna-pro | All (red-team all designs) | Yes |

---

## Panel Discussion

After individual rounds, all 7 experts review each other's findings. Panel format:
1. Each expert presents their key finding (2 min)
2. Disagreements are debated (5 min per conflict)
3. Cross-cutting themes identified
4. Every item final disposition confirmed

**Panel rules:**
- No repetition of already-stated findings
- Disagreements must be resolved with data, not opinion
- If experts disagree, both positions get dispositions (ADVANCE the one with evidence, PARK the other)
- The elephant in the room gets called out explicitly — if nobody names it, Donna names it

---

## Output Format

All findings go into a consolidated table per category:

### ERRORS
| # | Finding | Expert(s) | Severity | Impact | Fix | Disp |
|---|---------|-----------|----------|--------|-----|------|

### IDEAS
| # | Idea | Expert(s) | Impact | Test/Spec | Disp |
|---|------|-----------|--------|-----------|------|

### IMPROVEMENTS / SPECS
| # | Spec | Expert(s) | Effort | Detail | Disp |
|---|------|-----------|--------|--------|------|

### ELEPHANTS
| # | Elephant | Expert(s) | Why It's Uncomfortable | Disp |
|---|----------|-----------|------------------------|------|

### PANEL DISCUSSION
| Topic | Disagreement | Resolution | Disp |
|-------|-------------|------------|------|

### CLEANUP STATUS
| Category | Total | ADVANCE | PARK | KILL |
|----------|-------|---------|------|------|

### WHAT TO DO NEXT
| Order | Item | Effort | Type | Depends On |
|-------|------|--------|------|------------|