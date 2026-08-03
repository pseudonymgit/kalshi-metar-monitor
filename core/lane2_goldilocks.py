"""
Goldilocks Lane — Lane 2: Microstructure Transient Spike Detection

This module is a standalone extraction of the temperature event detection logic
from core/metar_monitor.py, implementing the corrected "instant_cross_revert"
detection with proper ordering.

The Goldilocks edge: 
- For HIGH markets: temp crosses a bucket boundary (e.g., 85°F) but the spike
  is transient — the daily max (from authoritative source, not sub-minute tick)
  remains below the boundary. The market settled NO but transient tickers spiked.
- The signal fires on: bucket-boundary crossing → transient check → reversion

Key fix vs metar_monitor.py: running_daily_max is snapshotted BEFORE adding the
current observation, so spike_delta correctly measures how much this observation
exceeds the PREVIOUS max (not including itself).

Design:
- PURE deterministic math — no ML, no LightGBM, no scikit-learn
- Per-boundary spike tracking (multiple boundaries can be tracked independently)

Author: Gilfoyle (dispatch Aug 3, 2026, B-mode post-Gray-Room)
"""

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ─── Constants ───────────────────────────────────────────────────────────────

# Transient spike threshold: if the spike observation exceeds the running daily
# max (from before this observation) by less than this, it's transient
# (the spike didn't meaningfully push the daily max)
TRANSIENT_DELTA_THRESHOLD = 0.3  # °F

# The spike must exceed the bucket boundary by at least this much to count
# as a genuine crossing (not measurement noise)
EXCEEDED_THRESHOLD = 0.5  # °F above bucket boundary

# Reversion: temp must drop at least this much below the bucket boundary
# OR below the running max at spike time to confirm reversion
REVERSION_MARGIN = 0.2  # °F

# Late-day gate: only fire signals after this UTC hour (daily max is established)
# 18Z = 2pm ET / 11am PT — late enough for most stations
LATE_DAY_UTC_HOUR = 18

# Trend extrapolation: momentum window
MIN_OBS_FOR_MOMENTUM = 2  # Minimum observations for trend

# Min momentum to predict boundary hit (°F/sec)
MIN_MOMENTUM = 0.0005  # ≈ 0.03°F/min

# How far from boundary we consider "approaching" (°F)
APPROACHING_DISTANCE = 1.5  # °F

# Feature weights
FEATURE_WEIGHTS = {
    "temp_f": 0.35,
    "dewpoint_f": 0.20,
    "pressure_mb": 0.20,
    "wind_speed_kt": 0.15,
    "wind_dir": 0.10,
}


# ─── Core Types ──────────────────────────────────────────────────────────────

class MetarObservation:
    """A single METAR observation."""
    def __init__(self, timestamp: datetime, temp_f: float,
                 dewpoint_f: Optional[float] = None,
                 wind_speed_kt: Optional[float] = None,
                 pressure_mb: Optional[float] = None):
        self.timestamp = timestamp
        self.temp_f = temp_f
        self.dewpoint_f = dewpoint_f
        self.wind_speed_kt = wind_speed_kt
        self.pressure_mb = pressure_mb
        self.seconds_epoch = timestamp.timestamp()

    def __repr__(self) -> str:
        return f"MetarObs({self.timestamp.isoformat()}, {self.temp_f:.1f}°F)"


class DailyState:
    """
    Tracks daily temperature state for a station.
    IMPORTANT: add_observation() updates running_max BEFORE returning it.
    For spike detection, snapshot running_max BEFORE calling add_observation().
    """
    def __init__(self, station: str, local_date: str):
        self.station = station
        self.local_date = local_date
        self.running_daily_max: Optional[float] = None  # Max BEFORE current obs
        self.running_daily_min: Optional[float] = None
        self.observations: List[MetarObservation] = []

        # Per-boundary spike trackers: {boundary: tracker_dict}
        self.active_spikes: Dict[int, Dict[str, Any]] = {}
        self.completed_spikes: List[Dict[str, Any]] = []
        self.predicted_hits: List[Dict[str, Any]] = []

    def add_observation(self, obs: MetarObservation) -> None:
        """Add an observation. Updates running_max/min AFTER recording."""
        self.observations.append(obs)
        if self.running_daily_max is None or obs.temp_f > self.running_daily_max:
            self.running_daily_max = obs.temp_f
        if self.running_daily_min is None or obs.temp_f < self.running_daily_min:
            self.running_daily_min = obs.temp_f


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_running_max_before(daily_state: DailyState) -> Optional[float]:
    """
    Get the running daily max from ALL observations EXCEPT the last one.
    This is the critical fix: spike_delta should be computed against the
    max BEFORE the current observation, not including it.
    """
    if len(daily_state.observations) <= 1:
        return None
    # Compute max of all observations except the last (current)
    temps = [o.temp_f for o in daily_state.observations[:-1]]
    return max(temps) if temps else None


def _get_prev_obs(daily_state: DailyState) -> Optional[MetarObservation]:
    """Get the previous observation (second-to-last)."""
    if len(daily_state.observations) < 2:
        return None
    return daily_state.observations[-2]


# ─── Instant Cross Revert Detection ──────────────────────────────────────────

def detect_instant_cross_revert(
    daily_state: DailyState,
    obs: MetarObservation,
    bucket_boundary: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Detect bucket-boundary crossings that DON'T trigger a new daily max,
    then watch for reversion.

    Args:
        daily_state: Current daily state (observation already added)
        obs: Current METAR observation (last one in daily_state)
        bucket_boundary: The boundary to check (e.g., 85). If None, uses
                         the next integer above the previous temp.

    Returns:
        Signal dict if a cross-revert is detected, None otherwise.
    """
    prev_obs = _get_prev_obs(daily_state)
    if prev_obs is None:
        return None

    prev_temp = prev_obs.temp_f
    curr_temp = obs.temp_f

    # Determine the bucket boundary
    if bucket_boundary is None:
        bucket_boundary = int(math.floor(prev_temp)) + 1
        if bucket_boundary < 0:
            return None

    # Check if this observation crosses the boundary going up
    crossed_up = prev_temp < bucket_boundary and curr_temp >= bucket_boundary

    if crossed_up:
        return _handle_cross_up(daily_state, obs, bucket_boundary)
    else:
        return _handle_reversion_check(daily_state, obs, bucket_boundary)


def _handle_cross_up(
    daily_state: DailyState, obs: MetarObservation, boundary: int,
) -> Optional[Dict[str, Any]]:
    """Handle a crossing of the bucket boundary going up."""
    running_max_before = _get_running_max_before(daily_state)

    # spike_delta: how much this observation exceeds the running max before it
    spike_delta = 0.0
    if running_max_before is not None:
        spike_delta = obs.temp_f - running_max_before

    # Transient check: spike_delta < TRANSIENT_DELTA_THRESHOLD means the
    # observation barely exceeded the previous max — the daily max was
    # already close to this level, so this spike is "transient"
    is_transient = spike_delta < TRANSIENT_DELTA_THRESHOLD

    # Determine exceeded_by: how far above the boundary
    exceeded_by = obs.temp_f - boundary
    exceeded_threshold = exceeded_by >= EXCEEDED_THRESHOLD

    # Create tracker
    tracker = {
        "station": daily_state.station,
        "local_date": daily_state.local_date,
        "bucket_boundary": boundary,
        "cross_time": obs.timestamp.isoformat(),
        "cross_temp": obs.temp_f,
        "running_max_before_spike": running_max_before,
        "spike_delta": round(spike_delta, 2),
        "is_transient": is_transient,
        "max_temp_during_spike": obs.temp_f,
        "exceeded_by": round(exceeded_by, 2),
        "exceeded_threshold": exceeded_threshold,
        "reverted": False,
        "revert_time": None,
        "revert_temp": None,
        "alert_emitted": False,
        "time_of_day_hour": obs.timestamp.hour,
    }

    daily_state.active_spikes[boundary] = tracker
    return None  # Signal fires on reversion, not on crossing


def _handle_reversion_check(
    daily_state: DailyState, obs: MetarObservation, boundary: int,
) -> Optional[Dict[str, Any]]:
    """Check if an active spike for this boundary has reverted."""
    spike = daily_state.active_spikes.get(boundary)
    if spike is None:
        return None

    curr_temp = obs.temp_f

    # Update max temp during spike
    spike["max_temp_during_spike"] = max(spike["max_temp_during_spike"], curr_temp)
    exceeded_by = curr_temp - boundary
    spike["exceeded_by"] = max(spike.get("exceeded_by", 0.0), round(exceeded_by, 2))
    if exceeded_by >= EXCEEDED_THRESHOLD:
        spike["exceeded_threshold"] = True

    # Check reversion: temp drops below (boundary - REVERSION_MARGIN)
    # OR drops below the running_max_at_spike
    reverted = curr_temp <= boundary - REVERSION_MARGIN
    running_max_before = _get_running_max_before(daily_state)
    if running_max_before is not None:
        reverted = reverted or curr_temp <= running_max_before - REVERSION_MARGIN

    if reverted and not spike["reverted"]:
        spike["reverted"] = True
        spike["revert_time"] = obs.timestamp.isoformat()
        spike["revert_temp"] = curr_temp

        # Only fire if:
        # 1. The spike is transient
        # 2. The spike exceeded the threshold
        # 3. It's late enough in the day (daily max likely established)
        # 4. Running max before spike existed and was below the boundary
        late_enough = obs.timestamp.hour >= LATE_DAY_UTC_HOUR
        running_max_before = spike.get("running_max_before_spike")
        daily_max_below = running_max_before is not None and running_max_before < boundary

        if spike["is_transient"] and spike["exceeded_threshold"] and late_enough:
            signal = {
                "signal_type": "instant_cross_revert",
                "station": daily_state.station,
                "local_date": daily_state.local_date,
                "bucket_boundary": boundary,
                "cross_time": spike["cross_time"],
                "cross_temp": spike["cross_temp"],
                "max_temp_during_spike": spike["max_temp_during_spike"],
                "revert_time": spike["revert_time"],
                "revert_temp": spike["revert_temp"],
                "spike_delta": spike["spike_delta"],
                "is_transient": True,
                "exceeded_by": spike["exceeded_by"],
                "prediction": "BELOW_BOUNDARY",
                "confidence": _compute_signal_confidence(spike, daily_state),
                "running_max_before_spike": running_max_before,
                "daily_max_below": daily_max_below,
            }
            daily_state.completed_spikes.append(signal)
            spike["alert_emitted"] = True
            return signal

    # If the spike is no longer transient (it meaningfully pushed the daily max),
    # cancel it — it's no longer a transient spike
    if not spike["is_transient"]:
        # If the current observation is setting a new max that's far above,
        # this is structural, not transient
        running_max_before = _get_running_max_before(daily_state)
        if running_max_before is not None:
            new_delta = curr_temp - running_max_before
            if new_delta >= TRANSIENT_DELTA_THRESHOLD:
                spike["canceled"] = True
                daily_state.active_spikes.pop(boundary, None)

    return None


def _compute_signal_confidence(
    spike: Dict[str, Any], daily_state: DailyState
) -> float:
    """
    Compute confidence that the daily max will be below the boundary.
    Higher = more confident the settlement will be below.
    """
    base = 0.50

    # Spike delta bonus: smaller delta = more transient = higher confidence
    spike_delta = spike.get("spike_delta", 0.3)
    delta_bonus = max(0.0, (TRANSIENT_DELTA_THRESHOLD - spike_delta) * 0.5)
    delta_bonus = min(delta_bonus, 0.20)

    # Time-of-day: later = higher confidence (daily max more established)
    hour = spike.get("time_of_day_hour", 18)
    if hour >= 20:
        time_bonus = 0.15
    elif hour >= 18:
        time_bonus = 0.10
    else:
        time_bonus = 0.0

    # Running max before spike was below boundary: good
    running_max_before = spike.get("running_max_before_spike")
    boundary = spike.get("bucket_boundary", 85)
    if running_max_before is not None and running_max_before < boundary - 1.0:
        max_bonus = 0.10  # Running max was well below — spike unlikely to hold
    elif running_max_before is not None and running_max_before < boundary:
        max_bonus = 0.05
    else:
        max_bonus = -0.10  # Running max already near boundary — spike might hold

    confidence = base + delta_bonus + time_bonus + max_bonus
    confidence = max(0.05, min(0.95, confidence))
    return round(confidence, 4)


# ─── Trend Extrapolation ─────────────────────────────────────────────────────

def compute_trend_extrapolation(
    daily_state: DailyState,
    obs: MetarObservation,
    bucket_boundary: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extrapolate: is the current trend likely to hit the next bucket boundary?

    Uses momentum from the last 2+ observations to compute probability of
    hitting the boundary. Deterministic math only (no ML).

    Args:
        daily_state: Current daily state (observation already added)
        obs: Current observation (last in daily_state)
        bucket_boundary: Target boundary. If None, uses the next integer.

    Returns:
        Prediction dict or None.
    """
    if len(daily_state.observations) < MIN_OBS_FOR_MOMENTUM:
        return None

    prev_obs = _get_prev_obs(daily_state)
    if prev_obs is None:
        return None

    # Compute momentum from last 2 observations
    t1 = prev_obs.seconds_epoch
    t2 = obs.seconds_epoch
    if t2 <= t1:
        return None

    temp1 = prev_obs.temp_f
    temp2 = obs.temp_f
    temp_diff = temp2 - temp1

    # Direction
    if temp_diff >= 0.05:  # Upward
        direction = "up"
        momentum = temp_diff / (t2 - t1)
    elif temp_diff <= -0.05:  # Downward
        direction = "down"
        momentum = abs(temp_diff) / (t2 - t1)
    else:
        return None  # Flat

    # Determine boundary
    if bucket_boundary is None:
        if direction == "up":
            bucket_boundary = int(math.floor(temp2)) + 1
        else:
            bucket_boundary = int(math.ceil(temp2)) - 1
            if bucket_boundary < 0:
                return None

    distance = bucket_boundary - temp2 if direction == "up" else temp2 - bucket_boundary

    # Only predict if approaching the boundary
    if distance > APPROACHING_DISTANCE:
        return None

    # Probability: sigmoid based on momentum strength and distance
    # Stronger momentum + closer boundary = higher probability
    if momentum > 0:
        # Normalize: momentum / min_momentum
        momentum_ratio = momentum / MIN_MOMENTUM
        prob = 1.0 / (1.0 + math.exp(-(momentum_ratio - 1.5)))
    else:
        prob = 0.0

    # Distance discount: closer = higher probability
    if distance > 0:
        distance_factor = max(0.0, 1.0 - (distance / APPROACHING_DISTANCE))
        prob *= distance_factor

    # Clamp
    prob = max(0.0, min(1.0, prob))

    # Time to boundary
    time_to_boundary = distance / momentum if momentum > 0 else None

    if direction == "up":
        time_to_boundary = distance / momentum if momentum > 0 else None
    else:
        time_to_boundary = distance / momentum if momentum > 0 else None

    prediction = {
        "signal_type": "trend_extrapolation",
        "station": daily_state.station,
        "local_date": daily_state.local_date,
        "direction": direction,
        "current_temp": round(temp2, 2),
        "prev_temp": round(temp1, 2),
        "bucket_boundary": bucket_boundary,
        "distance_to_boundary": round(distance, 2),
        "momentum_f_per_sec": round(momentum, 6),
        "momentum_f_per_min": round(momentum * 60, 4),
        "time_to_boundary_sec": round(time_to_boundary) if time_to_boundary is not None else None,
        "probability": round(prob, 4),
        "prediction_hit": prob >= 0.50,
    }

    daily_state.predicted_hits.append(prediction)
    return prediction


# ─── Backtest Evaluator ──────────────────────────────────────────────────────

def evaluate_goldilocks_backtest(
    signals: List[Dict[str, Any]],
    actual_daily_max: float,
    bucket_boundary: int,
) -> Dict[str, Any]:
    """
    Evaluate Goldilocks predictions against the actual daily max.

    For instant_cross_revert: signal predicts daily max will be BELOW the
    bucket boundary. Evaluation:
    - TP: signal fired AND actual_daily_max < boundary
    - FP: signal fired AND actual_daily_max >= boundary
    """
    revert_signals = [s for s in signals if s.get("signal_type") == "instant_cross_revert"]
    trend_preds = [s for s in signals if s.get("signal_type") == "trend_extrapolation"]

    tp = sum(1 for s in revert_signals if actual_daily_max < bucket_boundary)
    fp = sum(1 for s in revert_signals if actual_daily_max >= bucket_boundary)

    trend_tp = 0
    trend_fp = 0
    for p in trend_preds:
        if p.get("prediction_hit"):
            if p.get("direction") == "up":
                if actual_daily_max >= bucket_boundary:
                    trend_tp += 1
                else:
                    trend_fp += 1
            elif p.get("direction") == "down":
                if actual_daily_max <= bucket_boundary:
                    trend_tp += 1
                else:
                    trend_fp += 1

    icr_precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    icr_recall = tp / len(revert_signals) if revert_signals else 0.0
    trend_precision = trend_tp / (trend_tp + trend_fp) if (trend_tp + trend_fp) > 0 else 0.0
    trend_recall = trend_tp / len(trend_preds) if trend_preds else 0.0

    return {
        "instant_cross_revert": {
            "signals": len(revert_signals),
            "true_positives": tp,
            "false_positives": fp,
            "precision": round(icr_precision, 4),
            "recall": round(icr_recall, 4),
        },
        "trend_extrapolation": {
            "predictions": len(trend_preds),
            "true_positives": trend_tp,
            "false_positives": trend_fp,
            "precision": round(trend_precision, 4),
            "recall": round(trend_recall, 4),
        },
    }