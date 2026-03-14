# Full Repository Review — Causal-Chain Integrity

Date: 2026-03-04  
Scope: Deterministic lineage validation for runtime signal path

Signal path under review:

`METAR ingestion → ladder transition emission → market evaluation → suppression logic → alert emission`

Modules reviewed:

- `core/metar_monitor.py`
- `core/transition_emitter.py`
- `core/kalshi_monitor.py`
- `core/settlement_epoch_logger.py`
- `core/observability.py`
- `app.py`

---

## 1) Transition Emission Authority

### Determination

**PASS with boundary caveat**.

### Evidence

- `core.transition_emitter.emit_transition_if_changed(...)` is explicitly declared as the single authority and enforces `enforce_transition_emission_authority()` before routing emission. Only authorized caller module `core.metar_monitor` is allowed by boundary policy. 
- Transition routing in ingestion uses `emit_transition_if_changed(...)` at the only runtime transition decision points (`settlement_up`, instant transitions, and `goldilocks_reversion`) inside `_process_temperature_event(...)`.
- Repository-wide write path scan shows `INSERT INTO transition_events` appears only in `_log_transition_event(...)`.

### Caveat

- `_log_transition_event(...)` remains import-callable and is not itself boundary-guarded. Current code only passes it as `emit_fn` from `emit_transition_if_changed(...)`, but governance depends on caller discipline for this helper.

---

## 2) Transition Persistence Guarantees (Emission → Epoch History)

### Determination

**PARTIAL (best-effort, not hard-guaranteed).**

### Evidence

- `emit_transition_if_changed(...)` always calls `log_transition_for_settlement_epoch(...)` after calling `emit_fn` (if transition conditions pass), and forwards transition correlation fields when present.
- `_log_transition_event(...)` catches internal DB exceptions and returns `None`; this means epoch logging is still attempted even if transition event insert fails.

### Observability consistency risk

- `log_transition_for_settlement_epoch(...)` has no local exception handling around table creation/update/insert. Any sqlite or filesystem error propagates up and can break persistence/annotation continuity.
- Result: there is no atomic transaction spanning `transition_events` insert and `settlement_epochs` update/insert. Runtime can land in split states:
  - transition event persisted but epoch update failed,
  - or (if `emit_fn` returns `None` due failure) epoch history update without a valid `transition_event_id`.

---

## 3) Transition → Evaluation Correlation

### Determination

**PASS with observability caveat: evaluation is transition-driven, but correlation annotation can fail when correlation payload is missing.**

### Evidence

- Evaluation occurs inside `_send_alert(...)` after throttle/admission checks, and this path is reached from transition-triggered alert routing.
- Correlation backfill is performed via `_annotate_transition_history_market_eval(...)` using `transition_event_id`/`timestamp_utc` from `transition_correlation`.
- If correlation is missing, annotation is skipped with warning `transition_market_eval_annotation_skipped ... missing_correlation`.

### Annotation gap paths

1. `transition_correlation` missing/invalid while `_send_alert(...)` still evaluates (e.g., failed transition insert returns `None`, malformed correlation payload).
2. Rate-limit early return occurs before any evaluation/annotation (covered separately in section 6), causing transitions with no evaluation record.

---

## 4) Evaluation → Alert Eligibility Gates

### Determination

**PASS for runtime ingestion alert path; BYPASS exists via manual endpoint.**

### Runtime path evidence

- In ingestion runtime, `_send_alert(...)` invokes `process_ladder_transition(...)` before composed alert send; only when `transition.get("should_alert")` is true does it call `send_composed_weather_market_alert(...)`.
- `process_ladder_transition(...)` deterministically evaluates ladder membership, bucket transitions, and terminal-state blocking and returns explicit gate fields (`should_alert`, `reason`, `terminal_state_blocked`).

### Bypass path

- `app.py` exposes `/kalshi/composed` as a diagnostic/manual endpoint that directly calls `send_composed_weather_market_alert(...)` from request context, outside transition correlation/evaluation annotation flow.
- This endpoint is not intended to preserve causal lineage with transition emission.

---

## 5) Suppression Completeness

### Determination

**GAP FOUND: suppressed outcomes are not guaranteed to include explicit `suppression_reason`.**

### Evidence

- Suppression reason capture depends on `raw_reason = transition.get("reason")`; only non-empty values are retained.
- `_annotate_transition_history_market_eval(...)` writes `suppression_reason` only when non-empty.
- Evaluation outcome fallback can produce suppressed outcomes without explicit reason token persistence:
  - `reason_token = suppression_reason or "NO_TRANSITION"`
  - outcome becomes `SUPPRESSED_NO_TRANSITION`, but `suppression_reason` field remains absent if `suppression_reason is None`.

### Additional semantic gap

- `NO_ELIGIBLE_MARKET` and `TERMINAL_STATE` are represented as non-`SUPPRESSED_*` outcomes, yet they functionally block alert emission; suppression cause is not normalized to a mandatory reason field.

---

## 6) Rate-Limit Causal Lineage

### Determination

**FAIL (lineage gap): throttling can silently skip evaluation.**

### Evidence

- `_send_alert(...)` applies station-level throttle with `_KALSHI_CALL_THROTTLE_SECONDS`; if within window, function returns immediately.
- This return occurs before market evaluation loop, before `evaluation_outcome` synthesis, and before `_annotate_transition_history_market_eval(...)`.

### Impact

- Transition emitted and persisted, but no market evaluation metadata is attached.
- No explicit "throttled" outcome is recorded in transition metadata; causal trail appears as unevaluated rather than evaluated+suppressed-by-throttle.

---

## 7) Replay Equivalence Risks (Live vs Replay Divergence)

### Determination

**MULTIPLE RISKS IDENTIFIED.**

### R1 — Intentional replay scope limitation

- Replay uses `execute_ordered_replay_stream(...)` which enforces `allow_alert_delivery=False, persist_cache=False` and calls `_ingest_obs(...)` with those flags.
- `_process_temperature_event(...)` only calls `_emit_alert(...)` (and therefore `_send_alert(...)` evaluation/annotation logic) when `allow_alert_delivery` is true.

**Effect:** replay does not execute market evaluation/suppression/alert branch; live does.

### R2 — Transition timestamp source uses runtime-now

- `_log_transition_event(...)` stamps transition `created_utc` and in-memory `timestamp_utc` using `_now_utc_iso()` rather than observation timestamp.

**Effect:** identical observation replays can produce different transition timestamps and downstream correlation windows.

### R3 — Local-day derivation fallback can diverge

- Settlement epoch day key uses observation timestamp if parseable, otherwise falls back to event timestamp.
- Since event timestamp is runtime-now in `_log_transition_event(...)`, malformed or missing obs timestamps can shift epoch day attribution across runs.

### R4 — Operational divergence due to throttling

- Live execution may skip evaluation on `_KALSHI_CALL_THROTTLE_SECONDS`; replay bypasses `_send_alert(...)` entirely.

**Effect:** replay cannot reproduce throttle-induced causal gaps seen in live traffic.

---

## Causal-Chain Verdict by Objective

1. **Transition emission authority:** centrally enforced, with helper-callable caveat.  
2. **Transition persistence guarantees:** non-atomic best effort; observability consistency risk rather than primary causal-integrity break.  
3. **Transition → evaluation correlation:** evaluation is transition-driven, but correlation annotation can be missing when payload/correlation is unavailable or throttle short-circuits.  
4. **Evaluation → alert eligibility gates:** deterministic in ingestion path; manual API bypass exists.  
5. **Suppression completeness:** incomplete; explicit `suppression_reason` not guaranteed.  
6. **Rate-limit lineage:** incomplete; throttle silently prevents evaluation metadata emission.  
7. **Replay equivalence:** materially divergent in evaluation/alert branch and time-dependent fields.

---

## Recommended Hardening Actions (Review-Only Recommendations)

1. Make transition+epoch persistence atomic (single transactional unit or explicit two-phase commit with compensation log).
2. Record explicit evaluation outcome for throttle exits (e.g., `SUPPRESSED_RATE_LIMIT`) and annotate correlated transition when available.
3. Enforce mandatory `suppression_reason` for all non-alert outcomes, including `NO_ELIGIBLE_MARKET`, `TERMINAL_STATE`, and fallback suppressions.
4. Gate `/kalshi/composed` behind explicit debug-mode guardrails (or annotate outputs as manual/non-causal) to avoid lineage confusion.
5. Use observation-authoritative timestamp fields for replay-parity diagnostics (`observed_at_utc` + `recorded_at_utc`).
