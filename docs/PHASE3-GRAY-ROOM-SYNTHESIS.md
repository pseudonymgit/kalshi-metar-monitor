# Phase 3: Epoch-Based Analog Forecasting — Gray Room Synthesis

**Date:** 2026-06-17
**Panel:** 7 experts (State Machine, Signal Grammar, Market Microstructure, Scoring Engine, Infrastructure, Trajectory, Calibration)
**Model:** ollama/deepseek-v4-pro:cloud (GPT-5.4 was unreachable)
**Disposition:** ADVANCE — design is coherent, implementable, and respects all L0-L4 invariants

---

## 1. State Machine Invariants (State Machine Architect)

### Core Principle
Prediction layer (P3) reads from measurement layer (L0-L4) but NEVER writes to it. This is the firewall.

### Epoch Definition Invariants
- Epoch boundaries are measurement-driven, not prediction-driven
- Epoch identity is immutable once sealed
- Epoch granularity is uniform across the replay corpus
- Epoch metadata is a function of measurements only
- Epoch adjacency is a measurement-layer relation

### Admissible Transitions
| Transition | Condition |
|---|---|
| L0→L1→L2→L3→L4 | Standard measurement pipeline |
| L4 → P3_read | Prediction reads sealed L4 settlement |
| P3_match → P3_project | Analogs identified, forward trajectories extracted |
| P3_project → P3_output | Projection written to prediction namespace |

### Inadmissible (Must Be Rejected)
- P3_output → any L* layer (prediction cannot inject, recalibrate, fill gaps, seal epochs, or alter settlement)
- L4 → L3 (settlement is terminal, no downgrade)
- Any P3 write to measurement namespace

### Replayability Guarantees
- Deterministic analog matching (same inputs → same analog set)
- Measurement-layer immutability under replay
- Prediction outputs versioned and non-destructive (keyed by run_id, epoch_id)
- Forward trajectories are measurement-grounded (actual recorded future, not simulation)
- Replay order enforced: L0→L4 must complete before any P3 read
- Idempotent settlement (fixed-point or rational representation)

### Failure Modes
1. Prediction-as-observation injection (self-referential loop)
2. Epoch boundary leakage (prediction confidence influences when epoch seals)
3. Settlement contamination (forecast fills missing observations)
4. Analog corpus contamination (synthetic outputs stored as historical analogs)
5. Feature drift via feedback (prediction error adjusts calibration)
6. Replay non-determinism from mutable prediction state
7. Namespace collision (predictions and measurements in same table)
8. Temporal ordering violation (P3 runs on partially-settled epoch)

---

## 2. Signal Grammar — Epoch Similarity Matching (Signal Grammar Architect)

### Epoch Signature Fields

| Field | Type | Match Rule |
|---|---|---|
| `temp_trend_slope` | float (K/hr) | Absolute difference ≤ threshold |
| `temp_trend_r2` | float [0,1] | Bin match (high/medium/low) |
| `temp_volatility` | float (K) | Absolute difference ≤ threshold |
| `temp_range` | float (K) | Absolute difference ≤ threshold |
| `temp_acceleration_sign` | enum {+,-,0} | Exact match |
| `temp_acceleration_magnitude` | float (K/hr²) | Absolute difference ≤ threshold |
| `excursion_count` | int | Bin match (0,1,2,3+) |
| `excursion_amplitude_mean` | float (K) | Absolute difference ≤ threshold |
| `excursion_duration_mean` | float (hr) | Absolute difference ≤ threshold |
| `excursion_direction_ratio` | float [0,1] | Bin match |
| `normalized_time_position` | float [0,1] | Absolute difference ≤ threshold |
| `seasonal_phase` | float [0,1] | Circular distance |
| `preceding_duration_signature` | float[] | Cosine similarity |
| `preceding_trend_signature` | float[] | Cosine similarity |

### Similarity Computation
- Field-level distance d ∈ [0,1] per field
- Weighted sum: total_distance = Σ(w_i × d_i)
- Similarity = 1 - total_distance
- Match threshold τ = 0.75 (configurable)
- Tie-breaking: prefer closest normalized_time_position
- Minimum 3 analogs required before forecast generation
- Optional recency decay: (1 - λ × age_rank), λ small (~0.02)

### Excluded Fields (Future Leakage)
- absolute_temperature_mean/min/max (leaks absolute climate state)
- epoch_end_value (future knowledge)
- subsequent_epoch_duration/trend (what we're trying to predict)
- calendar_date/timestamp (leaks seasonal expectation)
- total_series_duration_at_epoch_end (leaks remaining data)
- Any forward-window statistic
- External data (forecasts, ENSO indices — engine must be self-contained)

### Minimum Viable Signature (5 fields)
1. temp_trend_slope
2. temp_volatility
3. excursion_count
4. normalized_time_position
5. seasonal_phase

---

## 3. Market Microstructure Constraints (Market Microstructure Analyst)

### Compatible Kalshi Market Types
- HIGH temperature ladder markets (between rungs + terminal greater)
- LOW temperature ladder markets (between rungs + terminal less)
- Single event ticker per station-day per market type
- Markets must be OPEN status at evaluation time
- 7 mapped stations: KDEN, KLAX, KNYC, KPHL, KMDW, KMIA, KAUS

### Mechanical Constraints
- **No live trading exists.** Phase 2 is public-only API reads. RSA auth is dormant.
- **Ladder hydration prerequisite:** Market evaluation blocked if cache invalid/missing
- **Directional strike window:** Only 3 nearest directional rungs retained
- **Single market per evaluation:** No multi-rung position spreading
- **Rate limiting:** Self-throttled per station
- **Execution domain guard:** Live calls blocked in observability/diagnostics/audit/replay
- **No spread or liquidity awareness:** yes_bid/ask/no_bid/ask are display-only
- **No position sizing logic:** No Kelly criterion, bankroll management, risk-per-trade

### Settlement Structure Constraints
- NWS observations are sole settlement source
- Monotonic settlement progression (never retreats within a day)
- Publication lag buffer: 90 seconds
- Observation acceptance grace: 600 seconds (10 min staleness possible)
- Station-local day reset at midnight (epoch predictions spanning day boundary are invalid)
- Terminal state: once greater/less rung reached, no further transitions
- Settlement epoch lifecycle: new epoch on settlement_up, closes on next settlement_up/day_reset/terminal
- Observation timestamp monotonicity: corrected/revised observations with same/older timestamps dropped

### Failure Modes
1. **Correct range prediction, no eligible market** — rung outside 3-rung directional window
2. **Correct prediction, ladder not hydrated** — cache stale/missing → HYDRATION_BLOCKED
3. **Correct prediction, terminal state already reached** — no trades possible
4. **Correct prediction, wrong settlement epoch** — new settlement_up between prediction and execution
5. **Correct direction, wrong rung granularity** — 1°F rung width vs multi-degree prediction range
6. **Correct prediction, observation lag kills entry** — 10-min-old reading, market already repriced
7. **Correct prediction, day boundary reset** — midnight severs epoch continuity
8. **Correct prediction, NWS correction changes settlement** — retroactive settlement revision
9. **Correct prediction, market illiquidity** — no counterparty at required size
10. **Correct prediction, Kalshi API failure** — market status unconfirmable at execution time

### Minimum Viable Signal Strength
- Prediction confidence ≥ 0.80 (from calibration framework)
- Station reliability floor: SR ≥ 0.60 for ≥ 50% of contributing stations
- Effective sample size Neff ≥ 15
- Market must be OPEN and within directional window
- Hydration cache must be valid for current trading day
- Epoch must not be in terminal state
- Observation recency ≤ 5 minutes (tighten from 10-min grace for trading)

---

## 4. Deterministic Scoring Engine (Scoring Engine Designer)

### Match Quality Scoring

**Feature Vector (14 dimensions):**
```
settlement_jump_magnitude, reversion_occurred, max_excursion_above_settlement,
duration_at_or_above_seconds, duration_strictly_above_seconds, terminal_state_reached,
transition_count, day_fraction_at_settlement, prior_settlement_bucket, settlement_bucket,
reversion_latency_seconds, goldilocks_emitted, station (exact gate), market_type (exact gate)
```

**Dimension Weights (invariant, never learned):**
- settlement_jump_magnitude: 3 (primary structural signature)
- reversion_occurred: 2 (strong discriminator)
- terminal_state_reached: 2 (structurally distinct)
- reversion_latency_seconds: 2 (key behavioral signature)
- goldilocks_emitted: 2 (high-signal discriminator)
- All others: 1
- station, market_type: ∞ (exact match gate)

**Distance Functions:**
- Integer: d = |a - b|
- Binary: d = 0 if equal else 1
- Float: d = |a - b| / MAX_RANGE (fixed per-dimension constants, never data-normalized)

**Match Score:**
```
raw_distance = Σ(weight[i] × distance_i)
match_score = 1.0 - (raw_distance / max_possible_distance)
```

**Thresholds:**
- Strong match: ≥ 0.85 (eligible for trajectory tracing)
- Weak match: 0.70–0.85 (recorded, traced only if strong matches absent)
- No match: < 0.70 (discarded)

### Trajectory Strength Scoring
- Trajectory = forward sequence of 3 epochs following matched epoch
- Trajectory vector: mean settlement jump, reversion rate, terminal rate, mean excursion, mean durations, mean transition count, goldilocks rate, settlement trend, mean reversion latency, trajectory length
- Consensus: cluster trajectories by outcome similarity; report cluster weights
- Conflicting trails: report all clusters with weights; no forced consensus

### Precedence Resolution (Conflicting Trails)
1. Cluster trajectories by outcome vector similarity
2. Rank clusters by: (member_count × mean_match_score)
3. Primary projection = highest-ranked cluster's mean outcome
4. Secondary projections = other clusters above minimum weight threshold
5. If top two clusters within 20% weight of each other → report as "divided"

### Sparse vs Abundant Data
- **Sparse (< 5 closed epochs):** Enter sparse mode — use relaxed τ = 0.65, minimum 1 analog, flag output SPARSE
- **Abundant (≥ 30 strong matches):** Use top-K (K=10) ranked by match_score × recency_factor
- **Normal (5–29):** Use all strong matches, fall back to weak if < 3 strong

### Replayability Invariants
- Fixed weights, fixed thresholds, fixed MAX_RANGE constants
- No data-normalization, no learned parameters, no random sampling
- Same epoch history + same query → bit-identical match set and projection
- Versioned output namespace, never overwrites

### Output Format
```
PROJECTION: {station} {market_type} epoch {id}
PRIMARY: settlement_bucket={X} (weight={W}%, N={count})
SECONDARY: [if divided] settlement_bucket={Y} (weight={W}%, N={count})
MATCHES: {strong_count} strong, {weak_count} weak
TOP ANALOG: {date} (score={S}, trajectory_outcome={O})
CONFIDENCE: {C_final} ({band})
```

---

## 5. Infrastructure Constraints (Infrastructure Refactor Engineer)

### Storage and Query
- **SQLite sufficient.** Existing `settlement_epochs` table has all needed fields.
- **Index required:** Composite `(station, market_type, local_trading_date)` — not currently present.
- **Query pattern:** Point-lookup by station/market_type with date-range filter. No JOINs needed.
- **DB path:** `/var/data/alerts.db`. Render persistent disk must be confirmed durable.
- **Growth bound:** ~5,110 rows/year (7 stations × 2 types × 365 days). SQLite handles millions.

### Computational Bounds
- ~14 epochs/day across all stations. Trivially small.
- Scan cost: ~365 rows per query (1 year lookback). Single-digit milliseconds.
- **Caching:** Prediction outputs cached by `(station, market_type, query_params_hash, last_epoch_id)`. Invalidate on new epoch commit only. No TTL needed.
- **No wall-clock dependency** in prediction path. Use only committed epoch data.

### API Gating
- **Zero new Kalshi calls** if prediction is purely epoch-history-based.
- If current market prices needed: use existing hydration cache, not inline API calls.
- Prediction must follow execution-domain gating (no live calls from observability).
- If fresh market data needed: enqueue hydration, never call Kalshi inline.

### Determinism Failure Modes
1. **DB loss on redeploy** — Render ephemeral disk wipes epoch history → prediction impossible
2. **Clock skew in timestamp comparisons** — use epoch sequence numbers, not wall clocks
3. **API timeout during hydration** — prediction must degrade gracefully with stale cache
4. **Concurrent prediction and ingestion** — prediction must read from committed epoch snapshot, not live DB
5. **Index missing** — full-table scans grow with history, eventually timeout

### Scheduling
- Prediction runs AFTER daily ingestion/settlement pipeline completes
- Trigger: post-settlement hook (after L4 commit for all active stations)
- Not time-based cron — event-driven from settlement completion
- Separate from hydration queue worker
- Prediction computation is read-only, can run in any execution domain

---

## 6. Trajectory Analysis (Sequence/Trajectory Analyst)

### Markov Properties
- **Weather is NOT first-order Markov.** N+1 depends on more than N.
- **Recommendation:** Use trajectory-as-state with sliding window K ≥ 3 epochs.
- Single-epoch Markov rejected — identical single-epoch matches can lead to radically different outcomes depending on preceding trend.

### Consecutive Match Weighting
- Consecutive matches exponentially more informative than isolated:
  - Isolated: weight = 1.0
  - 2 consecutive: weight = 3.0
  - 3 consecutive: weight = 9.0
  - N consecutive: weight = 3^(N-1), capped at 5
- Partial overlap: keep longest run, discard subsets
- Gap tolerance: 1-epoch gap gets 0.5 penalty multiplier

### Minimum Trajectory Length
- **3 epochs minimum** for predictive information
- **5 epochs** sweet spot (full diurnal cycle + trend)
- Beyond 7-10: diminishing returns, historical record thins
- Operational rule: ≥ 3 consecutive matches to activate trajectory prediction; below that, fall back to single-epoch baseline with LOW CONFIDENCE flag

### Temporal Proximity Weighting
- **Seasonal-phase proximity matters, not calendar proximity.**
- Same calendar week ±3 days, any year: weight = 1.0
- Same week + same ENSO/regime phase: weight = 2.0
- Adjacent week (±7-14 days): weight = 0.7
- Different season: weight = 0.2
- Within-season different month: weight = 0.5
- **Do NOT apply simple exponential decay by calendar distance.** 365-day-old match is phase-aligned, not decayed.

### Chaos-Theoretic Failure Modes
- **SDIC (Sensitive Dependence):** Lyapunov time ~3-5 days for synoptic weather. 5-epoch match → ~2-3 days useful projection before error doubles.
- **Attractor basins:** System may be in different basin than any historical analog → all matches misleading.
- **Phase transitions:** Weather regime changes (e.g., monsoon onset, sudden stratospheric warming) have no historical precedent in record → prediction impossible.

### Regime Change Detection
- Monitor divergence rate: if current trajectory's distance from all historical analogs is increasing over consecutive epochs, flag REGIME_CHANGE.
- When flagged: suppress prediction, emit "unprecedented conditions" alert.
- Detection threshold: mean similarity to top-5 analogs drops below 0.50 for 3 consecutive epochs.

### Overfitting Prevention
- Minimum 3 analogs required (never predict from single match)
- Fixed weights, fixed thresholds — no tuning to historical data
- Out-of-sample validation: when a new epoch closes, compare prediction to actual outcome; track Brier score per station
- Short records (< 90 days): flag STATION_YOUNG, reduce confidence weight

---

## 7. Calibration & Confidence Framework (Calibration/Confidence Designer)

### Confidence Factors (6 factors, weights sum to 1.0)

| Factor | Weight | Computation |
|---|---|---|
| Sample Size (N) | 0.25 | min(1.0, log10(N)/log10(30)). N_min=30. Floor 0.1 below 5. |
| Distribution Shape (Kurtosis) | 0.20 | 1.0 - clamp(excess_kurtosis/6, 0, 1) |
| Inter-Station Agreement (ISA) | 0.20 | 1.0 - (σ/μ) clamped to [0,1] |
| Station Reliability (SR) | 0.15 | 1.0 - mean Brier score over last 90 days |
| Temporal Proximity (TP) | 0.10 | exp(-0.15 × Δt_hours) |
| Signal Consensus Direction | 0.10 | |p_up - p_down| |

### Combination Formula
```
C_raw = Σ(weight_i × factor_i)
C_final = C_raw × M_penalty
```

**Multimodal penalty (M_penalty):**
- Unimodal: 1.0
- Bimodal: 0.70
- Trimodal+: 0.50

Detection: Hartigan's dip test p < 0.05 OR ≥ 2 KDE peaks with prominence ≥ 0.15

### Interpretation Bands
- ≥ 0.80: HIGH — actionable
- 0.60–0.80: MODERATE — actionable with uncertainty flag
- 0.40–0.60: LOW — informational only
- < 0.40: INSUFFICIENT — suppress alert

### Thresholds
| Action | Threshold | Additional |
|---|---|---|
| Alert emission | C ≥ 0.60 | — |
| Trade recommendation | C ≥ 0.80 | SR ≥ 0.60 for ≥ 50% of stations |
| Automated action | C ≥ 0.90 AND N ≥ 50 | Human-in-the-loop override required |
| Epoch+N (N ≥ 2) | C ≥ 0.50 | Tagged LONG-RANGE — SPECULATIVE |

### Temporal Decay
```
C_epoch+N = C_final × exp(-0.25 × N)
```
Epoch+1: 0.78×, Epoch+2: 0.61×, Epoch+3: 0.47×. Floor: 0.10.

### Calibration Failure Modes
1. **Overconfidence:** High N, low kurtosis, poor ISA. Detection: Brier drift > CI for 3 epochs. Mitigation: boost ISA weight.
2. **False Precision:** High N, low station diversity. Detection: Neff = N/(1+(N-1)×ρ) < 0.3×N. Mitigation: use Neff.
3. **Consensus Collapse:** ISA ≈ 1.0, SR < 0.40. Mitigation: hard-cap C at 0.50.
4. **Stale Calibration:** SR not updated > 7 days. Mitigation: reduce SR weight, flag STALE.

### User-Facing Output Format
```
PREDICTION: {epoch_label} | {variable} | {value} {units}
CONFIDENCE: {C_final} ({band})
EFFECTIVE N: {Neff} stations ({N} raw, ρ={correlation})
DISTRIBUTION: {unimodal|bimodal|trimodal} | kurtosis={value}
STATION SPREAD: σ/μ = {ratio} | best station: {name} (SR={value})
DECAY: epoch+{N} projection | base confidence {C_epoch+N}
[MULTIMODAL BLOCK — if applicable]
[WARNINGS — if any failure mode triggered]
RECOMMENDATION: {none|monitor|consider|act} — {rationale}
```

---

## 8. Synthesis: What Phase 3 Looks Like

### Architecture
```
L0 (raw METAR) → L1 (validated) → L2 (calibrated) → L3 (epoch sealed) → L4 (settlement)
                                                                              ↓
                                    P3_read ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← 
                                       ↓
                                    P3_match (feature extraction + similarity scoring)
                                       ↓
                                    P3_trace (forward trajectory lookup from analogs)
                                       ↓
                                    P3_project (consensus/conflict resolution)
                                       ↓
                                    P3_calibrate (confidence scoring)
                                       ↓
                                    P3_output → alert/trade recommendation
```

### Key Design Decisions
1. **Firewall:** P3 reads L4, never writes to L0-L4. Separate prediction namespace.
2. **Scoring:** Fixed weights, fixed thresholds, no learned parameters. Deterministic and replayable.
3. **Trajectory:** Sliding window K≥3, consecutive match bonus 3^(N-1), seasonal-phase proximity.
4. **Confidence:** 6-factor weighted sum × multimodal penalty. Honest about "no consensus."
5. **Infrastructure:** SQLite sufficient, one new composite index, zero new Kalshi calls for pure prediction.
6. **Market:** No live trading yet. Trade recommendation requires C≥0.80 + SR floor + market OPEN + within directional window.

### Implementation Order
1. **DB migration:** Add composite index on `settlement_epochs(station, market_type, local_trading_date)`
2. **Feature extractor:** Build epoch → feature vector function (14 dimensions)
3. **Match engine:** Similarity scoring with fixed weights and thresholds
4. **Trajectory tracer:** Forward lookup, cluster analysis, consensus resolution
5. **Calibration engine:** 6-factor confidence scoring with multimodal detection
6. **Output formatter:** Structured prediction message template
7. **Scheduler:** Post-settlement hook trigger
8. **API endpoint:** `/api/prediction/<station>/<market_type>` (read-only, no Kalshi calls)

### Deferred
- AI/ML gate (per Dan's instruction)
- Live trading execution (Phase 4)
- Trade/Position Sizing Advisor (separate Gray Room session when needed)
- Multi-rung position spreading
- Spread/liquidity analysis

---

## Disposition

**ADVANCE.** The design is coherent across all 7 domains. No contradictions between experts. The firewall between measurement and prediction is well-defined. Scoring is deterministic and replayable. Confidence is honest about uncertainty. Infrastructure changes are minimal (one index). Market constraints are documented but Phase 3 is prediction-only — trading is Phase 4.

**Next action:** Route to Gilfoyle for implementation. Start with DB migration and feature extractor.
