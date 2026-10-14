#!/usr/bin/env python3
"""Honest decision-time recalibration (2026-10-14) — single source of the
`calibrated_p` formula used by BOTH the companion back-fill script
(`scripts/recalibrate_companion.py`) and the SBOX entry gate
(`scripts/sbox_intraday_ev_bridge.py`).

Background (Workstream X — HONEST-ARSENAL-REEVALUATION-2026-10-13):
the pre-refit `calibrated_p` was a bare ceiling clamp — `min(p_fused, 0.7519)` —
which produced exactly two values (0.2481 / 0.7519) on the live companion table.
That is a clamp, not a calibration. The honest re-run measured the decision-time
signal at the 13:30 station-local pin:

  * rate_1h Brier ≈ 0.36, resolution 0.019, **ECE 0.47** (grossly miscalibrated);
  * honest decision-time ceiling **0.82–0.86** pooled (per-station 0.768–0.903);
  * the crossing base rate for the chosen strike axis is ≈ **0.80**.

The recalibration therefore (a) regresses the raw fused probability toward the
base rate in proportion to how unreliable the signal is (ECE/2), then (b) clamps
at the honest per-station ceiling:

    calibrated_p = min(ceiling, max(0.01, p_fused*(1 - ECE/2) + base_rate*ECE/2))

It is intentionally cheap and defensible: one linear shrink to the base rate,
one clamp at the honest ceiling. No fitting on the target rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

REPO = Path(__file__).resolve().parents[2]

# Honest constants (HONEST-ARSENAL-REEVALUATION-2026-10-13, corrected headlines).
ECE = 0.47             # rate_1h expected calibration error at the 13:30 pin
BASE_RATE = 0.80       # strike-crossing base rate for the chosen axis
POOLED_CEILING = 0.84  # pooled honest decision-time ceiling (0.82–0.86)

# Per-station ceilings from the ceiling refit (bounded ≤ pin).
CEILING_REFIT_PATH = REPO / "artifacts" / "ceiling-refit" / "ceiling_refit_2026-10-13.json"

_STATION_CEILINGS_CACHE: Optional[Dict[str, float]] = None


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def station_ceilings(path: Path = CEILING_REFIT_PATH) -> Dict[str, float]:
    """Per-station honest ceilings from the ceiling refit artifact (cached).

    Returns {} when the artifact is absent/corrupt — callers fall back to
    POOLED_CEILING.
    """
    global _STATION_CEILINGS_CACHE
    if _STATION_CEILINGS_CACHE is not None:
        return _STATION_CEILINGS_CACHE
    out: Dict[str, float] = {}
    try:
        d = json.loads(Path(path).read_text())
        per = d.get("A2_rate1h_bounded_per_station", {}) or {}
        for st, row in per.items():
            ce = row.get("ceiling")
            if ce is not None:
                out[str(st)] = float(ce)
    except Exception:
        out = {}
    _STATION_CEILINGS_CACHE = out
    return out


def station_ceiling(station: str, *, pooled: float = POOLED_CEILING) -> float:
    """Honest ceiling for a station; pooled fallback when unknown."""
    return float(station_ceilings().get(station, pooled))


def honest_calibrated_p(p_fused: float, ceiling: float, *,
                        ece: float = ECE,
                        base_rate: float = BASE_RATE) -> float:
    """Recalibrate a raw fused probability to the honest decision-time axis.

    Shifts `p_fused` toward `base_rate` proportional to the signal's
    unreliability (ECE/2), then clamps to `ceiling`. Values at/below the base
    rate that were inflated by an unreliable signal are pulled *up* toward the
    base rate; overconfident values are pulled down and clamped.

    Raises ValueError on a non-finite `p_fused` (callers fail closed).
    """
    if p_fused != p_fused:  # NaN
        raise ValueError("honest_calibrated_p: p_fused is NaN")
    ce = _clamp(float(ceiling), 0.5, 0.999)
    lam = _clamp(float(ece) / 2.0, 0.0, 1.0)
    v = float(p_fused) * (1.0 - lam) + float(base_rate) * lam
    return round(min(ce, max(0.01, v)), 4)


def composite_conf_from_p(p: Optional[float]) -> float:
    """Composite confidence derived from a calibrated probability.

    ``|p - 0.5| * 2`` — the same component-confidence shape the fusion uses,
    applied to the honest `calibrated_p` so the companion's stored confidence
    matches the probability it reports.
    """
    if p is None or p != p:
        return 0.0
    return round(abs(float(p) - 0.5) * 2.0, 4)
