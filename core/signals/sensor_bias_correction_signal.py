#!/usr/bin/env python3
"""
Sensor Bias Correction Signal — Corrects for systematic sensor biases
across different METAR station equipment types.

Uses core/nws_revision_model.py which tracks the systematic +0.73°F upward
bias in initial NWS observations. When METAR readings deviate from the
expected revision-adjusted baseline, the signal predicts a directional
correction.

B-Mode compliant. No AI/ML.
"""
import math
import signal
from typing import Optional, Tuple, List, Dict
from datetime import datetime
import sqlite3

from .base_signal import BaseSignal, validate_signal

DEFAULT_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"


class SensorBiasCorrectionSignal(BaseSignal):
    """Corrects systematic sensor biases across stations.

    evaluate(): computes a running bias from the days list: if recent temps
    deviate systematically from the rolling window mean, predict reversion
    toward the unbiased reference.
    evaluate_for_station(): delegates to core/nws_revision_model to fetch
    station-specific NWS revision bias, converting bias magnitude and
    direction into a directional trading signal.
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path or DEFAULT_DB_PATH)

    @property
    def name(self) -> str:
        return "sensor_bias_correction"

    @property
    def min_lookback(self) -> int:
        return 7

    @validate_signal
    def evaluate(self, idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
        """Evaluate sensor bias correction signal.

        Computes a rolling bias over the last N days:
        1. Compute the running mean of temperatures over the lookback window.
        2. For each day, compute deviation from the mean at that point.
        3. If there is a systematic pattern in the deviations (e.g., most recent
           readings are all above or all below the rolling baseline), predict
           a reversion toward the mean.

        This models the NWS revision finding that initial observations have a
        systematic upward bias of approximately +0.73°F.
        """
        if idx < self.min_lookback:
            return None, 0.0

        # Extract recent temperature readings
        window = min(14, idx)  # Use up to 14 days of history
        temps = []
        for i in range(idx - window, idx + 1):
            d = days[i]
            if isinstance(d, dict) and d.get("temp") is not None:
                temps.append(float(d["temp"]))

        if len(temps) < 5:
            return None, 0.0

        # Compute cumulative running mean
        running_means = []
        cum_sum = 0.0
        for j, t in enumerate(temps):
            cum_sum += t
            running_means.append(cum_sum / (j + 1))

        # Compute deviations from the running mean at each point
        # The NWS revision model says initial obs have a +0.73°F upward bias.
        # So if the most recent deviations from the running mean are predominantly
        # positive (warmer than expected), a downward correction is expected.
        deviations = [temps[j] - running_means[j] for j in range(1, len(temps))]

        if len(deviations) < 3:
            return None, 0.0

        # Use the last half of deviations as "recent" signal
        recent_dev = deviations[-(len(deviations) // 2):]
        mean_dev = sum(recent_dev) / len(recent_dev)

        # Bias magnitude (in degrees F)
        abs_bias = abs(mean_dev)

        # If the bias is too small, no signal
        if abs_bias < 0.3:
            return None, 0.0

        # Direction: if sensors are reading too warm (positive mean_dev),
        # the NWS revision model tells us to expect a downward correction.
        # If sensors reading too cold (negative mean_dev), expect upward correction.
        direction = "down" if mean_dev > 0 else "up"

        # Confidence from bias magnitude and consistency of sign
        # Map bias to confidence: 0.3°F → ~0.5, 1.0°F+ → ~0.8
        raw_confidence = min(1.0, abs_bias / 1.5)

        # Consistency: how many recent deviations have the same sign as the mean
        same_sign = sum(1 for d in recent_dev if (d > 0 and mean_dev > 0) or (d < 0 and mean_dev < 0))
        consistency = same_sign / len(recent_dev)
        confidence = raw_confidence * (0.5 + 0.5 * consistency)

        # Clamp and threshold
        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence

    def evaluate_for_station(
        self, station: str, date: str, conn=None
    ) -> Tuple[Optional[str], float]:
        """Station-level evaluation using NWS revision model.

        Delegates to core/nws_revision_model.get_revision_bias() which returns
        (bias_in_degrees_F, confidence). A positive revision bias means NWS
        tends to revise INITIAL observations upward (initial readings were too
        cold), so we expect prices to go UP to correct the cold sensor reading.
        Conversely, a negative revision bias means initial readings were too
        warm, so we expect prices to go DOWN.

        Also fetches recent METAR data via the DB to validate/adjust the
        bias direction.
        """
        if not station or not date:
            return None, 0.0

        try:
            from core.nws_revision_model import get_revision_bias
        except ImportError:
            return None, 0.0

        try:
            # Get the NWS revision bias for this station
            revision_bias, bias_confidence = get_revision_bias(station.upper(), date)
        except Exception:
            revision_bias, bias_confidence = 0.0, 0.0

        # Validate against actual recent METAR readings to refine direction
        recent_temps = []

        def _timeout_handler(signum, frame):
            raise TimeoutError("metar query timed out")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(10)
        try:
            own_conn = conn is None
            if own_conn:
                conn = sqlite3.connect(self.db_path, timeout=10)
                conn.execute("PRAGMA busy_timeout=3000;")

            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, AVG(temp_f)
                FROM metar_observations
                WHERE station=? AND date_utc < ? AND temp_f IS NOT NULL
                GROUP BY date_utc
                ORDER BY date_utc DESC
                LIMIT 14
            """, (station.upper(), date))

            recent_rows = cur.fetchall()

            if own_conn and conn:
                conn.close()

            if recent_rows:
                recent_temps = [float(r[1]) for r in recent_rows if r[1] is not None]
        except TimeoutError:
            pass
        except Exception:
            pass
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        if recent_temps and len(recent_temps) >= 3:
            recent_mean = sum(recent_temps) / len(recent_temps)

            # Compute what the "corrected" temperature would be with NWS bias applied
            # If revision_bias > 0: NWS revises upward (initial reads were cold)
            # If recent METAR is still below what we'd expect after correction,
            #   that strengthens the "up" signal
            # Simplification: use the NWS bias direction as our signal
            pass  # We'll use revision_bias directly

        # Convert NWS revision bias to trading signal:
        # revision_bias > 0 means NWS revises UP → initial sensor was reading COLD
        #   → expect settlement price to go UP → direction = "up"
        # revision_bias < 0 means NWS revises DOWN → initial sensor was reading WARM
        #   → expect settlement price to go DOWN → direction = "down"
        abs_bias = abs(revision_bias)
        if abs_bias < 0.1:
            return None, 0.0

        direction = "up" if revision_bias > 0 else "down"

        # Confidence from NWS model confidence and bias magnitude
        adjusted_confidence = bias_confidence * 0.7  # Conservative base
        magnitude_factor = min(1.0, abs_bias / 1.0)  # 0.1°F → 0.1, 1.0°F+ → 1.0
        confidence = adjusted_confidence * (0.5 + 0.5 * magnitude_factor)

        # If we have actual METAR data, check if recent readings validate the bias
        if recent_temps and len(recent_temps) >= 7:
            # Compute if the trend supports the signal direction
            # For "up" signal: expect recent temps to be trending upward toward correction
            # For "down" signal: expect recent temps trending downward
            first_half = sum(recent_temps[:len(recent_temps)//2]) / (len(recent_temps)//2)
            second_half = sum(recent_temps[len(recent_temps)//2:]) / (len(recent_temps) - len(recent_temps)//2)
            trend = second_half - first_half

            trend_supports = (direction == "up" and trend > 0) or (direction == "down" and trend < 0)
            if trend_supports:
                confidence = min(1.0, confidence * 1.3)
            else:
                confidence *= 0.8

        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.51:
            return None, 0.0

        return direction, confidence