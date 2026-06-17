# Kalshi METAR Monitor — Code and Design Review

**Date:** 2026-06-10  
**Scope:** Production Flask service deployed on Render (https://kalshi-metar-monitor.onrender.com)  
**Status:** Phase 2 (Kalshi Integration) — Public-only auth, no trading

---

## 1. Architecture Assessment

### 1.1 Component Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              app.py (Flask entrypoint)                  │
│  - HTTP orchestration, diagnostics, replay/debug endpoints              │
│  - Bridges runtime calls with explicit authority boundaries             │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
         ┌────────────────────────────┼────────────────────────────┐
         │                            │                            │
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  metar_monitor   │    │ kalshi_monitor   │    │ transition_emitter│
│  - METAR ingestion│    │ - Market discovery│   │ - Single entry for│
│  - State commit   │    │ - Hydration      │   │   transition emit │
│  - Transition emit│    │ - Ladder build   │   │ - Replays transitions│
└──────────────────┘    └──────────────────┘    └──────────────────┘
         │                            │                            │
         └────────────────────────────┼────────────────────────────┘
                                      │
                 ┌────────────────────┴────────────────────┐
                 │      Authoritative State & Persistence   │
                 │  - authoritative_state.py (in-memory)    │
                 │  - SQLite (alerts.db)                    │
                 └──────────────────────────────────────────┘
                                      │
         ┌────────────────────────────┼────────────────────────────┐
         │                            │                            │
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ settlement_epoch │    │  scoring_engine  │    │  alert_integrity  │
│    logger        │    │                  │    │   monitor         │
│ - Epoch tracking │    │ - Epoch scoring  │    │ - Pipeline gaps   │
│ - Duration accum │    │ - Replay-safe    │    │ - Suppression     │
│ - Reversion trail│    │   projections    │    │   detection       │
└──────────────────┘    └──────────────────┘    └──────────────────┘
```

### 1.2 Data Flow: Ingestion → Alert Delivery

1. **Ingestion** (`metar_monitor._ingest_obs`)
   - Poll METAR → parse → normalize → commit state → transition detect
   - Single-authority state mutations via `authoritative_state.commit_temperature_state()`

2. **Transition Emission** (`transition_emitter.emit_transition_if_changed`)
   - Centralized router: every transition passes through authority guard
   - Routes to `settlement_epoch_logger.log_transition_for_settlement_epoch()`
   - Calls persistence callback from `metar_monitor._log_transition_event()`

3. **Market Evaluation** (`kalshi_monitor.process_ladder_transition`)
   - Runs in same code path as ingestion (not decoupled)
   - Builds ladder snapshot → filters by direction/strike → evaluates eligibility
   - No side-effect separation: market calls may occur during ingestion

4. **Alert Delivery** (`metar_monitor._emit_alert` / `_send_alert`)
   - Conditional webhook delivery based on `allow_alert_delivery`
   - No retry queue; delivery failures are logged only
   - Missing ladder detection via webhook fallback with metadata

### 1.3 State Management

| Layer | Component | Persistence | Restart Survival |
|-------|-----------|-------------|------------------|
| In-memory | `authoritative_state._STATE` | None | ❌ |
| SQLite | alerts.db (`alerts`, `transition_events`, `settlement_epochs`) | Local | ✅ |
| Hybrid | `kalshi_monitor._SERIES_*` caches | None | ❌ |
| Hybrid | `kalshi_monitor._LAST_SETTLEMENT_UP_TS` | None | ❌ |
| Hybrid | `metar_monitor._SIGNAL_*` caches | None | ❌ |

**Risks:**
- All signal-layer state (`_SIGNAL_*` caches) is lost on restart → signal cooldowns reset
- Settlement-up timestamps are lost → goldilocks detection resets daily but not across restarts
- Series discovery cache is cleared on restart → re-discovery delays alert readiness

### 1.4 Scheduler Lifecycle & Idempotency

- **Scheduler Functions:** `start_scheduler()`, `stop_scheduler()`, `is_scheduler_running()`
- **Guarantees:** Single thread, no duplicate polling, idempotent start/stop
- **Missing:** No explicit health monitor; no automatic restart on crash
- **Risk:** If the Render container restarts without `METAR_AUTOSTART=true`, the scheduler stays down until manual restart

### 1.5 Execution Domain Discipline

**Execution domains:**
- `production` — Full side effects, alert delivery, live market calls
- `replay` — Deterministic, no side effects, no market calls
- `observability`, `diagnostics`, `audit`, `replay` — Forbidden from live Kalshi calls

**Checks:**
- `_FORBIDDEN_KALSHI_DOMAINS` guards in `kalshi_monitor._kalshi_public_get()`
- `kalshi_execution_domain()` context manager enforces domain boundaries
- `security_boundaries.enforce_execution_domain_guard()` for mixed-domain ingest flags

**Gaps:**
- No runtime domain validation before alert delivery (relies on `allow_alert_delivery` flag)
- Replay mode can still call `send_composed_weather_market_alert()` if called directly from `metar_monitor._send_alert()`

### 1.6 Gunicorn Assumptions & Risks

- **Current assumption:** Single-process Flask app (Render free tier default)
- **Missing:** No worker coordination, no distributed lock for scheduler
- **Risk:** If scaled to multiple workers:
  - Multiple scheduler threads could start
  - Alert delivery could duplicate
  - Transition emission would not be atomic across processes

---

## 2. HIGH/LOW Signal Alignment

### 2.1 Signal Type Coverage

| Signal | Direction | Current Support | LOW Equivalent |
|--------|-----------|-----------------|----------------|
| `instant_up` | ↑ | ✅ | ✅ |
| `instant_down` | ↓ | ✅ | ✅ |
| `settlement_up` | ↑ | ✅ | ✅ (via `_process_temperature_event` branch) |
| `reversion_after_settlement` | ↓ | ✅ | ✅ (time-boxed, ≤300s) |
| `goldilocks_reversion` | ↓ | ✅ | ✅ (same code, no directional logic) |
| `near_boundary_momentum_up` | ↑ | ✅ | ❌ |
| `near_boundary_momentum_down` | ↓ | ❌ | ❌ |
| `goldilocks_momentum_down` | ↓ | ❌ | ❌ |

**Finding:** Signal logic is *directionally agnostic* in most places, but signal types themselves are asymmetric:
- HIGH signals use `near_boundary_momentum_up`, `goldilocks_reversion`, `settlement_up`
- LOW has `goldilocks_reversion`, `reversion_after_settlement`, `settlement_up` (same)
- No explicit LOW signals for *momentum down* or *reversion from downward movement*

### 2.2 Settlement Epoch Tracking

** HIGH only:** Epoch context is *market-type agnostic* (`market_type IS NULL`) — `kalshi_monitor._load_current_epoch_context()` uses `market_type = ? OR market_type IS NULL` to support both.

**Risk:** If HIGH and LOW markets are tracked separately but share the same epoch table without `market_type` partitioning:
- `goldilocks_reversion` could fire for HIGH but LOW still has open epoch
- No way to express “reverted below *HIGH* settlement but still above *LOW* settlement”

### 2.3 Ladder Bucket Interpretation

- `settlement_bucket` = integer floor of temperature
- `instant_bucket` = integer floor of *current* temperature
- **No asymmetry found:** Both HIGH and LOW use same bucket interpretation
- **Missing:** No `bucket_index` metadata for LOW ladder selection in `transition_emitter`

**Finding:** Bucket logic is symmetric; direction is inferred from `transition_type` (`instant_up` vs `instant_down`) or market type. However, `transition_emitter` does not expose `market_type` to the persistence layer, so LOW/high epochs share tables without clear segregation.

---

## 3. Issues, Errors, and Gaps

### 3.1 Critical Severity

#### CRITICAL-1: No Alert Delivery Retry / Dead Letter Queue

- **Location:** `metar_monitor._emit_alert()`, `metar_monitor._send_alert()`
- **Symptom:** Webhook delivery failures are logged but not retried; alerts are lost if webhook is down
- **Impact:** Production alert loss for transient failures
- **Fix:** Implement retry queue (SQLite or in-memory) with exponential backoff; add dead-letter endpoint for manual inspection
- **Priority:** Production blocking — must be fixed before trading

#### CRITICAL-2: Signal Layer State Not Persisted

- **Location:** `metar_monitor._SIGNAL_OBSERVATION_WINDOWS`, `_SIGNAL_BOUNDARY_LAST_EMIT`, `_SIGNAL_GOLDILOCKS_EPOCH_TRACKER`
- **Symptom:** Signal cooldowns and observation windows reset on restart
- **Impact:** Signal emissions may be duplicated or suppressed incorrectly after restart
- **Fix:** Persist signal layer state in `alerts.db` (`signal_layer_state` table) with station + key composite; load at startup
- **Priority:** High — causes false positives/negatives after restart

#### CRITICAL-3: Market Cache Not Persisted

- **Location:** `kalshi_monitor._SERIES_MARKETS_CACHE`, `_DISCOVERED_WEATHER_MARKETS_BY_STATION`
- **Symptom:** All market discovery and hydration must re-run on restart
- **Impact:** Alert readiness delay; temporary alert suppression until cache repopulates
- **Fix:** Serialize market cache to SQLite (`market_cache` table) with station + series_ticker + hydrate_time composite; load at startup
- **Priority:** High — causes production alert latency

#### CRITICAL-4: Missing Low-Direction Momentum Signal

- **Location:** `metar_monitor._evaluate_deterministic_signal_layer()`
- **Symptom:** `near_boundary_momentum_down` and `goldilocks_momentum_down` signals are not defined
- **Impact:** LOW markets cannot emit momentum signals (only reversion)
- **Fix:** Add `near_boundary_momentum_down` and `goldilocks_momentum_down` signal emissions in `_process_temperature_event()`
- **Priority:** Medium — LOW market coverage gap

### 3.2 High Severity

#### HIGH-1: No Rate-Limit Handling for Kalshi Public API

- **Location:** `kalshi_monitor._kalshi_public_get()`, `_get_all_public_markets()`
- **Symptom:** If Kalshi public API enforces rate limits, failures are unhandled
- **Impact:** Service unavailability during API rate limiting
- **Fix:** Implement request throttling (per-minute limit), exponential backoff, and `Retry-After` header parsing
- **Priority:** Production blocking — could cause intermittent failures

#### HIGH-2: Execution Domain Not Enforced Before Alert Delivery

- **Location:** `metar_monitor._send_alert()` → `kalshi_monitor.send_composed_weather_market_alert()`
- **Symptom:** Replay mode can still call `send_composed_weather_market_alert()` if invoked directly
- **Impact:** Replay could emit alerts or modify live state if bypass guards
- **Fix:** Enforce execution domain guard in `_send_alert()` before calling `kalshi_monitor` functions
- **Priority:** Medium — architectural integrity issue

#### HIGH-3: No Market State Diff / Change Tracking

- **Location:** `kalshi_monitor` caches, `ladder_cache_observability.py`
- **Symptom:** No way to detect when market state changes (e.g., ladder closes, prices shift)
- **Impact:** Alerts may reference stale market data; no audit trail for market evolution
- **Fix:** Implement market state diff (`ladder_before`, `ladder_after`) and emit market change events
- **Priority:** Medium — improves debugging and auditability

#### HIGH-4: Settlement Epochs Not Segmented by Market Type

- **Location:** `settlement_epoch_logger.log_transition_for_settlement_epoch()` — table schema has `market_type` but default query uses `market_type IS NULL`
- **Symptom:** HIGH and LOW epochs share tables without clear separation
- **Impact:** Signal emissions may reference wrong epoch (e.g., reversion detected for HIGH but LOW epoch still open)
- **Fix:** Enforce `market_type` partitioning in epoch table and queries
- **Priority:** Medium — may cause signal misattribution

### 3.3 Medium Severity

#### MEDIUM-1: Alert Suppression Without Reason

- **Location:** `alert_integrity_monitor.FINDING_SUPPRESSION_WITHOUT_REASON`
- **Symptom:** Some alerts suppressed without explicit reason
- **Impact:** Operational ambiguity, hard to debug alert gaps
- **Fix:** Enforce `suppression_reason` on all suppressed alerts; add telemetry dashboard
- **Priority:** Medium — affects operational visibility

#### MEDIUM-2: No Replay Parity Validation in Production

- **Location:** `replay_parity_validator.py` exists but is not called in production
- **Symptom:** Replay vs production drift may go undetected
- **Impact:** Production alerts may differ from replay expectations
- **Fix:** Run parity check on every transition (lightweight) and log mismatches
- **Priority:** Medium — improves confidence in replay correctness

#### MEDIUM-3: Missing Test Coverage for Signal Layer

- **Location:** `tests/test_signal_layer_alerts.py` exists but is incomplete
- **Symptom:** Only 6 test cases for signal layer; edge cases untested
- **Impact:** Signal cooldowns and epoch tracking may break in production
- **Fix:** Add tests for low-direction momentum signals, epoch overlap, cooldown resets
- **Priority:** Medium — improves signal reliability

#### MEDIUM-4: No Alert Delivery Webhook Verification

- **Location:** `metar_monitor._emit_alert()`, `authorizer.py` referenced but not included
- **Symptom:** Webhook secret is loaded but never validated on receipt
- **Impact:** No way to detect webhook tampering or replay attacks
- **Fix:** Implement webhook signature verification on incoming requests
- **Priority:** Medium — security consideration

### 3.4 Low Severity

#### LOW-1: Documentation Drift — PROJECT_STATE.md vs Runtime

- **Location:** `PROJECT_STATE.md` vs `app.py` endpoints
- **Symptom:** `PROJECT_STATE.md` lists `/kalshi/markets` as “Public mode only” but runtime includes `/kalshi/ping` and `/kalshi/status`
- **Impact:** Operational confusion, incorrect endpoint discovery
- **Fix:** Sync `PROJECT_STATE.md` to current runtime endpoints

#### LOW-2: No Alert Type Categorization in DB Schema

- **Location:** `alert_schema.py`, `alerts` table
- **Symptom:** Alert types stored as free-text strings; no enum or constraint
- **Impact:** Inconsistent alert type names, hard to query by type
- **Fix:** Add `alert_type_category` column (e.g., “transition”, “signal”, “ladder_missing”) with enum-like values
- **Priority:** Low — improves data quality

#### LOW-3: Missing Low-Direction Market Discovery

- **Location:** `kalshi_monitor._discover_series_for_stations()` — only filters `HIGH`, `LOW`
- **Symptom:** LOW markets may be missed if ticker format differs from expected
- **Impact:** Some LOW markets not discovered; alert coverage incomplete
- **Fix:** Add `LOW` ticker pattern regex and test with real markets
- **Priority:** Low — coverage gap

#### LOW-4: No Station-Timezone Validation

- **Location:** `station_time.py`, `metar_monitor._to_local()`
- **Symptom:** Timezone conversion errors may go uncaught
- **Impact:** Incorrect local date keys, epoch misalignment
- **Fix:** Add timezone validity check and fail-closed on invalid timezone
- **Priority:** Low — edge-case resilience

### 3.5 Known Fragile Areas (from Docs)

| Area | Risk | Current Mitigation | Gaps |
|------|------|-------------------|------|
| Alert Routing Logic | Medium | `_emit_alert()` branches by alert type | No unit tests for routing decisions |
| Ladder Boundary Interpretation | High | `_directional_strike_window()` in `kalshi_monitor.py` | No tests for boundary edges |
| Distance-to-Next-Rung | High | `_compute_distance_to_next_rung()` in `kalshi_monitor.py` | Uses float math; may overflow |
| Event-Scoped Memory | High | `_MISSING_LADDER_DEDUPE` in `metar_monitor.py` | Uses in-memory dict; cleared on restart |
| Scheduler Lifecycle | Medium | Idempotent start/stop | No health monitor or auto-restart |
| Discovery/Hydration Mismatches | High | `hydration_health_classifier.py` | No automated resolution |

---

## 4. Action Plan

### 4.1 Production Trading Readiness

| # | Item | Effort | Blocking |
|---|------|--------|----------|
| 1 | **Alert Retry Queue** | 2 days | ✅ CRITICAL-1 |
| 2 | **Signal State Persistence** | 1 day | ✅ CRITICAL-2 |
| 3 | **Market Cache Persistence** | 1 day | ✅ CRITICAL-3 |
| 4 | **Rate-Limit Handling** | 1 day | ✅ HIGH-1 |
| 5 | **Low-Direction Momentum Signal** | 0.5 day | ⚠️ CRITICAL-4 |

**Estimated Total:** 5.5 days  
**Blocking:** 4 items (1,2,3,4)

### 4.2 Post-Launch Improvements

| # | Item | Effort | Priority |
|---|------|--------|----------|
| 6 | **Epoch Segmentation by Market Type** | 1 day | HIGH-4 |
| 7 | **Replay Parity Validation in Production** | 1 day | HIGH-3 |
| 8 | **Market State Diff Tracking** | 1 day | HIGH-2 |
| 9 | **Webhook Verification** | 0.5 day | MEDIUM-4 |
| 10 | **Signal Layer Test Coverage** | 2 days | MEDIUM-3 |
| 11 | **Suppression Reason Enforcement** | 0.5 day | MEDIUM-1 |
| 12 | **Documentation Sync** | 0.5 day | LOW-1 |
| 13 | **Alert Type Categorization** | 0.5 day | LOW-2 |
| 14 | **Station-Timezone Validation** | 0.5 day | LOW-4 |

**Estimated Total:** 7.5 days  
**Priority:** All medium-low

### 4.3 Implementation Order

1. **Week 1 (Production Readiness):**
   - Day 1: Signal state persistence + market cache persistence
   - Day 2: Alert retry queue + rate-limit handling
   - Day 3: Low-direction momentum signal + integration tests
   - Day 4: Production validation (replay parity, alert routing)
   - Day 5: Release candidate + smoke tests

2. **Week 2 (Post-Launch Improvements):**
   - Day 1-2: Epoch segmentation + replay parity validation
   - Day 3: Market state diff + webhook verification
   - Day 4: Signal test coverage + suppression reason enforcement
   - Day 5: Documentation sync + alert categorization + timezone validation

### 4.4 Estimated Effort Summary

| Category | Estimate |
|----------|----------|
| **Critical Fixes** | 5.5 days |
| **Post-Launch Improvements** | 7.5 days |
| **Total (Weeks)** | ~2 weeks |

### 4.5 Risk Mitigation

- **Rollback Plan:** All changes are additive or backward-compatible; no schema drops
- **Canary Strategy:** Deploy signal persistence first, validate in observability mode before enabling alert delivery
- **Testing Strategy:** Every critical fix includes unit tests + integration test in test suite
- **Monitoring Strategy:** Add `signal_state_persisted` and `market_cache_hit_rate` metrics before deployment

---

## 5. Summary Checklist

- [x] **Architecture Assessment** — Layered, centralized state, replay-safe design
- [x] **HIGH/LOW Signal Alignment** — Mostly symmetric; missing LOW momentum signals
- [x] **Critical Issues** — Alert retry, signal state persistence, market cache, low-direction signals
- [x] **High Issues** — Rate limits, execution domain enforcement, market state diff, epoch segmentation
- [x] **Action Plan** — 2-week rollout with clear dependencies
- [x] **Implementation Order** — Start with persistence + retry queue, then add parity validation

**Final Verdict:** System is **functionally complete but not production-ready for trading** due to alert delivery fragility, state loss on restart, and missing LOW market coverage. Fix prioritized items above before enabling live trading.

---

*End of Review*
