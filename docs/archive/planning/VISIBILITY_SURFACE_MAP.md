# VISIBILITY SURFACE MAP

Execution-visibility audit for production Kalshi METAR Monitor.

Scope: execution truth coverage only. This document does not change execution logic,
alert semantics, scheduler cadence, or replay behavior.

## 1) Execution Truth Inventory

| Execution Truth | Owning Domain | Trigger Condition | Persistence Scope |
|---|---|---|---|
| METAR observation accepted | Ingestion (`_ingest_obs` / authoritative state) | Observation passes source parsing, age/window checks, and ordering acceptance | In-memory authoritative snapshot (`last_obs`, `last_seen_iso`) and `metar_observations` audit table |
| METAR observation rejected or skipped | Ingestion | Observation fails acceptance constraints (e.g., stale/duplicate/window mismatch/parse failure) | Mostly process/log-visible; accepted-observation surfaces reflect absence rather than explicit rejection row |
| Instant temperature bucket transition (`instant_up` / `instant_down`) | Temperature transition engine (`_process_temperature_event`) | `floor(temp_f)` changes versus previous instant bucket | `transition_events` row + in-memory transition history |
| Settlement bucket advancement (`settlement_up`) | Temperature transition engine + settlement epoch logger | Running daily max floor exceeds prior settlement bucket | `transition_events`, `settlement_epochs`, in-memory transition history |
| Reversion after settlement (`reversion_after_settlement`) | Temperature transition engine + settlement epoch logger | Instant bucket drops while settlement bucket remains unchanged after settlement | `transition_events`, `settlement_epochs` reversion fields, in-memory transition history |
| Goldilocks structural reversion (`goldilocks_reversion`) | Temperature transition engine | Reversion occurs within 300s of prior settlement-up timestamp for station | `transition_events` + in-memory transition history |
| Transition evaluated but non-emitted | Transition emission authority (`emit_transition_if_changed`) | `transition_type` missing or no effective bucket change (`instant_changed==False` and `settlement_changed==False`) | No persisted transition event; only downstream absence/state inference |
| Ladder hydration state per station (`cache_valid` / `cache_missing` / `cache_stale`) | Kalshi market cache / hydration prerequisite | Poll or fetch path calls hydration prerequisite before ingestion | Runtime hydration result and cache metadata; surfaced through market-coverage and poll skip symptoms |
| Alert evaluation decision | Alert evaluation (`_send_alert`) | Integer cross with transition change invokes market evaluation pipeline | Transition metadata annotation (`evaluation_outcome`, `alerts_sent`, optional `suppression_reason`) + alert audit rows |
| Alert suppression path | Alert evaluation | Evaluation returns non-alert outcome (terminal, no transition, no market, webhook/rate/other gate) | Transition metadata annotation + observability diagnostics classification |
| Scheduler execution heartbeat | Scheduler loop (`_scheduler_loop`) | Each poll loop completion updates loop timestamp and poll counters | In-memory authoritative snapshot metrics returned by status/observability endpoints |
| Replay execution outcome | Replay domain (`run_replay_for_station_day` + replay engine) | Operator-triggered replay endpoint for station/day | Replay response payload; replay reuses deterministic ingest path without alert delivery |

## 2) Visibility Mapping

| Execution Truth | Visibility Surface | Operator Channel | Emission Trigger | Maximum Awareness Latency | Replay Visibility Status |
|---|---|---|---|---|---|
| METAR accepted observation | `/observability/ingestion-health`, `/metar/latest`, `/debug/state` | HTTP observability/ops endpoints | Next endpoint read after acceptance and state commit | Bounded by poll interval + query time | Replay updates same state path; visible after replay run completion |
| METAR rejected/skipped observation | Indirect via stale ingestion status and absence of state advancement | `/observability/ingestion-health` | Subsequent health computation after no new accepted obs | At stale threshold boundary (configured from poll seconds) | Replay does not expose explicit rejection classes either |
| Instant transitions | `/observability/transitions` and transition-derived summaries | HTTP observability | Transition emitted and persisted | Next endpoint read after event persistence | Replay emits same transition types through same engine |
| Settlement advancement | `/observability/transitions`, `/observability/current-epochs`, `/observability/day-structure`, `/observability/station-summary`, `/observability/alert-fire-audit` | HTTP observability | `settlement_up` emission and epoch logging | Next query after persistence | Replay produces same settlement transitions/epochs for same ordered inputs |
| Reversion-after-settlement | `/observability/transitions` plus epoch reversion fields (`reversion_occurred`, `first_reversion_timestamp_utc`) | HTTP observability | Reversion transition emission and epoch update | Next query after persistence | Replay-visible via same transition + epoch logic |
| Goldilocks structural condition | `transition_type=goldilocks_reversion` in transition stream | HTTP observability (`/observability/transitions`) | Goldilocks condition true during reversion handling | Next query after persistence | Replay-visible where temporal delta condition is met |
| Ladder hydration invalid/missing | Market coverage cache status + log-level poll skip reason | `/observability/market-coverage`, `/observability/trader-dashboard`, runtime logs | Hydration prerequisite fails before station ingestion | Next coverage query (or immediate log review) | Replay path does not depend on live ladder hydration |
| Alert decision emitted | Alert rows + transition metadata outcome | `/observability/alert-preview`, `/observability/alert-diagnostics`, `/observability/alert-fire-audit` | Market evaluation completes with alert sent/suppressed classification | Next diagnostics query after evaluation annotation | Replay disables alert delivery; evaluation surfaces are live-path oriented |
| Alert suppressed | Suppression outcome token and diagnostics class | `/observability/alert-diagnostics`, transition metadata in history | Evaluation completes with non-alert outcome | Next diagnostics query after annotation | Replay does not emit live alerts; suppression mostly not applicable unless replay-specific diagnostics added |
| Scheduler health | Poll/loop markers | `/metar/status`, execution boundary markers | Loop end updates `last_loop_utc` and poll counter | Bounded by one scheduler interval + query time | Replay temporarily pauses scheduler and then restores; visible via status timestamps |
| Replay/live divergence risk | Manual comparison only (replay endpoint output vs persisted transition history) | `/debug/replay` + observability endpoints | Operator runs replay then inspects parity manually | Unbounded (operator-driven) | Partial: replay available, parity surface not first-class |


## 2A) Coverage Reason Interpretation Matrix

Use `coverage_reason` and cache status together; this matrix is canonical triage guidance.

| coverage_reason | Typical cache status context | Interpretation | Operator action |
|---|---|---|---|
| `market_data_unknown` | `cache_missing` / `cache_stale` / upstream hydration attempt failure | Coverage cannot be resolved from current cache snapshot; this does **not** by itself imply scheduler or ingestion failure. | Check runtime-authority + hydration/market-coverage surfaces before escalation. |
| `not_in_live_station_universe` | `cache_valid` or fallback universe active | Station not currently in canonical live station scope for this evaluation cycle. | Validate station-universe resolution order and discovery snapshot. |
| `eligible_market_present` | `cache_valid` | At least one currently eligible market exists for station/type/day context. | Normal operations; continue monitoring transition outcomes. |
| `no_eligible_market` | `cache_valid` | Markets were evaluated but none passed deterministic eligibility filters. | Review eligibility diagnostics and filter breakdown. |

## 3) Silent Zones

Detection only (no remediation proposals):

1. **Transition non-emission silent path**: when `emit_transition_if_changed` exits for missing transition type or no effective bucket change, there is no explicit persisted suppression record; operator sees absence rather than explicit cause.
2. **Rejected-ingestion class opacity**: accepted ingestion is visible, but rejected/skipped METAR reasons are not surfaced as a deterministic per-station rejection stream.
3. **Hydration causality ambiguity**: hydration invalidity is visible in coverage/status context, but there is no dedicated per-poll hydration timeline; operators infer skipped ingestion from secondary surfaces.
4. **Scheduler degradation interpretation gap**: raw timestamps/counters are visible, but degraded-vs-stalled interpretation requires operator inference rather than explicit deterministic state class.
5. **Replay parity blind spot**: replay output exists, but no canonical replay-vs-live parity surface continuously states match/mismatch for operator confidence.
6. **Goldilocks prioritization gap**: Goldilocks is emitted as transition type but not elevated to a dedicated high-salience structural feed; discoverability depends on transition stream inspection cadence.

## 4) Suppression Taxonomy

| Suppression Class | Definition in Current System | Observable Today |
|---|---|---|
| Intentional suppression | Deterministic guard blocks alert emission (terminal state, no eligible market, no transition, webhook missing, runtime gating outcome) | Yes: evaluation outcomes and diagnostics classes expose many intentional blocks |
| Structural suppression | Upstream structure prevents evaluation/emission (missing series, inactive station, unsupported strike, cache unavailable/stale) | Yes, primarily through market coverage rows and derived dashboard attention |
| Timing suppression | Rate-limit throttle, poll cadence, stale data window, day-boundary timing causing delayed awareness | Partially: inferred from status timestamps and outcomes; not always explicit as timing-labeled events |
| Visibility omission | Execution-domain truth occurs without a first-class deterministic operator surface | Yes: non-emitted transition reasons, rejected-ingestion reasons, and replay parity omission are current omission zones |

## 5) Deterministic Visibility Guarantees

Minimum conditions required to assert **"Production Visibility Achieved"**:

1. **Every execution-domain state transition class is either emitted or explicitly non-emitted with deterministic reason coding** (including no-op transition evaluation paths).
2. **METAR ingestion provides dual visibility**: accepted observations and rejected/skipped classifications are both queryable by station/day.
3. **Settlement lifecycle is fully visible**: settlement advancement, reversion-after-settlement, terminal closure, and Goldilocks structural events remain queryable without log dependence.
4. **Hydration dependency state is first-class** at operator level for each live station and polling cycle (valid/missing/stale with deterministic timestamp context).
5. **Alert pipeline outcomes are total**: every evaluated transition has one canonical outcome token (alert sent or suppression reason) accessible in observability surfaces.
6. **Scheduler health semantics are deterministic**: heartbeat markers are visible and map to unambiguous operator health interpretation.
7. **Replay visibility parity is explicit**: replay results can be deterministically compared to live persisted history through a canonical operator surface.
8. **Observability remains non-causal**: all visibility channels remain read-only and cannot influence execution decisions.

## Deterministic Audit Verdict

Current system has broad visibility coverage for emitted transitions, settlement epochs,
alert outcomes, and scheduler heartbeat markers, but it does **not** yet eliminate silent-correctness
risk in all execution-adjacent paths.

Therefore, the deterministic answer to:

> "Can any market-relevant structural event occur without operator awareness?"

is:

**Yes — in bounded but material silent zones (notably non-emitted transition causes,
rejected-ingestion classes, and replay parity visibility).**
