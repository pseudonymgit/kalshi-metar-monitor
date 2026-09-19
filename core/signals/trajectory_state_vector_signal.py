#!/usr/bin/env python3
"""
Trajectory State Vector Signal — Kalman-filtered temperature trajectory.

REBUILT 2026-09-19 (was a hollow NO_FIRES stub).  Dan's directive: stop
parking signals — build them and test.

This signal encodes the recent temperature trajectory as a state vector
[temp, dT/dt] via a constant-velocity Kalman filter over the station's
daily temperature series, projects it forward one day, and emits a
DIRECTIONAL prediction (up/down) against the station's day-of-year
climatological reference, together with a MAGNITUDE output (the projected
temperature anomaly in °F).

Why magnitude matters (Dan's flagged angle): a direction-only signal that
predicts "warmer" by +0.2°F is worthless for strike-crossing — it must be
"warmer by ≥N°F".  The magnitude output lets downstream layers apply the
GRAY-ROOM-ROUND13-EXPERT-C gate ("only trade when |predicted change| > 3°F").

Deterministic.  No AI/ML.  Contemporaneous-only by construction: the state
vector is built exclusively from days strictly BEFORE the target index (no
lookahead), and the DB path reads daily_stats at/before the target date.

B-Mode compliant.  No AI/ML.
"""
from typing import Optional, Tuple, List, Dict
import math

from .base_signal import BaseSignal, validate_signal, _safe_get


# ── Kalman constants (constant-velocity temperature model) ──────────────
# Process noise: how much the temp can random-walk per day (position °F²/day
# and velocity °F²/day³), observation noise = daily-mean sensor precision.
_Q_POS = 0.5       # °F² per day — position drift
_Q_VEL = 2.0       # °F²/day² per day — velocity drift
_R_OBS = 0.25      # °F² — daily-mean observation variance (σ≈0.5°F)


def _kalman_state_vector(temps: List[float]) -> Tuple[float, float, float, float]:
    """Fit a constant-velocity Kalman state [T, dT/dt] over a daily series.

    Returns (temp, rate, var_temp, var_rate) at the END of the series.
    Deterministic.  Temps are °F, rate is °F/day.
    """
    n = len(temps)
    if n == 0:
        return 0.0, 0.0, 100.0, 100.0

    # Initialize from first observation with broad uncertainty.
    T = temps[0]
    V = 0.0  # initial rate unknown
    P = [[25.0, 0.0], [0.0, 4.0]]  # covariance [temp, rate]

    for k in range(1, n):
        dt = 1.0  # daily step
        # Predict: constant-velocity
        T_pred = T + V * dt
        V_pred = V
        P_tt = P[0][0] + 2.0 * P[0][1] * dt + P[1][1] * dt * dt + _Q_POS * dt
        P_tv = P[0][1] + P[1][1] * dt
        P_vv = P[1][1] + _Q_VEL * dt
        # Symmetric cov
        P_tv_sym = P_tv

        # Update with observation
        z = temps[k]
        S = P_tt + _R_OBS
        K_t = P_tt / S
        K_v = P_tv_sym / S
        innov = z - T_pred
        T = T_pred + K_t * innov
        V = V_pred + K_v * innov
        # Covariance update (Joseph form omitted for symmetry; standard form)
        P_tt = (1.0 - K_t) * P_tt
        P_tv = (1.0 - K_t) * P_tv_sym
        P_vv = P_vv - K_v * P_tv_sym
        P[0][0] = P_tt
        P[0][1] = P_tv
        P[1][0] = P_tv
        P[1][1] = P_vv

    return T, V, max(P[0][0], 1e-6), max(P[1][1], 1e-6)


def _climatology_reference(days: List[Dict], idx: int, field: str = "temp") -> Optional[float]:
    """Rolling climatological reference = mean of prior days' temps.

    Contemporaneous-only: uses only days strictly before idx (offset lookback).
    Returns None if insufficient history.
    """
    lookback = min(idx, 30)
    if lookback < 5:
        return None
    vals = []
    for i in range(idx - lookback, idx):
        d = days[i]
        if isinstance(d, dict) and d.get(field) is not None:
            try:
                vals.append(float(d[field]))
            except (TypeError, ValueError):
                continue
    if len(vals) < 5:
        return None
    return sum(vals) / len(vals)


class TrajectoryStateVectorSignal(BaseSignal):
    """Kalman-filtered trajectory state vector → directional + magnitude signal."""

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self.lookback_days = 7  # days of trajectory to encode

    @property
    def name(self) -> str:
        return "trajectory_state_vector"

    @property
    def min_lookback(self) -> int:
        return self.lookback_days + 1

    # ── Magnitude exposure (for downstream magnitude-gating) ────────────
    def magnitude(self, idx: int, days: List[Dict]) -> Optional[float]:
        """Return the projected temperature anomaly magnitude (°F) at idx.

        Positive = projected warmer-than-climatology, negative = colder.
        None when insufficient data.
        """
        if idx < self.min_lookback or idx >= len(days):
            return None

        temps = []
        for i in range(idx - self.lookback_days, idx):
            d = days[i]
            if isinstance(d, dict) and d.get("temp") is not None:
                try:
                    temps.append(float(d["temp"]))
                except (TypeError, ValueError):
                    temps.append(None)
        temps = [t for t in temps if t is not None]
        if len(temps) < 5:
            return None

        T, V, var_t, _ = _kalman_state_vector(temps)
        projected = T + V * 1.0  # one day forward

        ref = _climatology_reference(days, idx, field="temp")
        if ref is None:
            return None

        return projected - ref

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate trajectory state vector.

        Direction = sign of the Kalman-projected temperature anomaly vs the
        rolling climatological reference.  Confidence = f(anomaly magnitude,
        projected variance, and trajectory consistency).
        """
        if idx < self.min_lookback or idx >= len(days):
            return None, 0.0

        temps = []
        for i in range(idx - self.lookback_days, idx):
            d = days[i]
            if isinstance(d, dict) and d.get("temp") is not None:
                try:
                    temps.append(float(d["temp"]))
                except (TypeError, ValueError):
                    temps.append(None)
        temps = [t for t in temps if t is not None]
        if len(temps) < 5:
            return None, 0.0

        T, V, var_t, var_v = _kalman_state_vector(temps)
        projected = T + V * 1.0
        sigma_proj = math.sqrt(var_t + var_v + _Q_POS)

        ref = _climatology_reference(days, idx, field="temp")
        if ref is None:
            return None, 0.0

        anomaly = projected - ref

        # Dead-band: near-zero anomaly → no directional conviction.
        if abs(anomaly) < 0.5:
            return None, 0.0

        direction = "up" if anomaly > 0 else "down"

        # Confidence: magnitude of anomaly relative to projected uncertainty,
        # scaled into [0, 1].  A 3°F anomaly at σ≈2°F → strong signal.
        z = abs(anomaly) / (sigma_proj + 1e-6)
        confidence = min(1.0, 0.5 + 0.16 * z)

        # Consistency bonus: fraction of the last 3 days trending in the same
        # direction as the projection.
        if len(temps) >= 3:
            deltas = [temps[j] - temps[j - 1] for j in range(1, len(temps))]
            recent = deltas[-3:]
            same = sum(1 for dlt in recent if (dlt > 0 and anomaly > 0) or (dlt < 0 and anomaly < 0))
            consistency = same / len(recent)
            confidence *= (0.6 + 0.4 * consistency)

        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence

    def evaluate_for_station(self, station: str, date: str, conn=None) -> Tuple[Optional[str], float]:
        """Station-level evaluation using daily_stats temperature series.

        Reads daily avg_temp_f strictly before `date`, fits the Kalman state
        vector, projects one day forward, and compares against the station's
        day-of-year climatology from station_climatology.
        """
        import sqlite3

        if not station or not date:
            return None, 0.0

        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path, timeout=15)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT avg_temp_f FROM daily_stats
                WHERE station = ? AND date_utc < ? AND avg_temp_f IS NOT NULL
                ORDER BY date_utc DESC LIMIT ?
            """, (station.upper(), date, self.lookback_days))
            rows = cur.fetchall()
            temps = [float(r[0]) for r in rows if r[0] is not None]
            temps.reverse()  # chronological

            if len(temps) < 5:
                return None, 0.0

            T, V, var_t, var_v = _kalman_state_vector(temps)
            projected = T + V * 1.0
            sigma_proj = math.sqrt(var_t + var_v + _Q_POS)

            # Day-of-year climatology reference
            cur.execute("""
                SELECT AVG(mean_temp) FROM station_climatology
                WHERE station = ? AND day_of_year = ?
            """, (station.upper(), _doy(date)))
            ref_row = cur.fetchone()
            ref = ref_row[0] if ref_row and ref_row[0] is not None else None
            if ref is None:
                # Fall back to the mean of the lookback window itself
                ref = sum(temps) / len(temps)
        finally:
            if own_conn and conn:
                conn.close()

        anomaly = projected - ref
        if abs(anomaly) < 0.5:
            return None, 0.0

        direction = "up" if anomaly > 0 else "down"
        z = abs(anomaly) / (sigma_proj + 1e-6)
        confidence = min(1.0, 0.5 + 0.16 * z)

        if len(temps) >= 3:
            deltas = [temps[j] - temps[j - 1] for j in range(1, len(temps))]
            recent = deltas[-3:]
            same = sum(1 for dlt in recent if (dlt > 0 and anomaly > 0) or (dlt < 0 and anomaly < 0))
            confidence *= (0.6 + 0.4 * (same / len(recent)))

        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence


def _doy(date_str: str) -> int:
    """Day-of-year from YYYY-MM-DD."""
    try:
        from datetime import datetime
        return datetime.strptime(date_str, "%Y-%m-%d").timetuple().tm_yday
    except (ValueError, TypeError):
        return 0
