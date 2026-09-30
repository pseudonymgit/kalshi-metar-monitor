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
from typing import Dict, List, Optional, Tuple

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
def fuse(p_gefs: float, p_analog: float, p_trajectory: float) -> Tuple[float, Dict]:
    """Combine three P(high>=X) votes into (p_fused, meta)."""
    def _cl(v):
        return min(0.999, max(0.001, float(v)))

    pg, pa = _cl(p_gefs), _cl(p_analog)
    gap = abs(pg - pa)
    if gap <= AGREE_BAND:
        p_core = W_GEFS_AGREE * pg + (1 - W_GEFS_AGREE) * pa
        agreement, edge_mult = "agree", 1.0
    elif gap <= SPLIT_BAND:
        p_core = 0.5 * pg + 0.5 * pa
        agreement, edge_mult = "split", 1.25
    else:
        p_core = 0.5 * pg + 0.5 * pa
        p_core = 0.5 + SHRINK_DISAGREE * (p_core - 0.5)  # shrink toward 0.5
        agreement, edge_mult = "disagree", 1.50

    pt = p_trajectory
    if pt != pt:  # NaN -> gate could not speak
        mult, traj_verdict = 1.0, "unavailable"
    elif pt <= 0.001:
        mult, traj_verdict = TRAJ_DISCOUNT, "discount"
    elif pt >= 0.50:
        mult, traj_verdict = TRAJ_CONFIRM, "confirm"
    else:
        mult, traj_verdict = TRAJ_SOFT, "soft"

    p_fused = min(0.99, max(0.01, 0.5 + mult * (p_core - 0.5)))
    meta = {"agreement": agreement, "edge_mult": edge_mult,
            "traj_verdict": traj_verdict, "traj_mult": mult,
            "p_core": round(p_core, 4), "gap": round(gap, 4)}
    return p_fused, meta


def fuse_strike(p_gefs_fn, p_analog_fn, p_trajectory_fn, x: float,
                high_so_far: Optional[float] = None) -> Dict:
    """Fuse one strike. Returns dict with all p_* and fusion meta."""
    pg = p_gefs_fn(x)
    pa = p_analog_fn(x)
    pt = p_trajectory_fn(x)
    pf, meta = fuse(pg, pa, pt)
    return {"p_gefs": round(float(pg), 4), "p_analog": round(float(pa), 4),
            "p_trajectory": (round(float(pt), 4) if pt == pt else None),
            "p_fused": round(float(pf), 4), **meta}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive schema: forecaster columns on `trades` + companion table."""
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
    conn.commit()


FUSION_RULE_ID = "fuse_v1:blend(gefs.55/analog.45)@agree|.5/.5@split|.5x@disagree;traj(x.75d|1.05c|.95s)"
