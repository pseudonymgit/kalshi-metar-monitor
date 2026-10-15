#!/usr/bin/env python3
"""
Item 4/5/8 (2026-09-30) — Multi-forecaster fusion for the SBOX paper bridge.

Three INDEPENDENT probability votes for the same proposition P(high >= X):

  p_gefs       : WS-P calibrated emOS/GEFS Gaussian, intraday-truncated
                 (core/intraday/distribution_adapter.py).
  p_analog     : empirical conditional CDF from GHCN analogs
                 (core/intraday/analog_cdf.py). Independent of GEFS.
  p_trajectory : historical-analog outcome CDF from the trajectory gate
                 (core/trajectory_confirmation_gate.py). Independent of both.

Fusion rule (deterministic, no AI/ML) — see
docs/plans/SBOX-MULTI-FORECASTER-BRIDGE-2026-09-30.md:

  1. Core blend of the two distributional forecasters (GEFS primary):
        agree   (|p_gefs - p_analog| <= 0.10) -> 0.55*p_gefs + 0.45*p_analog, edge x1.0
        split   (<= 0.20)                       -> 0.50/0.50,                  edge x1.25
        disagree(>  0.20)                       -> 0.50/0.50 then shrink 0.5x
                                                   toward 0.5,                  edge x1.50
  2. Trajectory modulator (Item 4) applied to the core as a multiplier on the
     deviation from 0.5:
        p_trajectory <= 0.001 (no analog ever reached this temp) -> x0.75 DISCOUNT
        p_trajectory >= 0.50  (analogs confirm)                  -> x1.05 CONFIRM
        otherwise (0.001, 0.50)                                  -> x0.95 SOFT
     p_fused = clip(0.5 + mult * (p_core - 0.5), 0.01, 0.99)

The module also lands a synthetic query Epoch for "today" from live METAR so the
gate can match real conditions, and extends the GHCN series to today so the
independent analog CDF can build a target it has never seen.

Read-only on source DBs. Additive only.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from core.intraday.analog_cdf import AnalogCDF, _Series, _load_series, GHCN_DB
from core.trajectory_confirmation_gate import (
    Epoch, TrajectoryConfirmationGate, EpochBuildError, EpochBuilder,
)

REPO = Path(__file__).resolve().parents[2]
METAR_DB = REPO / "data" / "metar_backfill.db"
GATE_DB = REPO / "data" / "trajectory_gate.db"

# fusion constants
AGREE_BAND = 0.10
SPLIT_BAND = 0.20
W_GEFS_AGREE = 0.55
SHRINK_DISAGREE = 0.5
TRAJ_DISCOUNT = 0.75
TRAJ_CONFIRM = 1.05
TRAJ_SOFT = 0.95

# --- F2 (2026-10-13): calibrated confidence ceiling -------------------------
# data/ws_p_distribution.json -> holdout_metrics.brier_decomp_calibrated:
#   resolution = 0.0004, uncertainty = 0.2498
# Near-zero resolution means the ensemble separates outcomes no better than a
# constant base-rate predictor, so the 0.99 confidence tail is not earned by
# the data (EXPERT-METHODOLOGY-REVIEW-2026-10-13 E2).
HOLDOUT_RESOLUTION = 0.0004
HOLDOUT_UNCERTAINTY = 0.2498
RESOLUTION_FULL_SKILL = 0.05   # resolution at which the ceiling reaches full skill
CALIBRATION_FLOOR = 0.75       # ceiling at zero resolution (near-zero skill)
MAX_CEILING = 0.99             # never exceed the historical hard clip
CALIBRATED_MIN = 0.50          # floor of the ceiling itself
CLIP_ZONE = 0.90               # amplifier disabled once p_core is already here

# --- weighted fusion (WEIGHTED-FUSION-SPEC-2026-10-13) ----------------------
# om_gfs (2026-10-13 WIRE FLIP) — Open-Meteo bias-corrected GFS qualifies as a
# FIFTH fusion member. The honest re-run (`27463df0d`) found om_gfs(bc) Brier
# 0.123 beats honest rate_1h 0.362, flipping the prior DO_NOT_WIRE verdict.
# This is ADDITIVE: the lever-axis NWP substitution behind USE_OM_GFS_NWP is
# unchanged, and om_gfs now also casts a vote in the core blend + composite
# confidence. It replaces nothing.
OM_GFS_DEFAULT_WEIGHT = 0.15   # shipped per-station om_gfs vote weight
DEFAULT_WEIGHTS = {"gefs": 0.25, "analog": 0.25, "traj": 0.25, "market": 0.25,
                   "om_gfs": OM_GFS_DEFAULT_WEIGHT}

# --- agreement-based additive bump (2026-10-14, Dan) ------------------------
# The log-odds pool already blends the weaker signals implicitly; this is the
# EXPLICIT n-of-k concurrence term Dan asked for: when independent members agree
# on the same side of 0.5, nudge the fused probability further in that direction
# (a small bump from the weaker signals, scaled to how many concur). Gated behind
# USE_AGREEMENT_BOOST (default OFF) so the shipped fusion is byte-identical.
AGREEMENT_BOOST_MAX = 0.03     # magnitude at full concurrence (5 of 5)
AGREEMENT_BOOST_ENV = "USE_AGREEMENT_BOOST"


def agreement_boost_enabled() -> bool:
    """True when the explicit agreement bump is switched on (env, default OFF)."""
    import os
    return os.environ.get(AGREEMENT_BOOST_ENV, "0") != "0"


def agreement_bump(votes: Sequence[Optional[float]], p_core: float,
                   *, max_boost: float = AGREEMENT_BOOST_MAX) -> Tuple[float, int, str]:
    """n-of-k concurrence bump for the fused probability.

    ``votes`` are the present fusion members' P(high>=X) (None entries are
    skipped). Counts how many sit on the same side of 0.5 as ``p_core``:

      * >= 3 concur -> +max_boost * min(count - 2, 3) / 3 (3->0.01, 4->0.02, 5->0.03)
      * <= 1 concur -> -max_boost * min(2 - count, 3) / 3 (1->-0.01, 0->-0.02)
      * otherwise   -> 0

    Returns (bump, concurrence_count, direction) where direction is "high" when
    p_core >= 0.5 else "low".
    """
    present = [float(v) for v in votes if v is not None and v == v]
    k = len(present)
    if k == 0:
        return 0.0, 0, ("high" if p_core >= 0.5 else "low")
    direction = "high" if p_core >= 0.5 else "low"
    if direction == "high":
        count = sum(1 for v in present if v >= 0.5)
    else:
        count = sum(1 for v in present if v < 0.5)
    if count >= 3:
        bump = max_boost * min(count - 2, 3) / 3.0
    elif count <= 1:
        bump = -max_boost * min(2 - count, 3) / 3.0
    else:
        bump = 0.0
    return round(bump, 4), count, direction


def default_om_gfs_weight(weights: Optional[Dict] = None) -> float:
    """The om_gfs vote-weight rule: mean of the NWP-family weights (gefs,
    analog, traj) when supplied, else OM_GFS_DEFAULT_WEIGHT (0.15).

    Used to reason about / seed a station below the refit floor; with the
    shipped family weights (0.25 each) the mean is 0.25 — a per-station refit
    may adopt it, while the shipped default stays the conservative 0.15.
    """
    w = weights or {}
    fam = [w.get(k) for k in ("gefs", "analog", "traj")]
    vals = [float(v) for v in fam if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else OM_GFS_DEFAULT_WEIGHT


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# -----------------------------------------------------------------------------
# 1. Live query epoch for TODAY (from METAR obs over the local weather day)
# -----------------------------------------------------------------------------
def build_live_epoch(station: str, day_start: datetime, day_end: datetime,
                     now_utc: datetime) -> Optional[Epoch]:
    """Build a best-effort Epoch for the in-progress station-local day.

    Fields mirror EpochBuilder.build_epoch but read a partial day. Returns None
    if there is not enough data yet to form a usable feature vector.
    """
    hi_iso = min(now_utc, day_end).isoformat()
    c = _ro(METAR_DB)
    try:
        rows = c.execute(
            "SELECT temp_f, dewpoint_f, wind_speed_kt, wind_direction_deg, "
            "pressure_mb, ceiling_ft FROM metar_observations WHERE station=? "
            "AND timestamp_utc >= ? AND timestamp_utc < ? ORDER BY timestamp_utc",
            (station, day_start.isoformat(), hi_iso)).fetchall()
    finally:
        c.close()
    if len(rows) < 3:
        return None

    temps = [r[0] for r in rows if r[0] is not None]
    dews = [r[1] for r in rows if r[1] is not None]
    winds = [r[2] for r in rows if r[2] is not None]
    dirs = [r[3] for r in rows if r[3] is not None]
    press = [r[4] for r in rows if r[4] is not None]
    if not temps or not dews or not winds or not press:
        return None

    tmax, tmin = max(temps), min(temps)
    tmean = float(np.mean(temps))
    dp_mean = float(np.mean(dews))
    p_mean = float(np.mean(press))
    p_trend = (press[-1] - press[0]) if len(press) >= 2 else None

    if dirs:
        dc = Counter(int(round(d / 22.5) * 22.5 % 360) for d in dirs)
        wdir = dc.most_common(1)[0][0]
    else:
        wdir = None

    rh_vals = []
    for t, d in zip(temps, dews):
        es_t = 6.112 * math.exp(17.67 * t / (t + 243.5))
        es_d = 6.112 * math.exp(17.67 * d / (d + 243.5))
        rh_vals.append(min(100.0, max(0.0, (es_d / es_t) * 100.0)) if es_t > 0 else 0.0)
    rh_mean = float(np.mean(rh_vals)) if rh_vals else None

    cc = []
    for r in rows:
        ceil = r[5]
        if ceil is None:
            continue
        if ceil == 0:
            cc.append(1.0)
        elif ceil >= 25000:
            cc.append(0.0)
        else:
            cc.append(1.0 - ceil / 25000.0)
    cloud = float(np.mean(cc)) if cc else None

    date_str = day_start.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return Epoch(
        station=station, date=date_str,
        temp_max_f=tmax, temp_min_f=tmin, temp_mean_f=tmean,
        dewpoint_mean_f=dp_mean, rh_mean_pct=rh_mean,
        wind_speed_mean_kt=float(np.mean(winds)), wind_dir_mode=wdir,
        pressure_mean_mb=p_mean, pressure_trend=p_trend,
        cloud_cover_mean=cloud, temp_range_f=tmax - tmin,
        dp_depression_mean=tmean - dp_mean,
        wind_speed_max_kt=float(max(winds)), is_query=True)


# -----------------------------------------------------------------------------
# 2. Extend the GHCN series to TODAY so AnalogCDF.build can find the target
# -----------------------------------------------------------------------------
def _metar_gap_highs(station: str, start_date: str, end_date: str) -> Dict[str, float]:
    """Daily max temp_f per UTC date from raw METAR, for gap filling."""
    c = _ro(METAR_DB)
    try:
        rows = c.execute(
            "SELECT substr(timestamp_utc,1,10) d, MAX(temp_f) FROM metar_observations "
            "WHERE station=? AND timestamp_utc >= ? AND timestamp_utc < ? "
            "GROUP BY d", (station, start_date + "T00:00:00",
                           (datetime.strptime(end_date, "%Y-%m-%d")
                            + timedelta(days=1)).strftime("%Y-%m-%d") + "T00:00:00")
        ).fetchall()
    finally:
        c.close()
    return {d: float(v) for d, v in rows if v is not None}


def build_analog_cdf_today(station: str, target_date: str,
                           high_so_far: Optional[float] = None,
                           top_k: int = 80) -> AnalogCDF:
    """Build the independent analog CDF for a target date, extending the GHCN
    series with METAR-derived highs for any gap days up to the target.

    The target's own provisional high is EXCLUDED from its candidate pool by the
    existing strictly-before + exclusion-window guard, so no leakage occurs.
    """
    series = _load_series(station)
    dates = list(series.dates)
    highs = list(series.highs)

    if target_date not in dates:
        last = dates[-1] if dates else "1900-01-01"
        gap = {}
        if dates:
            gap = _metar_gap_highs(station, last, target_date)
        cur = datetime.strptime(last, "%Y-%m-%d")
        end = datetime.strptime(target_date, "%Y-%m-%d")
        d = cur + timedelta(days=1)
        last_known = highs[-1] if highs else None
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            if ds not in dates:
                dates.append(ds)
                h = gap.get(ds)
                # target day: use None (day incomplete) unless caller gave HSF
                if ds == target_date and high_so_far is not None:
                    h = high_so_far
                # gap day with no obs (e.g. missing METAR): carry forward the
                # last known high so the persistence/regime predictors stay
                # defined. Deterministic, documented; the target itself is still
                # excluded from its own candidate pool.
                if h is None and last_known is not None:
                    h = last_known
                if h is not None:
                    last_known = h
                highs.append(h)
            d += timedelta(days=1)
        series = _Series(dates, highs)

    return AnalogCDF.build(station, target_date, high_so_far=high_so_far,
                           top_k=top_k, series=series)


# -----------------------------------------------------------------------------
# 3. Trajectory-gate outcome CDF (Item 4), reusing AnalogMatcher
# -----------------------------------------------------------------------------
class TrajectoryForecaster:
    """Wraps the trajectory gate to expose P(high >= X) from analog outcomes.

    Uses the gate's AnalogMatcher (weighted Euclidean epoch distance) but reads
    each analog's SETTLEMENT high as its outcome instead of the coarse 4-bucket
    direction used by the confirmation gate. Match-quality weighted.
    """

    def __init__(self, gate: Optional[TrajectoryConfirmationGate] = None,
                 epoch_db: str = str(GATE_DB)):
        self.gate = gate or TrajectoryConfirmationGate(epoch_db=epoch_db)
        self._query_key = None
        self._analogs: List[Dict] = []
        self._outcomes: List[float] = []
        self._weights: List[float] = []

    def fit(self, query_epoch: Epoch, top_k: int = 200) -> "TrajectoryForecaster":
        self._analogs = self.gate.matcher.find_analogs(query_epoch, top_k=top_k)
        self._outcomes, self._weights = [], []
        for a in self._analogs:
            s = a["epoch"].settlement_temp
            if s is None:
                continue
            self._outcomes.append(float(s))
            self._weights.append(max(1e-6, float(a["match_score"])))
        return self

    @property
    def n_analogs(self) -> int:
        return len(self._outcomes)

    @property
    def n_match(self) -> int:
        """Analogs found by the matcher (may exceed n_analogs if unsettled)."""
        return len(self._analogs)

    def gate_verdict(self, signal_direction: str, signal_confidence: float) -> Dict:
        """Directional CONFIRM/NEUTRAL/OVERRIDE verdict from the SAME analogs.

        Reuses the gate's own disposition logic so we get Item-4's directional
        read without a second expensive find_analogs pass.
        """
        if len(self._analogs) < 10:
            return {"verdict": "NEUTRAL", "reason": "insufficient_analogs",
                    "p_warm": None, "mod_conf": signal_confidence}
        traj_dist = self.gate.matcher.compute_trajectory_distribution(self._analogs)
        if not traj_dist:
            return {"verdict": "NEUTRAL", "reason": "no_distribution",
                    "p_warm": None, "mod_conf": signal_confidence}
        traj_dir = self.gate._compute_trajectory_direction(traj_dist)
        if traj_dir is None:
            return {"verdict": "NEUTRAL", "reason": "ambiguous",
                    "p_warm": None, "mod_conf": signal_confidence}
        match_score = min(1.0, float(np.mean([a["match_score"]
                                             for a in self._analogs[:50]])))
        from core.trajectory_confirmation_gate import bucket_is_warm
        p_warm = sum(p for b, p in traj_dist.items() if bucket_is_warm(b))
        if signal_direction.lower() == traj_dir:
            v, mc = self.gate._confirm_disposition(len(self._analogs), match_score,
                                                   signal_confidence)
        else:
            v, mc = self.gate._override_disposition(len(self._analogs), match_score,
                                                    signal_confidence)
        return {"verdict": v, "reason": traj_dir, "p_warm": round(p_warm, 4),
                "mod_conf": round(mc, 4)}

    def p_high_ge(self, x: float, high_so_far: Optional[float] = None) -> float:
        if not self._outcomes:
            return float("nan")
        if high_so_far is not None and x <= high_so_far:
            return 1.0
        w = np.asarray(self._weights, dtype=float)
        o = np.asarray(self._outcomes, dtype=float)
        denom = w.sum()
        if denom <= 0:
            return float("nan")
        num = float(w[o >= x].sum())
        if high_so_far is None:
            return num / denom
        base_denom = float(w[o >= high_so_far].sum())
        if base_denom <= 1e-12:
            return 0.0
        # conditional on the day having reached high_so_far at all
        return min(1.0, (num / denom) / (base_denom / denom))


# -----------------------------------------------------------------------------
# 4. Fusion
# -----------------------------------------------------------------------------
def calibrated_ceiling(resolution: Optional[float] = None) -> float:
    """Per-station p_fused ceiling derived from holdout Brier resolution.

    Maps the Brier-decomposition `resolution` term to a confidence ceiling:
    zero resolution -> CALIBRATION_FLOOR (0.75, the current-data result),
    resolution >= RESOLUTION_FULL_SKILL -> MAX_CEILING. Refit by passing a
    per-station or refreshed `resolution`. This is the mechanism that turns
    "0.99 is not earned by the data" into a numeric cap.
    """
    r = HOLDOUT_RESOLUTION if resolution is None else float(resolution)
    if r != r:  # NaN
        r = 0.0
    r = max(0.0, r)
    frac = min(1.0, r / RESOLUTION_FULL_SKILL) if RESOLUTION_FULL_SKILL > 0 else 0.0
    return round(min(MAX_CEILING,
                     CALIBRATION_FLOOR + (MAX_CEILING - CALIBRATION_FLOOR) * frac), 4)


def component_conf(p: Optional[float]) -> float:
    """Derived component confidence: 0 at a coin flip, 1 at certainty."""
    if p is None or p != p:
        return 0.0
    return abs(float(p) - 0.5) * 2.0


def composite_confidence(p_gefs, p_analog, p_trajectory, p_market, weights,
                         p_om_gfs=None) -> float:
    """Weighted mean of component confidences over the components present.

    spec §2.3: composite_conf(s) = sum_i w_i * |p_i - 0.5| * 2.

    ``p_om_gfs`` is the optional FIFTH component (om_gfs, 2026-10-13 WIRE
    FLIP). Existing callers passing the four original components + weights are
    unchanged, and a missing om_gfs vote renormalizes out exactly like a
    missing market vote.
    """
    num = den = 0.0
    for key, p in (("gefs", p_gefs), ("analog", p_analog),
                   ("traj", p_trajectory), ("market", p_market),
                   ("om_gfs", p_om_gfs)):
        w = float(weights.get(key, 0.0) or 0.0)
        if w <= 0 or p is None or p != p:
            continue
        num += w * component_conf(p)
        den += w
    return round(num / den, 4) if den > 0 else 0.0


# Dan's 2026-10-12 grade ladder (composite confidence -> per-trade cap in USD).
GRADE_LADDER = (("S", 0.90, 25), ("A", 0.80, 15), ("B", 0.70, 10),
                ("C", 0.60, 5), ("F", 0.0, 0))


def grade_from_confidence(conf: Optional[float]) -> str:
    """Letter grade from composite confidence per the 2026-10-12 ladder."""
    c = 0.0 if conf is None or conf != conf else float(conf)
    for grade, threshold, _cap in GRADE_LADDER:
        if c >= threshold:
            return grade
    return "F"


def fuse(p_gefs: float, p_analog: float, p_trajectory: float, *,
         weights: Optional[Dict] = None, use_weights: bool = False,
         p_market: Optional[float] = None,
         p_om_gfs: Optional[float] = None,
         ceiling: Optional[float] = None,
         use_agreement_boost: Optional[bool] = None) -> Tuple[float, Dict]:
    """Combine P(high>=X) votes into (p_fused, meta).

    F2 (2026-10-13) fixes the saturation root cause:
      * the traj CONFIRM x1.05 amplifier is neutralized once p_core is already
        in the clip zone (>= CLIP_ZONE) — it can only push p_fused toward an
        unjustified ceiling, never add information;
      * p_fused is clamped to a calibrated ceiling [1-ce, ce] derived from the
        holdout Brier resolution (default 0.75 here), not 0.99.

    ``use_weights`` selects the spec §2.1 weighted core blend
    (sum_i w_i * p_i over {gefs, analog, traj, market, om_gfs}) instead of the
    legacy 0.55/0.45 agree-band blend. ``p_market`` and ``p_om_gfs`` are
    optional; when absent their weight is renormalized out. ``ceiling``
    overrides the derived per-station cap (a per-station refit hook).
    """
    def _cl(v):
        return min(0.999, max(0.001, float(v)))

    pg, pa = _cl(p_gefs), _cl(p_analog)
    gap = abs(pg - pa)
    if gap <= AGREE_BAND:
        agreement, edge_mult = "agree", 1.0
    elif gap <= SPLIT_BAND:
        agreement, edge_mult = "split", 1.25
    else:
        agreement, edge_mult = "disagree", 1.50

    w = dict(weights) if weights else dict(DEFAULT_WEIGHTS)
    pm = None if (p_market is None or p_market != p_market) else _cl(p_market)
    pog = None if (p_om_gfs is None or p_om_gfs != p_om_gfs) else _cl(p_om_gfs)
    ptv = 0.0 if p_trajectory != p_trajectory else float(p_trajectory)

    if use_weights:
        comps = [("gefs", pg, w.get("gefs", 0.0)),
                 ("analog", pa, w.get("analog", 0.0)),
                 ("traj", ptv, w.get("traj", 0.0))]
        if pm is not None:
            comps.append(("market", pm, w.get("market", 0.0)))
        # om_gfs (2026-10-13 WIRE FLIP): the fifth fusion member, ADDITIVE.
        if pog is not None:
            comps.append(("om_gfs", pog, w.get("om_gfs", 0.0)))
        den = sum(max(0.0, wi) for _, _, wi in comps)
        if den > 0:
            p_core = sum(max(0.0, wi) * pv for _, pv, wi in comps) / den
        else:
            p_core = 0.5 * pg + 0.5 * pa
    elif gap <= AGREE_BAND:
        p_core = W_GEFS_AGREE * pg + (1 - W_GEFS_AGREE) * pa
    elif gap <= SPLIT_BAND:
        p_core = 0.5 * pg + 0.5 * pa
    else:
        p_core = 0.5 * pg + 0.5 * pa
        p_core = 0.5 + SHRINK_DISAGREE * (p_core - 0.5)  # shrink toward 0.5

    if p_trajectory != p_trajectory:  # NaN -> gate could not speak
        mult, traj_verdict = 1.0, "unavailable"
    elif ptv <= 0.001:
        mult, traj_verdict = TRAJ_DISCOUNT, "discount"
    elif ptv >= 0.50:
        mult, traj_verdict = TRAJ_CONFIRM, "confirm"
    else:
        mult, traj_verdict = TRAJ_SOFT, "soft"

    # agreement-based additive bump (2026-10-14): explicit n-of-k concurrence
    # term over the present members, gated behind USE_AGREEMENT_BOOST (default
    # OFF). Applied to the core blend BEFORE the trajectory modulator; the
    # calibrated ceiling still clamps the result downstream.
    boost_on = (agreement_boost_enabled() if use_agreement_boost is None
                else bool(use_agreement_boost))
    agree_bump, agree_count, agree_dir = 0.0, 0, "high"
    if boost_on:
        agree_bump, agree_count, agree_dir = agreement_bump(
            [pg, pa, ptv, pm, pog], p_core)
        p_core = min(0.99, max(0.01, p_core + agree_bump))

    # F2: the x1.05 amplifier cannot act inside the clip zone.
    amplifier_neutralized = False
    if mult > 1.0 and p_core >= CLIP_ZONE:
        mult, amplifier_neutralized = 1.0, True

    ce = calibrated_ceiling() if ceiling is None else float(ceiling)
    ce = min(MAX_CEILING, max(CALIBRATED_MIN, ce))
    raw = 0.5 + mult * (p_core - 0.5)
    p_fused = min(0.99, max(0.01, raw))          # legacy hard clip
    calibrated_p = min(ce, max(1.0 - ce, p_fused))  # calibrated ceiling clamp

    conf = composite_confidence(pg, pa, ptv, pm, w, p_om_gfs=pog)
    meta = {"agreement": agreement, "edge_mult": edge_mult,
            "traj_verdict": traj_verdict, "traj_mult": mult,
            "p_core": round(p_core, 4), "gap": round(gap, 4),
            "amplifier_neutralized": amplifier_neutralized,
            "ceiling": round(ce, 4), "calibrated_p": round(calibrated_p, 4),
            "composite_conf": conf, "grade": grade_from_confidence(conf),
            "p_market": (round(pm, 4) if pm is not None else None),
            "p_om_gfs": (round(pog, 4) if pog is not None else None),
            "use_weights": bool(use_weights),
            "agreement_boost": ({"enabled": True, "bump": agree_bump,
                                 "count": agree_count, "direction": agree_dir}
                                if boost_on else {"enabled": False}),
            "weights_applied": {k: round(float(v), 4) for k, v in w.items()}}
    return calibrated_p, meta


def fuse_strike(p_gefs_fn, p_analog_fn, p_trajectory_fn, x: float,
                high_so_far: Optional[float] = None, *,
                weights: Optional[Dict] = None, use_weights: bool = False,
                p_market: Optional[float] = None,
                p_om_gfs: Optional[float] = None,
                ceiling: Optional[float] = None,
                use_agreement_boost: Optional[bool] = None) -> Dict:
    """Fuse one strike. Returns dict with all p_* and fusion meta."""
    pg = p_gefs_fn(x)
    pa = p_analog_fn(x)
    pt = p_trajectory_fn(x)
    pf, meta = fuse(pg, pa, pt, weights=weights, use_weights=use_weights,
                    p_market=p_market, p_om_gfs=p_om_gfs, ceiling=ceiling,
                    use_agreement_boost=use_agreement_boost)
    return {"p_gefs": round(float(pg), 4), "p_analog": round(float(pa), 4),
            "p_trajectory": (round(float(pt), 4) if pt == pt else None),
            "p_fused": round(float(pf), 4), **meta}


# F2 (2026-10-13): the fusion rule string now includes the calibration clamp and
# the optional weighted blend, so booked trades are self-describing.
FUSION_RULE_ID = (
    "fuse_v2:blend(gefs.55/analog.45)@agree|.5/.5@split|.5x@disagree"
    ";traj(x.75d|1.05c|.95s;x1.05 neutralized@pcore>=0.90)"
    ";clamp<=calibrated_ceiling(resolution)"
    ";weights=equal(.25gef/.25ana/.25traj/.25mkt/.15omgfs)")  # om_gfs 5th member (2026-10-13)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive schema: forecaster columns on `trades` + companion tables.

    All changes are additive (never mutate existing columns/rows). F2/weighted
    fusion adds: per-trade calibration columns on the companion
    ``trade_forecaster_probs``, a per-station weight store
    (``station_weights``), and a calibration companion table
    (``trade_calibration``).
    """
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    for name in ("p_gefs", "p_analog", "p_trajectory", "p_fused"):
        if name not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} REAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_forecaster_probs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_uuid TEXT NOT NULL,
            station TEXT NOT NULL,
            trade_date_utc TEXT NOT NULL,
            strike REAL, side TEXT, ticker TEXT,
            p_gefs REAL, p_analog REAL, p_trajectory REAL, p_fused REAL,
            n_analog INTEGER, n_traj INTEGER,
            agreement TEXT, edge_mult REAL, traj_verdict TEXT, traj_mult REAL,
            fusion_rule TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(trade_uuid)
        )
    """)
    # Additive calibration/grade columns on the companion table (may pre-exist).
    # This single list is the source of truth for the companion schema and must
    # match the LIVE book (28 columns): the 2026-10-14 honest calibration
    # (calibrated_p/ceiling/composite_conf/calibrated_at), the F7 grade columns
    # (grade_entered/grade_cap_usd), and the fusion metadata (p_market,
    # grade, weights_applied). CREATE TABLE IF NOT EXISTS never adds a column to
    # an already-created table, so every column is re-checked here and ALTERed in
    # when absent — a pre-existing table gains the column without data loss.
    tfp_cols = {d[1] for d in conn.execute("PRAGMA table_info(trade_forecaster_probs)")}
    for name, decl in (("calibrated_p", "REAL"), ("p_market", "REAL"),
                       ("composite_conf", "REAL"), ("grade", "REAL"),
                       ("ceiling", "REAL"), ("weights_applied", "TEXT"),
                       ("grade_entered", "TEXT"), ("grade_cap_usd", "REAL"),
                       ("calibrated_at", "TEXT")):
        if name not in tfp_cols:
            conn.execute(f"ALTER TABLE trade_forecaster_probs ADD COLUMN {name} {decl}")
    # Per-station weight store (WEIGHTED-FUSION-SPEC-2026-10-13 §4).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_weights (
            station TEXT PRIMARY KEY,
            weight_gefs REAL NOT NULL DEFAULT 0.25,
            weight_analog REAL NOT NULL DEFAULT 0.25,
            weight_traj REAL NOT NULL DEFAULT 0.25,
            weight_market REAL NOT NULL DEFAULT 0.25,
            weight_om_gfs REAL NOT NULL DEFAULT 0.15,
            composite_conf REAL,
            rolling_n INTEGER NOT NULL DEFAULT 0,
            updated_at_utc TEXT NOT NULL
        )
    """)
    # Additive column for pre-existing stores (CREATE IF NOT EXISTS never adds
    # a column to an already-created table). om_gfs 5th member, 2026-10-13.
    sw_cols = {d[1] for d in conn.execute("PRAGMA table_info(station_weights)")}
    if "weight_om_gfs" not in sw_cols:
        conn.execute("ALTER TABLE station_weights ADD COLUMN "
                     "weight_om_gfs REAL NOT NULL DEFAULT 0.15")
    # Calibration companion table — F2 asks for a calibrated_p column on the
    # companion table OR a new calibration table. Provide the latter so the
    # calibration is independently addressable and never mutates trades.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_calibration (
            trade_uuid TEXT PRIMARY KEY,
            station TEXT NOT NULL,
            trade_date_utc TEXT NOT NULL,
            strike REAL, side TEXT, ticker TEXT,
            p_fused_raw REAL, calibrated_p REAL, ceiling REAL,
            p_gefs REAL, p_analog REAL, p_trajectory REAL, p_market REAL,
            composite_conf REAL, grade TEXT,
            amplifier_neutralized INTEGER, fusion_rule TEXT,
            created_at_utc TEXT NOT NULL
        )
    """)
    conn.commit()


def ensure_station_weights_defaults(conn: sqlite3.Connection) -> int:
    """Seed the per-station weight store with EQUAL weights (0.25 each),
    plus the om_gfs fifth member at OM_GFS_DEFAULT_WEIGHT (0.15).

    Spec §3.3: a station below the n>=30 candidate floor keeps equal weights, so
    the store's default IS the shipping configuration until a refit runs.
    Returns the number of stations seeded. Additive/idempotent.
    """
    now = datetime.now(timezone.utc).isoformat()
    stations = [r[0] for r in conn.execute(
        "SELECT DISTINCT station FROM trades WHERE station IS NOT NULL")]
    n = 0
    for st in stations:
        cur = conn.execute(
            "INSERT OR IGNORE INTO station_weights "
            "(station, weight_gefs, weight_analog, weight_traj, weight_market, "
            " weight_om_gfs, composite_conf, rolling_n, updated_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (st, 0.25, 0.25, 0.25, 0.25, OM_GFS_DEFAULT_WEIGHT, None, 0, now))
        n += cur.rowcount
    conn.commit()
    return n


def load_weights(conn: sqlite3.Connection, station: str) -> Dict[str, float]:
    """Per-station weights from `station_weights`, defaulting to equal."""
    row = conn.execute(
        "SELECT weight_gefs, weight_analog, weight_traj, weight_market, "
        "weight_om_gfs FROM station_weights WHERE station=?", (station,)).fetchone()
    if not row:
        return dict(DEFAULT_WEIGHTS)
    return {"gefs": row[0], "analog": row[1], "traj": row[2], "market": row[3],
            "om_gfs": (row[4] if row[4] is not None else OM_GFS_DEFAULT_WEIGHT)}
