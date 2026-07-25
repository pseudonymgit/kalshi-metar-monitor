#!/usr/bin/env python3
"""
Spike Reversion Signal Module (formerly Goldilocks Signal)

Renamed per Gray Room Round 7, item A3: the daily-level signal is now
"SpikeReversionSignal" to disambiguate it from the real-time
MicrostructureSpikeDetector in metar_monitor.py.

Implements the C5 spike-direction asymmetry signal with:
- C5_up: Spike up → reversion down (is_down=False)
- C5_down: Spike down → reversion up (is_down=True)

Based on asymmetric confidence scoring from Gray Room Round 4-1.5:
- Up reversion (spike up → drop back): base 0.40, more reliable
- Down reversion (spike down → bounce back): base 0.25, less reliable

R7-A1: Confidence inversion fix applied — transient spikes get high base
(0.50), structural spikes get low base (0.10).
"""

from .base_signal import BaseSignal, _safe_get, validate_signal
from typing import Optional, Tuple, Dict, Any, List
import sqlite3
import math

from core.sqlite_utils import get_sqlite_connection


# Backward-compatible alias — existing code importing GoldilocksSignal
# will still work during the transition period.
class SpikeReversionSignal(BaseSignal):
    """
    Spike Reversion Signal (formerly "Goldilocks"): spike-direction asymmetry.

    Physical reasoning:
    - Spike up → reversion down: transient solar insolation peaks,
      warm-air advection pulses. Reversion is strong and reliable because the
      forcing is ephemeral. Higher confidence base.
    - Spike down → reversion up: transient cold-air drainage,
      evaporative cooling from precipitation. Reversion is weaker and less
      reliable — cold-air drainage can persist, evaporative cooling can be
      sustained if ground remains wet. Lower confidence base + discount.
    """

    # Legacy alias — callers that reference GoldilocksSignal still resolve.
    # Will be removed after full migration.

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self._load_signal_state(db_path)

    @property
    def name(self) -> str:
        return "spike_reversion"

    @property
    def min_lookback(self) -> int:
        return 2  # Requires at least 2 prior days

    def _load_signal_state(self, db_path: str) -> None:
        """Load signal state for epoch tracking."""
        self._signal_state = {}
        if db_path:
            try:
                conn = get_sqlite_connection(db_path)
                cur = conn.cursor()
                # Accept both old goldilocks_tracker and new spike_reversion_tracker keys
                cur.execute("""
                    SELECT signal_name, signal_data FROM signal_state
                    WHERE signal_name LIKE 'spike_reversion_tracker:%'
                       OR signal_name LIKE 'goldilocks_tracker:%'
                """)
                for row in cur.fetchall():
                    signal_name, signal_data = row
                    # Parse: spike_reversion_tracker:STATION:EPOCH or goldilocks_tracker:STATION:EPOCH
                    parts = signal_name.split(':')
                    if len(parts) == 3:
                        station, epoch_id = parts[1], parts[2]
                        if epoch_id not in self._signal_state:
                            self._signal_state[epoch_id] = {}
                        self._signal_state[epoch_id][station] = self._parse_signal_data(signal_data)
                conn.close()
            except Exception:
                pass

    def _parse_signal_data(self, data: str) -> Dict[str, Any]:
        """Parse signal state JSON string."""
        try:
            import json
            return json.loads(data)
        except Exception:
            return {}

    @validate_signal
    def evaluate(self, idx: int, days: List[dict], market_type: str = 'HIGH') -> Tuple[Optional[str], float]:
        """
        Evaluate Spike Reversion signal at day index `idx`.

        Supports both HIGH and LOW market detection (A2-lane).

        This signal looks for brief deviations from the prevailing trend that
        revert quickly. The key insight from Gray Room Round 4 is that spike
        direction matters:

        HIGH market (spike up → reversion down):
          - Uses daily highs for spike detection
          - Spike up means temperature jumped above the previous day's high
          - Reversion predicts the temperature will drop back
          - Base threshold: 2.0°F, base confidence: 0.40

        LOW market (spike down → reversion up):
          - Uses daily lows for spike detection
          - Spike down means temperature dropped below the previous day's low
          - Reversion predicts the temperature will bounce back
          - Base threshold: 1.5°F (lows are less volatile), base confidence: 0.30
            (downward spikes are less reliable than upward spikes)

        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure
            market_type: 'HIGH' or 'LOW' — determines which temperature field
                         to use for spike detection

        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.

            For spike_reversion:
            - 'down' direction means: spike up, now reverting down
            - 'up' direction means: spike down, now reverting up
        """
        if len(days) < self.min_lookback + 1:
            return None, 0.0

        if idx < 2:
            return None, 0.0

        if market_type == 'HIGH':
            return self._evaluate_high(idx, days)
        elif market_type == 'LOW':
            return self._evaluate_low(idx, days)
        else:
            return None, 0.0

    def _evaluate_high(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Evaluate spike reversion for HIGH market (up-spike detection)."""
        # Get prior day high data
        yesterday_val = _safe_get(days, idx - 1, 'high')
        day_before_val = _safe_get(days, idx - 2, 'high')

        if day_before_val is None or yesterday_val is None:
            return None, 0.0

        temp_change = yesterday_val - day_before_val

        # HIGH market spike threshold: 2.0°F
        SPIKE_THRESHOLD_HIGH = 2.0
        if abs(temp_change) < SPIKE_THRESHOLD_HIGH:
            return None, 0.0

        if temp_change > 0:
            # Spike up → reversion down
            base = 0.40
            direction = 'down'
            margin = max(0.0, temp_change / 5.0)
            confidence = base + margin * 0.15
            confidence = min(1.0, confidence)
            return direction, confidence

        return None, 0.0

    def _evaluate_low(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Evaluate spike reversion for LOW market (down-spike detection)."""
        # Get prior day low data
        yesterday_val = _safe_get(days, idx - 1, 'low')
        day_before_val = _safe_get(days, idx - 2, 'low')

        if day_before_val is None or yesterday_val is None:
            return None, 0.0

        temp_change = yesterday_val - day_before_val

        # LOW market spike threshold: 1.5°F (lows are less volatile than highs)
        SPIKE_THRESHOLD_LOW = 1.5
        if abs(temp_change) < SPIKE_THRESHOLD_LOW:
            return None, 0.0

        if temp_change < 0:
            # Spike down → reversion up
            base = 0.30
            direction = 'up'
            margin = max(0.0, abs(temp_change) / 4.0)
            confidence = base + margin * 0.10
            # Apply 15% discount for downward spike uncertainty
            confidence *= 0.85
            confidence = min(1.0, confidence)
            return direction, confidence

        return None, 0.0

    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None, market_type: str = 'HIGH') -> Tuple[Optional[str], float]:
        """
        Evaluate Spike Reversion signal for a specific station and date using DB data.

        Loads daily data from metar_observations and delegates to evaluate().

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB
            market_type: 'HIGH' or 'LOW' — determines which temperature field
                         to use for spike detection

        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = get_sqlite_connection(self.db_path) if self.db_path else None
            if conn is None:
                return None, 0.0

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                       AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                       AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                       AVG(pressure_mb) as pressure
                FROM metar_observations
                WHERE station=? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
                GROUP BY date_utc ORDER BY date_utc ASC
            """, (station,))
            days = []
            for r in cur.fetchall():
                if any(v is None for v in r[1:]):
                    continue
                days.append({
                    'date': r[0], 'high': r[1], 'low': r[2], 'dpoint': r[3],
                    'temp': r[4], 'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
                })

            # Find the index for the target date
            target_idx = None
            for i, d in enumerate(days):
                if d['date'] == date:
                    target_idx = i
                    break

            if target_idx is None:
                return None, 0.0

            return self.evaluate(target_idx, days, market_type=market_type)
        finally:
            if own_conn and conn:
                conn.close()


# Backward-compatible alias
GoldilocksSignal = SpikeReversionSignal


def create_spike_reversion_signal(db_path: str) -> SpikeReversionSignal:
    """Factory function to create a Spike Reversion signal instance."""
    return SpikeReversionSignal(db_path)


# Backward-compatible factory alias
create_goldilocks_signal = create_spike_reversion_signal