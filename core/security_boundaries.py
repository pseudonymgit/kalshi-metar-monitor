import inspect
from types import MappingProxyType, ModuleType
from typing import Any, Dict, Mapping

# Determinism enforcement layer: this module codifies architectural law, not business logic.
# Responsibilities:
# - enforce which modules are allowed to mutate authoritative runtime state
# - enforce which module is allowed to emit transitions into the persisted history
# - enforce strict execution-vs-replay domain flags so side effects cannot leak into replay
# - enforce observability as read-only data exposure
# - enforce forbidden import edges to preserve layered ownership boundaries
# Enforcement role:
# - fail closed via SecurityBoundaryViolation whenever execution crosses a forbidden boundary
# - preserve replay equivalence by preventing mixed execution modes and hidden write paths
# This module MUST NOT allow:
# - cross-layer imports that let lower-trust layers reach orchestration or scoring internals
# - unauthorized transition emission or authoritative state mutation from arbitrary modules
# - mutable objects crossing into observability snapshots


class SecurityBoundaryViolation(RuntimeError):
    """Raised when deterministic authority boundaries are violated."""


_STATE_MUTATION_AUTHORIZED_CALLERS = frozenset({
    "core.authoritative_state",
    "core.metar_monitor",
})

_TRANSITION_EMIT_AUTHORIZED_CALLERS = frozenset({
    "core.metar_monitor",
})

_FORBIDDEN_IMPORTS_BY_MODULE = {
    "core.transition_emitter": frozenset({"core.observability", "core.scoring_engine", "core.replay_engine"}),
    "core.replay_engine": frozenset({"core.observability", "core.scoring_engine"}),
    "core.observability": frozenset({"core.metar_monitor", "core.transition_emitter"}),
}


def _caller_module_name(frame_offset: int = 1) -> str:
    frame = inspect.currentframe()
    for _ in range(frame_offset):
        if frame is None:
            return ""
        frame = frame.f_back
    if frame is None:
        return ""
    return str(frame.f_globals.get("__name__") or "")


def enforce_authoritative_state_mutation_boundary(operation: str) -> None:
    # State mutation authority is intentionally centralized so every persisted mutation follows
    # the same observation -> transition -> persistence doctrine, rather than ad-hoc writes.
    caller_module = _caller_module_name(frame_offset=3)
    if caller_module not in _STATE_MUTATION_AUTHORIZED_CALLERS:
        raise SecurityBoundaryViolation(
            f"authoritative_state mutation denied for '{operation}' from '{caller_module}'"
        )


def enforce_transition_emission_authority() -> None:
    # Transition records are the replay source of truth. Only the orchestrator may append them,
    # otherwise replay integrity is broken by out-of-band event creation.
    caller_module = _caller_module_name(frame_offset=3)
    if caller_module not in _TRANSITION_EMIT_AUTHORIZED_CALLERS:
        raise SecurityBoundaryViolation(
            f"transition emission denied from unauthorized module '{caller_module}'"
        )


def enforce_execution_domain_guard(*, allow_alert_delivery: bool, persist_cache: bool) -> str:
    # Runtime domain is binary: execution (side effects allowed) or replay (side effects denied).
    # Mixed flags would make a run neither fully replay-safe nor fully production-authoritative.
    if allow_alert_delivery and persist_cache:
        return "execution"
    if not allow_alert_delivery and not persist_cache:
        return "replay"
    raise SecurityBoundaryViolation(
        "illegal mixed-domain ingest flags: allow_alert_delivery and persist_cache must both be true or both be false"
    )


def enforce_replay_domain_isolation(*, allow_alert_delivery: bool, persist_cache: bool) -> None:
    # Replay must remain observationally pure: it can evaluate historical behavior but cannot
    # emit alerts or mutate live caches, preserving deterministic re-execution semantics.
    if allow_alert_delivery:
        raise SecurityBoundaryViolation("replay domain cannot enable alert delivery")
    if persist_cache:
        raise SecurityBoundaryViolation("replay domain cannot persist runtime cache")


def verify_observability_read_only(snapshot: Mapping[str, Any]) -> None:
    # Observability is a terminal read path in the architecture; this guard ensures snapshots
    # cannot become backchannels for state mutation across layer boundaries.
    if not isinstance(snapshot, Mapping):
        raise SecurityBoundaryViolation("public state snapshot must be a mapping")

    for key, value in snapshot.items():
        if isinstance(value, MappingProxyType):
            continue
        if isinstance(value, tuple):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            continue
        raise SecurityBoundaryViolation(
            f"public state field '{key}' is not read-only exposed"
        )


def detect_illegal_cross_layer_imports(*, module_name: str, module_globals: Dict[str, Any]) -> None:
    # Import graph enforcement prevents high-authority orchestration/evaluation modules from
    # becoming ambient dependencies of read-only or transition-only layers.
    # This preserves ownership boundaries across: observation -> transition -> persistence ->
    # evaluation -> alert, with each stage only depending on allowed predecessors.
    forbidden = _FORBIDDEN_IMPORTS_BY_MODULE.get(module_name)
    if not forbidden:
        return

    imported_modules = {
        value.__name__
        for value in module_globals.values()
        if isinstance(value, ModuleType)
    }
    violations = sorted(forbidden.intersection(imported_modules))
    if violations:
        joined = ", ".join(violations)
        raise SecurityBoundaryViolation(
            f"illegal cross-layer imports in '{module_name}': {joined}"
        )
