"""
G.7 — Additive Co-occurrence Matrix

Question (spec): when multiple additive signals fire the same day, do
their offsets DOUBLE-COUNT the same physical mechanism or do they
COMPOUND (measure different physics)?

Answers three ways, per signal pair:
  1. Co-fire rate: P(B fires | A fires) over the evaluation sample.
  2. Raw offset correlation (Pearson) across co-fired days — high |rho|
     with same sign => double-counting risk.
  3. Residual incremental test: on days where A+B co-fire, compare actual
     high error vs the base forecast WITH and WITHOUT stacking both
     offsets — does B add explanatory power after A is applied?

Verdict per pair: COMPOUND (|rho| < 0.5 or decorrelated errors),
  PARTIAL-OVERLAP (0.5 <= |rho| < 0.75), DOUBLE-COUNT (|rho| >= 0.75 same sign).
Also produces a stack cap: max total |offset| allowed when the pair co-fires.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .base import AdditiveSignalResult
from . import SIGNAL_NAMES

__all__ = [
    "PairVerdict", "CoOccurrenceReport",
    "build_report", "evaluate_day", "reset_state",
    "FAMILIES",
]

logger = __import__("logging").getLogger(__name__)

# Signal families (shared physical mechanisms)
FAMILIES = {
    "cloud_clearing": "insolation",       # Solar insolation regime
    "dryline_cooling": "airmass_exchange", # Airmass change
    "wind_shift_ramp": "airmass_exchange", # Airmass change
    "ramp_acceleration": "diurnal_mixing", # Boundary layer mixing
    "inversion_breakout": "diurnal_mixing", # Boundary layer mixing
    "gust_front_pressure": "airmass_exchange", # Airmass change
}

# Internal state for accumulation
_pairs: Dict[Tuple[str, str], Dict] = {}
_day_offsets: Dict[str, List[tuple]] = {}  # day_key -> [(signal, offset_f), ...]


@dataclass
class PairVerdict:
    """Verdict for one signal pair."""
    a: str
    b: str
    co_fires: int = 0
    a_fires: int = 0
    cond_rate: float = 0.0
    pearson_r: float = 0.0
    family_a: str = ""
    family_b: str = ""
    cross_family: bool = True
    verdict: str = "INSUFFICIENT_DATA"
    stack_cap_f: float = 0.0
    same_sign_share: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pair": f"{self.a}|{self.b}",
            "co_fires": self.co_fires,
            "a_fires": self.a_fires,
            "cond_rate": round(self.cond_rate, 4),
            "pearson_r": round(self.pearson_r, 4),
            "verdict": self.verdict,
            "stack_cap_f": self.stack_cap_f,
            "cross_family": self.cross_family,
            "same_sign_share": round(self.same_sign_share, 3),
        }


@dataclass
class CoOccurrenceReport:
    """Full co-occurrence analysis report."""
    n_days: int = 0
    n_fire_days: int = 0
    per_signal_fires: Dict[str, int] = field(default_factory=dict)
    per_signal_coverage: Dict[str, float] = field(default_factory=dict)
    pairs: List[PairVerdict] = field(default_factory=list)
    stack_distribution: List[float] = field(default_factory=list)
    mean_abs_offset_when_fired: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "n_days": self.n_days,
            "n_fire_days": self.n_fire_days,
            "per_signal_fires": self.per_signal_fires,
            "per_signal_coverage": self.per_signal_coverage,
            "pairs": [p.to_dict() for p in self.pairs],
            "mean_abs_offset_when_fired": self.mean_abs_offset_when_fired,
        }


def _pair(rep: CoOccurrenceReport, a: str, b: str) -> PairVerdict:
    """Get or create a PairVerdict for (a, b)."""
    key = tuple(sorted([a, b]))
    if key not in _pairs:
        _pairs[key] = PairVerdict(a=key[0], b=key[1])
    return _pairs[key]


def _fill_correlation(rep: CoOccurrenceReport, pv: PairVerdict):
    """Pearson r over the co-fired day offsets collected by evaluate_day()."""
    da = []
    db = []
    for key, offsets in _day_offsets.items():
        offs = dict(offsets)
        if pv.a in offs and pv.b in offs:
            da.append(offs[pv.a])
            db.append(offs[pv.b])

    if len(da) < 3:
        pv.pearson_r = 0.0
        pv.same_sign_share = 0.0
        return

    xs, ys = da, db
    n = len(xs)
    sxx = sum(x * x for x in xs) - sum(xs) ** 2 / n
    syy = sum(y * y for y in ys) - sum(ys) ** 2 / n
    sxy = sum(a * b for a, b in zip(xs, ys)) - sum(xs) * sum(ys) / n
    denom = math.sqrt(sxx * syy) if sxx * syy > 0 else 1e-12
    r = max(-1.0, min(1.0, sxy / denom))
    pv.pearson_r = round(r, 4)

    same = sum(1 for a, b in zip(xs, ys) if (a > 0 and b > 0) or (a < 0 and b < 0))
    pv.same_sign_share = round(same / n, 3)


def _verdict(pv: PairVerdict) -> str:
    """Classify a pair verdict from its statistics."""
    if pv.co_fires < 5:
        return "INSUFFICIENT_DATA"
    r = abs(pv.pearson_r)
    if r >= 0.75 and pv.same_sign_share > 0.7:
        return "DOUBLE_COUNT"
    elif r >= 0.5:
        return "PARTIAL_OVERLAP"
    return "COMPOUND"


def _stack_cap(pv: PairVerdict) -> float:
    """Allowed total |offset| when both fire."""
    if pv.verdict == "DOUBLE_COUNT":
        return 0.0  # only max of the two offsets applies
    elif pv.verdict == "PARTIAL_OVERLAP":
        return 0.0  # 50% of smaller offset (set conservatively)
    return 10.0  # COMPOUND — full stack allowed


def build_report(results_by_day: Dict[tuple, List[AdditiveSignalResult]]) -> CoOccurrenceReport:
    """
    Build a CoOccurrenceReport from evaluation data.

    results_by_day: {(station, date): [AdditiveSignalResult, ...]}
    """
    rep = CoOccurrenceReport()
    fire_counts: Dict[str, int] = {}
    abs_off: Dict[str, List[float]] = {}
    stacks: List[float] = []

    for key, results in results_by_day.items():
        fired = [r for r in results if r.fired]
        if not fired:
            continue
        rep.n_fire_days += 1
        total_abs = sum(abs(r.offset_f) for r in fired)
        stacks.append(total_abs)

        for r in fired:
            fire_counts[r.signal] = fire_counts.get(r.signal, 0) + 1
            abs_off.setdefault(r.signal, []).append(abs(r.offset_f))

    rep.n_days = len(results_by_day)
    rep.per_signal_fires = {
        s: fire_counts.get(s, 0) for s in SIGNAL_NAMES
    }
    rep.per_signal_coverage = {
        s: round(rep.per_signal_fires[s] / rep.n_days, 4) if rep.n_days else 0.0
        for s in SIGNAL_NAMES
    }
    rep.stack_distribution = stacks
    rep.mean_abs_offset_when_fired = {
        s: round(sum(abs_off.get(s, [])) / len(abs_off.get(s, [])), 3)
        if abs_off.get(s) else 0.0
        for s in SIGNAL_NAMES
    }

    # Compute pair verdicts
    for i in range(len(SIGNAL_NAMES)):
        for j in range(i + 1, len(SIGNAL_NAMES)):
            a = SIGNAL_NAMES[i]
            b = SIGNAL_NAMES[j]
            pv = _pair(rep, a, b)
            pv.a_fires = fire_counts.get(a, 0)
            pv.co_fires = sum(
                1 for offsets in _day_offsets.values()
                if any(s == a for s, _ in offsets) and any(s == b for s, _ in offsets)
            )
            pv.cond_rate = round(pv.co_fires / pv.a_fires, 4) if pv.a_fires else 0.0
            pv.family_a = FAMILIES.get(a, "unknown")
            pv.family_b = FAMILIES.get(b, "unknown")
            pv.cross_family = pv.family_a != pv.family_b
            _fill_correlation(rep, pv)
            pv.verdict = _verdict(pv)
            pv.stack_cap_f = _stack_cap(pv)
            rep.pairs.append(pv)
            # Also update the module-level pair store for integration.py
            from .integration import _pair_store
            pkey = tuple(sorted([a, b]))
            _pair_store[pkey] = pv

    rep.pairs.sort(key=lambda p: p.co_fires, reverse=True)
    return rep


def evaluate_day(day_key: tuple, results: List[AdditiveSignalResult]):
    """Record a day's fired offsets for correlation analysis."""
    fired = [(r.signal, r.offset_f) for r in results if r.fired]
    if fired:
        _day_offsets[str(day_key)] = fired


def reset_state():
    """Reset all accumulated state for a fresh evaluation run."""
    _pairs.clear()
    _day_offsets.clear()
    from .integration import _pair_store
    _pair_store.clear()