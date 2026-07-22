#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-20 Fix Phase 2 audit bugs: Goldilocks NameError, Wind Direction Shift look-ahead]
# 2. [2026-07-18 Fix Bug 1: Goldilocks look-ahead bias - use days[idx-1]/idx-2 instead of idx/idx-1]
# 3. [2026-07-16 T9: Build 5 Tier 1 signals + combinatorial backtest harness]
# 4. [2026-07-16 T2: Remove 4 dead signals from all code paths]
#

"""
Goldilocks Signal Module

Implements the Goldilocks (C5) spike-direction asymmetry signal with:
- C5_up: Spike up → reversion down (is_down=False)
- C5_down: Spike down → reversion up (is_down=True)

Based on asymmetric confidence scoring from Gray Room Round 4-1.5:
- Up reversion (spike up → drop back): base 0.40, more reliable
- Down reversion (spike down → bounce back): base 0.25, less reliable
"""

from .base_signal import BaseSignal, _safe_get, validate_signal
from typing import Optional, Tuple, Dict, Any, List
import sqlite3
import math


class GoldilocksSignal(BaseSignal):
    """
    Goldilocks Signal: Spike-direction asymmetry exploitation.
    
    Physical reasoning:
    - Spike up → reversion down: transient solar insolation peaks,
      warm-air advection pulses. Reversion is strong and reliable because the
      forcing is ephemeral. Higher confidence base.
    - Spike down → reversion up: transient cold-air drainage,
      evaporative cooling from precipitation. Reversion is weaker and less
      reliable — cold-air drainage can persist, evaporative cooling can be
      sustained if ground remains wet. Lower confidence base + discount.
    """
    
    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self._load_signal_state(db_path)

    @property
    def name(self) -> str:
        return "goldilocks"

    @property
    def min_lookback(self) -> int:
        return 2  # Requires at least 2 prior days
    
    def _load_signal_state(self, db_path: str) -> None:
        """Load signal state for epoch tracking."""
        self._signal_state = {}
        if db_path:
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("""
                    SELECT signal_name, signal_data FROM signal_state
                    WHERE signal_name LIKE 'goldilocks_tracker:%'
                """)
                for row in cur.fetchall():
                    signal_name, signal_data = row
                    # Parse: goldilocks_tracker:STATION:EPOCH
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
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate Goldilocks signal at day index `idx`.
        
        This signal looks for "Goldilocks" spikes - brief deviations from the
        prevailing trend that revert quickly. The key insight from Gray Room
        Round 4 is that spike direction matters:
        
        - Spike UP → Reversion DOWN (is_down=False): Stronger, more reliable
          (solar/thermal pulses that burn off quickly)
        - Spike DOWN → Reversion UP (is_down=True): Weaker, less reliable
          (cold-air drainage can persist, evaporative cooling can sustain)
        
        Args:
            idx: Current day index in the `days` list
            days: List of daily weather dicts with keys:
                date, high, low, dewpoint, temp, wind_dir, wind_speed, pressure
        
        Returns:
            (direction, confidence) where direction is 'up' or 'down',
            or (None, 0.0) if signal does not fire.
            
            For goldilocks:
            - 'down' direction means: spike up, now reverting down
            - 'up' direction means: spike down, now reverting up
        """
        if len(days) < self.min_lookback + 1:
            return None, 0.0

        if idx < 2:
            return None, 0.0

        # Get prior day data using _safe_get — no look-ahead
        # Use days[idx-1] (yesterday's high) and days[idx-2] (day before)
        # to detect a spike that has already completed
        yesterday_high = _safe_get(days, idx - 1, 'high')
        day_before_high = _safe_get(days, idx - 2, 'high')

        if day_before_high is None or yesterday_high is None:
            return None, 0.0

        # Calculate temperature change using only prior-day data
        temp_change = yesterday_high - day_before_high

        # Determine if we're seeing a spike (rapid deviation)
        spike_threshold = 2.0
        is_spike = abs(temp_change) >= spike_threshold

        if not is_spike:
            return None, 0.0

        is_spike_up = temp_change > 0
        is_spike_down = temp_change < 0

        # Confidence scoring with asymmetric base values
        # Per Gray Room Round 4-1.5:
        # - Up reversion (spike up → drop): base 0.40
        # - Down reversion (spike down → bounce): base 0.25

        if is_spike_up:
            base = 0.40
            direction = 'down'
            daily_high_margin = max(0.0, temp_change / 5.0)
            confidence = base + daily_high_margin * 0.15
            confidence = min(1.0, confidence)
            return direction, confidence

        elif is_spike_down:
            base = 0.25
            direction = 'up'
            daily_low_margin = max(0.0, abs(temp_change) / 5.0)
            confidence = base + daily_low_margin * 0.10
            confidence *= 0.85
            confidence = min(1.0, confidence)
            return direction, confidence

        return None, 0.0
    
    def evaluate_for_station(self, station: str, date: str, conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate Goldilocks signal for a specific station and date using DB data.
        
        Loads daily data from metar_observations and delegates to evaluate().
        
        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB
        
        Returns:
            (direction, confidence) or (None, 0.0)
        """
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path) if self.db_path else None
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
                    'date': r[0], 'high': r[1], 'low': r[2], 'dewpoint': r[3],
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
            
            return self.evaluate(target_idx, days)
        finally:
            if own_conn and conn:
                conn.close()


def create_goldilocks_signal(db_path: str) -> GoldilocksSignal:
    """Factory function to create a Goldilocks signal instance."""
    return GoldilocksSignal(db_path)
