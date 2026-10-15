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
  * honest decision-time ceiling **0.82–0.86** pooled (per-station 0.768–0.903).

**2026-10-14 correction (external bug-hunt finding #2).** The first honest
formula, `p_fused·(1−ECE/2) + base_rate·(ECE/2)`, pulled EVERYTHING toward
`BASE_RATE=0.80` one-sidedly, so a coin-flip `p_fused=0.50` became
`calibrated_p=0.5705` — a manufactured YES edge. The shrink must instead act on
the *distance from 0.5* and must be neutral at 0.5:

    calibrated_p = clamp(ceiling, 0.5 + (p_fused − 0.5)·(1 − ECE/2))

This shrinks overconfidence toward 0.5 by the signal's unreliability (ECE/2)
without manufacturing a directional edge: `honest_calibrated_p(0.5, ce) == 0.5`
by construction. No fitting on the target rows; one shrink, one clamp.

**2026-10-15 repair (external calibration audit, ITEM 1).** The transform was
DEGRADING the shipped signal, measured independently on the honest cohort
(n=2927 sole om_gfs rows at the 13:30 pin):

    raw om_gfs_bc_ps Brier        0.1067
    shipped calibration Brier     0.1206   (WORSE; loses to raw at 14/20 stations)

Two independent defects, isolated by ablation:

  * **The ECE constant was rate_1h's.** `ECE=0.47` is the ECE of rate_1h — a
    signal with NO skill on the crossing axis. The SHIPPED signal's own ECE is
    **0.0376** (≈12× smaller). Shrinking the good signal by the broken signal's
    unreliability is the fix for the constant; at ECE=0.0376 the shrink is
    near-identity and Brier is 0.1063 (≤ raw).
  * **The per-station ceilings were rate_1h-derived too** — refit from rate_1h's
    (near-zero) Brier resolution over a 0.75 floor, giving 0.768–0.903. Those
    caps bound **1898/2927** shipped rows by a mean **0.156**, and the ceiling
    alone accounts for essentially all of the degradation (ceiling-only Brier
    0.1234 vs shrink-only 0.1063). The shipped signal is already well-calibrated
    at the top (top decile mean_p 0.9839 vs empirical 0.9744), so the honest cap
    is NEAR-IDENTITY: it must bind only where the signal is demonstrably
    overconfident. Ceilings are now refit from the shipped signal itself
    (`refit_ceiling_shipped_2026-10-15.py`): near-identity (~0.96–0.99), with a
    genuinely tighter cap only at the stations the shipped signal overshoots.

Net: calibrated Brier **0.1063 ≤ raw 0.1067** on the honest cohort; the
no-harm invariant is asserted in `tests/test_ev_gate_calibrated.py`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

REPO = Path(__file__).resolve().parents[2]

# Honest constants (2026-10-15 repair; provenance per the module docstring).
#
# ECE: the SHIPPED signal's own expected calibration error at the 13:30 pin
# (om_gfs_bc_ps, bias-corrected per-station) — NOT rate_1h's 0.47. rate_1h has
# no skill on the crossing axis; shrinking the shipped signal by its ECE
# degraded Brier (0.1067 -> 0.1206). See the audit ablation in the docstring.
ECE = 0.0376           # om_gfs_bc_ps (shipped signal) ECE at the 13:30 pin
BASE_RATE = 0.80       # strike-crossing base rate (diagnostic only; NOT a shrink target)
# Pooled fallback cap = the shipped signal's pooled top-decile crossing bound
# (rate + 2*SE, decile p>=0.9). Near-identity, not the old 0.84 rate_1h cap.
POOLED_CEILING = 0.982

# Per-station ceilings refit from the SHIPPED signal (near-identity; 2026-10-15).
CEILING_REFIT_PATH = (REPO / "artifacts" / "ceiling-refit"
                      / "ceiling_refit_omgfs_2026-10-15.json")

_STATION_CEILINGS_CACHE: Optional[Dict[str, float]] = None


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _require_finite(v, name: str) -> float:
    """Coerce to float, raising ValueError on None / non-finite (fail closed)."""
    if v is None:
        raise ValueError(f"honest_calibrated_p: {name} is None")
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"honest_calibrated_p: {name} is not a number: {v!r}") from exc
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"honest_calibrated_p: {name} is non-finite: {v!r}")
    return f


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
        # Shipped-signal refit (2026-10-15) is authoritative; the legacy
        # rate_1h key is accepted only for backward compatibility of callers
        # that still point at the 2026-10-13 artifact.
        per = (d.get("A2_omgfs_topbin_capped_per_station")
               or d.get("A2_rate1h_bounded_per_station", {}) or {})
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

    Shrinks the *distance from 0.5* by the signal's unreliability (ECE/2), then
    clamps to `ceiling`. **Two-sided and neutral at 0.5**: a coin-flip signal
    stays a coin flip — the transform can never manufacture a directional edge
    from an uninformative input. `base_rate` is accepted for provenance/logging
    only and is NOT a shrink target (the old one-sided shrink to 0.80 is gone).

    Fails CLOSED (ITEM 2, 2026-10-15): a non-finite/None `p_fused` OR a
    non-finite/None `ceiling` raises ValueError. A NaN ceiling used to silently
    clamp to 0.999 (fail-OPEN); `None` used to raise TypeError. Callers that can
    tolerate a missing measurement catch ValueError and skip the row (the bridge
    does exactly that) — the transform itself never manufactures a number from
    missing input.
    """
    p = _require_finite(p_fused, "p_fused")
    ce = _require_finite(ceiling, "ceiling")
    lam = _clamp(float(ece) / 2.0, 0.0, 1.0)
    v = 0.5 + (p - 0.5) * (1.0 - lam)
    return round(min(_clamp(ce, 0.5, 0.999), max(0.01, v)), 4)


def composite_conf_from_p(p: Optional[float]) -> float:
    """Composite confidence derived from a calibrated probability.

    ``|p - 0.5| * 2`` — the same component-confidence shape the fusion uses,
    applied to the honest `calibrated_p` so the companion's stored confidence
    matches the probability it reports.
    """
    if p is None or p != p:
        return 0.0
    return round(abs(float(p) - 0.5) * 2.0, 4)
