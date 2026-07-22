# CHANGELOG (last 10 broad changes):
# 1. [2026-03-04 Add architectural doctrine comments for determinism guards]
# 2. [2026-02-28 Add deterministic settlement epoch scoring foundation]
#


from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Determinism enforcement layer: scoring projection over immutable transition history.
# Responsibilities:
# - normalize transition ordering into a stable, replay-safe sequence
# - segment settlement epochs using settlement transitions as authoritative anchors
# - compute deterministic score projections from transition counts only
# - serialize score projections without introducing side effects or hidden state
# Enforcement role:
# - ensure evaluation remains a pure function of persisted transitions
# - preserve replay integrity by making scoring independent of runtime ordering artifacts
# This module MUST NOT allow:
# - direct mutation of authoritative state
# - transition creation, deletion, or reclassification during evaluation
# - dependence on non-deterministic external inputs

_SETTLEMENT_UP = "settlement_up"


@dataclass(frozen=True)
class SettlementEpoch:
    epoch_index: int
    start_transition_id: Optional[int]
    end_transition_id: Optional[int]
    settlement_transition_id: int
    station: str
    transition_ids: Tuple[int, ...]
    transition_type_counts: Tuple[Tuple[str, int], ...]


@dataclass(frozen=True)
class EpochScore:
    epoch_index: int
    station: str
    start_transition_id: Optional[int]
    end_transition_id: Optional[int]
    settlement_transition_id: int
    transition_count: int
    transition_type_counts: Tuple[Tuple[str, int], ...]
    score_total: int


def _event_id(event: Dict[str, Any], index: int) -> int:
    raw = event.get("id")
    if isinstance(raw, int):
        return raw
    return index


def _normalized_events(transition_history: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Replay integrity guard: sort by canonical id with source-index tie-breaker so projection is
    # stable even if source iterables arrive in non-deterministic container order.
    events = [dict(event) for event in transition_history]
    indexed = [(_event_id(event, index), index, event) for index, event in enumerate(events)]
    indexed.sort(key=lambda item: (item[0], item[1]))
    return [event for _, _, event in indexed]


def segment_settlement_epochs(transition_history: Iterable[Dict[str, Any]]) -> Tuple[SettlementEpoch, ...]:
    # Evaluation consumes persisted transitions in normalized order, enforcing architecture flow:
    # observation -> transition -> persistence -> evaluation -> alert.
    ordered_events = _normalized_events(transition_history)

    epochs: List[SettlementEpoch] = []
    current_events: List[Dict[str, Any]] = []
    current_settlement_event: Optional[Dict[str, Any]] = None

    for event in ordered_events:
        transition_type = str(event.get("transition_type") or "")
        if transition_type == _SETTLEMENT_UP:
            # Settlement transition is epoch authority boundary: each settlement starts a new
            # scoring window so projections remain anchored to explicit persisted events.
            if current_settlement_event is not None:
                epochs.append(_build_epoch(len(epochs), current_settlement_event, current_events))
            current_settlement_event = event
            current_events = [event]
            continue

        if current_settlement_event is not None:
            current_events.append(event)

    if current_settlement_event is not None:
        epochs.append(_build_epoch(len(epochs), current_settlement_event, current_events))

    return tuple(epochs)


def _build_epoch(epoch_index: int, settlement_event: Dict[str, Any], epoch_events: List[Dict[str, Any]]) -> SettlementEpoch:
    # Epoch metadata is derived only from transition payloads to keep scoring reproducible in
    # replay and prevent evaluator-owned state from influencing persisted interpretation.
    transition_ids = tuple(int(event["id"]) for event in epoch_events if isinstance(event.get("id"), int))
    start_transition_id = transition_ids[0] if transition_ids else None
    end_transition_id = transition_ids[-1] if transition_ids else None

    counts: Dict[str, int] = {}
    for event in epoch_events:
        event_type = str(event.get("transition_type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1

    settlement_transition_id = int(settlement_event["id"])
    return SettlementEpoch(
        epoch_index=epoch_index,
        start_transition_id=start_transition_id,
        end_transition_id=end_transition_id,
        settlement_transition_id=settlement_transition_id,
        station=str(settlement_event.get("station") or ""),
        transition_ids=transition_ids,
        transition_type_counts=tuple(sorted(counts.items())),
    )


def score_settlement_epochs(transition_history: Iterable[Dict[str, Any]]) -> Tuple[EpochScore, ...]:
    # Scoring is intentionally a projection layer: no writes, only deterministic arithmetic over
    # persisted transition categories.
    epochs = segment_settlement_epochs(transition_history)
    scores: List[EpochScore] = []

    for epoch in epochs:
        counts = dict(epoch.transition_type_counts)
        # Weights encode policy while remaining deterministic; identical transition history must
        # always yield identical score totals across execution and replay domains.
        score_total = (
            counts.get("settlement_up", 0) * 100
            + counts.get("instant_up", 0) * 10
            - counts.get("instant_down", 0) * 10
            - counts.get("reversion_after_settlement", 0) * 5
        )
        scores.append(
            EpochScore(
                epoch_index=epoch.epoch_index,
                station=epoch.station,
                start_transition_id=epoch.start_transition_id,
                end_transition_id=epoch.end_transition_id,
                settlement_transition_id=epoch.settlement_transition_id,
                transition_count=len(epoch.transition_ids),
                transition_type_counts=epoch.transition_type_counts,
                score_total=score_total,
            )
        )

    return tuple(scores)


def serialize_epoch_scores(scores: Iterable[EpochScore]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for score in scores:
        serialized.append(
            {
                "epoch_index": score.epoch_index,
                "station": score.station,
                "start_transition_id": score.start_transition_id,
                "end_transition_id": score.end_transition_id,
                "settlement_transition_id": score.settlement_transition_id,
                "transition_count": score.transition_count,
                "transition_type_counts": [
                    {"transition_type": transition_type, "count": count}
                    for transition_type, count in score.transition_type_counts
                ],
                "score_total": score.score_total,
            }
        )
    return serialized
