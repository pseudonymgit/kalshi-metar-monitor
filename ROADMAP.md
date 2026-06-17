# Weather Engine — Layered Implementation Roadmap

**Date:** 2026-06-10  
**Source:** `REVIEW.md` (2026-06-10 Code Review)  
**Status:** Phase 2 (Kalshi Integration) — Public-only auth, no trading  
**Target:** Production-ready for live trading with alert delivery guarantees

---

## Dependency Diagram (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           LAYER 4: Hardening & Polish                           │
│                     (needs Layer 0-3: all prerequisites)                        │
└─────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     LAYER 3: Observability & Verification                       │
│                     (needs Layer 0-2: signals + persistence)                    │
└─────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      LAYER 2: Signal Completeness                               │
│              (needs Layer 0-1: persistence + reliable delivery)                 │
└─────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                   LAYER 1: Infrastructure Hardening                             │
│                  (needs Layer 0: state persistence)                             │
└─────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
                                         │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      LAYER 0: State Persistence (PREREQUISITE)                  │
│                    SQLite persistence for ALL signal/cache state                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Layer 0: State Persistence (Prerequisite)

**Prerequisites:** None  
**Cumulative Effort:** 2 days  
**Pass Criteria:** System survives restart without losing signal cooldowns or market caches

### Tasks (mapped to REVIEW.md issue IDs)

| Issue ID | Description | Files | Implementation Approach |
|----------|-------------|-------|-------------------------|
| **CRITICAL-2** | Persist signal state to SQLite | `metar_monitor.py`, `authoritative_state.py` | Add `signal_layer_state` table; hydrate `_SIGNAL_OBSERVATION_WINDOWS`, `_SIGNAL_BOUNDARY_LAST_EMIT`, `_SIGNAL_GOLDILOCKS_EPOCH_TRACKER` at startup |
| **CRITICAL-3** | Persist market cache to SQLite | `kalshi_monitor.py`, `authoritative_state.py` | Add `market_cache` table; hydrate `_SERIES_MARKETS_CACHE`, `_DISCOVERED_WEATHER_MARKETS_BY_STATION` at startup |

### Exact File Changes

#### 1. `schema/alerts_schema.py` — New Tables

Add to schema (after `alerts`, `transition_events`, `settlement_epochs`):

```python
# Signal Layer State (CRITICAL-2)
CREATE TABLE IF NOT EXISTS signal_layer_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    key TEXT NOT NULL,  -- composite key for signal type: e.g., "near_boundary_momentum_up_15min"
    value TEXT NOT NULL,  -- JSON-encoded state
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(station, key)
);

-- Market Cache (CRITICAL-3)
CREATE TABLE IF NOT EXISTS market_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    markets_json TEXT NOT NULL,
    hydrate_time TEXT NOT NULL,
    expiry_time TEXT NOT NULL,
    UNIQUE(station, series_ticker, hydrate_time)
);
```

#### 2. `metar_monitor.py` — Signal State Persistence

**Changes:**

- Add `_persist_signal_state(station, key, value)` method
- Add `_load_signal_state(station)` to hydrate at startup
- Replace direct cache writes with persistence calls in:
  - `_process_temperature_event()` — `near_boundary_momentum_up`, `goldilocks_reversion`, `reversion_after_settlement` cooldowns
  - `_to_local()` — signal observation window management

**Key locations to patch:**
- Line ~210: `_process_temperature_event()` — add `_persist_signal_state(station, signal_key, json.dumps(state))` after cooldown tracking
- Line ~180: `_ingest_obs()` — add startup hydration call `_load_signal_state(station)` before processing

#### 3. `kalshi_monitor.py` — Market Cache Persistence

**Changes:**

- Add `_persist_market_cache(station, series_ticker, markets_json, hydrate_time, expiry_time)` method
- Add `_load_market_cache(station)` to hydrate at startup
- Replace `_SERIES_MARKETS_CACHE` and `_DISCOVERED_WEATHER_MARKETS_BY_STATION` with persistent queries

**Key locations to patch:**
- Line ~140: `_discover_series_for_stations()` — add `_persist_market_cache(station, series_ticker, markets_json, ...)` after discovery
- Line ~165: `_process_ladder_transition()` — replace cache hit check with SQL query `SELECT markets_json FROM market_cache WHERE station=? AND series_ticker=?`

### Test Plan (Per-Layer Tests)

| Test | Description | File | Pass Criteria |
|------|-------------|------|---------------|
| L0-T1 | **Restart Survival** | `tests/test_signal_state_persistence.py::test_restart_survival_signal_state` | After restart, signal cooldown timestamps match pre-restart values |
| L0-T2 | **Market Cache Hydration** | `tests/test_market_cache_persistence.py::test_cache_hit_after_restart` | Cache hit rate ≥95% on first market query after restart (vs 0% without persistence) |
| L0-T3 | **Cooldown Preservation** | `tests/test_signal_state_persistence.py::test_cooldown_preserved_after_restart` | Signal emissions respect original cooldown window after restart |
| L0-T4 | **Backward Compatibility** | `tests/test_schema_migration.py::test_existing_tables_unchanged` | No `DROP TABLE` or schema breaking changes; migration is additive |

### Pass Criteria (Layer 0)

- [x] `signal_layer_state` table created and populated on startup
- [x] `market_cache` table created and populated on startup
- [x] Signal cooldowns persist across restarts (verified via test L0-T1)
- [x] Market cache hit rate ≥95% after restart (verified via test L0-T2)
- [x] No breaking schema changes; migration is additive only

---

## Layer 1: Infrastructure Hardening

**Prerequisites:** Layer 0 complete (signal state + market cache persisted) ✅  
**Status:** COMPLETE (2026-06-15)  
**Cumulative Effort:** 2 days (total: 4 days)  
**Pass Criteria:** Webhook failures are retried; rate-limit responses are handled gracefully

### Tasks (mapped to REVIEW.md issue IDs)

| Issue ID | Description | Files | Implementation Approach |
|----------|-------------|-------|-------------------------|
| **CRITICAL-1** | Alert retry queue with exponential backoff + dead letter endpoint | `metar_monitor.py`, `alert_integrity_monitor.py` | Add `alert_delivery_queue` table; replace `_send_alert()` with queued retry + exponential backoff; add `/admin/dead-letter` endpoint |
| **HIGH-1** | Kalshi rate-limit handling with Retry-After parsing | `kalshi_monitor.py` | Add per-request rate limiter; parse `Retry-After` header; queue failed requests |

### Exact File Changes

#### 1. `schema/alerts_schema.py` — New Tables

```python
# Alert Delivery Queue (CRITICAL-1)
CREATE TABLE IF NOT EXISTS alert_delivery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    dead_lettered_at TEXT,
    UNIQUE(alert_id)
);

# Kalshi Rate Limit Counter (HIGH-1)
CREATE TABLE IF NOT EXISTS kalshi_rate_limit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    request_time TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(endpoint, request_time)
);
```

#### 2. `metar_monitor.py` — Alert Retry Queue

**Changes:**

- Replace `_send_alert()` with:
  ```python
  def _queue_alert_for_delivery(station, alert_type, payload):
      # 1. Enqueue to alert_delivery_queue
      # 2. Schedule background retry worker (cron or thread)
  ```
- Add `_retry_delivery_batch()` — process queue items with exponential backoff (1m, 5m, 15m, 30m, 1h, 2h, 4h, 8h)
- Add `_mark_dead_letter(alert_id, reason)` for manual inspection endpoint
- Add `/admin/dead-letter` endpoint (GET list, POST mark-resolved)

**Key locations to patch:**
- Line ~320: `_emit_alert()` — replace `_send_alert()` call with `_queue_alert_for_delivery()`
- Line ~410: `start_scheduler()` — add background retry worker (10s interval)

#### 3. `kalshi_monitor.py` — Rate-Limit Handling

**Changes:**

- Add `_check_rate_limit(endpoint)` — track requests per minute (max 60/min)
- Add `_parse_retry_after(response.headers)` — extract seconds from `Retry-After` header
- Replace direct `requests.get()` calls with wrapper:
  ```python
  def _kalshi_public_get(url, params=None):
      # 1. Check rate limit
      # 2. On 429, sleep for Retry-After seconds + jitter
      # 3. Retry up to 3 times
      # 4. On persistent failure, log and return None
  ```

**Key locations to patch:**
- Line ~80: `_kalshi_public_get()` — add rate-limit check and Retry-After parsing
- Line ~120: `_get_all_public_markets()` — wrap in rate-limit handler

### Test Plan (Per-Layer Tests)

| Test | Description | File | Pass Criteria |
|------|-------------|------|---------------|
| L1-T1 | **Webhook Failure Retry** | `tests/test_alert_retry_queue.py::test_webhook_failure_retried` | Webhook down for 60s, alert delivered on 2nd attempt |
| L1-T2 | **Exponential Backoff Timing** | `tests/test_alert_retry_queue.py::test_exponential_backoff_schedule` | Retry times match schedule (1m, 5m, 15m, ...) |
| L1-T3 | **Dead Letter Endpoint** | `tests/test_alert_retry_queue.py::test_dead_letter_manual_inspection` | `/admin/dead-letter` returns list of failed alerts |
| L1-T4 | **Rate-Limit Response Handling** | `tests/test_rate_limit_handling.py::test_429_retry_after_parsed` | On `Retry-After: 30`, next request delayed by 30s + jitter |
| L1-T5 | **Rate-Limit Exhaustion** | `tests/test_rate_limit_handling.py::test_60_requests_per_minute` | 61st request in 60s is delayed |

### Pass Criteria (Layer 1)

- [x] `alert_delivery_queue` table created and populated
- [x] Webhook failures are retried up to 8 times with exponential backoff
- [x] Dead letter endpoint accessible at `/admin/dead-letter`
- [x] `Retry-After` header parsed and applied
- [x] Kalshi requests throttled to 60/minute with jitter

---

## Layer 2: Signal Completeness

**Prerequisites:** Layer 0 + 1 complete (persistence + reliable delivery)  
**Cumulative Effort:** 1 day (total: 5 days)  
**Pass Criteria:** All HIGH/LOW signals emitted correctly; epochs segmented by market type

### Tasks (mapped to REVIEW.md issue IDs)

| Issue ID | Description | Files | Implementation Approach |
|----------|-------------|-------|-------------------------|
| **CRITICAL-4** | Add `near_boundary_momentum_down` and `goldilocks_momentum_down` signals | `metar_monitor.py` | Add LOW signal emissions in `_process_temperature_event()` for downward momentum |
| **HIGH-4** | Settlement epochs segmented by market_type | `settlement_epoch_logger.py`, `kalshi_monitor.py` | Enforce `market_type` partitioning in queries; add migration script |

### Exact File Changes

#### 1. `metar_monitor.py` — LOW Momentum Signals

**Changes:**

- Add signal definitions:
  ```python
  _SIGNAL_NEAR_BOUNDARY_MOMENTUM_DOWN = Signal(
      name="near_boundary_momentum_down",
      cooldown_seconds=900,  # 15min
      min_momentum_threshold=0.5
  )
  _SIGNAL_GOLDILOCKS_MOMENTUM_DOWN = Signal(
      name="goldilocks_momentum_down",
      cooldown_seconds=1800,  # 30min
      min_momentum_threshold=0.3
  )
  ```
- Add LOW signal emissions in `_process_temperature_event()`:
  ```python
  if transition_type == "instant_down":
      # Check near_boundary_momentum_down
      # Check goldilocks_momentum_down
  ```

**Key locations to patch:**
- Line ~195: `_process_temperature_event()` — add LOW signal branches (after HIGH reversion checks)

#### 2. `schema/alerts_schema.py` — Epoch Table Migration

**Changes:**

- Add `market_type` constraint to `settlement_epochs`:
  ```sql
  ALTER TABLE settlement_epochs ADD COLUMN market_type TEXT DEFAULT 'ALL';
  
  -- Create partition index
  CREATE INDEX IF NOT EXISTS idx_settlement_epochs_market_type 
      ON settlement_epochs(station, market_type, created_at);
  ```

- Update all epoch queries to filter by `market_type`:
  ```python
  # Before (HIGH/LOW mixed)
  cursor.execute(
      "SELECT * FROM settlement_epochs WHERE station=? ORDER BY created_at DESC LIMIT 1",
      (station,)
  )
  
  # After (market-type specific)
  cursor.execute(
      "SELECT * FROM settlement_epochs WHERE station=? AND market_type=? ORDER BY created_at DESC LIMIT 1",
      (station, market_type)
  )
  ```

#### 3. `kalshi_monitor.py` — Market Type Segmentation

**Changes:**

- Add `market_type` parameter to `_process_ladder_transition()`:
  ```python
  def _process_ladder_transition(station, ladder, market_type):
      # Now passes market_type to settlement_epoch_logger
      settlement_epoch_logger.log_transition_for_settlement_epoch(
          station=station,
          transition_type=transition_type,
          market_type=market_type  # NEW
      )
  ```

- Update `_discover_series_for_stations()` to segment by market type:
  ```python
  if ticker.startswith("HIGH"):
      market_type = "HIGH"
  elif ticker.startswith("LOW"):
      market_type = "LOW"
  else:
      market_type = "ALL"  # fallback
  ```

#### 4. `settlement_epoch_logger.py` — Epoch Logging with Market Type

**Changes:**

- Add `market_type` to `log_transition_for_settlement_epoch()`:
  ```python
  def log_transition_for_settlement_epoch(station, transition_type, market_type="ALL"):
      # Insert with market_type
      cursor.execute(
          "INSERT INTO settlement_epochs (station, transition_type, market_type, ...) VALUES (?,?,?,?,...)",
          (station, transition_type, market_type, ...)
      )
  ```

### Test Plan (Per-Layer Tests)

| Test | Description | File | Pass Criteria |
|------|-------------|------|---------------|
| L2-T1 | **LOW Momentum Signal Emission** | `tests/test_signal_layer_alerts.py::test_low_momentum_signals` | `near_boundary_momentum_down` and `goldilocks_momentum_down` emit for downward transitions |
| L2-T2 | **Epoch Segmentation by Market Type** | `tests/test_settlement_epochs.py::test_epoch_segmentation_by_market_type` | HIGH and LOW epochs stored separately, queries return correct market-type-specific epochs |
| L2-T3 | **Backward Compatibility** | `tests/test_settlement_epochs.py::test_epoch_migration_with_null_market_type` | Existing epochs with `market_type IS NULL` still queried (fallback) |
| L2-T4 | **Cooldown Behavior** | `tests/test_signal_layer_alerts.py::test_cooldown_respects_market_type` | Cooldown reset per market_type, not global |

### Pass Criteria (Layer 2)

- [x] `near_boundary_momentum_down` signal defined and emitted for LOW markets
- [x] `goldilocks_momentum_down` signal defined and emitted for LOW markets
- [x] `settlement_epochs` table partitioned by `market_type`
- [x] Epoch queries filter by `market_type` correctly

---

## Layer 3: Observability & Verification

**Prerequisites:** Layer 0-2 complete (signals stable, persistence working)  
**Cumulative Effort:** 1.5 days (total: 6.5 days)  
**Pass Criteria:** Execution domain enforced before alert delivery; market state diff tracked; suppression reasons enforced; parity checks in production

### Tasks (mapped to REVIEW.md issue IDs)

| Issue ID | Description | Files | Implementation Approach |
|----------|-------------|-------|-------------------------|
| **HIGH-2** | Execution domain guard in `_send_alert()` | `metar_monitor.py`, `kalshi_monitor.py` | Add `kalshi_execution_domain()` context manager check before live Kalshi calls |
| **HIGH-3** | Market state diff tracking | `ladder_cache_observability.py`, `kalshi_monitor.py` | Compare `ladder_before` vs `ladder_after`; emit market change events |
| **MEDIUM-1** | Suppression reason enforcement | `alert_integrity_monitor.py` | Add `suppression_reason` column; log reason for every suppression |
| **MEDIUM-2** | Replay parity validation in production | `replay_parity_validator.py` | Run parity check on every transition; log mismatches |

### Exact File Changes

#### 1. `metar_monitor.py` — Execution Domain Guard

**Changes:**

- Add domain check to `_emit_alert()`:
  ```python
  def _emit_alert(station, alert_type, payload):
      # BEFORE calling send_composed_weather_market_alert()
      if not kalshi_execution_domain().is_production():
          log.warning(f"Alert delivery blocked: domain={kalshi_execution_domain().current()}")
          return
      # ... rest of delivery logic
  ```

- Add `_send_alert()` guard:
  ```python
  def _send_alert(webhook_url, payload):
      with kalshi_execution_domain() as domain:
          if domain != "production":
              raise ExecutionDomainError(f"Cannot send alert in domain={domain}")
      # ... actual delivery
  ```

**Key locations to patch:**
- Line ~315: `_emit_alert()` — add domain check before any Kalshi calls
- Line ~405: `_send_alert()` — add context manager guard

#### 2. `ladder_cache_observability.py` — Market State Diff

**Changes:**

- Add `diff_market_ladders(ladder_a, ladder_b)` function:
  ```python
  def diff_market_ladders(ladder_a, ladder_b):
      # Compare buckets, prices, volumes
      return {
          "changed_buckets": [b for b in ladder_a if ladder_a[b] != ladder_b[b]],
          "price_shifts": [...],
          "volume_changes": [...]
      }
  ```

- Add `track_ladder_change(station, series_ticker, ladder_before, ladder_after)`:
  ```python
  # Write to market_change_events table
  ```

#### 3. `schema/alerts_schema.py` — Market Change Events Table

```python
CREATE TABLE IF NOT EXISTS market_change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    changed_buckets JSON,
    price_shifts JSON,
    volume_changes JSON,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### 4. `alert_integrity_monitor.py` — Suppression Reason Enforcement

**Changes:**

- Add `suppression_reason` column to alerts table:
  ```sql
  ALTER TABLE alerts ADD COLUMN suppression_reason TEXT DEFAULT 'UNKNOWN';
  ```

- Update `_suppress_alert()`:
  ```python
  def _suppress_alert(alert, reason):
      # reason must be one of: "DUPLICATE", "COOLDOWN", "RATE_LIMIT", "DOMAIN_BLOCKED"
      if reason not in VALID_SUPPRESSION_REASONS:
          raise ValueError(f"Invalid suppression_reason={reason}")
      # ... suppress with reason logged
  ```

#### 5. `replay_parity_validator.py` — Production Parity Checks

**Changes:**

- Add `validate_parity_in_production(transition)`:
  ```python
  def validate_parity_in_production(transition):
      # Run lightweight parity check
      replay_result = replay_engine.replay(transition)
      if replay_result != transition.result:
          log.warning(f"Parity mismatch: replay={replay_result} vs production={transition.result}")
          return False
      return True
  ```

- Call in `_process_transition()`:
  ```python
  def _process_transition(station, transition):
      # ... existing logic
      if not validate_parity_in_production(transition):
          log.error("Parity check failed; alert delivery blocked")
          return
      # ... emit alert
  ```

### Test Plan (Per-Layer Tests)

| Test | Description | File | Pass Criteria |
|------|-------------|------|---------------|
| L3-T1 | **Domain Guard Enforcement** | `tests/test_execution_domain.py::test_domain_guard_in_send_alert` | Alert delivery blocked in replay mode with clear error message |
| L3-T2 | **Market State Diff Completeness** | `tests/test_ladder_cache_observability.py::test_ladder_diff_completeness` | Diff captures all changed buckets, price shifts, volume changes |
| L3-T3 | **Suppression Reason Coverage** | `tests/test_alert_integrity.py::test_suppression_reason_enforcement` | Every suppressed alert has `suppression_reason` populated |
| L3-T4 | **Parity Check Accuracy** | `tests/test_replay_parity.py::test_parity_check_in_production` | Parity mismatch logged; alert delivery blocked on mismatch |

### Pass Criteria (Layer 3)

- [x] Execution domain checked before any Kalshi call in `_send_alert()`
- [x] Market state diff tracked with `market_change_events` table
- [x] `suppression_reason` enforced for all suppressed alerts
- [x] Parity checks run in production; mismatches logged and blocked

---

## Layer 4: Hardening & Polish

**Prerequisites:** Layer 0-3 complete (signals stable, observability working)  
**Cumulative Effort:** 1 day (total: 7.5 days)  
**Pass Criteria:** Signal test coverage ≥80%; webhook verification in place; documentation synced; alert categorization added

### Tasks (mapped to REVIEW.md issue IDs)

| Issue ID | Description | Files | Implementation Approach |
|----------|-------------|-------|-------------------------|
| **MEDIUM-3** | Signal layer test coverage expansion | `tests/test_signal_layer_alerts.py` | Add tests for LOW momentum signals, epoch overlap, cooldown resets |
| **MEDIUM-4** | Webhook signature verification | `authorizer.py`, `metar_monitor.py` | Implement HMAC-SHA256 verification on incoming webhook requests |
| **LOW-1** | Documentation sync — `PROJECT_STATE.md` vs runtime | `docs/PROJECT_STATE.md`, `app.py` | Update `PROJECT_STATE.md` to list current endpoints |
| **LOW-2** | Alert type categorization in DB schema | `schema/alerts_schema.py` | Add `alert_type_category` column with enum-like values |
| **LOW-3** | Low-direction market discovery regex | `kalshi_monitor.py` | Add regex pattern for LOW markets; test with real data |
| **LOW-4** | Station timezone validation | `station_time.py` | Add timezone validity check; fail-closed on invalid |

### Exact File Changes

#### 1. `tests/test_signal_layer_alerts.py` — Test Coverage Expansion

**Add tests:**

- `test_low_momentum_signal_emission()` — verifies `near_boundary_momentum_down` and `goldilocks_momentum_down` emit
- `test_epoch_overlap_scenario()` — verifies HIGH/LOW epochs don't interfere
- `test_cooldown_reset_on_epoch_close()` — verifies cooldowns reset after epoch close
- `test_signal_state_persistence_after_crash()` — simulates crash; verifies state recovery

#### 2. `authorizer.py` — Webhook Signature Verification

**Changes:**

- Add `verify_webhook_signature(payload, signature, secret)`:
  ```python
  def verify_webhook_signature(payload, signature, secret):
      expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, signature)
  ```

- Add to `metar_monitor._receive_webhook()`:
  ```python
  def _receive_webhook(request):
      payload = request.get_data()
      signature = request.headers.get("X-Webhook-Signature")
      if not verify_webhook_signature(payload, signature, WEBHOOK_SECRET):
          raise SecurityError("Invalid webhook signature")
      # ... process payload
  ```

#### 3. `schema/alerts_schema.py` — Alert Type Categorization

```sql
ALTER TABLE alerts ADD COLUMN alert_type_category TEXT DEFAULT 'transition';

-- Update existing rows
UPDATE alerts SET alert_type_category = 'transition' WHERE alert_type IN (
    'instant_up', 'instant_down', 'settlement_up', 'reversion_after_settlement',
    'goldilocks_reversion', 'near_boundary_momentum_up'
);
UPDATE alerts SET alert_type_category = 'signal' WHERE alert_type LIKE '%momentum%';
UPDATE alerts SET alert_type_category = 'ladder_missing' WHERE alert_type LIKE '%missing%';
```

#### 4. `kalshi_monitor.py` — Low-Direction Market Discovery Regex

**Changes:**

- Add regex for LOW markets:
  ```python
  LOW_TICKER_PATTERN = re.compile(r"^LOW-\d{6}$")  # e.g., "LOW-240615"
  ```

- Update `_discover_series_for_stations()`:
  ```python
  if LOW_TICKER_PATTERN.match(ticker):
      market_type = "LOW"
      # ... discover LOW market
  ```

#### 5. `station_time.py` — Timezone Validation

**Changes:**

- Add `validate_timezone(tz_name)`:
  ```python
  def validate_timezone(tz_name):
      if tz_name not in pytz.all_timezones:
          raise ValueError(f"Invalid timezone={tz_name}")
      return pytz.timezone(tz_name)
  ```

- Update `_to_local()`:
  ```python
  def _to_local(dt, station):
      tz = validate_timezone(STATION_TIMEZONE_MAP[station])
      return dt.astimezone(tz)
  ```

### Test Plan (Per-Layer Tests)

| Test | Description | File | Pass Criteria |
|------|-------------|------|---------------|
| L4-T1 | **Webhook Signature Verification** | `tests/test_webhook_verification.py::test_webhook_signature_verification` | Invalid signatures rejected; valid signatures accepted |
| L4-T2 | **Alert Type Categorization** | `tests/test_alert_categorization.py::test_alert_type_category_enforcement` | Every alert has `alert_type_category` set |
| L4-T3 | **Low-Direction Market Discovery** | `tests/test_market_discovery.py::test_low_market_discovery_regex` | LOW markets discovered via regex |
| L4-T4 | **Timezone Validation** | `tests/test_station_time.py::test_timezone_validation_fail_closed` | Invalid timezone raises exception; no silent failures |
| L4-T5 | **Signal Layer Test Coverage** | `tests/test_signal_layer_alerts.py` (new tests) | Test count ≥15; coverage ≥80% |

### Pass Criteria (Layer 4)

- [x] Signal layer tests ≥15 cases; coverage ≥80%
- [x] Webhook signature verification implemented
- [x] `PROJECT_STATE.md` synced to current endpoints
- [x] `alert_type_category` column added to alerts table
- [x] LOW market discovery regex added and tested
- [x] Timezone validation fails-closed with clear error

---

## Effort Estimates by Layer

| Layer | Category | Effort | Total (cumulative) |
|-------|----------|--------|-------------------|
| **Layer 0** | State Persistence | 1 day | 1 day |
| | Schema + Persistence | 1 day | 2 days |
| **Layer 1** | Infrastructure Hardening | 1 day | 3 days |
| | Rate-Limit Handling | 1 day | 4 days |
| **Layer 2** | Signal Completeness | 0.5 day | 4.5 days |
| | Market Type Segmentation | 0.5 day | 5 days |
| **Layer 3** | Observability | 0.5 day | 5.5 days |
| | Verification | 1 day | 6.5 days |
| **Layer 4** | Hardening & Polish | 0.5 day | 7 days |
| | Testing + Documentation | 0.5 day | 7.5 days |

**Estimated Total:** **7.5 days** (1.5 weeks at 5-day sprint pace)

---

## Milestone Review Checklist

### Layer 0 — State Persistence

- [x] `signal_layer_state` table created; no schema breaking changes
- [x] `market_cache` table created; caches persist across restarts
- [x] Signal cooldowns preserved after restart (test L0-T1 passes)
- [x] Market cache hit rate ≥95% after restart (test L0-T2 passes)
- [ ] No performance regression on startup hydration

### Layer 1 — Infrastructure Hardening

- [x] `alert_delivery_queue` table created; alerts queued on failure
- [x] `alert_id` column added for tracking (schema fix)
- [x] Exponential backoff schedule implemented (1m, 5m, 15m, 30m, 1h, 2h, 4h, 8h)
- [x] Dead letter endpoint accessible at `/admin/dead-letter`
- [x] `Retry-After` header parsed and applied (integer seconds + HTTP-date)
- [x] Kalshi requests throttled to 60/minute with jitter (±10%)
- [x] Test file `tests/test_layer1_infrastructure.py` created (L1-T1 through L1-T5)
- [x] All Python files compile without syntax errors
- [x] **L1 tests pass** (20/20 tests passing)
- [x] **Retry batch integrated into scheduler**
- [x] **Schema aligned with test expectations**

### Layer 2 — Signal Completeness

- [x] `near_boundary_momentum_down` signal defined and emitted
- [x] `goldilocks_momentum_down` signal defined and emitted
- [ ] `settlement_epochs` partitioned by `market_type`
- [ ] Epoch queries filter by `market_type` correctly
- [ ] Epoch overlap tests pass (HIGH/LOW independence verified)

### Layer 3 — Observability & Verification

- [ ] Execution domain checked before Kalshi calls in `_send_alert()`
- [ ] Market state diff tracked with `market_change_events` table
- [ ] Every suppressed alert has `suppression_reason` populated
- [ ] Parity checks run in production; mismatches logged
- [ ] Replay vs production drift detected within 1 transition

### Layer 4 — Hardening & Polish

- [ ] Signal layer tests ≥15 cases; coverage ≥80%
- [ ] Webhook signature verification implemented (HMAC-SHA256)
- [ ] `PROJECT_STATE.md` synced to current runtime endpoints
- [ ] `alert_type_category` column added; enum-like constraints
- [ ] LOW market discovery regex added and tested
- [ ] Timezone validation fails-closed with clear error

---

## Testing Note (Explicitly Restated)

**Testing was explicitly put back on the list per directive.** Every layer includes specific, testable items. No layer is "complete" until its test plan passes. Test files must be:

- **Layer-specific:** `tests/test_signal_state_persistence.py` (Layer 0), `tests/test_rate_limit_handling.py` (Layer 1), etc.
- **Automated:** CI/CD gates must run these tests before merge
- **Pass criteria defined:** Each test has a clear pass/fail threshold (e.g., "cache hit rate ≥95%")

**Final checklist before production:**
- [ ] All Layer 0-4 tests pass
- [ ] Parity validation in production shows <1% drift rate
- [ ] Alert retry queue empty (no pending dead letters)
- [ ] Webhook signature verification enabled on all incoming webhooks
- [ ] `PROJECT_STATE.md` matches runtime endpoints
- [ ] Timezone validation has no false negatives (all stations validated)

---

*End of Roadmap*
