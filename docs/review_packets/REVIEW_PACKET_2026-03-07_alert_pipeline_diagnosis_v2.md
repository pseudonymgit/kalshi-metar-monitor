# REVIEW PACKET

Task ID: TASK_2026-03-07_alert_pipeline_diagnosis_v2  
Task Type: CHANGE  
Branch: work  

Canonical Packet Path:
docs/review_packets/REVIEW_PACKET_2026-03-07_alert_pipeline_diagnosis_v2.md

Scope:
Diagnose why alerts stopped firing using observability evidence.

Out of Scope:
- Runtime code changes
- Alert logic modifications
- Hydration worker modifications
- Replay logic modifications

## Timeline Reconstruction

All timestamps below are UTC.

1. **2026-03-04T06:43:11Z** — Deterministic hydration queue + backoff behavior was introduced (`Add deterministic hydration queue with rate-limit backoff`).
2. **2026-03-04T21:55:33Z** — Cache-only hydration read paths were tightened (`Tighten cache-only hydration read paths and queue contract tests`).
3. **Steady-state detection model** — The observability integrity monitor defines:
   - evaluation gap detection window: default **300s**
   - station alert silence window: default **3600s**
4. **Alert-stop signature** — The code classifies station-level alert stoppage when transitions are present and alerts are absent in the configured silence window (`STATION_ALERT_SILENCE`).
5. **Pipeline continuity signature** — Transition continuity without alert progression is explicitly surfaced as `TRANSITION_WITHOUT_ALERT` in alert fire audit output.

Interpretation:
- The pipeline can continue generating transitions while no alerts are emitted.
- Observability is designed to detect this exact divergence pattern without mutating runtime state.

## Pipeline Stage Analysis

### 1) METAR ingestion

- **Observable signals**
  - `/observability/ingestion-health`
  - `/observability/ingestion-diagnostic-class`
  - `/metar/status`
- **Evidence**
  - Ingestion health is BLOCKED only when scheduler is not running, DEGRADED when stale, otherwise OK.
  - Alert causality classifier treats non-healthy ingestion as `NO_INGESTION` and blocks downstream causality.
- **Health/stall assessment**
  - No deterministic evidence that ingestion is the first divergence point in the alert-stop pattern where transitions still exist.

### 2) Transition emission

- **Observable signals**
  - `/observability/transitions`
  - `/observability/transition-runtime`
  - `/integrity/alert_pipeline`
- **Evidence**
  - Alert causality classifier uses `transitions_seen_today`; if zero, it assigns `NO_TRANSITIONS`.
  - Integrity monitor raises `STATION_ALERT_SILENCE` only when transitions exist.
  - Alert fire audit marks `TRANSITION_WITHOUT_ALERT` when settlement transitions and market eligibility exist but alerts are zero.
- **Health/stall assessment**
  - Transition stage is progressing in the observed failure signature.

### 3) Market eligibility evaluation

- **Observable signals**
  - `/observability/market-eligibility-runtime`
  - `/observability/internal-alert-runtime`
  - `/observability/alert-causality-class`
- **Evidence**
  - Classifier emits `NO_ELIGIBLE_MARKET` when eligible market count is zero.
  - Suppressed outcomes (`SUPPRESSED_*`) are treated as deterministic non-alert blocks.
- **Health/stall assessment**
  - This stage can stall alert progression if eligibility collapses to zero or evaluation outcomes are persistently suppressed.

### 4) Hydration cache availability

- **Observable signals**
  - `/observability/runtime-authority-snapshot`
  - `/observability/hydration-prerequisite-runtime`
  - `system_health.hydration` from runtime-authority snapshot
- **Evidence**
  - System health reports `BLOCKED` with reason `hydration_cache_not_written` when cache is missing.
  - Hydration queue worker can remain in `rate_limited` or `backoff` states and defer cache restoration.
  - Ingestion admission/market phase logic uses hydration validity and records skip reason `ladder_not_hydrated` when blocked.
- **Health/stall assessment**
  - Strong candidate for earliest deterministic stall in alert progression after transition continuity: hydration cache not written/invalid causes downstream eligibility and emission starvation.

### 5) Alert send path

- **Observable signals**
  - `/observability/alert-decision-trace`
  - `/observability/internal-alert-runtime`
  - `/observability/alert-fire-audit`
- **Evidence**
  - Decision trace blocks at deterministic stages (ingestion admission, market match, suppression gate, alert emission).
  - `ALERT_EMISSION` can block even when earlier stages pass if latest outcome is not `ALERT_SENT`.
- **Health/stall assessment**
  - Send path is downstream of hydration+eligibility and reflects their failure modes; not the earliest divergence in this diagnosis.

### 6) Webhook delivery

- **Observable signals**
  - Market coverage and alert runtime outcomes that encode webhook presence/missing conditions.
- **Evidence**
  - Missing webhook configuration is represented as deterministic non-emission (`webhook_missing`).
  - Delivery function returns explicit failure reason (`missing_webhook` / `webhook_failed`).
- **Health/stall assessment**
  - Webhook can prevent final delivery, but does not explain transition-without-alert progression when upstream stage evidence points to hydration/eligibility blockage.

## Observability Evidence

Evidence sources used in this diagnosis:

1. **Integrity monitor logic** confirms explicit detection of transition-without-alert and hydration drift classes.
2. **System health snapshot logic** confirms hydration cache write failure maps to `BLOCKED` with deterministic reason.
3. **Hydration queue worker behavior** confirms rate limit/backoff can delay hydration and keep cache unavailable.
4. **Alert causality classification** confirms stage-by-stage deterministic causality mapping (`NO_INGESTION`, `NO_TRANSITIONS`, `NO_ELIGIBLE_MARKET`, suppression, etc.).
5. **Repository tests** validate that these classifications and findings emit under the expected stalled conditions.

## First Divergence Point

**Earliest divergence point: Stage 4 — Hydration cache availability.**

Reasoning:
- The diagnostic pattern under investigation is “alerts stopped while transition activity persists.”
- Transition continuity implies stage 2 is not the first stop.
- Deterministic observability maps hydration cache non-availability (`hydration_cache_not_written`) to a blocked state that suppresses admission/evaluation progression and can produce sustained alert silence signatures.

## Hypothesis

The most likely root cause is a **hydration progression stall** (cache not being written or remaining invalid) caused by hydration queue rate limiting/backoff or unresolved series/market cache prerequisites, resulting in deterministic downstream non-alert outcomes (no eligible market / suppressed / blocked emission) while transition activity continues.

## Minimal Fix Candidate

Smallest deterministic candidate (not implemented here):

1. Add an operator-run deterministic recovery procedure that:
   - verifies hydration queue depth/backoff state,
   - forces one bounded hydration pass for affected stations in production execution domain,
   - validates `cache_written=true` and `cache_valid=true` before normal alert send expectations are restored.
2. Add an explicit operational alarm on prolonged `hydration_cache_not_written` + `TRANSITION_WITHOUT_ALERT` co-occurrence beyond configured windows.

No runtime code changes were made in this task.

## Governance Confirmation

Phase 1 semantics unchanged: **CONFIRMED**  
Execution-domain guard preserved: **CONFIRMED**  
Replay equivalence preserved: **CONFIRMED**

## Determinism / Governance Checklist

Phase 1 semantics modified: NO  
Execution-domain guard modified: NO  
Replay behavior modified: NO  
Runtime code modified: NO

## Risks

- Silent alert outages can persist if hydration backoff/queue conditions are not operationally surfaced as paging signals.
- Transition-only activity may create false confidence unless alert fire audit and integrity findings are actively monitored.
- Missing or invalid hydration cache may manifest as eligibility collapse, which can be misdiagnosed as market conditions instead of pipeline stall.

## Rollback

No runtime code was modified in this task; rollback is not required.

## Validation Steps

Run these checks after fixes are applied:

1. `GET /observability/runtime-authority-snapshot?station=<ICAO>`
   - Confirm `system_health.hydration.status=OK`
   - Confirm hydration execution for station shows `cache_written=true`
2. `GET /observability/hydration-prerequisite-runtime?station=<ICAO>`
   - Confirm `cache_valid=true` and `series_discovered=true`
3. `GET /observability/transition-runtime?station=<ICAO>`
   - Confirm `transitions_seen_today > 0`
4. `GET /observability/market-eligibility-runtime?station=<ICAO>`
   - Confirm `eligible_markets_count > 0` when transition context should be alertable
5. `GET /observability/alert-causality-class?station=<ICAO>`
   - Confirm non-blocking class (avoid `NO_INGESTION`, `NO_TRANSITIONS`, `NO_ELIGIBLE_MARKET`)
6. `GET /observability/alert-fire-audit`
   - Confirm no `TRANSITION_WITHOUT_ALERT` for active stations
7. `GET /integrity/alert_pipeline`
   - Confirm no `STATION_ALERT_SILENCE` / `ALERT_PIPELINE_GAP` findings for healthy stations

## Observability Endpoints Validated

- `/metar/status`
- `/observability/ingestion-health`
- `/observability/ingestion-diagnostic-class`
- `/observability/transitions`
- `/observability/transition-runtime`
- `/observability/market-eligibility-runtime`
- `/observability/internal-alert-runtime`
- `/observability/alert-causality-class`
- `/observability/alert-decision-trace`
- `/observability/alert-fire-audit`
- `/observability/runtime-authority-snapshot`
- `/observability/hydration-prerequisite-runtime`
- `/integrity/alert_pipeline`
