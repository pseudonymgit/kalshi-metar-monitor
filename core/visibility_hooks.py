# CHANGELOG (last 10 broad changes):
# 1. [2026-03-02 Harden visibility dispatcher hot-path safety]
#


from dataclasses import dataclass
from typing import Any, Callable, Dict, List


APPROVED_BOUNDARY_TYPES = frozenset(
    {
        "post-ingestion-decision",
        "post-transition-evaluation",
        "post-hydration-evaluation",
        "scheduler-loop-completion",
        "replay-completion",
    }
)


@dataclass(frozen=True)
class visibility_event_envelope:
    station_identifier: str
    day_session_key: str
    event_sequence_ordinal: int
    decision_classification: str
    finalized_outcome: Dict[str, Any]
    emission_outcome_marker: str
    boundary_type: str
    execution_mode: str
    run_correlation_marker: str


_VISIBILITY_HOOKS: Dict[str, List[Callable[[visibility_event_envelope], None]]] = {
    boundary_type: [] for boundary_type in APPROVED_BOUNDARY_TYPES
}
_DISPATCHER_FROZEN = False


def _assert_approved_boundary_type(boundary_type: str) -> None:
    if boundary_type not in APPROVED_BOUNDARY_TYPES:
        raise ValueError(f"Unapproved visibility boundary type: {boundary_type}")


def register_visibility_hook(
    boundary_type: str, handler: Callable[[visibility_event_envelope], None]
) -> None:
    if _DISPATCHER_FROZEN:
        return

    _assert_approved_boundary_type(boundary_type)
    _VISIBILITY_HOOKS[boundary_type].append(handler)


def emit_visibility_event(boundary_type: str, envelope: visibility_event_envelope) -> None:
    global _DISPATCHER_FROZEN

    try:
        if boundary_type not in APPROVED_BOUNDARY_TYPES:
            return

        _DISPATCHER_FROZEN = True
        handlers = tuple(_VISIBILITY_HOOKS.get(boundary_type, ()))
    except Exception:
        return

    if not handlers:
        return

    for handler in handlers:
        try:
            handler(envelope)
        except Exception:
            continue
