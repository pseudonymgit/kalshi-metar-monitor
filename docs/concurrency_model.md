# Weather Engine Concurrency Model

## Overview
This document describes the threading and locking model used throughout the Weather Engine codebase to prevent race conditions and ensure thread safety.

## Lock Inventory

### Core Module Locks

#### `p3_scheduler.py`
- **`_cache_lock`**: Protects the prediction result cache (`_cache` dict)
  - Used for: Cache read/write operations
  - Scope: Global module-level cache

#### `db_health_monitor.py`
- **`_last_health_check_lock`**: Protects last health check timestamp
  - Used for: Health check timing coordination
  - Scope: Module-level timing state

#### `market_monitor.py`
- **`_LADDER_LOCK`**: Protects ladder state information
- **`_SERIES_LOCK`**: Protects series information
- **`_PROXIMITY_LOCK`**: Protects proximity detection state
  - Used for: Market monitoring state management
  - Scope: Module-level market state

#### `near_miss_audit.py`
- **`_AUDIT_LOCK`**: Protects audit state
  - Used for: Near-miss audit operations
  - Scope: Module-level audit state

#### `order_manager.py`
- **`_LADDER_LOCK`**: Protects ladder state
- **`_SERIES_LOCK`**: Protects series information
- **`_PROXIMITY_LOCK`**: Protects proximity detection
  - Used for: Order management state
  - Scope: Module-level order state

#### `alert_retry_queue.py`
- **`_RETRY_LOCK`**: Protects retry queue operations
  - Used for: Alert retry coordination
  - Scope: Module-level retry state

#### `operation_state.py`
- **`self._lock`**: Instance-specific lock for operation state
  - Used for: Individual operation state management
  - Scope: Per-instance state

#### `kalshi_monitor.py`
- **`_LADDER_LOCK`**: Protects ladder state
- **`_SERIES_LOCK`**: Protects series information
- **`_PROXIMITY_LOCK`**: Protects proximity detection
  - Used for: Kalshi monitoring state
  - Scope: Module-level monitoring state

#### `authoritative_state.py`
- **`_STATE_LOCK`**: Global state lock
- **`state_lock()`**: Function returning global state lock
  - Used for: Authoritative state management
  - Scope: Global application state

#### `health_monitor.py`
- **`_AUDIT_LOCK`**: Protects health audit operations
  - Used for: Health monitoring coordination
  - Scope: Module-level health state

#### `heartbeat_monitor.py`
- **`_heartbeat_lock`**: Protects heartbeat operations
  - Used for: Heartbeat coordination
  - Scope: Module-level heartbeat state

#### `kalshi_price_fetcher.py`
- **`_PRICE_CACHE_LOCK`**: Protects price cache
  - Used for: Price caching operations
  - Scope: Module-level price cache

#### `data_processor.py`
- **`_SCHEDULER_LOCK`**: Protects scheduler operations
- **`_MISSING_LADDER_LOCK`**: Protects missing ladder detection
- **`_KALSHI_RATE_LIMIT_LOCK`**: Protects rate limiting state
  - Used for: Data processing coordination
  - Scope: Module-level processing state

#### `alert_reconciliation.py`
- **`self._lock`**: Instance-specific lock for alert reconciliation
  - Used for: Alert reconciliation operations
  - Scope: Per-instance reconciliation state

#### `api_circuit_breaker.py`
- **`self._lock`**: Instance-specific lock for circuit breaker
  - Used for: Circuit breaker state management
  - Scope: Per-instance circuit state

#### `metar_monitor.py`
- **`_SCHEDULER_LOCK`**: Protects scheduler operations
- **`_AUDIT_LOCK`**: Protects audit operations
- **`_MISSING_LADDER_LOCK`**: Protects missing ladder detection
- **`_KALSHI_RATE_LIMIT_LOCK`**: Protects rate limiting
- **`_TRANSITION_LOCK`**: Protects state transitions
  - Used for: METAR monitoring coordination
  - Scope: Module-level monitoring state

#### `price_fetcher.py`
- **`_LADDER_LOCK`**: Protects ladder state
- **`_SERIES_LOCK`**: Protects series information
- **`_PROXIMITY_LOCK`**: Protects proximity detection
  - Used for: Price fetching coordination
  - Scope: Module-level fetching state

#### `structured_logger.py`
- **`_root_lock`**: Protects root logger operations
  - Used for: Logger initialization coordination
  - Scope: Module-level logger state

## Lock Hierarchy and Potential Contention

### High Contention Areas
1. **Market Monitoring Locks** (`_LADDER_LOCK`, `_SERIES_LOCK`, `_PROXIMITY_LOCK`)
   - Used across multiple modules: `market_monitor.py`, `order_manager.py`, `kalshi_monitor.py`, `price_fetcher.py`
   - Potential for cross-module contention if accessed concurrently

2. **State Management Locks**
   - `authoritative_state.py` has global state lock
   - `operation_state.py` has per-instance locks
   - Generally lower contention due to localized scope

3. **Cache Locks**
   - `p3_scheduler.py` cache lock for prediction results
   - `kalshi_price_fetcher.py` cache lock for price data
   - Moderate contention during high-frequency operations

### Lock Ordering Guidelines
When acquiring multiple locks, follow this order:
1. Global state locks (`authoritative_state.py`)
2. Module-level coordination locks
3. Cache locks
4. Instance-specific locks

Avoid acquiring locks from different modules in nested fashion unless absolutely necessary.

## Best Practices

1. **Keep Lock Scope Minimal**: Acquire locks for the shortest duration possible
2. **Avoid Nested Locks**: Prefer single-lock operations where possible
3. **Use Context Managers**: Always use `with lock:` pattern for thread safety
4. **Document Lock Dependencies**: When locks interact across modules, document the relationships
5. **Monitor for Deadlocks**: Use logging and monitoring to detect potential deadlock scenarios

## Performance Considerations
- Most locks are module-specific and have low contention
- Market monitoring locks may experience higher contention during active trading
- Cache locks should be fast (in-memory operations)
- Consider using `threading.RLock()` for recursive locking patterns if needed