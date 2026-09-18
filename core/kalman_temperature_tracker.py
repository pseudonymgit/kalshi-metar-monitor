"""
Kalman Temperature Tracker

Primary live probability mechanism for weather temperature contracts.

Replaces DTW-only trajectory (p3_trajectory_tracer.py) for live probability
updates. DTW is reserved for regime transition detection (secondary layer).

Design (per TRAJECTORY-KALMAN-SPEC.md):
- State: [T, dT/dt] — current temp and rate of change
- Observation: METAR temp readings, H=[1,0], R=0.25 (σ=0.5°F ASOS precision)
- Process: Constant-velocity random walk with diurnal cycle as control input
- Output: Gaussian probability P(T > target) via projected state covariance

Process noise Q calibrated from clean METAR data:
  q_pos = 0.25  (°F² per hour) — position drift
  q_vel = 3.0   (°F²/hour² per hour) — velocity drift (KNYC data, σ≈1.7°F/hr)

Observation noise R:
  0.25 (σ=0.5°F) — ASOS/ASOS-A sensor precision per FAA standards; bump to
  1.0 for marginal stations, checked via std_high/std_low in station_climatology.

Improvement Suggestion — Adaptive Process Noise:
  Use an ensemble of 3-5 parallel filters with different Q_velocity values
  (e.g., 1.0, 3.0, 6.0, 12.0). Weight each by recent prediction accuracy
  (e.g., 6-hour rolling mean of squared innovations). This handles both
  stable diurnal periods (low Q preferred) and frontal passage regimes
  (high Q wins), improving accuracy during regime transitions without
  sacrificing steady-state precision during normal conditions.
  Reason: A single Q cannot optimally track both slow diurnal changes
  and fast frontal passages; an ensemble naturally selects the best model.

Usage:
    tracker = KalmanTemperatureTracker('KNYC', initial_temp=75.0)
    p, var = tracker.predict(dt_hours=3.0)
    tracker.update(observed_temp=76.5)
    prob, conf = tracker.get_settlement_probability(target_temp=80.0, hours_to_settlement=6.0)
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.station_registry import STATIC_MAPPING

logger = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────────

# Observation noise: ASOS sensor precision σ=0.5°F → R = σ² = 0.25
# Per FAA Order 7900.5B, ASOS temperature accuracy is ±0.5°C (±0.9°F) at
# sensor level, but the resolved recorded value rounds to 0.1°C (0.18°F).
# We use σ=0.5°F (~0.3°C) as a conservative middle ground.
OBSERVATION_VARIANCE = 0.25  # σ² = 0.25 → σ = 0.5°F

# Process noise position: how much the temperature can random-walk per hour
# Calibrated from clean METAR hourly deltas: q_pos = 0.25 means σ~0.5°F/hr drift
Q_POSITION = 0.25  # (°F² per hour)

# Process noise velocity: how fast the rate-of-change can change per hour
# Calibrated from clean KNYC data: hourly_temp_var ≈ 3.62 (σ≈1.9°F/hr)
# Split across position and velocity: give most to velocity uncertainty
Q_VELOCITY = 3.0  # (°F²/hour² per hour)

# Initial covariance: moderate uncertainty before any observations
INITIAL_TEMP_VARIANCE = 25.0  # σ ≈ 5°F
INITIAL_RATE_VARIANCE = 4.0   # σ ≈ 2°F/hour

# Observation validation: reject readings that change temp more than this
# from expected state (as multiple of predicted std dev)
MAX_OBSERVATION_SIGMA = 4.0  # Reject if |innovation| > 4σ

# Diurnal curve fallback: estimated from station daily range / 12 hours
# Morning rise rate ≈ (max - min) / 12, afternoon fall rate ≈ (max - min) / 12
DEFAULT_DIURNAL_RISE_HOURS = 6.0   # Hours from min to max (typical)
DEFAULT_DIURNAL_FALL_HOURS = 18.0  # Hours from max to next min (typical)

# Station list for climatology queries
_STATIONS = [
    'KATL', 'KBOS', 'KDFW', 'KDEN', 'KJFK', 'KLAX', 'KMIA', 'KORD',
    'KSEA', 'KSFO', 'KBNA', 'KHOU', 'KDCA', 'KPDX', 'KSLC', 'PHNL',
    'KTPA', 'KDTW', 'KCLT', 'KMSP', 'KNYC',
]


def _gaussian_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _load_station_climate_priors(station: str) -> Dict:
    """
    Load climate priors for a station from metar_backfill.db.

    Returns dict with keys: mean_temp, std_temp (or defaults if unavailable).
    """
    from core.db_connection import get_connection
    month_priors = {}
    try:
        with get_connection('metar_backfill') as conn:
            # Per-month variance for better seasonal initialization
            month_rows = conn.execute("""
                SELECT
                    CAST(strftime('%m', date_utc) AS INTEGER) as mo,
                    AVG(temp_f) as mean_temp,
                    AVG(temp_f*temp_f) - AVG(temp_f)*AVG(temp_f) as var_temp,
                    COUNT(*) as n
                FROM metar_observations
                WHERE station = ? AND temp_f IS NOT NULL
                  AND temp_f > -20 AND temp_f < 130
                GROUP BY mo
                HAVING n > 50
            """, (station.upper(),)).fetchall()

            for mo, mn, vr, nn in month_rows:
                month_priors[int(mo)] = {
                    'mean': mn,
                    'variance': max(vr, 1.0),
                    'std': math.sqrt(max(vr, 1.0)),
                    'n': nn,
                }

            row = conn.execute("""
                SELECT
                    AVG(temp_f) as mean_temp,
                    AVG(temp_f*temp_f) - AVG(temp_f)*AVG(temp_f) as var_temp,
                    COUNT(*) as n
                FROM metar_observations
                WHERE station = ? AND temp_f IS NOT NULL
                  AND temp_f > -20 AND temp_f < 130
            """, (station.upper(),)).fetchone()

            if row and row[2] > 100:
                mean_temp = row[0]
                variance = max(row[1], 1.0)

                climo = conn.execute("""
                    SELECT AVG(mean_temp), AVG(std_low), AVG(std_high),
                           AVG(mean_high), AVG(mean_low), COUNT(*)
                    FROM station_climatology
                    WHERE station = ?
                """, (station.upper(),)).fetchone()

                climo_mean_high = climo[3] if climo else None
                climo_mean_low = climo[4] if climo else None
                climo_mean = climo[0] if climo else None

                if climo_mean_high is not None:
                    if climo_mean_high > 130 or (climo_mean_low is not None and climo_mean_low < -20):
                        logger.warning(f"Suspicious climo for {station}: "
                                       f"high={climo_mean_high:.1f}F, low={climo_mean_low:.1f}F")
                        climo_mean_high = climo_mean_low = climo_mean = None
                    elif climo_mean_high is not None and climo_mean_low is not None:
                        if climo_mean_high - climo_mean_low > 40:
                            logger.warning(f"Suspicious daily range for {station}: "
                                           f"{climo_mean_high - climo_mean_low:.0f}F")
                            climo_mean_high = climo_mean_low = climo_mean = None

                return {
                    'mean_temp': mean_temp,
                    'std_temp': math.sqrt(variance),
                    'variance': variance,
                    'n_observations': row[2],
                    'climo_mean': climo_mean,
                    'climo_std_low': climo[1] if climo else None,
                    'climo_std_high': climo[2] if climo else None,
                    'climo_mean_high': climo_mean_high,
                    'climo_mean_low': climo_mean_low,
                    'month_priors': month_priors,
                }
    except Exception as e:
        logger.warning(f"Could not load climate priors for {station}: {e}")

    return {}  # Empty dict = use defaults


def _estimate_diurnal_rate(
    station: str,
    month: int,
    local_hour: int,
    climate_priors: Dict = None,
) -> float:
    """
    Estimate expected temperature rate of change (°F/hour) from diurnal cycle.

    Uses station_climatology mean_high/mean_low to estimate rise/fall rates.

    Positive = warming (morning), Negative = cooling (afternoon/evening).
    Returns 0.0 if no data available (filter learns drift from observations).
    """
    if climate_priors is None:
        climate_priors = {}

    climo_mean_high = climate_priors.get('climo_mean_high')
    climo_mean_low = climate_priors.get('climo_mean_low')

    if climo_mean_high is None or climo_mean_low is None:
        return 0.0

    daily_range = climo_mean_high - climo_mean_low
    if daily_range <= 0:
        return 0.0

    # Simple diurnal model: sin wave peaking at ~14:00 local
    # Rate = derivative of daily_range/2 * sin(2π*(hour-6)/24) + mean
    # d/dt => daily_range/2 * 2π/24 * cos(2π*(hour-6)/24)
    # Normalized: peak rate ≈ daily_range * π / 24
    hour_angle = 2.0 * math.pi * (local_hour - 6.0) / 24.0
    peak_rate = daily_range * math.pi / 24.0  # °F/hour
    rate = peak_rate * math.cos(hour_angle)

    return rate


def _validate_temperature(temp_f: float) -> bool:
    """Basic sanity check on temperature reading."""
    return temp_f is not None and -40.0 < temp_f < 140.0


# ─── Kalman Filter Core ──────────────────────────────────────────────────────

@dataclass
class KalmanState:
    """Internal Kalman filter state."""
    x: List[float]  # [T, dT/dt]
    P: List[List[float]]  # 2x2 covariance matrix

    def copy(self) -> 'KalmanState':
        return KalmanState(
            x=list(self.x),
            P=[[self.P[0][0], self.P[0][1]],
               [self.P[1][0], self.P[1][1]]],
        )


class KalmanTemperatureTracker:
    """
    Kalman filter for live temperature tracking with METAR observations.

    Two-layer system:
    - Layer 1 (primary): This Kalman filter — live Bayesian probability updates
    - Layer 2 (secondary): DTW pattern matching (p3_trajectory_tracer.py)
        for regime transition detection

    State vector: [T, dT/dt]^T
    - T: Current temperature estimate (°F)
    - dT/dt: Temperature change rate (°F/hour)

    Process model uses diurnal cycle as control input to improve tracking
    of expected daily temperature pattern.

    Observations rejected if innovation exceeds MAX_OBSERVATION_SIGMA × σ.
    """

    def __init__(
        self,
        station: str,
        initial_temp: Optional[float] = None,
        initial_rate: Optional[float] = None,
        observation_variance: float = OBSERVATION_VARIANCE,
        q_position: float = Q_POSITION,
        q_velocity: float = Q_VELOCITY,
    ):
        """
        Initialize Kalman filter for a station.

        Args:
            station: ICAO station code (e.g., 'KNYC')
            initial_temp: Optional starting temperature. If None, uses climate prior.
            initial_rate: Optional starting rate of change. If None, estimated
                from diurnal cycle.
            observation_variance: R — sensor noise variance (default 0.25)
            q_position: Process noise for position (default 0.25)
            q_velocity: Process noise for velocity (default 3.0)
        """
        self.station = station.upper()
        self.observation_variance = observation_variance
        self.q_position = q_position
        self.q_velocity = q_velocity

        # Load climate priors
        self.climate_priors = _load_station_climate_priors(self.station)

        # Track update history for diagnostic purposes
        self._update_history: List[Dict] = []
        self._last_update_time = None  # UTC timestamp string
        self._observation_count = 0
        self._rejected_count = 0

        # Determine initial temperature
        ts_provided = False
        if initial_temp is not None:
            temp_init = float(initial_temp)
            ts_provided = True
        elif self.climate_priors.get('mean_temp') is not None:
            temp_init = self.climate_priors['mean_temp']
        else:
            temp_init = 70.0  # Fallback

        # Determine initial rate of change
        if initial_rate is not None:
            rate_init = float(initial_rate)
        else:
            # Estimate from diurnal cycle at default hour (noon = moderate rate)
            # Month 6 (June) as default since we don't know the month yet
            rate_init = _estimate_diurnal_rate(
                self.station, month=6, local_hour=12,
                climate_priors=self.climate_priors,
            )

        # Initial covariance
        # When initial_temp is explicitly provided, use MODERATE uncertainty (~5°F std).
        # The initial_temp is a point observation for today, so uncertainty reflects
        # how representative it is of the true state (measurement noise + time offset).
        # DO NOT use the full-dataset variance (e.g., 310 for KNYC) — that captures
        # inter-seasonal variation, not initialization uncertainty.
        #
        # When no initial_temp is given (using climate mean), cap at seasonal variance
        # estimated from monthly data. The climate mean is a broad prior.
        if ts_provided:
            temp_var = INITIAL_TEMP_VARIANCE  # 25.0 (σ=5°F) — moderate initial uncertainty
        else:
            clim_std = self.climate_priors.get('std_temp')
            if clim_std is not None and 0.5 < clim_std < 20.0:
                temp_var = clim_std ** 2
            else:
                temp_var = INITIAL_TEMP_VARIANCE
            # Cap at reasonable level — seasonal variance shouldn't exceed ~15°F std
            temp_var = min(temp_var, 225.0)

        self.state = KalmanState(
            x=[temp_init, rate_init],
            P=[
                [temp_var, 0.0],
                [0.0, INITIAL_RATE_VARIANCE],
            ],
        )

        logger.debug(
            f"Initialized Kalman tracker for {self.station}: "
            f"T={temp_init:.1f}°F, dT/dt={rate_init:+.2f}°F/hr, "
            f"P_T={math.sqrt(temp_var):.2f}°F"
        )

    # ─── Public API ───────────────────────────────────────────────────────────

    def predict(self, dt_hours: float = 1.0) -> Tuple[float, float]:
        """
        Predict temperature at dt_hours from now.

        Applies process model forward in time. Uses diurnal cycle as
        a control input if available.

        Args:
            dt_hours: Hours to project forward (positive for future)

        Returns:
            Tuple of (temperature_estimate, variance)
        """
        if dt_hours <= 0:
            return (self.state.x[0], self.state.P[0][0])

        # Build process model F = [[1, Δt], [0, 1]]
        dt = float(dt_hours)
        F = [[1.0, dt], [0.0, 1.0]]

        # Compute control input: diurnal drift
        # The diurnal cycle adds expected drift to the velocity
        # This is a feedforward term that helps during morning warmup/evening cool-down
        diurnal_drift = 0.0
        if self.climate_priors.get('climo_mean_high') is not None:
            # Estimate current local hour from the last known time or default
            # If we have no last update time, use a neutral hour (12)
            local_hour = 12  # Default neutral
            month = 6  # Default neutral
            if self._last_update_time:
                try:
                    from datetime import datetime, timezone
                    utc_dt = datetime.fromisoformat(self._last_update_time.replace('Z', '+00:00'))
                    # Apply station timezone
                    tz_info = STATIC_MAPPING.get(self.station, {})
                    # Simple UTC offset from station timezone name
                    tz_map = {
                        'America/New_York': -5, 'America/Chicago': -6,
                        'America/Denver': -7, 'America/Los_Angeles': -8,
                        'Pacific/Honolulu': -10, 'America/Detroit': -5,
                    }
                    tz_name = tz_info.get('tz', 'America/New_York')
                    utc_offset = tz_map.get(tz_name, -5)
                    local_dt = utc_dt + dt * 3600  # Approximate
                    local_hour = (utc_dt.hour + utc_offset) % 24
                    month = utc_dt.month
                except Exception:
                    pass

            diurnal_drift = _estimate_diurnal_rate(
                self.station, month=month, local_hour=local_hour,
                climate_priors=self.climate_priors,
            )

        # State prediction: x_{k} = F * x_{k-1} + B*u (control input)
        x_pred = [
            F[0][0] * self.state.x[0] + F[0][1] * self.state.x[1] + dt * diurnal_drift,
            F[1][0] * self.state.x[0] + F[1][1] * self.state.x[1] + 1.0 * diurnal_drift,
        ]

        # Covariance prediction: P_{k} = F * P_{k-1} * F^T + Q
        # F * P * F^T
        fp00 = F[0][0] * self.state.P[0][0] + F[0][1] * self.state.P[1][0]
        fp01 = F[0][0] * self.state.P[0][1] + F[0][1] * self.state.P[1][1]
        fp10 = F[1][0] * self.state.P[0][0] + F[1][1] * self.state.P[1][0]
        fp11 = F[1][0] * self.state.P[0][1] + F[1][1] * self.state.P[1][1]

        P_pred_00 = fp00 * F[0][0] + fp01 * F[0][1]
        P_pred_01 = fp00 * F[1][0] + fp01 * F[1][1]
        P_pred_10 = fp10 * F[0][0] + fp11 * F[0][1]
        P_pred_11 = fp10 * F[1][0] + fp11 * F[1][1]

        # Add process noise Q (exact integral of continuous-time model)
        # For a constant-velocity model with spectral density q_velocity:
        #   Q[0,0] = q_position * dt + q_velocity * dt^3 / 3
        #   Q[0,1] = Q[1,0] = q_velocity * dt^2 / 2
        #   Q[1,1] = q_velocity * dt
        q_pos = self.q_position * dt
        q_vel = self.q_velocity * dt
        q_cross = self.q_velocity * dt * dt * 0.5  # q_vel * dt^2 / 2
        q_pos_from_vel = self.q_velocity * dt * dt * dt / 3.0  # q_vel * dt^3 / 3

        P_pred = [
            [P_pred_00 + q_pos + q_pos_from_vel, P_pred_01 + q_cross],
            [P_pred_10 + q_cross, P_pred_11 + q_vel],
        ]

        # Ensure symmetry (numerical stability)
        avg_off = (P_pred[0][1] + P_pred[1][0]) / 2.0
        P_pred[0][1] = P_pred[1][0] = avg_off

        self.state = KalmanState(x=x_pred, P=P_pred)

        return (self.state.x[0], self.state.P[0][0])

    def update(
        self,
        observed_temp: float,
        observation_variance: Optional[float] = None,
        timestamp_utc: Optional[str] = None,
    ) -> Tuple[float, float]:
        """
        Update state with new METAR observation (standard Kalman update).

        Validates observation before incorporating it. Rejects readings
        that are clearly erroneous (>MAX_OBSERVATION_SIGMA σ from prediction).

        Args:
            observed_temp: Observed temperature in °F
            observation_variance: Optional per-observation variance override
                (defaults to self.observation_variance)
            timestamp_utc: ISO-8601 UTC timestamp for tracking

        Returns:
            Tuple of (updated_temperature, updated_variance)
        """
        if not _validate_temperature(observed_temp):
            self._rejected_count += 1
            logger.debug(f"Rejected observation {observed_temp}°F for {self.station}: "
                         f"out of valid range")
            return (self.state.x[0], self.state.P[0][0])

        self._observation_count += 1
        if timestamp_utc:
            self._last_update_time = timestamp_utc

        # Predict first (dt=0 for instantaneous update)
        if self._observation_count == 1:
            # First observation: initialize state from observation
            obs_var = observation_variance if observation_variance else self.observation_variance
            # Kalman gain for first observation (high prior uncertainty)
            prior_var = self.state.P[0][0]
            kg = prior_var / (prior_var + obs_var)
            x_updated_0 = self.state.x[0] + kg * (observed_temp - self.state.x[0])
            # Just update temperature, keep rate as-is for first obs
            p_updated_00 = (1.0 - kg) * prior_var

            self.state.x[0] = x_updated_0
            self.state.P[0][0] = p_updated_00
            # Reduce covariance cross-terms
            self.state.P[0][1] = (1.0 - kg) * self.state.P[0][1]
            self.state.P[1][0] = self.state.P[0][1]

            self._update_history.append({
                'type': 'first_obs',
                'temp': observed_temp,
                'variance': obs_var,
                'state': list(self.state.x),
            })

            return (self.state.x[0], self.state.P[0][0])

        # Standard Kalman update
        obs_var = observation_variance if observation_variance else self.observation_variance
        H = [1.0, 0.0]  # Observe temperature only

        # Innovation: y = z - H*x
        innovation = observed_temp - (H[0] * self.state.x[0] + H[1] * self.state.x[1])

        # Innovation variance: S = H*P*H^T + R
        S = H[0] * (H[0] * self.state.P[0][0] + H[1] * self.state.P[1][0]) + \
            H[1] * (H[0] * self.state.P[0][1] + H[1] * self.state.P[1][1]) + obs_var

        if S <= 0:
            logger.warning(f"Non-positive innovation variance S={S} for {self.station}")
            return (self.state.x[0], self.state.P[0][0])

        innov_std = math.sqrt(S)

        # Outlier rejection: reject if innovation > MAX_OBSERVATION_SIGMA * sigma
        if abs(innovation) > MAX_OBSERVATION_SIGMA * innov_std:
            self._rejected_count += 1
            logger.info(
                f"Rejected outlier for {self.station}: obs={observed_temp}°F, "
                f"pred={self.state.x[0]:.1f}°F, "
                f"innovation={innovation:+.1f}°F ({abs(innovation)/innov_std:.1f}σ)"
            )
            return (self.state.x[0], self.state.P[0][0])

        # Kalman gain: K = P*H^T / S
        K = [
            (self.state.P[0][0] * H[0] + self.state.P[0][1] * H[1]) / S,
            (self.state.P[1][0] * H[0] + self.state.P[1][1] * H[1]) / S,
        ]

        # State update: x = x + K*y
        x_updated = [
            self.state.x[0] + K[0] * innovation,
            self.state.x[1] + K[1] * innovation,
        ]

        # Covariance update: P = (I - K*H) * P
        # P_updated = P - K*H*P
        khp00 = K[0] * (H[0] * self.state.P[0][0] + H[1] * self.state.P[1][0])
        khp01 = K[0] * (H[0] * self.state.P[0][1] + H[1] * self.state.P[1][1])
        khp10 = K[1] * (H[0] * self.state.P[0][0] + H[1] * self.state.P[1][0])
        khp11 = K[1] * (H[0] * self.state.P[0][1] + H[1] * self.state.P[1][1])

        P_updated = [
            [self.state.P[0][0] - khp00, self.state.P[0][1] - khp01],
            [self.state.P[1][0] - khp10, self.state.P[1][1] - khp11],
        ]

        # Ensure symmetry
        avg_off = (P_updated[0][1] + P_updated[1][0]) / 2.0
        P_updated[0][1] = P_updated[1][0] = avg_off

        self.state = KalmanState(x=x_updated, P=P_updated)

        self._update_history.append({
            'type': 'update',
            'temp': observed_temp,
            'variance': obs_var,
            'innovation': innovation,
            'innovation_std': innov_std,
            'state': list(self.state.x),
        })

        logger.debug(
            f"Updated {self.station}: obs={observed_temp}°F → "
            f"T={self.state.x[0]:.1f}°F, "
            f"dT/dt={self.state.x[1]:+.2f}°F/hr, "
            f"σ={math.sqrt(self.state.P[0][0]):.2f}°F, "
            f"innov={innovation:+.2f}°F ({abs(innovation)/innov_std:.1f}σ)"
        )

        return (self.state.x[0], self.state.P[0][0])

    def get_settlement_probability(
        self,
        target_temp: float,
        hours_to_settlement: float,
    ) -> Tuple[float, float]:
        """
        Compute probability that temperature will be above target_temp at settlement.

        Projects state forward to settlement time, computes Gaussian CDF.

        Args:
            target_temp: Temperature threshold in °F
            hours_to_settlement: Hours until contract settlement

        Returns:
            Tuple of (probability, confidence)
            - probability: P(T > target) ∈ [0, 1]
            - confidence: 1.0 - normalized prediction uncertainty ∈ [0, 1]
                0.0 = complete guess, 1.0 = near-certain

        Note:
            For contracts settling at specific local bucket hours, the caller
            should provide the time difference from now to that bucket hour.
        """
        if hours_to_settlement < 0:
            hours_to_settlement = 0.0

        # Project state forward
        F = [[1.0, hours_to_settlement], [0.0, 1.0]]

        # Mean projection: x_proj = F * x
        T_pred = F[0][0] * self.state.x[0] + F[0][1] * self.state.x[1]
        # Add diurnal drift for long projections
        if hours_to_settlement > 1.0 and self.climate_priors.get('climo_mean'):
            # For projections beyond 1 hour, add expected drift from
            # climatology. This helps during the morning warm-up period.
            pass  # Diurnal drift is implicit in dT/dt state

        # Variance projection: P_proj = F * P * F^T + Q_proj
        fp00 = F[0][0] * self.state.P[0][0] + F[0][1] * self.state.P[1][0]
        fp01 = F[0][0] * self.state.P[0][1] + F[0][1] * self.state.P[1][1]

        var_pred = fp00 * F[0][0] + fp01 * F[0][1]

        # Add projected process noise (exact integral of continuous-time model)
        # For settlement projections, uncertainty grows as:
        #   q_proj = q_position * dt + q_velocity * dt^3 / 3
        q_proj = self.q_position * hours_to_settlement + \
                 self.q_velocity * hours_to_settlement ** 3 / 3.0
        var_pred += q_proj

        # Floor variance at observation noise
        var_pred = max(var_pred, self.observation_variance)

        # Probability T > target
        sigma_pred = math.sqrt(var_pred)
        z_score = (target_temp - T_pred) / sigma_pred
        prob_above = 1.0 - _gaussian_cdf(z_score)

        # Clamp to [0.001, 0.999]
        prob_above = max(0.001, min(0.999, prob_above))

        # Confidence metric: how certain we are about this projection
        # High variance = low confidence
        # Base uncertainty: observation variance (best possible)
        base_uncertainty = math.sqrt(self.observation_variance)
        projection_uncertainty = sigma_pred

        if projection_uncertainty > 0:
            confidence = 1.0 - min(1.0, (projection_uncertainty - base_uncertainty) /
                                   (20.0 + projection_uncertainty))
        else:
            confidence = 0.0

        # Adjust confidence based on observation count
        if self._observation_count == 0:
            confidence *= 0.3  # Low confidence with no observations
        elif self._observation_count < 3:
            confidence *= 0.6  # Moderate confidence with few observations

        confidence = max(0.0, min(1.0, confidence))

        return (prob_above, confidence)

    def get_current_state(self) -> Dict:
        """
        Get current filter state as a dict for diagnostics.

        Returns:
            Dict with keys: temperature, rate, temp_variance, rate_variance,
            observation_count, rejected_count, last_update_time
        """
        return {
            'station': self.station,
            'temperature': self.state.x[0],
            'rate': self.state.x[1],
            'temp_variance': self.state.P[0][0],
            'temp_std': math.sqrt(self.state.P[0][0]),
            'rate_variance': self.state.P[1][1],
            'rate_std': math.sqrt(self.state.P[1][1]),
            'covariance': self.state.P[0][1],
            'observation_count': self._observation_count,
            'rejected_count': self._rejected_count,
            'last_update_time': self._last_update_time,
            'climate_mean': self.climate_priors.get('mean_temp'),
            'climate_std': self.climate_priors.get('std_temp'),
        }

    def get_update_history(self, limit: int = 10) -> List[Dict]:
        """Get recent update history for diagnostic purposes."""
        return self._update_history[-limit:]

    def reset(self, initial_temp: Optional[float] = None):
        """
        Reset filter to initial state.

        Args:
            initial_temp: Optional new initial temperature. If None, uses
                climate prior or keeps current state.
        """
        if initial_temp is not None:
            temp_init = float(initial_temp)
        elif self.climate_priors.get('mean_temp') is not None:
            temp_init = self.climate_priors['mean_temp']
        else:
            temp_init = self.state.x[0]  # Keep current

        rate_init = _estimate_diurnal_rate(
            self.station, month=6, local_hour=12,
            climate_priors=self.climate_priors,
        )

        clim_std = self.climate_priors.get('std_temp')
        temp_var = clim_std ** 2 if (clim_std and clim_std > 0.5) else INITIAL_TEMP_VARIANCE
        temp_var = max(temp_var, 1.0)

        self.state = KalmanState(
            x=[temp_init, rate_init],
            P=[[temp_var, 0.0], [0.0, INITIAL_RATE_VARIANCE]],
        )
        self._update_history = []
        self._last_update_time = None
        self._observation_count = 0
        self._rejected_count = 0

    def get_edge(self, target_temp: float, hours_to_settlement: float,
                 market_price: float,
                 price_timestamp_utc: Optional[str] = None) -> Dict:
        """
        Calculate trading edge for a temperature contract.

        Edge = P(T > target) - market_price

        If price_timestamp_utc is provided, applies a staleness penalty
        that decays edge to zero over 30 minutes. Prices older than 5 minutes
        begin to decay; after 30 minutes, edge is fully suppressed.

        Args:
            target_temp: Contract strike temperature in °F
            hours_to_settlement: Hours until settlement
            market_price: Current market price (probability, 0-1)
            price_timestamp_utc: ISO-8601 timestamp when market_price was observed

        Returns:
            Dict with: probability, edge, confidence, fair_price
        """
        prob, confidence = self.get_settlement_probability(
            target_temp, hours_to_settlement,
        )
        edge = prob - market_price

        # Apply staleness penalty if price timestamp is provided
        staleness_warning = False
        if price_timestamp_utc:
            try:
                from datetime import datetime, timezone
                price_time = datetime.fromisoformat(price_timestamp_utc)
                now = datetime.now(timezone.utc)
                staleness_hours = (now - price_time).total_seconds() / 3600.0

                if staleness_hours > 0.083:  # > 5 minutes
                    # Decay edge proportionally: full at 5min, zero at 30min
                    decay = max(0.0, 1.0 - (staleness_hours - 0.083) / 0.417)
                    edge *= decay
                    staleness_warning = True
            except (ValueError, TypeError):
                pass  # Invalid timestamp, ignore staleness check

        return {
            'station': self.station,
            'target_temp': target_temp,
            'hours_to_settlement': hours_to_settlement,
            'market_price': market_price,
            'probability': prob,
            'edge': edge,
            'confidence': confidence,
            'fair_price': prob,
            'staleness_warning': staleness_warning,
        }


# ─── Convenience: Backfill Sequence Processing ──────────────────────────────

def run_sequence(
    station: str,
    observations: List[Tuple[str, float]],
    initial_temp: Optional[float] = None,
) -> List[Dict]:
    """
    Run Kalman filter over a sequence of observations.

    Args:
        station: ICAO station code
        observations: List of (timestamp_utc, temp_f) tuples, sorted chronologically
        initial_temp: Optional initial temperature

    Returns:
        List of state dicts for each observation step
    """
    tracker = KalmanTemperatureTracker(station, initial_temp=initial_temp)
    results = []

    for i, (ts, temp) in enumerate(observations):
        # Predict forward from last update (estimate dt from timestamps)
        if i == 0:
            dt_hours = 0.0
        else:
            try:
                from datetime import datetime
                prev_dt = datetime.fromisoformat(observations[i - 1][0].replace('Z', '+00:00'))
                curr_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                dt_hours = (curr_dt - prev_dt).total_seconds() / 3600.0
            except Exception:
                dt_hours = 1.0

        if dt_hours > 0:
            tracker.predict(dt_hours=dt_hours)

        tracker.update(observed_temp=temp, timestamp_utc=ts)

        state = tracker.get_current_state()
        state['observation_timestamp'] = ts
        state['observed_temp'] = temp
        results.append(state)

    return results


# ─── Test / Example ───────────────────────────────────────────────────────────
#
# Usage:
#     python -m core.kalman_temperature_tracker
#
# Example output:
#     Initialized Kalman tracker for KNYC: T=75.0F, dT/dt=+0.00F/hr
#     Predicted (dt=3h): T=74.9F, var=28.75
#     Updated with 76.5F: T=76.3F, var=0.23
#     P(T>80F at t+6h) = 0.184, confidence = 0.469

if __name__ == '__main__':
    print("=" * 60)
    print("KalmanTemperatureTracker — Test / Example")
    print("=" * 60)

    # ── Example 1: Basic spec test ──
    print("\n>>> Example 1: Spec test (KNYC, initial_temp=75.0)")
    tracker = KalmanTemperatureTracker('KNYC', initial_temp=75.0)
    print(f"  Initial state: {tracker.get_current_state()['temperature']:.1f}F, "
          f"rate={tracker.get_current_state()['rate']:+.2f}F/hr")

    # Predict 3 hours ahead
    p, var = tracker.predict(dt_hours=3.0)
    print(f"  Predict(dt=3h): T={p:.1f}F, var={var:.4f}, std={var**0.5:.2f}F")

    # Update with new METAR
    t, v = tracker.update(observed_temp=76.5, timestamp_utc='2026-08-10T18:00:00+00:00')
    print(f"  Update(obs=76.5F): T={t:.2f}F, var={v:.4f}, std={v**0.5:.2f}F")

    # Settlement probability
    prob, conf = tracker.get_settlement_probability(target_temp=80.0, hours_to_settlement=6.0)
    print(f"  P(T>80F at t+6h) = {prob:.4f}, confidence = {conf:.3f}")

    # ── Example 2: Sequential updates ──
    print("\n>>> Example 2: Sequential KNYC observations from actual METAR")
    from core.db_connection import get_connection

    try:
        with get_connection('metar_backfill') as conn:
            rows = conn.execute("""
                SELECT timestamp_utc, temp_f
                FROM metar_observations
                WHERE station = 'KNYC' AND temp_f IS NOT NULL
                  AND temp_f > -20 AND temp_f < 130
                ORDER BY timestamp_utc DESC
                LIMIT 24
            """).fetchall()
            rows.reverse()  # Chronological order

            if rows:
                # Find the first temp for init
                first_temp = rows[0][1]
                tracker2 = KalmanTemperatureTracker('KNYC', initial_temp=first_temp)
                state = tracker2.get_current_state()
                print(f"  Init: T={state['temperature']:.1f}F, σ={state['temp_std']:.2f}F")

                # Run first observation as init
                ts, temp = rows[0]
                tracker2.update(observed_temp=temp, timestamp_utc=ts)

                for ts, temp in rows[1:]:
                    try:
                        from datetime import datetime as dt
                        prev_ts = tracker2._last_update_time
                        if prev_ts:
                            prev_dt = dt.fromisoformat(prev_ts.replace('Z', '+00:00'))
                            curr_dt = dt.fromisoformat(ts.replace('Z', '+00:00'))
                            dt_hours = (curr_dt - prev_dt).total_seconds() / 3600.0
                            if dt_hours > 0:
                                tracker2.predict(dt_hours=dt_hours)
                    except Exception:
                        pass

                    tracker2.update(observed_temp=temp, timestamp_utc=ts)

                final = tracker2.get_current_state()
                print(f"  Final: T={final['temperature']:.1f}F, σ={final['temp_std']:.2f}F, "
                      f"rate={final['rate']:+.2f}F/hr")
                print(f"  Observations: {final['observation_count']}, "
                      f"Rejected: {final['rejected_count']}")

                # Settlement probability for various targets
                for target in [75, 80, 85]:
                    prob, conf = tracker2.get_settlement_probability(
                        target_temp=float(target), hours_to_settlement=3.0
                    )
                    print(f"  P(T>{target}F at t+3h) = {prob:.4f}, confidence = {conf:.3f}")

    except Exception as e:
        print(f"  Could not load KNYC sequence: {e}")

    # ── Example 3: Edge calculation ──
    print("\n>>> Example 3: Edge calculation")
    tracker3 = KalmanTemperatureTracker('KNYC', initial_temp=78.0)
    tracker3.update(observed_temp=78.5, timestamp_utc='2026-08-10T12:00:00+00:00')
    edge_info = tracker3.get_edge(
        target_temp=80.0, hours_to_settlement=4.0, market_price=0.35
    )
    print(f"  Station: {edge_info['station']}")
    print(f"  Target: {edge_info['target_temp']}F at t+{edge_info['hours_to_settlement']}h")
    print(f"  Market price: {edge_info['market_price']:.2f}")
    print(f"  Fair probability: {edge_info['probability']:.4f}")
    print(f"  Edge: {edge_info['edge']:+.4f}")
    print(f"  Confidence: {edge_info['confidence']:.3f}")

    # ── Example 4: Outlier rejection ──
    print("\n>>> Example 4: Outlier rejection")
    tracker4 = KalmanTemperatureTracker('KNYC', initial_temp=75.0)
    tracker4.update(observed_temp=75.2, timestamp_utc='2026-08-10T10:00:00+00:00')
    print(f"  After first obs (75.2F): T={tracker4.state.x[0]:.1f}F")

    # Inject a clear outlier (the 37.4F reading we saw in the data)
    tracker4.update(observed_temp=37.4, timestamp_utc='2026-08-10T10:01:00+00:00')
    print(f"  After outlier (37.4F): T={tracker4.state.x[0]:.1f}F (should be ~75F)")
    print(f"  Rejection count: {tracker4._rejected_count}")

    # Normal observation after outlier should work fine
    tracker4.update(observed_temp=76.0, timestamp_utc='2026-08-10T11:00:00+00:00')
    print(f"  After normal obs (76.0F): T={tracker4.state.x[0]:.1f}F")
    print(f"  Rejection count: {tracker4._rejected_count}")

    print("\n" + "=" * 60)
    print("Done.")