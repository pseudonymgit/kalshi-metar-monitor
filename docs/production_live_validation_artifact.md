# Production Validation Artifact — Live Transition → Alert Flow

## 1. Live Validation Runbook

### Step 1 — Confirm ingestion

- **Endpoints / code surfaces to inspect**
  - `GET /metar/status`
  - `GET /metar/latest?icao=<STATION>&source=nws`
  - `GET /metar/status` for each 60-second cycle sample window (minimum 3 consecutive polls)
  - `GET /observability/ingestion-health` (if deployed; otherwise treat as observability gap)
- **Exact invariants being verified**
  - `scheduler_running == true`.
  - `poll_count` increases monotonically over 60-second cadence checks.
  - `last_poll_utc` and `last_loop_utc` advance on each completed cycle.
  - `/metar/latest` returns a valid latest observation for every monitored station, including parseable METAR timestamp and current temp.
  - If present, `/observability/ingestion-health` reports per-station freshness with no stale station beyond 2 polling intervals.
- **Failure interpretation**
  - Freshness failure or non-incrementing poll counters means ingestion/scheduler degradation before transition logic.
  - Station-specific missing latest METAR with scheduler healthy indicates per-station ingest/parse failure.
  - Missing `/observability/ingestion-health` endpoint means ingestion traceability is incomplete, not necessarily ingest failure.
- **Next action**
  - If scheduler counters stall: investigate scheduler thread/runtime first.
  - If one station is stale: isolate station fetch/parsing path and external METAR source response.
  - If endpoint missing: proceed with `/metar/status` + `/metar/latest` fallback and log observability gap for closure.

### Step 2 — Confirm transitions emitted

- **Endpoints / code surfaces to inspect**
  - `GET /observability/transitions?limit=500`
  - `GET /observability/transitions?station=<STATION>&limit=500`
  - `GET /metar/latest?icao=<STATION>&source=nws`
- **Exact invariants being verified**
  - Every transition record is tied to a station and timestamp (`timestamp_utc`/created time).
  - Transition types are authoritative transition categories only (`instant_up`, `instant_down`, `settlement_up`, `reversion_after_settlement`).
  - `instant_bucket_after == floor(current temp)` at transition time.
  - `settlement_bucket == floor(running daily max)` and never decreases intraday for each station.
  - Transitions are emitted only when bucket-relevant state changes, not on unchanged repeats.
- **Failure interpretation**
  - Fresh ingestion with zero transitions across all stations during known bucket changes indicates transition emission defect.
  - Settlement bucket regression indicates monotonicity violation and authoritative-state corruption risk.
- **Next action**
  - Validate ingestion timestamps and bucket derivation inputs for affected station(s).
  - Stop downstream alert diagnosis until authoritative transition emission is restored.

### Step 3 — Confirm market evaluation executed

- **Endpoints / code surfaces to inspect**
  - `GET /observability/transitions?station=<STATION>&limit=500`
  - In each transition entry, inspect `market_evaluated` and `alerts_sent` fields when present.
  - `GET /debug/alerts?limit=500` to correlate evaluation windows with persisted alert audit rows.
- **Exact invariants being verified**
  - For transitions in active evaluation windows, `market_evaluated == true` is present on corresponding transition entries.
  - `alerts_sent` is populated deterministically after evaluation.
  - Evaluation outcomes are explainable as one of: alert emitted, explicit suppression, no eligible market, terminal/absorbing state reached.
- **Failure interpretation**
  - Transitions present without any evaluation marker for expected stations means evaluation path interruption after emission.
  - Evaluation marker present with no resulting alert and no suppression explanation indicates silent decision path.
- **Next action**
  - Correlate specific transition timestamp with market query/selection logs and alert audit table writes.
  - Block expansion work until evaluation provenance is available for every authoritative transition.

### Step 4 — Confirm alert emission and suppression reasons

- **Endpoints / code surfaces to inspect**
  - `GET /debug/alerts?limit=500`
  - `GET /observability/transitions?station=<STATION>&limit=500`
  - `GET /metar/latest?icao=<STATION>&source=nws` for final bucket context
- **Exact invariants being verified**
  - Every emitted alert can be linked to a prior authoritative transition for the same station and causal direction.
  - `MAX_REACHED` / `MIN_REACHED` absorbing alerts fire once per station/day/market side.
  - No alerts originate from non-authoritative conditions (no transition, stale replay divergence, or synthetic non-transition inputs).
  - When no alert is sent after evaluation, suppression/no-eligibility reason is explicitly recorded.
- **Failure interpretation**
  - Alert without matching transition = non-authoritative emission defect.
  - Duplicate absorbing alerts = absorbing-state idempotency failure.
  - Missing suppression reason with no alert = silent failure in decision observability.
- **Next action**
  - Freeze downstream logic changes; repair authoritative gating and deterministic audit coverage first.

## 2. P0 Validation Checklist

- [ ] **Transitions emitted for all stations:** each actively ingesting station has non-empty same-day entries in `/observability/transitions` during known bucket changes.
- [ ] **`settlement_up` parity across cities:** per-city/station `settlement_up` counts align with observed daily max bucket advances; no station with verified bucket climb has zero `settlement_up`.
- [ ] **Absorbing alerts fire once:** `MAX_REACHED` and `MIN_REACHED` appear at most once per station/day/market side.
- [ ] **Stations ingesting without alerts are explainable:** if station ingests and transitions occur but no alert sent, explicit suppression/no-market/terminal reason exists.
- [ ] **Alert payload completeness:** each sent alert contains all mandatory reconstruction fields in the Payload Completeness Contract.

## 3. Station Parity Table Schema

| station | latest_METAR_timestamp | latest_temp_f | latest_instant_bucket | latest_settlement_bucket | transition_count_today | settlement_up_count_today | alerts_sent_today | suppressions_today | absorbing_state_reached | last_alert_timestamp |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| `<ICAO>` | `<ISO-8601>` | `<float>` | `<int>` | `<int>` | `<int>` | `<int>` | `<int>` | `<int>` | `<NONE\|MAX_REACHED\|MIN_REACHED\|BOTH>` | `<ISO-8601\|NULL>` |

## 4. Failure Matrix

| Condition | Interpretation | Action |
|---|---|---|
| Fresh ingestion, no transitions | Ingestion alive but transition detector not emitting authoritative events (or bucket state not changing when expected). | Verify bucket derivation inputs and transition emission path before market logic checks. |
| Transitions present, no market evaluation evidence | `handle_market_transition()` path not executing or not annotating execution markers. | Trace transition handling invocation and restore deterministic evaluation marker logging. |
| Evaluation occurred, no alert and no suppression reason | Silent decision branch; cannot reconstruct why alert was not sent. | Add/restore deterministic suppression/no-eligibility reason recording; block expansion until resolved. |
| Evaluation occurred with explicit suppression | Expected non-send outcome if suppression condition is valid and deterministic. | Confirm suppression reason matches transition and market state; treat as pass if consistent. |
| Duplicate MAX/MIN absorbing alerts | Absorbing-state idempotency failure; terminal alerts re-fired. | Enforce once-per-day absorbing guard and validate replay determinism against duplicate emission. |
| Alerts emitted from non-authoritative conditions | Alert authority boundary violation (alert not caused by authoritative transition). | Immediately gate alerts to authoritative transition source only; audit for invalid emissions. |

## 5. Payload Completeness Contract

Minimum alert payload fields required to reconstruct alert causality:

- `station` (ICAO)
- `city`
- `market_side` (e.g., HIGH/LOW)
- `event_ticker`
- `transition_type`
- `direction`
- `current_temp_f`
- `instant_bucket`
- `settlement_bucket`
- `previous_relevant_bucket`
- `absorbing_state_flag` (`NONE`/`MAX_REACHED`/`MIN_REACHED`)
- `timestamp_utc`
- `reason` (or deterministic evaluation basis)

Acceptance rule: any emitted alert missing one or more required fields is non-reconstructable and fails production validation.

## 6. Observability Gap Assessment

**Current sufficiency assessment:** **Not sufficient** for full causal tracing in all non-send paths.

Current exposed surfaces are adequate to observe ingestion heartbeat (`/metar/status`), latest station reads (`/metar/latest`), transition stream (`/observability/transitions`), and sent alert audit rows (`/debug/alerts`). However, they do not guarantee deterministic visibility for every evaluated-but-unsent outcome.

Minimum additional deterministic observability required (smallest traceability addition, no architecture redesign):

1. **`/observability/ingestion-health`** (or equivalent existing endpoint) with per-station freshness and last accepted observation timestamp.
2. **Per-transition evaluation outcome marker** tied by station + transition timestamp/id with one explicit terminal result enum:
   - `ALERT_SENT`
   - `SUPPRESSED_<REASON>`
   - `NO_ELIGIBLE_MARKET`
   - `TERMINAL_STATE`
3. **Suppression reason surfacing** in observability payload for all evaluated transitions where alert count is zero.

These additions are deterministic metadata only and preserve current authoritative flow.

## 7. Distilled Operational Conclusion

Before any expansion work, production must prove end-to-end causal integrity for each station:
**ingestion observed → authoritative transition emitted → market evaluation executed → alert sent or explicit non-send reason recorded**.

A **silent failure** is any case where ingestion/transition/evaluation occurred but the system cannot explicitly show whether an alert was sent, suppressed, market-ineligible, or terminal.

Downstream logic must not be modified before upstream signal presence is confirmed, because non-authoritative or untraceable upstream behavior invalidates alert correctness and corrupts replay determinism.
