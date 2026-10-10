#!/usr/bin/env python3
"""
gamma_signals.py — Gamma signal family (G1-G6) for ensemble augmentation

Implements 6 distinct Gamma signal classes designed to complement the 
82-member ensemble by providing orthogonal information channels.
"""

import os
import math
import sqlite3
import struct
import numpy as np
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta

from core.signals.base_signal import BaseSignal, validate_signal


class GammaBaseSignal(BaseSignal):
    """
    Base class for all Gamma signals.
    Provides common functionality for ensemble-augmentation signals.
    """

    #: Default GEFS archive (ensemble members) — repo-root relative.
    GEFS_DB = os.path.join("data", "gefs_archive.db")

    def __init__(self, db_path=None, **kwargs):
        super().__init__(db_path)
        self.signal_name = "gamma_base"
        self.gefs_db = kwargs.get("gefs_db") or self.GEFS_DB
        self._gefs_conn = None
        # Platt calibration (WS-J 2026-09-27). Subclasses set _platt_A/_platt_B
        # from a walk-forward fit; _calibrate maps raw confidence to a value
        # that tracks realised accuracy. Defaults are identity-ish (no-op).
        self._platt_A = kwargs.get("platt_A", 1.0)
        self._platt_B = kwargs.get("platt_B", 0.0)

    def _calibrate(self, raw_conf: float) -> float:
        """Apply fitted Platt scaling so confidence tracks realised accuracy.

        P = sigmoid(A*(raw_conf - 0.5) + B).
        """
        xs = raw_conf - 0.5
        z = self._platt_A * xs + self._platt_B
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))
        
    @property
    def min_lookback(self) -> int:
        return 1  # Most Gamma signals require at least 1 day of history
        
    def _get_station_data(self, days: List[dict], idx: int, field: str):
        """Helper to safely retrieve field from days[idx]."""
        if idx < len(days) and days[idx] is not None:
            return days[idx].get(field)
        return None

    def _get_gefs_conn(self):
        """Cached read connection to the GEFS archive (None if unavailable)."""
        if self._gefs_conn is None:
            try:
                self._gefs_conn = sqlite3.connect(self.gefs_db)
            except sqlite3.Error:
                self._gefs_conn = None
        return self._gefs_conn

    def _load_gefs_members(
        self, station: str, target_date: str, step: int = 24
    ) -> np.ndarray:
        """Load GEFS ensemble member temperatures (°C) for a station/date.

        `gefs_archive.member_values` stores signed int8 offsets (×0.1 °C) from
        `ensemble_mean`. Returns an empty array when the row, blob, or member
        count is unusable (caller falls back gracefully).
        """
        conn = self._get_gefs_conn()
        if conn is None or not station or not target_date:
            return np.array([])
        try:
            row = conn.execute(
                "SELECT ensemble_mean, member_values, n_members FROM gefs_archive "
                "WHERE target_date=? AND station=? AND step=?",
                (target_date, station, step),
            ).fetchone()
            if not row:
                return np.array([])
            mean, blob, n = row
            if mean is None or not blob or n is None or n < 2 or len(blob) < n:
                return np.array([])

            offsets = struct.unpack(f"{n}b", blob[:n])
            return np.array([mean + o * 0.1 for o in offsets], dtype=float)
        except (sqlite3.Error, struct.error, TypeError, ValueError):
            return np.array([])


class G1_UncertaintySignal(GammaBaseSignal):
    """
    G1: Ensemble Uncertainty Modulator
    
    Measures the disagreement/uncertainty within the 82-member ensemble
    and modulates confidence accordingly.
    """
    
    def __init__(self, db_path=None, uncertainty_threshold=0.15, **kwargs):
        super().__init__(db_path, **kwargs)
        self.uncertainty_threshold = uncertainty_threshold
        self.signal_name = "gamma_g1_uncertainty"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit on raw wave3 data:
        # raw conf mean 0.9306 -> calibrated 0.5459 (actual acc 0.5458, n=1594).
        self._platt_A = 0.5707
        self._platt_B = -0.0617
        
    @property
    def name(self) -> str:
        return "gamma_g1_uncertainty"
        
    @property
    def min_lookback(self) -> int:
        return 0  # Uses ensemble data, not historical
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Ensemble-spread (uncertainty) signal.

        Real signal (replaces the prior np.random stub):
          - Loads the GEFS ensemble members for this station/date.
          - Direction = the ensemble's lean vs the prior day's high
            (majority of members warmer than yesterday -> UP; cooler -> DOWN).
          - Confidence = member agreement, DAMPED by ensemble spread (std).
            A tight ensemble raises confidence; a wide one lowers it.
        Returns (None, 0.0) when the ensemble row is unavailable or the vote is
        perfectly split (no signal).
        """
        if idx < 1 or idx >= len(days):
            return None, 0.0
        station = days[idx].get("station")
        target_date = days[idx].get("date")
        ref = days[idx - 1].get("high")
        if not station or not target_date or ref is None:
            return None, 0.0

        members_c = self._load_gefs_members(station, target_date, step=24)
        if members_c.size < 3:
            return None, 0.0
        members_f = members_c * 9.0 / 5.0 + 32.0

        frac_up = float((members_f > ref).mean())
        if frac_up == 0.5:
            return None, 0.0  # perfectly split ensemble -> no directional call
        direction = "up" if frac_up > 0.5 else "down"
        agreement = max(frac_up, 1.0 - frac_up)  # 0.5 .. 1.0

        # Spread damping: std of members (deg F) above the configured threshold
        # reduces confidence; a tight ensemble keeps it high.
        spread_f = float(members_f.std())
        over = max(0.0, spread_f - self.uncertainty_threshold * 10.0)
        raw_conf = agreement - min(0.15, over * 0.01)
        raw_conf = float(min(0.95, max(0.5, raw_conf)))
        # WS-J: calibrate so confidence tracks realised accuracy (was ~0.93 vs 0.55).
        return direction, self._calibrate(raw_conf)


class G2_RegimeSignal(GammaBaseSignal):
    """
    G2: Dynamic Regime Classifier
    
    Identifies current weather regime and adjusts ensemble weights accordingly.
    """
    
    def __init__(self, db_path=None, regime_window=7, **kwargs):
        super().__init__(db_path, **kwargs)
        self.regime_window = regime_window
        self.signal_name = "gamma_g2_regime"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit:
        # raw conf 0.7216 -> 0.4042 (actual acc 0.4042, n=1566).
        self._platt_A = -1.9660
        self._platt_B = 0.0438
        
    @property
    def name(self) -> str:
        return "gamma_g2_regime"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Classify regime from recent temperature patterns.

        LEAKAGE CONTRACT (2026-09-27): reads only days[idx-1] and earlier.
        Previously the window started at days[idx] (the target day).
        """
        if idx < self.regime_window + 1:
            return None, 0.0

        # Extract recent temperature data ending at the last COMPLETED day.
        recent_temps = []
        for i in range(self.regime_window):
            pos = idx - 1 - i
            if pos >= 0:
                temp = self._get_station_data(days, pos, 'high')
                if temp is not None:
                    recent_temps.append(temp)

        if len(recent_temps) < 3:
            return None, 0.0

        trend = recent_temps[0] - recent_temps[-1]  # Latest vs oldest
        volatility = np.std(recent_temps)

        if volatility < 2.0:  # Stable regime
            direction = 'up' if trend > 0 else 'down'
            confidence = min(0.7, 0.3 + abs(trend) * 0.1)
        elif abs(trend) < 1.0:  # Oscillating regime
            direction = 'down'  # Expect reversion in oscillating regime
            confidence = 0.6
        else:  # Trending regime
            direction = 'up' if trend > 0 else 'down'
            confidence = min(0.8, 0.4 + abs(trend) * 0.15)

        confidence = float(min(0.8, max(0.5, confidence)))
        # WS-J: calibrate so confidence tracks realised accuracy.
        return direction, self._calibrate(confidence)


class G3_AnalogSignal(GammaBaseSignal):
    """
    G3: Historical Analog Matcher

    Finds historical days whose *prior-day pattern* resembles the current
    prior-day pattern, then predicts from those analogs' realized next-day
    outcomes.

    LEAKAGE CONTRACT (2026-09-27 rewrite):
      - The target day is `days[idx]`. This signal MUST NOT read any field of
        `days[idx]` (that was the old tautology bug: it compared the target
        day's own high to the prior day, i.e. the exact settlement quantity).
      - Only days[0 .. idx-1] are read. The current pattern uses days[idx-1],
        days[idx-2], ... and every analog's outcome is realised strictly before
        the target (analog end index j satisfies j+1 <= idx-1).
    """

    def __init__(self, db_path=None, analog_window=30, min_matches=5,
                 pattern_len=3, k_neighbors=10, **kwargs):
        super().__init__(db_path, **kwargs)
        self.analog_window = analog_window
        self.min_matches = min_matches
        self.pattern_len = pattern_len
        self.k_neighbors = k_neighbors
        self.signal_name = "gamma_g3_analog"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit:
        # raw conf 0.6708 -> 0.6042 (actual acc 0.6045, n=1168).
        self._platt_A = 1.0409
        self._platt_B = 0.2467

    @property
    def name(self) -> str:
        return "gamma_g3_analog"

    def _pattern_at(self, days, end_idx, length):
        """[high(end_idx), high(end_idx-1), ...] or None if OOB/None."""
        if end_idx - length + 1 < 0:
            return None
        vals = []
        for k in range(length):
            v = self._get_station_data(days, end_idx - k, 'high')
            if v is None:
                return None
            vals.append(v)
        return vals

    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """k-NN analog vote using ONLY data before the target day (idx)."""
        L = self.pattern_len
        # Need history to build the current pattern (ends at idx-1) and at least
        # a few analog candidates with a realised outcome before the target.
        if idx < self.analog_window or idx - 1 < L:
            return None, 0.0

        current = self._pattern_at(days, idx - 1, L)
        if current is None:
            return None, 0.0

        # Candidates: end index j with j+1 <= idx-1 (outcome strictly before target)
        analogs = []  # (distance, outcome_dir)
        for j in range(L - 1, idx - 1):
            pat = self._pattern_at(days, j, L)
            if pat is None:
                continue
            nxt = self._get_station_data(days, j + 1, 'high')
            cur_j = self._get_station_data(days, j, 'high')
            if nxt is None or cur_j is None:
                continue
            if nxt == cur_j:
                continue  # flat outcome carries no directional info
            dist = sum(abs(a - b) for a, b in zip(current, pat))
            analogs.append((dist, 'up' if nxt > cur_j else 'down'))

        if len(analogs) < self.min_matches:
            return None, 0.0

        analogs.sort(key=lambda x: x[0])
        kk = analogs[: self.k_neighbors]
        # Inverse-distance weighted vote (eps guards dist==0)
        up_w = sum(1.0 / (d + 1e-6) for d, o in kk if o == 'up')
        dn_w = sum(1.0 / (d + 1e-6) for d, o in kk if o == 'down')
        total = up_w + dn_w
        if total <= 0:
            return None, 0.0
        direction = 'up' if up_w >= dn_w else 'down'
        agreement = max(up_w, dn_w) / total  # 0.5 .. 1.0
        # Confidence: analog agreement scaled into a sane band.
        confidence = float(min(0.85, max(0.5, agreement)))
        # WS-J: calibrate so confidence tracks realised accuracy.
        return direction, self._calibrate(confidence)


class G4_PressureAdvectionSignal(GammaBaseSignal):
    """
    G4: Pressure-Based Advection Indicator
    
    Uses pressure gradients and wind patterns to predict temperature movement.
    """
    
    def __init__(self, db_path=None, pressure_threshold=2.0, **kwargs):
        super().__init__(db_path, **kwargs)
        self.pressure_threshold = pressure_threshold
        self.signal_name = "gamma_g4_pressure_adv"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit:
        # raw conf 0.7868 -> 0.4685 (actual acc 0.4686, n=685).
        self._platt_A = -0.9971
        self._platt_B = 0.1594
        
    @property
    def name(self) -> str:
        return "gamma_g4_pressure_adv"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Pressure-change advection signal.

        LEAKAGE CONTRACT (2026-09-27): reads only days[idx-1] and earlier.
        Previously current = days[idx] (the target day).
        """
        cur = idx - 1  # last completed day
        if cur < 1:
            return None, 0.0

        current_press = self._get_station_data(days, cur, 'pressure')
        prev_press = self._get_station_data(days, cur - 1, 'pressure')

        if current_press is None or prev_press is None:
            return None, 0.0

        pressure_change = current_press - prev_press

        # Falling pressure often indicates warmer air approaching (low pressure systems)
        # Rising pressure often indicates cooler air (high pressure systems)
        if abs(pressure_change) >= self.pressure_threshold:
            if pressure_change < 0:  # Pressure falling
                direction = 'up'  # Expect warming
                confidence = 0.6 + min(0.3, abs(pressure_change) * 0.05)
            else:  # Pressure rising
                direction = 'down'  # Expect cooling
                confidence = 0.6 + min(0.3, pressure_change * 0.05)
            return direction, self._calibrate(float(min(0.9, max(0.5, confidence))))
        # Small pressure change - no clear direction
        return None, 0.0


class G5_DewpointSignal(GammaBaseSignal):
    """
    G5: Dewpoint Depression Indicator
    
    Uses humidity information to predict temperature outcomes.
    """
    
    def __init__(self, db_path=None, dewpoint_threshold=10.0, **kwargs):
        super().__init__(db_path, **kwargs)
        self.dewpoint_threshold = dewpoint_threshold
        self.signal_name = "gamma_g5_dewpoint"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit:
        # raw conf 0.7257 -> 0.4657 (actual acc 0.4656, n=844).
        self._platt_A = -0.2585
        self._platt_B = -0.0790
        
    @property
    def name(self) -> str:
        return "gamma_g5_dewpoint"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Dewpoint depression signal.

        LEAKAGE CONTRACT (2026-09-27): reads only days[idx-1] and earlier.
        Previously this read days[idx] (the target day) — the exact settlement
        quantity — producing a look-ahead leak.
        """
        cur = idx - 1  # last completed day
        if cur < 1:
            return None, 0.0

        temp = self._get_station_data(days, cur, 'temp')
        dewpoint = self._get_station_data(days, cur, 'dewpoint')

        if temp is None or dewpoint is None:
            return None, 0.0

        dewpoint_dep = temp - dewpoint  # Dewpoint depression

        # Higher dewpoint depression (drier air) can lead to larger temp swings
        if dewpoint_dep > self.dewpoint_threshold:
            prev_temp = self._get_station_data(days, cur - 1, 'temp')
            if prev_temp is None:
                return None, 0.0
            trend = temp - prev_temp
            if trend == 0:
                return None, 0.0
            direction = 'up' if trend > 0 else 'down'
            confidence = 0.55 + min(0.25, dewpoint_dep * 0.01)
            return direction, self._calibrate(float(min(0.85, max(0.5, confidence))))
        # Moist air - no clear direction
        return None, 0.0


class G6_CloudCoverSignal(GammaBaseSignal):
    """
    G6: Cloud Cover Radiational Effect
    
    Uses cloud information to predict temperature outcomes based on radiational cooling/heating.
    """
    
    def __init__(self, db_path=None, cloud_threshold=50.0, **kwargs):
        super().__init__(db_path, **kwargs)
        self.cloud_threshold = cloud_threshold
        self.signal_name = "gamma_g6_cloud_cover"
        # Platt scaling (WS-J 2026-09-27), walk-forward fit:
        # raw conf 0.7913 -> 0.4882 (actual acc 0.4881, n=928).
        self._platt_A = -0.0525
        self._platt_B = -0.0320
        
    @property
    def name(self) -> str:
        return "gamma_g6_cloud_cover"
        
    @validate_signal
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """Use cloud cover to predict temperature outcomes.

        LEAKAGE CONTRACT (2026-09-27): reads only days[idx-1] and earlier (the
        last completed day); the target day days[idx] is never read. Previously
        this method also fell through without returning, so it never fired.
        """
        if idx < 2:
            return None, 0.0

        cur = idx - 1  # last completed day
        cloud_cover = self._get_station_data(days, cur, 'cloud_cover')

        # If no cloud data, try sky condition or visibility as proxy
        if cloud_cover is None:
            sky_condition = self._get_station_data(days, cur, 'sky_condition')
            visibility = self._get_station_data(days, cur, 'visibility')
            if sky_condition:
                condition_map = {
                    'CLR': 0, 'SKC': 0, 'NSC': 0, 'NCD': 0,  # Clear
                    'FEW': 15, 'SCT': 40,  # Partly cloudy
                    'BKN': 80, 'VV': 95,  # Mostly cloudy
                    'OVC': 100  # Overcast
                }
                cloud_cover = condition_map.get(sky_condition.upper(), 50)
            elif visibility is not None:
                cloud_cover = max(0, min(100, 100 - visibility * 2))
            else:
                return None, 0.0

        # Clear skies -> stronger radiational trend continuation.
        if cloud_cover < self.cloud_threshold:
            current_temp = self._get_station_data(days, cur, 'temp')
            prev_temp = self._get_station_data(days, cur - 1, 'temp')
            if current_temp is None or prev_temp is None:
                return None, 0.0
            trend = current_temp - prev_temp
            if trend == 0:
                return None, 0.0
            direction = 'up' if trend > 0 else 'down'
            confidence = 0.55 + (self.cloud_threshold - cloud_cover) * 0.008
            return direction, self._calibrate(float(min(0.85, max(0.5, confidence))))
        # Cloudy skies -> no clear direction.
        return None, 0.0


# Convenience function to get all Gamma signals
def get_gamma_signals(db_path=None):
    """
    Returns a list of all Gamma signal instances.
    """
    return [
        G1_UncertaintySignal(db_path),
        G2_RegimeSignal(db_path),
        G3_AnalogSignal(db_path),
        G4_PressureAdvectionSignal(db_path),
        G5_DewpointSignal(db_path),
        G6_CloudCoverSignal(db_path)
    ]


if __name__ == "__main__":
    # Test the Gamma signals
    print("Testing Gamma signal classes:")
    
    gamma_signals = get_gamma_signals()
    
    for i, signal in enumerate(gamma_signals, 1):
        print(f"G{i}: {signal.name}")
        print(f"  Min lookback: {signal.min_lookback}")
        print(f"  Instance type: {type(signal).__name__}")
    
    print("\nAll Gamma signals created successfully!")