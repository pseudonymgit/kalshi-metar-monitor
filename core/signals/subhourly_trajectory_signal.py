#!/usr/bin/env python3
"""
Sub-hourly temperature-trajectory slope nowcast signal.

Predicts whether the settlement temperature will CROSS the strike (settle strictly
above strike) on a "greater-than" market, using the OOS-validated anticipatory cell:

    gap-to-strike in [1.0, 3.0) degF  AND  slope >= 0.05 degF/min

This is a PURELY PREDICTIVE signal. The mechanical `gap < 1` "already-happened" cell
is deliberately excluded (OOS validation ef1e9d8c0 showed it is 21.8% already crossed
at evaluation time).

Constraints (non-negotiable):
  - Deterministic (pure arithmetic, no randomness/ML/AI in the loop).
  - Contemporaneous-only: only observations with `valid <= nowcast time T` are used;
    settlement truth is label-only (never a feature).
  - Correct axis: settlement_temp_f vs strike_price, strict `>`.
  - No lookahead.
  - NOT wired into the live/paper trading path (separate Dan-gated step).

Build spec: docs/plans/SUBHOURLY-SIGNAL-SPEC-2026-09-19.md
"""

import os
import sqlite3
from datetime import datetime, timezone
from statistics import fmean
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .base_signal import BaseSignal

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Station → local timezone (identical to OOS validator ef1e9d8c0) ──
_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}
STATIONS = list(_ICAO_TZ.keys())

# ── Thresholds (frozen from OOS validation ef1e9d8c0) ──
GAP_LO = 1.0          # inclusive lower bound (°F)
GAP_HI = 3.0          # exclusive upper bound (°F)
SLOPE_THRESHOLD = 0.05  # °F/min
SLOPE_CONF_REF = 0.15  # °F/min → confidence 1.0 (slope-gradient plateaus P≈1.0 here)

# ── Trajectory / window constants (identical to validator) ──
TRAJECTORY_WINDOW_MIN = 90
MIN_OBS_FOR_FIT = 4
RES = 5  # downsample 1-min → 5-min steps
WINDOW_BEFORE_HOTD = (180, 240)  # minutes before daily high


def _parse_utc(s):
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _tz_of(icao):
    return ZoneInfo(_ICAO_TZ.get(icao, "America/New_York"))


class SubhourlyTrajectorySignal(BaseSignal):
    """
    Sub-hourly trajectory-slope nowcast: cross / no-cross prediction.

    This signal operates on a sub-hourly (minute-of-day) axis, so it does NOT use the
    daily-day `evaluate(idx, days)` interface. The sweep scores it via
    `evaluate_nowcast(...)`.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self._asos_db = os.path.join(REPO, "data", "iem_asos_1min.db")
        self._truth_db = os.path.join(REPO, "data", "metar_backfill.db")

    # ── BaseSignal interface (not used for sub-hourly; kept for registry compliance) ──
    @property
    def name(self) -> str:
        return "subhourly_trajectory"

    @property
    def min_lookback(self) -> int:
        return 0

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        # Sub-hourly signal: the daily-day interface is not applicable.
        return None, 0.0

    # ── Core math ──
    @staticmethod
    def compute_slope(obs_window: List[Tuple[int, float]]) -> float:
        """
        OLS slope of temperature (°F) on minute-of-day (min). Units: °F/min.

        Matches the OOS validator's ramp_slope() exactly.
        """
        if len(obs_window) < 2:
            return 0.0
        xs = [m for m, _ in obs_window]
        ys = [v for _, v in obs_window]
        mx = fmean(xs)
        my = fmean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 0:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom

    @staticmethod
    def _confidence_for_slope(slope: float) -> float:
        """Deterministic ranking confidence, monotonic in slope, clipped to [0,1]."""
        if slope < 0.0:
            return 0.0
        return max(0.0, min(1.0, slope / SLOPE_CONF_REF))

    # ── Data access ──
    def _load_strike_days(self):
        c = sqlite3.connect(self._truth_db)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            """
            SELECT station, market_type, local_trading_date,
                   settlement_temp_f, strike_price, strike_type
            FROM settlement_truth
            WHERE strike_type='greater' AND trustworthy=1
              AND strike_price IS NOT NULL AND settlement_temp_f IS NOT NULL
            """
        )]
        c.close()
        return [r for r in rows if r["station"] in STATIONS]

    def _load_asos_by_station(self):
        c = sqlite3.connect(self._asos_db)
        cur = c.cursor()
        from collections import defaultdict
        out = defaultdict(lambda: defaultdict(list))
        for st in STATIONS:
            cur.execute(
                "SELECT valid, tmpf FROM observations WHERE station=? AND tmpf IS NOT NULL ORDER BY valid",
                (st,),
            )
            for valid, tmpf in cur.fetchall():
                dt = _parse_utc(valid)
                if dt is None:
                    continue
                loc = dt.astimezone(_tz_of(st))
                mod = loc.hour * 60 + loc.minute
                out[st][loc.date().isoformat()].append((mod, tmpf))
        c.close()
        for st in out:
            for ld in out[st]:
                out[st][ld].sort()
        return out

    @staticmethod
    def _downsample(obs, step_min=RES):
        if step_min <= 1:
            return obs
        out = []
        for m, v in obs:
            if not out or (m - out[-1][0]) >= step_min:
                out.append((m, v))
        return out

    @staticmethod
    def _hotd_minute(asos_day):
        if not asos_day:
            return None
        return max(asos_day, key=lambda x: x[1])[0]

    # ── Primary nowcast entry-point ──
    def evaluate_nowcast(self, station, local_trading_date, t_minute,
                         asos_day=None, strike=None, settle=None):
        """
        Emit a cross / no-cross prediction at a specific nowcast minute `t_minute`
        (local minute-of-day) for a station's trading date.

        Returns a dict per spec §10. `prediction` is None when the signal abstains.
        """
        # Load the day's contemporaneous observations if not provided.
        if asos_day is None:
            asos = self._load_asos_by_station()
            asos_day = asos.get(station, {}).get(local_trading_date)
        if not asos_day or len(asos_day) < MIN_OBS_FOR_FIT:
            return self._result(station, local_trading_date, t_minute, strike, settle,
                                cur_max=None, gap=None, slope=0.0,
                                already_crossed=False, prediction=None, confidence=0.0,
                                outcome=None)

        ds = self._downsample(asos_day, RES)
        past = [(m, v) for (m, v) in ds if m <= t_minute]
        if len(past) < MIN_OBS_FOR_FIT:
            return self._result(station, local_trading_date, t_minute, strike, settle,
                                cur_max=None, gap=None, slope=0.0,
                                already_crossed=False, prediction=None, confidence=0.0,
                                outcome=None)

        win = [(m, v) for (m, v) in past if m >= t_minute - TRAJECTORY_WINDOW_MIN]
        slope = self.compute_slope(win)
        cur_max = max(v for _, v in past)

        # Label (if strike/settle provided) — label-only, never used to predict.
        outcome = None
        if strike is not None and settle is not None:
            outcome = 1 if settle > strike else 0

        gap = (strike - cur_max) if strike is not None else None
        already_crossed = (cur_max > strike) if strike is not None else False

        # Decision rule.
        prediction = None
        confidence = self._confidence_for_slope(slope)

        if strike is not None:
            if already_crossed:
                prediction = None  # "already happened" — abstain (mechanical regime)
            elif gap is not None and GAP_LO <= gap < GAP_HI and slope >= SLOPE_THRESHOLD:
                prediction = "cross"
            else:
                prediction = "no-cross"

        return self._result(station, local_trading_date, t_minute, strike, settle,
                            cur_max=cur_max, gap=gap, slope=slope,
                            already_crossed=already_crossed, prediction=prediction,
                            confidence=confidence, outcome=outcome)

    @staticmethod
    def _result(station, trading_date, t_minute, strike, settle, cur_max, gap, slope,
                already_crossed, prediction, confidence, outcome):
        return {
            "station": station,
            "trading_date": trading_date,
            "t_minute": t_minute,
            "strike": strike,
            "settle": settle,
            "cur_max": cur_max,
            "gap": gap,
            "slope": slope,
            "already_crossed": already_crossed,
            "prediction": prediction,
            "confidence": confidence,
            "outcome": outcome,
        }

    # ── Iterator: emit all nowcast events for scoring (mirrors validator emit_events) ──
    def iter_nowcast_events(self, date_filter=None):
        """
        Yield one nowcast result per (station, trading_date, pre-HOTD minute), in the
        3-4h window before the daily high. Contemporaneous-only (obs <= t).
        """
        strike_days = self._load_strike_days()
        asos = self._load_asos_by_station()

        from collections import defaultdict
        strike_lookup = defaultdict(list)
        for r in strike_days:
            strike_lookup[(r["station"], r["local_trading_date"])].append(r)

        for (station, ldate), rows in strike_lookup.items():
            if date_filter and not date_filter(ldate):
                continue
            asos_day = asos.get(station, {}).get(ldate)
            if not asos_day or len(asos_day) < MIN_OBS_FOR_FIT:
                continue
            hi_mod = self._hotd_minute(asos_day)
            if hi_mod is None:
                continue
            t_lo = max(0, hi_mod - WINDOW_BEFORE_HOTD[1])
            t_hi = max(0, hi_mod - WINDOW_BEFORE_HOTD[0])
            for t in range(t_lo, t_hi + 1, RES):
                for r in rows:
                    yield self.evaluate_nowcast(
                        station, ldate, t, asos_day=asos_day,
                        strike=r["strike_price"], settle=r["settlement_temp_f"],
                    )
