# REPLAY PARITY VISIBILITY SPEC

## Scope and Authority

This specification defines **operator visibility design** for replay-vs-live parity only.
It does not modify replay execution, ingestion ordering, transition emission logic,
scheduler cadence, or alert semantics.

Design objective: operators must deterministically answer:

> "Does replay confirm live reality?"

without manual comparison.

---

## 1) Parity Comparison Model

### 1.1 Canonical Comparison Unit

Parity is evaluated on a deterministic key called **Parity Event Unit (PEU)**:

- `station_id`
- `trading_day` (UTC day boundary used by execution domain)
- `event_sequence` (stable index within ordered transition history)
- `event_type` (`instant_up`, `instant_down`, `settlement_up`, `reversion_after_settlement`, `goldilocks_reversion`)
- `event_timestamp_utc`
- `instant_bucket`
- `settlement_bucket`

A replay run and live history are parity-comparable iff both can be normalized into the
same ordered PEU list.

### 1.2 Comparison Windows

Two deterministic windows are defined:

1. **Closed-Day Window**: full-day parity comparison when day is operationally complete.
2. **Rolling Intraday Window**: prefix comparison from day start through a specified cutoff timestamp.

Window type is recorded in parity output and must not be inferred.

### 1.3 Comparison Contract

For a given `(station_id, trading_day, window)`:

- Build `live_peu[]` from persisted live transition/epoch truth.
- Build `replay_peu[]` from replay output normalized to PEU schema.
- Compare by ordered sequence and payload equality.

Deterministic result states:

- **MATCH**: identical cardinality and element-wise equality.
- **MISMATCH**: at least one divergence point exists.
- **INCONCLUSIVE**: required source material missing/insufficient for a deterministic judgment.

### 1.4 Divergence Localization Rule

When `MISMATCH`, system reports first divergence index `k` and classifies:

- first index where `live_peu[k] != replay_peu[k]`, or
- append/truncation boundary when one list ends earlier.

This creates a single canonical “first break” location for operator action.

---

## 2) Deterministic Parity Taxonomy

### 2.1 Top-Level Parity Verdict Taxonomy

- `PARITY_MATCH`
- `PARITY_MISMATCH`
- `PARITY_INCONCLUSIVE`

Exactly one verdict is emitted per comparison instance.

### 2.2 Divergence Class Taxonomy (for `PARITY_MISMATCH`)

1. **SEQUENCE_LENGTH_MISMATCH**  
   Cardinality differs after normalizing window.

2. **EVENT_TYPE_MISMATCH**  
   Same sequence position, differing transition class.

3. **TIMESTAMP_MISMATCH**  
   Same sequence position/type, differing event timestamp.

4. **BUCKET_PAYLOAD_MISMATCH**  
   Same sequence position/type, differing instant/settlement bucket values.

5. **ORDERING_MISMATCH**  
   Equivalent event set but differing deterministic order.

6. **EPOCH_STATE_MISMATCH**  
   Settlement/reversion structural state disagrees (e.g., first reversion marker alignment).

### 2.3 Inconclusive Class Taxonomy (for `PARITY_INCONCLUSIVE`)

1. **LIVE_BASELINE_UNAVAILABLE**
2. **REPLAY_OUTPUT_UNAVAILABLE**
3. **NORMALIZATION_FAILURE**
4. **WINDOW_UNSATISFIABLE**

Inconclusive classes are deterministic and mutually exclusive per comparison result.

### 2.4 Severity Mapping

For operator visibility prioritization:

- **Critical**: `EVENT_TYPE_MISMATCH`, `ORDERING_MISMATCH`, `EPOCH_STATE_MISMATCH`
- **High**: `SEQUENCE_LENGTH_MISMATCH`, `BUCKET_PAYLOAD_MISMATCH`
- **Medium**: `TIMESTAMP_MISMATCH`
- **Info**: inconclusive classes

Severity affects visibility prominence only; not execution behavior.

---

## 3) Visibility Surfaces

### 3.1 Required Surfaces

1. **Parity Summary Surface** (station/day rollup)  
   Answers yes/no/inconclusive for each station-day and latest run.

2. **Parity Detail Surface** (single comparison record)  
   Exposes first divergence index, divergence class, and side-by-side PEU snapshot at break.

3. **Parity Timeline Surface** (historical trend)  
   Shows verdict evolution across repeated replay runs for same station-day.

4. **Operator Attention Surface**  
   Filters to unresolved mismatches and recent inconclusive outcomes.

### 3.2 Mandatory Fields per Comparison Record

- `comparison_id` (immutable)
- `station_id`
- `trading_day`
- `window_type` (`closed_day` | `rolling_intraday`)
- `window_cutoff_utc` (nullable for closed day)
- `verdict`
- `divergence_class` (nullable unless mismatch)
- `inconclusive_class` (nullable unless inconclusive)
- `first_divergence_index` (nullable unless mismatch)
- `first_divergence_live_event` (nullable unless mismatch)
- `first_divergence_replay_event` (nullable unless mismatch)
- `compared_event_count_live`
- `compared_event_count_replay`
- `comparison_generated_at_utc`
- `source_live_version_marker`
- `source_replay_run_id`

### 3.3 Deterministic Operator Queries

Surfaces must support deterministic queries:

- "latest verdict for station/day"
- "all mismatches by divergence class in last N days"
- "first divergence payload for comparison_id"
- "all inconclusive outcomes requiring operator retry"

### 3.4 Presentation Constraints

- No free-text-only verdicting; every outcome uses taxonomy tokens.
- No log-scraping dependency for core parity decision.
- No implicit defaults when fields are unavailable; use inconclusive class.

---

## 4) Persistence Requirements

### 4.1 Immutable Comparison Ledger

Each parity comparison is persisted as an append-only record.
No mutation of past verdict payloads.

### 4.2 Idempotency Key

Comparison writes are idempotent on:

`(station_id, trading_day, window_type, normalized_window_boundary, parity_context_key)`

Prevents duplicate records for same deterministic input set.

### 4.3 Deterministic Parity Comparison Identity Anchoring

Logical comparison identity is anchored to the canonical tuple:

`(station_id, trading_day, window_type, normalized_window_boundary, parity_context_key)`

Where:

- `normalized_window_boundary` is the deterministic comparison boundary after window normalization
  (for example, closed-day terminal boundary or rolling-intraday normalized cutoff).
- `parity_context_key` is derived only from deterministic, replayable comparison inputs, including:
  - ordered PEU normalization hash,
  - `source_live_version_marker`,
  - replay normalization context.

Identity rules:

- Re-computation MUST NOT create a new logical comparison record when this tuple is unchanged.
- Replay reruns with identical normalized inputs MUST reproduce the same identity tuple.
- Comparison timestamps are observational metadata and MUST NOT be used as identity authority.

Persistence layer must deduplicate on this deterministic identity tuple.
Execution-time fields (for example `comparison_generated_at_utc`) represent when comparison
was performed, not what logical comparison instance it is.

### 4.4 Retention

Minimum retention must cover:

- active trading day,
- prior day,
- configurable historical window sufficient for incident postmortems.

Design default: retain at least 30 days of comparison ledger.

### 4.5 Referential Integrity

Persisted comparison references must map to existing live baseline artifacts and replay run artifacts.
Broken references are stored as `PARITY_INCONCLUSIVE` with explicit inconclusive class,
not silent nulls.

---

## 5) Awareness Latency Bounds

### 5.1 Definitions

- `T_replay_complete`: timestamp replay processing finishes.
- `T_parity_persisted`: timestamp parity comparison record is persisted.
- `T_surface_visible`: timestamp parity verdict appears on operator summary surface.

### 5.2 Required Bounds

- **Computation bound**: `T_parity_persisted - T_replay_complete <= 10s`
- **Visibility bound**: `T_surface_visible - T_parity_persisted <= 5s`
- **End-to-end awareness**: `T_surface_visible - T_replay_complete <= 15s`

If any bound is violated, the system must still emit deterministic parity state;
latency breach is a visibility SLO event, not a parity-class substitution.

### 5.3 Staleness Signaling

If latest comparison age exceeds configured freshness threshold,
summary surface must show deterministic stale marker:

- `PARITY_STATUS_STALE`

This marker is orthogonal to last verdict (match/mismatch/inconclusive).

---

## 6) Non-Causality Proof

### 6.1 Claim

Parity visibility is strictly observational and cannot influence execution outcomes.

### 6.2 Boundary Conditions

1. Inputs are read-only projections from persisted live history and replay output.
2. Outputs are parity records/surfaces only.
3. No parity artifact is consumed by ingestion, transition engine, scheduler, or alert decision paths.
4. Failure in parity subsystem cannot block or alter live execution/replay execution; it only degrades visibility.

### 6.3 Proof Sketch

Let `E` be execution state machine (ingest → transition → persistence → alert path).
Let `V` be parity visibility machine.

- `V` reads from persisted artifacts emitted by `E` and replay outputs.
- There exists no edge `V -> E` in control/data authority graph.
- Therefore, parity outputs cannot alter transition guards, ordering, scheduling, or alert emissions.
- Hence parity subsystem is non-causal with respect to market-relevant execution semantics.

### 6.4 Audit Assertion

A release satisfies non-causality iff architectural review verifies:

- zero write path from parity components into execution control state,
- zero runtime dependency where execution blocks on parity availability.

---

## Operational Acceptance Criteria

Operators can answer "Does replay confirm live reality?" deterministically when:

1. Every station-day has a latest parity verdict token.
2. Mismatches expose first divergence location + class.
3. Inconclusive outcomes expose deterministic missing-data class.
4. Verdict is visible within awareness latency bounds.
5. No manual side-by-side replay/live inspection is required for primary parity judgment.
