# Gray Room: Grand Sweep Comprehensive Plan Design Review

**Date:** 2026-08-10
**Arbiter:** Donna Paulsen
**Format:** Full SOP protocol — each expert receives the same base packet, answers their specific question, and every output item gets ADVANCE/PARK/KILL.
**Instruction:** Correct and improve. Do NOT kill anything. Flag assumptions, identify gaps, suggest alternatives. Rate each component: KEEP / FIX / REPLACE.

---

## Base Packet (Same for All Experts)

### Context

The weather engine has been through 5+ sweep iterations with ad-hoc fixes. Issues found too late: P&L inflation (kelly_fraction up to 4.0), look-ahead bias (ECMWF signal using days[idx] instead of days[idx-1]), stub calibration metrics (temp_accuracy and market_accuracy both equal plain accuracy), misinterpreted "+0.25" consensus boost. The testing infrastructure has lost trust.

The mandate: produce a clean, comprehensive, trustworthy sweep from first principles. Build it once, build it right.

### Current State

**Data available:**
- `metar_backfill.db` — 12.8M METAR observations, 19 stations
- `gefs_archive.db` — GEFS ensemble, 365K rows, 6-31 members
- `tigge_archive.db` — ECMWF IFS 50-member ensemble, 181K rows
- `nwp_forecasts.db` — 6-model NWP (ECMWF, GFS, ICON, GEM, ERA5, GEFS), 462K rows
- `kalshi_settlements.db` — 6,311 settlement records, 20 stations, 40 markets
- `iem_asos_1min.db` — 754 MB, 1-minute ASOS observations
- `whale_watch.db` — Live Polymarket whale detection data
- `era5_archive.db` — ERA5 reanalysis, 40K daily records

**Active signals (15 firing):** gaussian (59.7%), gaussian_v2 (62.0%), pressure_delta (63.4%), corrected_pressure_delta (63.4%), forecast_disagreement (66.3%), calendar_climatology (65.6%), seasonal_regime (66.7% DJF), ecmwf_bias_corrected (86.1%), metar_trend (64.7%), persistence (51.9%), simple_trend (51.9%), wind_direction_shift (48.4%), spike_reversion (71.4%, low vol), goldilocks (72.2%, 18 trades), radiational_cooling (34.7%)

**Proposed 4-signal core:** ecmwf_bias_corrected, pressure_delta, seasonal_regime, wind_direction_shift (others are redundant clusters)

**42 sweep parameters** across 9 groups: SignalSpecific, Confidence, Gates, Levers, Modulators, Calibration, Validation, Regime, StationSizing

**3-layer cascade architecture:**
- Layer 1: Temperature belief (sequential narrowing tracker: DTW trajectory + HMM + live METAR + consensus boost)
- Layer 2: Settlement belief (goldilocks lane: microstructure detection)
- Layer 3: Decision & sizing (edge = P - price, 2-bucket sizing, WhaleWatch conviction, regime-aware thresholds)

**Key open issues:**
- Inverted optimization (confidence_floor=0.80 gives 68% acc, -$479 P&L, 119 trades; 0.55 gives 51.7% acc, +$490, 2,476 trades)
- JJA performance collapse (47.2% acc, -$264 P&L)
- 25 dead signals (6 root causes)
- forecast_disagreement geo-holdout gap (-30pp)
- temp_accuracy and market_accuracy are stubs (== plain accuracy)
- "+0.25" consensus boost was misinterpreted (not quarter-Kelly, it's agreement boost)

### Key Documents

Full comprehensive plan: `docs/plans/GRAND-SWEEP-COMPREHENSIVE-PLAN.md`
System architecture: `docs/plans/FP-SYSTEM-ARCHITECTURE.md`
Signal correlation analysis: `docs/weather-engine/SIGNAL_COMBINATION_ANALYSIS.md`
Prediction methods: `docs/weather-engine/PREDICTION_METHODS_EXPLORATION.md`
QC review: `docs/plans/QC-REVIEW-FP-SPECS.md`
Design discussion: `memory/2026-08-10-sweep-fix-specs-design-discussion.md`

---

## Expert Assignments

### Expert A — Model: openai/gpt-5.6-luna-pro
**Role:** Sweep Architecture & Calibration
**Focus:** The sweep design, calibration framework, validation protocol, and scalarized objective options.

**Questions:**
1. The sweep uses 3 proposed scalarized objective options (weighted sum, consensus bonus, Pareto front). Which is theoretically soundest? What are the tradeoffs?
2. The 6 acceptance criteria (acc ≥55%, sharpe ≥0.5, drawdown ≤0.5, P&L >0, trades ≥50, geo-holdout gap ≤10pp) — are these the right thresholds? What's missing?
3. The multi-target calibration (temp_accuracy, market_accuracy, P&L, sharpe, brier, maxDD) — is this the right set? What methodology for each target?
4. The walk-forward validation (80/20 time split) — is this sufficient? What about year-over-year validation?
5. The convergence criteria (last 20 configs < 0.5pp accuracy change) — is this sufficient to prevent early stopping?
6. The regime-specific calibration table (DJF 0.55, JJA 0.80 confidence floors) — is this approach sound or is there a better way?
7. What's the minimum number of configs needed for a trustworthy sweep?

**Output:** A structured critique of the sweep architecture. Every item gets ADVANCE/PARK/KILL. At least one improvement suggestion focused on accuracy with a short reason why it helps.

### Expert B — Model: openai/gpt-5.6-luna-pro
**Role:** Signal & Meteorology
**Focus:** The signal taxonomy, 4-signal core proposal, trajectory/DTW approach, HMM design, and dead signal disposition.

**Questions:**
1. The 4-signal core (ecmwf_bias_corrected, pressure_delta, seasonal_regime, wind_direction_shift) — is this sufficient? What meteorological dimensions are missing?
2. The 3 redundancy clusters (GEFS composite, METAR trend, spike detection) — is the correlation analysis correct? Is keeping one representative per cluster the right approach?
3. The trajectory rewrite as DTW sequence matching on METAR — is DTW the right method for meteorological pattern matching? What are the alternatives?
4. The Regime-Switching HMM (3-5 states, METAR features) — is this the right approach for regime detection? What latent states should be modeled?
5. The 25 dead signals — of the 10 that "genuinely don't work," which are worth fixing vs killing? What's the priority order?
6. The consensus boost (+X when N signals agree) — is +0.30 to +0.40 the right range for a 4-signal core? How should this be calibrated?
7. The ECMWF IFS Direct signal (50-member ensemble from tigge_archive.db) — is this the highest-impact new signal to build?

**Output:** A structured critique of the signal architecture. Every item gets ADVANCE/PARK/KILL. At least one improvement suggestion focused on accuracy with a short reason why it helps.

### Expert C — Model: openai/gpt-5.6-luna-pro
**Role:** Decision Layer & Quant Finance
**Focus:** The decision framework (edge = P - price, Kelly sizing, 2-bucket strategy, WhaleWatch conviction, regime-aware thresholds).

**Questions:**
1. Edge = P(T > B | trajectory) — Market_Price — is this the correct formula? What are the edge cases (negative edge, zero edge, stale prices)?
2. The 2-bucket sizing strategy (high-confidence gets Kelly, low-confidence gets fixed) — is this sound? What's the optimal split point?
3. WhaleWatch conviction multipliers (1.0 default, 1.25 DETECTED, 1.5 HIGH_CONVICTION) — are these values reasonable? What validation would confirm?
4. The quick-Kelly cap (0.25) — conservative enough? What's the risk of ruin at this level?
5. The stop_loss_kind=2 (trailing) — how should this work with binary options? What are the mechanics?
6. The three decisions (enter, exit, change) — is the "change" scenario well-defined? What triggers it?
7. The regime-aware confidence thresholds (DJF 0.55, JJA 0.80) — does this align with optimal Kelly sizing per regime?

**Output:** A structured critique of the decision layer. Every item gets ADVANCE/PARK/KILL. At least one improvement suggestion focused on accuracy with a short reason why it helps.

### Expert D — Model: openrouter/deepseek/deepseek-chat-v3.1
**Role:** Integration & Feasibility
**Focus:** The integration map, dependency chain, build effort estimates, sequencing, and risk assessment.

**Questions:**
1. The dependency chain (data → signal → fusion → gate → decision → sizing) — is this correct? What's the critical path for a working system?
2. The build effort estimates (HMM 3.5 days, trajectory 4.5 days, ECMWF 2 days, multi-model 3.5 days) — are these realistic? What's the risk of underestimation?
3. The sequencing (HMM first, then ECMWF, then multi-model, then trajectory) — is this the right order? What would you change?
4. The integration risk — what are the top 3 things that could break when integrating new modules with the existing pipeline?
5. The data dependency — what data sources are missing? What's the risk of data pipeline failure?
6. The A-Mode (live trading) vs B-Mode (backtest) separation — is the architecture clean enough for both?
7. What's the minimum viable system that could ship in 2 weeks?

**Output:** A structured integration and feasibility assessment. Every item gets ADVANCE/PARK/KILL. At least one improvement suggestion focused on accuracy with a short reason why it helps.