from typing import Any, Callable, Dict, List

from core.security_boundaries import (
    detect_illegal_cross_layer_imports,
    enforce_replay_domain_isolation,
)


detect_illegal_cross_layer_imports(module_name=__name__, module_globals=globals())


def execute_ordered_replay_stream(
    *,
    station: str,
    ordered_observations: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    ingest_fn: Callable[..., Any],
) -> Dict[str, Any]:
    """
    Replay entrypoint that routes historical observations through the
    same ingestion execution path used by production runtime.

    The input list must already be ordered in replay-authoritative order.
    """
    station = (station or "").strip().upper()
    enforce_replay_domain_isolation(allow_alert_delivery=False, persist_cache=False)

    observations_processed, _ = ingest_fn(
        station,
        ordered_observations,
        cfg,
        allow_alert_delivery=False,
        persist_cache=False,
    )
    return {
        "station": station,
        "observations_processed": int(observations_processed),
        "status": "completed",
    }

