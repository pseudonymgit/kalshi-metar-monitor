"""
FP-API-CIRCUIT-BREAKER: 5-State Circuit Breaker for External API Calls

5-state machine with per-group (per-API) + global parent circuit.

States:
    CLOSED      Normal operation, requests pass through
    OPEN        Failing fast, requests blocked. Auto-transitions to HALF_OPEN
                after exponential backoff timeout.
    HALF_OPEN   Probation: let 1 request through to test recovery
    DISABLED    Manual override, always pass through
    FORCED_OPEN Manual override, always fail fast

3-tier error classification:
    TRANSIENT       Retryable (timeout, 429, 503)
    NON_TRANSIENT   Not retryable (400, 404, 401)
    UNKNOWN         Unclassified

Per-group circuit + global parent:
    Parent circuit trips slower than children. If parent opens,
    ALL child circuits are affected.

Target: All external API calls (Kalshi markets, NOAA/NWS, ERA5)
"""

import time
import logging
import threading
from enum import Enum
from typing import Dict, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """5-state circuit breaker state machine."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    DISABLED = "DISABLED"
    FORCED_OPEN = "FORCED_OPEN"


class ErrorTier(Enum):
    """3-tier error classification."""
    TRANSIENT = "TRANSIENT"
    NON_TRANSIENT = "NON_TRANSIENT"
    UNKNOWN = "UNKNOWN"


@dataclass
class CircuitConfig:
    """Per-circuit configuration."""
    name: str
    failure_threshold: int = 3
    success_threshold: int = 2
    reset_timeout_base: float = 1.0
    reset_timeout_max: float = 300.0
    max_retries: int = 3
    retry_backoff: float = 1.0


@dataclass
class CircuitStateData:
    """Mutable state for a circuit."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    last_open_time: float = 0.0
    open_count: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    consecutive_transient_failures: int = 0


class CircuitBreaker:
    """
    Per-API-group circuit breaker with 5-state machine.

    Usage:
        cb = CircuitBreaker(CircuitConfig(name="kalshi_api"))
        success, result = cb.call(kalshi_api_function, arg1, arg2)
    """

    def __init__(self, config: CircuitConfig):
        self.config = config
        self.state_data = CircuitStateData()
        self._lock = threading.Lock()
        self._parent: Optional['CircuitBreakerGroup'] = None

    def set_parent(self, parent: 'CircuitBreakerGroup') -> None:
        """Set the parent group circuit for coordinated tripping."""
        self._parent = parent

    @property
    def is_available(self) -> bool:
        """Whether this circuit allows requests through."""
        with self._lock:
            if self.state_data.state == CircuitState.DISABLED:
                return True
            if self.state_data.state == CircuitState.FORCED_OPEN:
                return False
            if self.state_data.state == CircuitState.CLOSED:
                return True
            if self.state_data.state == CircuitState.HALF_OPEN:
                return True
            if time.time() - self.state_data.last_open_time >= self._current_timeout():
                return True
            return False

    def _current_timeout(self) -> float:
        """Calculate exponential backoff timeout."""
        n = self.state_data.open_count
        timeout = min(
            self.config.reset_timeout_base * (self.config.retry_backoff ** n),
            self.config.reset_timeout_max
        )
        return timeout

    def call(self, func: Callable, *args, **kwargs) -> Tuple[bool, Any]:
        """
        Execute a function through the circuit breaker.

        Returns:
            Tuple of (success: bool, result/error: Any)
        """
        if self._parent and not self._parent.is_available:
            self._record_failure()
            return False, "Parent circuit breaker is open"

        if not self.is_available:
            self._record_failure()
            return False, f"Circuit breaker '{self.config.name}' is OPEN (timeout={self._current_timeout():.1f}s)"

        last_error = None
        for attempt in range(self.config.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                self._record_success()
                return True, result
            except Exception as e:
                last_error = e
                tier = self._classify_error(e)

                if tier == ErrorTier.TRANSIENT and attempt < self.config.max_retries:
                    wait = self.config.retry_backoff ** attempt
                    logger.warning(
                        "Circuit '%s' transient error (attempt %d/%d): %s. Retrying in %.1fs",
                        self.config.name, attempt + 1, self.config.max_retries + 1, e, wait
                    )
                    time.sleep(wait)
                else:
                    break

        self._record_failure()
        return False, last_error

    def _record_success(self) -> None:
        """Record a successful call and update state."""
        with self._lock:
            self.state_data.total_calls += 1
            self.state_data.total_successes += 1
            self.state_data.last_success_time = time.time()
            self.state_data.failure_count = 0
            self.state_data.consecutive_transient_failures = 0

            if self.state_data.state == CircuitState.HALF_OPEN:
                self.state_data.success_count += 1
                if self.state_data.success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self.state_data.state == CircuitState.OPEN:
                self._transition_to(CircuitState.HALF_OPEN)
                self.state_data.success_count = 1

    def _record_failure(self) -> None:
        """Record a failed call and update state."""
        with self._lock:
            self.state_data.total_calls += 1
            self.state_data.total_failures += 1
            self.state_data.last_failure_time = time.time()
            self.state_data.failure_count += 1

            if self.state_data.failure_count >= self.config.failure_threshold:
                if self.state_data.state == CircuitState.CLOSED:
                    self._transition_to(CircuitState.OPEN)
                elif self.state_data.state == CircuitState.HALF_OPEN:
                    self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Handle state transitions with logging."""
        old_state = self.state_data.state
        self.state_data.state = new_state

        if new_state == CircuitState.OPEN:
            self.state_data.last_open_time = time.time()
            self.state_data.open_count += 1
            self.state_data.success_count = 0
            timeout = self._current_timeout()
            logger.warning(
                "Circuit '%s' transitioned: %s -> OPEN. Failures: %d, Open count: %d, Timeout: %.1fs",
                self.config.name, old_state.value,
                self.state_data.failure_count, self.state_data.open_count, timeout
            )
        elif new_state == CircuitState.CLOSED:
            self.state_data.failure_count = 0
            self.state_data.success_count = 0
            logger.info(
                "Circuit '%s' transitioned: %s -> CLOSED. Total successes: %d",
                self.config.name, old_state.value, self.state_data.total_successes
            )
        elif new_state == CircuitState.HALF_OPEN:
            self.state_data.success_count = 0
            logger.info(
                "Circuit '%s' transitioned: %s -> HALF_OPEN",
                self.config.name, old_state.value
            )

    def _classify_error(self, error: Exception) -> ErrorTier:
        """Classify an exception into an error tier."""
        import requests

        if isinstance(error, requests.exceptions.Timeout):
            return ErrorTier.TRANSIENT
        if isinstance(error, requests.exceptions.ConnectionError):
            return ErrorTier.TRANSIENT
        if hasattr(error, 'response'):
            status = getattr(error.response, 'status_code', 0)
            if status in (429, 503, 502, 504):
                return ErrorTier.TRANSIENT
            if status in (400, 401, 403, 404, 405, 410):
                return ErrorTier.NON_TRANSIENT
        if isinstance(error, ValueError):
            return ErrorTier.NON_TRANSIENT
        return ErrorTier.UNKNOWN

    def force_state(self, state: CircuitState) -> None:
        """Manually force a circuit state (for DISABLED/FORCED_OPEN)."""
        with self._lock:
            self.state_data.state = state
            logger.warning("Circuit '%s' manually forced to %s", self.config.name, state.value)

    def get_status(self) -> dict:
        """Return current status for monitoring."""
        return {
            'name': self.config.name,
            'state': self.state_data.state.value,
            'failure_count': self.state_data.failure_count,
            'success_count': self.state_data.success_count,
            'open_count': self.state_data.open_count,
            'total_calls': self.state_data.total_calls,
            'total_failures': self.state_data.total_failures,
            'total_successes': self.state_data.total_successes,
            'is_available': self.is_available,
            'timeout_seconds': round(self._current_timeout(), 1),
        }


class CircuitBreakerGroup:
    """
    Group of circuit breakers with a parent circuit for coordinated tripping.

    If the parent circuit opens, ALL child circuits are affected.

    Usage:
        group = CircuitBreakerGroup("kalshi")
        markets_cb = group.add_circuit("markets", failure_threshold=3)
        success, result = markets_cb.call(my_api_function)
    """

    def __init__(self, name: str, config: Optional[CircuitConfig] = None):
        self.name = name
        self.parent = CircuitBreaker(
            config or CircuitConfig(
                name=f"{name}_parent",
                failure_threshold=10,
                success_threshold=3,
                reset_timeout_base=5.0,
                reset_timeout_max=600.0,
            )
        )
        self._circuits: Dict[str, CircuitBreaker] = {}

    def add_circuit(self, name: str, **overrides) -> CircuitBreaker:
        """Add a child circuit breaker to this group."""
        base_config = CircuitConfig(name=f"{self.name}.{name}", **overrides)
        cb = CircuitBreaker(base_config)
        cb.set_parent(self.parent)
        self._circuits[name] = cb
        return cb

    def get_circuit(self, name: str) -> Optional[CircuitBreaker]:
        """Get a child circuit by name."""
        return self._circuits.get(name)

    @property
    def is_available(self) -> bool:
        """Whether the parent circuit allows requests through."""
        return self.parent.is_available

    def all_status(self) -> dict:
        """Return status of parent and all children for monitoring."""
        return {
            'group': self.name,
            'parent': self.parent.get_status(),
            'children': {
                name: cb.get_status()
                for name, cb in self._circuits.items()
            }
        }

    def force_parent_state(self, state: CircuitState) -> None:
        """Manually force the parent circuit state."""
        self.parent.force_state(state)


# Module-level default circuits
_DEFAULT_CIRCUITS: Dict[str, CircuitBreakerGroup] = {}


def _ensure_circuits():
    """Initialize default circuit groups lazily."""
    if not _DEFAULT_CIRCUITS:
        kalshi_group = CircuitBreakerGroup("kalshi")
        kalshi_group.add_circuit("market_price", failure_threshold=3)
        kalshi_group.add_circuit("settlements", failure_threshold=5)
        kalshi_group.add_circuit("orderbook", failure_threshold=3)
        _DEFAULT_CIRCUITS["kalshi"] = kalshi_group

        weather_group = CircuitBreakerGroup("weather_api")
        weather_group.add_circuit("noaa_nws", failure_threshold=3)
        weather_group.add_circuit("era5", failure_threshold=3)
        weather_group.add_circuit("openweather", failure_threshold=3)
        _DEFAULT_CIRCUITS["weather_api"] = weather_group

        internal_group = CircuitBreakerGroup("internal")
        internal_group.add_circuit("backtest", failure_threshold=5)
        internal_group.add_circuit("sweep", failure_threshold=5)
        _DEFAULT_CIRCUITS["internal"] = internal_group


def get_circuit_group(name: str) -> CircuitBreakerGroup:
    """Get or create a named circuit group."""
    _ensure_circuits()
    if name not in _DEFAULT_CIRCUITS:
        _DEFAULT_CIRCUITS[name] = CircuitBreakerGroup(name)
    return _DEFAULT_CIRCUITS[name]


def get_circuit(group_name: str, circuit_name: str) -> Optional[CircuitBreaker]:
    """Get a specific circuit from a group."""
    group = get_circuit_group(group_name)
    return group.get_circuit(circuit_name)


def circuit_call(group_name: str, circuit_name: str,
                 func: Callable, *args, **kwargs) -> Tuple[bool, Any]:
    """
    Convenience: execute a function through a named circuit.

    Usage:
        success, result = circuit_call("kalshi", "market_price",
                                       kalshi_api.get_price, "KDEN")
    """
    cb = get_circuit(group_name, circuit_name)
    if cb is None:
        try:
            return True, func(*args, **kwargs)
        except Exception as e:
            return False, e
    return cb.call(func, *args, **kwargs)


def get_all_circuit_status() -> dict:
    """Get status of all registered circuits for monitoring dashboard."""
    _ensure_circuits()
    return {
        name: group.all_status()
        for name, group in _DEFAULT_CIRCUITS.items()
    }


def kalshi_price_with_circuit(station: str, market_type: str = "HIGH",
                                date_str: str = None):
    """
    Get Kalshi market price through circuit breaker.
    Falls back to (0.5, fallback_meta) if circuit is open or API fails.
    """
    from core.kalshi_price_fetcher import get_live_market_price

    def _fetch():
        return get_live_market_price(station, market_type, date_str)

    success, result = circuit_call("kalshi", "market_price", _fetch)
    if success:
        return result
    logger.warning("kalshi_price_fallback station=%s market=%s error=%s",
                   station, market_type, result)
    return (0.5, {"source": "circuit_breaker_fallback", "station": station})


def circuit_breaker_health() -> dict:
    """Return health status for monitoring dashboards."""
    status = get_all_circuit_status()
    healthy = True
    for group_name, group_data in status.items():
        if group_data.get('parent', {}).get('state') == 'OPEN':
            healthy = False
        for child_name, child_data in group_data.get('children', {}).items():
            if child_data.get('state') == 'OPEN':
                healthy = False
    return {
        'healthy': healthy,
        'circuits': status,
        'timestamp': time.time(),
    }