# First-Principles API Circuit Breaker — Kalshi Trade API

**Design Date:** 2026-08-01  
**Author:** First-Principles Systems Design Expert  
**Status:** Design Draft  
**Target System:** Weather trading engine polling Kalshi `/trade-api/v2`  

---

## 1. Design Philosophy (First Principles)

### Principle 1: Failure is the default, success is the exception
Retry logic treats failure as the norm. The circuit breaker treats sustained failure as a system-level signal that must propagate upward, not be swallowed by exponential backoff.

### Principle 2: Recoverability is a first-class state, not a side effect
HALF-OPEN is not a "try one request" hack. It is a full system verification gate that tests not just the API endpoint but also whether our cached state and internal invariants survived the outage.

### Principle 3: Backpressure must be observable
Every circuit transition emits a structured event. The absence of events is itself a signal (circuit is CLOSED and healthy).

### Principle 4: Rate limits and circuit breaks are complementary, not redundant
A rate limiter prevents self-inflicted denial of service. A circuit breaker detects *upstream* distress. Both must coexist with clear boundary definitions.

---

## 2. State Machine

### States

```
   ┌─────────────┐
   │   CLOSED    │  ← Normal operation — requests flow normally
   └──────┬──────┘
          │ consecutive_errors >= threshold
          │ OR error_rate_in_window >= threshold
          ▼
   ┌─────────────┐
   │    OPEN     │  ← Fail-fast — requests rejected immediately
   └──────┬──────┘
          │ cool_down_expired
          ▼
   ┌─────────────┐
   │  HALF_OPEN  │  ← Probe mode — limited trial requests allowed
   └──────┬──────┘
          │                    │
          ├─ probe_success    ─┤
          │                    │
          ▼                    ▼
   ┌─────────────┐   ┌─────────────┐
   │   CLOSED    │   │  HALF_OPEN_ │
   │             │   │  BACKOFF    │
   └─────────────┘   └──────┬──────┘
                            │ cooldown
                            ▼
                     ┌─────────────┐
                     │  HALF_OPEN  │  ← Retry probe
                     └─────────────┘
```

### Full State Table

| State | Meaning | Request Handling | Entry Condition | Exit Condition |
|---|---|---|---|---|
| `CLOSED` | Normal operation | Pass through; track error metrics | Startup / recovery confirmed | Error threshold breached |
| `OPEN` | Circuit tripped | Fail-fast (raise exception immediately) | Error threshold exceeded | Cooldown timer expires |
| `HALF_OPEN` | Probe mode | Allow limited test requests | Cooldown expired | Decision: success→CLOSED, failure→HALF_OPEN_BACKOFF |
| `HALF_OPEN_BACKOFF` | Probe failed | Fail-fast again | Probe request in HALF_OPEN failed | Backoff timer expires |
| `BYPASS` | Manual override | Pass through unconditionally | Operator intervention | Operator clears or timeout |

### Transition Triggers

#### CLOSED → OPEN transitions (multiple guards, OR logic)

| Guard | Parameter | Default | Rationale |
|---|---|---|---|
| Consecutive 5xx/429/timeout | `failure_count_threshold` | 5 | Prevents flapping from a single transient blip |
| Error rate in sliding window | `error_rate_threshold` | 0.5 | Catches slow degradation (e.g., 3 of last 5 requests failed) |
| 403/401 auth failure | `auth_failure_threshold` | 1 | One auth failure = dead credentials; open immediately |
| Retry-After > max tolerance | `max_retry_after_seconds` | 600 | If Kalshi says "wait 15 min", we circuit-break |

#### OPEN → HALF_OPEN

| Guard | Parameter | Default |
|---|---|---|
| Cooldown timer expired | `base_cooldown_seconds` | 60 |
| Jitter addition | `cooldown_jitter_max` | 30 |

#### HALF_OPEN → CLOSED

| Guard | Parameter | Default |
|---|---|---|
| Successful probe requests | `probe_success_count` | 1 |
| (Optional) consecutive successes | `probe_consecutive_successes` | 2 |

#### HALF_OPEN → HALF_OPEN_BACKOFF

| Guard | Parameter | Default |
|---|---|---|
| Probe request failed | — | immediate |

#### HALF_OPEN_BACKOFF → HALF_OPEN

| Guard | Parameter | Default |
|---|---|---|
| Backoff timer expired | `retry_backoff_seconds` | 120 (→ 240 → 480 → cap 3600) |

### Complete Transition Diagram (including internal maintenance)

```
                    ┌─────────────────────────────────────┐
                    │          MAINTENANCE CLOCK           │
                    │  (runs every 15 seconds)             │
                    └──────────┬──────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
   ┌────────────┐      ┌────────────┐      ┌────────────┐
   │  CLOSED    │      │   OPEN     │      │ HALF_OPEN  │
   │            │      │            │      │            │
   │ • count    │      │ • check    │      │ • if all   │
   │   errors   │      │   cooldown │      │   requests │
   │ • emit     │      │ • emit     │      │   succeed  │
   │   metrics  │      │   counting │      │   → CLOSED │
   │            │      │   down     │      │ • else     │
   │ if too     │      │ • when     │      │   → HALF_  │
   │ many       │      │   expired  │      │   OPEN_BK  │
   │ fail: OPEN │      │   → HALF_  │      │            │
   │            │      │   OPEN     │      │            │
   └────────────┘      └────────────┘      └────────────┘
```

---

## 3. Parameter Recommendations

### Core Thresholds

```python
# File: core/circuit_breaker.py (proposed values)

FAILURE_COUNT_THRESHOLD    = 5       # Consecutive errors before OPEN
ERROR_RATE_THRESHOLD       = 0.5     # In a sliding window of 10 requests
SLIDING_WINDOW_SIZE        = 10      # Last N requests for rate calculation
AUTH_FAILURE_THRESHOLD     = 1       # 401/403 = instant open

BASE_COOLDOWN_SECONDS      = 60      # First cool-down duration
COOLDOWN_JITTER_MAX        = 30      # Random jitter added (0–30s)

PROBE_SUCCESS_COUNT        = 1       # Successful probes needed to close
PROBE_MIN_REQUESTS         = 1       # Minimum probe requests in HALF_OPEN
PROBE_MAX_REQUESTS         = 3       # Max probe requests in HALF_OPEN

RETRY_BACKOFF_MULTIPLIER   = 2.0     # Multiplicative backoff
RETRY_BACKOFF_CAP          = 3600    # Cap at 1 hour
RETRY_BACKOFF_BASE         = 120     # Starting backoff for HALF_OPEN_BACKOFF

MAX_RETRY_AFTER_HEADER     = 600     # If Retry-After > 600s, circuit break
```

### Sliding Window vs. Consecutive Counter

**Use BOTH, not either/or:**

- **Consecutive counter** (`CLOSED→OPEN`: 5 consecutive failures):  
  Catches sudden outage fast. Resets to 0 on any success.

- **Sliding window error rate** (`CLOSED→OPEN`: error_rate > 0.5 in last 10):  
  Catches slow degradation. Does not reset on success — only slides.

Implementation:

```python
_rolling_errors = collections.deque(maxlen=10)  # [bool]*10: True if error

def _sliding_error_rate() -> float:
    if not _rolling_errors:
        return 0.0
    return sum(_rolling_errors) / len(_rolling_errors)

def _record_outcome(success: bool):
    _rolling_errors.append(not success)
```

### Per-Endpoint vs. Global

**Recommendation: Two-tier hierarchy**

```
GlobalCircuit
  ├── /markets (public, GET)
  ├── /series  (public, GET)
  └── Authenticated endpoints
       ├── /portfolio (GET)
       ├── /orders    (POST/GET)
       └── /settlement (GET)
```

- **Per-endpoint (or per-group) circuits** for the most specific protection
- **Global circuit** as a parent guard that opens if ALL sub-circuits are open or if a catastrophic error (network unreachable, DNS failure) occurs

Rationale: `/series?tags=Daily%20temperature` might be rate-limited while `/markets?limit=5` works fine. Conversely, if the entire Kalshi API is down, we don't want to probe each endpoint individually.

### Endpoint Grouping Strategy

| Group ID | Endpoints | Circuit Key | Priority |
|---|---|---|---|
| `public_discovery` | `/series`, `/markets` | `circuit:public:disc` | High |
| `public_hydration` | `/series/{ticker}` | `circuit:public:hydrate` | High |
| `auth_portfolio` | `/portfolio/*` | `circuit:auth:portfolio` | Medium |
| `auth_orders` | `/orders/*` | `circuit:auth:orders` | Low |
| `auth_settlement` | `/settlement/*` | `circuit:auth:settlement` | Low |
| `global` | ALL | `circuit:global` | Critical |

---

## 4. Error Classification

### Error → Circuit Breaker Response Matrix

| Error Type | HTTP/Signal | Recoverable? | Counts Towards Open? | Circuit Entry on First Occurrence? |
|---|---|---|---|---|
| **Rate Limited** | 429 | Yes | Yes | No (handle via retry-after first) |
| **Service Unavailable** | 503 | Yes | Yes | No (handle via retry-after first) |
| **Gateway Timeout** | 504 | Yes | Yes | No (single retry after 1s) |
| **Connection Timeout** | socket.timeout | Yes | Yes | No (single retry) |
| **Connection Reset** | ConnectionResetError | Yes | Yes | No (single retry) |
| **DNS Failure** | socket.gaierror | Yes | Yes | Yes (immediate open, global) |
| **Bad Gateway** | 502 | Partial | Yes | No |
| **Too Many Requests** | 429 + Retry-After > 10m | No | No | Yes (immediate open) |
| **Unauthorized** | 401 | No (key rotation) | No | Yes (open, emit CRITICAL alert) |
| **Forbidden** | 403 | No (permissions) | No | Yes (open, emit CRITICAL alert) |
| **Bad Request** | 400 | No (client bug) | No | No (log ERROR, do not circuit-break) |
| **Not Found** | 404 | No (data issue) | No | No (log WARNING) |
| **Internal Server Error** | 500 | Uncertain | Yes | No |

### Fatal vs. Non-Fatal Distinction

```
is_fatal = response.status_code in {401, 403}
           or (response.status_code == 400 and "invalid" in body.lower())

is_rate_limit = response.status_code == 429

is_service_error = response.status_code in {502, 503, 504}
                   or isinstance(error, (ConnectionError, TimeoutError))
```

**Fatal errors bypass the circuit breaker entirely** — they are escalated directly to the alert system, do not transition the circuit state, but DO block the calling request.

**Rate limit errors** are first handled by the existing rate limiter (`_check_rate_limit`, `_persist_rate_limit_entry`). The circuit breaker only sees them if the rate limiter was already saturated (i.e., we're hitting 429 despite respecting limits, which means Kalshi has lowered their limit).

---

## 5. Integration Points with Existing Code

### Existing `core/kalshi_monitor.py` — Key Integration Hooks

| Existing Function | Integration Point | What Changes |
|---|---|---|
| `_kalshi_public_get(path)` | All public GET requests go through a circuit-broken wrapper | Replace direct `requests.get()` with `_ciruited_get()` |
| `_kalshi_public_get_with_rate_limit(path)` | Rate-limited variant | Add circuit breaker check BEFORE rate limit check |
| `_kalshi_get(path)` | Authenticated GET requests | Replace direct call with circuit-broken call |
| `_check_rate_limit()` | Existing SQLite-based rate limiter | Circuit breaker reads/writes to same SQLite DB |
| `_persist_rate_limit_entry()` | Records request timestamps | No change needed (circuit breaker reads this) |
| `_hydration_backoff_until` | Per-station hydration backoff | Keep; circuit breaker is orthogonal (for API-level outages, not station-specific issues) |
| `ensure_ladder_hydration_prerequisite()` | Calls `_kalshi_public_get` indirectly | Circuit breaker will protect this call path |
| `ensure_series_discovery_loaded()` | Calls `_kalshi_public_get` | Circuit breaker protects |

### New Module: `core/circuit_breaker.py`

Proposed structure:

```python
# core/circuit_breaker.py

import time
import random
import logging
import threading
import enum
from typing import Optional

logger = logging.getLogger(__name__)

class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    HALF_OPEN_BACKOFF = "HALF_OPEN_BACKOFF"
    BYPASS = "BYPASS"

class CircuitBreaker:
    def __init__(self, name: str, config: dict = None):
        self.name = name
        self._state = CircuitState.CLOSED
        self._lock = threading.Lock()
        
        # Error tracking
        self._consecutive_failures = 0
        self._rolling_errors = collections.deque(maxlen=10)
        
        # Timing
        self._last_failure_time = 0.0
        self._state_changed_at = 0.0
        self._open_until = 0.0
        
        # HALF_OPEN probe tracking
        self._probe_requests_allowed = 0
        self._probe_successes = 0
        self._probe_total = 0
        
        # Backoff
        self._backoff_attempt = 0
        
        # Config
        self._config = {**self._default_config(), **(config or {})}
    
    # ... (see full implementation details below)
```

### Integration Wrapper Pattern

Instead of modifying every call site, create a single wrapper:

```python
# In core/kalshi_monitor.py or new core/circuit_breaker.py

def _circuited_public_get(path: str, circuit_name: str = "public_discovery") -> dict:
    """Circuit-broken wrapper around _kalshi_public_get."""
    
    circuit = _get_circuit(circuit_name)
    
    # Pre-flight check
    if circuit.state in (CircuitState.OPEN, CircuitState.HALF_OPEN_BACKOFF):
        # Fail fast — raise a structured exception
        circuit._record_attempt(fast_fail=True)
        raise CircuitOpenError(
            f"Circuit '{circuit_name}' is {circuit.state.value}. "
            f"Cooldown remaining: {circuit.remaining_cooldown():.0f}s"
        )
    
    if circuit.state == CircuitState.HALF_OPEN and circuit.probe_exhausted():
        raise CircuitOpenError(
            f"Circuit '{circuit_name}' is HALF_OPEN but probe budget exhausted. "
            f"Wait for next probe window."
        )
    
    try:
        # Call the existing rate-limited function
        result = _kalshi_public_get_with_rate_limit(path)
        circuit._record_success()
        return result
    except requests.HTTPError as e:
        status = e.response.status_code if hasattr(e, 'response') else None
        circuit._record_failure(status=status, error=e)
        raise
    except (ConnectionError, TimeoutError) as e:
        circuit._record_failure(status=None, error=e)
        raise
    except Exception as e:
        circuit._record_failure(status=None, error=e)
        raise
```

---

## 6. Alert Emissions

### Event Types

| Event | Severity | Channel | Payload |
|---|---|---|---|
| `circuit.opened` | ERROR | logging + structured alert | name, state, consecutive_failures, error_rate, last_error |
| `circuit.half_open` | WARNING | logging + structured alert | name, cooldown_duration, backoff_attempt |
| `circuit.closed` | INFO | logging + notification | name, duration_open_seconds, total_failures_during_open |
| `circuit.probe_failed` | WARNING | logging | name, attempts, last_error |
| `circuit.auth_failure` | CRITICAL | logging + structured alert + notification | name, endpoint, error_detail |
| `circuit.fatal_error` | CRITICAL | logging + structured alert + notification | name, error_type, error_detail |
| `circuit.long_open` | WARNING | logging | name, open_duration_seconds (emitted after 5/15/30min) |

### Integration with Existing `_send_kalshi_market_alert()`

The existing alert function writes to `kalshi_rate_limit` table and uses `_alert_db_path()`. Circuit breaker alerts should:

1. Write to a new `circuit_breaker_events` table in the same SQLite DB
2. Also emit to `_LOGGER.warning/error` with a structured prefix (`circuit_breaker=`)
3. For CRITICAL events (auth failure, fatal), also call the existing alert path with high priority

Proposed schema:

```sql
CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    circuit_name TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- 'opened' | 'closed' | 'half_open' | 'auth_failure' | 'fatal_error'
    previous_state TEXT,
    new_state TEXT,
    consecutive_failures INTEGER,
    error_rate REAL,
    cooldown_seconds INTEGER,
    backoff_attempt INTEGER,
    error_detail TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);
```

### Logging Format

All circuit events must be parseable by the observability layer:

```
circuit_breaker=1 circuit=public_discovery event=opened state=CLOSED→OPEN failures=5 error_rate=0.50 reason="503 Service Unavailable" cooldown=60
circuit_breaker=1 circuit=public_discovery event=half_open state=OPEN→HALF_OPEN cooldown_remaining=0 backoff_attempt=0
circuit_breaker=1 circuit=public_discovery event=closed state=HALF_OPEN→CLOSED probe_successes=1 duration_open=67.3s
circuit_breaker=1 circuit=global event=auth_failure state=CLOSED→OPEN failures=1 error="401 Unauthorized - key expired?"
```

---

## 7. Cooldown and Backoff Strategy

### Standard Cooldown (OPEN → HALF_OPEN)

```
cooldown = base_cooldown_seconds + random(0, cooldown_jitter_max)
```

**Why jitter?** If multiple processes/containers trip simultaneously, jitter prevents thundering herd on the probe.

### Backoff (HALF_OPEN_BACKOFF → HALF_OPEN)

```
backoff = min(RETRY_BACKOFF_BASE * (RETRY_BACKOFF_MULTIPLIER ** attempt), RETRY_BACKOFF_CAP)
backoff += random(0, min(backoff * 0.2, 60))  # 20% jitter, capped at 60s
```

Where `attempt` resets to 0 on successful transition to CLOSED.

### Probe Budget (HALF_OPEN)

In HALF_OPEN, allow `PROBE_MAX_REQUESTS` (default 3) requests total. Track:

```python
def _enter_half_open(self):
    self._probe_requests_allowed = self._config['probe_max_requests']
    self._probe_successes = 0
    self._probe_total = 0

def probe_exhausted(self) -> bool:
    return self._probe_total >= self._probe_requests_allowed

def _record_probe_outcome(self, success: bool):
    self._probe_total += 1
    if success:
        self._probe_successes += 1
```

Decision criteria:
- If any probe fails → `HALF_OPEN_BACKOFF`
- If ALL probes succeed → `CLOSED` (need `probe_success_count >= probe_min_requests`)

### Maximum Open Duration

If a circuit stays OPEN for > 1 hour, emit a `circuit.long_open` WARNING event every 30 minutes. After 4 hours, consider:
- Escalating to CRITICAL
- Suspending the 15-minute polling cycle entirely (not just the API calls)
- Notifying operator via external channel

---

## 8. Pending Requests During OPEN State

### Policy: Fail Fast

When a circuit is OPEN:
1. Immediately raise `CircuitOpenError` — do NOT queue, do NOT retry
2. The caller (polling loop) handles `CircuitOpenError` by:
   - Skipping that cycle
   - Logging: `"circuit_open_skip cycle={cycle} circuit={name}"`
   - Continuing to the next station/endpoint

### Rationale

- **15-minute polling cycle is long enough** that queueing is meaningless
- **State is perishable** — a queued request from 15 minutes ago is stale by the time the circuit closes
- **No request coalescing** needed; we don't send bulk queries
- **Simplicity wins** — no queue management, no re-insertion logic, no memory pressure

### Graceful Degradation Flow

```
Poll tick (every 15 min per station)
  │
  ├─ Check circuit state for endpoint group
  │    │
  │    ├─ CLOSED → make request (normal path)
  │    │
  │    ├─ OPEN → skip, log "circuit_open_skip", await next cycle
  │    │         Optional: read from market cache (SQLite) as fallback
  │    │
  │    └─ HALF_OPEN → make request if probe budget available
  │
  └─ After request:
       ├─ Success → circuit._record_success()
       └─ Failure → circuit._record_failure()
```

### Cache Fallback During OPEN

When circuit is OPEN, the polling loop SHOULD:
1. Read cached market data from SQLite (`_load_market_cache`)
2. Use the last known good snapshot for station-level decision making
3. Log: `"using_stale_cache circuit={name} age={age_seconds}"`

This ensures the system continues producing output (even if stale) rather than going dark.

---

## 9. Testing Plan

### Unit Tests (no network)

| Test | Description | Verification |
|---|---|---|
| `test_closed_to_open_consecutive` | Feed 5 consecutive failures, verify state→OPEN | Assert state == OPEN |
| `test_closed_to_open_rate` | Feed 6 failures out of 10, verify state→OPEN | Assert state == OPEN |
| `test_open_to_half_open` | Wait for cooldown+jitter, verify state→HALF_OPEN | Assert state == HALF_OPEN |
| `test_half_open_to_closed` | Feed 1 success in HALF_OPEN, verify state→CLOSED | Assert state == CLOSED |
| `test_half_open_to_backoff` | Feed 1 failure in HALF_OPEN, verify state changes | Assert state == HALF_OPEN_BACKOFF |
| `test_backoff_exponential` | Verify successive backoff durations are ~2x, capped | Assert durations within tolerance |
| `test_jitter_non_deterministic` | Verify two sequential cooldowns differ | Assert cooldowns not equal |
| `test_fatal_error_immediate_open` | Simulate 401, verify immediate OPEN | Assert state == OPEN, assert AUTH_FAILURE event |
| `test_error_classification` | Verify 400 vs 429 vs 503 classification | Assert is_fatal/is_rate_limit/is_service_error |
| `test_sliding_window_reset` | Verify successes push errors out of window | Assert error_rate decreases |
| `test_concurrent_safety` | 10 threads hammering the breaker | Assert no race conditions, state consistent |

### Integration Tests (mock Kalshi)

| Test | Description | Verification |
|---|---|---|
| `test_kalshi_429_integration` | Mock server returns 429 6 times, then 200 | Verify circuit opens after 5, closes after cooldown+probe |
| `test_kalshi_503_flap` | Mock returns 503 3 times, 200 2 times, 503 3 times | Verify sliding window catches it |
| `test_kalshi_401_immediate` | Mock returns 401 once | Verify circuit opens on first, CRITICAL emitted |
| `test_multi_endpoint_independence` | Mock: `/markets` failing, `/series` working | Verify per-endpoint circuit independence |
| `test_global_circuit_propagation` | Force global circuit open | Verify ALL endpoint circuits honor it |

### Simulation Tests (against Kalshi sandbox)

| Test | Description | Verification |
|---|---|---|
| `test_sandbox_rate_limit_behavior` | Exceed rate limit deliberately, observe 429 | Verify circuit opens as expected |
| `test_sandbox_recovery` | After rate limit cooldown, verify auto-recovery | Verify circuit transitions CLOSED |
| `test_sandbox_auth_failure` | Use expired key, verify immediate circuit open | Verify CRITICAL logged |

### Load Test (against mock)

```
Send 100 requests/minute to circuit-broken endpoint
├─ 50 succeed
├─ 30 return 503
├─ 10 return 429
└─ 10 time out
Verify:
  - Circuit opens within 15 requests
  - Probe budget is respected
  - Fast-fail rate meets expected (OPEN responses in <1ms)
```

### Test Harness: `tests/test_circuit_breaker.py`

```python
"""Test circuit breaker state machine and integration."""

import time
import pytest
from unittest.mock import Mock, patch
from core.circuit_breaker import CircuitBreaker, CircuitState, CircuitOpenError

def test_consecutive_failure_opens_circuit():
    cb = CircuitBreaker("test", {"failure_count_threshold": 3})
    assert cb.state == CircuitState.CLOSED
    
    cb._record_failure(status=503)
    cb._record_failure(status=503)
    cb._record_failure(status=503)
    
    assert cb.state == CircuitState.OPEN
    assert cb._consecutive_failures == 3

def test_success_resets_consecutive_counter():
    cb = CircuitBreaker("test", {"failure_count_threshold": 5})
    cb._record_failure(status=503)
    cb._record_failure(status=503)
    cb._record_success()
    cb._record_failure(status=503)
    
    assert cb._consecutive_failures == 1  # Only counts after last success
    assert cb.state == CircuitState.CLOSED

def test_auth_failure_immediate_open():
    cb = CircuitBreaker("test")
    cb._record_failure(status=401)
    assert cb.state == CircuitState.OPEN

def test_cooldown_elapsed_enters_half_open():
    cb = CircuitBreaker("test", {"base_cooldown_seconds": 0.1, "cooldown_jitter_max": 0})
    cb._force_open()  # simulate open
    assert cb.state == CircuitState.OPEN
    
    time.sleep(0.15)
    cb._maintenance_tick()  # simulated clock
    assert cb.state == CircuitState.HALF_OPEN

def test_half_open_probe_success_closes():
    cb = CircuitBreaker("test", {"probe_success_count": 1, "probe_min_requests": 1})
    cb._force_half_open()
    assert cb.state == CircuitState.HALF_OPEN
    
    cb._record_success()  # probe success
    assert cb.state == CircuitState.CLOSED

def test_half_open_probe_failure_backoff():
    cb = CircuitBreaker("test")
    cb._force_half_open()
    cb._record_failure(status=503)
    assert cb.state == CircuitState.HALF_OPEN_BACKOFF

def test_error_rate_threshold():
    cb = CircuitBreaker("test", {"error_rate_threshold": 0.5})
    # 6 failures out of 10 = 60% error rate
    for _ in range(6):
        cb._record_failure(status=503)
    for _ in range(4):
        cb._record_success()
    assert cb._sliding_error_rate() == pytest.approx(0.6)
    assert cb.state == CircuitState.OPEN

def test_global_circuit_overrides_local():
    global_cb = CircuitBreaker("global")
    local_cb = CircuitBreaker("public_discovery")
    
    global_cb._force_open()
    assert global_cb.state == CircuitState.OPEN
    
    with pytest.raises(CircuitOpenError):
        _check_global_circuit()  # hypothetical wrapper
```

---

## 10. Persistence and Restart Survival

### State Persistence

Circuit breaker state should survive process restarts:

```python
# Proposed schema addition to existing _ensure_alert_schema()

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    circuit_name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    consecutive_failures INTEGER DEFAULT 0,
    last_failure_time TEXT,
    state_changed_at TEXT,
    open_until TEXT,
    backoff_attempt INTEGER DEFAULT 0,
    rolling_errors_json TEXT DEFAULT '[]',  -- serialize deque
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Restart Recovery

On startup:
1. Read all circuit states from DB
2. If circuit was OPEN, check if cooldown has already expired:
   - If expired → transition to HALF_OPEN immediately
   - If not expired → remain OPEN with remaining cooldown
3. If circuit was HALF_OPEN → treat as OPEN (reset probe budget)
4. If circuit was CLOSED → CLOSED (normal)

### Startup Sequence Integration

In `core/kalshi_monitor.py`, after `_ensure_alert_schema()`:

```python
from core.circuit_breaker import restore_circuit_states

def initialize_circuit_breakers():
    restore_circuit_states()
    _LOGGER.info("circuit_breaker_initialized state_count=%d", _get_active_circuits())
```

---

## 11. Metrics and Observability

### Prometheus-style Metrics (for observability dashboard)

```
# HELP kalshi_circuit_breaker_state Current circuit breaker state (0=CLOSED, 1=OPEN, 2=HALF_OPEN, 3=BACKOFF, 4=BYPASS)
# TYPE kalshi_circuit_breaker_state gauge
kalshi_circuit_breaker_state{circuit="public_discovery"} 0
kalshi_circuit_breaker_state{circuit="public_hydration"} 1

# HELP kalshi_circuit_breaker_transitions_total Total circuit breaker state transitions
# TYPE kalshi_circuit_breaker_transitions_total counter
kalshi_circuit_breaker_transitions_total{circuit="public_discovery",from_state="CLOSED",to_state="OPEN"} 3

# HELP kalshi_circuit_breaker_fast_failures_total Requests rejected due to open circuit
# TYPE kalshi_circuit_breaker_fast_failures_total counter
kalshi_circuit_breaker_fast_failures_total{circuit="public_discovery"} 47

# HELP kalshi_circuit_breaker_open_duration_seconds How long the circuit has been open
# TYPE kalshi_circuit_breaker_open_duration_seconds gauge
kalshi_circuit_breaker_open_duration_seconds{circuit="public_discovery"} 123.4
```

### Observability Hook Integration

The existing observability layer (`core/observability.py`, `test_observability_*.py`) should be extended with:

```python
def get_circuit_breaker_status():
    """Return snapshot of all circuit breaker states for observability dashboard."""
    circuits = _get_all_circuits()
    return {
        name: {
            "state": cb.state.value,
            "open_seconds_remaining": cb.remaining_cooldown(),
            "consecutive_failures": cb._consecutive_failures,
            "rolling_error_rate": cb._sliding_error_rate(),
            "backoff_attempt": cb._backoff_attempt,
            "state_changed_at": datetime.fromtimestamp(cb._state_changed_at, tz=timezone.utc).isoformat(),
        }
        for name, cb in circuits.items()
    }
```

---

## 12. Edge Cases

### Edge Case 1: Circuit opens during HALF_OPEN probe
**Risk:** A probe request that causes a 401 (auth failure) while in HALF_OPEN should transition to OPEN, not HALF_OPEN_BACKOFF.  
**Solution:** `_record_failure()` checks: if failure is fatal (401/403), always transition to OPEN regardless of current state.

### Edge Case 2: Kalshi changes rate limit headers mid-session
**Risk:** `X-RateLimit-Remaining` drops to 0 without a 429.  
**Solution:** The circuit breaker does NOT depend on headers — it is error-driven. However, the rate limiter should track headers:

```python
def _update_rate_limit_from_headers(headers: dict):
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        try:
            if int(remaining) <= 2:  # Danger zone
                _LOGGER.warning("rate_limit_low remaining=%s", remaining)
        except (ValueError, TypeError):
            pass
```

### Edge Case 3: Cooldown timer expires while system is mid-maintenance
**Risk:** Maintenance (e.g., DB migration) delays the maintenance clock tick.  
**Solution:** Use `time.monotonic()` for all timer comparisons in the circuit breaker. The maintenance clock runs in a background thread and is independent of request-processing threads. If the maintenance tick is delayed, the worst case is a slightly delayed transition from OPEN to HALF_OPEN — no data corruption risk.

### Edge Case 4: All endpoints return errors simultaneously
**Risk:** Global outage (Kalshi down, network partition, DNS failure).
**Solution:** The global circuit (`circuit:global`) aggregates all endpoint-level circuits. If any 3 of 5 endpoint circuits open within a 5-minute window, the global circuit opens. When global is open, ALL polling stops and a CRITICAL alert fires.

### Edge Case 5: Circuit opens and never re-closes
**Risk:** Kalshi has an extended outage (hours/days). The system keeps cycling OPEN → HALF_OPEN → HALF_OPEN_BACKOFF indefinitely.
**Solution:** After 4 hours of sustained open state:
1. Escalate to CRITICAL severity
2. Suspend the 15-minute polling cycle entirely (not just the API calls)
3. Emit persistent notification (not just log line)
4. Require manual operator intervention to resume (BYPASS mode or config change)