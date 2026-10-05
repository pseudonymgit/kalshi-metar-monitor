#!/usr/bin/env python3
"""
gamma_scoring.py — Scoring bridge for the Gamma G1-G6 signals (E4, 2026-10-05)
=============================================================================
The Gamma signals (`core/signals/gamma_signals.py`) implement the daily
`evaluate(idx, days) -> (direction, confidence)` interface and are registered in
`core/signals/__init__.py`'s `SignalRegistry`. They were NEVER evaluated in the
live daemon path: the intraday evaluator (`core/intraday/signal_evaluator.py`)
runs a *different* registry (`_INTRADAY_SIGNALS`) of METAR-vector detectors, and
`ENABLED_BY_DEFAULT` (core/additive) gates a different family entirely. So the
gamma rows never reached `additive_signal_results`.

This module is the scoring-only bridge: it evaluates G1-G6 over a station's
daily series and converts each into `AdditiveSignalResult` rows under the gamma
signal names, so the existing M1 store (`data/signal_cache.db`,
`additive_signal_results`) sees them and downstream calibration scores them.

SCORING-ONLY (Dan-gated): nothing here returns a trading instruction; the caller
must NOT feed these rows into fusion / entry / SBOX. Deterministic, no AI/ML.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

GAMMA_KEYS: List[str] = [
    "gamma_g1_uncertainty",
    "gamma_g2_regime",
    "gamma_g3_analog",
    "gamma_g4_pressure_adv",
    "gamma_g5_dewpoint",
    "gamma_g6_cloud_cover",
]


def load_days(db_path: str, station: str, limit: int = 400) -> List[dict]:
    """Daily METAR aggregates shaped for the `evaluate(idx, days)` interface.

    Includes the `station` key (G1 reads `days[idx]["station"]` for its GEFS
    lookup; the sweep's days builder omits it, which would silently no-op G1).
    """
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT date_utc, MAX(temp_f) AS high, MIN(temp_f) AS low, "
            "AVG(dewpoint_f) AS dewpoint, AVG(temp_f) AS temp, "
            "AVG(pressure_mb) AS pressure, AVG(wind_speed_kt) AS wind_speed, "
            "AVG(wind_direction_deg) AS wind_dir "
            "FROM metar_observations "
            "WHERE station=? AND temp_f IS NOT NULL AND temp_f < 200 "
            "GROUP BY date_utc ORDER BY date_utc ASC",
            (station,)).fetchall()
    finally:
        c.close()
    days = [{"station": station, "date": r[0], "high": r[1], "low": r[2],
             "dewpoint": r[3], "temp": r[4], "pressure": r[5],
             "wind_speed": r[6], "wind_dir": r[7]} for r in rows]
    return days[-limit:] if limit else days


def _target_index(days: List[dict], target_date: Optional[str]) -> Optional[int]:
    """Index of `target_date` in `days`, else the last (most recent) day."""
    if not days:
        return None
    if target_date:
        for i, d in enumerate(days):
            if d.get("date") == target_date:
                return i
    return len(days) - 1


def score_station(gamma_signals, station: str, days: List[dict],
                  target_date: Optional[str] = None) -> List:
    """Run every gamma signal at the target day; return AdditiveSignalResult rows."""
    from core.additive.base import AdditiveSignalResult

    idx = _target_index(days, target_date)
    if idx is None or idx < 1:
        return []
    date_str = days[idx].get("date")

    out = []
    for sig in gamma_signals:
        try:
            direction, confidence = sig.evaluate(idx, days)
        except Exception as e:  # a broken signal must not kill the pass
            logger.warning(f"gamma {getattr(sig, 'name', sig)} failed for {station}: {e}")
            direction, confidence = None, 0.0
        conf = float(confidence or 0.0)
        fired = direction is not None and conf > 0.0
        out.append(AdditiveSignalResult(
            signal=getattr(sig, "name", str(sig)),
            offset_f=0.0,
            confidence=conf,
            fired=bool(fired),
            reason=(f"gamma:{direction}" if direction is not None else "gamma:no_fire"),
            meta={"station": station, "date": date_str, "direction": direction,
                  "source": "gamma_scoring"},
        ))
    return out


def score_all_stations(gamma_signals, db_path: str, stations: List[str],
                       target_date: Optional[str] = None) -> Dict[tuple, List]:
    """{ (station, date): [rows...] } for every station with a scorable series."""
    out: Dict[tuple, List] = {}
    for st in stations:
        days = load_days(db_path, st)
        if len(days) < 2:
            continue
        rows = score_station(gamma_signals, st, days, target_date=target_date)
        if rows:
            out[(st, days[_target_index(days, target_date)]["date"])] = rows
    return out


if __name__ == "__main__":  # manual smoke: python3 -m core.signals.gamma_scoring
    import sys
    from core.signals.gamma_signals import get_gamma_signals
    from core.additive.persistence import persist_results_to_db
    repo = Path(__file__).resolve().parents[2]
    stations = ["KATL", "KOKC", "KNYC"]
    gs = get_gamma_signals(db_path=str(repo / "data" / "metar_backfill.db"))
    by_day = score_all_stations(gs, str(repo / "data" / "metar_backfill.db"), stations)
    n = persist_results_to_db(by_day, db_path=repo / "data" / "signal_cache.db")
    print(f"gamma scoring: {n} rows for {len(by_day)} station-days")
    for (st, d), rows in list(by_day.items())[:3]:
        print(st, d, [(r.signal, r.fired, round(r.confidence, 3)) for r in rows])
    sys.exit(0)
