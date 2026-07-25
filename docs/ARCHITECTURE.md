# Canonical Operational Architecture

## System Purpose

Kalshi METAR Monitor is a deterministic live-trading reliability system.

Its production purpose is to:
- derive active station authority from currently tradable Kalshi weather markets,
- ingest METAR observations for those market-authoritative stations,
- emit deterministic transition-driven alerts for `HIGH` ladders by default (with `LOW` also supported when `KALSHI_TARGET_MARKET_TYPE=HIGH,LOW`),
- preserve exact replay equivalence with live execution,
- detect Goldilocks structural events that can create temporary trader-awareness asymmetry.

Goldilocks structural-event detection is part of live trading reliability, not optional analytics.

## Deterministic Authority Flow

Market Availability (Kalshi listings)
→ Station Authority Set
→ Execution Domain
→ Transition History
→ Replay Domain
→ Observability Domain
→ Scoring Domain

Runtime pipeline order clarification: observation ingest → transition classification / emission → signal evaluation → alert gating → market evaluation (which may enqueue hydration when cache prerequisites are missing) → delivery attempts, with audit writes occurring both before delivery for certain structural transition records and after delivery for successful composed alerts.
This sequence defines the canonical live execution path used by runtime diagnostics and observability interpretation.


## Station-Universe Resolution Order

Canonical live station scope resolves in this strict order:

1. Market-derived station universe from live Kalshi discovery when discovery/hydration path is available.
2. Deterministic fallback union when market-derived resolution is unavailable:
   - configured station list,
   - runtime state station keys,
   - watchlist stations,
   - discovered series station keys.

This preserves market-authoritative intent while keeping execution deterministic during upstream market-discovery disruption.

## Canonical Control Surface

HTTP endpoint definitions are canonicalized in `docs/API_REFERENCE.md`.

That reference is authoritative for endpoint domain assignment, execution authority, safety boundaries, and data-source mapping.

## Domain Model

### Market Authority Domain

- Markets are the source of truth for active stations.
- Station configuration can scope or filter monitoring, but cannot override market authority.
- METAR ingestion follows market availability so ingestion and settlement interpretation stay aligned.

### Execution Domain

- Produces deterministic state progression from canonical observations under frozen Phase 1 semantics.
- Settlement bucket progression is monotonic for each station-day.
- Alerts are transition-driven and emitted from authoritative execution transitions only.
- Holds transition authority.
- Transition authority transfers atomically at emission creation inside the authoritative evaluation cycle.
- No intermediate mutable transition state may exist outside that cycle.

Runtime transition taxonomy clarification: the full runtime transition set is `instant_up`, `instant_down`, `settlement_up`, `reversion_after_settlement`, and `goldilocks_reversion`.
If another document presents a simplified transition model, treat it as a conceptual summary rather than the authoritative runtime taxonomy.

### Historical Domain

- Stores canonical historical observations and committed transition history.
- Historical observations are replay reconstruction authority.
- Committed transition history is immutable.

### Replay Domain

- Reconstructs deterministic system state from historically valid deterministic system state produced under Phase 1 semantics.
- Must reproduce live transitions exactly (station, direction, settlement bucket, alert outcomes).
- External initialization values are prohibited.
- Stored transition history may be used for validation comparison.
- Stored transition history is not replay reconstruction authority.

### Observability Domain

- Provides deterministic execution visibility and audit integrity.
- Is strictly epistemic.
- Must not participate in execution causality.
- Must not initiate live Kalshi API calls.
- Operates from persisted runtime state and cached market data snapshots.
- Cache hydration is owned by execution/ingestion pathways, not observability endpoints.
- May expose deterministic artifacts produced by downstream deterministic derivation layers operating within architectural constraints.
- Shall not assume or define signal-layer authority.

### Scoring Domain

- Defines deterministic scoring classification derived exclusively from emitted transition history.
- Is strictly post-transitional.
- Possesses zero execution authority.
- Time normalization is derived exclusively from deterministic transition ordering or epoch-relative positional metrics.
- Must not depend on wall-clock timestamps or execution duration.

### Security Domain

- Protects deterministic legitimacy, authority protection, and boundary integrity.
- Does not participate in execution behavior.
- Replay may validate against committed transition history.
- Replay reconstruction authority derives exclusively from canonical historical observations governed under Phase 1 semantics.

## Replay Guarantees

- Replay execution remains behaviorally identical to production execution under Phase 1 semantics and deterministic architecture constraints.
- Replay initialization state is derived exclusively from historically valid deterministic system state.
- External initialization values are prohibited.
- Replay acceptance requires exact transition parity with live history, not approximate similarity.
- Stored transition history may validate replay but is not replay reconstruction authority.

## Observability Constraints

- Observability is strictly epistemic.
- Observability must not participate in execution causality.
- Observability endpoints must not call Kalshi.
- Observability reads persisted runtime state plus cached market data only.
- Cache hydration expectations must be explicit in operations docs and runbooks.
- Observability may expose deterministic artifacts from downstream deterministic derivation layers within architectural constraints.
- Observability shall not assume or define signal-layer authority.
- Alert absence alone must never be used to infer hydration failure or discovery failure; operators must corroborate with `/observability/runtime-authority-snapshot` and market-coverage surfaces.
- `/observability/pipeline-truth` is currently a partial/non-authoritative troubleshooting surface and must not be treated as canonical execution truth.

## Governance Invariants

DO NOT:
- introduce ML execution
- introduce probabilistic execution paths
- smooth temperature transitions
- suppress rapid reversions
- reinterpret Goldilocks events statistically
- shift execution authority outside Execution domain

## Contributor Guardrails

- Preserve Phase 1 behavioral semantics as immutable baseline.
- Preserve market-derived station authority.
- Preserve `HIGH` default monitoring behavior and configured `LOW` + `HIGH` symmetric behavior when enabled.
- Keep alerts transition-driven only.
- Do not introduce execution causality into Replay, Observability, Scoring, or Security domains.
- Do not use external initialization values for Replay.
- Do not treat transition history as replay reconstruction authority.
- Keep scoring normalization independent of wall-clock timestamps and execution duration.
- Any proposed Phase 1 behavioral modification requires explicit versioning and explicit documentation updates.


## Runtime Pipeline Order

The deterministic runtime pipeline executes in the following order:

observation ingest  
→ transition classification / emission  
→ signal evaluation  
→ alert gating  
→ market evaluation (may enqueue hydration when cache prerequisites are missing)  
→ delivery attempts

Audit writes can occur:
- before delivery for certain structural transition records
- after delivery for successful composed alerts

This ordering reflects the runtime gating logic implemented in transition handling, alert gating, and market evaluation checks.

## Deterministic Signal Layer

The signal layer runs inside `_process_temperature_event()` after transition classification and before temperature state commit. It is deterministic and replay-safe because it depends only on:

- observation timestamps
- observation temperatures
- deterministic in-memory runtime state

No wall-clock functions (`time.time()`, `datetime.now()`) are used in signal decision paths.

## Deterministic Alert Origins

Two deterministic alert origins exist:

- Structural alerts originate from authoritative transition emission.
- Signal alerts originate from deterministic signal-layer evaluation, still require transition gating, and may produce audited alert records independent of transition emission objects.

### `near_boundary_momentum_up`

Emits only when all are true:
- `0 < distance_to_integer <= 0.10°F`
- last 3 observations are monotonic non-decreasing with strictly increasing observation timestamps
- `x3 - x1 >= 0.05°F`
- momentum slope `>= 0.002°F/s`
- hydration cache valid and eligible market count > 0
- station and boundary cooldown constraints are satisfied

The informational metric `pressure_to_boundary_seconds` is included in payload context and does not gate emission.

### `goldilocks_reversion_alert`

Tracks per-station deterministic settlement epoch state after `settlement_up`:
- settlement bucket at up
- max post-up temperature
- exceeded (`+1.2°F`) flag
- reverted (`-0.2°F`) flag
- alert emitted flag

Alert emits once per epoch after spike+reversion are both observed, subject to station cooldown and hydration/eligibility suppression checks.

---

## Phase 10-14 Modules (added 2026-07-24/25)

### `core/dual_hypothesis_engine.py` — H1 vs H2 Hypothesis Testing

Introduced in Phase 10 to replace the old monolithic GoldilocksSignal interpretation. Tests two competing hypotheses for every detected structural event:
- **H1 (Overreaction):** The observed spike is a transient overreaction that will revert within the settlement window
- **H2 (Underreaction):** The observed spike represents a genuine regime shift that will persist

Runs in shadow mode — produces signals for ensemble but does not independently execute trades.

### `core/metar_qc_parser.py` — Quality Control Parsing

Parses METAR observation quality-control flags to filter observations with known instrumental errors, human transcription mistakes, or automated sensor faults. Applied as a pre-filter in the Goldilocks detection path (`goldilocks_qc_filter`).

### `core/settlement_execution_gate.py` — Trade Gating

Scenario-classification gate that determines whether a signal should produce a trade or be suppressed based on settlement window proximity, expected volatility, and risk parameters. Prevents stale or premature entries.

### `core/station_rank_selective.py` — Per-Station Selection

Applies station-ranking logic to activate Goldilocks and related signals only on major hub stations. This prevents thin-market issues on low-volume stations while conserving system resources. Selection is deterministically derived from market volume and station rank.

### `core/rolling_calibration.py` — Rolling 30-Day Calibration

Maintains a rolling 30-day calibration window for each active signal and station. Calibration parameters (thresholds, baselines) are continuously updated from the most recent 30 days of performance data. The window preserves Phase 1 determinism through strict timestamp-based edge alignment.

### `core/signal_fusion.py` — Bayesian Log-Odds Fusion

Implements MI-decorrelation + log-odds fusion to combine signals into a single coherent ensemble prediction. Steps:
1. Mutual information matrix computed across active signals
2. Signals decorrelated by weighting inversely to MI overlap
3. Log-odds aggregated into consensus prediction
4. Variance-weighted position sizing derived from consensus confidence

Replaces the previous equal-weighted ensemble.

### `core/lane_manager.py` — Three-Lane Architecture Manager

Formalizes the three-lane operational architecture:
- **Lane 1: Core Execution** — Alert engine, transition detection, METAR ingestion (Phase 1 semantics, always running)
- **Lane 2: Signal Layer** — All active signals, fusion, calibration (deterministic, replay-safe)
- **Lane 3: Trading Execution** — Paper test trades, settlement verification, P&L tracking (B-mode only)

Lanes are process-aware: Lane 3 cannot modify Lane 1 or Lane 2 state. Each lane has independent failure boundaries.

### `core/spatial_coherence.py` — Cross-Station Correlation

Computes and tracks spatial correlation between stations to prevent cluster-trading on correlated observations. Maintains a real-time correlation matrix of active stations and flags situations where multiple positions would be taken on highly correlated signals (e.g., two nearby stations both triggering on the same weather front).

### `core/paper_test_controller.py` — 30-Day Paper Test Controller

Orchestrates the three-phase paper test rollout:
- **Smoke Phase:** Single station, $100 capital, 24-hour validation
- **Stabilization Phase:** Expand to 5 stations, $500 capital, 7-day stability check
- **Autonomous Phase:** All active stations, $2,500 capital, 22-day unsupervised run

Manages graduation conditions, phase transition verification, and abort triggers.

### `core/production_gate.py` — Production Gates

Enforces the three production-allocation gates for real-money trading:
- **Gate 1:** Accuracy ≥ 60% (30-day rolling)
- **Gate 2:** Shadow mode minimum 30 days
- **Gate 3:** Sharpe ratio ≥ 0.30

All three gates must pass before any real-money allocation is permitted. Gate state is auditable and non-bypassable.

### `core/pnl_tracking.py` — P&L Tracking

Settlement-confirmed profit and loss tracking. Records every paper trade with:
- Entry and exit timestamps (settlement-confirmed)
- Signal and station identifiers
- Capital allocation and fee-adjusted P&L
- Rolling win rate, average return, and Sharpe ratio

All metrics are derived from committed settlement data, not estimated. Used by the production gate for accuracy and Sharpe verification.

### `core/risk_controls.py` — Risk Management

Implements skew-aware position limits and loss-distribution controls:
- Maximum position size per station
- Maximum correlated exposure across stations (from spatial coherence)
- Maximum daily drawdown (percentage-based stop)
- Skew-aware position sizing (penalizes heavy-tail loss distributions)

Runs as a pre-trade sanity check in Lane 3. Also includes StopLossMonitor integration for real-time stop-loss enforcement.
