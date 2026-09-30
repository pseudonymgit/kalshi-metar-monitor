#!/usr/bin/env python3
"""
Phase B / Item 3 — AnalogMatcher promoted to an INDEPENDENT conditional-CDF
forecaster.

The analog-matching path inside core/trajectory_confirmation_gate.py
(`AnalogMatcher`) historically acted only as a *discount* on the GEFS
distribution. This module extracts that idea and rebuilds it as a standalone
forecaster that produces its OWN P(high >= X) from the GHCN analog archive — no
GEFS, no emOS, no modulation of another distribution.

Method (deterministic, data-on-disk only, no AI/ML):
  1. Predictors for a target (station, date) — all knowable BEFORE the day:
       - season     : circular day-of-year distance
       - level      : previous-day observed high (persistence)
       - regime     : trailing 3-day mean high (local regime proxy)
  2. Candidate analogs = same station, historical days strictly before the
     target minus an exclusion window (calendar-proximity guard against
     autocorrelation leakage). Same predictor vector is computed per candidate.
  3. Nearest top_k candidates by weighted, climatology-normalized Euclidean
     distance (season / level / regime).
  4. Empirical conditional CDF from the analogs' OWN outcomes:
        P(high >= X) = #{analog high_f >= X} / n_analogs
  5. Optional intraday truncation: given `high_so_far`, the daily max cannot be
     below it, so for X > high_so_far we return the conditional
     P(high>=X)/P(high>=high_so_far), and P(high>=X)=1 for X <= high_so_far.
     This mirrors the DistributionState contract in
     core/intraday/distribution_adapter.py without depending on it.

Strike selection reuses core/strike_selection_ev.py (`select_strike` with a
`p_fn`, exactly as distribution_adapter.py does).

Read-only on source DBs. Additive only — does not touch the existing gate.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GHCN_DB = os.path.join(ROOT, "data", "ghcn_settlements.db")

# ---- tuning (deterministic constants) ----
EXCLUDE_DAYS = 10      # calendar-proximity exclusion window (anti-autocorrelation)
TOP_K = 80             # nearest analogs forming the empirical CDF
DOY_SCALE = 25.0       # degrees-day normalizer for the seasonal axis
W_SEASON = 1.0
W_LEVEL = 2.0          # persistence is the strongest single predictor
W_REGIME = 1.0
MIN_ANALOGS = 8        # below this the CDF is not credible -> widen the pool


# =============================================================================
# Predictor construction
# =============================================================================
def _doy(d: str) -> int:
    return datetime.strptime(d[:10], "%Y-%m-%d").timetuple().tm_yday


def _circ_doy(a: int, b: int) -> float:
    """Circular day-of-year distance, wrapped to [0, 182]."""
    raw = abs(a - b)
    return float(min(raw, 366 - raw))


@dataclass
class _Series:
    """Per-station ordered series with precomputed predictors (high_f only)."""
    dates: List[str]
    highs: List[Optional[float]]

    def __post_init__(self):
        # Precompute once: doy/prev/regime arrays + parsed date ordinals. The
        # naive per-candidate strptime made the holdout sweep O(days^2) in parse
        # cost (~minutes); this makes it one pass. numpy arrays keep the per-
        # target distance scan vectorized (holdout = thousands of target days).
        n = len(self.dates)
        self.H = np.full(n, np.nan)
        self.DOY = np.zeros(n)
        self.ORD = np.zeros(n, dtype=np.int64)
        self.PREV = np.full(n, np.nan)
        self.REG = np.full(n, np.nan)
        for i, ds in enumerate(self.dates):
            dt = datetime.strptime(ds, "%Y-%m-%d")
            self.DOY[i] = dt.timetuple().tm_yday
            self.ORD[i] = dt.toordinal()
            if self.highs[i] is not None:
                self.H[i] = float(self.highs[i])
        for i in range(3, n):
            prev = self.highs[i - 1]
            if prev is None:
                continue
            window = [self.highs[i - k] for k in (1, 2, 3)]
            if any(v is None for v in window):
                continue
            self.PREV[i] = float(prev)
            self.REG[i] = sum(window) / 3.0
        # A candidate is usable iff predictor triple + outcome are all present.
        self.USABLE = (
            np.isfinite(self.PREV) & np.isfinite(self.REG) & np.isfinite(self.H)
        )
        self.CLIM_STD = max(float(np.nanstd(self.H)), 1.0)

    def predictor(self, i: int) -> Optional[Tuple[float, float, float]]:
        """(doy, prev_high, regime3) at index i, or None if predictors missing."""
        if not (np.isfinite(self.PREV[i]) and np.isfinite(self.REG[i])):
            return None
        return (float(self.DOY[i]), float(self.PREV[i]), float(self.REG[i]))

    def ordinal(self, i: int) -> int:
        return int(self.ORD[i])


def _load_series(station: str, db: str = GHCN_DB) -> _Series:
    """Load (date, high_f) for one station, ordered asc. READ-ONLY."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT date, high_f FROM ghcn_settlements "
            "WHERE station=? ORDER BY date", (station,)
        ).fetchall()
    finally:
        conn.close()
    return _Series([r[0][:10] for r in rows], [r[1] for r in rows])


# =============================================================================
# Independent conditional-CDF forecaster
# =============================================================================
class AnalogCDF:
    """An independent empirical P(high >= X) built from GHCN analogs.

    Construct with `.build(station, date, ...)`. The object exposes:
      - `p_high_ge(x)`      : the CDF over X in deg F
      - `select_strikes(..)`: strike picker reusing core/strike_selection_ev.py
      - `crps(y)`           : CRPS of the (untruncated) distribution vs truth
    """

    def __init__(self, station: str, target_date: str,
                 analog_highs: Sequence[float],
                 high_so_far: Optional[float] = None,
                 n_candidates: int = 0):
        self.station = station
        self.date = target_date
        self.high_so_far = high_so_far
        self.n_candidates = n_candidates
        self._samples = sorted(float(h) for h in analog_highs)
        self._n = len(self._samples)

    # ---- empirical distribution ----
    @property
    def n_analogs(self) -> int:
        return self._n

    def _count_ge(self, x: float) -> int:
        # samples sorted asc; count of values >= x
        import bisect
        i = bisect.bisect_left(self._samples, x)
        return self._n - i

    def p_high_ge(self, x: float) -> float:
        """P(high >= X) with the high_so_far truncation boundary enforced."""
        if self._n == 0:
            # No analogs -> no usable CDF. Honestly uninformative.
            return 1.0 if (self.high_so_far is not None and x <= self.high_so_far) else 0.0
        hsf = self.high_so_far
        if hsf is not None and x <= hsf:
            return 1.0
        base = self._count_ge(x) / self._n
        if hsf is None:
            return base
        denom = self._count_ge(hsf) / self._n  # P(high >= high_so_far)
        if denom <= 1e-12:
            return 0.0
        return min(1.0, base / denom)

    # ---- CRPS on the UNTRUNCATED empirical distribution ----
    def crps(self, y: float) -> float:
        """Exact CRPS for an empirical sample vs scalar y (>= 0)."""
        n = self._n
        if n == 0:
            return float("nan")
        s = self._samples
        term1 = sum(abs(v - y) for v in s) / n
        # sum_{i,j}|s_i - s_j| = 2 * sum_k s_k * (2k - n + 1) for sorted s
        tot = sum(s[k] * (2 * k - n + 1) for k in range(n))
        term2 = tot / (n * n)
        return term1 - term2

    # ---- API mirrors (moments for strike_selection_ev args) ----
    def moments(self) -> Tuple[float, float]:
        if self._n == 0:
            return 0.0, 1.0
        mu = sum(self._samples) / self._n
        var = sum((v - mu) ** 2 for v in self._samples) / self._n
        return mu, max(0.5, math.sqrt(var))

    def select_strikes(self, strikes: List[Dict], min_edge: float = 0.0) -> Optional[Dict]:
        """Strike picker reusing core/strike_selection_ev.py with p_fn=self.p_high_ge."""
        from core.strike_selection_ev import select_strike
        mu, sigma = self.moments()
        return select_strike(mu, sigma, strikes, min_edge=min_edge, p_fn=self.p_high_ge)

    # ---- builder ----
    @classmethod
    def build(cls, station: str, target_date: str,
              high_so_far: Optional[float] = None,
              top_k: int = TOP_K, exclude_days: int = EXCLUDE_DAYS,
              db: str = GHCN_DB, series: Optional[_Series] = None) -> "AnalogCDF":
        series = series or _load_series(station, db)
        tgt_idx = None
        for i, d in enumerate(series.dates):
            if d == target_date[:10]:
                tgt_idx = i
                break
        if tgt_idx is None:
            raise ValueError(f"target {station} {target_date} not in archive")

        tgt_pred = series.predictor(tgt_idx)
        if tgt_pred is None:
            raise ValueError(f"missing predictors for {station} {target_date}")
        t_doy, t_prev, t_reg = tgt_pred
        t_ord = series.ordinal(tgt_idx)
        clim_std = series.CLIM_STD

        # Vectorized nearest-neighbor scan over the station's usable candidates.
        # Strictly-before + calendar exclusion window guard against autocorrelation.
        doy = series.DOY
        circ = np.minimum(np.abs(doy - t_doy), 366.0 - np.abs(doy - t_doy))
        d_season = circ / DOY_SCALE
        d_level = np.abs(t_prev - series.PREV) / clim_std
        d_regime = np.abs(t_reg - series.REG) / clim_std
        dist = np.sqrt(W_SEASON * d_season ** 2 + W_LEVEL * d_level ** 2
                       + W_REGIME * d_regime ** 2)
        eligible = series.USABLE & ((t_ord - series.ORD) > exclude_days)
        idx = np.nonzero(eligible)[0]
        n_cand = int(idx.size)
        if n_cand:
            order = idx[np.argsort(dist[idx], kind="stable")]
            k = min(top_k, n_cand)
            analogs = series.H[order[:k]].tolist()
            if len(analogs) < MIN_ANALOGS and n_cand > len(analogs):
                analogs = series.H[order[:max(MIN_ANALOGS, len(analogs))]].tolist()
        else:
            analogs = []

        return cls(station, target_date[:10], analogs,
                   high_so_far=high_so_far, n_candidates=n_cand)


# =============================================================================
# Module-level convenience CDF (the required callable signature)
# =============================================================================
def p_high_ge(station: str, date: str, x: float,
              high_so_far: Optional[float] = None,
              top_k: int = TOP_K) -> float:
    """Independent P(high >= X) in deg F for (station, date). Builds the analog
    CDF on the fly. For repeated X on one (station, date), build AnalogCDF once
    and call `.p_high_ge(x)` instead."""
    return AnalogCDF.build(station, date, high_so_far=high_so_far,
                           top_k=top_k).p_high_ge(x)


# =============================================================================
# Holdout CRPS evaluation (against GHCN settlement truth)
# =============================================================================
STATIONS = ["KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
            "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
            "KPHX", "KSAT", "KSEA", "KSFO"]


def holdout_crps(split: str = "2026-06-01", stations: Optional[List[str]] = None,
                 top_k: int = TOP_K) -> Dict:
    """Independent holdout CRPS of the analog CDF vs GHCN observed highs.

    NOTE: the analog corpus is restricted to days STRICTLY BEFORE the target
    minus the exclusion window, so this is a genuine out-of-sample evaluation.
    """
    stations = stations or STATIONS
    per_station: Dict[str, List[float]] = {}
    all_crps: List[float] = []
    for st in stations:
        series = _load_series(st)
        for i, d in enumerate(series.dates):
            if d < split or series.highs[i] is None:
                continue
            try:
                cdf = AnalogCDF.build(st, d, top_k=top_k, series=series)
            except ValueError:
                continue
            c = cdf.crps(float(series.highs[i]))
            if c == c:  # not NaN
                all_crps.append(c)
                per_station.setdefault(st, []).append(c)
    mean = sum(all_crps) / len(all_crps) if all_crps else float("nan")
    return {
        "split": split,
        "n": len(all_crps),
        "crps_analog": round(mean, 4),
        "per_station": {s: round(sum(v) / len(v), 4) for s, v in sorted(per_station.items())},
    }


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="KNYC")
    ap.add_argument("--date", default=None)
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--split", default="2026-06-01")
    args = ap.parse_args()

    if args.holdout:
        print(json.dumps(holdout_crps(args.split), indent=1))
    else:
        d = args.date or _load_series(args.station).dates[-3]
        cdf = AnalogCDF.build(args.station, d)
        print(f"{args.station} {d}: n_analogs={cdf.n_analogs} cand={cdf.n_candidates}")
        y = _load_series(args.station)
        truth = y.highs[y.dates.index(d)]
        for x in range(int(cdf.moments()[0]) - 6, int(cdf.moments()[0]) + 7, 2):
            print(f"  P(high>={x}) = {cdf.p_high_ge(x):.3f}")
        print(f"  truth={truth}  CRPS={cdf.crps(truth):.4f}")
