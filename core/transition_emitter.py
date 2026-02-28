from typing import Any, Callable, Dict, Optional


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
    emit_fn: Callable[..., None],
) -> None:
    """
    Single authority for transition emission routing.
    No state mutation is allowed in this layer.
    """
    if not transition_type:
        return
    if not (instant_changed or settlement_changed):
        return

    emit_fn(
        station=station,
        transition_type=transition_type,
        instant_bucket_before=instant_bucket_before,
        instant_bucket_after=instant_bucket_after,
        settlement_bucket=settlement_bucket,
        running_max=running_max,
        current_temp=current_temp,
        metadata=metadata,
    )
