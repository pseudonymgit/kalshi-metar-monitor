#!/usr/bin/env python3
"""
subhourly_scoring.py — Scoring bridge for the sub-hourly trajectory nowcast.

The `SubhourlyTrajectorySignal` operates on a sub-hourly axis and exposes
`evaluate_nowcast(...)` / `iter_nowcast_events()`, NOT the daily
`evaluate(idx, days)` interface the core/additive evaluators use. So it cannot
flow through those harnesses. This module is the parallel path: it runs the
signal for a trading date and converts its per-event output into
`AdditiveSignalResult` rows so the same `additive_signal_results` store (and the
same downstream calibration) sees it.

Scoring-only. Nothing here returns a trading instruction; the caller must not
feed these rows into fusion/entry. (Dan-gated from live/paper, like the gamma
signals whose ENABLED_BY_DEFAULT is empty.)

Two keys are emitted per (station, date), both carrying the `subhourly_trajectory`
prefix (MOD-4):

  subhourly_trajectory              — the signal's directional output for the
                                      day (cross OR no-cross), slope-based
                                      confidence. This is "what the predictor
                                      produces" on every evaluation, not just
                                      the rare OOS cell.
  subhourly_trajectory_anticipatory — the OOS-validated anticipatory cell only
                                      (gap∈[1,3) AND slope≥0.05 → cross),
                                      flagged so the validated predictor is
                                      visible separately.

Deterministic only — arithmetic, no AI/ML, no lookahead.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional

from .base_signal import BaseSignal  # noqa: F401 (documents the interface)
from .subhourly_trajectory_signal import SubhourlyTrajectorySignal

logger = logging.getLogger(__name__)

SIGNAL_KEY = "subhourly_trajectory"
ANTICIPATORY_KEY = "subhourly_trajectory_anticipatory"


def _rows_for_station(station: str, date_str: str, events: List[dict]):
    """Aggregate one station-day's nowcast events into two AdditiveSignalResult rows."""
    from core.additive.base import AdditiveSignalResult

    n_events = len(events)
    fired = [e for e in events if e.get("prediction") is not None]
    n_cross = sum(1 for e in events if e.get("prediction") == "cross")
    n_nocross = sum(1 for e in events if e.get("prediction") == "no-cross")
    n_abstain = sum(1 for e in events if e.get("prediction") is None)

    # Settlement label for the day (any event carries it; label-only, never a feature).
    outcome = None
    strike = settle = None
    for e in events:
        if e.get("outcome") is not None:
            outcome = e["outcome"]
            strike, settle = e.get("strike"), e.get("settle")
            break

    # Signal #1: the directional output (cross/no-cross).
    if n_events == 0:
        row_main = AdditiveSignalResult(
            signal=SIGNAL_KEY, offset_f=0.0, confidence=0.0, fired=False,
            reason="traj:no_events",
            meta={"station": station, "date": date_str, "outcome": outcome})
    else:
        max_conf = max((e.get("confidence") or 0.0) for e in fired) if fired else 0.0
        dominant = "cross" if n_cross >= n_nocross else "no-cross"
        row_main = AdditiveSignalResult(
            signal=SIGNAL_KEY, offset_f=0.0, confidence=float(max_conf),
            fired=bool(fired),
            reason=f"traj:{dominant}" if fired else "traj:abstain",
            meta={"station": station, "date": date_str,
                  "n_events": n_events, "n_cross": n_cross,
                  "n_nocross": n_nocross, "n_abstain": n_abstain,
                  "outcome": outcome, "strike": strike, "settle": settle})

    # Signal #2: the OOS-validated anticipatory cell.
    row_cell = AdditiveSignalResult(
        signal=ANTICIPATORY_KEY, offset_f=0.0,
        confidence=float(max((e.get("confidence") or 0.0) for e in events
                             if e.get("prediction") == "cross") if n_cross else 0.0),
        fired=bool(n_cross),
        reason="traj:anticipatory_cell" if n_cross else "traj:no_cell",
        meta={"station": station, "date": date_str, "n_cell": n_cross,
              "outcome": outcome, "strike": strike, "settle": settle})

    return [row_main, row_cell]


def score_day(sig: SubhourlyTrajectorySignal, date_str: str,
              stations: Optional[List[str]] = None) -> Dict[str, List]:
    """Score every station for one trading date.

    Returns {station: [AdditiveSignalResult, ...]} — feed this straight to
    core.additive.persistence.persist_results_to_db.
    """
    events = list(sig.iter_nowcast_events(date_filter=lambda d: d == date_str))
    by_station: Dict[str, List[dict]] = defaultdict(list)
    for e in events:
        by_station[e["station"]].append(e)

    if stations is None:
        stations = sorted(by_station.keys())

    out: Dict[str, List] = {}
    for st in stations:
        out[st] = _rows_for_station(st, date_str, by_station.get(st, []))
    return out


def latest_scorable_date(sig: SubhourlyTrajectorySignal) -> Optional[str]:
    """Newest trading date that has a strike+truth row the signal can score."""
    rows = sig._load_strike_days()
    dates = [r["local_trading_date"] for r in rows if r.get("local_trading_date")]
    return max(dates) if dates else None


def pick_date(sig: SubhourlyTrajectorySignal, preferred: str) -> str:
    """Use `preferred` if it has scorable events; else the latest scorable date."""
    ev = list(sig.iter_nowcast_events(date_filter=lambda d: d == preferred))
    if ev:
        return preferred
    fallback = latest_scorable_date(sig)
    if fallback and fallback != preferred:
        logger.info(
            "Sub-hourly trajectory: no events for %s; scoring latest labeled date %s",
            preferred, fallback)
        return fallback or preferred
    return preferred
