"""
Transition Emitter

This module is the sole routing authority for transition emission events.

Responsibilities
- Enforce transition emission authority boundaries.
- Route validated transition changes to the persistence callback.
- Propagate transition identity into settlement epoch logging.

This module MUST NOT
- Evaluate market eligibility.
- Deliver alerts.
- Mutate authoritative temperature state.
"""

from typing import Any, Callable, Dict, Optional

from core.settlement_epoch_logger import log_transition_for_settlement_epoch
from core.security_boundaries import (
    detect_illegal_cross_layer_imports,
    enforce_transition_emission_authority,
)


detect_illegal_cross_layer_imports(module_name=__name__, module_globals=globals())


def emit_transition_if_changed(
    *,
    transition_type: Optional[str],
    instant_changed: bool,
    settlement_changed: bool,
    station: str,
    instant_bucket_before: Optional[int],
    instant_bucket_after: int,
    settlement_bucket: int,
    running_max: float,
    current_temp: float,
    metadata: Dict[str, Any],
    emit_fn: Callable[..., Any],
) -> Optional[Any]:
    """
    Single authority for transition emission routing.
    No state mutation is allowed in this layer.
    """
    # Architectural boundary: every transition event must pass this guard so
    # emission remains deterministic and centralized in one authority.
    enforce_transition_emission_authority()

    if not transition_type:
        return None
    if not (instant_changed or settlement_changed):
        return None

    # Causal flow: observation-derived transition -> persisted transition event.
    transition_correlation = emit_fn(
        station=station,
        transition_type=transition_type,
        instant_bucket_before=instant_bucket_before,
        instant_bucket_after=instant_bucket_after,
        settlement_bucket=settlement_bucket,
        running_max=running_max,
        current_temp=current_temp,
        metadata=metadata,
    )

    transition_event_id = None
    event_timestamp_utc = None
    if isinstance(transition_correlation, dict):
        raw_id = transition_correlation.get("transition_event_id")
        if isinstance(raw_id, int):
            transition_event_id = raw_id
        raw_timestamp = transition_correlation.get("timestamp_utc")
        if raw_timestamp is not None:
            event_timestamp_utc = str(raw_timestamp)

    # Causal flow continuation: emitted transition -> settlement epoch tracker.
    log_transition_for_settlement_epoch(
        station=station,
        transition_type=transition_type,
        settlement_bucket=settlement_bucket,
        current_temp=current_temp,
        metadata=metadata,
        transition_event_id=transition_event_id,
        event_timestamp_utc=event_timestamp_utc,
    )

    return transition_correlation
